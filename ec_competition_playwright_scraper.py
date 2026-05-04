#!/usr/bin/env python3
"""
Playwright scraper for EC Competition merger cases search results.

Target URL example:
https://competition-cases.ec.europa.eu/search?caseInstrument=M&caseOngoing=ongoing&pageSize=50&sortField=caseLastDecisionDate&sortOrder=DESC

What it does:
- Opens the search page
- Collects visible case detail links
- Opens each case in a new tab, waits for SPA content to render
- Extracts case details (title, companies, dates, regulation, etc.)
- Paginates until Next is unavailable
- Saves JSON output

Install:
    pip install playwright
    playwright install chromium

Run:
    python ec_competition_playwright_scraper.py
    python ec_competition_playwright_scraper.py --max-pages 3
    python ec_competition_playwright_scraper.py --output cases.json --headed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

START_URL = (
    "https://competition-cases.ec.europa.eu/search"
    "?caseInstrument=M&pageSize=10"
    "&sortField=caseLastDecisionDate&sortOrder=DESC"
)

WAIT_SELECTORS = [
    "a[href*='/cases/']",
    "main a[href*='/cases/']",
    "[role='main'] a[href*='/cases/']",
]

NEXT_BUTTON_SELECTORS = [
    "button[aria-label*='Next']",
    "a[aria-label*='Next']",
    "button:has-text('Next')",
    "a:has-text('Next')",
]

SPA_CONTENT_INDICATORS = [
    "text=Companies:",
    "text=Case type:",
    "text=Regulation:",
    "text=Notification date:",
    "text=Last decision date:",
]

COOKIE_ACCEPT_SELECTORS = [
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept all')",
    "button[id*='cookie'] >> text=Accept",
]


@dataclass
class CaseRecord:
    search_title: str
    case_url: str
    proc_code: Optional[str] = None
    page_title: Optional[str] = None
    case_number: Optional[str] = None
    case_title: Optional[str] = None
    companies: Optional[str] = None
    last_decision_date: Optional[str] = None
    case_type: Optional[str] = None
    investigation_phase: Optional[str] = None
    regulation: Optional[str] = None
    notification_date: Optional[str] = None
    provisional_deadline: Optional[str] = None
    economic_activities: Optional[str] = None
    metadata: Optional[Dict[str, List[str]]] = None
    raw_text: Optional[str] = None


def extract_proc_code(url: str) -> Optional[str]:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    values = qs.get("proc_code")
    if values:
        return values[0]
    match = re.search(r"/cases/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def safe_text(locator) -> str:
    try:
        return locator.inner_text(timeout=2000).strip()
    except Exception:
        return ""


def dismiss_cookie_banner(page) -> None:
    for selector in COOKIE_ACCEPT_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=3000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def wait_for_results(page, timeout_ms: int = 30000) -> str:
    last_error = None
    for selector in WAIT_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            return selector
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not find result links on page. Last error: {last_error}")


def collect_case_links(page, selector: str) -> List[Dict[str, str]]:
    links = page.locator(selector)
    count = links.count()
    results: List[Dict[str, str]] = []
    seen: Set[str] = set()

    for i in range(count):
        item = links.nth(i)
        href = item.get_attribute("href")
        text = safe_text(item)

        if not href:
            continue
        if "/cases/" not in href:
            continue

        if not href.startswith("http"):
            href = f"https://competition-cases.ec.europa.eu{href}"

        if href in seen:
            continue

        seen.add(href)
        results.append(
            {
                "title": normalize_space(text) or "Untitled Case",
                "url": href,
            }
        )

    return results


def extract_label_value_pairs(raw_text: str) -> Dict[str, List[str]]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    metadata: Dict[str, List[str]] = {}

    label_like = {
        "companies:",
        "last decision date:",
        "case type:",
        "investigation phase:",
        "regulation:",
        "notification date:",
        "provisional deadline:",
        "economic activities:",
        "simplified procedure:",
        "case notified under:",
    }

    i = 0
    while i < len(lines):
        key = lines[i].strip().lower()
        if key in label_like:
            clean_key = key.rstrip(":")
            if i + 1 < len(lines):
                val = lines[i + 1].strip()
                metadata.setdefault(clean_key, []).append(val)
            i += 2
        else:
            i += 1

    return metadata


def wait_for_spa_content(page, timeout_s: int = 15) -> bool:
    """Poll for SPA content indicators. Returns True if content loaded."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for indicator in SPA_CONTENT_INDICATORS:
            try:
                loc = page.locator(indicator).first
                if loc.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        page.wait_for_timeout(500)
    return False


def extract_case_title_from_text(raw_text: str, proc_code: str) -> Optional[str]:
    """
    The SPA renders the case title as a prominent line after the case number
    and 'Subscribe for updates'. Extract it by looking for the text block
    between 'Subscribe for updates' and 'Companies:'.
    """
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    try:
        sub_idx = None
        comp_idx = None
        for i, line in enumerate(lines):
            if "subscribe for updates" in line.lower():
                sub_idx = i
            if line.lower().startswith("companies:") or line.lower() == "companies:":
                comp_idx = i
                break
        if sub_idx is not None and comp_idx is not None and comp_idx > sub_idx + 1:
            title = lines[sub_idx + 1]
            if title.lower() != proc_code.lower() and len(title) > 3:
                return title
    except Exception:
        pass
    return None


def parse_case_page(context, search_title: str, url: str) -> CaseRecord:
    """Open a new tab, wait for SPA to render, extract data, close tab."""
    case_page = context.new_page()
    try:
        case_page.goto(url, wait_until="domcontentloaded", timeout=60000)
        dismiss_cookie_banner(case_page)

        spa_loaded = wait_for_spa_content(case_page, timeout_s=15)
        if not spa_loaded:
            case_page.wait_for_timeout(3000)

        raw_text = safe_text(case_page.locator("body"))
        metadata = extract_label_value_pairs(raw_text)
        proc_code = extract_proc_code(url)

        case_title = extract_case_title_from_text(raw_text, proc_code or "")

        companies_raw = (metadata.get("companies") or [None])[0]
        if companies_raw:
            companies_raw = companies_raw.replace(
                " |", ",").replace("| ", ",").replace("|", ",")

        record = CaseRecord(
            search_title=search_title,
            case_url=url,
            proc_code=proc_code,
            page_title=case_page.title(),
            case_number=proc_code,
            case_title=case_title,
            companies=companies_raw,
            last_decision_date=(metadata.get(
                "last decision date") or [None])[0],
            case_type=(metadata.get("case type") or [None])[0],
            investigation_phase=(metadata.get(
                "investigation phase") or [None])[0],
            regulation=(metadata.get("regulation") or [None])[0],
            notification_date=(metadata.get("notification date") or [None])[0],
            provisional_deadline=(metadata.get(
                "provisional deadline") or [None])[0],
            economic_activities=(metadata.get(
                "economic activities") or [None])[0],
            metadata=metadata or {},
            raw_text=raw_text,
        )
        return record
    finally:
        case_page.close()


def click_next_page(page) -> bool:
    for selector in NEXT_BUTTON_SELECTORS:
        locator = page.locator(selector)
        count = locator.count()
        for i in range(count):
            btn = locator.nth(i)
            try:
                if not btn.is_visible():
                    continue
                aria_disabled = (btn.get_attribute(
                    "aria-disabled") or "").lower()
                disabled = btn.get_attribute("disabled")
                if aria_disabled == "true" or disabled is not None:
                    continue
                btn.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


def scrape(start_url: str, max_pages: Optional[int], headed: bool) -> List[CaseRecord]:
    all_records: List[CaseRecord] = []
    visited_urls: Set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        search_page = context.new_page()

        search_page.goto(
            start_url, wait_until="domcontentloaded", timeout=60000)
        search_page.wait_for_timeout(3000)
        dismiss_cookie_banner(search_page)
        selector = wait_for_results(search_page)

        current_page = 1
        while True:
            print(
                f"[INFO] Search page {current_page}: collecting case links...", flush=True)
            search_page.wait_for_timeout(1000)
            links = collect_case_links(search_page, selector)
            print(f"[INFO] Found {len(links)} case links", flush=True)

            for item in links:
                if item["url"] in visited_urls:
                    continue
                visited_urls.add(item["url"])

                try:
                    record = parse_case_page(
                        context, item["title"], item["url"])
                    all_records.append(record)
                    print(
                        f"[OK] {record.proc_code or 'NO_PROC'} | "
                        f"{record.case_title or item['title']}",
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[ERROR] Failed for {item['url']}: {exc}", flush=True)
                    all_records.append(
                        CaseRecord(
                            search_title=item["title"],
                            case_url=item["url"],
                            proc_code=extract_proc_code(item["url"]),
                            metadata={"error": [str(exc)]},
                        )
                    )

            if max_pages is not None and current_page >= max_pages:
                break

            if not click_next_page(search_page):
                break

            selector = wait_for_results(search_page)
            current_page += 1

        context.close()
        browser.close()

    return all_records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=START_URL,
                        help="Search URL to scrape")
    parser.add_argument(
        "--output", default="ec_merger_ongoing_cases.json", help="Output JSON file")
    parser.add_argument("--max-pages", type=int,
                        default=None, help="Optional page limit")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser in visible mode")
    args = parser.parse_args()

    records = scrape(args.url, args.max_pages, args.headed)
    output_path = Path(args.output)
    output_path.write_text(
        json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[DONE] Saved {len(records)} records to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
