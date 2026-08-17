"""
Shared helpers for CCI India combination scrapers.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from email_subject_builder import build_subject
from n8n_email_service import post_email_payload
from llm_verification_service import verify_usa_relation
from mongodb_connection import get_database, get_deal_by_id, get_deals_collection


# python under_review_scraper.py              # live
# python under_review_scraper.py --dry-run    # scrape only, no DB/email
# python under_review_scraper.py --headed     # visible browser
# python orders_section31_scraper.py
# python orders_section43a_44_scraper.py
# python orders_approved_with_modification_scraper.py

load_dotenv(".env")

logger = logging.getLogger("cci_common")


def attach_cci_common_logging(script_logger: logging.Logger) -> None:
    """
    Send cci_common log records to the same file/console handlers as the scraper.

    Scrapers configure only their own logger (e.g. cci_section43a_44); without this,
    LLM/deal-match logs on logging.getLogger("cci_common") are dropped.
    """
    common = logging.getLogger("cci_common")
    common.setLevel(script_logger.level)
    for handler in list(common.handlers):
        common.removeHandler(handler)
    for handler in script_logger.handlers:
        common.addHandler(handler)
    common.propagate = False


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=120.0)

CCI_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]
CCI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CCI_LAUNCH_TIMEOUT_MS = 90_000
CCI_DEFAULT_TIMEOUT_MS = 60_000
CCI_MAX_LIST_PAGES = int(os.getenv("CCI_MAX_LIST_PAGES", "200"))


def launch_cci_browser(playwright, headed: bool = False):
    """Launch Chromium with bounded timeouts for CCI scrapers."""
    browser = playwright.chromium.launch(
        headless=not headed,
        timeout=CCI_LAUNCH_TIMEOUT_MS,
        args=CCI_CHROMIUM_ARGS,
    )
    context = browser.new_context(user_agent=CCI_USER_AGENT)
    page = context.new_page()
    page.set_default_timeout(CCI_DEFAULT_TIMEOUT_MS)
    page.set_default_navigation_timeout(CCI_DEFAULT_TIMEOUT_MS)
    return browser, context, page


def close_cci_browser(browser) -> None:
    if browser is None:
        return
    try:
        browser.close()
    except Exception:
        logger.warning("CCI browser.close() failed", exc_info=True)

MATCH_TEXT_LOG_MAX = 300


def _log_snippet(text: str, max_len: int = MATCH_TEXT_LOG_MAX) -> str:
    t = (text or "").strip().replace("\n", " ")
    if not t:
        return "(empty)"
    if len(t) <= max_len:
        return t
    return t[:max_len] + "…"


def log_cci_db_lookup(
    reg_no: str,
    existing: Optional[Dict[str, Any]],
    source_key: str,
) -> None:
    """Log MongoDB lookup result before deciding insert/skip."""
    if not existing:
        logger.info(
            "  DB lookup: no record for %s (will insert if processed)", reg_no)
        return
    source_pages = existing.get("source_pages") or {}
    source_seen = (existing.get("source_seen_at") or {}).get(source_key) or {}
    logger.info(
        "  DB lookup: record exists | deal_id=%s | source_pages.%s=%s | "
        "first_seen_at=%s | last_seen_at=%s",
        existing.get("deal_id") or "(none)",
        source_key,
        source_pages.get(source_key, False),
        source_seen.get("first_seen_at") or "(none)",
        source_seen.get("last_seen_at") or "(none)",
    )


BASE_URL = os.getenv("BASE_URL", "")
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_INTERNAL",
    f"{BASE_URL}/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

COLLECTION_NAME = "cci_cases"

SOURCE_NOTICE_UNDER_REVIEW = "notice_under_review"
SOURCE_SECTION31 = "section31_order"
SOURCE_SECTION43A_44 = "section43a_44_order"
SOURCE_APPROVED_MOD = "approved_with_modification"

DETAIL_URL_TEMPLATES: Dict[str, str] = {
    SOURCE_NOTICE_UNDER_REVIEW: (
        "https://www.cci.gov.in/combination/order/details/summary/{id}/0/notice-under-review"
    ),
    SOURCE_SECTION31: (
        "https://www.cci.gov.in/combination/order/details/summary/{id}/0/orders-section31"
    ),
    SOURCE_SECTION43A_44: (
        "https://www.cci.gov.in/combination/order/details/order/{id}/0/orders-section43a_44"
    ),
    SOURCE_APPROVED_MOD: (
        "https://www.cci.gov.in/combination/order/details/order/{id}/0/cases-approved-with-modification"
    ),
}

# CCI public list pages (for emails — user opens the table being scraped)
CCI_LIST_PAGE_URLS: Dict[str, str] = {
    SOURCE_NOTICE_UNDER_REVIEW: "https://www.cci.gov.in/combination/notice-under-review",
    SOURCE_SECTION31: "https://www.cci.gov.in/combination/orders-section31",
    SOURCE_SECTION43A_44: "https://www.cci.gov.in/combination/orders-section43a_44",
    SOURCE_APPROVED_MOD: (
        "https://www.cci.gov.in/combination/cases-approved-with-modification"
    ),
}

# These pipelines have no meaningful cci_status on the list page — do not set stage
SOURCES_WITHOUT_STAGE = frozenset({SOURCE_SECTION43A_44, SOURCE_APPROVED_MOD})

STAGE_MAP: Dict[str, str] = {
    "Under Review": "UNDER_REVIEW",
    "Under Review/Information incomplete & RFI issued": "UNDER_REVIEW",
    "Approved": "APPROVED",
    "Deemed Approved (Section 6(5))": "DEEMED_APPROVED",
    "Deemed approved (Regulation 5A)": "DEEMED_APPROVED_REG5A",
    "Approved with modification": "APPROVED_WITH_MODIFICATION",
    "No AAEC": "NO_AAEC",
    "Notice Not Valid": "NOTICE_NOT_VALID",
    "Transaction called off": "TRANSACTION_CALLED_OFF",
    "Notice withdrawn": "NOTICE_WITHDRAWN",
    "Exempt": "EXEMPT",
}

VIEW_PDF_RE = re.compile(r"viewPdf\('([^']+\.pdf)'\)", re.IGNORECASE)
DT_ROW_RE = re.compile(r"^dt_row_(\d+)$")


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_date_dmy(date_str: str) -> Optional[str]:
    """Convert DD/MM/YYYY to YYYY-MM-DD."""
    if not date_str:
        return None
    text = date_str.strip()
    try:
        dt = datetime.strptime(text, "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_date_dmy_to_date(date_str: str) -> Optional[date]:
    iso = parse_date_dmy(date_str)
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_stage(cci_status: str) -> str:
    normalized = (cci_status or "").strip()
    return STAGE_MAP.get(normalized, normalized.upper().replace(" ", "_") if normalized else "UNKNOWN")


def get_cci_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db[COLLECTION_NAME]


def ensure_cci_indexes(collection) -> None:
    try:
        collection.create_index(
            "combination_registration_no",
            unique=True,
            name="combination_registration_no_unique",
        )
    except Exception as exc:
        logger.warning("Could not ensure cci_cases index: %s", exc)


def empty_detail_urls() -> Dict[str, None]:
    return {
        SOURCE_NOTICE_UNDER_REVIEW: None,
        SOURCE_SECTION31: None,
        SOURCE_SECTION43A_44: None,
        SOURCE_APPROVED_MOD: None,
    }


def empty_source_pages() -> Dict[str, bool]:
    return {
        SOURCE_NOTICE_UNDER_REVIEW: False,
        SOURCE_SECTION31: False,
        SOURCE_SECTION43A_44: False,
        SOURCE_APPROVED_MOD: False,
    }


def empty_source_seen_at() -> Dict[str, Dict[str, Optional[str]]]:
    blank = {"first_seen_at": None, "last_seen_at": None}
    return {
        SOURCE_NOTICE_UNDER_REVIEW: dict(blank),
        SOURCE_SECTION31: dict(blank),
        SOURCE_SECTION43A_44: dict(blank),
        SOURCE_APPROVED_MOD: dict(blank),
    }


def build_detail_url(internal_id: int, source_key: str) -> str:
    template = DETAIL_URL_TEMPLATES[source_key]
    return template.format(id=internal_id)


def extract_internal_id_from_row_id(row_id: str) -> Optional[int]:
    match = DT_ROW_RE.match((row_id or "").strip())
    if not match:
        return None
    return int(match.group(1))


def extract_pdf_urls_from_html(html: str) -> List[str]:
    return VIEW_PDF_RE.findall(html or "")


def extract_summary_pdf_url(html: str) -> Optional[str]:
    urls = extract_pdf_urls_from_html(html)
    return urls[0] if urls else None


def source_already_processed(existing: Optional[Dict[str, Any]], source_key: str) -> bool:
    if not existing:
        return False
    return bool((existing.get("source_pages") or {}).get(source_key))


def _apply_first_seen(
    fields: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
    source_key: str,
    now_iso: str,
) -> None:
    if not existing or not (
        (existing.get("source_seen_at") or {}).get(
            source_key, {}).get("first_seen_at")
    ):
        fields[f"source_seen_at.{source_key}.first_seen_at"] = now_iso


def build_skeleton_doc(
    row: Dict[str, Any],
    source_key: str,
    detail_url: str,
    notice_pdf_url: Optional[str],
    now_iso: str,
) -> Dict[str, Any]:
    """Full document for first insert from under_review scraper."""
    return build_skeleton_doc_for_source(
        row,
        source_key,
        detail_url,
        now_iso,
        notice_under_review_url=notice_pdf_url,
    )


def build_skeleton_doc_for_source(
    row: Dict[str, Any],
    source_key: str,
    detail_url: str,
    now_iso: str,
    *,
    notice_under_review_url: Optional[str] = None,
    section31_summary_url: Optional[str] = None,
    section31_order_url: Optional[str] = None,
    section43a_44_order_url: Optional[str] = None,
    approved_with_modification_url: Optional[str] = None,
) -> Dict[str, Any]:
    reg_no = row["combination_registration_no"]
    cci_status = (row.get("cci_status") or "").strip()

    detail_urls = empty_detail_urls()
    detail_urls[source_key] = detail_url

    source_seen = empty_source_seen_at()
    source_seen[source_key] = {
        "first_seen_at": now_iso, "last_seen_at": now_iso}

    source_pages = empty_source_pages()
    source_pages[source_key] = True

    if source_key in SOURCES_WITHOUT_STAGE:
        stage = None
        status_for_doc = (
            "Approved with modification" if source_key == SOURCE_APPROVED_MOD else None
        )
    else:
        status_for_doc = cci_status or None
        stage = get_stage(cci_status) if cci_status else None

    return {
        "combination_registration_no": reg_no,
        "detail_urls": detail_urls,
        "notifying_parties": row.get("notifying_parties"),
        "description": row.get("description"),
        "form": row.get("form"),
        "date_of_notification": row.get("date_of_notification"),
        "stage": stage,
        "cci_status": status_for_doc,
        "decision_date": row.get("decision_date"),
        "under_section": row.get("under_section"),
        "notice_under_review_url": notice_under_review_url,
        "section31_summary_url": section31_summary_url,
        "section31_order_url": section31_order_url,
        "section43a_44_order_url": section43a_44_order_url,
        "approved_with_modification_url": approved_with_modification_url,
        "source_pages": source_pages,
        "source_seen_at": source_seen,
        "deal_id": None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def build_under_review_update_fields(
    row: Dict[str, Any],
    detail_url: str,
    notice_pdf_url: Optional[str],
    now_iso: str,
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    cci_status = (row.get("cci_status") or "").strip()
    fields: Dict[str, Any] = {
        "detail_urls.notice_under_review": detail_url,
        "notice_under_review_url": notice_pdf_url,
        "notifying_parties": row.get("notifying_parties"),
        "form": row.get("form"),
        "date_of_notification": row.get("date_of_notification"),
        "cci_status": cci_status,
        "stage": get_stage(cci_status),
        "source_pages.notice_under_review": True,
        "source_seen_at.notice_under_review.last_seen_at": now_iso,
        "updated_at": now_iso,
    }
    if not existing or not (
        existing.get("source_seen_at", {})
        .get(SOURCE_NOTICE_UNDER_REVIEW, {})
        .get("first_seen_at")
    ):
        fields["source_seen_at.notice_under_review.first_seen_at"] = now_iso
    return fields


def update_last_seen_at(
    collection,
    reg_no: str,
    source_key: str,
    now_iso: str,
) -> None:
    """Update last_seen_at whenever the case appears on a source list page."""
    if not reg_no:
        return
    collection.update_one(
        {"combination_registration_no": reg_no},
        {"$set": {f"source_seen_at.{source_key}.last_seen_at": now_iso}},
    )


def flatten_mongo_set(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Already uses dot notation for nested keys."""
    return fields


def parse_under_review_table(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    for tr in soup.select("#datatable_ajax tbody tr[id^='dt_row_']"):
        row_id = tr.get("id", "")
        internal_id = extract_internal_id_from_row_id(row_id)
        if internal_id is None:
            continue

        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        reg_no = tds[1].get_text(" ", strip=True)
        if not reg_no:
            continue

        parties = tds[2].get_text(" ", strip=True)
        form_val = tds[3].get_text(strip=True)
        date_raw = tds[4].get_text(strip=True)
        status = tds[5].get_text(" ", strip=True)
        date_iso = parse_date_dmy(date_raw)

        list_detail_link = None
        link = tds[6].find("a", href=True) if len(tds) > 6 else None
        if link:
            list_detail_link = link["href"].strip()

        rows.append(
            {
                "combination_registration_no": reg_no,
                "cci_internal_id": internal_id,
                "notifying_parties": parties,
                "form": form_val,
                "date_of_notification": date_iso,
                "date_of_notification_raw": date_raw,
                "cci_status": status,
                "list_detail_link": list_detail_link,
                "detail_url": build_detail_url(internal_id, SOURCE_NOTICE_UNDER_REVIEW),
            }
        )

    return rows


def row_within_cutoff(
    row: Dict[str, Any],
    cutoff: date,
    field: str = "date_of_notification",
) -> bool:
    val = row.get(field)
    if not val:
        return True
    try:
        row_date = datetime.strptime(val, "%Y-%m-%d").date()
        return row_date >= cutoff
    except ValueError:
        return True


def page_all_older_than_cutoff(
    rows: List[Dict[str, Any]],
    cutoff: date,
    field: str = "date_of_notification",
) -> bool:
    dated = [r for r in rows if r.get(field)]
    if not dated:
        return False
    for row in dated:
        if row_within_cutoff(row, cutoff, field=field):
            return False
    return True


def goto_with_retries(
    page,
    url: str,
    *,
    max_retries: int = 3,
    timeout_ms: int = 60000,
    settle_ms: int = 3000,
) -> None:
    """Navigate to url; retry on Playwright timeouts (CCI is often slow)."""
    last_err: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Navigating (attempt %s/%s): %s", attempt, max_retries, url
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            return
        except PlaywrightTimeoutError as exc:
            last_err = exc
            logger.warning(
                "Navigation timed out (attempt %s/%s): %s",
                attempt,
                max_retries,
                url,
            )
            if attempt < max_retries:
                page.wait_for_timeout(5000)
    assert last_err is not None
    raise last_err


def wait_for_datatable_ready(
    page, timeout_ms: int = 30000, max_retries: int = 3
) -> None:
    """Wait for CCI DataTables rows; retry on timeout without reloading."""
    last_err: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            page.wait_for_selector(
                "#datatable_ajax tbody tr", timeout=timeout_ms
            )
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('#datatable_ajax_processing');
                        if (!el) return true;
                        const style = window.getComputedStyle(el);
                        return style.display === 'none' || el.style.display === 'none';
                    }""",
                    timeout=timeout_ms,
                )
            except Exception:
                page.wait_for_timeout(1000)
            return
        except PlaywrightTimeoutError as exc:
            last_err = exc
            logger.warning(
                "Datatable not ready (attempt %s/%s)", attempt, max_retries
            )
            if attempt < max_retries:
                page.wait_for_timeout(2000)
    assert last_err is not None
    raise last_err


def load_cci_list_page(
    page, list_url: str, *, max_retries: int = 3
) -> None:
    """Load list URL and wait for datatable; full re-navigation on failure."""
    last_err: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Loading list page (attempt %s/%s): %s",
                attempt,
                max_retries,
                list_url,
            )
            page.goto(
                list_url, wait_until="domcontentloaded", timeout=60000
            )
            page.wait_for_timeout(3000)
            wait_for_datatable_ready(page, max_retries=1)
            return
        except PlaywrightTimeoutError as exc:
            last_err = exc
            logger.warning(
                "List page load timed out (attempt %s/%s): %s",
                attempt,
                max_retries,
                list_url,
            )
            if attempt < max_retries:
                page.wait_for_timeout(5000)
    assert last_err is not None
    raise last_err


def paginate_cci_list(
    page,
    parse_fn,
    cutoff: Optional[date],
    cutoff_field: str,
    collection=None,
    source_key: str = "",
    single_page: bool = False,
    max_pages: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Collect list rows within cutoff; update last_seen_at for every row on each page."""
    collected: List[Dict[str, Any]] = []
    page_num = 1
    now_iso = utc_now_iso()
    prev_signature = None
    page_limit = CCI_MAX_LIST_PAGES if max_pages is None else max_pages

    while True:
        if page_num > page_limit:
            logger.warning(
                "Reached max list pages (%s); stopping pagination", page_limit
            )
            break

        wait_for_datatable_ready(page)
        html = page.content()
        rows = parse_fn(html)
        logger.info("List page %s: %s rows", page_num, len(rows))

        if not rows:
            break

        signature = tuple(
            (r.get("combination_registration_no") or "") for r in rows[:5]
        )
        if page_num > 1 and signature and signature == prev_signature:
            logger.warning(
                "List page %s unchanged after Next click; stopping pagination",
                page_num,
            )
            break
        prev_signature = signature

        for row in rows:
            reg_no = row.get("combination_registration_no")
            if collection is not None and reg_no and source_key:
                update_last_seen_at(collection, reg_no, source_key, now_iso)
            if cutoff is None or row_within_cutoff(row, cutoff, field=cutoff_field):
                collected.append(row)

        if cutoff is not None and page_all_older_than_cutoff(rows, cutoff, field=cutoff_field):
            logger.info(
                "All rows on page %s older than cutoff; stopping pagination", page_num
            )
            break

        if single_page:
            break

        next_li = page.locator("li#datatable_ajax_next")
        try:
            classes = (next_li.get_attribute("class", timeout=10000) or "").lower()
        except PlaywrightTimeoutError:
            logger.warning(
                "Next button not found on page %s; stopping pagination", page_num
            )
            break
        if "disabled" in classes:
            break

        try:
            next_li.locator("a.page-link").click(timeout=10000)
        except PlaywrightTimeoutError:
            logger.warning(
                "Next page click timed out on page %s; stopping pagination",
                page_num,
            )
            break
        page.wait_for_timeout(1500)
        page_num += 1

    return collected


def paginate_under_review_list(
    page,
    cutoff: date,
    collection=None,
    source_key: str = SOURCE_NOTICE_UNDER_REVIEW,
) -> List[Dict[str, Any]]:
    return paginate_cci_list(
        page,
        parse_under_review_table,
        cutoff,
        "date_of_notification",
        collection=collection,
        source_key=source_key,
    )


def parse_section31_table(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    for tr in soup.select("#datatable_ajax tbody tr[id^='dt_row_']"):
        internal_id = extract_internal_id_from_row_id(tr.get("id", ""))
        if internal_id is None:
            continue
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        reg_no = tds[1].get_text(" ", strip=True)
        if not reg_no:
            continue

        rows.append(
            {
                "combination_registration_no": reg_no,
                "cci_internal_id": internal_id,
                "notifying_parties": tds[2].get_text(" ", strip=True),
                "form": tds[3].get_text(strip=True),
                "date_of_notification": parse_date_dmy(tds[4].get_text(strip=True)),
                "cci_status": tds[5].get_text(" ", strip=True),
                "decision_date": parse_date_dmy(tds[6].get_text(strip=True)),
                "detail_url": build_detail_url(internal_id, SOURCE_SECTION31),
            }
        )
    return rows


def parse_section43a_44_table(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    for tr in soup.select("#datatable_ajax tbody tr[id^='dt_row_']"):
        internal_id = extract_internal_id_from_row_id(tr.get("id", ""))
        if internal_id is None:
            continue
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        reg_no = tds[1].get_text(" ", strip=True)
        if not reg_no:
            continue

        rows.append(
            {
                "combination_registration_no": reg_no,
                "cci_internal_id": internal_id,
                "description": tds[2].get_text(" ", strip=True),
                "under_section": tds[3].get_text(strip=True),
                "decision_date": parse_date_dmy(tds[4].get_text(strip=True)),
                "detail_url": build_detail_url(internal_id, SOURCE_SECTION43A_44),
            }
        )
    return rows


def parse_approved_with_modification_table(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    for tr in soup.select("#datatable_ajax tbody tr[id^='dt_row_']"):
        internal_id = extract_internal_id_from_row_id(tr.get("id", ""))
        if internal_id is None:
            continue
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        reg_no = tds[1].get_text(" ", strip=True)
        if not reg_no:
            continue

        rows.append(
            {
                "combination_registration_no": reg_no,
                "cci_internal_id": internal_id,
                "notifying_parties": tds[2].get_text(" ", strip=True),
                "detail_url": build_detail_url(internal_id, SOURCE_APPROVED_MOD),
            }
        )
    return rows


def fetch_detail_html(page, detail_url: str) -> str:
    logger.info("  Fetching detail: %s", detail_url)
    goto_with_retries(page, detail_url, settle_ms=2000)
    return page.content()


def fetch_detail_pdf_url(page, detail_url: str) -> Optional[str]:
    return extract_summary_pdf_url(fetch_detail_html(page, detail_url))


def extract_section31_pdfs(html: str) -> Tuple[Optional[str], Optional[str]]:
    urls = extract_pdf_urls_from_html(html)
    summary = urls[0] if len(urls) > 0 else None
    order = urls[1] if len(urls) > 1 else None
    return summary, order


def fetch_section31_detail_pdfs(page, detail_url: str) -> Tuple[Optional[str], Optional[str]]:
    html = fetch_detail_html(page, detail_url)
    return extract_section31_pdfs(html)


def fetch_order_pdf_url(page, detail_url: str) -> Optional[str]:
    html = fetch_detail_html(page, detail_url)
    urls = extract_pdf_urls_from_html(html)
    return urls[0] if urls else None


def build_section31_update_fields(
    row: Dict[str, Any],
    detail_url: str,
    summary_url: Optional[str],
    order_url: Optional[str],
    now_iso: str,
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    cci_status = (row.get("cci_status") or "").strip()
    fields: Dict[str, Any] = {
        "detail_urls.section31_order": detail_url,
        "section31_summary_url": summary_url,
        "section31_order_url": order_url,
        "cci_status": cci_status,
        "stage": get_stage(cci_status),
        "date_of_notification": row.get("date_of_notification"),
        "decision_date": row.get("decision_date"),
        "source_pages.section31_order": True,
        "source_seen_at.section31_order.last_seen_at": now_iso,
        "updated_at": now_iso,
    }
    if row.get("notifying_parties"):
        if not existing or not existing.get("notifying_parties"):
            fields["notifying_parties"] = row["notifying_parties"]
    if row.get("form"):
        if not existing or not existing.get("form"):
            fields["form"] = row["form"]
    _apply_first_seen(fields, existing, SOURCE_SECTION31, now_iso)
    return fields


def build_section43a_44_update_fields(
    row: Dict[str, Any],
    detail_url: str,
    order_url: Optional[str],
    now_iso: str,
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "detail_urls.section43a_44_order": detail_url,
        "section43a_44_order_url": order_url,
        "description": row.get("description"),
        "under_section": row.get("under_section"),
        "decision_date": row.get("decision_date"),
        "source_pages.section43a_44_order": True,
        "source_seen_at.section43a_44_order.last_seen_at": now_iso,
        "updated_at": now_iso,
    }
    _apply_first_seen(fields, existing, SOURCE_SECTION43A_44, now_iso)
    return fields


def build_approved_mod_update_fields(
    row: Dict[str, Any],
    detail_url: str,
    order_url: Optional[str],
    now_iso: str,
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "detail_urls.approved_with_modification": detail_url,
        "approved_with_modification_url": order_url,
        "cci_status": "Approved with modification",
        "source_pages.approved_with_modification": True,
        "source_seen_at.approved_with_modification.last_seen_at": now_iso,
        "updated_at": now_iso,
    }
    if row.get("notifying_parties"):
        if not existing or not existing.get("notifying_parties"):
            fields["notifying_parties"] = row["notifying_parties"]
    _apply_first_seen(fields, existing, SOURCE_APPROVED_MOD, now_iso)
    return fields


def section31_cutoff_date() -> date:
    return (datetime.now() - timedelta(days=30)).date()


def section43a_44_cutoff_date() -> date:
    return (datetime.now() - timedelta(days=15)).date()


def match_case_to_deal(match_text: str, reg_no: str = "") -> Optional[str]:
    """LLM deal match on notifying parties or description."""
    prefix = f"  [{reg_no}] " if reg_no else "  "
    text = (match_text or "").strip()
    if not text:
        logger.info(
            "%sLLM match skipped: empty notifying_parties/description", prefix)
        return None

    logger.info(
        "%sLLM match input (%s chars): %s",
        prefix,
        len(text),
        _log_snippet(text),
    )

    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            logger.warning(
                "%sDeals collection unavailable; cannot run LLM match", prefix)
            return None

        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        logger.info(
            "%sFetching deals from DB (Open/Unknown status filter)...", prefix)
        deals = list(deals_collection.find(status_filter))
        logger.info("%sLoaded %s deal(s) from DB for LLM matching",
                    prefix, len(deals))
        if not deals:
            logger.info("%sLLM match skipped: no deals in database", prefix)
            return None

        lines = []
        for d in deals:
            deal_id = str(d.get("_id"))
            target = d.get("target") or d.get("target_name", "N/A")
            acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
            line = f"Deal ID: {deal_id} | Target: {target} | Acquirer: {acquirer}"
            target_aliases = d.get("target_aliases") or []
            parent_aliases = d.get("parent_aliases") or []
            if target_aliases:
                line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
            if parent_aliases:
                line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
            lines.append(line)

        deals_text = "\n".join(lines)
        prompt = f"""You are an expert M&A deal matcher. Your task is to determine if ANY company mentioned in the CCI combination case text appears in our deals database.

DEALS DATABASE:
{deals_text}

CCI CASE TEXT TO MATCH (notifying parties / description):
{text}

MATCHING INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned in the CCI case text (acquirer(s), target(s), and notifying parties). Numbered lists (e.g. "1. Company A 2. Company B") count as explicit mentions.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the CCI text.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the CCI case text.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the CCI case text:
   - one match for the Acquirer side
   - one match for the Target side
5. Do not return a match if only one side is present, even if that single company is an exact match.
6. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - "Inc." vs "Incorporated"
   - "Corp." vs "Corporation"
   - "Ltd" vs "Limited"
   - obvious spacing/casing differences
7. Do not match based only on sector, business type, article topic, indirect association, or partial deal overlap.
8. If the CCI case text does not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
-If BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY: Match: DEAL_ID
-If no deal satisfies this rule, respond exactly: None
"""

        logger.info("%sCalling LLM for deal match (model=gpt-5.2)...", prefix)
        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert M&A deal identifier and matcher.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = (res.choices[0].message.content or "").strip()
        logger.info("%sLLM raw response: %s", prefix,
                    _log_snippet(content, max_len=200))
        if not content.lower().startswith("match"):
            logger.info("%sLLM result: no deal match", prefix)
            return None
        try:
            _prefix, deal_id_raw = content.split(":", 1)
            deal_id = deal_id_raw.strip()
            if deal_id:
                logger.info("%sLLM result: matched deal_id=%s",
                            prefix, deal_id)
                return deal_id
            logger.warning("%sLLM returned Match: but empty deal_id", prefix)
            return None
        except Exception as parse_exc:
            logger.warning(
                "%sLLM response parse failed: %s (content=%s)",
                prefix,
                parse_exc,
                _log_snippet(content, max_len=120),
            )
            return None
    except Exception as exc:
        logger.exception("%sLLM match error: %s", prefix, exc)
        raise


def _pdf_link_row(label: str, url: Optional[str]) -> str:
    if not url:
        return ""
    return (
        f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">{escape_html(label)}:</td>'
        f'<td style="padding:6px 0 6px 12px;font-size:14px;">'
        f'<a href="{escape_html(url)}" target="_blank" style="color:#0ea5e9;">View PDF</a></td></tr>'
    )


def _detail_link_row(label: str, url: Optional[str]) -> str:
    if not url:
        return ""
    return (
        f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">{escape_html(label)}:</td>'
        f'<td style="padding:6px 0 6px 12px;font-size:14px;">'
        f'<a href="{escape_html(url)}" target="_blank" style="color:#0ea5e9;">Open page</a></td></tr>'
    )


def _stage_for_email(record: Dict[str, Any]) -> Optional[str]:
    stage = record.get("stage")
    if not stage:
        return None
    text = str(stage).strip()
    if not text or text.upper() == "UNKNOWN":
        return None
    return text


def _email_field_row(label: str, value: Any) -> str:
    """Table row only when value is present (skip N/A placeholders)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return ""
    return (
        f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">{escape_html(label)}:</td>'
        f'<td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(text)}</td></tr>'
    )


def _build_changes_html(changes: Dict[str, Any]) -> str:
    """Render a 'What Changed' table for update emails (old → new, skips unchanged)."""
    old = changes.get("old") or {}
    new = changes.get("new") or {}

    CHANGE_LABELS = [
        ("cci_status", "Status"),
        ("stage", "Stage"),
        ("decision_date", "Decision Date"),
        ("notice_under_review_url", "Notice PDF URL"),
    ]

    rows_html = ""
    for field, label in CHANGE_LABELS:
        old_val = (old.get(field) or "").strip() if isinstance(
            old.get(field), str) else (old.get(field) or "")
        new_val = (new.get(field) or "").strip() if isinstance(
            new.get(field), str) else (new.get(field) or "")
        if str(old_val) == str(new_val):
            continue
        old_display = escape_html(
            str(old_val)) if old_val else '<span style="color:#94a3b8;">—</span>'
        new_display = escape_html(
            str(new_val)) if new_val else '<span style="color:#94a3b8;">—</span>'
        rows_html += (
            f'<tr>'
            f'<td style="padding:5px 8px 5px 0;font-size:13px;color:#334155;white-space:nowrap;">{escape_html(label)}</td>'
            f'<td style="padding:5px 12px 5px 8px;font-size:13px;color:#dc2626;">{old_display}</td>'
            f'<td style="padding:5px 0 5px 8px;font-size:13px;color:#16a34a;font-weight:600;">{new_display}</td>'
            f'</tr>'
        )

    if not rows_html:
        return ""

    return f"""
<div style="background:#f0fdf4;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #16a34a;">
  <strong style="font-size:14px;color:#15803d;">What Changed</strong>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr>
      <th style="text-align:left;color:#64748b;font-size:12px;padding:4px 8px 4px 0;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;">Field</th>
      <th style="text-align:left;color:#64748b;font-size:12px;padding:4px 12px 4px 8px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;">Previous</th>
      <th style="text-align:left;color:#64748b;font-size:12px;padding:4px 0 4px 8px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;">New</th>
    </tr>
    {rows_html}
  </table>
</div>"""


def build_cci_email_html(
    record: Dict[str, Any],
    deal_match: Optional[Dict[str, Any]],
    event_type: str,
    source_label: str,
    list_page_url: Optional[str] = None,
    changes: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    subject = build_subject("cci", event_type, deal_match)
    reg_no = record.get("combination_registration_no", "N/A")
    parties = record.get("notifying_parties") or record.get(
        "description") or "N/A"

    detail_urls = record.get("detail_urls") or {}
    notice_detail = detail_urls.get(SOURCE_NOTICE_UNDER_REVIEW)

    list_link_html = ""
    if list_page_url:
        list_link_html = (
            f'<p style="margin:12px 0 0;">'
            f'<a href="{escape_html(list_page_url)}" target="_blank" '
            f'style="color:#0ea5e9;font-size:14px;font-weight:600;">'
            f'View CCI listing page &rarr;</a></p>'
        )

    pipeline_banner = f"""
<div style="background:#f1f5f9;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #64748b;">
  <strong>Pipeline:</strong> {escape_html(source_label)}
  {list_link_html}
</div>"""

    if deal_match:
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")
        banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
  <strong>Matched Deal:</strong> {escape_html(str(target))} / {escape_html(str(acquirer))}<br>
  <strong>Deal ID:</strong> {escape_html(str(deal_id))}
</div>"""
    else:
        banner = """
<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
  <strong>USA-Related Case</strong> — No deal match found; case appears related to the United States.
</div>"""

    pdf_rows = "".join(
        [
            _pdf_link_row("Notice Under Review (Summary)",
                          record.get("notice_under_review_url")),
            _pdf_link_row("Section 31 Summary",
                          record.get("section31_summary_url")),
            _pdf_link_row("Section 31 Order",
                          record.get("section31_order_url")),
            _pdf_link_row("Section 43A/44 Order",
                          record.get("section43a_44_order_url")),
            _pdf_link_row("Approved with Modification Order",
                          record.get("approved_with_modification_url")),
        ]
    )
    detail_rows = "".join(
        [
            _detail_link_row("Notice Under Review page", notice_detail),
            _detail_link_row("Section 31 page",
                             detail_urls.get(SOURCE_SECTION31)),
            _detail_link_row("Section 43A/44 page",
                             detail_urls.get(SOURCE_SECTION43A_44)),
            _detail_link_row("Approved with Modification page",
                             detail_urls.get(SOURCE_APPROVED_MOD)),
        ]
    )

    changes_html = _build_changes_html(changes) if changes else ""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;">
  <h2 style="color:#333;margin-top:0;border-bottom:3px solid #2563eb;padding-bottom:12px;">{escape_html(subject)}</h2>
  {pipeline_banner}
  {banner}
  {changes_html}
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr><td style="padding:6px 0;color:#64748b;font-size:14px;">Combination Registration No.:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(str(reg_no))}</td></tr>
    <tr><td style="padding:6px 0;color:#64748b;font-size:14px;">Notifying Parties / Description:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(str(parties))}</td></tr>
    {_email_field_row("Form", record.get("form"))}
    {_email_field_row("Date of Notification", record.get("date_of_notification"))}
    {_email_field_row("Status", record.get("cci_status"))}
    {_email_field_row("Stage", _stage_for_email(record))}
    {_email_field_row("Decision Date", record.get("decision_date"))}
    {_email_field_row("Under Section", record.get("under_section"))}
  </table>
  <h3 style="color:#334155;font-size:16px;">Documents</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">{pdf_rows}</table>
  <h3 style="color:#334155;font-size:16px;">CCI Pages</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">{detail_rows}</table>
  <p style="color:#999;font-size:12px;">Automated email from CCI India scraper.</p>
</div></body></html>"""
    return subject, html


def send_cci_email(
    record: Dict[str, Any],
    deal_match: Optional[Dict[str, Any]],
    event_type: str,
    source_label: str = "Notice Under Review",
    list_page_url: Optional[str] = None,
    source_key: Optional[str] = None,
    changes: Optional[Dict[str, Any]] = None,
) -> bool:
    reg_no = record.get("combination_registration_no", "")
    tag = "[FRMD]" if deal_match else "[FRUD]"
    if deal_match:
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        logger.info(
            "  Email: sending %s %s | reg_no=%s | deal_id=%s | %s / %s",
            tag,
            event_type,
            reg_no,
            deal_match.get("deal_id", "N/A"),
            target,
            acquirer,
        )
    else:
        logger.info(
            "  Email: sending %s %s | reg_no=%s | USA-related, no deal match",
            tag,
            event_type,
            reg_no,
        )

    if not list_page_url and source_key:
        list_page_url = CCI_LIST_PAGE_URLS.get(source_key)

    subject, html = build_cci_email_html(
        record, deal_match, event_type, source_label,
        list_page_url=list_page_url, changes=changes,
    )
    detail_urls = record.get("detail_urls") or {}
    detail_for_source = detail_urls.get(source_key) if source_key else None
    payload: Dict[str, Any] = {
        "subject": subject,
        "html": html,
        "combination_registration_no": reg_no,
        "source": "cci_india",
        "cci_source_page": source_label,
        "cci_pipeline": source_label,
        "is_new_case": event_type == "new",
        "case_url": list_page_url or detail_for_source or detail_urls.get(
            SOURCE_NOTICE_UNDER_REVIEW
        ),
        "list_page_url": list_page_url,
    }
    if deal_match and deal_match.get("deal_id"):
        payload["deal_id"] = deal_match["deal_id"]
    else:
        payload["deal_id"] = record.get("deal_id")
        payload["is_unmatched"] = True

    return post_email_payload(payload, subject=subject, default_url=N8N_WEBHOOK_URL)


def process_deal_match_and_email(
    collection,
    record: Dict[str, Any],
    is_new_record: bool,
    error_items: List[Dict[str, Any]],
    source_label: str = "Notice Under Review",
    list_page_url: Optional[str] = None,
    source_key: Optional[str] = None,
    changes: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Deal match / USA check / email per plan. Skips LLM if deal_id already set.

    changes: optional {"old": {...}, "new": {...}} dict built by the scraper when
             a status-change update is detected; forwarded into the email HTML.

    Returns True if an email was sent (webhook succeeded), False otherwise.
    """
    reg_no = record.get("combination_registration_no", "")
    event_type = "new" if is_new_record else "update"

    logger.info(
        "  Post-DB: starting deal match / email | reg_no=%s | event_type=%s | "
        "source=%s | is_new_record=%s",
        reg_no,
        event_type,
        source_label,
        is_new_record,
    )

    existing_deal_id = record.get("deal_id")
    if existing_deal_id:
        logger.info(
            "  deal_id already set (%s); skipping LLM and USA checks",
            existing_deal_id,
        )
        deal_match = get_deal_by_id(str(existing_deal_id))
        if not deal_match:
            logger.warning(
                "  deal_id=%s in cci_cases but deal not found in deals collection",
                existing_deal_id,
            )
        return send_cci_email(
            record,
            deal_match,
            event_type,
            source_label,
            list_page_url=list_page_url,
            source_key=source_key,
            changes=changes,
        )

    match_text = record.get(
        "notifying_parties") or record.get("description") or ""
    if not match_text.strip():
        logger.info(
            "  No notifying_parties or description on record; skipping LLM and USA checks"
        )
        logger.info("  Email not sent: no match text available")
        return False

    matched_deal_id: Optional[str] = None
    try:
        matched_deal_id = match_case_to_deal(match_text, reg_no=reg_no)
    except Exception as exc:
        from scraper_error_utils import collect_error

        logger.error("  LLM match failed for %s: %s", reg_no, exc)
        collect_error(
            error_items,
            str(exc),
            step="match_case_to_deal",
            context={"combination_registration_no": reg_no},
        )
        return False

    if matched_deal_id:
        logger.info("  Saving deal_id=%s on cci_cases record %s",
                    matched_deal_id, reg_no)
        collection.update_one(
            {"combination_registration_no": reg_no},
            {"$set": {"deal_id": matched_deal_id, "updated_at": utc_now_iso()}},
        )
        record = collection.find_one(
            {"combination_registration_no": reg_no}) or record
        record["deal_id"] = matched_deal_id
        deal_match = get_deal_by_id(matched_deal_id)
        if not deal_match:
            logger.warning(
                "  LLM matched deal_id=%s but get_deal_by_id returned None",
                matched_deal_id,
            )
        return send_cci_email(
            record,
            deal_match,
            event_type,
            source_label,
            list_page_url=list_page_url,
            source_key=source_key,
            changes=changes,
        )

    logger.info("  No deal match; running USA relation check (case_type=CCI)...")
    try:
        is_usa = bool(
            verify_usa_relation(company_details=match_text, case_type="CCI")
        )
    except Exception as exc:
        from scraper_error_utils import collect_error

        logger.error("  USA relation check failed for %s: %s", reg_no, exc)
        collect_error(
            error_items,
            str(exc),
            step="verify_usa_relation",
            context={"combination_registration_no": reg_no},
        )
        return False

    logger.info("  USA relation check result: %s", is_usa)
    if is_usa:
        return send_cci_email(
            record,
            None,
            event_type,
            source_label,
            list_page_url=list_page_url,
            source_key=source_key,
            changes=changes,
        )

    logger.info(
        "  Email not sent: no deal match and not USA-related (reg_no=%s)",
        reg_no,
    )
    return False


def under_review_cutoff_date() -> Optional[date]:
    """No cutoff — all notice-under-review rows are checked on every run."""
    return None
