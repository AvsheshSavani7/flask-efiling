"""
CADE Brazil — retry LLM match + email for specific process numbers.

Looks up each process in brazil_cases. If deal_id is already set, skip.
If deal_id is missing, run LLM match, regex fallback, then one-side FRPMD,
then USA/FRUD, and send email.

Usage:
    python cade_reanalyze.py 08700.001234/2026-11
    python cade_reanalyze.py 08700.001234/2026-11 08700.005678/2026-12
    python cade_reanalyze.py --dry-run 08700.001234/2026-11
"""

from scraper_error_utils import collect_error, send_error_summary
from mongodb_connection import get_deal_by_id, init_mongodb_connection
from log_utils import cleanup_old_logs, refresh_log_file
from llm_verification_service import verify_usa_relation
from deal_match_regex import regex_match_cade_deal
from deal_match_llm import fetch_open_deals
from cade_cases_register import (
    get_brazil_cases_collection,
    match_case_to_deal,
    match_case_to_deal_partial,
    send_usa_related_email,
    translate_to_english,
    utc_now_iso,
)
from cade_document_summariser import (
    apply_summariser_pending_flags,
    summarise_cade_cases_parallel,
)
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_NAME = "brazil_cases_reanalyze"
PERSISTENT_LOG_DIR = "/var/data/logs"
IST = timezone(timedelta(hours=5, minutes=30))

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
# Helpers
# ---------------------------------------------------------------------------

def _has_deal_id(record: Dict[str, Any]) -> bool:
    deal_id = record.get("deal_id")
    return bool(deal_id) and str(deal_id).strip() not in ("", "None", "null")


def _interessados_en(record: Dict[str, Any]) -> str:
    translated = (record.get("interessados_en") or "").strip()
    if translated:
        return translated
    original = (record.get("interessados") or "").strip()
    if not original:
        return ""
    translated = translate_to_english(original)
    record["interessados_en"] = translated
    return translated


def _verify_usa_relation(record: Dict[str, Any], interessados: str, translated: str) -> bool:
    company_details = (
        f"Process: {record.get('process', '')}\n"
        f"Type: {record.get('type', '')}\n"
        f"Registration Date: {record.get('registration_date', '')}\n"
        f"Interested Parties (PT): {interessados}\n"
        f"Interested Parties (EN): {translated}\n"
        f"Detail URL: {record.get('detail_url', '')}"
    )
    return bool(verify_usa_relation(
        company_details=company_details,
        case_type="BRAZIL",
    ))


def update_deal_id(collection, record: Dict[str, Any], deal_id: str, dry_run: bool) -> bool:
    process = record.get("process", "?")
    if dry_run:
        logger.info(f"  [DRY-RUN] Would set deal_id={deal_id} on {process}")
        return True
    try:
        result = collection.update_one(
            {"_id": record["_id"]},
            {"$set": {
                "deal_id": deal_id,
                "summariser_pending": True,
                "updated_at": utc_now_iso(),
            }},
        )
        return result.matched_count > 0
    except Exception as exc:
        logger.warning(f"  Error updating {process}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def reanalyze(process_numbers: List[str], dry_run: bool) -> Dict[str, Any]:
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats = {
        "total": len(process_numbers),
        "skipped_has_deal_id": 0,
        "not_found": 0,
        "matched_llm": 0,
        "matched_regex": 0,
        "matched_partial": 0,
        "usa_related": 0,
        "errors": 0,
    }

    logger.info("=" * 60)
    logger.info("[STEP 1] CADE Brazil — retry LLM match by process number")
    logger.info(f"         Process: {process_numbers}")
    logger.info(f"         Dry-run: {dry_run}")
    logger.info("=" * 60)

    try:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(
                error_items,
                f"MongoDB init failed: {message}",
                step="init_mongodb_connection",
            )
            return {"success": False, "error": message}

        collection = get_brazil_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "brazil_cases collection not available",
                step="get_brazil_cases_collection",
            )
            return {"success": False, "error": "brazil_cases collection unavailable"}

        open_deals = fetch_open_deals()
        logger.info(
            f"[STEP 2] Processing {len(process_numbers)} process(es) ({len(open_deals)} open deals)...")
        frmd_jobs: List[Dict[str, Any]] = []

        for idx, process in enumerate(process_numbers, 1):
            process = process.strip()
            logger.info(f"[{idx}/{len(process_numbers)}] {process}")

            record = collection.find_one({"process": process})
            if record is None:
                logger.warning(f"  Not found in brazil_cases — skipping")
                stats["not_found"] += 1
                continue

            if _has_deal_id(record):
                logger.info(
                    f"  deal_id already set ({record.get('deal_id')}) — skipping")
                stats["skipped_has_deal_id"] += 1
                continue

            interessados = (record.get("interessados") or "").strip()
            if not interessados:
                logger.warning(f"  No interessados text — skipping LLM match")
                continue

            translated = _interessados_en(record)
            logger.info(f"  Interessados (EN): {translated[:150]}...")

            matched_deal_id: Optional[str] = None
            matched_by_regex = False
            try:
                matched_deal_id = match_case_to_deal(
                    interessados, translated, deals=open_deals,
                )
            except Exception as exc:
                logger.exception(f"  LLM match error: {exc}")
                collect_error(
                    error_items,
                    str(exc),
                    step="match_case_to_deal",
                    context={"process": process},
                )
                stats["errors"] += 1
                continue

            if matched_deal_id:
                stats["matched_llm"] += 1
            else:
                matched_deal_id = regex_match_cade_deal(translated, open_deals)
                if matched_deal_id:
                    matched_by_regex = True
                    stats["matched_regex"] += 1
                    logger.info(
                        f"  Regex fallback matched deal_id={matched_deal_id}")
                else:
                    logger.info("  No match (LLM + regex both returned None)")

            if matched_deal_id:
                logger.info(f"  Deal matched → deal_id={matched_deal_id}")
                update_deal_id(collection, record, matched_deal_id, dry_run)
                record_with_deal = {**record, "deal_id": matched_deal_id}
                if dry_run:
                    logger.info(
                        f"  [DRY-RUN] Would summarise documents + send FRMD emails for {process}")
                else:
                    deal_match = get_deal_by_id(matched_deal_id)
                    frmd_jobs.append({
                        "documents": list(record_with_deal.get("table_records") or []),
                        "case_doc": record_with_deal,
                        "deal": deal_match,
                        "event_type": "new",
                        "matched_by_regex": matched_by_regex,
                        "test_mode": False,
                        "headless": True,
                    })
                continue

            partial_match = None
            try:
                partial_match = match_case_to_deal_partial(
                    interessados, translated, deals=open_deals,
                )
            except Exception as exc:
                logger.exception(f"  Partial match error: {exc}")
                collect_error(
                    error_items,
                    str(exc),
                    step="match_case_to_deal_partial",
                    context={"process": process},
                )
                stats["errors"] += 1

            if partial_match:
                _partial_deal_id, partial_side = partial_match
                stats["matched_partial"] += 1
                logger.info(
                    f"  Partial match → deal_id={_partial_deal_id} side={partial_side} "
                    "(not storing deal_id)"
                )
                if dry_run:
                    logger.info(
                        f"  [DRY-RUN] Would send FRPMD email for {process}")
                else:
                    if not send_usa_related_email(record, partial_side=partial_side):
                        collect_error(
                            error_items,
                            "Failed to send FRPMD email",
                            step="send_email",
                            context={"process": process},
                        )
                        stats["errors"] += 1
                    else:
                        logger.info(f"  Sent FRPMD email for {process}")
                continue

            logger.info("  No deal match — checking USA relation...")
            is_usa = False
            try:
                is_usa = _verify_usa_relation(record, interessados, translated)
            except Exception as exc:
                logger.exception(f"  USA check error: {exc}")
                collect_error(
                    error_items,
                    str(exc),
                    step="verify_usa_relation",
                    context={"process": process},
                )
                stats["errors"] += 1

            if is_usa:
                logger.info("  USA-related → sending unmatched email")
                if dry_run:
                    logger.info(
                        f"  [DRY-RUN] Would send USA-related email for {process}")
                else:
                    if not send_usa_related_email(record):
                        collect_error(
                            error_items,
                            "Failed to send USA-related email",
                            step="send_email",
                            context={"process": process},
                        )
                        stats["errors"] += 1
                    else:
                        logger.info(f"  Sent USA-related email for {process}")
                stats["usa_related"] += 1
            else:
                logger.info("  Not USA-related → no email")

            time.sleep(1)

        if frmd_jobs:
            logger.info(
                f"[STEP 3] Summarising {len(frmd_jobs)} FRMD case(s) in parallel"
            )
            summariser_results = summarise_cade_cases_parallel(frmd_jobs)
            apply_summariser_pending_flags(collection, summariser_results)
            for res in summariser_results:
                if res.get("success"):
                    continue
                collect_error(
                    error_items,
                    res.get("error") or "CADE document summariser failed",
                    step=res.get("step") or "brazil_summariser",
                    context={
                        "process": res.get("process"),
                        "brazil_case_id": res.get("brazil_case_id"),
                        "documento_processo": res.get("documento_processo"),
                    },
                )
                stats["errors"] += 1

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
        logger.info(f"  Process numbers given  : {stats.get('total', 0)}")
        logger.info(f"  Not found in DB        : {stats.get('not_found', 0)}")
        logger.info(
            f"  Skipped (has deal_id)  : {stats.get('skipped_has_deal_id', 0)}")
        logger.info(
            f"  LLM deal matches       : {stats.get('matched_llm', 0)}")
        logger.info(
            f"  Regex fallback matches : {stats.get('matched_regex', 0)}")
        logger.info(
            f"  Partial one-side matches : {stats.get('matched_partial', 0)}")
        logger.info(
            f"  USA-related + emailed  : {stats.get('usa_related', 0)}")
        logger.info(f"  Errors                 : {len(error_items)}")
        logger.info(f"  Dry-run                : {dry_run}")
        logger.info(f"  Total time             : {elapsed}s")
        logger.info("=" * 60)

    return {
        "success": True,
        "total": stats.get("total", 0),
        "not_found": stats.get("not_found", 0),
        "skipped_has_deal_id": stats.get("skipped_has_deal_id", 0),
        "matched_llm": stats.get("matched_llm", 0),
        "matched_regex": stats.get("matched_regex", 0),
        "matched_partial": stats.get("matched_partial", 0),
        "usa_related": stats.get("usa_related", 0),
        "errors": len(error_items),
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Retry LLM deal matching and send email for specific CADE "
            "process numbers already in brazil_cases. Skips records that "
            "already have a deal_id."
        )
    )
    parser.add_argument(
        "process",
        nargs="+",
        metavar="PROCESS",
        help="CADE process number(s), e.g. 08700.001234/2026-11",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without writing to DB or sending emails",
    )
    args = parser.parse_args()
    reanalyze(process_numbers=args.process, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
