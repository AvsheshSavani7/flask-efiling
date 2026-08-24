"""
BWB (Austrian Federal Competition Authority) merger register → bwb_cases collection
===================================================================================
Scrapes the current-year Zusammenschlüsse listing, inserts new cases, and fetches
each new detail page once. Stores German and English text fields.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from html import escape as escape_html
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from bwb_cases_common import (
    BwbWorkflowError,
    COLLECTION_NAME,
    current_year,
    determine_is_open,
    ensure_bwb_cases_indexes,
    fetch_listing_page_html,
    fetch_page_html,
    get_bwb_cases_collection,
    listing_url,
    parse_detail_page,
    parse_listing_table,
    playwright_page,
    translate_to_english_required,
    utc_now_iso,
)
from deal_match_llm import fetch_open_deals, llm_match_deal_id
from deal_match_regex import apply_regex_match_subject, regex_match_bwb_deal
from email_subject_builder import build_subject
from llm_verification_service import verify_usa_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import (
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from n8n_email_service import post_email_payload
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

# Local backfill only — not written during production register runs.
BACKFILL_BACKUP_JSON = "bwb_cases_backfill_backup.json"
ENV_PATH = ".env"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "bwb_cases_register"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))
DETAIL_FETCH_DELAY_MS = int(os.getenv("BWB_DETAIL_DELAY_MS", "500"))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


logger = logging.getLogger(SCRIPT_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

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


def case_exists(collection, file_number: str) -> bool:
    try:
        return (
            collection.count_documents(
                {"file_number": file_number}, limit=1) > 0
        )
    except Exception as e:
        logger.exception("Error checking existing case: %s", e)
        return False


def match_case_to_deal(
    parties_en: str,
    file_number: str,
    status_en: str,
    deals: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    return llm_match_deal_id(
        regulator_name="Austrian Federal Competition Authority (BWB)",
        case_sections={
            "FILE NUMBER": file_number,
            "PARTIES": parties_en,
            "STATUS": status_en,
        },
        source_label="the BWB merger parties and status",
        deals=deals,
    )


def generate_matched_case_email_html(
    case_info: Dict[str, Any], deal: Dict[str, Any]
) -> str:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = str(deal.get("_id")) if deal.get("_id") else "N/A"
    detail_url = case_info.get("detail_url") or listing_url()

    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>BWB - New Case</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;">
    <span style="font-weight:700;">Acquirer:</span> {escape_html(str(acquirer))}
    <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Target:</span> {escape_html(str(target))}
    <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Deal ID:</span> {escape_html(deal_id)}
  </div>
  <div style="margin-top:10px;">
    <a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Case →</a>
  </div>
</div>
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">BWB Austria - New Merger Filing</div>
  <div style="display:grid;grid-template-columns:200px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">File Number:</div><div>{escape_html(case_info.get("file_number", "N/A"))}</div>
    <div style="font-weight:700;">Parties:</div><div>{escape_html(case_info.get("parties_en") or case_info.get("parties", "N/A"))}</div>
    <div style="font-weight:700;">Merger Date:</div><div>{escape_html(case_info.get("merger_date", "N/A"))}</div>
    <div style="font-weight:700;">Status:</div><div>{escape_html(case_info.get("status_en") or case_info.get("status", "N/A"))}</div>
  </div>
</div>
</div>
</body>
</html>"""


def generate_usa_related_email_html(case_info: Dict[str, Any]) -> str:
    detail_url = case_info.get("detail_url") or listing_url()
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>USA-Related BWB Case</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">USA-Related BWB Case</div>
  <div style="font-size:14px;color:#1e3a8a;">This merger filing appears to involve USA-related parties or markets.</div>
  <div style="margin-top:10px;">
    <a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Case →</a>
  </div>
</div>
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">Case Details</div>
  <div style="display:grid;grid-template-columns:200px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">File Number:</div><div>{escape_html(case_info.get("file_number", "N/A"))}</div>
    <div style="font-weight:700;">Parties:</div><div>{escape_html(case_info.get("parties_en") or case_info.get("parties", "N/A"))}</div>
    <div style="font-weight:700;">Merger Date:</div><div>{escape_html(case_info.get("merger_date", "N/A"))}</div>
    <div style="font-weight:700;">Status:</div><div>{escape_html(case_info.get("status_en") or case_info.get("status", "N/A"))}</div>
  </div>
</div>
</div>
</body>
</html>"""


def send_email_via_webhook(
    subject: str,
    html_content: str,
    case_info: Dict[str, Any],
    deal_id: Optional[str] = None,
) -> bool:
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "file_number": case_info.get("file_number", "N/A"),
            "parties": case_info.get("parties_en") or case_info.get("parties", "N/A"),
            "merger_date": case_info.get("merger_date", "N/A"),
            "status": case_info.get("status_en") or case_info.get("status", "N/A"),
            "detail_url": case_info.get("detail_url", ""),
            "deal_id": deal_id,
            "is_new_case": True,
            "source": "bwb_austria",
        }
        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.warning("Error sending email: %s", e)
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    try:
        result = collection.insert_one(case_info)
        return str(result.inserted_id)
    except Exception as e:
        logger.error("Error inserting case: %s", e)
        return None


def run_bwb_cases_register(
    headless: Optional[bool] = None,
    bootstrap: bool = False,
) -> None:
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = datetime.now()
    error_items: List[Dict[str, Any]] = []
    new_cases: List[Dict[str, Any]] = []
    llm_match_count = 0
    regex_match_count = 0
    inserted_count = 0
    skipped_existing = 0
    parsed_count = 0
    year = current_year()
    url = listing_url(year)
    mode_label = "Bootstrap (DB only)" if bootstrap else "New case monitor"

    logger.info("=" * 60)
    logger.info("Starting BWB Cases Register — %s (year=%s)", mode_label, year)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("=" * 60)

    try:
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            collect_error(
                error_items,
                f"MongoDB connection failed: {message}",
                step="mongodb_connect",
            )
            return
        logger.info("MongoDB: %s", message)

        if not is_connected():
            collect_error(
                error_items,
                "MongoDB not connected. Exiting.",
                step="mongodb_connect",
            )
            return

        collection = get_bwb_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                f"Could not access '{COLLECTION_NAME}' collection. Exiting.",
                step="get_collection",
            )
            return
        ensure_bwb_cases_indexes(collection)

        open_deals = None if bootstrap else fetch_open_deals()

        with playwright_page(headless=headless) as page:
            listing_html = fetch_listing_page_html(page, url)
            all_rows = parse_listing_table(listing_html, year=year)
            parsed_count = len(all_rows)
            if not all_rows:
                logger.warning("No listing rows parsed. Exiting.")
                return

            for idx, row in enumerate(all_rows, 1):
                try:
                    file_number = (row.get("file_number") or "").strip()
                    detail_url = (row.get("detail_url") or "").strip()
                    logger.info(
                        "[%d/%d] %s | %s",
                        idx,
                        len(all_rows),
                        file_number,
                        (row.get("parties") or "")[:60],
                    )

                    if case_exists(collection, file_number):
                        skipped_existing += 1
                        logger.info("Already in %s; skipping", COLLECTION_NAME)
                        continue

                    detail_html = fetch_page_html(
                        page,
                        detail_url,
                        wait_ms=DETAIL_FETCH_DELAY_MS,
                    )
                    detail = parse_detail_page(detail_html)
                    if detail.get("file_number") and detail["file_number"] != file_number:
                        logger.warning(
                            "Detail file_number mismatch: listing=%s detail=%s",
                            file_number,
                            detail.get("file_number"),
                        )

                    parties = row.get("parties", "")
                    status = row.get("status", "")
                    parties_en = translate_to_english_required(
                        parties, field="parties", file_number=file_number
                    )
                    status_en = translate_to_english_required(
                        status, field="status", file_number=file_number
                    )
                    detail_content = detail.get("detail_content", "")
                    detail_content_en = translate_to_english_required(
                        detail_content,
                        field="detail_content",
                        file_number=file_number,
                    )

                    now_iso = utc_now_iso()
                    case_info: Dict[str, Any] = {
                        "file_number": file_number,
                        "parties": parties,
                        "parties_en": parties_en,
                        "merger_date": row.get("merger_date", ""),
                        "status": status,
                        "status_en": status_en,
                        "detail_url": detail_url,
                        "source_year": year,
                        "title": detail.get("title", parties),
                        "title_en": translate_to_english_required(
                            detail.get("title", parties),
                            field="title",
                            file_number=file_number,
                        ),
                        "announcement_date": detail.get("announcement_date", ""),
                        "detail_content": detail_content,
                        "detail_content_en": detail_content_en,
                        "is_open": determine_is_open(status),
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }

                    if bootstrap:
                        inserted_id = insert_case(collection, case_info)
                        if inserted_id:
                            inserted_count += 1
                            backup_case = dict(case_info)
                            new_cases.append(backup_case)
                            logger.info(
                                "Bootstrap inserted (id=%s)", inserted_id
                            )
                        else:
                            collect_error(
                                error_items,
                                "Failed to insert case",
                                step="insert_case",
                                context={"file_number": file_number},
                            )
                        continue

                    try:
                        matched_deal_id = match_case_to_deal(
                            parties_en,
                            file_number,
                            status_en,
                            deals=open_deals,
                        )
                    except Exception as e:
                        logger.exception("Deal matching error: %s", e)
                        collect_error(
                            error_items,
                            str(e),
                            step="match_case_to_deal",
                            context={"file_number": file_number},
                        )
                        matched_deal_id = None

                    matched_by_regex = False
                    if matched_deal_id:
                        llm_match_count += 1
                    else:
                        matched_deal_id = regex_match_bwb_deal(
                            parties_en, open_deals or []
                        )
                        if matched_deal_id:
                            matched_by_regex = True
                            regex_match_count += 1
                            logger.info(
                                "Regex fallback matched deal_id=%s", matched_deal_id
                            )

                    if matched_deal_id:
                        case_info["deal_id"] = matched_deal_id
                        deal = get_deal_by_id(matched_deal_id)
                        if deal:
                            subject = build_subject("bwb", "new", deal)
                            subject = apply_regex_match_subject(
                                subject, matched_by_regex
                            )
                            html_email = generate_matched_case_email_html(
                                case_info, deal
                            )
                            if not send_email_via_webhook(
                                subject,
                                html_email,
                                case_info,
                                deal_id=matched_deal_id,
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to send matched-case email",
                                    step="send_email",
                                    context={
                                        "file_number": file_number,
                                        "deal_id": matched_deal_id,
                                    },
                                )
                        else:
                            collect_error(
                                error_items,
                                "Matched deal_id but deal document not found",
                                step="fetch_matched_deal",
                                context={
                                    "file_number": file_number,
                                    "deal_id": matched_deal_id,
                                },
                            )
                    else:
                        try:
                            details_for_llm = (
                                f"File Number: {file_number}\n"
                                f"Parties: {parties_en}\n"
                                f"Status: {status_en}\n"
                                f"Merger Date: {row.get('merger_date', '')}\n"
                                f"Detail: {detail_content_en[:2000]}"
                            )
                            is_usa = bool(
                                verify_usa_relation(
                                    company_details=details_for_llm,
                                    case_type="BWB Austria",
                                )
                            )
                        except Exception as e:
                            logger.exception("USA verification error: %s", e)
                            collect_error(
                                error_items,
                                str(e),
                                step="verify_usa_relation",
                                context={"file_number": file_number},
                            )
                            is_usa = False

                        if is_usa:
                            subject = build_subject("bwb", "new")
                            html_email = generate_usa_related_email_html(
                                case_info)
                            if not send_email_via_webhook(
                                subject, html_email, case_info
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to send USA-related email",
                                    step="send_email",
                                    context={"file_number": file_number},
                                )
                        else:
                            logger.info(
                                "Not matched and not USA-related; silent insert")

                    inserted_id = insert_case(collection, case_info)
                    if inserted_id:
                        inserted_count += 1
                        backup_case = dict(case_info)
                        backup_case.pop("_id", None)
                        new_cases.append(backup_case)
                        logger.info(
                            "Inserted into %s (id=%s)", COLLECTION_NAME, inserted_id
                        )
                    else:
                        collect_error(
                            error_items,
                            "Failed to insert case",
                            step="insert_case",
                            context={"file_number": file_number},
                        )
                except BwbWorkflowError as e:
                    collect_error(
                        error_items,
                        str(e),
                        step=e.step,
                        context=e.context,
                    )
                    if e.step == "translate":
                        logger.error(
                            "Translation failed for this case; skipping: %s",
                            e,
                        )
                        continue
                    logger.error("Workflow stopped: %s", e)
                    raise
                except Exception as e:
                    logger.exception("Error processing row #%d: %s", idx, e)
                    collect_error(
                        error_items,
                        str(e),
                        step="process_row",
                        context={"file_number": (
                            row.get("file_number") or "")},
                    )

        if bootstrap and new_cases:
            try:
                with open(BACKFILL_BACKUP_JSON, "w", encoding="utf-8") as f:
                    json.dump(new_cases, f, indent=2, ensure_ascii=False)
                logger.info(
                    "Saved %d backfill records to local JSON: %s",
                    len(new_cases),
                    BACKFILL_BACKUP_JSON,
                )
            except Exception as e:
                logger.warning("Error writing backfill JSON: %s", e)

    except BwbWorkflowError:
        logger.error("BWB register workflow aborted due to fatal error")
    except Exception as e:
        logger.exception("Unhandled error in run_bwb_cases_register: %s", e)
        collect_error(error_items, f"Unhandled error: {e}", step="run_main")
    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round((datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  Mode                         : %s", mode_label)
        logger.info("  Year                         : %s", year)
        logger.info("  Rows parsed                  : %s", parsed_count)
        logger.info("  Skipped (already in DB)      : %s", skipped_existing)
        logger.info("  Inserted                     : %s", inserted_count)
        if not bootstrap:
            logger.info("  LLM deal matches             : %s", llm_match_count)
            logger.info("  Regex fallback matches       : %s",
                        regex_match_count)
        logger.info("  Errors encountered           : %s", len(error_items))
        logger.info("  Total time                   : %ss", elapsed)
        logger.info("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="BWB merger cases register scraper")
    parser.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="Run browser headless (default: BWB_HEADLESS env or true)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser with visible window",
    )
    args = parser.parse_args()
    headless = False if args.no_headless else args.headless
    run_bwb_cases_register(headless=headless)
