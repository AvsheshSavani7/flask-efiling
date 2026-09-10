"""
ACCC Cases Re-Analysis Script
==============================
Re-runs LLM deal matching and USA-relation checks for accc_cases records
that were inserted on/after 2026-05-22 without analysis (due to a bad API key).

What it does:
1. Queries accc_cases for records where:
   - created_at >= CUTOFF_DATE
   - deal_id is null/missing/empty
   - reanalyzed_at is absent (not yet re-processed)
2. For each record:
   - Runs LLM deal matching using the case title
     → Match found: updates deal_id in DB, sends [FRMD] email
     → No match: one-side FRPMD check, then USA-relation check
       → Partial match: sends [FRPMD-A]/[FRPMD-T] email (no deal_id stored)
       → USA-related: sends [FRUD] email
   - Sets reanalyzed_at to prevent re-processing on future runs

Usage:
    python accc_reanalyze.py
    python accc_reanalyze.py --cutoff 2026-05-22
    python accc_reanalyze.py --dry-run        # logs only, no DB writes or emails
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(".env")

# Reuse helpers from the main scraper — avoids duplicating logic
from accc_cases_register import (
    get_accc_cases_collection,
    match_case_to_deal,
    match_case_to_deal_partial,
    prepare_case_payload_for_llm,
    send_new_case_email,
    send_unmatched_usa_related_email,
    utc_now_iso,
)
from scraper_error_utils import collect_error, send_error_summary
from llm_verification_service import verify_usa_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import init_mongodb_connection

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_NAME = "australia_cases_reanalyze"
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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def fetch_unanalyzed_records(collection, cutoff_str: str) -> List[Dict[str, Any]]:
    """
    Fetch accc_cases records that:
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
    case_number: str,
    deal_id: Optional[str],
    dry_run: bool,
) -> bool:
    """
    Update an accc_cases record after re-analysis:
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
    stats = {"total": 0, "matched": 0, "matched_partial": 0, "usa_related": 0, "errors": 0}

    logger.info("=" * 60)
    logger.info(f"[STEP 1] ACCC Cases Re-Analysis")
    logger.info(f"         Cutoff : {cutoff_str}")
    logger.info(f"         Dry-run: {dry_run}")
    logger.info("=" * 60)

    # MongoDB init
    try:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(
                error_items,
                f"MongoDB init failed: {message}",
                step="init_mongodb_connection",
            )
            return {"success": False, "error": message}

        collection = get_accc_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "accc_cases collection not available",
                step="get_accc_cases_collection",
            )
            return {"success": False, "error": "accc_cases collection unavailable"}

        records = fetch_unanalyzed_records(collection, cutoff_str)
        if not records:
            logger.info("No unanalyzed records found. Nothing to do.")
            return {"success": True, "total": 0, "matched": 0, "usa_related": 0, "errors": 0}

        stats = {
            "total": len(records),
            "matched": 0,
            "matched_partial": 0,
            "usa_related": 0,
            "errors": 0,
        }

        logger.info(f"[STEP 2] Processing {len(records)} records...")

        for idx, record in enumerate(records, 1):
            case_number = (record.get("case_number") or "").strip()
            title = record.get("title", "") or ""

            logger.info(f"[{idx}/{len(records)}] {case_number} — {title[:60]}...")

            if not case_number:
                logger.warning("  Missing case_number, skipping")
                continue

            if not title:
                logger.warning(f"  {case_number}: No title available, skipping LLM match")
                update_record_after_analysis(collection, case_number, None, dry_run)
                continue

            matched_deal_id: Optional[str] = None
            try:
                matched_deal_id = match_case_to_deal(title)
            except Exception as exc:
                logger.exception(f"  {case_number}: LLM match error: {exc}")
                collect_error(
                    error_items,
                    str(exc),
                    step="match_case_to_deal",
                    case_number=case_number,
                )
                stats["errors"] += 1
                continue

            if matched_deal_id:
                logger.info(f"  {case_number}: Deal matched → deal_id={matched_deal_id}")

                update_record_after_analysis(collection, case_number, matched_deal_id, dry_run)

                record_with_deal = {**record, "deal_id": matched_deal_id}
                if dry_run:
                    logger.info(f"  [DRY-RUN] Would send [FRMD] email for {case_number}: {title[:60]}")
                else:
                    send_new_case_email(record_with_deal, matched_deal_id)
                    logger.info(f"  Sent [FRMD] email for {case_number}")

                stats["matched"] += 1
                continue

            partial_match = None
            try:
                partial_match = match_case_to_deal_partial(title)
            except Exception as exc:
                logger.exception(f"  {case_number}: Partial match error: {exc}")
                collect_error(
                    error_items,
                    str(exc),
                    step="match_case_to_deal_partial",
                    case_number=case_number,
                )
                stats["errors"] += 1

            if partial_match:
                _partial_deal_id, partial_side = partial_match
                stats["matched_partial"] += 1
                logger.info(
                    f"  {case_number}: Partial match → deal_id={_partial_deal_id} "
                    f"side={partial_side} (not storing deal_id)"
                )
                update_record_after_analysis(collection, case_number, None, dry_run)
                if dry_run:
                    logger.info(
                        f"  [DRY-RUN] Would send FRPMD email for {case_number}")
                else:
                    send_unmatched_usa_related_email(
                        record, partial_side=partial_side)
                    logger.info(f"  Sent FRPMD email for {case_number}")
                continue

            logger.info(f"  {case_number}: No deal match — checking USA relation...")
            is_usa = False
            try:
                case_details_str = prepare_case_payload_for_llm(record)
                is_usa = bool(
                    verify_usa_relation(
                        company_details=case_details_str,
                        case_type="ACCC",
                    )
                )
            except Exception as exc:
                logger.exception(f"  {case_number}: USA check error: {exc}")
                collect_error(
                    error_items,
                    str(exc),
                    step="verify_usa_relation",
                    case_number=case_number,
                )
                stats["errors"] += 1

            update_record_after_analysis(collection, case_number, None, dry_run)

            if is_usa:
                logger.info(f"  {case_number}: USA-related → sending [FRUD] email")
                if dry_run:
                    logger.info(f"  [DRY-RUN] Would send [FRUD] email for {case_number}")
                else:
                    send_unmatched_usa_related_email(record)
                    logger.info(f"  Sent [FRUD] email for {case_number}")
                stats["usa_related"] += 1
            else:
                logger.info(f"  {case_number}: Not USA-related → silent (no email)")

            time.sleep(1)

    except Exception as exc:
        logger.exception(f"Unhandled error in reanalyze(): {exc}")
        collect_error(
            error_items,
            f"Unhandled error in reanalyze(): {exc}",
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Cutoff date            : {cutoff_str}")
        logger.info(f"  Total records found    : {stats.get('total', 0)}")
        logger.info(f"  Deal matched + emailed : {stats.get('matched', 0)}")
        logger.info(f"  Partial one-side + emailed : {stats.get('matched_partial', 0)}")
        logger.info(f"  USA-related + emailed  : {stats.get('usa_related', 0)}")
        logger.info(f"  Errors                 : {len(error_items)}")
        logger.info(f"  Dry-run                : {dry_run}")
        logger.info(f"  Total time             : {elapsed}s")
        logger.info("=" * 60)

    return {
        "success": True,
        "cutoff_date": cutoff_str,
        "total": stats.get("total", 0),
        "matched": stats.get("matched", 0),
        "matched_partial": stats.get("matched_partial", 0),
        "usa_related": stats.get("usa_related", 0),
        "errors": len(error_items),
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Re-run LLM analysis on accc_cases records missed due to bad API key."
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
