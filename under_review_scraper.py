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
    python under_review_scraper.py --test-email   # scrape + generate HTML previews, no DB/email
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cci_common import (
    SOURCE_NOTICE_UNDER_REVIEW,
    attach_cci_common_logging,
    build_cci_email_html,
    build_skeleton_doc,
    build_under_review_update_fields,
    ensure_cci_indexes,
    fetch_detail_pdf_url,
    get_cci_cases_collection,
    get_stage,
    log_cci_db_lookup,
    paginate_under_review_list,
    process_deal_match_and_email,
    source_already_processed,
    under_review_cutoff_date,
    utc_now_iso,
)
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import get_deal_by_id, init_mongodb_connection, is_connected
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

LIST_URL = "https://www.cci.gov.in/combination/notice-under-review"

SCRIPT_NAME = "cci_under_review"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger, _get_log_file = ensure_script_logger(SCRIPT_NAME, log_level=LOG_LEVEL)
LOG_FILE = refresh_script_log(logger, _get_log_file)
attach_cci_common_logging(logger)

TEST_EMAIL_DIR = "test_email_output"


def _save_test_email_html(reg_no: str, html: str) -> str:
    """Write generated email HTML to a local file and return the path."""
    Path(TEST_EMAIL_DIR).mkdir(exist_ok=True)
    safe_name = reg_no.replace("/", "_").replace("\\", "_")
    filepath = os.path.join(TEST_EMAIL_DIR, f"{safe_name}.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


def run_under_review_scraper(
    headed: bool = False,
    dry_run: bool = False,
    test_email: bool = False,
) -> None:
    global LOG_FILE
    LOG_FILE = refresh_script_log(logger, _get_log_file, LOG_FILE)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats = {
        "list_rows": 0,
        "skipped_processed": 0,
        "inserted": 0,
        "updated": 0,
        "status_updates": 0,
        "test_emails_saved": 0,
        "emails_sent": 0,
        "emails_not_sent": 0,
        "errors": 0,
    }

    cutoff = under_review_cutoff_date()
    if test_email:
        mode = "TEST-EMAIL"
    elif dry_run:
        mode = "DRY-RUN"
    else:
        mode = "LIVE"
    logger.info("=" * 60)
    logger.info("CCI Notice Under Review scraper (%s)", mode)
    logger.info("Cutoff (date_of_notification): %s", cutoff.isoformat() if cutoff else "none (all rows)")
    logger.info("Log file: %s", LOG_FILE)
    logger.info("=" * 60)

    collection = None
    if not dry_run:
        success, message = init_mongodb_connection(".env")
        if not success:
            if test_email:
                # In test-email mode DB is optional — continue without existing records
                logger.warning("MongoDB unavailable (%s); test emails will show as new cases", message)
            else:
                collect_error(error_items, message, step="mongodb_connect")
                send_error_summary(error_items, SCRIPT_NAME)
                return
        else:
            logger.info(message)
            if not is_connected():
                if test_email:
                    logger.warning("MongoDB not connected; test emails will show as new cases")
                else:
                    collect_error(error_items, "MongoDB not connected", step="mongodb_connect")
                    send_error_summary(error_items, SCRIPT_NAME)
                    return
            else:
                collection = get_cci_cases_collection()
                if collection is None:
                    if test_email:
                        logger.warning("cci_cases collection unavailable; test emails will show as new cases")
                    else:
                        collect_error(
                            error_items, "cci_cases collection unavailable", step="get_collection")
                        send_error_summary(error_items, SCRIPT_NAME)
                        return
                elif not test_email:
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
                # Pass None in test-email/dry-run modes to skip last_seen_at writes
                collection=collection if (not dry_run and not test_email) else None,
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

                # TEST-EMAIL mode: read DB, detect status change, generate HTML preview.
                # No DB writes, no LLM, no webhook.
                if test_email:
                    existing_te = collection.find_one(
                        {"combination_registration_no": reg_no}
                    ) if collection is not None else None

                    old_status_te = (existing_te.get("cci_status") or "").strip() if existing_te else ""
                    new_status_te = (row.get("cci_status") or "").strip()
                    old_decision_te = (existing_te.get("decision_date") or "").strip() if existing_te else ""
                    new_decision_te = (row.get("decision_date") or "").strip()

                    is_new_te = existing_te is None
                    status_changed_te = (
                        not is_new_te
                        and source_already_processed(existing_te, SOURCE_NOTICE_UNDER_REVIEW)
                        and (old_status_te != new_status_te or old_decision_te != new_decision_te)
                    )

                    if not is_new_te and not status_changed_te:
                        logger.info(
                            "  [TEST-EMAIL] Skip: already processed, status unchanged (%s)",
                            old_status_te,
                        )
                        continue

                    try:
                        pdf_url = fetch_detail_pdf_url(page, detail_url)
                    except Exception as exc:
                        logger.exception("  Detail page error: %s", exc)
                        stats["errors"] += 1
                        continue

                    changes_te: Optional[Dict[str, Any]] = None
                    if status_changed_te:
                        changes_te = {
                            "old": {
                                "cci_status":              old_status_te or None,
                                "stage":                   existing_te.get("stage"),
                                "decision_date":           old_decision_te or None,
                                "notice_under_review_url": existing_te.get("notice_under_review_url"),
                            },
                            "new": {
                                "cci_status":              new_status_te or None,
                                "stage":                   get_stage(new_status_te) if new_status_te else None,
                                "decision_date":           new_decision_te or None,
                                "notice_under_review_url": pdf_url,
                            },
                        }

                    # Build a record merging existing DB data with fresh scraped values
                    base = dict(existing_te) if existing_te else {}
                    base.update({
                        "combination_registration_no": reg_no,
                        "notifying_parties": row.get("notifying_parties") or base.get("notifying_parties"),
                        "form": row.get("form") or base.get("form"),
                        "date_of_notification": row.get("date_of_notification") or base.get("date_of_notification"),
                        "cci_status": new_status_te or None,
                        "stage": get_stage(new_status_te) if new_status_te else base.get("stage"),
                        "decision_date": new_decision_te or base.get("decision_date"),
                        "notice_under_review_url": pdf_url,
                        "detail_urls": {SOURCE_NOTICE_UNDER_REVIEW: detail_url},
                    })

                    event_type_te = "new" if is_new_te else "update"
                    deal_match_te = None
                    if existing_te and existing_te.get("deal_id"):
                        deal_match_te = get_deal_by_id(str(existing_te["deal_id"]))

                    _, html = build_cci_email_html(
                        base,
                        deal_match=deal_match_te,
                        event_type=event_type_te,
                        source_label="Notice Under Review",
                        list_page_url=LIST_URL,
                        changes=changes_te,
                    )
                    filepath = _save_test_email_html(reg_no, html)
                    logger.info(
                        "  [TEST-EMAIL] %s → %s",
                        "status-update" if status_changed_te else "new-case",
                        filepath,
                    )
                    stats["test_emails_saved"] += 1
                    continue

                now_iso = utc_now_iso()
                existing = None
                if collection is not None:
                    existing = collection.find_one(
                        {"combination_registration_no": reg_no}
                    )
                    log_cci_db_lookup(reg_no, existing, SOURCE_NOTICE_UNDER_REVIEW)

                # Detect whether this is a first-time process, a status-change
                # update, or a truly unchanged already-processed record.
                changes = None
                is_status_update = False
                if existing and source_already_processed(existing, SOURCE_NOTICE_UNDER_REVIEW):
                    old_status = (existing.get("cci_status") or "").strip()
                    new_status = (row.get("cci_status") or "").strip()
                    old_decision = (existing.get("decision_date") or "").strip()
                    new_decision = (row.get("decision_date") or "").strip()

                    if old_status == new_status and old_decision == new_decision:
                        stats["skipped_processed"] += 1
                        logger.info(
                            "  Skip: already processed, status unchanged (%s)",
                            old_status,
                        )
                        continue

                    # Something changed — re-process and send update email
                    is_status_update = True
                    logger.info(
                        "  Status change detected: '%s' → '%s' | decision_date: '%s' → '%s'",
                        old_status, new_status,
                        old_decision, new_decision,
                    )
                    changes = {
                        "old": {
                            "cci_status":              old_status or None,
                            "stage":                   existing.get("stage"),
                            "decision_date":           old_decision or None,
                            "notice_under_review_url": existing.get("notice_under_review_url"),
                        },
                        "new": {
                            "cci_status":  new_status or None,
                            "stage":       get_stage(new_status) if new_status else None,
                            "decision_date": new_decision or None,
                            # pdf_url filled in after fetch_detail_pdf_url below
                        },
                    }

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

                # Fill in the new PDF URL now that we have it
                if changes is not None:
                    changes["new"]["notice_under_review_url"] = pdf_url

                is_new_record = existing is None

                if dry_run:
                    if is_status_update:
                        action = "status-update"
                    elif is_new_record:
                        action = "insert"
                    else:
                        action = "update"
                    logger.info(
                        "  [DRY-RUN] would %s — pdf=%s | changes=%s (no DB write, no LLM/email)",
                        action, pdf_url, changes,
                    )
                    if is_new_record:
                        stats["inserted"] += 1
                    elif is_status_update:
                        stats["status_updates"] += 1
                    else:
                        stats["updated"] += 1
                    continue

                logger.info(
                    "  Persisting to cci_cases (%s)...",
                    "status-update" if is_status_update else ("insert" if is_new_record else "update"),
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
                    if is_status_update:
                        stats["status_updates"] += 1
                        logger.info("  Updated existing case (status change)")
                    else:
                        stats["updated"] += 1
                        logger.info("  Updated existing case (first time from this source)")

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
                    changes=changes,
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
        if not test_email:
            send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  List rows (within cutoff)  : %s", stats["list_rows"])
        if test_email:
            logger.info("  Test emails saved          : %s", stats["test_emails_saved"])
            logger.info("  Output dir                 : %s/", TEST_EMAIL_DIR)
        else:
            logger.info("  Inserted                   : %s", stats["inserted"])
            logger.info("  Updated (first time)       : %s", stats["updated"])
            logger.info("  Status updates             : %s", stats["status_updates"])
            logger.info("  Skipped (no change)        : %s", stats["skipped_processed"])
            logger.info("  Emails sent                : %s", stats["emails_sent"])
            logger.info("  Emails not sent            : %s", stats["emails_not_sent"])
        logger.info("  Errors                     : %s", len(error_items))
        logger.info("  Elapsed                    : %ss", elapsed)
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
    parser.add_argument(
        "--test-email",
        action="store_true",
        help=(
            "Scrape real data and generate HTML email previews in "
            f"{TEST_EMAIL_DIR}/ — no DB writes, no LLM, no email sent"
        ),
    )
    args = parser.parse_args()
    run_under_review_scraper(
        headed=args.headed,
        dry_run=args.dry_run,
        test_email=args.test_email,
    )


if __name__ == "__main__":
    main()
