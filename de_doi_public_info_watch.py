"""
Delaware DOI Public Information watch → insurance collection.

Fetches https://insurance.delaware.gov/publicinformationsessions/,
parses Current Public Hearing / Meeting / Information Session links,
and emails when a title mentions Brighthouse or Aquarian.

Usage:
  python de_doi_public_info_watch.py --dry-run
  python de_doi_public_info_watch.py
  python de_doi_public_info_watch.py --test-email
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
    send_watch_email as send_insurance_email,
    utc_now_iso,
)
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import init_mongodb_connection
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

SCRIPT_NAME = "de_doi_public_info_watch"
SOURCE = "de_doi"
SOURCE_NAME = "Delaware Department of Insurance"
LIST_PAGE_URL = "https://insurance.delaware.gov/publicinformationsessions/"
BASE_URL = "https://insurance.delaware.gov"

CATEGORIES = {
    "public hearing": "Public Hearing",
    "public meeting": "Public Meeting",
    "public information session": "Public Information Session",
}

NONE_SCHEDULED_RE = re.compile(r"none scheduled", re.IGNORECASE)
DATE_PREFIX_RE = re.compile(
    r"^([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})\s+[–—-]\s+"
)

logger, get_log_file = ensure_script_logger(SCRIPT_NAME)


def heading_text(tag: Tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()


def normalize_category(raw: str) -> Optional[str]:
    key = re.sub(r"[:\s]+$", "", (raw or "").strip()).lower()
    return CATEGORIES.get(key)


def extract_date_text(title: str) -> str:
    match = DATE_PREFIX_RE.match(title or "")
    return match.group(1).strip() if match else ""


def make_unique_key(item: Dict[str, str]) -> str:
    item_url = (item.get("item_url") or "").strip()
    if item_url:
        return f"{SOURCE}|{item_url}"
    title = re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())
    date_text = (item.get("date_text") or "").strip().lower()
    category = (item.get("category") or "").strip().lower()
    return f"{SOURCE}|{category}|{date_text}|{title}"


def fetch_page_html(url: str = LIST_PAGE_URL, max_retries: int = 3) -> Optional[str]:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                url,
                headers={**FETCH_HEADERS, "Referer": BASE_URL},
                timeout=30,
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                logger.info("Fetched %s (%s chars)", url, f"{len(resp.text):,}")
                return resp.text
            logger.warning(
                "Attempt %s: HTTP %s, %s chars",
                attempt,
                resp.status_code,
                f"{len(resp.text):,}",
            )
        except Exception as e:
            logger.warning("Attempt %s error: %s", attempt, e)
        if attempt < max_retries:
            time.sleep(3)
    return None


def parse_current_items(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    current = None
    for h2 in soup.find_all("h2"):
        if heading_text(h2).lower() == "current":
            current = h2
            break
    if current is None:
        logger.warning("Could not find Current heading")
        return []

    items: List[Dict[str, str]] = []
    category: Optional[str] = None
    for sibling in current.find_next_siblings():
        if not isinstance(sibling, Tag):
            continue
        if sibling.name == "h2" and heading_text(sibling).lower() == "past":
            break
        if sibling.name == "h3":
            category = normalize_category(heading_text(sibling))
            continue
        if sibling.name != "blockquote" or not category:
            continue
        for p in sibling.find_all("p"):
            text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            if not text or NONE_SCHEDULED_RE.search(text):
                continue
            anchor = p.find("a", href=True)
            href = (anchor.get("href") or "").strip() if anchor else ""
            title = (
                re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
                if anchor
                else text
            )
            if not title:
                continue
            item = {
                "category": category,
                "title": title,
                "item_url": urljoin(BASE_URL, href) if href else "",
                "date_text": extract_date_text(title),
            }
            item["unique_key"] = make_unique_key(item)
            items.append(item)
    return items


def build_subject(matched_keywords: List[str]) -> str:
    names = " / ".join(matched_keywords)
    return f"Insurance Delaware finding new about {names}"


def build_email_html(item: Dict[str, str], matched_keywords: List[str], subject: str) -> str:
    item_url = item.get("item_url") or ""
    rows = [
        ("Agency", SOURCE_NAME),
        ("Category", item.get("category") or "N/A"),
        ("Date", item.get("date_text") or "N/A"),
        ("Matched", ", ".join(matched_keywords)),
        ("Title", item.get("title") or "N/A"),
        ("Notice", html_link(item_url, "Open notice →", bold=True)),
        ("Source page", html_link(LIST_PAGE_URL, LIST_PAGE_URL)),
    ]
    return build_watch_email_html(
        subject=subject,
        source_blurb="Source: Delaware Department of Insurance — Current public notices",
        matched_phrase="Title mentions",
        matched_keywords=matched_keywords,
        rows=rows,
        footer="Automated alert from Delaware DOI public information watch.",
    )


def send_watch_email(
    item: Dict[str, str],
    matched_keywords: List[str],
    *,
    test_mode: bool,
    error_items: List[Dict[str, Any]],
) -> bool:
    subject = build_subject(matched_keywords)
    html = build_email_html(item, matched_keywords, subject)
    return send_insurance_email(
        subject=subject,
        html=html,
        extras={
            "source": SOURCE,
            "category": item.get("category"),
            "title": item.get("title"),
            "item_url": item.get("item_url"),
            "list_page_url": LIST_PAGE_URL,
        },
        test_mode=test_mode,
        error_items=error_items,
    )


def run(*, dry_run: bool = False, test_mode: bool = False) -> Dict[str, int]:
    refresh_script_log(logger, get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats = {
        "total_seen": 0,
        "skipped_no_match": 0,
        "skipped_existing": 0,
        "emails_sent": 0,
        "inserted": 0,
    }

    logger.info("=" * 60)
    logger.info("DELAWARE DOI PUBLIC INFORMATION WATCH")
    if dry_run:
        logger.info("DRY-RUN: no DB writes or emails")
    if test_mode:
        logger.info("TEST-EMAIL: emails → %s via send_direct_email", TEST_RECIPIENT)
    logger.info("=" * 60)

    collection = None
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

        html = fetch_page_html()
        if not html:
            collect_error(
                error_items,
                "Failed to fetch Delaware DOI public information page",
                step="fetch_page",
                context={"url": LIST_PAGE_URL},
            )
            return stats

        items = parse_current_items(html)
        stats["total_seen"] = len(items)
        logger.info("Parsed %s Current items", len(items))
        if not items:
            logger.info("No Current hearing/meeting/session links found")
            return stats

        for item in items:
            title = item.get("title") or ""
            unique_key = item["unique_key"]
            matched = match_keywords(title)
            if not matched:
                stats["skipped_no_match"] += 1
                logger.info("No watch match: %s", title[:120])
                continue

            logger.info("MATCH %s → %s", matched, title[:120])
            if not dry_run and item_exists(collection, unique_key):
                stats["skipped_existing"] += 1
                logger.info("Already in %s; skipping", COLLECTION_NAME)
                continue

            if dry_run:
                logger.info("[DRY-RUN] Would email and insert: %s", title[:120])
                continue

            ok = send_watch_email(
                item, matched, test_mode=test_mode, error_items=error_items
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
                "category": item.get("category"),
                "title": title,
                "item_url": item.get("item_url") or "",
                "date_text": item.get("date_text") or "",
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

    except Exception as e:
        logger.exception("Unhandled error in run(): %s", e)
        collect_error(error_items, f"Unhandled error: {e}", step="run_main")
    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  Total Current items   : %s", stats["total_seen"])
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
        description="Delaware DOI public information watch → insurance collection"
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
    args = parser.parse_args()
    run(dry_run=args.dry_run, test_mode=args.test_email)
