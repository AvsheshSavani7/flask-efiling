"""
EC Cases Re-Analysis Script
=============================
Re-runs LLM deal matching and USA-relation checks for ec_cases records
that were inserted on/after 2026-05-22 without analysis (due to a bad API key).

What it does:
1. Queries ec_cases for records where:
   - created_at >= CUTOFF_DATE
   - deal_id is null/missing/empty
   - reanalyzed_at is absent (not yet re-processed)
2. For each record:
   - Runs LLM deal matching using the companies list stored in the record
     → Match found: looks up full deal, updates deal_id in DB, sends [FRMD] email
     → No match: runs USA-relation check against companies list
       → USA-related: sends [FRUD] email
   - Sets reanalyzed_at to prevent re-processing on future runs

Usage:
    python ec_reanalyze.py
    python ec_reanalyze.py --cutoff 2026-05-22
    python ec_reanalyze.py --dry-run        # logs only, no DB writes or emails
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

from bson import ObjectId
from dotenv import load_dotenv

load_dotenv(".env")

# Reuse helpers from the main scraper — avoids duplicating logic
from new_ec_cases_html import (
    fetch_deals,
    generate_matched_email,
    generate_usa_email,
    get_ec_cases_collection,
    match_case_to_deal,
    send_email_via_webhook,
    utc_now_iso,
)
from error_email_service import send_error_email
from llm_verification_service import verify_usa_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import get_deals_collection, init_mongodb_connection

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_NAME = "ec_cases_reanalyze"
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
    Fetch ec_cases records that:
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


def lookup_deal(deal_id: str, deal_by_id: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Look up a full deal dict by deal_id.
    First checks the in-memory cache, then falls back to a direct DB query.
    """
    deal = deal_by_id.get(deal_id)
    if deal:
        return deal

    logger.info(f"  deal_id={deal_id} not in cache, querying DB...")
    try:
        deals_coll = get_deals_collection()
        if deals_coll:
            raw = deals_coll.find_one({"_id": ObjectId(deal_id)})
            if raw:
                raw["deal_id"] = str(raw["_id"])
                logger.info(
                    f"  Found deal in DB: target={raw.get('target')} | acquirer={raw.get('acquirer')}"
                )
                return raw
    except Exception as exc:
        logger.warning(f"  Error looking up deal {deal_id}: {exc}")
    return None


def update_record_after_analysis(
    collection,
    case_number: str,
    deal_id: Optional[str],
    dry_run: bool,
) -> bool:
    """
    Update an ec_cases record after re-analysis:
      - Sets deal_id (if matched)
      - Sets reanalyzed_at to prevent re-processing on future runs
      - Sets updated_at
    """
    if dry_run:
        logger.info(f"  [DRY-RUN] Would update {case_number}: deal_id={deal_id}")
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
            {"case_number": case_number},
            {"$set": update_fields},
        )
        return result.matched_count > 0
    except Exception as exc:
        logger.warning(f"  Error updating {case_number}: {exc}")
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
    logger.info(f"[STEP 1] EC Cases Re-Analysis")
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

    collection = get_ec_cases_collection()
    if collection is None:
        _log_critical_error_and_email(
            "ec_cases collection not available", {"step": "get_ec_cases_collection"}
        )
        return {"success": False, "error": "ec_cases collection unavailable"}

    # Fetch active deals once upfront
    deals = fetch_deals(error_items)
    deal_by_id: Dict[str, Dict[str, Any]] = {
        (d.get("deal_id") or str(d.get("_id", ""))): d
        for d in deals if d.get("deal_id") or d.get("_id")
    }
    logger.info(f"[STEP 2] Loaded {len(deals)} open/unknown deals")

    # Fetch unanalyzed records from DB
    records = fetch_unanalyzed_records(collection, cutoff_str)
    if not records:
        logger.info("No unanalyzed records found. Nothing to do.")
        return {"success": True, "total": 0, "matched": 0, "usa_related": 0, "errors": 0}

    stats = {"total": len(records), "matched": 0, "usa_related": 0, "errors": 0}

    logger.info(f"[STEP 3] Processing {len(records)} records...")

    for idx, record in enumerate(records, 1):
        case_number = (record.get("case_number") or "").strip()
        case_title = record.get("case_title") or "N/A"
        companies: List[str] = record.get("companies") or []

        logger.info(f"[{idx}/{len(records)}] {case_number} — {case_title[:60]}...")

        if not case_number:
            logger.warning("  Missing case_number, skipping")
            continue

        if not companies:
            logger.warning(f"  {case_number}: No companies list in record, skipping LLM match")
            update_record_after_analysis(collection, case_number, None, dry_run)
            continue

        # --- LLM #1: deal matching ---
        match_result: Optional[str] = None
        try:
            match_result = match_case_to_deal(companies, deals) if deals else None
        except Exception as exc:
            logger.exception(f"  {case_number}: LLM match error: {exc}")
            error_items.append({"case_number": case_number, "error": str(exc), "step": "match_case_to_deal"})
            stats["errors"] += 1
            continue

        if match_result:
            matched_deal_id = match_result
            logger.info(
                f"  {case_number}: Deal matched → deal_id={matched_deal_id} | "
                f""
            )

            deal = lookup_deal(matched_deal_id, deal_by_id)
            if deal:
                # Update DB with deal_id + reanalyzed_at
                update_record_after_analysis(collection, case_number, matched_deal_id, dry_run)

                # Send [FRMD] email (identical to original scraper email)
                subject, html_email = generate_matched_email(record, deal)
                if dry_run:
                    logger.info(f"  [DRY-RUN] Would send [FRMD] email: {subject}")
                else:
                    send_email_via_webhook(subject, html_email, case_number, case_title, deal_id=matched_deal_id)
                    logger.info(f"  Sent [FRMD] email for {case_number}")

                stats["matched"] += 1
                continue
            else:
                logger.warning(
                    f"  {case_number}: LLM returned deal_id={matched_deal_id} but deal not found; "
                    f"falling through to USA check"
                )

        # --- LLM #2: USA-relation check ---
        logger.info(f"  {case_number}: No deal match — checking USA relation...")
        is_usa = False
        try:
            is_usa = verify_usa_relation(
                company_details=companies,
                case_type="EC",
            )
        except Exception as exc:
            logger.exception(f"  {case_number}: USA check error: {exc}")
            error_items.append({"case_number": case_number, "error": str(exc), "step": "verify_usa_relation"})
            stats["errors"] += 1

        # Mark as reanalyzed regardless of result
        update_record_after_analysis(collection, case_number, None, dry_run)

        if is_usa:
            logger.info(f"  {case_number}: USA-related → sending [FRUD] email")
            subject, html_email = generate_usa_email(record)
            if dry_run:
                logger.info(f"  [DRY-RUN] Would send [FRUD] email: {subject}")
            else:
                send_email_via_webhook(subject, html_email, case_number, case_title, usa_related=True)
                logger.info(f"  Sent [FRUD] email for {case_number}")
            stats["usa_related"] += 1
        else:
            logger.info(f"  {case_number}: Not USA-related → silent (no email)")

        time.sleep(1)

    # Report errors via error email
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
        description="Re-run LLM analysis on ec_cases records missed due to bad API key."
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
