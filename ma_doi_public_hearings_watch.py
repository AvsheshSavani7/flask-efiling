"""
Massachusetts DOI public hearings watch → insurance collection.

Fetches https://www.mass.gov/lists/division-of-insurance-public-hearings,
uses only the top featured "{Month} {Year} Public Hearing Schedule" links
(stops before Table of Contents / year PDF archives), opens each schedule
page, and emails when a table Case mentions Brighthouse or Aquarian.

Usage:
  python ma_doi_public_hearings_watch.py --dry-run
  python ma_doi_public_hearings_watch.py
  python ma_doi_public_hearings_watch.py --test-email
"""

from __future__ import annotations

import argparse
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from insurance_watch_common import (
    COLLECTION_NAME,
    FETCH_HEADERS,
    TEST_RECIPIENT,
    build_watch_email_html,
    ensure_indexes,
    get_collection,
    html_link,
    insert_item,
    item_exists,
    match_keywords,
    normalize_space,
    send_watch_email,
    utc_now_iso,
)
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import init_mongodb_connection
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

SCRIPT_NAME = "ma_doi_public_hearings_watch"
SOURCE = "ma_doi"
SOURCE_NAME = "Massachusetts Division of Insurance"
LIST_PAGE_URL = "https://www.mass.gov/lists/division-of-insurance-public-hearings"
BASE_URL = "https://www.mass.gov"

MONTHS = (
    "January|February|March|April|May|June|July|"
    "August|September|October|November|December"
)
SCHEDULE_LINK_RE = re.compile(
    rf"^({MONTHS})\s+\d{{4}}\s+Public Hearing Schedule$",
    re.IGNORECASE,
)
STOP_HEADING_RE = re.compile(
    r"^(table of contents|year\s+\d{4}\s+public hearings)$",
    re.IGNORECASE,
)
EMPTY_TABLE_RE = re.compile(r"no hearings currently scheduled", re.IGNORECASE)

HEADER_MAP = {
    "docket no": "docket_no",
    "docket no.": "docket_no",
    "docket number": "docket_no",
    "time / room": "time_room",
    "time/room": "time_room",
    "date": "date_text",
    "case": "title",
    "presiding officer": "officer",
}

logger, get_log_file = ensure_script_logger(SCRIPT_NAME)


def heading_text(tag: Tag) -> str:
    return normalize_space(tag.get_text(" ", strip=True))


def make_unique_key(item: Dict[str, str]) -> str:
    docket_no = normalize_space(item.get("docket_no") or "")
    if docket_no:
        return f"{SOURCE}|{docket_no}"
    title = normalize_space(item.get("title") or "").lower()
    date_text = normalize_space(item.get("date_text") or "").lower()
    schedule_url = (item.get("schedule_url") or "").strip()
    return f"{SOURCE}|{schedule_url}|{date_text}|{title}"


def fetch_html_requests(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers={**FETCH_HEADERS, "Referer": BASE_URL}, timeout=30)
        if resp.status_code == 200 and len(resp.text) > 500 and "this page is forbidden" not in resp.text.lower():
            logger.info("Fetched %s via requests (%s chars)", url, f"{len(resp.text):,}")
            return resp.text
        logger.warning("Requests fetch %s: HTTP %s, %s chars", url, resp.status_code, f"{len(resp.text):,}")
    except Exception as e:
        logger.warning("Requests fetch %s failed: %s", url, e)
    return None


def fetch_html_playwright(page, url: str) -> Optional[str]:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        html = page.content()
        if html and len(html) > 500 and "this page is forbidden" not in html.lower():
            logger.info("Fetched %s via Playwright (%s chars)", url, f"{len(html):,}")
            return html
        logger.warning("Playwright fetch %s returned short/forbidden HTML (%s chars)", url, len(html or ""))
    except Exception as e:
        logger.warning("Playwright fetch %s failed: %s", url, e)
    return None


def fetch_page_html(url: str, page=None) -> Optional[str]:
    if page is not None:
        html = fetch_html_playwright(page, url)
        if html:
            return html
    return fetch_html_requests(url)


def parse_schedule_links(html: str) -> List[Dict[str, str]]:
    """Collect top featured month-schedule links; ignore TOC and year archives."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    links = _collect_schedule_links_until_stop(main)
    if links:
        return links
    logger.warning(
        "No schedule links before Table of Contents; scanning featured links only"
    )
    return _collect_schedule_links_excluding_archives(main)


def _collect_schedule_links_until_stop(root: Tag) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen = set()
    for el in root.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name in ("h2", "h3", "h4") and STOP_HEADING_RE.match(heading_text(el)):
            break
        _maybe_add_schedule_link(el, links, seen)
    return links


def _collect_schedule_links_excluding_archives(root: Tag) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen = set()
    past_year_archive = False
    for el in root.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name in ("h2", "h3", "h4") and re.match(
            r"^year\s+\d{4}\s+public hearings$", heading_text(el), re.IGNORECASE
        ):
            past_year_archive = True
            continue
        if past_year_archive:
            continue
        if el.find_parent(class_=re.compile(r"(^|__)toc($|-|_)", re.I)):
            continue
        if el.find_parent(id=re.compile(r"table-of-contents", re.I)):
            continue
        _maybe_add_schedule_link(el, links, seen)
    return links


def _maybe_add_schedule_link(el: Tag, links: List[Dict[str, str]], seen: set) -> None:
    if el.name != "a" or not el.get("href"):
        return
    text = normalize_space(el.get_text(" ", strip=True))
    if not text or not SCHEDULE_LINK_RE.match(text):
        return
    url = urljoin(BASE_URL, el["href"].strip())
    if url in seen:
        return
    seen.add(url)
    links.append({"label": text, "url": url})


def _header_key(raw: str) -> str:
    key = normalize_space(raw).lower().rstrip(".")
    return HEADER_MAP.get(key, "")


def parse_hearing_rows(html: str, schedule: Dict[str, str]) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, str]] = []
    schedule_url = schedule.get("url") or ""
    schedule_label = schedule.get("label") or ""

    for table in soup.find_all("table"):
        header_cells = table.find("tr")
        if header_cells is None:
            continue
        headers = [_header_key(th.get_text(" ", strip=True)) for th in header_cells.find_all(["th", "td"])]
        if "title" not in headers:
            continue

        body_rows = table.find_all("tr")[1:]
        for tr in body_rows:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            joined = normalize_space(" ".join(c.get_text(" ", strip=True) for c in cells))
            if not joined or EMPTY_TABLE_RE.search(joined):
                continue
            values = [normalize_space(c.get_text(" ", strip=True)) for c in cells]
            row: Dict[str, str] = {
                "category": "Public Hearing",
                "schedule_url": schedule_url,
                "schedule_label": schedule_label,
                "docket_no": "",
                "time_room": "",
                "date_text": "",
                "title": "",
                "officer": "",
            }
            for key, value in zip(headers, values):
                if key:
                    row[key] = value
            if not row["title"]:
                continue
            row["item_url"] = schedule_url
            row["unique_key"] = make_unique_key(row)
            items.append(row)
    return items


def build_subject(matched_keywords: List[str]) -> str:
    names = " / ".join(matched_keywords)
    return f"Insurance Massachusetts finding new about {names}"


def build_email_html(item: Dict[str, str], matched_keywords: List[str], subject: str) -> str:
    schedule_url = item.get("schedule_url") or ""
    rows = [
        ("Agency", SOURCE_NAME),
        ("Category", item.get("category") or "Public Hearing"),
        ("Docket No.", item.get("docket_no") or "N/A"),
        ("Date", item.get("date_text") or "N/A"),
        ("Time / Room", item.get("time_room") or "N/A"),
        ("Matched", ", ".join(matched_keywords)),
        ("Case", item.get("title") or "N/A"),
        ("Schedule", html_link(schedule_url, "Open schedule →", bold=True)),
        ("Source page", html_link(LIST_PAGE_URL, LIST_PAGE_URL)),
    ]
    return build_watch_email_html(
        subject=subject,
        source_blurb="Source: Massachusetts Division of Insurance — Current public hearing schedule",
        matched_phrase="Case mentions",
        matched_keywords=matched_keywords,
        rows=rows,
        footer="Automated alert from Massachusetts DOI public hearings watch.",
    )


def process_items(
    items: List[Dict[str, str]],
    *,
    collection,
    dry_run: bool,
    test_mode: bool,
    error_items: List[Dict[str, Any]],
    stats: Dict[str, int],
) -> None:
    stats["total_seen"] += len(items)
    for item in items:
        title = item.get("title") or ""
        haystack = " ".join(
            [
                item.get("docket_no") or "",
                title,
                item.get("officer") or "",
            ]
        )
        matched = match_keywords(haystack)
        if not matched:
            stats["skipped_no_match"] += 1
            logger.info("No watch match: %s", title[:120])
            continue

        unique_key = item["unique_key"]
        logger.info("MATCH %s → %s", matched, title[:120])
        if not dry_run and item_exists(collection, unique_key):
            stats["skipped_existing"] += 1
            logger.info("Already in %s; skipping", COLLECTION_NAME)
            continue

        if dry_run:
            logger.info("[DRY-RUN] Would email and insert: %s", title[:120])
            continue

        subject = build_subject(matched)
        html = build_email_html(item, matched, subject)
        ok = send_watch_email(
            subject=subject,
            html=html,
            extras={
                "source": SOURCE,
                "category": item.get("category"),
                "title": title,
                "docket_no": item.get("docket_no"),
                "item_url": item.get("item_url"),
                "schedule_url": item.get("schedule_url"),
                "list_page_url": LIST_PAGE_URL,
            },
            test_mode=test_mode,
            error_items=error_items,
        )
        if not ok:
            logger.warning("Email failed — not inserting so it can retry")
            continue
        stats["emails_sent"] += 1

        now = utc_now_iso()
        doc = {
            "unique_key": unique_key,
            "source": SOURCE,
            "source_name": SOURCE_NAME,
            "list_page_url": LIST_PAGE_URL,
            "schedule_url": item.get("schedule_url") or "",
            "schedule_label": item.get("schedule_label") or "",
            "category": item.get("category") or "Public Hearing",
            "docket_no": item.get("docket_no") or "",
            "title": title,
            "item_url": item.get("item_url") or "",
            "date_text": item.get("date_text") or "",
            "time_room": item.get("time_room") or "",
            "officer": item.get("officer") or "",
            "matched_keywords": matched,
            "emailed": True,
            "emailed_at": now,
            "created_at": now,
        }
        if insert_item(collection, doc):
            stats["inserted"] += 1
            logger.info("Inserted unique_key=%s", unique_key)
        else:
            collect_error(
                error_items,
                "DB insert returned False after email was sent",
                step="insert",
                context={"unique_key": unique_key, "title": title},
            )


def run(*, dry_run: bool = False, test_mode: bool = False, headless: bool = True) -> Dict[str, int]:
    refresh_script_log(logger, get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats = {
        "schedule_links": 0,
        "total_seen": 0,
        "skipped_no_match": 0,
        "skipped_existing": 0,
        "emails_sent": 0,
        "inserted": 0,
    }

    logger.info("=" * 60)
    logger.info("MASSACHUSETTS DOI PUBLIC HEARINGS WATCH")
    if dry_run:
        logger.info("DRY-RUN: no DB writes or emails")
    if test_mode:
        logger.info("TEST-EMAIL: emails → %s via send_direct_email", TEST_RECIPIENT)
    logger.info("=" * 60)

    collection = None
    playwright_cm = None
    page = None
    try:
        if not dry_run:
            success, message = init_mongodb_connection(".env")
            if not success:
                collect_error(
                    error_items,
                    f"MongoDB init failed: {message}",
                    step="init_mongodb",
                )
                return stats
            collection = get_collection()
            if collection is None:
                collect_error(
                    error_items,
                    f"Could not access '{COLLECTION_NAME}' collection",
                    step="get_collection",
                )
                return stats
            ensure_indexes(collection)

        list_html = None
        try:
            playwright_cm = sync_playwright().start()
            browser = playwright_cm.chromium.launch(headless=headless)
            page = browser.new_page(extra_http_headers=FETCH_HEADERS)
            list_html = fetch_page_html(LIST_PAGE_URL, page=page)
        except Exception as e:
            logger.warning("Playwright unavailable (%s); trying requests", e)
            list_html = fetch_page_html(LIST_PAGE_URL, page=None)

        if not list_html:
            collect_error(
                error_items,
                "Failed to fetch Massachusetts DOI public hearings list page",
                step="fetch_page",
                context={"url": LIST_PAGE_URL},
            )
            return stats

        schedules = parse_schedule_links(list_html)
        stats["schedule_links"] = len(schedules)
        logger.info("Found %s top schedule link(s)", len(schedules))
        if not schedules:
            logger.info("No month Public Hearing Schedule links above Table of Contents")
            return stats

        for schedule in schedules:
            logger.info("Opening schedule: %s → %s", schedule["label"], schedule["url"])
            sched_html = fetch_page_html(schedule["url"], page=page)
            if not sched_html:
                collect_error(
                    error_items,
                    "Failed to fetch month schedule page",
                    step="fetch_schedule",
                    context={"url": schedule["url"], "title": schedule["label"]},
                )
                continue
            items = parse_hearing_rows(sched_html, schedule)
            logger.info("Parsed %s hearing row(s) from %s", len(items), schedule["label"])
            if not items:
                logger.info("No hearings currently scheduled on %s", schedule["label"])
                continue
            process_items(
                items,
                collection=collection,
                dry_run=dry_run,
                test_mode=test_mode,
                error_items=error_items,
                stats=stats,
            )
    except Exception as e:
        logger.exception("Unhandled error in run(): %s", e)
        collect_error(error_items, f"Unhandled error: {e}", step="run_main")
    finally:
        try:
            if playwright_cm is not None:
                playwright_cm.stop()
        except Exception:
            pass
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  Schedule links        : %s", stats["schedule_links"])
        logger.info("  Hearing rows          : %s", stats["total_seen"])
        logger.info("  Skipped (no match)    : %s", stats["skipped_no_match"])
        logger.info("  Skipped (in DB)       : %s", stats["skipped_existing"])
        logger.info("  Emails sent           : %s", stats["emails_sent"])
        logger.info("  Inserted              : %s", stats["inserted"])
        logger.info("  Errors                : %s", len(error_items))
        logger.info("  Total time            : %ss", elapsed)
        logger.info("=" * 60)

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Massachusetts DOI public hearings watch → insurance collection"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and match only — no DB writes or emails",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help=f"Send emails to {TEST_RECIPIENT} via N8N_WEBHOOK_ONLY_ME",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Playwright with a visible browser",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, test_mode=args.test_email, headless=not args.headed)
