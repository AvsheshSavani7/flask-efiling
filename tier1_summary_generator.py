#!/usr/bin/env python3
"""
Tier1 Summary Generator
========================
Generates only tier1_summary from metadata and text using the same prompt as docket_entry_analyzer.
Handles long content via truncation or chunked summarization.
Saves to MongoDB: hash_id, metadata, content (full extracted text), summary,
original_content_length, summary_length.
"""

import os
import hashlib
from typing import Dict, Any, Optional
from datetime import datetime, timezone

import anthropic
from openai import OpenAI
from docket_entry_analyzer import (
    convert_date_to_datetime,
    COMPREHENSIVE_SUMMARY_MODEL,
    _generate_comprehensive_summary_with_file_upload,
)
from pymongo import MongoClient

ENV_FILE = ".env"
TIER1_MODEL = "claude-haiku-4-5-20251001"

# Claude 3 Haiku context ~200k tokens (~800k chars). Use conservative limits.
MAX_CONTENT_CHARS = 350_000
TRUNCATE_HEAD_CHARS = 280_000
TRUNCATE_TAIL_CHARS = 70_000
CHUNK_CHARS = 300_000
COLLECTION_NAME = "docket"


def _load_env_file(env_path: str) -> None:
    """Load environment variables from .env file"""
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                os.environ[key] = value


def _normalize_date_for_storage(date_val: Any) -> Any:
    """
    Normalize date for MongoDB: return timezone-aware UTC datetime when parseable,
    otherwise return original string or "N/A". Stored as BSON Date for date-wise filters.
    """
    if date_val is None or date_val == "":
        return "N/A"
    if isinstance(date_val, datetime):
        if date_val.tzinfo is None:
            return date_val.replace(tzinfo=timezone.utc)
        return date_val.astimezone(timezone.utc)
    if not isinstance(date_val, str):
        return "N/A"
    dt = convert_date_to_datetime(date_val)
    return dt if dt is not None else date_val


def _build_query_filter(metadata: Dict[str, Any]) -> dict:
    """
    Same query_filter as docket_entry_analyzer: by docket_type and docket_number.
    Used to count existing entries in this docket so next_entry_number = len(all_entries) + 1.
    """
    docket_type = metadata.get("docket_type", "N/A")
    docket_number = metadata.get("docket_number", "N/A")
    query_filter = {}
    if docket_type and docket_type != "N/A":
        query_filter["metadata.docket_type"] = docket_type
    if docket_number and docket_number != "N/A":
        docket_number_values = [docket_number]
        try:
            if isinstance(docket_number, str):
                try:
                    docket_number_values.append(int(docket_number))
                except ValueError:
                    pass
                try:
                    docket_number_values.append(float(docket_number))
                except ValueError:
                    pass
            elif isinstance(docket_number, (int, float)):
                docket_number_values.append(str(docket_number))
        except (ValueError, TypeError):
            pass
        if len(docket_number_values) > 1:
            query_filter["metadata.docket_number"] = {
                "$in": docket_number_values}
        else:
            query_filter["metadata.docket_number"] = docket_number
    return query_filter


def _tier1_prompt(entry_number: int, entry_metadata: Dict[str, Any], content: str) -> str:
    """Same prompt as docket_entry_analyzer tier1."""
    return f"""You are extracting key facts from a legal docket entry. Be concise and factual.

ENTRY METADATA:
Entry Number: {entry_number}
Type: {entry_metadata.get('document_type', 'N/A')}
Date: {entry_metadata.get('date', 'N/A')}
Filed By: {entry_metadata.get('on_behalf_of', 'N/A')}
Info: {entry_metadata.get('additional_info', 'N/A')}

CONTENT:
{content}

Extract the key facts in 3-5 bullet points (max 500 words total):
- What type of filing is this?
- Who filed it and what do they want?
- What are the main arguments/concerns raised?
- Any commitments, recommendations, or conclusions?

Be factual and concise. Focus on substantive content, not procedural details."""


def _call_tier1(client: anthropic.Anthropic, prompt: str) -> tuple[str, int, int]:
    """Call Claude for tier1 summary. Returns (summary_text, input_tokens, output_tokens)."""
    msg = client.messages.create(
        model=TIER1_MODEL,
        max_tokens=1000,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = msg.content[0].text.strip()
    return summary, msg.usage.input_tokens, msg.usage.output_tokens


def _comprehensive_summary_prompt(
    next_entry_number: int, entry_metadata: Dict[str, str], full_text: str
) -> str:
    """Same prompt as docket_entry_analyzer for comprehensive summary."""
    return f"""You are summarizing a legal docket entry for further analysis. Create a comprehensive summary that preserves all important details.

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

FULL DOCUMENT CONTENT:
{full_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a COMPREHENSIVE SUMMARY that will be used for further legal analysis. Include:

1. Document type and purpose
2. All parties involved and their positions
3. All key arguments, claims, and concerns raised
4. Any evidence, data, or exhibits referenced
5. Procedural requests or recommendations
6. Any commitments, conditions, or proposed remedies
7. Legal citations or regulatory references
8. Timeline information or deadlines mentioned

Be thorough and detailed. Preserve specific facts, numbers, names, and legal arguments. 
This summary must contain enough detail for downstream analysis of legal significance and risk assessment.

In summary with other details, you must include the following:
- Filing: Deal Name or Parties
- Type: filing type
- Summary: 1-2 sentence content summary
- Relevance: High/Medium/Low - short justification

Target length: 1000-2000 words depending on complexity."""


def _get_comprehensive_summary_for_tier1(
    openai_client: OpenAI,
    full_text: str,
    entry_metadata: Dict[str, str],
    next_entry_number: int,
) -> str:
    """
    Generate comprehensive summary via OpenAI for use as tier1 input when context is too long.
    Tries direct COMPREHENSIVE_SUMMARY_MODEL first; on token limit, uses file upload.
    Returns the summary text.
    """
    prompt = _comprehensive_summary_prompt(
        next_entry_number, entry_metadata, full_text
    )
    try:
        print("Generating comprehensive summary (direct)...")
        msg = openai_client.chat.completions.create(
            model=COMPREHENSIVE_SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = msg.choices[0].message.content.strip()
        print(f"✓ Comprehensive summary: {len(summary):,} chars")
        return summary
    except Exception as direct_error:
        err_str = str(direct_error)
        if (
            "context_length_exceeded" in err_str
            or "tokens exceed" in err_str.lower()
            or "string too long" in err_str.lower()
        ):
            print(
                f"⚠ Direct comprehensive summary failed (token limit): {err_str[:200]}")
            print("Using file upload approach for comprehensive summary...")
            estimated_tokens = len(full_text) // 4
            data = _generate_comprehensive_summary_with_file_upload(
                openai_client=openai_client,
                full_text=full_text,
                entry_metadata=entry_metadata,
                estimated_tokens=estimated_tokens,
                next_entry_number=next_entry_number,
            )
            return data["summary"]
        raise


def generate_tier1_summary(
    metadata: Dict[str, Any],
    text: str,
    hash_id: Optional[int] = None,
    entry_number: int = 1,
    test_mode: bool = False,
) -> Dict[str, Any]:
    """
    Generate tier1 summary from metadata and text. Handles long content.
    Saves to MongoDB: hash_id, metadata, content (extracted text), summary,
    original_content_length, summary_length.

    Args:
        metadata: Dict with keys such as date, document_type, additional_info, on_behalf_of,
                  document_id, docket_number, docket_type (optional).
        text: Full text content to summarize.
        hash_id: Optional integer; if not provided, next id from collection (same as docket_entry_analyzer).
        entry_number: Fallback when no MongoDB (e.g. test_mode); otherwise computed as len(all_entries)+1
                      using same query_filter as docket_entry_analyzer (docket_type, docket_number).
        test_mode: If True, do not save to MongoDB.

    Returns:
        Dict with summary, original_content_length, summary_length, hash_id, metadata,
        and status (saved/skipped/error).
    """
    _load_env_file(ENV_FILE)

    api_key = os.environ.get(
        "CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "error": "Anthropic API key not found",
            "metadata": {**metadata, "date": _normalize_date_for_storage(metadata.get("date", "N/A"))},
        }

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    mongodb_db_name = (os.environ.get("MONGODB_DATABASE_NAME") or "").strip()
    if not mongodb_uri and not test_mode:
        return {
            "error": "MongoDB connection string not found in .env",
            "metadata": {**metadata, "date": _normalize_date_for_storage(metadata.get("date", "N/A"))},
        }

    def _get_db(client: MongoClient):
        if mongodb_db_name:
            return client.get_database(mongodb_db_name)
        return client.get_database()

    doc_id = metadata.get("document_id")

    # Same as docket_entry_analyzer: next_entry_number = len(all_entries) + 1 using query_filter
    next_entry_number = entry_number
    next_hash_id = hash_id

    if mongodb_uri:
        try:
            mongo_client = MongoClient(mongodb_uri)
            db = _get_db(mongo_client)
            coll = db[COLLECTION_NAME]

            existing = coll.find_one({"metadata.document_id": doc_id})
            if existing and not test_mode:
                # Same as docket_entry_analyzer: skip when entry already exists
                existing.pop("_id", None)
                existing_meta = existing.get("metadata", metadata)
                return {
                    "hash_id": existing.get("hash_id"),
                    "metadata": {**existing_meta, "date": _normalize_date_for_storage(existing_meta.get("date", "N/A"))},
                    "summary": existing.get("summary", ""),
                    "original_content_length": existing.get("original_content_length", 0),
                    "summary_length": existing.get("summary_length", 0),
                    "status": "skipped",
                }

            # Build same query_filter as docket_entry_analyzer for this docket
            query_filter = _build_query_filter(metadata)
            all_entries = list(
                coll.find(query_filter).sort("metadata.date", 1)
            )
            # Next entry number = count of existing entries in this docket + 1 (same as docket_entry_analyzer)
            next_entry_number = len(all_entries) + 1
            if next_hash_id is None:
                if all_entries:
                    next_hash_id = max(
                        e.get("hash_id", 0) for e in all_entries
                        if isinstance(e.get("hash_id"), (int, float))
                    ) + 1
                else:
                    next_hash_id = 1
        except Exception as e:
            if not test_mode:
                return {
                    "error": f"MongoDB error (query): {str(e)}",
                    "metadata": {**metadata, "date": _normalize_date_for_storage(metadata.get("date", "N/A"))},
                }
            next_entry_number = 1
            next_hash_id = hash_id or int(
                hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:8], 16
            )
    else:
        next_hash_id = hash_id or int(
            hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:8], 16
        )

    client = anthropic.Anthropic(api_key=api_key)
    original_length = len(text)

    # Normalize date: store as datetime (BSON Date) in MongoDB for date-wise filters
    normalized_date = _normalize_date_for_storage(metadata.get("date", "N/A"))
    metadata_for_save = {**metadata, "date": normalized_date}

    # For prompt use string (LLM-readable)
    date_for_prompt = (
        normalized_date.isoformat()
        if isinstance(normalized_date, datetime)
        else str(normalized_date)
    )
    # Normalize metadata for prompt (same shape as docket_entry_analyzer entry_metadata)
    entry_metadata = {
        "date": date_for_prompt,
        "document_type": metadata.get("document_type", "N/A"),
        "additional_info": metadata.get("additional_info", "N/A"),
        "on_behalf_of": metadata.get("on_behalf_of", "N/A"),
    }

    # Call LLM: tier1 (Claude). On any Anthropic/API error → comprehensive summary then tier1 → then file upload summary.
    content_for_tier1 = text
    summary = None
    try:
        prompt = _tier1_prompt(
            next_entry_number, entry_metadata, content_for_tier1)
        summary, _, _ = _call_tier1(client, prompt)
    except Exception as anthropic_error:
        # Any Anthropic failure (rate limit, context too long, connection, etc.) → try fallbacks
        print(
            f"⚠ Tier1 failed ({type(anthropic_error).__name__}): {anthropic_error}")
        print("Falling back to comprehensive summary then tier1...")
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not openai_api_key:
            return {
                "error": "Anthropic failed and OPENAI_API_KEY not set for fallback",
                "metadata": metadata_for_save,
            }
        openai_client = OpenAI(api_key=openai_api_key)
        try:
            content_for_tier1 = _get_comprehensive_summary_for_tier1(
                openai_client, text, entry_metadata, next_entry_number
            )
            prompt = _tier1_prompt(
                next_entry_number, entry_metadata, content_for_tier1)
            summary, _, _ = _call_tier1(client, prompt)
        except Exception as fallback_error:
            # Comprehensive summary or second tier1 failed → use file upload summary as final output
            print(
                f"⚠ Fallback (comprehensive + tier1) failed: {fallback_error}")
            print("Falling back to file upload summary as tier1 output...")
            try:
                estimated_tokens = len(text) // 4
                data = _generate_comprehensive_summary_with_file_upload(
                    openai_client=openai_client,
                    full_text=text,
                    entry_metadata=entry_metadata,
                    estimated_tokens=estimated_tokens,
                    next_entry_number=next_entry_number,
                )
                summary = data.get("summary", "").strip() or None
            except Exception as file_upload_error:
                return {
                    "error": f"All fallbacks failed. Last (file upload): {file_upload_error}",
                    "metadata": metadata_for_save,
                }
    if summary is None:
        return {
            "error": "No summary generated",
            "metadata": metadata_for_save,
        }
    summary_length = len(summary)

    # MongoDB: get or assign hash_id, then save (metadata with normalized date)
    # Include full extracted text as `content` (same field enrichment / analyze_docket_entry use).
    record = {
        "metadata": metadata_for_save,
        "content": text,
        "summary": summary,
        "original_content_length": original_length,
        "summary_length": summary_length,
        "updated_at": datetime.now().isoformat(),
    }

    if not test_mode and mongodb_uri:
        try:
            mongo_client = MongoClient(mongodb_uri)
            db = _get_db(mongo_client)
            coll = db[COLLECTION_NAME]

            record["hash_id"] = next_hash_id
            record["created_at"] = datetime.now().isoformat()
            coll.insert_one(record)
            return {
                "hash_id": next_hash_id,
                "metadata": metadata_for_save,
                "content": text,
                "summary": summary,
                "original_content_length": original_length,
                "summary_length": summary_length,
                "status": "saved",
            }
        except Exception as e:
            return {
                "error": f"MongoDB error: {str(e)}",
                "metadata": metadata_for_save,
                "content": text,
                "summary": summary,
                "original_content_length": original_length,
                "summary_length": summary_length,
                "status": "error",
            }

    # test_mode or no MongoDB: return without saving
    return {
        "hash_id": next_hash_id,
        "metadata": metadata_for_save,
        "content": text,
        "summary": summary,
        "original_content_length": original_length,
        "summary_length": summary_length,
        "status": "not_saved" if test_mode else "no_mongodb",
    }


if __name__ == "__main__":
    import json
    import sys

    # Example: pass metadata and text (e.g. from stdin or args)
    meta = {
        "document_id": "test-doc-001",
        "date": "2025-12-11",
        "document_type": "Filing",
        "additional_info": "Sample docket",
        "on_behalf_of": "Staff",
        "docket_number": "FD-36873",
        "docket_type": "stb-document",
    }
    sample_text = (
        "Surface Transportation Board. Office of Economics. December 10, 2025. "
        "Re: Waybill Request WB25-55. I have approved the addition of the following individual "
        "to the waybill access letter in WB25-55. a) Kim Hillenbrand - Berkley Research Group. "
        "Prior payment of processing and mailing cost of $72 ($72 per signature) is required and received."
    )
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        result = generate_tier1_summary(meta, sample_text, test_mode=True)
    else:
        result = generate_tier1_summary(meta, sample_text)
    print(json.dumps(result, indent=2))
