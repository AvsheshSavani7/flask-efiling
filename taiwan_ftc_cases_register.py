"""
taiwan_ftc_cases_register.py
============================
Scraper for Taiwan FTC (公平交易委員會) merger consultation forum.

Source:
  https://www.ftc.gov.tw/internet/enterprise/forum/forumList.aspx?forum_web_place=1

Pipeline:
  1. Fetch listing pages (live=1, backfill=5) — ASP.NET pagination
  2. Dedup against taiwan_cases by forum_id (missing ⇒ new)
  3. Format period + poster locally; GPT-translate topic only
  4. LLM deal match on topic title → regex → USA → email / silent insert
  5. On LLM/regex match only: fetch detail page → LLM JSON summary → include in email
     (USA / unmatched: no detail fetch)

Usage:
  python taiwan_ftc_cases_register.py
  python taiwan_ftc_cases_register.py --backfill
  python taiwan_ftc_cases_register.py --dry-run --test-email
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import time
from datetime import timezone
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from deal_match_llm import fetch_open_deals, llm_match_deal_id
from deal_match_regex import apply_regex_match_subject, regex_match_taiwan_deal
from email_subject_builder import build_subject
from llm_verification_service import verify_usa_relation
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import (
    get_database,
    get_deal_by_id,
    init_mongodb_connection,
)
from n8n_email_service import post_email_payload, send_direct_email
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

SCRIPT_NAME = "taiwan_ftc_cases_register"
COLLECTION_NAME = "taiwan_cases"
BASE_URL = "https://www.ftc.gov.tw"
LIST_URL = (
    "https://www.ftc.gov.tw/internet/enterprise/forum/"
    "forumList.aspx?forum_web_place=1"
)
DETAIL_URL_TEMPLATE = (
    "https://www.ftc.gov.tw/internet/enterprise/forum/"
    "view.aspx?forum_id={forum_id}&forum_web_place=1"
)

MAX_PAGES_LIVE = 1
MAX_PAGES_BACKFILL = 2
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.5

TEST_RECIPIENT = "avshesh.savani@teqnodux.com"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
}

_http_session = requests.Session()
_http_session.headers.update(HTTP_HEADERS)

logger, _get_log_file = ensure_script_logger(SCRIPT_NAME)

TRANSLATE_MODEL = "gpt-5.2"
_openai_client: Optional[OpenAI] = None

FORUM_ID_RE = re.compile(r"forum_id=(\d+)", re.IGNORECASE)
PERIOD_RE = re.compile(
    r"(\d{4}/\d{1,2}/\d{1,2})\s*~\s*(\d{4}/\d{1,2}/\d{1,2})"
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return (
        datetime.datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def translate_topic_zh_to_en(
    text: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    forum_id: str = "",
) -> str:
    """Translate Traditional Chinese topic (主題) to English via OpenAI."""
    if not text or not isinstance(text, str) or not text.strip():
        return text
    if len(text) > 1500:
        logger.warning(
            "  Topic translation skipped: text too long (%d chars)", len(text))
        if error_items is not None:
            collect_error(
                error_items,
                f"Translation skipped: topic too long ({len(text)} chars)",
                step="translate",
                context={"forum_id": forum_id, "field": "topic"},
            )
        return text

    try:
        response = _get_openai_client().chat.completions.create(
            model=TRANSLATE_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a professional Traditional Chinese-to-English translator "
                        "for Taiwan FTC (Fair Trade Commission) merger-control filing titles.\n"
                        "Rules:\n"
                        "1. Return ONLY the translated English title.\n"
                        "2. Use well-known official English company names where possible.\n"
                        "3. Do NOT explain or add alternatives.\n"
                        "4. Preserve regulatory meaning (merger notification, combination, etc.)."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Translate this Taiwan FTC case topic to English:\n{text}"
                    ),
                },
            ],
        )
        translated = (response.choices[0].message.content or "").strip()
        if translated:
            logger.info(
                "  Translated topic OK (OpenAI %s): %.120s",
                TRANSLATE_MODEL, translated,
            )
            return translated
        logger.warning("  OpenAI topic translation empty — using original")
        if error_items is not None:
            collect_error(
                error_items,
                "OpenAI translation returned empty text (topic)",
                step="translate",
                context={"forum_id": forum_id, "field": "topic"},
            )
    except Exception as exc:
        logger.warning(
            "  OpenAI topic translation failed: %s — using original", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"OpenAI translation failed (topic): {exc}",
                step="translate",
                context={"forum_id": forum_id, "field": "topic"},
            )
    return text


_MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

POSTER_EN_MAP = {
    "公平交易委員會": "Fair Trade Commission",
}


def format_period_en(
    period_start: str = "",
    period_end: str = "",
    period_raw: str = "",
) -> str:
    """Format consultation period in English without an LLM call."""
    if period_start and period_end:
        try:
            y1, m1, d1 = (int(x) for x in period_start.split("-"))
            y2, m2, d2 = (int(x) for x in period_end.split("-"))
            if y1 == y2 and m1 == m2:
                return f"{_MONTH_NAMES[m1]} {d1}–{d2}, {y1}"
            if y1 == y2:
                return (
                    f"{_MONTH_NAMES[m1]} {d1} – {_MONTH_NAMES[m2]} {d2}, {y1}"
                )
            return (
                f"{_MONTH_NAMES[m1]} {d1}, {y1} – "
                f"{_MONTH_NAMES[m2]} {d2}, {y2}"
            )
        except (TypeError, ValueError, IndexError):
            pass
    return (period_raw or "").strip()


def format_poster_en(poster: str) -> str:
    """Map known Chinese poster names to English (no LLM)."""
    raw = (poster or "").strip()
    if not raw:
        return ""
    return POSTER_EN_MAP.get(raw, raw)


def parse_period(period_raw: str) -> Tuple[str, str, str]:
    """
    Normalize period text and parse start/end dates.

    Returns (period_raw_normalized, period_start YYYY-MM-DD or '', period_end).
    """
    cleaned = re.sub(r"\s+", " ", (period_raw or "").strip())
    cleaned = cleaned.replace("～", "~")
    m = PERIOD_RE.search(cleaned)
    if not m:
        return cleaned, "", ""

    def to_iso(raw: str) -> str:
        parts = raw.split("/")
        if len(parts) != 3:
            return raw
        y, mo, d = parts
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    start_raw, end_raw = m.group(1), m.group(2)
    normalized = f"{start_raw}~{end_raw}"
    return normalized, to_iso(start_raw), to_iso(end_raw)


def extract_forum_id(href: str) -> str:
    if not href:
        return ""
    m = FORUM_ID_RE.search(href)
    if m:
        return m.group(1)
    try:
        qs = parse_qs(urlparse(href).query)
        vals = qs.get("forum_id") or []
        return (vals[0] or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

def get_collection():
    db = get_database()
    if db is None:
        return None
    return db[COLLECTION_NAME]


def ensure_indexes(collection) -> None:
    try:
        collection.create_index(
            "forum_id", unique=True, name="forum_id_unique")
        logger.info("Indexes ensured on %s", COLLECTION_NAME)
    except Exception as exc:
        logger.warning("Could not ensure indexes: %s", exc)


def forum_id_exists(collection, forum_id: str) -> bool:
    if collection is None or not forum_id:
        return False
    return collection.find_one(
        {"forum_id": str(forum_id).strip()}, {"_id": 1}
    ) is not None


# ---------------------------------------------------------------------------
# HTTP + parsing (listing only)
# ---------------------------------------------------------------------------

def _extract_aspnet_fields(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    fields: Dict[str, str] = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", {"name": name})
        if el and el.get("value") is not None:
            fields[name] = el["value"]
    return fields


def fetch_list_page(
    page_num: int,
    aspnet_fields: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[str, Dict[str, str]]]:
    """
    Fetch a listing page. Page 1 = GET; pages 2+ = ASP.NET POST with ViewState.
    Returns (html, updated_aspnet_fields) or None.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if page_num <= 1:
                logger.info(
                    "  GET page %d (attempt %d/%d)", page_num, attempt, MAX_RETRIES)
                resp = _http_session.get(LIST_URL, timeout=REQUEST_TIMEOUT)
            else:
                if not aspnet_fields or not aspnet_fields.get("__VIEWSTATE"):
                    logger.error(
                        "  Missing ASP.NET ViewState for page %d", page_num)
                    return None
                data = {
                    "__EVENTTARGET": "ctl00$ContentPlaceHolder1$dl_toPage",
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": aspnet_fields.get("__VIEWSTATE", ""),
                    "__VIEWSTATEGENERATOR": aspnet_fields.get(
                        "__VIEWSTATEGENERATOR", ""),
                    "__EVENTVALIDATION": aspnet_fields.get(
                        "__EVENTVALIDATION", ""),
                    "ctl00$ContentPlaceHolder1$dl_pageSize": "10",
                    "ctl00$ContentPlaceHolder1$dl_toPage": str(page_num),
                }
                logger.info(
                    "  POST page %d (attempt %d/%d)", page_num, attempt, MAX_RETRIES)
                resp = _http_session.post(
                    LIST_URL, data=data, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
            return html, _extract_aspnet_fields(html)
        except Exception as exc:
            logger.warning(
                "  List fetch failed page=%d attempt=%d/%d: %s",
                page_num, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(5)
    return None


def parse_list_page(html: str) -> List[Dict[str, Any]]:
    """Extract listing rows — no detail-page fetch."""
    soup = BeautifulSoup(html, "html.parser")
    records: List[Dict[str, Any]] = []

    for li in soup.select("li.list_content"):
        title_a = li.select_one(".list_content_4 a[href]")
        if not title_a:
            continue
        href = (title_a.get("href") or "").strip()
        title = re.sub(r"\s+", " ", title_a.get_text(" ", strip=True)).strip()
        forum_id = extract_forum_id(href)
        if not forum_id or not title:
            continue

        period_el = li.select_one(".list_content_1")
        poster_el = li.select_one(".list_content_2")
        reply_el = li.select_one(".list_content_3")

        period_raw_src = period_el.get_text(
            " ", strip=True) if period_el else ""
        period_raw, period_start, period_end = parse_period(period_raw_src)
        poster = (
            re.sub(r"\s+", " ", poster_el.get_text(" ", strip=True)).strip()
            if poster_el else ""
        )
        reply_raw = (
            re.sub(r"\s+", " ", reply_el.get_text(" ", strip=True)).strip()
            if reply_el else "0"
        )
        try:
            reply_count = int(reply_raw)
        except ValueError:
            reply_count = 0

        detail_url = DETAIL_URL_TEMPLATE.format(forum_id=forum_id)
        if href and not href.startswith("http"):
            detail_url = urljoin(LIST_URL, href.split()[0])

        records.append({
            "forum_id": forum_id,
            "detail_url": detail_url,
            "title": title,
            "title_en": "",
            "period_raw": period_raw,
            "period_en": "",
            "period_start": period_start,
            "period_end": period_end,
            "poster": poster,
            "poster_en": "",
            "reply_count": reply_count,
        })

    return records


def scrape_all_pages(
    max_pages: int,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    all_records: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    aspnet_fields: Dict[str, str] = {}

    for page_num in range(1, max_pages + 1):
        logger.info("[Page %d] Fetching listing", page_num)
        result = fetch_list_page(page_num, aspnet_fields)
        if not result:
            logger.warning(
                "[Page %d] Fetch failed — stopping pagination", page_num)
            if error_items is not None:
                collect_error(
                    error_items,
                    f"Failed to fetch Taiwan FTC listing page {page_num}",
                    step="fetch_list_page",
                    context={"page": page_num, "url": LIST_URL},
                )
            break

        html, aspnet_fields = result
        page_records = parse_list_page(html)
        logger.info("[Page %d] Parsed %d row(s)", page_num, len(page_records))
        if not page_records:
            if error_items is not None:
                collect_error(
                    error_items,
                    f"No rows parsed on Taiwan FTC listing page {page_num}",
                    step="parse_list_page",
                    context={"page": page_num},
                )
            break

        for rec in page_records:
            fid = rec["forum_id"]
            if fid in seen_ids:
                continue
            seen_ids.add(fid)
            all_records.append(rec)

        time.sleep(REQUEST_DELAY)

    logger.info("Scraped %d unique listing record(s)", len(all_records))
    return all_records


# ---------------------------------------------------------------------------
# Deal match
# ---------------------------------------------------------------------------

def match_deal(
    title: str,
    title_en: str,
    period_en: str,
    open_deals: List[Dict[str, Any]],
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    forum_id: str = "",
) -> Tuple[Optional[str], Optional[str], bool]:
    """LLM → regex. Returns (deal_id, match_type, matched_by_regex)."""
    matched_deal_id: Optional[str] = None
    match_type: Optional[str] = None
    matched_by_regex = False

    try:
        matched_deal_id = llm_match_deal_id(
            regulator_name="Taiwan FTC",
            case_sections={
                "TITLE (original Traditional Chinese)": title,
                "TITLE (English translation)": title_en,
                "CONSULTATION PERIOD (English)": period_en or "",
            },
            source_label="the Taiwan FTC case title",
            deals=open_deals,
        )
        if matched_deal_id:
            match_type = "llm"
            logger.info("  Deal match LLM hit deal_id=%s", matched_deal_id)
        else:
            logger.info("  Deal match LLM: no match")
    except Exception as exc:
        logger.error("  LLM deal match error: %s", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"LLM deal match error: {exc}",
                step="llm_match_deal_id",
                context={"forum_id": forum_id, "title": title[:200]},
            )

    if not matched_deal_id:
        matched_deal_id = regex_match_taiwan_deal(
            title_en or title, open_deals)
        if matched_deal_id:
            match_type = "regex"
            matched_by_regex = True
            logger.info("  Deal match regex hit deal_id=%s", matched_deal_id)
        else:
            logger.info("  Deal match regex: no match")

    if matched_deal_id:
        deal = get_deal_by_id(matched_deal_id)
        if not deal:
            logger.warning(
                "  deal_id=%s not found in deals collection", matched_deal_id)
            if error_items is not None:
                collect_error(
                    error_items,
                    f"Matched deal_id={matched_deal_id} not found in deals collection",
                    step="get_deal_by_id",
                    context={
                        "forum_id": forum_id,
                        "title": title[:200],
                        "deal_id": matched_deal_id,
                    },
                )
            return None, None, False

    return matched_deal_id, match_type, matched_by_regex


# ---------------------------------------------------------------------------
# Detail page + matched-case summary (option A: match only)
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """You are given merger case details from the Taiwan Fair Trade Commission (公平交易委員會), usually in Traditional Chinese.

Extract the relevant information, translate it into professional English, and return ONLY valid JSON using exactly this structure:

{
"Case": "",
"Authority": "",
"Public consultation period": "",
"Industry": "",
"Parties involved": [],
"Proposed transaction": [],
"Regulatory basis": [],
"Public comments": [],
"Current status": ""
}

Rules:

* Do not add or infer facts not supported by the source.
* Ignore webpage elements such as Facebook, Twitter, Google+, print links, image links, navigation, and other irrelevant content.
* Convert Taiwan ROC years to Gregorian years (e.g. 115 = 2026).
* Use official English company names when clearly known from the provided text; otherwise preserve the original company name rather than inventing one.
* `Case`: concise English title of the merger/transaction.
* `Authority`: responsible regulatory authority, normally "Taiwan Fair Trade Commission (FTC)".
* `Public consultation period`: format naturally, e.g. "March 11–17, 2026".
* `Industry`: translate the stated 所屬行業.
* `Parties involved`: include all entities explicitly listed as 參與結合事業.
* `Proposed transaction`: summarize the transaction structure, ownership/share acquisition, control, merger mechanics, surviving/dissolved entities, or operational arrangements explicitly stated.
* `Regulatory basis`: include the applicable Fair Trade Act provisions, notification threshold/reason, Article 12 exemption status, and why notification was required.
* `Public comments`: summarize how comments may be submitted and how the FTC will consider them. Do not include postal addresses unless necessary.
* `Current status`: if the source states "此主題已結束討論" or equivalent, return "Public consultation closed".
* Do NOT interpret closure of public consultation as approval, clearance, rejection, or completion of the merger.
* If information is unavailable, use `null` for scalar fields and `[]` for array fields.
* Keep each point concise and avoid duplicates.
* Return ONLY the JSON object with no Markdown or additional explanation.

Case details:

{{CASE_DETAILS}}
"""

SUMMARY_SCALAR_KEYS = (
    "Case",
    "Authority",
    "Public consultation period",
    "Industry",
    "Current status",
)
SUMMARY_LIST_KEYS = (
    "Parties involved",
    "Proposed transaction",
    "Regulatory basis",
    "Public comments",
)


def fetch_detail_case_text(
    detail_url: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    forum_id: str = "",
) -> str:
    """Fetch detail page and return cleaned plain-text case details."""
    if not detail_url:
        return ""

    html = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(
                "  GET detail (attempt %d/%d): %s",
                attempt, MAX_RETRIES, detail_url,
            )
            resp = _http_session.get(detail_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
            break
        except Exception as exc:
            logger.warning(
                "  Detail fetch failed attempt=%d/%d: %s",
                attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(5)

    if not html:
        if error_items is not None:
            collect_error(
                error_items,
                f"Failed to fetch detail page: {detail_url}",
                step="fetch_detail",
                context={"forum_id": forum_id, "url": detail_url},
            )
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")
        topic = ""
        run_date = ""
        poster = ""
        content = ""
        status = ""

        el = soup.select_one("#ContentPlaceHolder1_forum_topic")
        if el:
            topic = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        el = soup.select_one("#ContentPlaceHolder1_run_date")
        if el:
            run_date = re.sub(
                r"\s+", " ", el.get_text(" ", strip=True)).strip()
        el = soup.select_one("#ContentPlaceHolder1_user_name")
        if el:
            poster = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        el = soup.select_one("#ContentPlaceHolder1_forum_content")
        if el:
            content = el.get_text("\n", strip=True).strip()
        el = soup.select_one("#ContentPlaceHolder1_isForumPost")
        if el:
            status = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()

        parts = [
            f"Topic: {topic}" if topic else "",
            f"Poster: {poster}" if poster else "",
            f"Consultation period: {run_date}" if run_date else "",
            f"Status note: {status}" if status else "",
            f"Content:\n{content}" if content else "",
        ]
        text = "\n\n".join(p for p in parts if p).strip()
        if not text:
            if error_items is not None:
                collect_error(
                    error_items,
                    "Detail page had no extractable case text",
                    step="fetch_detail",
                    context={"forum_id": forum_id, "url": detail_url},
                )
            return ""
        logger.info("  Detail text extracted (%d chars)", len(text))
        return text
    except Exception as exc:
        logger.warning("  Detail parse failed: %s", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"Detail parse failed: {exc}",
                step="fetch_detail",
                context={"forum_id": forum_id, "url": detail_url},
            )
        return ""


def _strip_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def summarize_case_details(
    case_details: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    forum_id: str = "",
) -> Optional[Dict[str, Any]]:
    """LLM structured summary from detail-page text. Returns dict or None."""
    if not case_details or not case_details.strip():
        return None

    truncated = case_details.strip()
    if len(truncated) > 12000:
        truncated = truncated[:12000]
        logger.warning(
            "  Case details truncated to 12000 chars for summary LLM")

    prompt = SUMMARY_PROMPT.replace("{{CASE_DETAILS}}", truncated)
    try:
        response = _get_openai_client().chat.completions.create(
            model=TRANSLATE_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract structured merger-case summaries for Taiwan FTC. "
                        "Return only valid JSON matching the requested schema."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(_strip_json_fences(raw))
        if not isinstance(parsed, dict):
            raise ValueError("Summary JSON root is not an object")
        logger.info("  Case summary LLM OK (keys=%s)", list(parsed.keys()))
        return parsed
    except Exception as exc:
        logger.warning("  Case summary LLM failed: %s", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"Case summary LLM failed: {exc}",
                step="summarize_case",
                context={"forum_id": forum_id},
            )
        return None


def _format_summary_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        items = [str(v).strip()
                 for v in value if v is not None and str(v).strip()]
        return "\n".join(f"• {item}" for item in items) if items else "—"
    text = str(value).strip()
    return text if text else "—"


def render_summary_html(summary: Optional[Dict[str, Any]]) -> str:
    """Render structured summary as an email HTML block."""
    if not summary or not isinstance(summary, dict):
        return ""

    rows: List[str] = []
    for key in SUMMARY_SCALAR_KEYS:
        if key not in summary:
            continue
        val = _format_summary_value(summary.get(key))
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 0;color:#64748b;font-size:14px;vertical-align:top;'
            f'width:190px;">{escape_html(key)}</td>'
            f'<td style="padding:8px 0 8px 12px;font-size:14px;color:#111827;'
            f'white-space:pre-line;">{escape_html(val)}</td>'
            f'</tr>'
        )
    for key in SUMMARY_LIST_KEYS:
        if key not in summary:
            continue
        val = _format_summary_value(summary.get(key))
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 0;color:#64748b;font-size:14px;vertical-align:top;'
            f'width:190px;">{escape_html(key)}</td>'
            f'<td style="padding:8px 0 8px 12px;font-size:14px;color:#111827;'
            f'white-space:pre-line;">{escape_html(val)}</td>'
            f'</tr>'
        )

    if not rows:
        return ""

    return f"""
  <h3 style="color:#0f766e;margin:24px 0 8px 0;font-size:16px;">Case Summary</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    {''.join(rows)}
  </table>
"""


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(
    record: Dict[str, Any],
    deal_match: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    subject = build_subject("taiwan_ftc", "new", deal_match)

    title = record.get("title") or "N/A"
    title_en = record.get("title_en") or title
    period_raw = record.get("period_raw") or "N/A"
    period_en = record.get("period_en") or period_raw
    poster = record.get("poster") or "N/A"
    poster_en = record.get("poster_en") or poster
    reply_count = record.get("reply_count", 0)
    url = record.get("detail_url") or ""
    summary_block = render_summary_html(record.get("summary"))

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

    def _row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">{escape_html(label)}:</td>'
            f'<td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(value)}</td></tr>'
        )

    doc_link = (
        f'<a href="{escape_html(url)}" target="_blank" '
        f'style="color:#0ea5e9;font-size:14px;font-weight:600;">View consultation &rarr;</a>'
        if url else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;">
  <h2 style="color:#333;margin-top:0;border-bottom:3px solid #0f766e;padding-bottom:12px;">
    {escape_html(subject)}
  </h2>
  {banner}
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    {_row("Topic (ZH)", title)}
    {_row("Topic (EN)", title_en)}
    {_row("Period (ZH)", str(period_raw))}
    {_row("Period (EN)", str(period_en))}
    {_row("Poster (ZH)", str(poster))}
    {_row("Poster (EN)", str(poster_en))}
    {_row("Replies", str(reply_count))}
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Link:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{doc_link}</td>
    </tr>
  </table>
  {summary_block}
  <p style="color:#999;font-size:12px;margin-top:24px;">
    Automated email — Taiwan FTC merger consultation forum.
  </p>
</div>
</body>
</html>"""
    return subject, html


def _send_email(
    payload: Dict[str, Any],
    subject: str,
    test_mode: bool,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    try:
        if test_mode:
            webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
            if not webhook_url:
                logger.warning(
                    "N8N_WEBHOOK_ONLY_ME not set — test email skipped")
                if error_items is not None:
                    collect_error(
                        error_items,
                        "N8N_WEBHOOK_ONLY_ME not set — test email skipped",
                        step="send_email",
                        context={"url": payload.get(
                            "url"), "subject": subject},
                    )
                return False
            logger.info(
                "[TEST] Sending to %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
            ok = send_direct_email(
                [TEST_RECIPIENT], payload, webhook_url=webhook_url)
        else:
            ok = post_email_payload(payload, subject=subject)
        if not ok and error_items is not None:
            collect_error(
                error_items,
                f"Email send failed: {subject[:120]}",
                step="send_email",
                context={"url": payload.get("url"), "subject": subject},
            )
        return bool(ok)
    except Exception as exc:
        logger.error("  Email send error: %s", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"Email send error: {exc}",
                step="send_email",
                context={"url": payload.get("url"), "subject": subject},
            )
        return False


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------

def process_record(
    record: Dict[str, Any],
    collection,
    open_deals: List[Dict[str, Any]],
    error_items: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
    test_mode: bool = False,
) -> str:
    """
    Full pipeline for one listing row.

    Returns: skipped | matched | usa | no_email
    """
    forum_id = str(record.get("forum_id") or "").strip()
    title = (record.get("title") or "").strip()
    detail_url = (record.get("detail_url") or "").strip()

    if not forum_id or not title:
        logger.warning(
            "  Skip — missing forum_id or title: forum_id=%s title=%s",
            forum_id, title,
        )
        collect_error(
            error_items,
            "Record missing forum_id or title",
            step="process_record",
            context={"forum_id": forum_id, "title": title},
        )
        return "skipped"

    logger.info("-" * 50)
    logger.info("  RECORD | forum_id=%s | url=%s", forum_id, detail_url)
    logger.info("  Topic (ZH): %s", title)

    if forum_id_exists(collection, forum_id):
        logger.info("  Already in DB — skip")
        return "skipped"

    logger.info("  New forum_id — processing")

    record["period_en"] = format_period_en(
        record.get("period_start") or "",
        record.get("period_end") or "",
        record.get("period_raw") or "",
    )
    record["poster_en"] = format_poster_en(record.get("poster") or "")
    record["title_en"] = translate_topic_zh_to_en(
        title,
        error_items=error_items,
        forum_id=forum_id,
    )
    logger.info("  Period (EN): %s", record["period_en"])
    logger.info("  Poster (EN): %s", record["poster_en"])
    logger.info("  Topic (EN): %s", record["title_en"])

    matched_deal_id, match_type, matched_by_regex = match_deal(
        title,
        record["title_en"],
        record.get("period_en") or "",
        open_deals,
        error_items=error_items,
        forum_id=forum_id,
    )

    deal_match: Optional[Dict[str, Any]] = None
    if matched_deal_id:
        deal_match = get_deal_by_id(matched_deal_id) or {}
        if deal_match and "deal_id" not in deal_match:
            deal_match["deal_id"] = matched_deal_id

    # Option A: detail summary only for LLM/regex deal matches
    record["summary"] = None
    if matched_deal_id and deal_match:
        logger.info("  Match found — fetching detail for case summary")
        case_text = fetch_detail_case_text(
            detail_url, error_items=error_items, forum_id=forum_id,
        )
        if case_text:
            record["summary"] = summarize_case_details(
                case_text, error_items=error_items, forum_id=forum_id,
            )
        if not record.get("summary"):
            logger.info(
                "  Summary unavailable — matched email will use listing fields only"
            )

    now = utc_now_iso()
    doc = {
        "forum_id": forum_id,
        "detail_url": detail_url,
        "title": title,
        "title_en": record.get("title_en") or "",
        "period_raw": record.get("period_raw") or "",
        "period_en": record.get("period_en") or "",
        "period_start": record.get("period_start") or "",
        "period_end": record.get("period_end") or "",
        "poster": record.get("poster") or "",
        "poster_en": record.get("poster_en") or "",
        "reply_count": int(record.get("reply_count") or 0),
        "summary": record.get("summary"),
        "deal_id": matched_deal_id,
        "match_type": match_type,
        "is_open": True,
        "created_at": now,
        "updated_at": now,
    }

    if dry_run:
        logger.info("  DRY-RUN — skip DB insert/email | deal_id=%s",
                    matched_deal_id)
        if matched_deal_id and deal_match:
            return "matched"
        return "no_email"

    try:
        collection.insert_one(doc)
        logger.info("  Inserted forum_id=%s deal_id=%s",
                    forum_id, matched_deal_id)
    except Exception as exc:
        logger.error("  DB insert failed: %s", exc)
        collect_error(
            error_items, str(exc),
            step="insert_record",
            context={"forum_id": forum_id, "title": title[:200]},
        )
        return "skipped"

    if deal_match and matched_deal_id:
        subject, html = build_email_html(record, deal_match)
        if matched_by_regex:
            subject = apply_regex_match_subject(subject, True)
        payload: Dict[str, Any] = {
            "subject": subject,
            "html": html,
            "url": detail_url,
            "source": "taiwan_ftc",
            "is_new_case": True,
            "deal_id": matched_deal_id,
            "forum_id": forum_id,
        }
        _send_email(payload, subject, test_mode, error_items=error_items)
        logger.info(
            "  EMAIL sent | kind=deal_match | deal_id=%s | subject=%s",
            matched_deal_id, subject,
        )
        return "matched"

    if matched_deal_id and not deal_match:
        logger.info(
            "  OUTCOME: no_email (deal_id=%s matched, deal doc missing)",
            matched_deal_id,
        )
        return "no_email"

    is_usa = False
    try:
        is_usa = bool(
            verify_usa_relation(
                company_details=record.get("title_en") or title,
                case_type="TAIWAN_FTC",
            )
        )
        logger.info("  USA check: %s", is_usa)
    except Exception as exc:
        logger.error("  USA check error: %s", exc)
        collect_error(
            error_items,
            str(exc),
            step="verify_usa_relation",
            context={"forum_id": forum_id, "title": title[:200]},
        )

    if is_usa:
        subject, html = build_email_html(record, None)
        payload = {
            "subject": subject,
            "html": html,
            "url": detail_url,
            "source": "taiwan_ftc",
            "is_new_case": True,
            "is_unmatched": True,
            "forum_id": forum_id,
        }
        _send_email(payload, subject, test_mode, error_items=error_items)
        logger.info("  EMAIL sent | kind=usa_unmatched | subject=%s", subject)
        return "usa"

    logger.info(
        "  OUTCOME: no_email | forum_id=%s | deal_id=%s | usa=False",
        forum_id, matched_deal_id,
    )
    return "no_email"


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_taiwan_ftc_cases_register(
    backfill: bool = False,
    dry_run: bool = False,
    test_mode: bool = False,
) -> Dict[str, int]:
    refresh_script_log(logger, _get_log_file)

    max_pages = MAX_PAGES_BACKFILL if backfill else MAX_PAGES_LIVE
    if backfill:
        test_mode = True
    run_start = datetime.datetime.now()
    error_items: List[Dict[str, Any]] = []

    stats: Dict[str, int] = {
        "records_scraped": 0,
        "records_skipped": 0,
        "matched": 0,
        "usa": 0,
        "no_email": 0,
    }

    logger.info("=" * 60)
    logger.info(
        "START: Taiwan FTC Scraper | dry_run=%s | test_email=%s | backfill=%s | max_pages=%d",
        dry_run, test_mode, backfill, max_pages,
    )
    if backfill:
        logger.info("BACKFILL: emails → %s only (test mode)", TEST_RECIPIENT)
    logger.info("=" * 60)

    try:
        ok, msg = init_mongodb_connection()
        if not ok:
            collect_error(
                error_items, f"MongoDB: {msg}", step="mongodb_connect")
            return stats
        logger.info("MongoDB: %s", msg)

        collection = get_collection()
        if collection is None:
            collect_error(
                error_items, "Could not get collection", step="get_collection")
            return stats
        ensure_indexes(collection)

        open_deals = fetch_open_deals()
        logger.info("Loaded %d open deals", len(open_deals))

        records = scrape_all_pages(max_pages, error_items=error_items)
        stats["records_scraped"] = len(records)
        logger.info("Processing %d record(s)", len(records))

        for idx, record in enumerate(records, start=1):
            logger.info(
                "==== [%d/%d] forum_id=%s %s ====",
                idx, len(records),
                record.get("forum_id"),
                (record.get("title") or "")[:80],
            )
            try:
                result = process_record(
                    record,
                    collection,
                    open_deals,
                    error_items,
                    dry_run=dry_run,
                    test_mode=test_mode,
                )
            except Exception as exc:
                logger.exception("  Unhandled record error: %s", exc)
                collect_error(
                    error_items,
                    f"Unhandled record error: {exc}",
                    step="process_record",
                    context={
                        "forum_id": record.get("forum_id"),
                        "title": (record.get("title") or "")[:200],
                    },
                )
                result = "skipped"

            stat_key = {
                "skipped": "records_skipped",
                "matched": "matched",
                "usa": "usa",
                "no_email": "no_email",
            }.get(result, "records_skipped")
            stats[stat_key] += 1
            logger.info("  RESULT [%d/%d]: %s", idx, len(records), result)
            time.sleep(0.5)

    except Exception as exc:
        logger.exception("Unhandled error: %s", exc)
        collect_error(
            error_items,
            f"Unhandled error: {exc}",
            step="main",
            context={"traceback": str(exc)},
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(
            (datetime.datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        for key, val in stats.items():
            logger.info("  %-20s: %d", key, val)
        logger.info("  Errors              : %d", len(error_items))
        logger.info("  Total time          : %ss", elapsed)
        logger.info("=" * 60)

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Taiwan FTC merger consultation forum → taiwan_cases")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch up to {MAX_PAGES_BACKFILL} pages instead of {MAX_PAGES_LIVE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and match only — no DB writes or emails",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help=f"Send emails to {TEST_RECIPIENT} via N8N_WEBHOOK_ONLY_ME",
    )
    args = parser.parse_args()
    run_taiwan_ftc_cases_register(
        backfill=args.backfill,
        dry_run=args.dry_run,
        test_mode=args.test_email,
    )
