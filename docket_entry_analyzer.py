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
TIER2_MODEL = "claude-sonnet-5"
TIER3_MODEL = "claude-sonnet-4-6"
# Haiku Tier1 is ~200k context; summarize via OpenAI file upload when estimate exceeds this.
TIER1_MAX_ESTIMATED_TOKENS = 200_000


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
    "va-puc",
    "CPUC",
})

# Restrict enrichment to specific docket numbers when a type has multiple dockets.
_ENRICHMENT_DOCKET_NUMBERS: Dict[str, frozenset] = {
    "CPUC": frozenset({"A2507016"}),
}

# Maps docket collection type → dashboard_docket_type for enrich_docket_entry
_DOCKET_TO_DASHBOARD_TYPE: Dict[str, str] = {
    "stb-document":             "stb",
    "stb-environmentalComment": "stb",
    "mt-psc":                   "mt-psc",
    "sd-puc":                   "sd-puc",
    "nm-prc":                   "nm-prc",
    "ne-psc":                   "ne-psc",
    "va-puc":                   "va-puc",
    "CPUC":                     "CPUC",
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


def _should_schedule_enrichment(docket_type: str, docket_number: str = "") -> bool:
    if docket_type not in DOCKET_TYPES_WITH_ENRICHMENT:
        return False
    allowed_numbers = _ENRICHMENT_DOCKET_NUMBERS.get(docket_type)
    if allowed_numbers is None:
        return True
    return docket_number in allowed_numbers


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
        max_wait_time = 600  # 5 minutes max
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
        test_mode:
    Returns:
        Dictionary containing analysis results with tier1, tier2 and tier3 responses
    """
    global LOG_FILE

    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    _load_env_file(ENV_FILE)

    logger.info("Analyzing docket entry: %s",
                metadata.get("document_id", "N/A"))

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

    # Tier1 uses Haiku (~200k). Tier2/Tier3 stay unchanged.
    # Prefer an existing Tier2-fallback comprehensive summary when present.
    # Otherwise, if estimated tokens exceed the limit, generate one via OpenAI
    # file upload for Tier1 only — then store/return it as comprehensive_summary.
    content = content_for_tier2
    if comprehensive_summary_data:
        # Tier2 fallback already produced a summary — reuse for Tier1 (no re-upload).
        content = comprehensive_summary_data["summary"]
        logger.info(
            "✓ Tier1 will reuse existing comprehensive summary from Tier2 path (%s chars)",
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

    tier1_used_summary = bool(comprehensive_summary_data)

    def _build_tier1_prompt(tier1_content: str) -> str:
        return f"""You are extracting key facts from a legal docket entry. Be concise and factual.

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

    def _call_tier1(tier1_content: str, used_summary: bool):
        tier1_prompt = _build_tier1_prompt(tier1_content)
        logger.info(
            "Generating Tier1 summary (content_chars=%s, est_tokens=%s, used_summary=%s)",
            f"{len(tier1_content):,}",
            f"{estimated_tokens:,}",
            used_summary,
        )
        message = client.messages.create(
            model=TIER1_MODEL,
            max_tokens=1000,
            temperature=0.1,
            messages=[{"role": "user", "content": tier1_prompt}]
        )
        logger.info("Tier1 message: %s", message)
        return message

    try:
        tier1_message = _call_tier1(content, tier1_used_summary)
    except Exception as tier1_error:
        if not _is_context_length_error(tier1_error):
            logger.error("Tier1 generation failed: %s", str(tier1_error))
            raise

        logger.warning(
            "Tier1 failed due to context/token limit: %s",
            str(tier1_error),
        )

        # Only upload+retry when the failed call still used full/large text.
        # If we already sent a comprehensive_summary, do not retry the same blob.
        if tier1_used_summary:
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
        tier1_used_summary = True
        logger.info(
            "✓ Retrying Tier1 with comprehensive summary (%s chars)",
            f"{len(content):,}",
        )
        try:
            tier1_message = _call_tier1(content, tier1_used_summary)
        except Exception as tier1_retry_error:
            logger.error(
                "Tier1 retry after comprehensive summary also failed: %s",
                str(tier1_retry_error),
            )
            raise

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

    # Store comprehensive_summary only when it was generated (Tier1 size gate
    # when est. tokens > 200k, or Tier2 fallback). Otherwise callers use full text.
    if comprehensive_summary_data:
        new_entry["comprehensive_summary"] = comprehensive_summary_data["summary"]

    inserted_id = None
    enrichment_scheduled = False
    if not test_mode:
        try:
            insert_result = collection.insert_one(new_entry)
            inserted_id = insert_result.inserted_id
            logger.info("✓ Saved entry to MongoDB _id=%s", inserted_id)
            if _should_schedule_enrichment(docket_type, docket_number):
                _schedule_docket_enrichment(str(inserted_id), docket_type)
                enrichment_scheduled = True
            else:
                logger.info(
                    "Skipping enrichment for docket_type=%s docket_number=%s",
                    docket_type,
                    docket_number,
                )
        except Exception as e:
            logger.warning(
                "Failed to save to MongoDB: %s", str(e))
    elif _should_schedule_enrichment(docket_type, docket_number):
        _schedule_docket_enrichment_test(new_entry)
        enrichment_scheduled = True

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
        "tier3_risk_assessment": {
            "response": tier3_response,
            "tokens": {
                "input": tier3_input_tokens,
                "output": tier3_output_tokens
            },
            "cost": tier3_cost
        },
        "total_cost": total_cost,
        "comprehensive_summary": comprehensive_summary_out,
        "timestamp": datetime.now().isoformat(),
        "database_updated": inserted_id is not None,
        # MongoDB native _id (string) for client reference only — not stored as a separate field
        "record_id": str(inserted_id) if inserted_id else None,
        "enrichment_scheduled": enrichment_scheduled,
        "deal_id": deal_id,
    }

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

    doc_num = "https://dcms-external.s3.amazonaws.com/DCMS_External_PROD/1784898488020/311833.pdf"
    text = "Lindsay Williams \n1322 Milby St. \nHouston, TX 77003 \nwilliamslindsay123@gmail.com \nJuly 23, 2026 \nSurface Transportation Board \n395 E Street, S.W. \nWashington, DC 20423 Re:  Finance Docket No. 36873, Union Paci ﬁc Corporation and Norfolk Southern Corporation, \nControl and Merger \nComment on the Reliability of the Applicants’ Houston Tra ﬃc Data and the Operational Impacts \nthe Application Does Not Count \nTo the Members of the Board: \nI am a resident of Eastwood in Houston’s East End, and I have previously submitted comments in this \ndocket regarding blocked crossings and train length. This comment addresses a more basic concern: \nwhether the data being presented to the Board accurately reﬂects actual conditions. \nApplicants’ tra ﬃc ﬁgures are inconsistent with their own operating data, federal research, State of Texas \ndata, City of Houston crossing sensor records, and observable conditions documented by residents. \nGiven these discrepancies, the Board should require validation of the underlying data before relying on \nthe Applicants’ impact projections for communities such as mine. \nExisting Houston Terminal Rail Tra ﬃc Undercounted \nThe Applicants’ ﬁling presents two materially di ﬀerent representations of rail activity in Houston. The \noperating plan workpapers submitted as Document 311221 project post-merger tra ﬃc using a model \nthat counts only UP and NS road trains (Electronic Appendix T). The same ﬁling’s 2024 density data \n(Electronic Appendix C) re ﬂects actual network activity, including local trains and movements by other \nrailroads operating over UP infrastructure. \nThose two data sets show substantially di ﬀerent levels of activity on the very rail segments that run \nthrough East Houston neighborhoods: \nHouston line segment Operating plan, \nroad trains only \n(App. T) Actual 2024 \ndensity, all trains \n(App. C) Independent \nmeasurement \nTerminal Sub, Tower 26 to Tower 87 34 to 41 per day 85 per day TxDOT: East Belt \n80 to 90; West Belt \n65 to 75 (all trains) \nPalestine Sub, Spring to Belt \nJunction 15.5 per day 74 per day \n          311833 \n \n        ENTERED  \nOffice  of  Chief Counsel \n    July 23, 2026  \n          Part of   \n    Public Record \nGalveston\n \nSub\n \n(GH&H),\n \nTower\n \n30\n \nsouth\n \n1.3\n \nper\n \nday\n \nup\n \nto\n \n21\n \nper\n \nday\n \nTxDOT:\n \n7-8\n \nper\n \nday;\n \nresident\n \nvideo\n \ncounts,\n \nsee\n \nbelow\n \nSealy\n \nto\n \nAlvin\n \n(shared\n \nBNSF\n \nline)\n \n1.4\n \nper\n \nday\n \nup\n \nto\n \n49\n \nper\n \nday\n \n \n \nBoth\n \nsets\n \nof\n \nﬁgures\n \ncome\n \nfrom\n \nthe\n \nApplicants’\n \nown\n \nﬁling.\n \nThe\n \ndiﬀerence\n \nis\n \nnot\n \nthe\n \nunderlying\n \nlocation—it’s\n \nthe\n \nmethod\n \nof\n \ncounting.\n \nThe\n \noperating\n \nplan\n \nexcludes\n \nlocal\n \ntrains\n \nand\n \nyard\n \nmovements\n \nand\n \ndoes\n \nnot\n \ninclude\n \ntrains\n \nfrom\n \nother\n \ncarriers\n \n–\n \nincluding\n \nBNSF,\n \nAmtrak,\n \nand\n \nCPKC\n \n–\n \noperating\n \nover\n \nthe\n \nsame\n \ninfrastructure.\n \nThe\n \nTexas\n \nDepartment\n \nof\n \nTransportation’s\n \nHouston-Beaumont\n \nRegion\n \nFreight\n \nStudy\n \nfound\n \nthat\n \nthese\n \ntypes\n \nof\n \nmovements\n \nrepresent\n \na\n \nsigniﬁcant\n \nshare\n \nof\n \nHouston\n \nnetwork\n \nrail\n \nactivity.\n \nResidents\n \nexperience\n \nevery\n \ntrain\n \nmovement,\n \nregardless\n \nof\n \nhow\n \nit\n \nis\n \ncategorized\n \nin\n \na\n \nmodeling\n \nexercise.\n \nWhen\n \nApplicants\n \nstate\n \na\n \nsegment\n \nwill\n \nexperience\n \n“one\n \nmore\n \ntrain\n \nper\n \nday,”\n \nthat\n \nincrease\n \nis\n \nbeing\n \nmeasured\n \nagainst\n \na\n \nbaseline\n \nthat\n \nexcludes\n \na\n \nsubstantial\n \nportion\n \nof\n \nthe\n \ntrains\n \nalready\n \nmoving\n \nthrough\n \nthese\n \ncommunities.\n \nApplicants’\n \nCertiﬁed\n \nCrossing\n \nData\n \nFails\n \nIndependent\n \nReview\n \nThe\n \nrailroad’s\n \ncertiﬁed\n \ncrossing\n \ndata\n \npresents\n \na\n \npicture\n \nof\n \nrail\n \nactivity\n \nthat\n \nis\n \ninconsistent\n \nwith\n \nmultiple\n \nindependent\n \nsources.\n \nUnder\n \n49\n \nCFR\n \n234.409,\n \nthe\n \noperating\n \nrailroad\n \nmust\n \ncertify\n \naccurate\n \nand\n \ncurrent\n \ntrain\n \ncounts\n \nfor\n \nevery\n \ncrossing\n \nat\n \nleast\n \nonce\n \nevery\n \nthree\n \nyears.\n \nAt\n \nHirsch\n \nRoad\n \nin\n \nHouston’s\n \nFifth\n \nWard\n \n(FRA\n \nCrossing\n \n755640L),\n \nUnion\n \nPaciﬁc\n \nhas\n \nrepeatedly\n \ncertiﬁed\n \nthat\n \nzero\n \nthrough\n \ntrains\n \noperate\n \nat\n \nthe\n \ncrossing,\n \nincluding\n \na\n \ncertiﬁcation\n \nsubmitted\n \nas\n \nrecently\n \nas\n \nMarch\n \n2025.\n \nYet\n \nCity\n \nof\n \nHouston\n \ncrossing\n \nsensors\n \nrecord\n \napproximately\n \neight\n \ndaily\n \ntrain\n \nmovements,\n \nwith\n \neach\n \nmovement\n \nblocking\n \nthe\n \nroadway\n \nfor\n \nan\n \naverage\n \nof\n \n21\n \nminutes.\n \nThe\n \nFRA-sponsored\n \nresearch\n \nreport\n \n“Assessing\n \nthe\n \nSafety\n \nBeneﬁts\n \nof\n \na\n \nReal-Time\n \nRailroad\n \nCrossing\n \nInformation\n \nSystem\n \nfor\n \nEmergency\n \nResponders”\n \n(Report\n \nDOT/FRA/ORD-25/02,\n \nJanuary\n \n2025,\n \nTable\n \n7)\n \ndocumented\n \nweekly\n \nemergency\n \nresponder\n \ndelays\n \nat\n \na\n \ncrossing\n \nwhere\n \nUnion\n \nPaciﬁc\n \ncertiﬁes\n \nzero\n \nthrough\n \ntrains.\n \nTxDOT’s\n \nHouston-Beaumont\n \nRegion\n \nFreight\n \nStudy\n \nlikewise\n \nidentiﬁed\n \nthe\n \nlocation\n \nfor\n \npotential\n \ngrade\n \nseparation\n \nbased\n \non\n \nrecurring\n \ncrossing\n \nimpacts.\n \nThree\n \narms\n \nof\n \ngovernment\n \nhave\n \ndocumented\n \ntrain\n \nactivity\n \nat\n \na\n \ncrossing\n \nthe\n \nrailroad\n \ncertiﬁes\n \nas\n \nhaving\n \nno\n \nthrough\n \ntrains.\n \n \nBelow\n \nis\n \na\n \nscreenshot\n \nof\n \nan\n \ninteractive\n \nchart\n \nmade\n \nto\n \ndemonstrate\n \nthe\n \ndiﬀerences\n \nin\n \nreported\n \ntrains\n \nfrom\n \nUnion\n \nPaciﬁc\n \nvs\n \nthe\n \nCity\n \nof\n \nHouston\n \nSensor\n \nprogram:\n \n \n \n \nAt South Lockwood Drive and York Street, the certi ﬁed counts of 4 through trains and 16 switching \nmovements have remained essentially unchanged since they were ﬁrst reported by the Galveston, \nHouston and Henderson in the 1970s; Union Paci ﬁc re-certi ﬁed those same ﬁgures in August 2025.  \nBy contrast, at Commerce Street, the certi ﬁed count of 25 matches City sensor data exactly, \ndemonstrating that accurate reporting is achievable. The complete revision history for eight East \nHouston crossings, compiled from FRA’s public historical ﬁle, is included as an exhibit. \nHirsch Road FRA Crossing 755640L • Settegast area, Fifth Ward \n--through trains/day (UP filing) • • • • switching moves/day (UP filing) \n20 , ............................................................. . \n15 \n10 __________________________ , _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ ciky sensors measure -8/day _ \n0 ----- o:----•------------\n•-----------•- ... ·---------' •- • I I I I 1970 1975 1980 1985 1990 1995 2000 2005 2010 2015 2020 2025 \nCity sensors: about 8 movements/day averaging 21 minutes each. UP has certified ZERO through trains since May 2017, most recently March 2025. \nI City sensors: about 8 movements/day averaging 21 minutes each. UP has certified ZERO through trains since May 2017, most recently March 2025. \nT Full revision record (29 filings) \nRevision date Through/day Switching/day Filed by Reason given \n1976-02-24 6 0 SP Change in Data \n1981-09-16 6 0 SP Change in Data \n1983-02-23 6 0 SP Change in Data \n1983-07-26 6 0 SP Change in Data \n1988-03-28 2 20 SP Change in Data \n1991-02-11 2 20 SP Change in Data \n1991-09-23 2 20 SP Change in Data \n1992-07-29 2 20 SP Change in Data \n1994-03-01 2 20 SP Change in Data \n1998-03-18 2 20 UP Change in Data \n1999-11-04 2 20 UP Change in Data \n2004-08-23 2 20 UP Change in Data \n2009-01-01 2 20 UP Change in Data \n2011-01-01 2 20 UP Change in Data \n2012-01-01 2 20 UP Change in Data \n2016-02-25 6 0 UP Change in Data \n2016-11-11 6 0 UP Change in Data \n2017-05-18 0 3 UP Change in Data \n2018-12-20 0 3 UP Change in Data \n2019-01-08 0 3 UP Change in Data \n2019-03-18 0 3 UP Admin Correction \n2019-05-13 0 3 UP Admin Correction \n2020-06-19 0 3 UP Admin Correction \n2021-07-20 0 3 UP Change in Data \n2021-08-31 0 3 UP Change in Data \n2022-09-06 0 3 UP Date Change Only \n2022-10-14 0 3 UP Change in Data \n2024-12-30 0 3 UP Change in Data \n2025-03-12 0 3 UP Change in Data \n \n \nThe data must be evaluated in context. At ﬁrst glance, the graph above may suggest that City sensor \ndata falls between Union Paci ﬁc’s reported through-train and switching-train counts. However, that \ninterpretation overlooks a critical issue: according to discussions with Union Paci ﬁc operations personnel \nand Gulf Coast Rail District representatives, this segment does not regularly experience switching \nactivity. If so, the reported switching-movement ﬁgures raise further questions about the reliability of the \nunderlying data. \nThe State of Texas could not verify these numbers either. The 2024 Texas Rail Plan, the State’s \nfederally required rail planning document, lists “Average Number of Trains per Day: Unknown” for every \nUnion Paciﬁc subdivision in the Houston Service Unit, and ﬂags several entries as unable to be \nconﬁrmed. If the State of Texas cannot obtain or verify train counts from this railroad for a statutory \nplanning document, the Board should not assume the counts in a merger application are reliable. \nCommunity measurement keeps ﬁnding the same thing. The City of Houston’s smart crossing \nsensors, reported monthly through the Rail Safety Task Force, measure daily train activity at seven East \nEnd crossings. Trainfo sensors collect data at 94 crossings. In addition, residents conduct manual video \ncounts on the Galveston Subdivision, with results consistently exceeding the volumes reported in \nApplicants’ operating plan.  \n \nSouth Lockwood Drive FRA Crossing 859523F. East End (GH&H line) \n20 -through trains/day (UP filing) • • • • switching moves/day (UP filing) \n15 \n10 _______________________________________________________________ city sensors measure-9/day_ \n1970 1975 1980 1985 1990 1995 2000 2005 2010 2015 2020 2025 \nI City sensors: about 9 movements/day . The 4 through+ 16 switching figures date to the 1970s GH&H filings. \n% of Trains w/ Blockage >10 Minutes 15% 26% 34% 26% 24% 34% 22% 25% 23% 28% 27% 26% 26% 26% \nTotal Duration of Blockages (hrs) 10.80 17.10 44.55 30.78 30.40 36.77 28.73 16.15 24.47 25.52 18.32 17.52 301.10 25.09 \n% of month w/ a Blockage 2% 3% 6% 4% 4% 5% 4% 4% 3% 4% 3% 5% 4% 4% \nLongest Single Blockage (Hours) 3.63 1.75 2.87 3.62 3.20 2.25 4.73 2.22 2.32 6.20 1.13 5.18 6.20 3.26 \n5.25 7.46 7.68 7.67 8.03 8.57 8.10 8.50 7.10 6.50 6.65 6.71 7.41 7.35 \n34.11 29.96 32.60 30.78 29.90 25.07 31.35 26.92 29.96 34.02 26.17 43.79 374.62 31.22 \n\" 28 31 30 31 30 31 18 30 \" 23 ,. 312 \" \"7days \"15days \"7days '7days \nmissing missing missing missing \n \nNotes about the above: The counts above were derived from manual review of surveillance camera \nfootage. This location provides a useful proxy for South Lockwood crossing (FRA 859523F) because it is \nlocated one block away. A train occupying approximately 14 cars east of this point can block South \nLockwood. Accordingly, observed train movements at this crossing closely correspond to the \nmovements aﬀecting South Lockwood in both directions. \nThe reported daily train average is calculated using the total number of trains observed divided by the \nnumber of days for which data was collected, not the total number of calendar days in the month. \nWhere measurement exists, it agrees with Appendix C and disagrees with the picture the plan presents. \nWhat this means for the application. The projected impacts in this docket—including added trains per \nday, blocked crossing estimates, and the mitigation commitments sized to those estimates—depend on \nthe accuracy of the underlying baseline. If that baseline understates existing rail activity in aﬀected \ncommunities, then the resulting impact projections will also understate potential harms, and the \ncorresponding mitigation measures may be insuﬃcient. \nThese discrepancies also a ﬀect the weight the Board should give the Applicants’ analysis. The same \nApplicant asking the Board to rely on its operating model has certi ﬁed crossing data that appears \ninconsistent with independent data, including research sponsored by its own regulator. The Board \nshould require the underlying data to be validated before relying on projections derived from it. \nThe impacts the application does not count are the ones we live with. The plan’s counting rules \nexclude precisely the operations that block our streets longest. In my neighborhood, the railroad builds \ntrains, idles them, and performs air brake tests across residential crossings. At Leeland Street (FRA \nCrossing 288224V) and Cullen Boulevard (FRA Crossing 288221A), I observe trains being assembled \ndaily and idling for hours. The federal record corroborates this: 1,198 blocked-crossing reports totaling \nabout 1,659 hours at that crossing from 2022 through 2025, and the City’s sensors log 10 to 20 \nblockages exceeding 1 hour every month. None of that activity appears in the plan’s trains-per-day. \n% of Trains w/ Blockage >10 Minutes 30% 28% 31% 34% 32% 25% 23% 30% 29% \nTotal Duration of Blockages (hrs) 17.20 26.72 36.05 32.72 32.85 -7.27 4.70 142.97 20.42 \n% of month w/ a Blockage 3% 4% 5% 5% 8% -1% 2% 4% 4% \nLongest Single Blockage (Hours) 2.25 2.20 1.77 2.40 12.33 1.37 0.77 12.33 3.30 \n# of Trains par Day 6.57 7.29 7.29 7.32 7.76 7.08 6.90 7.19 7.17 \nAvg. Length of Blockage (Minutes) 26.76 27.64 30.90 31.16 46.93 22.50 17.62 203.51 29.07 \n21 28 31 25 17 24 10 156 22 \n\"5days '1Jdays \nmissing missing \n \nSimilar to the Eastwood and Lockwood crossings, Leeland and Cullen are only ~100 yards apart. When \none has a passing train, the other has the same amount.  \nThe physical cause is documented in my prior exhibits in this docket. Union Paci ﬁc’s average train is \nnow 9,729 feet by its own reporting, and trains of 14,000 to 16,000 feet operate through the Houston \ncomplex, while the East End yard tracks where those trains are assembled hold roughly 5,000 feet. A \n15,000-foot train built in a 5,000-foot yard leaves about two miles of train hanging on the main line and \nacross neighborhood crossings while it is assembled and brake tested. The merger proposes to add \ntrains to this same plant: ﬁve more per day on the Lafayette corridor, about two more across the Terminal \nSubdivision and Glidden corridors, with the busiest segment reaching 51 trains a day by the plan’s own \ncount, which excludes the switching just described. \nOne crossing shows the arithmetic. At South Lockwood Drive (FRA Crossing 859523F), using only the \nﬁgures Union Paci ﬁc has certi ﬁed to FRA, 4 through trains and 16 switching movements- the crossing is \ninaccessible roughly seven hours a day in a neighborhood near schools, rising further with the additional \ntrain the growth plan assigns to that segment, a 25 percent increase in through traﬃc.  \nIf Union Paci ﬁc answers that the true ﬁgure is lower, then the data it certi ﬁed under 49 CFR 234.409 is \nwrong and has been wrong for decades. Either the certi ﬁed data is accurate, and the crossing is \nunavailable for much of the day, or the certi ﬁed data is inaccurate, and the Board is being asked to \napprove a merger on numbers the railroad itself cannot stand behind.  \nMeasured conservatively from City sensor data alone, the crossing is occupied or blocked between two \nand three hours a day before the merger adds anything. The State of Texas has already documented the \nmechanism at this crossing. The 2024 Houston-Beaumont Region Freight Study Update (draft, April \n2024) found that trains delayed by West Belt congestion hold between GH&H Junction and Tower 85 \nand “will block grade crossings,” naming Oakhurst Street, Eastwood Street, Lockwood Drive (859523F), \nDumble Street, and Altic Street. The same study describes 8,500 feet of track clear of crossings as “a \nrarity in the complex.” The state’s remedy for the blockage at my crossings is infrastructure; nothing in \nthe Applicants’ plan builds it. \nThe merger crosses the Board’s own environmental thresholds, by the Applicants’ own numbers. \nAll eight counties of the Houston complex are a Clean Air Act ozone nonattainment area, so the Board’s \nenvironmental rules apply their lower screening thresholds: under 49 CFR 1105.7(e)(5)(ii), air quality \nanalysis and a State Implementation Plan conformity statement are required where a proposal increases \nLeeland St/ Cullen Blvd FRA Crossing 288224V • East End \n-through trains/day (UP filing) •••• switching moves/day (UP filing) •------------------------, 50 \n40 ......... \n30 \n1970 1975 1980 1985 1990 1995 2000 2005 2010 2015 2020 2025 \nI City sensors: about 20 movements/day . Current filing of 20 matches. \ntraﬃc\n \nby\n \nat\n \nleast\n \nthree\n \ntrains\n \na\n \nday\n \nor\n \n50\n \npercent\n \nin\n \ngross\n \nton\n \nmiles\n \non\n \nany\n \nsegment,\n \nor\n \nincreases\n \nrail\n \nyard\n \nactivity\n \nby\n \nat\n \nleast\n \n20\n \npercent\n \nin\n \ncarloads.\n \nThe\n \nApplicants’\n \nworkpapers\n \ncross\n \nboth.\n \nThe\n \nLafayette\n \nSubdivision\n \nsegments\n \nfrom\n \nDawes\n \nto\n \nTower\n \n87,\n \nfrom\n \nDawes\n \nto\n \nDayton,\n \nand\n \nfrom\n \nDayton\n \nto\n \nBeaumont\n \neach\n \ngain\n \nexactly\n \n5\n \ntrains\n \nper\n \nday.\n \nAnd\n \nthe\n \nApplicants’\n \nown\n \nthreshold\n \nscreening,\n \nElectronic\n \nAppendix\n \nQ\n \nof\n \nDocument\n \n311221,\n \nlists\n \nEnglewood\n \nYard\n \nat\n \n2,044\n \ncars\n \nper\n \nday\n \nin\n \nthe\n \nbase\n \nplan\n \nrising\n \nto\n \n2,456\n \nin\n \nthe\n \ngrowth\n \nplan,\n \nan\n \nincrease\n \nof\n \n20.1\n \npercent,\n \nthe\n \nsecond\n \nlargest\n \nyard\n \nincrease\n \nin\n \nthe\n \ncountry\n \non\n \ntheir\n \nlist.\n \nThe\n \nyard\n \nthat\n \ncrosses\n \nthe\n \nBoard’s\n \nenvironmental\n \nthreshold\n \nis\n \nthe\n \nsame\n \nyard\n \nwhose\n \ntrain\n \nbuilding,\n \nidling,\n \nand\n \nbrake\n \ntesting\n \nalready\n \nspill\n \nacross\n \nour\n \nresidential\n \ncrossings.\n \nYet\n \nthe\n \nyard\n \ndata\n \nunderlying\n \nthat\n \ndetermination,\n \nElectronic\n \nAppendices\n \nF,\n \nJ,\n \nand\n \nN,\n \nis\n \nredacted\n \nas\n \nhighly\n \nconﬁdential,\n \nso\n \nthe\n \ncommunities\n \nmost\n \naﬀected\n \ncannot\n \nverify\n \na\n \nﬁgure\n \nthat\n \nclears\n \nthe\n \n20\n \npercent\n \nline\n \nby\n \nbarely\n \none-tenth\n \nof\n \na\n \npercentage\n \npoint,\n \ncomputed\n \non\n \nthe\n \nsame\n \nunveriﬁed\n \nbaseline\n \ndescribed\n \nabove.\n \nWhat\n \nI\n \nask\n \nthe\n \nBoard\n \nto\n \ndo.\n \n1.\n \nRequire\n \nthe\n \nApplicants\n \nto\n \nreconcile\n \nElectronic\n \nAppendices\n \nC\n \nand\n \nT\n \nof\n \nDocument\n \n311221\n \nfor\n \nthe\n \nHouston\n \ncomplex,\n \nand\n \nto\n \nstate,\n \ncrossing\n \nby\n \ncrossing,\n \nthe\n \ncurrent\n \nand\n \nprojected\n \ndaily\n \ntrain\n \nmovements\n \nincluding\n \nlocal\n \ntrains,\n \nyard\n \nand\n \nswitching\n \nmovements,\n \nand\n \ntenant\n \nrailroads,\n \nand\n \nto\n \nstate\n \nwhether\n \nthe\n \noperating\n \nplan\n \nassumes\n \nconstruction\n \nof\n \nany\n \nof\n \nthe\n \nrail\n \nimprovements\n \nidentiﬁed\n \nin\n \nthe\n \n2024\n \nUpdate\n \nof\n \nthe\n \nHouston-Beaumont\n \nRegion\n \nFreight\n \nStudy.\n \n2.\n \nRequire\n \nindependent\n \nveriﬁcation\n \nof\n \nthe\n \nHouston\n \nbaseline\n \nbefore\n \nthe\n \noperating\n \nplan\n \nis\n \ngiven\n \nevidentiary\n \nweight,\n \nusing\n \nthe\n \nCity\n \nof\n \nHouston\n \nsensor\n \nprogram,\n \nthe\n \nEast\n \nEnd\n \nDistrict’s\n \nTrainfo\n \ndata\n \nfor\n \n94\n \ncrossings,\n \nand\n \ncommunity\n \ncounts,\n \nand\n \ndirect\n \nthat\n \nthe\n \nenvironmental\n \nreview\n \nuse\n \nthe\n \nveriﬁed\n \nbaseline\n \nrather\n \nthan\n \nthe\n \nplan’s\n \nroad-train\n \ncounts.\n \n3.\n \nRefer\n \nthe\n \ncrossing\n \ninventory\n \ndiscrepancies\n \nidentiﬁed\n \nhere,\n \nincluding\n \nHirsch\n \nRoad,\n \nto\n \nthe\n \nFederal\n \nRailroad\n \nAdministration\n \nfor\n \ncorrective\n \naction\n \nunder\n \n49\n \nCFR\n \nPart\n \n234,\n \nSubpart\n \nF.\n \n4.\n \nRequire\n \nthe\n \nair\n \nquality\n \nanalysis\n \nand\n \nState\n \nImplementation\n \nPlan\n \nconformity\n \nstatement\n \ncontemplated\n \nby\n \n49\n \nCFR\n \n1105.7(e)(5)(ii)\n \nfor\n \nthe\n \nLafayette\n \nSubdivision\n \nsegments\n \ngaining\n \nﬁve\n \ntrains\n \na\n \nday\n \nand\n \nfor\n \nEnglewood\n \nYard’s\n \n20.1\n \npercent\n \ncarload\n \nincrease,\n \ncomputed\n \nfrom\n \na\n \nveriﬁed\n \nbaseline,\n \nand\n \ndirect\n \nthat\n \nthe\n \nyard\n \ndata\n \nunderlying\n \nElectronic\n \nAppendix\n \nQ\n \nbe\n \nmade\n \navailable\n \nto\n \nthe\n \nenvironmental\n \nreviewers\n \nand,\n \nunder\n \nappropriate\n \nprotective\n \norder,\n \nto\n \naﬀected\n \ncommunities.\n \n5.\n \nCondition\n \nany\n \napproval\n \non\n \nenforceable\n \nHouston\n \nterminal\n \nprotections:\n \nno\n \ntrain\n \nassembly,\n \nstaging,\n \nor\n \nair\n \nbrake\n \ntesting\n \nacross\n \npublic\n \ncrossings\n \nin\n \nresidential\n \nneighborhoods;\n \ntrain\n \nlength\n \nlimits\n \nfor\n \nthe\n \nHouston\n \ncomplex\n \nconsistent\n \nwith\n \nmy\n \nJune\n \n23,\n \n2026\n \ncomment;\n \nfunded\n \ngrade\n \nseparations\n \nat\n \nthe\n \ncrossings\n \nthe\n \nregion\n \nhas\n \nalready\n \nidentiﬁed;\n \nfunding\n \nof\n \nthe\n \nthree\n \nrail\n \nimprovements\n \nshortlisted\n \nin\n \nthe\n \n2024\n \nUpdate\n \nof\n \nthe\n \nstate’s\n \nHouston-Beaumont\n \nRegion\n \nFreight\n \nStudy\n \n(the\n \nBNSF\n \nMykawa\n \nsecond\n \ntrack,\n \nabout\n \n$75\n \nmillion;\n \nthe\n \nUPRR\n \nGalveston\n \nSubdivision\n \nsecond\n \ntrack\n \nfrom\n \nTower\n \n85\n \nto\n \nKaty\n \nNeck,\n \nabout\n \n$178\n \nmillion;\n \nand\n \nthe\n \nHB&T\n \nEast\n \nBelt\n \nsecond\n \ntrack\n \nover\n \nBuﬀalo\n \nBayou),\n \nwhich\n \nthe\n \nstate’s\n \nmodeling\n \nshows\n \nreduce\n \ndelay\n \nacross\n \nthe\n \ncomplex,\n \nincluding\n \na\n \n26\n \npercent\n \nreduction\n \non\n \nthe\n \nWest\n \nBelt;\n \nand\n \nbinding\n \nblocked\n \ncrossing\n \nreduction\n \ntargets\n \nmeasured\n \nagainst\n \nthe\n \nveriﬁed\n \nbaseline,\n \nwith\n \nﬁnancial\n \npenalties.\n \n6.\n \nUntil\n \nthe\n \ndata\n \nis\n \nvalidated,\n \ntreat\n \nthe\n \napplication’s\n \nHouston\n \nimpact\n \nprojections\n \nas\n \nunproven.\n \nCommunities\n \nlike\n \nmine\n \nare\n \nbeing\n \nasked\n \nto\n \nabsorb\n \nmore\n \ntrains\n \nbased\n \non\n \nnumbers\n \nthat\n \nno\n \nindependent\n \nparty\n \nhas\n \nbeen\n \nable\n \nto\n \nconﬁrm\n \nand\n \nthat\n \nthe\n \nApplicants’\n \nown\n \ndocuments\n \ncontradict.\n \n7.\n \nRequire\n \na\n \nfull\n \nimpact\n \nstudy\n \nfor\n \nthe\n \npost-merger\n \nblocked\n \ncrossing\n \nactivity\n \nfor\n \nthe\n \nHouston\n \ncomplex,\n \nwith\n \nthe\n \ncorrected\n \nand\n \nvalidated\n \ndata,\n \nand\n \nprovide\n \nthis\n \ndata\n \nfor\n \nfeedback\n \non\n \nmitigation,\n \nlong\n \nbefore\n \nan\n \nenvironmental\n \nstudy\n \nis\n \ncompleted.\n \n \nThis\n \ncomment\n \nis\n \nsubmitted\n \nfor\n \nthe\n \nmerits\n \nrecord\n \nand\n \nalso\n \nfor\n \nthe\n \nenvironmental\n \nreview\n \nrecord,\n \nso\n \nthat\n \nthe\n \nOﬃce\n \nof\n \nEnvironmental\n \nAnalysis\n \nhas\n \nit\n \nwhen\n \nthe\n \nenvironmental\n \nreview\n \nresumes.\n \nThe\n \nEast\n \nEnd\n \nhas\n \nmeasured,\n \nrecorded,\n \nand\n \ndocumented\n \nwhat\n \nwe\n \nlive\n \nwith.\n \nWe\n \nask\n \nthe\n \nBoard\n \nto\n \nrequire\n \nthe\n \nrailroad\n \nto\n \nmeet\n \nthe\n \nsame\n \nstandard\n \nbefore\n \nit\n \nis\n \nallowed\n \nto\n \nadd\n \nto\n \nit.\n \nRespectfully,\n \nLindsay\n \nWilliams\n \n1322\n \nMilby\n \nSt.\n \nHouston,\n \nTX\n \n77003\n \nExhibits\n \nreferenced:\n \nUP\n \nCrossing\n \nCount\n \nHistory\n \nExhibit\n \n(FRA\n \nForm\n \n71\n \nrevision\n \nhistories,\n \neight\n \nEast\n \nHouston\n \ncrossings,\n \n1970\n \nto\n \n2026,\n \nwith\n \nCity\n \nsensor\n \ncomparison);\n \nTrain\n \nCount\n \nValidation\n \nmemorandum\n \n(source\n \ncomparison\n \ntable);\n \nEast\n \nEnd\n \nControl-Point\n \nSpacing\n \nvs.\n \nModern\n \nTrain\n \nLength\n \nand\n \nHow\n \nLonger\n \nTrains\n \nBlock\n \nEast\n \nHouston,\n \npreviously\n \nﬁled.\n \nSources\n \ninclude\n \nSTB\n \nDocket\n \nFD\n \n36873\n \nDocument\n \n311221,\n \nElectronic\n \nAppendices\n \nC,\n \nQ\n \nand\n \nT;\n \n49\n \nCFR\n \n1105.7(e)(5)(ii);\n \nFRA\n \nCrossing\n \nInventory\n \nData,\n \nHistorical;\n \nFRA\n \nReport\n \nDOT/FRA/ORD-25/02\n \n(January\n \n2025);\n \nCity\n \nof\n \nHouston\n \nRail\n \nSafety\n \nTask\n \nForce\n \nsensor\n \nreports\n \n(2023\n \nto\n \n2026);\n \n2024\n \nTexas\n \nRail\n \nPlan,\n \nAppendix\n \nA,\n \nTable\n \nA-7;\n \nTxDOT\n \nHouston\n \nRegion\n \nFreight\n \nStudy\n \nand\n \nHouston-Beaumont\n \nRegion\n \nFreight\n \nStudy\n \n2024\n \nUpdate\n \n(draft,\n \nApril\n \n2024),\n \nSection\n \n4\n \nand\n \nAppendix\n \nF;\n \nEast\n \nEnd\n \nDistrict\n \nImpact\n \nStudy\n \n(May\n \n2023);\n \nFRA\n \nBlocked\n \nCrossing\n \nIncident\n \nReports\n \n(2022\n \nto\n \n2025).\n \n \n \n \n \n \n \n \n \n \n \n \n \n \n \nSource\n \nIndex\n \nFinance\n \nDocket\n \nNo.\n \n36873\n  \n|\n  \nComment\n \nof\n \nLindsay\n \nWilliams,\n \nEastwood,\n \nHouston,\n \nTX\n \nExhibits\n \nreferenced\n \nin\n \nthe\n \ncomment\n \nEx.\n \nTitle\n \nCont ents\n \nA\n \nEast\n \nEnd\n \nContr ol-Point\n \nSpacing\n \nvs.\n \nModern\n \nTrain\n \nLength\n \nTower -to-tower\n \nsegment\n \ndistances\n \n(from\n \nTxDO T\n \nmileposts)\n \ncompar ed\n \nwith\n \nmodern\n \ntrain\n \nlengths.\n \nB\n \nInteractive\n \nMaps\n \nInteractive\n \nmaps\n \nbuilt\n \nusing\n \nUPRR\n \nFRA\n \nCrossing\n \nInvent ory\n \nData\n \nto\n \nshow\n \nthe\n \nripple\n \neﬀect\n \nof\n \nstopped\n \ntrains\n \non\n \nadditional\n \nlines\n \nand\n \ndiscr epancies\n \nin\n \nwhat\n \nis\n \nbeing\n \nreported\n \nto\n \nthe\n \nFRA\n \nvs.\n \nwhat\n \nis\n \nbeing\n \nmeasur ed.\n \n \n \nSour ce\n \nindex\n \n#\n \nSour ce\n \nWher e\n \nto\n \nﬁnd\n \n/\n \nstatus\n \n1\n \nSTB\n \nDock et\n \nFD\n \n36873,\n \nDocument\n \n311221,\n \nElectr onic\n \nAppendices\n \nC,\n \nQ,\n \nand\n \nT\n \n(Applicants’\n \nﬁling).\n \nLocate\n \nin\n \nSTB\n \nrecord\n \nsearch\n;\n \ndock et\n \nmaterials\n \nalso\n \nat\n \nUP-NS\n \nEIS\n \nsite\n.\n \nConﬁrm\n \nthe\n \nexact\n \ndocument\n \nnumber .\n \n2\n \n49\n \nCFR\n \n1105.7(e)(5)(ii)\n \n(STB\n \nenvir onmental\n \nreport\n \ncontent).\n \neCFR\n \n49\n \nCFR\n \n1105.7\n \n3\n \nFRA\n \nHighway-Rail\n \nCrossing\n \nInvent ory\n \nData,\n \nhistorical\n \n(Form\n \n71).\n \nFRA\n \nCrossing\n \nInvent ory\n  \n(data\n \nportal:\n \ncrossings.dot.gov).\n \n4\n \nFRA\n \nRepor t\n \nDOT/FRA/ORD-25/02,\n \nJanuar y\n \n2025.\n \nFRA\n \neLibr ary.\n \nSearch\n \nthe\n \nreport\n \nnumber\n \nat\n \nrailroads.dot.gov/elibr ary\n.\n \n5\n \nCity\n \nof\n \nHoust on\n \nRail\n \nSafety\n \nTask\n \nForce\n \nsensor\n \nreports,\n \n2023\n \nto\n \n2026.\n \nYour\n \nﬁles\n \n/\n \nCity\n \nof\n \nHoust on\n \n(Smar t\n \nCity\n \nProgram).\n \nIncludes\n \nthe\n \nHoust on\n \nSafe\n \nRailroad\n \nCorridors\n \ndeck\n \nalready\n \nprovided.\n \n6\n \n2024\n \nTexas\n \nRail\n \nPlan,\n \nAppendix\n \nA,\n \nTable\n \nA-7.\n \n2024\n \nTexas\n \nRail\n \nPlan\n  \n|\n  \nAppendices\n \n(incl.\n \nAppendix\n \nA)\n \n7\n \nTxDO T\n \nHoust on\n \nRegion\n \nFreight\n \nStudy\n \n(Section\n \n4,\n \nAppendix\n \nF).\n \nOn\n \nﬁle\n \n(provided).\n \nOriginal\n \nTxDO T\n \nstudy .\n \n8\n \nHoust on-Beaumont\n \nRegion\n \nFreight\n \nStudy,\n \n2024\n \nUpdat e\n \n(draft,\n \nApril\n \n2024),\n \nSection\n \n4\n \nand\n \nAppendix\n \nF.\n \nHoust on-Beaumont\n \n2024\n \nUpdat e\n \n(draft)\n  \n|\n  \nstudy\n \npage\n \n9\n \nEast\n \nEnd\n \nDistrict\n \nImpact\n \nStudy,\n \nMay\n \n2023.\n \nEast\n \nEnd\n \nDistrict\n \nBusiness\n \nImpact\n \nStudy\n \n10\n \nFRA\n \nBlock ed\n \nCrossing\n \nIncident\n \nRepor ts,\n \n2022\n \nto\n \n2025.\n \nFRA\n \nBlock ed\n \nCrossing\n \nportal\n \n \nNote:\n \nLinks\n \npoint\n \nto\n \nthe\n \npublic\n \nsource.\n \nItems\n \nmark ed\n \nas\n \nneeding\n \ndata\n \nrequir e\n \nthe\n \neight\n \ncrossing\n \nIDs\n \n(Exhibit\n \nC)\n \nor\n \nthe\n \nCity\n \nsensor\n \nand\n \nTable\n \nA-7\n \nﬁgures\n \n(Exhibits\n \nC\n \nand\n \nD).\n \n \n \nExhibit\n \nA.\n \nEast\n \nEnd\n \nControl-Point\n \nSpacing\n \nvs.\n \nModern\n \nTrain\n \nLength\n \nFinance\n \nDocket\n \nNo.\n \n36873\n  \n|\n  \nComment\n \nof\n \nLindsay\n \nWilliams,\n \nEastwood,\n \nHouston,\n \nTX\n \nUnion\n \nPaciﬁc\n \nruns\n \ntrains\n \nlonger\n \nthan\n \nthe\n \ndistance\n \nbetween\n \nthe\n \ncontrol\n \npoints\n \nwhere\n \nthose\n \ntrains\n \ncan\n \nbe\n \nrouted\n \nor\n \nheld\n \nin\n \nthe\n \nEast\n \nEnd,\n \nso\n \na\n \nstopped\n \ntrain\n \nblocks\n \nseveral\n \nroad\n \ncrossings\n \nat\n \nonce.\n \nThe\n \nEast\n \nEnd\n \nterminal\n \nwas\n \nbuilt\n \nas\n \na\n \nlattice\n \nof\n \nclosely\n \nspaced\n \ncontrol\n \npoints,\n \nstill\n \nidentiﬁed\n \nby\n \ntheir\n \nhistoric\n \n“Tower”\n \ndesignations\n \nand\n \nused\n \noperationally\n \ntoday.\n \nUsing\n \nthe\n \nmileposts\n \nstated\n \nin\n \nthe\n \nTxDOT\n \nHouston\n \nRegion\n \nFreight\n \nStudy,\n \nthe\n \ndistance\n \nbetween\n \nadjacent\n \ncontrol\n \npoints\n \nruns\n \nfrom\n \nabout\n \n0.60\n \nmiles\n \nto\n \n3.89\n \nmiles:\n \nControl-point\n \nsegment\n \nSubdivisio \nn\n \nMileposts\n \nDistance\n \nLength\n \nin\n \nfeet\n \nTower\n \n71\n \nto\n \nTower\n \n210\n \nLufkin\n \nMP\n \n1.50\n \n–\n \n2.10\n \n0.60\n \nmi\n \n3,168\n \nft\n \nTower\n \n26\n \nto\n \nTower\n \n71\n \nLufkin\n \nMP\n \n0.74\n \n–\n \n1.50\n \n0.76\n \nmi\n \n4,013\n \nft\n \nTower\n \n86\n \nto\n \nTower\n \n85\n \nEast\n \nBelt\n \nMP\n \n7.60\n \n–\n \n9.40\n \n1.80\n \nmi\n \n9,504\n \nft\n \nTower\n \n210\n \nto\n \nTower\n \n76\n \nLufkin\n \nMP\n \n2.10\n \n–\n \n4.10\n \n2.00\n \nmi\n \n10,560\n \nft\n \nTower\n \n87\n \nto\n \nTower\n \n86\n \nEast\n \nBelt\n \nMP\n \n4.70\n \n–\n \n7.60\n \n2.90\n \nmi\n \n15,312\n \nft\n \nTower\n \n26\n \nto\n \nTower\n \n87\n \nTerminal\n \nMP\n \n356.80\n \n–\n \n360.69\n \n3.89\n \nmi\n \n20,539\n \nft\n \nDistances\n \nare\n \napproximate,\n \nderived\n \nfrom\n \nthe\n \nmileposts\n \nin\n \nthe\n \nTxDOT\n \nHouston\n \nRegion\n \nFreight\n \nStudy,\n \nExisting\n \nConditions\n \nsection.\n \nModern\n \ntrain\n \nlength,\n \nfor\n \ncomparison\n \n•\n  \nTxDOT\n \nstudy\n \nbenchmark:\n \na\n \n“long”\n \nmodern\n \ntrain\n \nof\n \n120\n \ncars,\n \n12,000\n \ntons,\n \nand\n \n7,200\n \nfeet.\n \n•\n  \nUnion\n \nPaciﬁc\n \nsystem\n \naverage:\n \n9,729\n \nfeet\n \n(Q4\n \n2025\n \nrecord),\n \nand\n \nrising.\n \n•\n  \nGAO:\n \nsome\n \ntrains\n \nnear\n \n14,000\n \nfeet\n \nby\n \n2021;\n \nrailroads\n \nrun\n \ntrains\n \nover\n \n12,000\n \nand\n \nover\n \n16,000\n \nfeet;\n \nabout\n \n25\n \npercent\n \nexceed\n \n7,500\n \nfeet.\n \nA\n \ntrain\n \nof\n \n14,000\n \nto\n \n16,000\n \nfeet\n \nis\n \nlonger\n \nthan\n \nﬁve\n \nof\n \nthe\n \nsix\n \nsegments\n \nabove.\n \nOn\n \nthose\n \nsegments,\n \na\n \nsingle\n \nstopped\n \ntrain\n \nis\n \nlonger\n \nthan\n \nthe\n \nentire\n \ndistance\n \nbetween\n \nthe\n \npoints\n \nwhere\n \nit\n \ncan\n \nbe\n \nrouted\n \nor\n \nheld,\n \nso\n \nit\n \nmust\n \nsit\n \nacross\n \nthe\n \nroad\n \ncrossings\n \nin\n \nbetween.\n \nThe\n \nTxDOT\n \nstudy\n \nreached\n \nthe\n \nsame\n \nconclusion,\n \nﬁnding\n \nthat\n \ndelay\n \nis\n \n“caused\n \nby\n \na\n \nlack\n \nof\n \nlong\n \nsidings\n \nwithout\n \ninterior\n \nroad\n \ncrossings,”\n \nand\n \nthat\n \non\n \nthe\n \nEast\n \nBelt\n \n“both\n \nmain\n \ntracks\n \nin\n \nthis\n \narea\n \nare\n \ncut\n \nby\n \nroad\n \ncrossings\n \nand\n \ntherefore\n \ntrains\n \ncannot\n \nhold\n \nbetween\n \nDouble\n \nTrack\n \nJunction\n \nand\n \nTower\n \n86\n \nto\n \nmeet\n \nor\n \npass\n \nother\n \ntrains.”\n \n \n \n \n \n \nExhibit\n \nB.\n \nInteractive\n \nMaps\n \n \nInteractive\n \nmap\n \ndemonstrating\n \nthe\n \nimpact\n \nof\n \nthe\n \nmerger\n \nin\n \nthe\n \ncommunity.\n \n \n \nInteractive\n \ngraph\n \nof\n \nthe\n \ndiscrepancies\n \nalong\n \nwith\n \nthe\n \nrevision\n \nhistory\n \nof\n \nthe\n \nFederal\n \nRailroad\n \nAdministration\n \nCrossing\n \nInventory.\n \n \n \nInteractive\n \nheat\n \nmap\n \nshowing\n \nthe\n \nUP\n \nBlocked\n \nCrossings\n \nfrom\n \nthe\n \nFederal\n \nRailroad\n \nAdministration’s\n \nBlocked\n \nCrossing\n \nPortal\n \n \n \n \n \n\nCER TIFIC ATE\n \nOF\n \nSERVICE\n \n \n \nI\n \nhereby\n \ncertify\n \nthat\n \non\n \nthis\n \n23\n \nday\n \nof\n \nJuly,\n \n2026,\n \nthe\n \nforegoing\n \ndocument\n \nwas\n \nserved\n \non\n \nall\n \nparties\n \nof\n \nrecord\n \nto\n \nthis\n \nproceeding.\n \n \n \n/s/\n \nLindsay\n \nWilliams"
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
        "date": "07/27/2026",
        "document_type": "Environmental Incoming",
        "additional_info": "UNION PACIFIC CORPORATION AND UNION PACIFIC RAILROAD COMPANY&mdash;CONTROL&mdash;NORFOLK SOUTHERN CORPORATION AND NORFOLK SOUTHERN RAILWAY COMPANY | Comments: Updated Master Data Tables",
        "on_behalf_of": "submitter: Thomas Brugato",
        "docket_number": "FD-36873",
        "document_id": "https://dcms-external.s3.amazonaws.com/DCMS_External_PROD/1785250537407/EI-34263.pdf",
        "docket_type": "stb-environmentalComment"
    }

    result = analyze_docket_entry(doc_num, text, metadata)
# logger.info(json.dumps(result, indent=2))
