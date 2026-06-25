"""
Bundeskartellamt Re-Analysis Script
=====================================
Re-runs LLM deal matching and USA-relation checks for german_cases records
that were inserted on/after 2026-05-22 without analysis (due to a bad API key).

What it does:
1. Queries german_cases for records where:
   - created_at >= CUTOFF_DATE
   - deal_id is null/missing
   - reanalyzed_at is missing (not yet re-processed)
2. For each record:
   - Runs LLM deal matching against open/unknown deals
     → Match found: updates deal_id in DB, sends [FRMD] email
     → No match: runs USA-relation check
       → USA-related: sends [FRUD] email
   - Sets reanalyzed_at on the record (prevents double-processing)
3. Existing translations (pursue_en etc.) are reused — no re-translation.

Usage:
    python bundeskartellamt_reanalyze.py
    python bundeskartellamt_reanalyze.py --cutoff 2026-05-22
    python bundeskartellamt_reanalyze.py --dry-run        # logs only, no DB writes or emails
"""

import argparse
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(".env")

# Reuse helpers from the main scraper — avoids duplicating logic
from bundeskartellamt_initial_proxy import (
    SOURCE_INITIAL_FILING,
    fetch_deals,
    generate_matched_email,
    generate_usa_related_email,
    get_german_cases_collection,
    match_deal_with_llm,
    parse_llm_match,
    send_email_via_webhook,
    utc_now_iso,
)
from error_email_service import send_error_email
from llm_verification_service import verify_country_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import init_mongodb_connection

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_NAME = "germany_cases_reanalyze"
PERSISTENT_LOG_DIR = "/var/data/logs"
IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_CUTOFF = "2026-05-22"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger(SCRIPT_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


if not logger.handlers:
    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


def _log_critical_error_and_email(msg: str, context: Optional[dict] = None):
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context or {},
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def fetch_unanalyzed_records(collection, cutoff_str: str) -> List[Dict[str, Any]]:
    """
    Fetch german_cases records that:
      - were created on/after cutoff_str (ISO string comparison)
      - have no deal_id (null, missing, or empty string)
      - have not been re-analyzed yet (reanalyzed_at field absent)
    """
    query = {
        "created_at": {"$gte": cutoff_str},
        "$or": [
            {"deal_id": None},
            {"deal_id": {"$exists": False}},
            {"deal_id": ""},
        ],
        "reanalyzed_at": {"$exists": False},
    }
    docs = list(collection.find(query))
    logger.info(f"Found {len(docs)} unanalyzed records with created_at >= {cutoff_str}")
    return docs


def update_record_after_analysis(
    collection,
    file_number: str,
    deal_id: Optional[str],
    dry_run: bool,
) -> bool:
    """
    Update a german_cases record after re-analysis:
      - Sets deal_id (if matched)
      - Sets reanalyzed_at to prevent re-processing on future runs
      - Sets updated_at
    """
    if dry_run:
        logger.info(f"  [DRY-RUN] Would update {file_number}: deal_id={deal_id}")
        return True

    now = utc_now_iso()
    update_fields: Dict[str, Any] = {
        "reanalyzed_at": now,
        "updated_at": now,
    }
    if deal_id:
        update_fields["deal_id"] = deal_id

    try:
        result = collection.update_one(
            {"file_number": file_number},
            {"$set": update_fields},
        )
        return result.matched_count > 0
    except Exception as exc:
        logger.warning(f"  Error updating {file_number}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main re-analysis pipeline
# ---------------------------------------------------------------------------

def reanalyze(cutoff_str: str, dry_run: bool) -> Dict[str, Any]:
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []

    logger.info("=" * 60)
    logger.info(f"[STEP 1] Bundeskartellamt Re-Analysis")
    logger.info(f"         Cutoff : {cutoff_str}")
    logger.info(f"         Dry-run: {dry_run}")
    logger.info("=" * 60)

    # MongoDB init
    success, message = init_mongodb_connection(".env")
    if not success:
        _log_critical_error_and_email(
            f"MongoDB init failed: {message}", {"step": "init_mongodb_connection"}
        )
        return {"success": False, "error": message}

    gc_collection = get_german_cases_collection()
    if gc_collection is None:
        _log_critical_error_and_email(
            "german_cases collection not available", {"step": "get_german_cases_collection"}
        )
        return {"success": False, "error": "german_cases collection unavailable"}

    # Fetch active deals for LLM matching
    deals = fetch_deals()
    deal_by_id = {str(d.get("deal_id", "")): d for d in deals if d.get("deal_id")}
    logger.info(f"[STEP 2] Loaded {len(deals)} open/unknown deals")

    # Fetch unanalyzed records from DB
    records = fetch_unanalyzed_records(gc_collection, cutoff_str)
    if not records:
        logger.info("No unanalyzed records found. Nothing to do.")
        return {"success": True, "total": 0, "matched": 0, "usa_related": 0, "errors": 0}

    stats = {"total": len(records), "matched": 0, "usa_related": 0, "errors": 0}

    logger.info(f"[STEP 3] Processing {len(records)} records...")

    for idx, record in enumerate(records, 1):
        fn = (record.get("file_number") or "").strip()
        pursue_en = record.get("pursue_en", "") or ""

        logger.info(
            f"[{idx}/{len(records)}] {fn} — {pursue_en[:60]}..."
        )

        if not fn:
            logger.warning("  Missing file_number, skipping")
            continue

        if not pursue_en or pursue_en == "[Translation failed]":
            logger.warning(f"  {fn}: No pursue_en available, skipping LLM match")
            # Still mark as reanalyzed so it's not retried endlessly
            update_record_after_analysis(gc_collection, fn, None, dry_run)
            continue

        # --- LLM deal matching ---
        deal_match = None
        matched_deal_id: Optional[str] = None

        try:
            match_result = match_deal_with_llm(pursue_en, deals)
            deal_match, _, _ = parse_llm_match(match_result or "", deal_by_id)
        except Exception as exc:
            logger.exception(f"  {fn}: LLM match error: {exc}")
            error_items.append({"file_number": fn, "error": str(exc), "step": "match_deal_with_llm"})
            stats["errors"] += 1
            continue

        if deal_match:
            matched_deal_id = deal_match.get("deal_id")
            logger.info(f"  {fn}: Deal matched → deal_id={matched_deal_id}")

            # Update DB with deal_id + reanalyzed_at
            update_record_after_analysis(gc_collection, fn, matched_deal_id, dry_run)

            # Send [FRMD] email (identical to original scraper email)
            subject, html = generate_matched_email(record, deal_match)
            if dry_run:
                logger.info(f"  [DRY-RUN] Would send [FRMD] email: {subject}")
            else:
                send_email_via_webhook(subject, html, fn, deal_id=matched_deal_id)
                logger.info(f"  Sent [FRMD] email: {subject}")

            stats["matched"] += 1
            continue

        # --- No deal match: check USA relation ---
        logger.info(f"  {fn}: No deal match — checking USA relation...")
        is_usa = False
        try:
            company_details = {
                "today_date": datetime.now().strftime("%Y-%m-%d"),
                "record": {k: v for k, v in record.items() if k != "_id"},
            }
            is_usa = verify_country_relation(
                company_details=company_details, country="USA", case_type="GERMANY"
            )
        except Exception as exc:
            logger.exception(f"  {fn}: USA check error: {exc}")
            error_items.append({"file_number": fn, "error": str(exc), "step": "verify_country_relation"})
            stats["errors"] += 1

        # Mark as reanalyzed regardless of result
        update_record_after_analysis(gc_collection, fn, None, dry_run)

        if is_usa:
            logger.info(f"  {fn}: USA-related → sending [FRUD] email")
            subject, html = generate_usa_related_email(record)
            if dry_run:
                logger.info(f"  [DRY-RUN] Would send [FRUD] email: {subject}")
            else:
                send_email_via_webhook(subject, html, fn)
                logger.info(f"  Sent [FRUD] email: {subject}")
            stats["usa_related"] += 1
        else:
            logger.info(f"  {fn}: Not USA-related → silent (no email)")

        # Small delay to avoid overwhelming the LLM API
        time.sleep(1)

    # Report any errors via error email
    if error_items:
        send_error_email(
            script_name=SCRIPT_NAME,
            error_message=f"{len(error_items)} errors during re-analysis",
            context={"error_count": len(error_items), "errors": error_items[:20]},
            traceback_str=None,
        )

    elapsed = round(time.time() - run_start, 1)
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Cutoff date            : {cutoff_str}")
    logger.info(f"  Total records found    : {stats['total']}")
    logger.info(f"  Deal matched + emailed : {stats['matched']}")
    logger.info(f"  USA-related + emailed  : {stats['usa_related']}")
    logger.info(f"  Errors                 : {stats['errors']}")
    logger.info(f"  Dry-run                : {dry_run}")
    logger.info(f"  Total time             : {elapsed}s")
    logger.info("=" * 60)

    return {
        "success": True,
        "cutoff_date": cutoff_str,
        "total": stats["total"],
        "matched": stats["matched"],
        "usa_related": stats["usa_related"],
        "errors": stats["errors"],
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Re-run LLM analysis on german_cases records missed due to bad API key."
    )
    parser.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF,
        help=f"Earliest created_at date to reprocess (YYYY-MM-DD, default: {DEFAULT_CUTOFF})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without writing to DB or sending emails",
    )
    args = parser.parse_args()

    try:
        datetime.strptime(args.cutoff, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid cutoff date '{args.cutoff}'. Use YYYY-MM-DD format.", file=sys.stderr)
        sys.exit(1)

    reanalyze(cutoff_str=args.cutoff, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log_critical_error_and_email(f"Unhandled error in main: {e}", {"step": "main"})
        raise
