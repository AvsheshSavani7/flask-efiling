#!/usr/bin/env python3
"""
CCI India — Notice Under Review scraper (Phase 1)
=================================================
Scrapes https://www.cci.gov.in/combination/notice-under-review
Stores cases in cci_cases, matches deals, sends email notifications.

Usage:
    python under_review_scraper.py
    python under_review_scraper.py --headed
    python under_review_scraper.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any, Dict, List

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cci_common import (
    SOURCE_NOTICE_UNDER_REVIEW,
    attach_cci_common_logging,
    build_skeleton_doc,
    build_under_review_update_fields,
    ensure_cci_indexes,
    fetch_detail_pdf_url,
    get_cci_cases_collection,
    log_cci_db_lookup,
    paginate_under_review_list,
    process_deal_match_and_email,
    source_already_processed,
    under_review_cutoff_date,
    utc_now_iso,
)
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import init_mongodb_connection, is_connected
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

LIST_URL = "https://www.cci.gov.in/combination/notice-under-review"

SCRIPT_NAME = "cci_under_review"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger, _get_log_file = ensure_script_logger(SCRIPT_NAME, log_level=LOG_LEVEL)
LOG_FILE = refresh_script_log(logger, _get_log_file)
attach_cci_common_logging(logger)


def run_under_review_scraper(headed: bool = False, dry_run: bool = False) -> None:
    global LOG_FILE
    LOG_FILE = refresh_script_log(logger, _get_log_file, LOG_FILE)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats = {
        "list_rows": 0,
        "skipped_processed": 0,
        "inserted": 0,
        "updated": 0,
        "emails_sent": 0,
        "emails_not_sent": 0,
        "errors": 0,
    }

    cutoff = under_review_cutoff_date()
    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("=" * 60)
    logger.info("CCI Notice Under Review scraper (%s)", mode)
    logger.info("Cutoff (date_of_notification): >= %s", cutoff.isoformat())
    logger.info("Log file: %s", LOG_FILE)
    logger.info("=" * 60)

    collection = None
    if not dry_run:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(error_items, message, step="mongodb_connect")
            send_error_summary(error_items, SCRIPT_NAME)
            return
        logger.info(message)
        if not is_connected():
            collect_error(error_items, "MongoDB not connected",
                          step="mongodb_connect")
            send_error_summary(error_items, SCRIPT_NAME)
            return
        collection = get_cci_cases_collection()
        if collection is None:
            collect_error(
                error_items, "cci_cases collection unavailable", step="get_collection")
            send_error_summary(error_items, SCRIPT_NAME)
            return
        ensure_cci_indexes(collection)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not headed,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            logger.info("Loading list page: %s", LIST_URL)
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            rows = paginate_under_review_list(
                page,
                cutoff,
                collection=collection if not dry_run else None,
            )
            stats["list_rows"] = len(rows)
            logger.info("Rows within cutoff: %s", len(rows))

            for idx, row in enumerate(rows, 1):
                LOG_FILE = refresh_script_log(logger, _get_log_file, LOG_FILE)

                reg_no = row.get("combination_registration_no", "").strip()
                detail_url = row.get("detail_url", "")
                logger.info(
                    "[%s/%s] %s — %s",
                    idx,
                    len(rows),
                    reg_no,
                    (row.get("notifying_parties") or "")[:80],
                )

                if not reg_no or not detail_url:
                    logger.warning("  Missing reg_no or detail_url; skipping")
                    continue

                now_iso = utc_now_iso()
                existing = None
                if collection is not None:
                    existing = collection.find_one(
                        {"combination_registration_no": reg_no}
                    )
                    log_cci_db_lookup(reg_no, existing, SOURCE_NOTICE_UNDER_REVIEW)

                if existing and source_already_processed(existing, SOURCE_NOTICE_UNDER_REVIEW):
                    stats["skipped_processed"] += 1
                    logger.info(
                        "  Skip: already processed from notice_under_review "
                        "(no detail visit, no email)",
                    )
                    continue

                try:
                    pdf_url = fetch_detail_pdf_url(page, detail_url)
                except Exception as exc:
                    logger.exception("  Detail page error: %s", exc)
                    collect_error(
                        error_items,
                        str(exc),
                        step="fetch_detail_pdf_url",
                        context={
                            "combination_registration_no": reg_no,
                            "detail_url": detail_url,
                        },
                    )
                    stats["errors"] += 1
                    continue

                is_new_record = existing is None

                if dry_run:
                    logger.info(
                        "  [DRY-RUN] would %s — pdf=%s (no DB write, no LLM/email)",
                        "insert" if is_new_record else "update",
                        pdf_url,
                    )
                    if is_new_record:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1
                    continue

                logger.info(
                    "  Persisting to cci_cases (%s)...",
                    "insert" if is_new_record else "update",
                )
                if is_new_record:
                    doc = build_skeleton_doc(
                        row,
                        SOURCE_NOTICE_UNDER_REVIEW,
                        detail_url,
                        pdf_url,
                        now_iso,
                    )
                    collection.insert_one(doc)
                    stats["inserted"] += 1
                    logger.info("  Inserted new case")
                else:
                    update_fields = build_under_review_update_fields(
                        row, detail_url, pdf_url, now_iso, existing
                    )
                    collection.update_one(
                        {"combination_registration_no": reg_no},
                        {"$set": update_fields},
                    )
                    stats["updated"] += 1
                    logger.info(
                        "  Updated existing case (first time from this source)")

                record = collection.find_one(
                    {"combination_registration_no": reg_no})
                if not record:
                    logger.warning(
                        "  Record missing after persist; skipping deal match / email (reg_no=%s)",
                        reg_no,
                    )
                    continue

                logger.info("  Running deal match / email pipeline...")
                email_sent = process_deal_match_and_email(
                    collection,
                    record,
                    is_new_record=is_new_record,
                    error_items=error_items,
                    source_label="Notice Under Review",
                    list_page_url=LIST_URL,
                    source_key=SOURCE_NOTICE_UNDER_REVIEW,
                )
                logger.info(
                    "  Deal match / email pipeline finished (email_sent=%s)",
                    email_sent,
                )
                if email_sent:
                    stats["emails_sent"] += 1
                else:
                    stats["emails_not_sent"] += 1

            browser.close()

    except Exception as exc:
        logger.exception("Unhandled error: %s", exc)
        collect_error(error_items, str(exc), step="run_main")

    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  List rows (within cutoff) : %s", stats["list_rows"])
        logger.info("  Inserted                  : %s", stats["inserted"])
        logger.info("  Updated                   : %s", stats["updated"])
        logger.info("  Skipped (already processed): %s",
                    stats["skipped_processed"])
        logger.info("  Emails sent               : %s", stats["emails_sent"])
        logger.info("  Emails not sent           : %s", stats["emails_not_sent"])
        logger.info("  Errors                    : %s", len(error_items))
        logger.info("  Elapsed                   : %ss", elapsed)
        logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CCI Notice Under Review scraper")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape only; do not write to MongoDB or send email",
    )
    args = parser.parse_args()
    run_under_review_scraper(headed=args.headed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
