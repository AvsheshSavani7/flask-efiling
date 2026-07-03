#!/usr/bin/env python3
"""
Docket Entry Analyzer
=====================
Analyzes docket entries with tier 1, tier 2, and tier 3 summaries.
If entry already exists in database, skips it and returns skip status.
Otherwise generates new analysis using all historical summaries and adds to database.
Only returns new entries that are analyzed and added to the database.
"""

import json
import logging
import os
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import anthropic
from openai import OpenAI
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from log_utils import cleanup_old_logs, refresh_log_file

ENV_FILE = ".env"
COMPREHENSIVE_SUMMARY_MODEL = "gpt-5-mini-2025-08-07"
# Model for Assistants API (must support file_search)
ASSISTANTS_API_MODEL = "gpt-4o-mini"
TIER1_MODEL = "claude-haiku-4-5-20251001"
TIER2_MODEL = "claude-haiku-4-5-20251001"
TIER3_MODEL = "claude-sonnet-4-6"


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
SCRIPT_NAME = "docket_entry_analyzer"
LOGGER_NAME = "docket_entry_analyzer"
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

# Docket types that run post-insert enrichment (docket_pipeline)
DOCKET_TYPES_WITH_ENRICHMENT = frozenset({
    "stb-environmentalComment",
    "stb-document",
    "mt-psc",
    "sd-puc",
    "nm-prc",
    "ne-psc",
})

# Maps docket collection type → dashboard_docket_type for enrich_docket_entry
_DOCKET_TO_DASHBOARD_TYPE: Dict[str, str] = {
    "stb-document":             "stb",
    "stb-environmentalComment": "stb",
    "mt-psc":                   "mt-psc",
    "sd-puc":                   "sd-puc",
    "nm-prc":                   "nm-prc",
    "ne-psc":                   "ne-psc",
}

DOCKET_HISTORY_PROJECTION = {
    "_id": 0,
    "hash_id": 1,
    "metadata": 1,
    "summary": 1,
}

_docket_indexes_ensured = False
_docket_index_lock = threading.Lock()


def _ensure_docket_indexes(collection) -> None:
    """Create indexes so docket history queries sort without exceeding memory."""
    global _docket_indexes_ensured
    with _docket_index_lock:
        if _docket_indexes_ensured:
            return
        collection.create_index(
            [
                ("metadata.docket_type", 1),
                ("metadata.docket_number", 1),
                ("metadata.date", 1),
            ],
            name="docket_type_number_date",
            background=True,
        )
        collection.create_index(
            [("metadata.document_id", 1)],
            name="metadata_document_id",
            background=True,
        )
        _docket_indexes_ensured = True


def _fetch_sorted_docket_entries(collection, query_filter: Dict[str, Any]) -> list:
    """Load prior docket entries in date order for history context and hash_id."""
    cursor = (
        collection.find(query_filter, DOCKET_HISTORY_PROJECTION)
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
            {"$project": DOCKET_HISTORY_PROJECTION},
        ]
        return list(collection.aggregate(pipeline, allowDiskUse=True))


def _next_hash_id(entries: list) -> int:
    max_hash_id = 0
    for entry in entries:
        hash_id = entry.get("hash_id")
        if isinstance(hash_id, int):
            max_hash_id = max(max_hash_id, hash_id)
    return max_hash_id + 1 if entries else 1


def _should_schedule_enrichment(docket_type: str) -> bool:
    return docket_type in DOCKET_TYPES_WITH_ENRICHMENT


def _schedule_docket_enrichment(record_id: str, docket_type: str) -> None:
    """Run enrichment in a background thread (does not block API response)."""
    dashboard_type = _DOCKET_TO_DASHBOARD_TYPE.get(docket_type, "stb")

    def _run():
        try:
            from docket_pipeline.enrich_entry import enrich_docket_entry
            result = enrich_docket_entry(
                record_id=record_id, test_mode=False,
                dashboard_docket_type=dashboard_type)
            if result.get("success"):
                logger.info(
                    "Background enrichment completed for _id=%s", record_id)
            else:
                logger.warning(
                    "Background enrichment failed for _id=%s: %s",
                    record_id,
                    result.get("error"),
                )
        except Exception as e:
            logger.exception(
                "Background enrichment error for _id=%s: %s", record_id, e)

    threading.Thread(target=_run, daemon=True).start()


def _schedule_docket_enrichment_test(entry: Dict[str, Any]) -> None:
    """Test mode: write enrichment JSON only, no MongoDB update."""
    def _run():
        try:
            from docket_pipeline.enrich_entry import enrich_docket_entry
            result = enrich_docket_entry(entry=entry, test_mode=True)
            if result.get("success"):
                logger.info(
                    "Test-mode enrichment wrote JSON: %s",
                    result.get("output_path"),
                )
            else:
                logger.warning(
                    "Test-mode enrichment failed: %s",
                    result.get("error"),
                )
        except Exception as e:
            logger.exception("Test-mode enrichment error: %s", e)

    threading.Thread(target=_run, daemon=True).start()


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


def _generate_comprehensive_summary_with_file_upload(
    openai_client: OpenAI,
    full_text: str,
    entry_metadata: Dict[str, str],
    estimated_tokens: int,
    next_entry_number: int
) -> Dict[str, Any]:
    """
    Generate comprehensive summary by uploading file to OpenAI.
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
    logger.info("Using file upload approach for comprehensive summary...")

    # Create a temporary text file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp_file:
        tmp_file.write(full_text)
        tmp_file_path = tmp_file.name

    try:
        # Upload file to OpenAI
        logger.info(
            f"Uploading file to OpenAI ({len(full_text):,} characters)...")
        with open(tmp_file_path, 'rb') as file:
            uploaded_file = openai_client.files.create(
                file=file,
                purpose='assistants'
            )

        file_id = uploaded_file.id
        logger.info(f"✓ File uploaded with ID: {file_id}")

        # Create an assistant
        logger.info("Creating assistant...")
        assistant = openai_client.beta.assistants.create(
            name="Document Summarizer",
            instructions="""You are a legal document summarizer. Create comprehensive summaries that preserve all important details for further analysis.""",
            model=ASSISTANTS_API_MODEL,
            tools=[{"type": "file_search"}]
        )

        logger.info(f"✓ Assistant created with ID: {assistant.id}")

        # Create a thread with the file
        logger.info("Creating thread with file...")
        thread = openai_client.beta.threads.create(
            messages=[
                {
                    "role": "user",
                    "content": f"""You are summarizing a legal docket entry for further analysis. Create a comprehensive summary that preserves all important details.

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
- Filing: Deal Name or Parties
- Type: filing type
- Summary: 1-2 sentence content summary
- Relevance: High/Medium/Low - short justification

Target length: 1000-2000 words depending on complexity.""",
                    "attachments": [
                        {
                            "file_id": file_id,
                            "tools": [{"type": "file_search"}]
                        }
                    ]
                }
            ]
        )

        logger.info(f"✓ Thread created with ID: {thread.id}")

        # Run the assistant
        logger.info("Running assistant...")
        run = openai_client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=assistant.id
        )

        # Wait for completion
        max_wait_time = 300  # 5 minutes max
        start_time = time.time()

        while run.status in ['queued', 'in_progress']:
            if time.time() - start_time > max_wait_time:
                raise TimeoutError("Assistant run exceeded maximum wait time")

            time.sleep(2)
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )
            logger.info(f"  Status: {run.status}")

        if run.status != 'completed':
            raise Exception(f"Assistant run failed with status: {run.status}")

        logger.info("✓ Assistant run completed")

        # Get the messages
        messages = openai_client.beta.threads.messages.list(
            thread_id=thread.id
        )

        # Extract the assistant's response
        assistant_message = None
        for message in messages.data:
            if message.role == 'assistant':
                assistant_message = message
                break

        if not assistant_message or not assistant_message.content:
            raise Exception("No response from assistant")

        comprehensive_summary_text = assistant_message.content[0].text.value

        # Estimate token usage (since Assistants API doesn't provide exact counts)
        comprehensive_summary_input_tokens = estimated_tokens + \
            500  # Add buffer for instructions
        comprehensive_summary_output_tokens = len(
            comprehensive_summary_text) // 4
        comprehensive_summary_cost = _estimate_cost(
            comprehensive_summary_input_tokens,
            comprehensive_summary_output_tokens,
            ASSISTANTS_API_MODEL
        )

        logger.info(
            f"✓ Generated comprehensive summary: {len(comprehensive_summary_text):,} characters")

        # Clean up
        try:
            openai_client.files.delete(file_id)
            logger.info("✓ Cleaned up uploaded file")
        except Exception as e:
            logger.warning(
                "Could not delete uploaded file: %s", str(e))

        try:
            openai_client.beta.assistants.delete(assistant.id)
            logger.info("✓ Cleaned up assistant")
        except Exception as e:
            logger.warning(
                "Could not delete assistant: %s", str(e))

        return {
            "summary": comprehensive_summary_text,
            "tokens": {
                "input": comprehensive_summary_input_tokens,
                "output": comprehensive_summary_output_tokens,
                "estimated_original": estimated_tokens
            },
            "cost": comprehensive_summary_cost,
            "generated": True,
            "method": "file_upload",
            "reason": f"Content too large ({estimated_tokens:,} tokens)"
        }

    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_file_path)
        except Exception as e:
            logger.warning(
                "Could not delete temp file: %s", str(e))


def analyze_docket_entry(
    doc_number: str,
    full_text: str,
    metadata: Optional[Dict[str, str]] = None,
    test_mode: bool = False
) -> Dict[str, Any]:
    """
    Analyze a docket entry by document number and full text.

    Args:
        doc_number: The document ID/number (e.g., "202510-224401-01" or "URL)
        full_text: The full text content of the document
        metadata: Optional metadata dict with keys: date, document_type, 
                 additional_info, on_behalf_of, docket_number
        test_mode: Whether to run in test mode
    Returns:
        Dictionary containing analysis results with tier1, tier2 and tier3 responses
    """
    global LOG_FILE

    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    _load_env_file(ENV_FILE)

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        return {
            "error": "MongoDB connection string not found in .env",
            "doc_number": doc_number
        }

    if metadata is None:
        metadata = {}

    # Get docket_type and docket_number from metadata for filtering
    docket_type = metadata.get("docket_type", "N/A")
    docket_number = metadata.get("docket_number", "N/A")
    date = metadata.get("date", "N/A")
    on_behalf_of = metadata.get("on_behalf_of", "N/A")

    # Populated from mergers collection when docket_type + docket_number match
    target_company_name = ""
    deal_id = None

    try:
        mongo_client = MongoClient(mongodb_uri)
        db = mongo_client.get_database()
        collection = db["docket"]
        _ensure_docket_indexes(collection)

        # Query mergers collection to find target_company_name
        if docket_type and docket_type != "N/A" and docket_number and docket_number != "N/A":
            try:
                mergers_collection = db["mergers"]
                merger = mergers_collection.find_one({
                    "dockets": {
                        "$elemMatch": {
                            "docket_type": docket_type,
                            "docket_number": docket_number
                        }
                    }
                })
                if merger:
                    target_company_name = merger.get(
                        "target_company_name", "")
                    raw_deal_id = merger.get("deal_id")
                    if raw_deal_id is not None and raw_deal_id != "":
                        deal_id = str(raw_deal_id)
                    logger.info(
                        "Found merger for docket %s/%s — target=%s deal_id=%s",
                        docket_type,
                        docket_number,
                        target_company_name,
                        deal_id,
                    )
            except Exception as e:
                logger.warning(
                    "Could not query mergers collection: %s", str(e))

        existing_entry = collection.find_one(
            {"metadata.document_id": doc_number})

        if existing_entry and not test_mode:
            existing_entry.pop("_id", None)
            # Entry already exists, skip it and don't return it
            # Extract comprehensive_summary.summary if it's an object, otherwise use the value directly
            comprehensive_summary_obj = existing_entry.get(
                "comprehensive_summary")
            comprehensive_summary_text = None
            if comprehensive_summary_obj:
                if isinstance(comprehensive_summary_obj, dict):
                    comprehensive_summary_text = comprehensive_summary_obj.get(
                        "summary")
                elif isinstance(comprehensive_summary_obj, str):
                    comprehensive_summary_text = comprehensive_summary_obj

            return {
                "doc_number": doc_number,
                "status": "skipped",
                "message": "Entry already exists in database",
                "metadata": existing_entry.get("metadata", {}),
                "tier2_analysis": existing_entry.get("tier2_analysis", {}),
                "tier3_risk_assessment": existing_entry.get("tier3_risk_assessment", {}),
                "comprehensive_summary": comprehensive_summary_text,
            }

        # Filter entries by docket_type and docket_number if provided
        query_filter = {}
        if docket_type and docket_type != "N/A":
            query_filter["metadata.docket_type"] = docket_type
        if docket_number and docket_number != "N/A":
            # Handle both string and number types in database
            # MongoDB is type-sensitive, so we need to check both string and numeric versions
            docket_number_values = [docket_number]

            # Try to convert to number if it's a numeric string
            try:
                if isinstance(docket_number, str):
                    # Try integer first
                    try:
                        docket_number_values.append(int(docket_number))
                    except ValueError:
                        pass
                    # Try float if int fails
                    try:
                        docket_number_values.append(float(docket_number))
                    except ValueError:
                        pass
                elif isinstance(docket_number, (int, float)):
                    # If it's already a number, also check string version
                    docket_number_values.append(str(docket_number))
            except (ValueError, TypeError):
                pass

            # Use $in to match any of the possible type variations
            if len(docket_number_values) > 1:
                query_filter["metadata.docket_number"] = {
                    "$in": docket_number_values}
            else:
                query_filter["metadata.docket_number"] = docket_number

        if not query_filter:
            mongo_client.close()
            return {
                "error": (
                    "metadata.docket_type and metadata.docket_number are "
                    "required to load docket history"
                ),
                "doc_number": doc_number,
            }

        all_entries = _fetch_sorted_docket_entries(collection, query_filter)
        next_hash_id = _next_hash_id(all_entries)

        logger.info("All entries: length %s", len(all_entries))

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

    openai_api_key = os.environ.get("OPENAI_API_KEY_DOCKET")
    if not openai_api_key:
        return {
            "error": "OpenAI API key not found",
            "doc_number": doc_number
        }

    client = anthropic.Anthropic(api_key=api_key)
    openai_client = OpenAI(api_key=openai_api_key)

    # Build historical context from filtered entries with sequential numbering
    historical_context = _build_historical_context(all_entries)

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
        "docket_number": metadata.get("docket_number", "N/A"),
        "document_id": doc_number,
        "docket_type": docket_type,
        "target_company_name": target_company_name,
        "url": url_value,
    }

    # Estimate token count (rough estimate: 1 token ≈ 4 characters)
    estimated_tokens = len(full_text) // 4
    logger.info("Estimated tokens: %s", estimated_tokens)

    comprehensive_summary_data = None
    content_for_tier2 = full_text

    # Try to generate Tier2 directly with full_text first
    tier2_prompt = f"""You are a legal analyst specializing in M&A regulatory proceedings.

    Always prioritize the filing's concrete legal or procedural function over its rhetorical tone. When possible, use the filer’s own language to describe what they are asking the agency or other parties to do, and avoid vague phrasing such as "raises concerns" or "highlights issues" when you can state the specific request, effect, or role of the filing in the proceeding.

COMPLETE DOCKET HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

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
            max_tokens=1000,
            temperature=0.3,
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
        comprehensive_summary_prompt = f"""You are summarizing a legal docket entry for further analysis. Create a comprehensive summary that preserves all important details.

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
            tier2_prompt_fallback = f"""You are a legal analyst specializing in M&A regulatory proceedings.

COMPLETE DOCKET HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

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
                max_tokens=1000,
                temperature=0.3,
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

    tier3_prompt = f"""You are a senior legal analyst providing risk assessment for an M&A transaction regulatory review.

    Always prioritize procedural and legal consequences over rhetorical intensity or the mere volume of comments when assessing risk. Focus on filings and orders that actually change the regulatory posture, timing, or available remedies.


    COMPLETE DOCKET HISTORY (Entries 1-{next_entry_number}):
    {historical_context}

    MOST RECENT ENTRY (#{next_entry_number}):
    {tier2_response}

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Based on ALL evidence from Entry #1 through #{next_entry_number}, provide comprehensive risk assessment:

    1. DEAL CHALLENGE RISK SCORE (0-100):

    Score: [X]

    Where:
    • 0-30: Limited opposition, mostly procedural concerns, deal structure sound
    • 31-60: Multiple substantive intervenors, significant concerns but deal viable with conditions
    • 61-100: Widespread strong opposition, fundamental public interest concerns, approval unlikely

    Reasoning (4-5 sentences):
    Ground this score in filings and actions that create concrete legal or procedural exposure,
    such as complaints, enforcement activity, adverse staff recommendations, motions directed
    at blocking or conditioning the deal, formal opposition from enforcement agencies or
    state regulators, or clear signals of potential litigation. Do not inflate the score based
    solely on the volume or emotional intensity of public comments or political rhetoric unless
    they have already produced identifiable procedural consequences (e.g., expanded discovery,
    new hearings, schedule changes). Cite specific entries by number to support your score.

    2. TIMING RISK SCORE (0-100):

    Score: [X]

    Where:
    • 0-30: Standard review timeline, few intervenors, proceeding smoothly
    • 31-60: Contested case, multiple intervenors, 6-12 month timeline
    • 61-100: Highly contested, procedural disputes, likely 12+ month delay

    Reasoning (4-5 sentences):
    Ground this score in events that directly affect timing, such as schedule changes,
    extensions of statutory deadlines, motions for more time, expanded discovery,
    additional hearing days, or procedural complications that make timely resolution
    unlikely. Do not infer high timing risk solely from controversy or public interest;
    tie it to actual orders, motions, or procedural bottlenecks in the docket.
    Cite specific entries by number to support your score.

    3. KEY RISK FACTORS 
    List the 3-5 most significant risk factors that have emerged, focusing on those that
    realistically affect (i) probability of deal challenge or litigation, (ii) likelihood or
    severity of remedies/conditions, and (iii) timing of closing.

    4. TRAJECTORY ASSESSMENT (3-4 sentences): 
    Looking at the arc from Entry #1 to #{next_entry_number}, explain whether the deal is
        strengthening or weakening from a regulatory risk perspective. Identify the key
        inflection points where the posture meaningfully changed (e.g., major enforcement
        filings, significant political interventions with procedural consequences, schedule
        changes, or major commitments by the parties), and cite those entries by number.

CRITICAL: You must provide numerical scores (0-100) for both risks. Be decisive and ground all assessments in specific entries from the docket history."""

    tier3_message = client.messages.create(
        model=TIER3_MODEL,
        max_tokens=2000,
        temperature=0.2,
        messages=[{"role": "user", "content": tier3_prompt}]
    )
    logger.info("Tier 3 prompt: %s", tier3_prompt)
    logger.info("Tier 3 response: %s", tier3_message)

    tier3_response = tier3_message.content[0].text
    tier3_input_tokens = tier3_message.usage.input_tokens
    tier3_output_tokens = tier3_message.usage.output_tokens
    tier3_cost = _estimate_cost(
        tier3_input_tokens, tier3_output_tokens, TIER3_MODEL)

    content = content_for_tier2

    tier1_prompt = f"""You are extracting key facts from a legal docket entry. Be concise and factual.

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

CONTENT:
{content}

Extract the key facts in 3-5 bullet points (max 500 words total):
- What type of filing is this?
- Who filed it and what do they want?
- What are the main arguments/concerns raised?
- Any commitments, recommendations, or conclusions?

Be factual and concise. Focus on substantive content, not procedural details."""

    tier1_message = client.messages.create(
        model=TIER1_MODEL,
        max_tokens=1000,
        temperature=0.1,
        messages=[{"role": "user", "content": tier1_prompt}]
    )
    logger.info("Tier1 message: %s", tier1_message)
    logger.info("Tier1 prompt: %s", tier1_prompt)

    tier1_summary = tier1_message.content[0].text.strip()
    tier1_input_tokens = tier1_message.usage.input_tokens
    tier1_output_tokens = tier1_message.usage.output_tokens
    tier1_cost = _estimate_cost(
        tier1_input_tokens, tier1_output_tokens, TIER1_MODEL)

    # Calculate total cost including comprehensive summary if generated
    total_cost = tier1_cost + tier2_cost + tier3_cost
    if comprehensive_summary_data:
        total_cost += comprehensive_summary_data["cost"]

    new_entry = {
        "hash_id": next_hash_id,
        "content": content_for_tier2,
        "deal_id": deal_id,
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
        "tier3_risk_assessment": {
            "response": tier3_response,
            "tokens": {
                "input": tier3_input_tokens,
                "output": tier3_output_tokens
            },
            "cost": tier3_cost
        },
        "total_analysis_cost": total_cost,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }

    # Add comprehensive summary field if generated
    if comprehensive_summary_data:
        new_entry["comprehensive_summary"] = comprehensive_summary_data["summary"] if comprehensive_summary_data else full_text

    inserted_id = None
    enrichment_scheduled = False
    if not test_mode:
        try:
            insert_result = collection.insert_one(new_entry)
            inserted_id = insert_result.inserted_id
            logger.info("✓ Saved entry to MongoDB _id=%s", inserted_id)
            if _should_schedule_enrichment(docket_type):
                _schedule_docket_enrichment(str(inserted_id), docket_type)
                enrichment_scheduled = True
            else:
                logger.info(
                    "Skipping enrichment for docket_type=%s (not in %s)",
                    docket_type,
                    sorted(DOCKET_TYPES_WITH_ENRICHMENT),
                )
        except Exception as e:
            logger.warning(
                "Failed to save to MongoDB: %s", str(e))
    elif _should_schedule_enrichment(docket_type):
        _schedule_docket_enrichment_test(new_entry)
        enrichment_scheduled = True

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
        "tier3_risk_assessment": {
            "response": tier3_response,
            "tokens": {
                "input": tier3_input_tokens,
                "output": tier3_output_tokens
            },
            "cost": tier3_cost
        },
        "total_cost": total_cost,
        "comprehensive_summary": comprehensive_summary_data["summary"] if comprehensive_summary_data else full_text,
        "timestamp": datetime.now().isoformat(),
        "database_updated": inserted_id is not None,
        # MongoDB native _id (string) for client reference only — not stored as a separate field
        "record_id": str(inserted_id) if inserted_id else None,
        "enrichment_scheduled": enrichment_scheduled,
        "deal_id": deal_id,
    }

    # Add comprehensive summary to result if generated
    if comprehensive_summary_data:
        result["comprehensive_summary"] = comprehensive_summary_data["summary"] if comprehensive_summary_data else full_text

    return result


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


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate API cost based on token usage"""
    pricing = {
        # Anthropic pricing (per 1M tokens)
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3-5-haiku-20241022": {"input": 0.8, "output": 4.0},
        "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
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


if __name__ == "__main__":
    import sys

    doc_num = "202510-224026-01"
    text = "Office of the City Council  \n                                         8650 California Av enue, South Gate, CA 90280  \nP: (323) 563 -9543 F: (323) 569 -2678  \nwww.cityofsouthgate.org         \n  \n \n \nJOSHUA BARRON  \n  Mayor          \n \nMay 13, 2026  \n \n \nVIA ELECTRONIC FILING  \nSurface Transportation Board  \n395 E Street SW  \nWashington, DC 20423  \n \nRe: Docket No. FD 36873 - Union Pacific Corporation and Union Pacific Railroad Company  \n– Control  – Norfolk Southern Corporation and Norfolk Southern Railway Company  –  \nRequest for conditions protecting environmental justice communities and facilitating \npublic reuse of inactive rail corridors  \n \nDear Members of the Board:  \n \nOn behalf of a coalition of Gateway and Southeast Los Angeles County cities, including the cities \nof South Gate, Cerritos, Cudahy , Downey , Huntington Park , Industry, Lakewood, La Mirada, \nLong Beach, Lynwood, Maywood, Paramount,  Pico Rivera , and  Santa Fe Springs  (collectively, \n“Gateway Cities”) , we respectfully submit these comments regarding the proposed transaction \nbetween Union Pacific Railroad (“Union Pacific”) and Norfolk Southern Railway (“Norfolk \nSouthern”) (collectively, “Applicants”).   On behalf of the Gateway Cities, we oppose the \nproposed transaction unless and until the concerns raised herein are addressed and effective \nenforcement mechanism is established.  \n \nAs Mayor of the City of South Gate, I submit these comments in collaboration with neighboring \njurisdictions that share longstanding concerns regarding the impacts of inactive, abandoned, and \nunderutilized rail corridors throughout  Southeast Los Angeles County and the Gateway Cities \nregion.  \n \nOur coalition respectfully requests that the Board impose conditions necessary to ensure that the \nproposed transaction satisfies the public interest requirements established under federal law, \nincluding mitigation measures addressing neglected rail infrast ructure, environmental justice \nconcerns, and the public reuse of inactive rail corridors within densely populated urban \ncommunities.  \n \nI.  PUBLIC INTEREST STANDARD AND BOARD AUTHORITY  \nUnder 49 U.S.C. § 11324(c), the Board may approve a transaction only if it determines that \nthe proposal is consistent with the public interest. The Board additionally retains broad soITth Gate·· \n          311345 \n \n        ENTERED \nOffice of Chief Counsel \n   May 13, 2026 \n          Part of  \n    Public Record \nSurface Transportation Board  \nMay 12, 2026  \nPage 2 of 4 \n \n \n \nauthority under 49 U.S.C. § 10101 to impose conditions necessary to mitigate adverse \nimpacts and further national transportation policy objectives, including:  \n• Ensuring safe and efficient rail transportation  \n• Promoting sound economic conditions  \n• Encouraging environmental protection and energy conservation  \n• Reducing adverse environmental and community impacts  \n• Fostering coordination between rail carriers and local communities  \n \nThe coalition respectfully submits that the Applicants’ management of inactive and \nunderutilized rail corridors throughout Southeast Los Angeles County raises concerns \ndirectly relevant to these statutory obligations.  \n \nII.  CONDITIONS WITHIN GATEWAY CITIES AND SOUTHEAST LOS ANGELES \nCOMMUNITIES  \nAcross our communities, segments of Union Pacific -owned rail corridors that are inactive, \nabandoned, or underutilized have become recurring sites of:  \n• Illegal dumping of solid and hazardous waste  \n• Fire hazards and criminal activity  \n• Encampments and public nuisance conditions  \n• Visual blight and environmental degradation  \n• Barriers to connectivity, mobility, and community reinvestment  \n \nThese impacts disproportionately burden densely populated working -class communities that \nare predominantly Latino and already experience some of the highest cumulative \nenvironmental burdens in California due to freight movement, industrial activity, and \ntransportation infrastructure.  \n \nThe continued neglect of these corridors undermines public safety, environmental quality, \nand economic revitalization efforts across the region and is inconsistent with the national rail \ntransportation policy goals established by Congress.  \n \nIII. REQUEST FOR CONDITIONS: PUBLIC USE, RAIL BANKING, AND \nCOMMUNITY REINVESTMENT  \nIn light of the foregoing, the coalition respectfully requests that the Board condition any \napproval of the proposed transaction upon the Applicants’ agreement to the following:  \n \nA.  Inventory and Public Disclosure  \nConduct and publicly disclose a comprehensive inventory identifying:  \n• All inactive, abandoned, or underutilized rail corridors located within urbanized \nareas served by the Applicants  \n• Current operational status and future operational plans for such corridors  \n• Corridors suitable for interim public use or rail banking opportunities  \nSurface Transportation Board  \nMay 12, 2026  \nPage 3 of 4 \n \n \n \n \nB.  Good Faith Negotiations with Local Jurisdictions  \nEngage in good faith negotiations with affected municipalities regarding:  \n• Interim public use under 49 C.F.R. § 1152.28 (Public Use Condition)  \n• Long -term corridor preservation pursuant to the National Trails System Act, \n16 U.S.C. § 1247(d) (“rail banking”)  \n• Potential transfer, lease, or joint -use agreements for community -serving \ninfrastructure projects  \n \nC. Facilitation of Greenway and Active Transportation Projects  \nProvide reasonable accommodation and cooperation to facilitate conversion of inactive \ncorridors into:  \n• Bicycle paths and regional bike networks  \n• Pedestrian walkways and greenways  \n• Open space, recreational corridors, and climate -resilient infrastructure  \n• Multi -use mobility corridors connecting underserved communities   \nThese projects directly support regional, state, and federal goals relating to:  \no Climate resilience and greenhouse gas reduction  \no Active transportation and public health  \no Environmental remediation and sustainable land use  \no Equitable infrastructure investment in disadvantaged communities  \n \nD.  Maintenance and Interim Mitigation Measures  \nUntil reuse or redevelopment occurs, require Applicants to:  \n• Maintain inactive corridors in a condition that prevents illegal dumping and \nnuisance activity  \n• Implement appropriate fencing, signage, vegetation management, and security \nmeasures  \n• Coordinate with local governments regarding corridor maintenance and public \nsafety concerns  \n• Establish dedicated regional points of contact for municipal coordination  \n \nIV. ENVIRONMENTAL JUSTICE AND EQUITABLE INFRASTRUCTURE \nINVESTMENT  \nThe Board has increasingly recognized the importance of environmental justice \nconsiderations in evaluating major rail transactions. The communities represented by this \ncoalition have historically borne disproportionate impacts associated with freight movem ent, \nindustrial land uses, rail infrastructure, and transportation emissions.  \n \nThis transaction presents an opportunity not only to consolidate rail operations, but also to \nadvance equitable community investment by transforming neglected infrastructure into \nSurface Transportation Board  \nMay 12, 2026  \nPage 4 of 4 \n \n \n \npublic assets that improve quality of life, mobility, environmental conditions, and \nneighborhood connectivity.  \n \nCommunities that have long hosted the burdens associated with freight transportation should \nalso share in the benefits of reinvestment and infrastructure modernization.  \n \nV.  CONCLUSION  \nThe coalition of Gateway and Southeast Los Angeles County cities respectfully urges the \nBoard to condition any approval of the proposed transaction on meaningful commitments \nthat: \n• Address the impacts of inactive and neglected rail infrastructure  \n• Facilitate public reuse and rail banking opportunities  \n• Advance environmental justice objectives  \n• Improve safety, environmental conditions, and mobility within affected \ncommunities  \n \nAbsent such conditions, the proposed transaction risks perpetuating longstanding harms while \nfurther consolidating control over critical transportation infrastructure without corresponding \ncommunity accountability.  \n \nWe appreciate the Board’s consideration of these comments and stand ready to engage further \nregarding these matters.  Please feel free to contact me at jbarron@sogate.org  or via phone at (323) \n563-9543.  \n \nRespectfully submitted,  \n \n \nJoshua Barron  \nMayor, City of South Gate  on behalf of the coalition of the cities of:  \n \nCerritos  Lakewood  Paramount  \nCudahy  La Mirada  Pico Rivera  \nDowney  Long Breach  Santa Fe Springs  \nHuntington Park  Lynwood  South Gate  \nIndustry  Maywood   \n \ncc: U.S. Department of Transportation  \n U.S. Department of Justice (Antitrust Division)  \n UP & NS respective legal counsels  \n Parties of Record._______________"
    # metadata ={
    #     "docket_type": "PUC",
    #     "date": "2025-10-15",
    #     "document_type": "Public Comment",
    #     "additional_info": "Kristy M.",
    #     "on_behalf_of": "PUC",
    #     "docket_number": "24-198 (PA)",
    #     "document_id": "202510-224026-01"
    # }

    metadata = {
        "date": "2025-12-11T00:00:00.000Z",
        "document_type": "Filing",
        "additional_info": "UNION PACIFIC CORPORATION AND UNION PACIFIC RAILROAD COMPANY &mdash;CONTROL&mdash; NORFOLK SOUTHERN CORPORATION AND NORFOLK SOUTHERN RAILWAY COMPANY",
        "on_behalf_of": "Gateway Cities of Southeast Los Angeles",
        "docket_number": "FD-36873",
        "document_id": "https://dcms-external.s3.amazonaws.com/DCMS_External_PROD/1778713213720/311345.pdf",
        "docket_type": "stb-document"
    }

    result = analyze_docket_entry(doc_num, text, metadata)
# logger.info(json.dumps(result, indent=2))
