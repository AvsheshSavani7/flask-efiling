"""
Shared runtime for CCI datatable scrapers (logging, browser, main loop).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cci_common import (
    attach_cci_common_logging,
    ensure_cci_indexes,
    get_cci_cases_collection,
    log_cci_db_lookup,
    paginate_cci_list,
    process_deal_match_and_email,
    source_already_processed,
    utc_now_iso,
)
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import init_mongodb_connection, is_connected
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def setup_cci_logger(script_name: str) -> Tuple[logging.Logger, Callable[[], str]]:
    """IST-dated logger (same as ACCC/FTC). Call refresh_script_log at each run."""
    log, get_log_file = ensure_script_logger(script_name, log_level=LOG_LEVEL)
    attach_cci_common_logging(log)
    return log, get_log_file


@dataclass
class CciScraperConfig:
    list_url: str
    script_name: str
    source_key: str
    source_label: str
    title: str
    parse_table: Callable[[str], List[Dict[str, Any]]]
    cutoff: Optional[date]
    cutoff_field: str
    single_page: bool = False
    row_label: Callable[[Dict[str, Any]], str] = lambda r: (
        (r.get("notifying_parties") or r.get("description") or "")[:80]
    )
    fetch_detail: Callable[..., Any] = None  # set per scraper
    persist_record: Callable[..., None] = None  # set per scraper


def run_cci_datatable_scraper(
    config: CciScraperConfig,
    headed: bool = False,
    dry_run: bool = False,
) -> None:
    logger, get_log_file = setup_cci_logger(config.script_name)
    log_file = refresh_script_log(logger, get_log_file)
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

    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("=" * 60)
    logger.info("%s (%s)", config.title, mode)
    if config.cutoff:
        logger.info("Cutoff (%s): >= %s", config.cutoff_field, config.cutoff.isoformat())
    else:
        logger.info("Cutoff: none (single page scrape)")
    logger.info("Log file: %s", log_file)
    logger.info("=" * 60)

    collection = None
    if not dry_run:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(error_items, message, step="mongodb_connect")
            send_error_summary(error_items, config.script_name)
            return
        logger.info(message)
        if not is_connected():
            collect_error(error_items, "MongoDB not connected", step="mongodb_connect")
            send_error_summary(error_items, config.script_name)
            return
        collection = get_cci_cases_collection()
        if collection is None:
            collect_error(
                error_items, "cci_cases collection unavailable", step="get_collection"
            )
            send_error_summary(error_items, config.script_name)
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

            logger.info("Loading list page: %s", config.list_url)
            page.goto(config.list_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            rows = paginate_cci_list(
                page,
                config.parse_table,
                config.cutoff,
                config.cutoff_field,
                collection=collection if not dry_run else None,
                source_key=config.source_key,
                single_page=config.single_page,
            )
            stats["list_rows"] = len(rows)
            logger.info("Rows to process: %s", len(rows))

            for idx, row in enumerate(rows, 1):
                log_file = refresh_script_log(logger, get_log_file, log_file)

                reg_no = (row.get("combination_registration_no") or "").strip()
                detail_url = row.get("detail_url", "")
                logger.info(
                    "[%s/%s] %s — %s",
                    idx,
                    len(rows),
                    reg_no,
                    config.row_label(row),
                )

                if not reg_no or not detail_url:
                    logger.warning("  Missing reg_no or detail_url; skipping")
                    continue

                existing = None
                if collection is not None:
                    existing = collection.find_one(
                        {"combination_registration_no": reg_no}
                    )
                    log_cci_db_lookup(reg_no, existing, config.source_key)

                if existing and source_already_processed(existing, config.source_key):
                    stats["skipped_processed"] += 1
                    logger.info(
                        "  Skip: already processed from %s (no detail visit, no email)",
                        config.source_key,
                    )
                    continue

                try:
                    detail_data = config.fetch_detail(page, row, detail_url)
                except Exception as exc:
                    logger.exception("  Detail page error: %s", exc)
                    collect_error(
                        error_items,
                        str(exc),
                        step="fetch_detail",
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
                        "  [DRY-RUN] would %s — %s (no DB write, no LLM/email)",
                        "insert" if is_new_record else "update",
                        detail_data,
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
                config.persist_record(
                    collection,
                    row,
                    detail_url,
                    detail_data,
                    existing,
                    is_new_record,
                    utc_now_iso(),
                )
                if is_new_record:
                    stats["inserted"] += 1
                    logger.info("  Inserted new case")
                else:
                    stats["updated"] += 1
                    logger.info("  Updated existing case (first time from this source)")

                record = collection.find_one({"combination_registration_no": reg_no})
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
                    source_label=config.source_label,
                    list_page_url=config.list_url,
                    source_key=config.source_key,
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
        send_error_summary(error_items, config.script_name)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  List rows                : %s", stats["list_rows"])
        logger.info("  Inserted                 : %s", stats["inserted"])
        logger.info("  Updated                  : %s", stats["updated"])
        logger.info("  Skipped (already done)   : %s", stats["skipped_processed"])
        logger.info("  Emails sent              : %s", stats["emails_sent"])
        logger.info("  Emails not sent          : %s", stats["emails_not_sent"])
        logger.info("  Errors                   : %s", len(error_items))
        logger.info("  Elapsed                  : %ss", elapsed)
        logger.info("=" * 60)
