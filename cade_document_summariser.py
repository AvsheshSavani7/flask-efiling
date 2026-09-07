#!/usr/bin/env python3
"""
CADE Document Summariser
========================
Per-document Tier 1 / Tier 2 analysis for CADE Brazil FRMD cases.

Uniqueness: (documento_processo + deal_id + brazil_cases._id).
History for cumulative Tier 2 is scoped to the same brazil_cases record.
Writes to MongoDB collection ``brazil_summariser``.
"""

import html as _html_lib
import json
import logging
import os
import re
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from io import BytesIO
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import anthropic
from openai import OpenAI
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure

from email_subject_builder import build_subject
from error_email_service import send_error_email
from log_utils import cleanup_old_logs, refresh_log_file
from n8n_email_service import post_email_payload, send_direct_email
from bson import ObjectId

ENV_FILE = ".env"
COMPREHENSIVE_SUMMARY_MODEL = "gpt-5-mini-2025-08-07"
# Model for Responses API file_search overflow summaries
ASSISTANTS_API_MODEL = "gpt-4o-mini"
TIER1_MODEL = "claude-sonnet-5"
TIER2_MODEL = "claude-sonnet-5"
# Sonnet 5 is ~1M context; summarize via OpenAI file upload when estimate exceeds this.
TIER1_MAX_ESTIMATED_TOKENS = 800_000

_ENGLISH_OUTPUT_RULE = (
    "LANGUAGE (mandatory): Write every heading, sentence, bullet, and field in English. "
    "Source documents are often Portuguese. Translate all content, quotes, headings, "
    "and document-type names. Do not leave Portuguese in the output except official "
    "party names, process numbers, and acronyms (CADE, SEI, SG, Tribunal). "
    "Translate document types, e.g. Certidão de Trânsito em Julgado → Certificate of "
    "Final and Unappealable Judgment; Lista de Presença → Attendance List; "
    "Ato de Concentração → Concentration Act / merger filing; "
    "Despacho → Order; Edital → Public notice. "
    "You may keep the Portuguese original in parentheses after the English translation."
)


def _load_env_file(env_path: str) -> None:
    """Load environment variables from .env file"""
    if not os.path.exists(env_path):
        return

    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value


_load_env_file(ENV_FILE)

# -----------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# Timestamps in IST (UTC+5:30)
# -----------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "cade_document_summariser"
LOGGER_NAME = "cade_document_summariser"
COLLECTION_NAME = "brazil_summariser"
CADE_SUMMARISER_WORKERS = int(os.getenv("CADE_SUMMARISER_WORKERS", "2"))
INTAKE_MODEL = "gpt-4.1-mini"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

if not logger.handlers:
    class _ISTFormatter(logging.Formatter):
        def converter(self, timestamp):
            return datetime.fromtimestamp(timestamp, tz=IST)

        def formatTime(self, record, datefmt=None):
            ct = self.converter(record.created)
            if datefmt:
                return ct.strftime(datefmt)
            return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")

    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)

CADE_HISTORY_PROJECTION = {
    "_id": 0,
    "hash_id": 1,
    "metadata": 1,
    "summary": 1,
}

_cade_indexes_ensured = False
_cade_index_lock = threading.Lock()


def _ensure_cade_indexes(collection) -> None:
    """Unique triple + history sort index for brazil_summariser."""
    global _cade_indexes_ensured
    with _cade_index_lock:
        if _cade_indexes_ensured:
            return
        collection.create_index(
            [
                ("metadata.document_id", 1),
                ("deal_id", 1),
                ("brazil_case_id", 1),
            ],
            name="doc_deal_case_unique",
            unique=True,
            background=True,
        )
        collection.create_index(
            [
                ("brazil_case_id", 1),
                ("metadata.date", 1),
            ],
            name="brazil_case_id_date",
            background=True,
        )
        _cade_indexes_ensured = True


def _fetch_sorted_docket_entries(collection, query_filter: Dict[str, Any]) -> list:
    """Load prior CADE summaries in date order for history context and hash_id."""
    cursor = (
        collection.find(query_filter, CADE_HISTORY_PROJECTION)
        .sort("metadata.date", 1)
    )
    try:
        return list(cursor)
    except OperationFailure as e:
        if e.code != 292:
            raise
        logger.warning(
            "Sort exceeded memory limit; retrying with aggregation allowDiskUse"
        )
        pipeline = [
            {"$match": query_filter},
            {"$sort": {"metadata.date": 1}},
            {"$project": CADE_HISTORY_PROJECTION},
        ]
        return list(collection.aggregate(pipeline, allowDiskUse=True))


def _next_hash_id(entries: list) -> int:
    max_hash_id = 0
    for entry in entries:
        hash_id = entry.get("hash_id")
        if isinstance(hash_id, int):
            max_hash_id = max(max_hash_id, hash_id)
    return max_hash_id + 1 if entries else 1


def convert_date_to_datetime(date_str: str) -> Optional[datetime]:
    """
    Convert date string to timezone-aware UTC datetime for MongoDB-friendly filtering.

    Args:
        date_str: Date string in MM/DD/YYYY, ISO 8601 (e.g. 2026-02-04T23:07:37.966853Z),
                  or 2025-01-09T00:00:00.000+00:00

    Returns:
        Timezone-aware datetime in UTC (e.g. 2025-01-09T00:00:00.000+00:00), or None if conversion fails.
        Use in MongoDB filters for correct date-wise queries.
    """
    s = date_str.strip()
    # strptime %z expects +0000 not +00:00
    s_normalized = s.replace("+00:00", "+0000").replace("-00:00", "-0000")
    formats = (
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    )
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(s_normalized, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    # Always return UTC timezone-aware for MongoDB
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _is_context_length_error(error: Exception) -> bool:
    """True if the exception looks like an LLM context/token limit failure."""
    error_str = str(error).lower()
    markers = (
        "prompt is too long",
        "context_length_exceeded",
        "tokens exceed",
        "maximum context",
        "context window",
        "too many tokens",
        "string too long",
        "> 200000 maximum",
        "200000 maximum",
    )
    return any(marker in error_str for marker in markers)


def _poll_vector_store_file(
    openai_client: OpenAI, vector_store_id: str, file_id: str
) -> None:
    """Wait until a vector-store file is indexed (or fail)."""
    deadline = time.time() + 180
    while time.time() < deadline:
        vs_file = openai_client.vector_stores.files.retrieve(
            vector_store_id=vector_store_id,
            file_id=file_id,
        )
        status = getattr(vs_file, "status", None)
        logger.info("  vector_store file status=%s", status)
        if status == "completed":
            return
        if status == "failed":
            raise RuntimeError(f"Vector store indexing failed: {vs_file}")
        time.sleep(2)
    raise TimeoutError("Vector store file indexing timed out")


def _generate_comprehensive_summary_with_file_upload(
    openai_client: OpenAI,
    full_text: str,
    entry_metadata: Dict[str, str],
    estimated_tokens: int,
    next_entry_number: int
) -> Dict[str, Any]:
    """
    Generate comprehensive summary via Responses API file_search.
    Use this when content is too large for direct API calls.

    Args:
        openai_client: OpenAI client instance
        full_text: Full document text
        entry_metadata: Document metadata
        estimated_tokens: Estimated token count
        next_entry_number: The entry number for this document

    Returns:
        Dictionary with summary, tokens, and cost information
    """
    logger.info("Using Responses file_search for comprehensive summary...")

    prompt = f"""You are summarizing a CADE (Brazil) merger-control filing for further analysis. Create a comprehensive summary that preserves all important details.

{_ENGLISH_OUTPUT_RULE}

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

The full document content is in the attached file. Please read it and create a COMPREHENSIVE SUMMARY that includes:

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
- Filing: Deal Name or Parties (English)
- Type: English filing type (Portuguese original in parentheses if useful)
- Summary: 1-2 sentence English content summary
- Relevance: High/Medium/Low - short English justification

Target length: 1000-2000 words depending on complexity."""

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp_file:
        tmp_file.write(full_text)
        tmp_file_path = tmp_file.name

    file_id = None
    vector_store_id = None
    try:
        logger.info(
            "Uploading file to OpenAI (%s characters)...",
            f"{len(full_text):,}",
        )
        with open(tmp_file_path, "rb") as file:
            uploaded_file = openai_client.files.create(
                file=file,
                purpose="assistants",
            )

        file_id = uploaded_file.id
        logger.info("✓ File uploaded with ID: %s", file_id)

        vector_store = openai_client.vector_stores.create(
            name="docket-comprehensive-summary"
        )
        vector_store_id = vector_store.id
        logger.info("✓ Vector store created with ID: %s", vector_store_id)

        create_and_poll = getattr(
            openai_client.vector_stores.files, "create_and_poll", None
        )
        if create_and_poll:
            create_and_poll(vector_store_id=vector_store_id, file_id=file_id)
            logger.info("✓ Vector store file indexed")
        else:
            openai_client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=file_id,
            )
            _poll_vector_store_file(openai_client, vector_store_id, file_id)

        logger.info(
            "Calling responses.create file_search model=%s ...",
            ASSISTANTS_API_MODEL,
        )
        response = openai_client.responses.create(
            model=ASSISTANTS_API_MODEL,
            instructions=(
                "You are a legal document summarizer. Create comprehensive "
                "summaries that preserve all important details for further analysis. "
                "Write the entire summary in English. If the source is Portuguese, "
                "translate it; do not leave Portuguese in the output except official "
                "names, process numbers, and acronyms (CADE, SEI)."
            ),
            tools=[
                {
                    "type": "file_search",
                    "vector_store_ids": [vector_store_id],
                    "max_num_results": 50,
                }
            ],
            input=prompt,
        )

        comprehensive_summary_text = (
            getattr(response, "output_text", None) or ""
        ).strip()
        if not comprehensive_summary_text:
            raise RuntimeError("No summary returned from responses.create")

        usage = getattr(response, "usage", None)
        comprehensive_summary_input_tokens = (
            getattr(usage, "input_tokens", None) if usage is not None else None
        ) or (estimated_tokens + 500)
        comprehensive_summary_output_tokens = (
            getattr(usage, "output_tokens",
                    None) if usage is not None else None
        ) or (len(comprehensive_summary_text) // 4)
        comprehensive_summary_cost = _estimate_cost(
            comprehensive_summary_input_tokens,
            comprehensive_summary_output_tokens,
            ASSISTANTS_API_MODEL,
        )

        logger.info(
            "✓ Generated comprehensive summary: %s characters",
            f"{len(comprehensive_summary_text):,}",
        )

        return {
            "summary": comprehensive_summary_text,
            "tokens": {
                "input": comprehensive_summary_input_tokens,
                "output": comprehensive_summary_output_tokens,
                "estimated_original": estimated_tokens,
            },
            "cost": comprehensive_summary_cost,
            "generated": True,
            "method": "file_search",
            "reason": f"Content too large ({estimated_tokens:,} tokens)",
        }

    finally:
        if vector_store_id:
            try:
                openai_client.vector_stores.delete(vector_store_id)
                logger.info("✓ Cleaned up vector store")
            except Exception as e:
                logger.warning("Could not delete vector store: %s", str(e))
        if file_id:
            try:
                openai_client.files.delete(file_id)
                logger.info("✓ Cleaned up uploaded file")
            except Exception as e:
                logger.warning("Could not delete uploaded file: %s", str(e))
        try:
            os.unlink(tmp_file_path)
        except Exception as e:
            logger.warning("Could not delete temp file: %s", str(e))


def _generate_tier1_summary(
    *,
    client: anthropic.Anthropic,
    openai_client: OpenAI,
    full_text: str,
    entry_metadata: Dict[str, Any],
    next_entry_number: int,
    estimated_tokens: int,
    comprehensive_summary_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run Claude Tier 1, with a comprehensive-summary fallback for large docs."""
    content = full_text
    if comprehensive_summary_data:
        content = comprehensive_summary_data["summary"]
        logger.info(
            "✓ Tier1 will reuse existing comprehensive summary (%s chars)",
            f"{len(content):,}",
        )
    elif estimated_tokens > TIER1_MAX_ESTIMATED_TOKENS:
        logger.info(
            "Tier1 content too large (%s est. tokens > %s); "
            "generating OpenAI file-upload summary for Tier1...",
            f"{estimated_tokens:,}",
            f"{TIER1_MAX_ESTIMATED_TOKENS:,}",
        )
        comprehensive_summary_data = _generate_comprehensive_summary_with_file_upload(
            openai_client=openai_client,
            full_text=full_text,
            entry_metadata=entry_metadata,
            estimated_tokens=estimated_tokens,
            next_entry_number=next_entry_number,
        )
        comprehensive_summary_data["reason"] = (
            f"Tier1 gate: estimated tokens {estimated_tokens:,} > "
            f"{TIER1_MAX_ESTIMATED_TOKENS:,}"
        )
        content = comprehensive_summary_data["summary"]
        logger.info(
            "✓ Tier1 will use comprehensive summary (%s chars)",
            f"{len(content):,}",
        )

    used_summary = bool(comprehensive_summary_data)

    def _build_tier1_prompt(tier1_content: str) -> str:
        return f"""You are extracting key facts from a CADE (Brazil) merger-control document. Be concise and factual.

{_ENGLISH_OUTPUT_RULE}

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

CONTENT:
{tier1_content}

Extract the key facts in 3-5 bullet points (max 500 words total):
- What type of filing is this?
- Who filed it and what do they want?
- What are the main arguments/concerns raised?
- Any commitments, recommendations, or conclusions?

Be factual and concise. Focus on substantive content, not procedural details."""

    def _call_tier1(tier1_content: str, used: bool):
        logger.info(
            "Generating Tier1 summary (content_chars=%s, est_tokens=%s, used_summary=%s)",
            f"{len(tier1_content):,}",
            f"{estimated_tokens:,}",
            used,
        )
        return client.messages.create(
            model=TIER1_MODEL,
            max_tokens=1000,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": _build_tier1_prompt(tier1_content)}],
        )

    try:
        tier1_message = _call_tier1(content, used_summary)
    except Exception as tier1_error:
        if not _is_context_length_error(tier1_error):
            logger.error("Tier1 generation failed: %s", str(tier1_error))
            raise

        logger.warning(
            "Tier1 failed due to context/token limit: %s",
            str(tier1_error),
        )
        if used_summary:
            raise RuntimeError(
                "Tier1 failed due to context/token limit even after using "
                f"comprehensive_summary ({len(content):,} chars). "
                f"Original error: {tier1_error}"
            ) from tier1_error

        logger.info(
            "Failed Tier1 used full/large text; generating OpenAI file-upload "
            "summary, then retrying Tier1 once..."
        )
        comprehensive_summary_data = _generate_comprehensive_summary_with_file_upload(
            openai_client=openai_client,
            full_text=full_text,
            entry_metadata=entry_metadata,
            estimated_tokens=estimated_tokens,
            next_entry_number=next_entry_number,
        )
        comprehensive_summary_data["reason"] = (
            f"Tier1 retry after context limit: {str(tier1_error)}"
        )
        content = comprehensive_summary_data["summary"]
        used_summary = True
        logger.info(
            "✓ Retrying Tier1 with comprehensive summary (%s chars)",
            f"{len(content):,}",
        )
        try:
            tier1_message = _call_tier1(content, used_summary)
        except Exception as tier1_retry_error:
            logger.error(
                "Tier1 retry after comprehensive summary also failed: %s",
                str(tier1_retry_error),
            )
            raise

    summary = tier1_message.content[0].text.strip()
    input_tokens = tier1_message.usage.input_tokens
    output_tokens = tier1_message.usage.output_tokens
    cost = _estimate_cost(input_tokens, output_tokens, TIER1_MODEL)
    logger.info("Tier1 message: %s", tier1_message)
    return {
        "summary": summary,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
        "comprehensive_summary_data": comprehensive_summary_data,
    }


def analyze_cade_document(
    doc_number: str,
    full_text: str,
    metadata: Optional[Dict[str, Any]] = None,
    test_mode: bool = False,
    *,
    deal_id: Optional[str] = None,
    brazil_case_id: Optional[str] = None,
    historico_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Analyze one CADE SEI document (Tier 1 + Tier 2) and store in brazil_summariser.

    Unique key: documento_processo + deal_id + brazil_cases._id.
    """
    global LOG_FILE

    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    _load_env_file(ENV_FILE)

    if metadata is None:
        metadata = {}

    logger.info("Analyzing CADE document: %s",
                metadata.get("document_id", doc_number))

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        return {
            "error": "MongoDB connection string not found in .env",
            "doc_number": doc_number
        }

    docket_type = metadata.get("docket_type", "cade")
    docket_number = metadata.get(
        "docket_number") or metadata.get("process", "N/A")
    date = metadata.get("date", "N/A")
    on_behalf_of = metadata.get("on_behalf_of", "N/A")
    target_company_name = metadata.get("target_company_name") or ""
    deal_id = str(deal_id or metadata.get("deal_id") or "")
    brazil_case_id = str(
        brazil_case_id or metadata.get("brazil_case_id") or "")

    if not deal_id or not brazil_case_id:
        return {
            "error": "deal_id and brazil_case_id are required",
            "doc_number": doc_number,
        }

    try:
        mongo_client = MongoClient(mongodb_uri)
        db = mongo_client.get_database()
        collection = db[COLLECTION_NAME]
        _ensure_cade_indexes(collection)

        unique_filter = {
            "metadata.document_id": str(doc_number),
            "deal_id": deal_id,
            "brazil_case_id": brazil_case_id,
        }
        existing_entry = collection.find_one(unique_filter)

        if existing_entry and not test_mode:
            comprehensive_summary_obj = existing_entry.get(
                "comprehensive_summary")
            comprehensive_summary_text = None
            if comprehensive_summary_obj:
                if isinstance(comprehensive_summary_obj, dict):
                    comprehensive_summary_text = comprehensive_summary_obj.get(
                        "summary")
                elif isinstance(comprehensive_summary_obj, str):
                    comprehensive_summary_text = comprehensive_summary_obj

            status = (
                "retry_email"
                if not existing_entry.get("email_sent")
                else "skipped"
            )
            payload = {
                "doc_number": doc_number,
                "status": status,
                "message": (
                    "Entry exists but email was not sent"
                    if status == "retry_email"
                    else "Entry already exists in database"
                ),
                "metadata": existing_entry.get("metadata", {}),
                "tier2_analysis": existing_entry.get("tier2_analysis", {}),
                "tier3_risk_assessment": existing_entry.get("tier3_risk_assessment", {}),
                "comprehensive_summary": comprehensive_summary_text,
                "deal_id": deal_id,
                "brazil_case_id": brazil_case_id,
                "record_id": str(existing_entry.get("_id", "")),
            }
            return payload

        query_filter = {"brazil_case_id": brazil_case_id}
        all_entries = _fetch_sorted_docket_entries(collection, query_filter)
        next_hash_id = _next_hash_id(all_entries)

        logger.info("All entries: length %s", len(all_entries))

        case_row = db["brazil_cases"].find_one(
            {"_id": ObjectId(brazil_case_id)},
            {"historico_records": 1, "_id": 0},
        )
        db_hist = (case_row or {}).get("historico_records") or []
        historico_records = db_hist or historico_records or []
        logger.info("Historico records: %s", len(historico_records))

    except Exception as e:
        return {
            "error": f"MongoDB error: {str(e)}",
            "doc_number": doc_number
        }

    api_key = os.environ.get(
        "CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "error": "Anthropic API key not found",
            "doc_number": doc_number
        }

    openai_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get(
        "OPENAI_API_KEY_DOCKET"
    )
    if not openai_api_key:
        return {
            "error": "OpenAI API key not found",
            "doc_number": doc_number
        }

    client = anthropic.Anthropic(api_key=api_key)
    openai_client = OpenAI(api_key=openai_api_key)

    # Build historical context from filtered entries with sequential numbering
    historical_context = _build_historical_context(all_entries)
    historico_context = _build_historico_context(historico_records or [])

    # Next entry number is simply the count of filtered entries + 1
    next_entry_number = len(all_entries) + 1

    logger.info("Next entry number: %s", next_entry_number)

    # Convert date to datetime object if it exists and is a string
    date_value = metadata.get("date", "N/A")
    if date_value != "N/A" and isinstance(date_value, str):
        dt = convert_date_to_datetime(date_value)
        if dt:
            date_value = dt
        # If conversion fails, keep the original string value

    if metadata.get("url"):
        url_value = metadata["url"]
    elif "http" in (doc_number or ""):
        url_value = doc_number
    else:
        url_value = ""

    entry_metadata = {
        "date": date_value,
        "document_type": metadata.get("document_type", "N/A"),
        "additional_info": metadata.get("additional_info", "N/A"),
        "on_behalf_of": metadata.get("on_behalf_of", "N/A"),
        "docket_number": docket_number,
        "document_id": str(doc_number),
        "docket_type": docket_type or "cade",
        "target_company_name": target_company_name,
        "url": url_value,
        "process": metadata.get("process") or docket_number,
    }

    # Estimate token count (rough estimate: 1 token ≈ 4 characters)
    estimated_tokens = len(full_text) // 4
    logger.info("Estimated tokens: %s", estimated_tokens)

    comprehensive_summary_data = None
    content_for_tier2 = full_text

    logger.info("Historical context: %s", historical_context[:200] + "...")
    logger.info("Historico context: %s", historico_context[:200] + "...")
    # Try to generate Tier2 directly with full_text first
    tier2_prompt = f"""You are a legal analyst specializing in M&A merger-control proceedings at CADE (Brazil).

{_ENGLISH_OUTPUT_RULE}

Always prioritize the filing's concrete legal or procedural function over its rhetorical tone. When possible, use the filer's own language (translated into English) to describe what they are asking CADE or other parties to do, and avoid vague phrasing such as "raises concerns" or "highlights issues" when you can state the specific request, effect, or role of the filing in the proceeding.

COMPLETE CASE HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

SEI PROCESS HISTORY (Date/Time | Unit | Description):
{historico_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEW ENTRY #{next_entry_number} TO ANALYZE:
Document ID: {doc_number}
Date: {date}
Type: {docket_type}
Filed By: {on_behalf_of}

CONTENT:
{content_for_tier2}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Based on the COMPLETE docket history above and this new entry, provide:

1. ENTRY SUMMARY (2-3 sentences):
   Describe this filing in terms of its regulatory or procedural function. Identify:
   (i) who filed it,
   (ii) what specific regulatory, procedural, or substantive action they are requesting (if any),
   and (iii) the main issues they are asking the agency or other decision-maker to evaluate.
   If the filer is not requesting a concrete action, explicitly state that they are urging
   consideration or re-weighting of certain factors rather than demanding a specific outcome.

2. LEGAL/REGULATORY SIGNIFICANCE (3-4 sentences): 
   Explain how this filing affects the legal and procedural posture of the case, if at all.
   Be explicit about whether it:
   • changes the scope of review, evidentiary burden, available remedies, schedule,
     or procedural rights; or
   • is primarily non-binding advocacy or commentary without direct procedural effect.
   Distinguish clearly between binding procedural or legal consequences (e.g., motions,
   orders, schedule changes, formal commitments) and persuasive advocacy (e.g., public
   comments, letters of support or opposition). Describe how this filing escalates,
   narrows, reinforces, or contradicts the themes and positions in specific prior entries
   (cite entry numbers).

3. CUMULATIVE IMPACT (3-4 sentences):
   
   Considering EVERYTHING that has happened from Entry #1 through #{next_entry_number},
   assess how this filing changes the overall risk profile and deal dynamics.
   Does it:
   • increase or decrease the probability of a formal challenge, remedies/conditions,
     or delays; or
   • mainly add weight to existing themes already present in prior entries?
   Be explicit about whether this filing introduces a new risk vector or simply reinforces
   existing ones, and state whether it tends to strengthen or weaken the deal’s position.
   Cite specific entry numbers when making comparisons or describing patterns.

Be specific and cite entry numbers when referencing prior events."""

    try:
        logger.info("Attempting to generate tier2 analysis directly...")
        tier2_message = client.messages.create(
            model=TIER2_MODEL,
            max_tokens=1500,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": tier2_prompt}]
        )
        logger.info("Tier2 prompt: %s", tier2_prompt)
        logger.info("Tier2 message: %s", tier2_message)

        logger.info("✓ Tier2 analysis generated directly")

        tier2_response = tier2_message.content[0].text
        tier2_input_tokens = tier2_message.usage.input_tokens
        tier2_output_tokens = tier2_message.usage.output_tokens
        tier2_cost = _estimate_cost(
            tier2_input_tokens, tier2_output_tokens, TIER2_MODEL)

    except Exception as tier2_error:
        logger.warning(
            "Direct tier2 generation failed: %s", str(tier2_error))
        logger.info("Falling back to comprehensive summary approach...")

        # FALLBACK: Generate comprehensive summary first
        comprehensive_summary_prompt = f"""You are summarizing a CADE (Brazil) merger-control filing for further analysis. Create a comprehensive summary that preserves all important details.

{_ENGLISH_OUTPUT_RULE}

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
- Filing: Deal Name or Parties (English)
- Type: English filing type (Portuguese original in parentheses if useful)
- Summary: 1-2 sentence English content summary
- Relevance: High/Medium/Low - short English justification

Target length: 1000-2000 words depending on complexity."""

        try:
            # Always try direct comprehensive summary first
            logger.info("Generating comprehensive summary...")
            try:
                comprehensive_summary_message = openai_client.chat.completions.create(
                    model=COMPREHENSIVE_SUMMARY_MODEL,
                    messages=[
                        {"role": "user", "content": comprehensive_summary_prompt}
                    ]
                )

                comprehensive_summary_text = comprehensive_summary_message.choices[0].message.content.strip(
                )
                comprehensive_summary_input_tokens = comprehensive_summary_message.usage.prompt_tokens
                comprehensive_summary_output_tokens = comprehensive_summary_message.usage.completion_tokens
                comprehensive_summary_cost = _estimate_cost(
                    comprehensive_summary_input_tokens,
                    comprehensive_summary_output_tokens,
                    COMPREHENSIVE_SUMMARY_MODEL
                )

                comprehensive_summary_data = {
                    "summary": comprehensive_summary_text,
                    "tokens": {
                        "input": comprehensive_summary_input_tokens,
                        "output": comprehensive_summary_output_tokens,
                        "estimated_original": estimated_tokens
                    },
                    "cost": comprehensive_summary_cost,
                    "generated": True,
                    "method": "direct",
                    "reason": f"Fallback: Direct tier2 generation failed with error: {str(tier2_error)}"
                }

                logger.info(
                    "✓ Generated comprehensive summary: %s tokens → %s chars",
                    f"{estimated_tokens:,}",
                    len(comprehensive_summary_text),
                )

            except Exception as direct_error:
                error_str = str(direct_error)
                logger.error("err %s", error_str)
                # Check if it's a token limit error
                if "context_length_exceeded" in error_str or "tokens exceed" in error_str.lower() or "string too long" in error_str.lower():
                    logger.warning(
                        "Direct API call failed due to token limit: %s",
                        error_str)
                    logger.info(
                        "Switching to file upload approach...")

                    comprehensive_summary_data = _generate_comprehensive_summary_with_file_upload(
                        openai_client=openai_client,
                        full_text=full_text,
                        entry_metadata=entry_metadata,
                        estimated_tokens=estimated_tokens,
                        next_entry_number=next_entry_number
                    )
                    comprehensive_summary_data["reason"] = f"Fallback + File Upload: Token limit exceeded in direct call"
                    comprehensive_summary_text = comprehensive_summary_data["summary"]

                    logger.info(
                        "✓ Generated comprehensive summary: %s tokens → %s chars",
                        f"{estimated_tokens:,}",
                        len(comprehensive_summary_text),
                    )
                else:
                    # If it's not a token error, re-raise
                    raise

            # Now retry tier2 with comprehensive summary
            content_for_tier2 = comprehensive_summary_text
            tier2_prompt_fallback = f"""You are a legal analyst specializing in M&A merger-control proceedings at CADE (Brazil).

{_ENGLISH_OUTPUT_RULE}

COMPLETE CASE HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

SEI PROCESS HISTORY (Date/Time | Unit | Description):
{historico_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEW ENTRY #{next_entry_number} TO ANALYZE:
Document ID: {doc_number}

CONTENT:
{content_for_tier2}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Based on the COMPLETE docket history above and this new entry, provide:

1. ENTRY SUMMARY (2-3 sentences): What is this entry and what does it contain?

2. LEGAL/REGULATORY SIGNIFICANCE (3-4 sentences): 
   - What legal or procedural issues does this raise?
   - How does it relate to previous entries? (cite specific entry numbers)
   - What stakeholder positions are emerging or evolving?

3. CUMULATIVE IMPACT (3-4 sentences):
   Given EVERYTHING that has happened from Entry #1 through #{next_entry_number}, how does 
   this entry change the overall picture? Does it strengthen/weaken the deal's position? 
   Does it introduce new themes or continue existing patterns?

Be specific and cite entry numbers when referencing prior events."""

            logger.info(
                "Generating tier2 analysis from comprehensive summary...")
            tier2_message = client.messages.create(
                model=TIER2_MODEL,
                max_tokens=1500,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": tier2_prompt_fallback}]
            )

            logger.info(
                "Tier2 message from comprehensive summary: %s",
                tier2_message)
            logger.info(
                "Tier2 prompt from comprehensive summary: %s",
                tier2_prompt_fallback)

            logger.info(
                "✓ Tier2 analysis generated from comprehensive summary")

            tier2_response = tier2_message.content[0].text
            tier2_input_tokens = tier2_message.usage.input_tokens
            tier2_output_tokens = tier2_message.usage.output_tokens
            tier2_cost = _estimate_cost(
                tier2_input_tokens, tier2_output_tokens, TIER2_MODEL)

        except Exception as e:
            logger.error("Error in fallback generation: %s", str(e))
            return {
                "error": f"Both direct and fallback tier2 generation failed. Direct error: {str(tier2_error)}, Fallback error: {str(e)}",
                "doc_number": doc_number,
                "metadata": entry_metadata
            }

    # Tier1 uses Sonnet 5 (~1M). Reuse a Tier2-fallback summary when present.
    tier1_result = _generate_tier1_summary(
        client=client,
        openai_client=openai_client,
        full_text=full_text,
        entry_metadata=entry_metadata,
        next_entry_number=next_entry_number,
        estimated_tokens=estimated_tokens,
        comprehensive_summary_data=comprehensive_summary_data,
    )
    tier1_summary = tier1_result["summary"]
    tier1_input_tokens = tier1_result["input_tokens"]
    tier1_output_tokens = tier1_result["output_tokens"]
    tier1_cost = tier1_result["cost"]
    comprehensive_summary_data = tier1_result["comprehensive_summary_data"]

    # Calculate total cost including comprehensive summary if generated
    total_cost = tier1_cost + tier2_cost
    if comprehensive_summary_data:
        total_cost += comprehensive_summary_data["cost"]

    new_entry = {
        "hash_id": next_hash_id,
        "content": content_for_tier2,
        "deal_id": deal_id,
        "brazil_case_id": brazil_case_id,
        "process": metadata.get("process") or docket_number,
        "email_sent": False,
        "metadata": entry_metadata,
        "summary": tier1_summary,
        "original_content_length": len(full_text),
        "summary_length": len(tier1_summary),
        "tokens": {
            "input": tier1_input_tokens,
            "output": tier1_output_tokens,
            "summary_estimated": len(tier1_summary) // 4
        },
        "cost": tier1_cost,
        "tier2_analysis": {
            "response": tier2_response,
            "tokens": {
                "input": tier2_input_tokens,
                "output": tier2_output_tokens
            },
            "cost": tier2_cost
        },
        "total_analysis_cost": total_cost,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }

    # Store comprehensive_summary only when it was generated (Tier1 size gate
    # when est. tokens > 200k, or Tier2 fallback). Otherwise callers use full text.
    if comprehensive_summary_data:
        new_entry["comprehensive_summary"] = comprehensive_summary_data["summary"]

    inserted_id = None
    if not test_mode:
        try:
            insert_result = collection.insert_one(new_entry)
            inserted_id = insert_result.inserted_id
            logger.info("✓ Saved entry to brazil_summariser _id=%s", inserted_id)
        except DuplicateKeyError:
            logger.info(
                "Duplicate brazil_summariser row for doc=%s deal=%s case=%s",
                doc_number, deal_id, brazil_case_id,
            )
            existing = collection.find_one({
                "metadata.document_id": str(doc_number),
                "deal_id": deal_id,
                "brazil_case_id": brazil_case_id,
            })
            return {
                "doc_number": doc_number,
                "status": "skipped" if (existing or {}).get("email_sent") else "retry_email",
                "message": "Duplicate unique key",
                "metadata": (existing or {}).get("metadata", entry_metadata),
                "tier2_analysis": (existing or {}).get("tier2_analysis", {}),
                "comprehensive_summary": (existing or {}).get("comprehensive_summary"),
                "deal_id": deal_id,
                "brazil_case_id": brazil_case_id,
                "record_id": str((existing or {}).get("_id", "")),
            }
        except Exception as e:
            logger.warning(
                "Failed to save to MongoDB: %s", str(e))

    # Return generated summary only when we created one; otherwise full text.
    comprehensive_summary_out = (
        comprehensive_summary_data["summary"]
        if comprehensive_summary_data
        else full_text
    )

    result = {
        "doc_number": doc_number,
        "status": "new_analysis",
        "metadata": entry_metadata,
        "tier1_summary": {
            "summary": tier1_summary,
            "tokens": {
                "input": tier1_input_tokens,
                "output": tier1_output_tokens
            },
            "cost": tier1_cost
        },
        "tier2_analysis": {
            "response": tier2_response,
            "tokens": {
                "input": tier2_input_tokens,
                "output": tier2_output_tokens
            },
            "cost": tier2_cost
        },
        "total_cost": total_cost,
        "comprehensive_summary": comprehensive_summary_out,
        "timestamp": datetime.now().isoformat(),
        "database_updated": inserted_id is not None,
        "record_id": str(inserted_id) if inserted_id else None,
        "deal_id": deal_id,
        "brazil_case_id": brazil_case_id,
    }

    return result


def analyze_cade_document_tier1_only(
    doc_number: str,
    full_text: str,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    deal_id: Optional[str] = None,
    brazil_case_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Generate Tier 1 only and upsert into brazil_summariser.

    No Tier 2, no email. Sets email_sent=True so production FRMD runs skip
    these rows. Unique key is still documento_processo + deal_id + brazil_case_id.
    """
    global LOG_FILE

    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    _load_env_file(ENV_FILE)

    if metadata is None:
        metadata = {}

    logger.info("Tier1 backfill: %s", metadata.get("document_id", doc_number))

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        return {"error": "MongoDB connection string not found in .env", "doc_number": doc_number}

    deal_id = str(deal_id or metadata.get("deal_id") or "")
    brazil_case_id = str(brazil_case_id or metadata.get("brazil_case_id") or "")
    docket_number = metadata.get("docket_number") or metadata.get("process", "N/A")
    if not deal_id or not brazil_case_id:
        return {
            "error": "deal_id and brazil_case_id are required",
            "doc_number": doc_number,
        }

    unique_filter = {
        "metadata.document_id": str(doc_number),
        "deal_id": deal_id,
        "brazil_case_id": brazil_case_id,
    }

    try:
        mongo_client = MongoClient(mongodb_uri)
        db = mongo_client.get_database()
        collection = db[COLLECTION_NAME]
        _ensure_cade_indexes(collection)
        existing_entry = collection.find_one(unique_filter)
        all_entries = _fetch_sorted_docket_entries(
            collection, {"brazil_case_id": brazil_case_id}
        )
        next_hash_id = _next_hash_id(all_entries)
    except Exception as e:
        return {"error": f"MongoDB error: {str(e)}", "doc_number": doc_number}

    if existing_entry and not force:
        logger.info(
            "  skip existing brazil_summariser row doc=%s", doc_number
        )
        return {
            "doc_number": doc_number,
            "status": "skipped",
            "message": "Entry already exists in brazil_summariser",
            "record_id": str(existing_entry.get("_id", "")),
            "deal_id": deal_id,
            "brazil_case_id": brazil_case_id,
        }

    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    openai_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get(
        "OPENAI_API_KEY_DOCKET"
    )
    if not api_key:
        return {"error": "Anthropic API key not found", "doc_number": doc_number}
    if not openai_api_key:
        return {"error": "OpenAI API key not found", "doc_number": doc_number}

    date_value = metadata.get("date", "N/A")
    if date_value != "N/A" and isinstance(date_value, str):
        dt = convert_date_to_datetime(date_value)
        if dt:
            date_value = dt

    if metadata.get("url"):
        url_value = metadata["url"]
    elif "http" in (doc_number or ""):
        url_value = doc_number
    else:
        url_value = ""

    entry_metadata = {
        "date": date_value,
        "document_type": metadata.get("document_type", "N/A"),
        "additional_info": metadata.get("additional_info", "N/A"),
        "on_behalf_of": metadata.get("on_behalf_of", "N/A"),
        "docket_number": docket_number,
        "document_id": str(doc_number),
        "docket_type": metadata.get("docket_type") or "cade",
        "target_company_name": metadata.get("target_company_name") or "",
        "url": url_value,
        "process": metadata.get("process") or docket_number,
    }

    estimated_tokens = len(full_text) // 4
    next_entry_number = (
        existing_entry.get("hash_id")
        if existing_entry and isinstance(existing_entry.get("hash_id"), int)
        else next_hash_id
    )
    logger.info(
        "  Tier1-only doc=%s hash_id=%s est_tokens=%s",
        doc_number, next_entry_number, estimated_tokens,
    )

    client = anthropic.Anthropic(api_key=api_key)
    openai_client = OpenAI(api_key=openai_api_key)
    try:
        tier1_result = _generate_tier1_summary(
            client=client,
            openai_client=openai_client,
            full_text=full_text,
            entry_metadata=entry_metadata,
            next_entry_number=next_entry_number,
            estimated_tokens=estimated_tokens,
        )
    except Exception as e:
        return {"error": str(e), "doc_number": doc_number}

    tier1_summary = tier1_result["summary"]
    now_iso = datetime.now().isoformat()
    payload = {
        "hash_id": next_entry_number,
        "deal_id": deal_id,
        "brazil_case_id": brazil_case_id,
        "process": metadata.get("process") or docket_number,
        "metadata": entry_metadata,
        "summary": tier1_summary,
        "original_content_length": len(full_text),
        "summary_length": len(tier1_summary),
        "tokens": {
            "input": tier1_result["input_tokens"],
            "output": tier1_result["output_tokens"],
            "summary_estimated": len(tier1_summary) // 4,
        },
        "cost": tier1_result["cost"],
        "total_analysis_cost": tier1_result["cost"],
        "updated_at": now_iso,
    }
    if not existing_entry:
        payload["created_at"] = now_iso
        payload["email_sent"] = True
        payload["analysis_tier"] = "tier1_only"
        payload["tier2_analysis"] = {
            "response": "",
            "tokens": {"input": 0, "output": 0},
            "cost": 0,
        }
    comprehensive_summary_data = tier1_result.get("comprehensive_summary_data")
    if comprehensive_summary_data:
        payload["comprehensive_summary"] = comprehensive_summary_data["summary"]
        payload["total_analysis_cost"] = (
            tier1_result["cost"] + comprehensive_summary_data.get("cost", 0)
        )

    try:
        if existing_entry:
            collection.update_one(unique_filter, {"$set": payload})
            record_id = str(existing_entry.get("_id", ""))
            status = "updated"
            logger.info("✓ Updated brazil_summariser tier1 doc=%s", doc_number)
        else:
            insert_result = collection.insert_one(payload)
            record_id = str(insert_result.inserted_id)
            status = "new_analysis"
            logger.info(
                "✓ Saved brazil_summariser tier1 _id=%s doc=%s",
                record_id, doc_number,
            )
    except DuplicateKeyError:
        logger.info("Duplicate brazil_summariser row for doc=%s", doc_number)
        return {
            "doc_number": doc_number,
            "status": "skipped",
            "message": "Duplicate unique key",
            "deal_id": deal_id,
            "brazil_case_id": brazil_case_id,
        }
    except Exception as e:
        return {"error": f"Failed to save: {e}", "doc_number": doc_number}

    return {
        "doc_number": doc_number,
        "status": status,
        "record_id": record_id,
        "deal_id": deal_id,
        "brazil_case_id": brazil_case_id,
        "hash_id": next_entry_number,
    }


def _build_historical_context(entries: list) -> str:
    """Build historical context string from filtered entries using hash_id"""
    if not entries:
        return "No prior entries."

    context_parts = []
    # Use hash_id from entry, fallback to index if hash_id doesn't exist
    for idx, entry in enumerate(entries, start=1):
        # Use hash_id if available, otherwise use index
        hash_id = entry.get("hash_id", idx)
        metadata = entry.get("metadata", {})
        date = metadata.get("date", "N/A")
        doc_type = metadata.get("document_type", "N/A")
        summary = entry.get("summary", "")

        context_parts.append(
            f"Entry #{hash_id} ({date}) - {doc_type}:\n{summary}"
        )

    return "\n\n".join(context_parts)


def _build_historico_context(records: Optional[List[Dict[str, Any]]]) -> str:
    """Format brazil_cases historico_records for the Tier 2 prompt."""
    if not records:
        return "No SEI history records."

    lines = ["Date/Time | Unit | Description"]
    for rec in records:
        if not isinstance(rec, dict):
            continue
        date_time = str(rec.get("date_time") or "").strip()
        unit = str(rec.get("unit") or "").strip()
        description = str(rec.get("description") or "").strip()
        if not (date_time or unit or description):
            continue
        lines.append(f"{date_time} | {unit} | {description}")

    if len(lines) == 1:
        return "No SEI history records."
    return "\n".join(lines)


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate API cost based on token usage"""
    pricing = {
        # Anthropic pricing (per 1M tokens)
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3-5-haiku-20241022": {"input": 0.8, "output": 4.0},
        "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
        "claude-sonnet-5": {"input": 3.0, "output": 15.0},
        # OpenAI pricing (per 1M tokens)
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.150, "output": 0.600},
        "gpt-4-turbo": {"input": 10.00, "output": 30.00},
        "gpt-5-mini-2025-08-07": {"input": 0.25, "output": 2},

    }

    if model not in pricing:
        return 0.0

    input_cost = (input_tokens / 1_000_000) * pricing[model]["input"]
    output_cost = (output_tokens / 1_000_000) * pricing[model]["output"]

    return input_cost + output_cost


# ---------------------------------------------------------------------------
# Cloned intake note (CADE copy — not shared with docket_engine)
# ---------------------------------------------------------------------------

_INTAKE_PROMPT_TEMPLATE = """You are an analyst supporting a merger arbitrage strategy.
You will be given the text of a CADE (Brazil) merger-control filing. It may be in Portuguese.

LANGUAGE RULES (mandatory):
- Every JSON value must be in English.
- Translate Portuguese document types into English (e.g. Certidão de Trânsito em Julgado → Certificate of Final and Unappealable Judgment). You may keep the Portuguese name in parentheses after the English translation.
- "Filing" must be the deal/parties in English (use the deal names below when provided). Never put the Portuguese process title (e.g. "Ato de Concentração nº …") in Filing.
- Do not output Portuguese sentences.

Deal / parties: {parties}

Text: {summary}

Your task:
1. Identify the type of filing in English (e.g. merger notification, order, public notice, questionnaire, third-party intervention, decision, certificate of final and unappealable judgment).
2. Summarize the main content in 1–2 English sentences.
3. Assess the relevance for merger arbitrage (High / Medium / Low) based on whether the filing:
   - materially affects approval risk or deal timing (High),
   - reinforces narrative/political dynamics without adding new facts (Medium),
   - or is operational/procedural detail with minimal impact (Low).
4. Output only JSON:
{{
  "Filing": "Deal name or parties in English",
  "Type": "English filing type (Portuguese original in parentheses if useful)",
  "Summary": "1–2 sentence English content summary",
  "Relevance": "High/Medium/Low – short English justification"
}}"""


def _deal_parties_label(deal: Optional[Dict[str, Any]]) -> str:
    if not deal:
        return ""
    acquirer = (
        deal.get("acquirer")
        or deal.get("acquire_name")
        or deal.get("acquirer_name")
        or ""
    )
    target = deal.get("target") or deal.get("target_name") or ""
    parts = [p for p in (str(acquirer).strip(), str(target).strip()) if p]
    if len(parts) == 2:
        return f"{parts[0]} / {parts[1]}"
    return parts[0] if parts else ""


def generate_cade_intake_note(
    comprehensive_summary: str,
    *,
    parties: str = "",
) -> Optional[dict]:
    """GPT intake note for a CADE document summary. Cloned from docket intake."""
    if not comprehensive_summary or not comprehensive_summary.strip():
        logger.warning("intake: empty comprehensive_summary — skipping.")
        return None

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get(
        "OPENAI_API_KEY_DOCKET"
    )
    if not api_key:
        logger.warning(
            "intake: OPENAI_API_KEY not set — skipping intake note.")
        return None

    client = OpenAI(api_key=api_key)
    prompt = _INTAKE_PROMPT_TEMPLATE.format(
        summary=comprehensive_summary.strip(),
        parties=(
            parties.strip()
            or "Not provided — infer deal/parties in English from the text."
        ),
    )

    try:
        logger.info("  Generating intake note via %s...", INTAKE_MODEL)
        response = client.chat.completions.create(
            model=INTAKE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
        note = json.loads(raw)
        logger.info("  Intake note — Relevance: %s",
                    note.get("Relevance", "?"))
        return note
    except json.JSONDecodeError as e:
        logger.warning(
            "  intake: JSON parse failed: %s | raw=%r", e, raw[:200])
        return None
    except Exception as e:
        logger.warning("  intake: API call failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Cloned email HTML renderer (CADE copy — not shared with docket_engine)
# ---------------------------------------------------------------------------

def _esc(s: Any = "") -> str:
    return _html_lib.escape(str(s or ""))


def _format_date(raw: Any) -> str:
    if not raw:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("$date", "") or ""
    if isinstance(raw, datetime):
        return raw.strftime("%m/%d/%Y")
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return raw


def _parse_sections(text: str = "") -> Dict[str, str]:
    t = str(text or "").replace("\r\n", "\n")
    entry_m = re.search(
        r"(?:^|\n)\s*(?:#+\s*)?1\.\s*ENTRY\s+SUMMARY\s*:?\s*([\s\S]*?)"
        r"(?=(?:\n\s*(?:#+\s*)?2\.\s*LEGAL\s*/\s*REGULATORY\s+SIGNIFICANCE\s*:?)|\s*$)",
        t, re.IGNORECASE,
    )
    legal_m = re.search(
        r"(?:^|\n)\s*(?:#+\s*)?2\.\s*LEGAL\s*/\s*REGULATORY\s+SIGNIFICANCE\s*:?\s*([\s\S]*?)"
        r"(?=(?:\n\s*(?:#+\s*)?3\.\s*CUMULATIVE\s+IMPACT\s*:?)|\s*$)",
        t, re.IGNORECASE,
    )
    cumulative_m = re.search(
        r"(?:^|\n)\s*(?:#+\s*)?3\.\s*CUMULATIVE\s+IMPACT\s*:?\s*([\s\S]*?)\s*$",
        t, re.IGNORECASE,
    )
    return {
        "entry_summary": entry_m.group(1).strip() if entry_m else "",
        "legal_reg_significance": legal_m.group(1).strip() if legal_m else "",
        "cumulative_impact": cumulative_m.group(1).strip() if cumulative_m else "",
    }


def render_cade_intake_card(intake_note: Dict[str, Any], document_url: str = "") -> str:
    TYPE = _esc(intake_note.get("Type", ""))
    FILING = _esc(intake_note.get("Filing", ""))
    RELEVANCE = _esc(intake_note.get("Relevance", ""))
    BORDER = "#e5e7eb"
    TEXT, MUTED = "#1f2937", "#6b7280"
    return f"""
          <tr>
            <td style="padding:20px 24px;border-bottom:1px solid {BORDER}">
              <div style="font-size:14px;color:{MUTED};margin-bottom:4px">{TYPE}</div>
              <div style="font-size:20px;font-weight:700;color:{TEXT};line-height:1.3">{FILING}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px 4px 24px">
              <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse">
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};width:180px;vertical-align:top">Filing</td>
                  <td style="padding:8px 0;color:{TEXT}">{FILING}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">Type</td>
                  <td style="padding:8px 0;color:{TEXT}">{TYPE}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">Relevance</td>
                  <td style="padding:8px 0;color:{TEXT}">{RELEVANCE}</td>
                </tr>
              </table>
            </td>
          </tr>
    """


def render_cade_email_html(
    tier2_response: str,
    base_html: str,
    metadata: Dict[str, Any],
) -> str:
    tier2 = _parse_sections(tier2_response)
    ENTRY_SUMMARY = _esc(tier2["entry_summary"])
    LEGAL_REG = _esc(tier2["legal_reg_significance"])
    CUMULATIVE = _esc(tier2["cumulative_impact"])
    doc_url = metadata.get("url") or metadata.get("document_id") or ""
    DATE = _format_date(metadata.get("date", ""))
    BG, CARD, BORDER = "#f8fafc", "#ffffff", "#e5e7eb"
    TEXT = "#1f2937"
    doc_link_row = (
        f"""<tr>
          <td style="padding:12px 0;font-weight:700;color:{TEXT};vertical-align:top">Document URL</td>
          <td style="padding:12px 0;color:{TEXT};line-height:1.6">
            <a href="{_esc(doc_url)}">Link</a>
          </td>
        </tr>"""
        if doc_url else ""
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CADE Document Update</title>
</head>
<body style="margin:0;padding:0;background:{BG};-webkit-text-size-adjust:100%">
  <table role="presentation" cellpadding="0" cellspacing="0"
         style="width:100%;border-collapse:collapse;background:{BG}">
    <tr>
      <td align="center" style="padding:24px 12px">
        <table role="presentation" cellpadding="0" cellspacing="0"
               style="width:100%;max-width:640px;background:{CARD};border:1px solid {BORDER};
                      border-radius:10px;overflow:hidden;border-collapse:separate">
          {base_html}
          <tr>
            <td style="padding:4px 24px 16px 24px">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;border-collapse:collapse">
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};width:180px;vertical-align:top">Date</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{DATE}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};width:180px;vertical-align:top">ENTRY SUMMARY</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{ENTRY_SUMMARY}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">LEGAL / REGULATORY SIGNIFICANCE</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{LEGAL_REG}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">CUMULATIVE IMPACT</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{CUMULATIVE}</td>
                </tr>
                {doc_link_row}
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Document text extraction (CADE copy of update-monitor extractor)
# ---------------------------------------------------------------------------

def _pdf_bytes_to_text(pdf_bytes: bytes) -> str:
    if not pdf_bytes:
        return ""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            try:
                text = page.extract_text()
                if text:
                    parts.append(text)
            except Exception:
                continue
        result = "\n".join(parts).strip()
        if result:
            return result
    except Exception as e:
        logger.warning("    PyPDF2 extraction failed: %s", e)

    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = [page.get_text() or "" for page in doc]
        doc.close()
        return "\n".join(parts).strip()
    except Exception as e:
        logger.warning("    pymupdf extraction failed: %s", e)
        return ""


def _ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    """Best-effort OCR fallback for scanned CADE PDFs."""
    if not pdf_bytes:
        return ""
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        try:
            for page_number, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(dpi=300, alpha=False)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                try:
                    text = pytesseract.image_to_string(
                        image, lang="por+eng", config="--psm 6"
                    )
                except Exception:
                    text = pytesseract.image_to_string(
                        image, lang="eng", config="--psm 6"
                    )
                pages.append(text.strip())
        finally:
            document.close()
        return "\n".join(p for p in pages if p).strip()
    except Exception as e:
        logger.warning("    OCR fallback failed: %s", e)
        return ""


def extract_document_text(context, url: str) -> str:
    """Open a CADE SEI document URL and return extracted text."""
    if not url:
        return ""
    page = None
    pdf_bytes = b""
    try:
        try:
            api_resp = context.request.get(url, timeout=90_000)
            body = api_resp.body()
            ctype = (api_resp.headers.get("content-type") or "").lower()
            if body[:4] == b"%PDF" or "pdf" in ctype:
                pdf_bytes = body
                text = _pdf_bytes_to_text(body)
                if text:
                    return text
        except Exception as e:
            logger.info("    Document request.get failed, opening page: %s", e)

        page = context.new_page()
        download_chunks: List[bytes] = []

        def _on_download(download):
            try:
                path = download.path()
                if path:
                    with open(path, "rb") as fh:
                        download_chunks.append(fh.read())
            except Exception:
                pass

        page.on("download", _on_download)
        resp = page.goto(url, wait_until="networkidle", timeout=90000)
        time.sleep(2)

        if download_chunks:
            data = download_chunks[0]
            if data[:4] == b"%PDF":
                pdf_bytes = data
                text = _pdf_bytes_to_text(data)
                if text:
                    return text

        if resp:
            body = resp.body()
            ctype = (resp.headers.get("content-type") or "").lower()
            if body[:4] == b"%PDF" or "pdf" in ctype:
                pdf_bytes = body
                text = _pdf_bytes_to_text(body)
                if text:
                    return text

        html = page.content()
        if "captcha" in html.lower() and "g-recaptcha" in html.lower():
            raise RuntimeError(f"CAPTCHA on document URL: {url}")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        html_text = soup.get_text(" ", strip=True)
        if html_text:
            return html_text
        if pdf_bytes:
            return _ocr_pdf_bytes(pdf_bytes)
        return ""
    except RuntimeError:
        raise
    except Exception:
        logger.exception("    Failed to extract document text: %s", url)
        if pdf_bytes:
            ocr = _ocr_pdf_bytes(pdf_bytes)
            if ocr:
                return ocr
        return ""
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# FRMD email + case orchestration
# ---------------------------------------------------------------------------

def _mark_email_sent(deal_id: str, brazil_case_id: str, doc_number: str) -> None:
    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        return
    try:
        client = MongoClient(mongodb_uri)
        client.get_database()[COLLECTION_NAME].update_one(
            {
                "metadata.document_id": str(doc_number),
                "deal_id": str(deal_id),
                "brazil_case_id": str(brazil_case_id),
            },
            {"$set": {"email_sent": True, "updated_at": datetime.now().isoformat()}},
        )
        client.close()
    except Exception as e:
        logger.warning("Could not mark email_sent: %s", e)


def _send_case_error_email(
    *,
    process: str,
    brazil_case_id: str,
    deal_id: str,
    documento_processo: str,
    step: str,
    error: str,
    traceback_str: str = "",
) -> None:
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=error,
        context={
            "process": process,
            "brazil_case_id": brazil_case_id,
            "deal_id": deal_id,
            "documento_processo": documento_processo,
            "step": step,
        },
        traceback_str=traceback_str or None,
    )


def send_cade_document_frmd_email(
    *,
    result: Dict[str, Any],
    rec: Dict[str, Any],
    case_doc: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    event_type: str,
    matched_by_regex: bool = False,
    test_recipients: Optional[List[str]] = None,
) -> bool:
    """Send one CADE FRMD email for a summarised document."""
    metadata = result.get("metadata") or {}
    comprehensive_summary = result.get("comprehensive_summary") or ""
    if not comprehensive_summary:
        comprehensive_summary = (
            (result.get("tier2_analysis") or {}).get("response") or ""
        )
    intake_note = generate_cade_intake_note(
        comprehensive_summary,
        parties=_deal_parties_label(deal),
    )
    if intake_note is None:
        raise RuntimeError(
            f"GPT intake note failed for document {rec.get('documento_processo')}"
        )

    document_url = metadata.get("url") or rec.get("document_url") or ""
    base_html = render_cade_intake_card(intake_note, document_url)
    email_html = render_cade_email_html(
        tier2_response=(result.get("tier2_analysis")
                        or {}).get("response", ""),
        base_html=base_html,
        metadata=metadata,
    )

    subject = build_subject("cade", event_type, deal)
    if matched_by_regex:
        subject = subject.replace("[FRMD]", "[FRRMD]")
    doc_id = str(rec.get("documento_processo")
                 or result.get("doc_number") or "")
    doc_type = str(
        rec.get("document_type") or rec.get("tipo_documento") or ""
    ).strip()
    suffix = " – ".join(p for p in (doc_type, doc_id) if p)
    if suffix:
        subject = f"{subject} – {suffix}"
    if test_recipients:
        subject = f"[TEST] {subject}"

    deal_id = str(
        result.get("deal_id")
        or case_doc.get("deal_id")
        or (deal or {}).get("_id")
        or ""
    )
    logger.info("  Sending CADE FRMD email: %s", subject)
    payload = {
        "subject": subject,
        "html": email_html,
        "process": case_doc.get("process", "N/A"),
        "deal_id": deal_id,
        "detail_url": case_doc.get("detail_url", ""),
        "documento_processo": doc_id,
        "is_new_case": event_type == "new",
        "update_type": "cade_document_summary",
    }
    if test_recipients:
        return send_direct_email(test_recipients, payload)
    return post_email_payload(payload)


def _build_cade_doc_metadata(
    rec: Dict[str, Any],
    case_doc: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    deal_id: str,
    brazil_case_id: str,
) -> Dict[str, Any]:
    process = case_doc.get("process", "")
    target = ""
    if deal:
        target = (
            deal.get("target_ticker")
            or deal.get("target_name")
            or deal.get("target")
            or ""
        )
    return {
        "docket_type": "cade",
        "docket_number": process,
        "process": process,
        "document_id": str(rec.get("documento_processo") or ""),
        "date": rec.get("data_documento") or rec.get("data_registro") or "",
        "document_type": rec.get("document_type") or rec.get("tipo_documento") or "N/A",
        "on_behalf_of": rec.get("unidade") or "N/A",
        "additional_info": (
            case_doc.get("interessados_en") or case_doc.get(
                "interessados") or "N/A"
        )[:200],
        "url": rec.get("document_url") or "",
        "target_company_name": str(target),
        "deal_id": deal_id,
        "brazil_case_id": brazil_case_id,
    }


def summarise_cade_case_documents(
    *,
    documents: List[Dict[str, Any]],
    case_doc: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    event_type: str,
    matched_by_regex: bool = False,
    playwright_context=None,
    test_mode: bool = False,
    headless: bool = True,
    test_recipients: Optional[List[str]] = None,
    force_email: bool = False,
) -> Dict[str, Any]:
    """
    Sequential per-document summarise + FRMD email for one brazil_cases record.

    On any error, stop remaining docs for this case and send an error email.
    """
    brazil_case_id = str(case_doc.get("_id") or "")
    deal_id = str(
        case_doc.get("deal_id")
        or (deal or {}).get("_id")
        or ""
    )
    process = str(case_doc.get("process") or "N/A")
    failing_doc = ""
    step = "init"

    if not brazil_case_id or not deal_id:
        err = "missing brazil_case_id or deal_id"
        _send_case_error_email(
            process=process,
            brazil_case_id=brazil_case_id,
            deal_id=deal_id,
            documento_processo="",
            step=step,
            error=err,
        )
        return {
            "success": False,
            "brazil_case_id": brazil_case_id,
            "process": process,
            "error": err,
            "step": step,
        }

    if not documents:
        return {
            "success": True,
            "brazil_case_id": brazil_case_id,
            "process": process,
            "error": None,
            "step": "no_documents",
        }

    own_browser = False
    context = playwright_context
    browser = None
    pw = None
    if context is None:
        step = "playwright"
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        own_browser = True

    try:
        for rec in documents:
            failing_doc = str(rec.get("documento_processo") or "")
            url = rec.get("document_url") or ""
            if not failing_doc:
                raise RuntimeError("document missing documento_processo")
            if not url:
                raise RuntimeError(
                    f"document {failing_doc} missing document_url"
                )

            step = "extract_text"
            logger.info(
                "  [%s] Extracting document %s", process, failing_doc
            )
            text = extract_document_text(context, url)
            if not (text or "").strip():
                raise RuntimeError(
                    f"No text extracted for document {failing_doc}"
                )

            step = "analyze"
            metadata = _build_cade_doc_metadata(
                rec, case_doc, deal, deal_id, brazil_case_id
            )
            result = analyze_cade_document(
                doc_number=failing_doc,
                full_text=text,
                metadata=metadata,
                test_mode=test_mode,
                deal_id=deal_id,
                brazil_case_id=brazil_case_id,
                historico_records=case_doc.get("historico_records") or [],
            )
            if result.get("error"):
                raise RuntimeError(result["error"])

            status = result.get("status")
            logger.info(
                "  [%s] doc=%s status=%s", process, failing_doc, status
            )
            if status == "skipped" and not force_email:
                continue

            if test_mode:
                continue

            step = "send_email"
            if not send_cade_document_frmd_email(
                result=result,
                rec=rec,
                case_doc=case_doc,
                deal=deal,
                event_type=event_type,
                matched_by_regex=matched_by_regex,
                test_recipients=test_recipients,
            ):
                raise RuntimeError(
                    f"FRMD email failed for document {failing_doc}"
                )
            if not test_recipients:
                _mark_email_sent(deal_id, brazil_case_id, failing_doc)

        return {
            "success": True,
            "brazil_case_id": brazil_case_id,
            "process": process,
            "error": None,
            "step": "done",
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception(
            "CADE summariser stopped for process=%s doc=%s step=%s: %s",
            process, failing_doc, step, e,
        )
        _send_case_error_email(
            process=process,
            brazil_case_id=brazil_case_id,
            deal_id=deal_id,
            documento_processo=failing_doc,
            step=step,
            error=str(e),
            traceback_str=tb,
        )
        return {
            "success": False,
            "brazil_case_id": brazil_case_id,
            "process": process,
            "error": str(e),
            "step": step,
            "documento_processo": failing_doc,
        }
    finally:
        if own_browser:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass


def summarise_cade_cases_parallel(
    jobs: List[Dict[str, Any]],
    max_workers: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Run one sequential document pipeline per case, in parallel across cases.

    Each worker launches its own Playwright browser (sync API is not thread-safe
    on a shared context). A failed case does not stop other cases.
    """
    if not jobs:
        return []
    workers = max_workers if max_workers is not None else CADE_SUMMARISER_WORKERS
    workers = max(1, min(int(workers), len(jobs)))
    logger.info(
        "Starting CADE summariser for %s case(s) with %s worker(s)",
        len(jobs), workers,
    )

    def _run(job: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(job)
        payload.pop("playwright_context", None)
        return summarise_cade_case_documents(**payload)

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run, job) for job in jobs]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def apply_summariser_pending_flags(cases_collection, results: List[Dict[str, Any]]) -> None:
    """Clear summariser_pending on brazil_cases after a successful case run."""
    if cases_collection is None:
        return
    for result in results:
        cid = result.get("brazil_case_id")
        if not cid or not result.get("success"):
            continue
        try:
            cases_collection.update_one(
                {"_id": ObjectId(str(cid))},
                {"$set": {"summariser_pending": False}},
            )
        except Exception as e:
            logger.warning(
                "Could not clear summariser_pending for %s: %s", cid, e
            )
