"""
Shared helpers for Austrian BWB (Bundeswettbewerbsbehörde) merger scrapers.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.bwb.gv.at"
COLLECTION_NAME = "bwb_cases"
CLOSED_STATUSES = frozenset({"Fristablauf", "Prüfungsverzicht"})
TRANSLATION_FAILED_MARKER = "[Translation failed]"

logger = logging.getLogger(__name__)


class BwbWorkflowError(Exception):
    """Fatal BWB workflow error — stop processing and report via error email."""

    def __init__(
        self,
        message: str,
        *,
        step: str = "workflow",
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.step = step
        self.context = context or {}


def current_year() -> int:
    return datetime.now().year


def listing_url(year: Optional[int] = None) -> str:
    y = year if year is not None else current_year()
    return f"{BASE_URL}/zusammenschluesse/{y}"


def monitor_listing_years(now: Optional[datetime] = None) -> List[int]:
    """
    Listing years to scrape for the BWB update monitor.

    Jan–Apr: current year + prior year (year rollover).
    May–Dec: current year only.
    """
    now = now or datetime.now()
    years = [now.year]
    if now.month <= 4:
        years.append(now.year - 1)
    return sorted(set(years))


def utc_now_iso() -> str:
    from datetime import timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_de_text(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_merger_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def determine_is_open(status: str) -> bool:
    return (status or "").strip() not in CLOSED_STATUSES


def translate_to_english(text: str) -> str:
    if not text or not text.strip():
        return ""
    text = text.strip()
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "de", "tl": "en", "dt": "t", "q": text}
        response = requests.get(url, params=params, timeout=30)
        if response.status_code == 200:
            data = response.json()
            segments = data[0] if data and isinstance(data[0], list) else []
            parts = [
                seg[0].strip()
                for seg in segments
                if isinstance(seg, (list, tuple)) and seg and seg[0]
            ]
            if parts:
                return " ".join(parts).strip()
    except Exception as e:
        logger.warning("Translation failed for %s... → %s", text[:50], e)
    return TRANSLATION_FAILED_MARKER


def translate_to_english_required(
    text: str,
    *,
    field: str,
    file_number: str,
) -> str:
    """Translate German text; raise BwbWorkflowError on failure."""
    result = translate_to_english(text)
    if not result or result.strip() == TRANSLATION_FAILED_MARKER:
        raise BwbWorkflowError(
            f"Translation failed for field '{field}' (file_number={file_number})",
            step="translate",
            context={
                "file_number": file_number,
                "field": field,
                "source_text_snippet": (text or "")[:300],
            },
        )
    return result


def extract_detail_content_de(soup: BeautifulSoup) -> str:
    node = soup.select_one("div.row.content")
    if node is None:
        return ""
    node = BeautifulSoup(str(node), "html.parser").select_one("div.row.content")
    if node is None:
        return ""
    for el in node.select(".backtolist"):
        el.decompose()
    return normalize_de_text(node.get_text(" ", strip=True))


def parse_detail_page(html_content: str) -> Dict[str, str]:
    soup = BeautifulSoup(html_content, "html.parser")
    title_el = soup.select_one("h1.title")
    ref_el = soup.select_one("#ref-number p")
    date_el = soup.select_one(".ref-date .date")
    return {
        "title": normalize_de_text(title_el.get_text(" ", strip=True) if title_el else ""),
        "file_number": normalize_de_text(ref_el.get_text(" ", strip=True) if ref_el else ""),
        "announcement_date": parse_merger_date(
            date_el.get_text(strip=True) if date_el else ""
        ),
        "detail_content": extract_detail_content_de(soup),
    }


def parse_listing_table(html_content: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Parse all merger rows from the year listing page HTML."""
    soup = BeautifulSoup(html_content, "html.parser")
    rows_out: List[Dict[str, Any]] = []
    source_year = year if year is not None else current_year()

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = [
            th.get_text(" ", strip=True).lower()
            for th in header_row.find_all(["th", "td"])
        ]
        if not any("aktenzahl" in h for h in headers):
            continue

        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue

            file_number = normalize_de_text(cells[0].get_text(" ", strip=True))
            if not file_number.startswith("BWB/"):
                continue

            link = cells[1].find("a", href=True)
            parties = normalize_de_text(
                link.get_text(" ", strip=True)
                if link
                else cells[1].get_text(" ", strip=True)
            )
            detail_url = (
                urljoin(BASE_URL, link["href"].strip()) if link else ""
            )
            merger_date = parse_merger_date(cells[2].get_text(strip=True))
            status = normalize_de_text(cells[3].get_text(" ", strip=True))

            if not detail_url:
                logger.warning("Skipping %s — no detail URL", file_number)
                continue

            rows_out.append(
                {
                    "file_number": file_number,
                    "parties": parties,
                    "merger_date": merger_date,
                    "status": status,
                    "detail_url": detail_url,
                    "source_year": source_year,
                    "is_open": determine_is_open(status),
                }
            )

    logger.info("Parsed %d listing rows for year %s", len(rows_out), source_year)
    return rows_out


def build_listing_lookup(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        fn = (row.get("file_number") or "").strip()
        if fn:
            lookup[fn] = row
    return lookup


@contextmanager
def playwright_page(headless: Optional[bool] = None):
    from playwright.sync_api import Page, sync_playwright

    if headless is None:
        headless = os.getenv("BWB_HEADLESS", "true").lower() in (
            "true",
            "1",
            "yes",
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="de-AT",
        )
        page = context.new_page()
        try:
            yield page
        finally:
            browser.close()


def _dismiss_cookie_consent_if_present(page, wait_ms: int = 500) -> bool:
    """Dismiss eRecht24 / Usercentrics cookie banner if it blocks the page."""
    accept_labels = (
        "Alles akzeptieren",
        "Accept all",
        "Alle akzeptieren",
    )
    reject_labels = (
        "Ablehnen",
        "Reject",
        "Alle ablehnen",
    )

    def _try_click(locator, label: str) -> bool:
        try:
            if locator.count() > 0 and locator.first.is_visible(timeout=3000):
                locator.first.click(timeout=8000)
                logger.info("Dismissed cookie consent via: %s", label)
                page.wait_for_timeout(wait_ms)
                return True
        except Exception as e:
            logger.debug("Cookie consent click failed (%s): %s", label, e)
        return False

    for text in accept_labels:
        if _try_click(page.get_by_role("button", name=text, exact=True), text):
            return True
        if _try_click(page.locator(f'button:has-text("{text}")'), text):
            return True

    for text in reject_labels:
        if _try_click(page.get_by_role("button", name=text, exact=True), text):
            return True
        if _try_click(page.locator(f'button:has-text("{text}")'), text):
            return True

    for selector in (
        "#uc-btn-accept-banner",
        '[data-testid="uc-accept-all-button"]',
        "button.uc-btn-accept",
        "#cookie-accept",
    ):
        if _try_click(page.locator(selector), selector):
            return True

    try:
        clicked = page.evaluate(
            """() => {
                const texts = ['alles akzeptieren', 'accept all', 'alle akzeptieren', 'ablehnen'];
                const buttons = Array.from(document.querySelectorAll('button, a[role="button"]'));
                for (const text of texts) {
                    const btn = buttons.find((el) =>
                        (el.textContent || '').trim().toLowerCase().includes(text)
                    );
                    if (btn) {
                        btn.click();
                        return true;
                    }
                }
                const byId = document.querySelector('#uc-btn-accept-banner');
                if (byId) {
                    byId.click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            logger.info("Dismissed cookie consent via JS fallback")
            page.wait_for_timeout(wait_ms)
            return True
    except Exception as e:
        logger.debug("Cookie consent JS fallback failed: %s", e)

    logger.debug("No cookie consent banner found (or already dismissed)")
    return False


def _reveal_all_listing_rows(page, wait_ms: int = 1500) -> None:
    """Click month filter 'Alle' and expand DataTables so all year rows are in the DOM."""
    _dismiss_cookie_consent_if_present(page)
    page.wait_for_selector("table", timeout=60000)

    alle_clicked = False
    for locator in (
        page.get_by_role("link", name="Alle", exact=True),
        page.get_by_role("button", name="Alle", exact=True),
        page.locator("a, button", has_text=re.compile(r"^Alle$")),
        page.locator("a", has_text=re.compile(r"^Alle$")),
    ):
        try:
            if locator.count() > 0:
                locator.first.click(timeout=10000)
                alle_clicked = True
                logger.info("Clicked month filter 'Alle'")
                break
        except Exception as e:
            logger.debug("Could not click Alle via %s: %s", locator, e)

    if not alle_clicked:
        logger.warning("Month filter 'Alle' not found; listing may be partial")

    page.wait_for_timeout(wait_ms)

    # DataTables page length — show all entries (value -1 or label "Alle"/"All").
    length_select = page.locator(".dataTables_length select")
    if length_select.count() > 0:
        select = length_select.first
        expanded = False
        for value, label in (("-1", None), (None, "Alle"), (None, "All")):
            try:
                if value is not None:
                    select.select_option(value=value, timeout=5000)
                else:
                    select.select_option(label=label, timeout=5000)
                expanded = True
                logger.info("DataTables length set to show all entries")
                break
            except Exception:
                continue
        if not expanded:
            try:
                last_value = select.evaluate(
                    "el => el.options[el.options.length - 1]?.value"
                )
                if last_value:
                    select.select_option(value=last_value, timeout=5000)
                    expanded = True
                    logger.info("DataTables length set to last option (%s)", last_value)
            except Exception as e:
                logger.debug("Could not expand DataTables length menu: %s", e)
        if expanded:
            page.wait_for_timeout(wait_ms)
    else:
        # Fallback: call DataTables API if jQuery plugin is present.
        try:
            page.evaluate(
                """() => {
                    if (!window.jQuery || !jQuery.fn.dataTable) return;
                    jQuery('table').each(function () {
                        if (jQuery.fn.dataTable.isDataTable(this)) {
                            jQuery(this).DataTable().page.len(-1).draw();
                        }
                    });
                }"""
            )
            page.wait_for_timeout(wait_ms)
            logger.info("Expanded listing via DataTables JS API")
        except Exception as e:
            logger.debug("DataTables JS expand skipped: %s", e)

    try:
        row_count = page.locator("table tbody tr").filter(
            has=page.locator("td")
        ).count()
        logger.info("Listing table row count after expand: %d", row_count)
    except Exception:
        pass


def fetch_page_html(
    page,
    url: str,
    wait_ms: int = 2000,
    timeout_ms: int = 60000,
) -> str:
    logger.info("Fetching %s", url)
    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    _dismiss_cookie_consent_if_present(page)
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    return page.content()


def fetch_listing_page_html(
    page,
    url: str,
    wait_ms: int = 2000,
    timeout_ms: int = 60000,
) -> str:
    """Fetch year listing with all months visible (clicks 'Alle' + expands DataTables)."""
    logger.info("Fetching listing (all months): %s", url)
    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    _reveal_all_listing_rows(page, wait_ms=max(wait_ms, 1000))
    return page.content()


def get_bwb_cases_collection():
    from mongodb_connection import get_database

    db = get_database()
    if db is None:
        return None
    return db[COLLECTION_NAME]


_indexes_ensured = False


def ensure_bwb_cases_indexes(collection) -> None:
    """Ensure unique index on file_number (idempotent)."""
    global _indexes_ensured
    if _indexes_ensured or collection is None:
        return
    collection.create_index(
        "file_number",
        unique=True,
        name="file_number_unique",
    )
    _indexes_ensured = True
    logger.info("Ensured unique index on %s.file_number", COLLECTION_NAME)
