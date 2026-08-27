"""
taiwan_ftc_update_monitor.py
============================
Update monitor for Taiwan FTC merger news (結合案件新聞資料).

Source:
  https://www.ftc.gov.tw/internet/main/doc/docList.aspx?mid=1769&uid=1932

Pipeline:
  1. Fetch news listing (live=1 page, backfill=2 pages)
  2. Dedup against taiwan_update by doc_id (docidn)
  3. LLM-match news headline → open taiwan_cases (is_open=true) by forum_id
  4. No match → silent insert into taiwan_update
  5. Match → fetch detail → LLM status extract
       - parent has deal_id → reuse (no deal LLM)
       - else → LLM → regex → USA deal match
       → update email + set parent is_open=false + save taiwan_update

Usage:
  python taiwan_ftc_update_monitor.py
  python taiwan_ftc_update_monitor.py --backfill
  python taiwan_ftc_update_monitor.py --dry-run --test-email
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

SCRIPT_NAME = "taiwan_ftc_update_monitor"
CASES_COLLECTION = "taiwan_cases"
UPDATE_COLLECTION = "taiwan_update"
BASE_URL = "https://www.ftc.gov.tw"
LIST_URL = (
    "https://www.ftc.gov.tw/internet/main/doc/"
    "docList.aspx?mid=1769&uid=1932"
)

MAX_PAGES_LIVE = 1
MAX_PAGES_BACKFILL = 2
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.5

TEST_RECIPIENT = "avshesh.savani@teqnodux.com"
TRANSLATE_MODEL = "gpt-5.2"

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
_openai_client: Optional[OpenAI] = None

DOC_ID_RE = re.compile(r"(?:docidn|docid)=(\d+)", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")

CASE_LINK_PROMPT = """You match Taiwan FTC merger news headlines to existing open consultation cases.

OPEN TAIWAN CASES (forum_id | Chinese title | English title):
{cases_block}

NEWS HEADLINE (Traditional Chinese):
{headline}

NEWS HEADLINE (English translation):
{headline_en}

NEWS DATE: {news_date}

RULES:
1. Return a match ONLY if the news clearly refers to the same merger/combination as one open case (same parties / same transaction).
2. Use both the Chinese and English headlines, and prefer matches against case English titles when company names align.
3. Do not match on industry alone or weak topical similarity.
4. Prefer the strongest party-overlap match.
5. If none clearly match, return None.

RESPONSE FORMAT (exactly one line):
- Match: FORUM_ID
- None
"""

ALLOWED_STATUSES = frozenset({"Approved", "Rejected", "Pending", "Withdrawn"})

STATUS_PROMPT = """You will be given Taiwan Fair Trade Commission (公平交易委員會) merger case details, usually in Traditional Chinese.

Your task is to determine the merger case status and write a short English summary.

Return ONLY valid JSON with exactly this structure:
{{
  "status": "",
  "summary_en": ""
}}

Allowed status values (exactly one):
Approved
Rejected
Pending
Withdrawn

Status rules:

* If the source states phrases such as:
  * 不禁止其結合
  * 公平會通過
  * 公平會放行
  * 公平會點頭放行
  * 公平會開綠燈
  * 整體經濟利益大於限制競爭之不利益，因此不禁止其結合
  then set status to: Approved

* If the source explicitly states that the merger is prohibited, rejected, or not approved, set status to: Rejected

* If the case is still under review, consultation, or no final decision is stated, set status to: Pending

* If the filing or transaction was explicitly withdrawn, set status to: Withdrawn

Important:

* Determine the status only from the provided case details.
* Do not infer approval merely because a public consultation has closed.
* A phrase such as "此主題已結束討論" only means the consultation discussion has closed; if no final merger decision is stated, return "Pending".
* summary_en: 1-3 concise professional English sentences covering the parties, transaction, and decision/outcome stated in the source. Do not invent facts.
* Ignore webpage elements such as Facebook, Twitter, Google+, print links, navigation, and other irrelevant content.
* Return ONLY the JSON object with no Markdown or additional explanation.

Case details:

{{CASE_DETAILS}}
"""


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


def _strip_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_news_date(raw: str) -> str:
    m = DATE_RE.search(raw or "")
    if not m:
        return (raw or "").strip()
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def extract_doc_id(href: str) -> str:
    if not href:
        return ""
    m = DOC_ID_RE.search(href)
    if m:
        return m.group(1)
    try:
        qs = parse_qs(urlparse(href).query)
        for key in ("docidn", "docid", "DocID"):
            vals = qs.get(key) or []
            if vals and vals[0].strip():
                return vals[0].strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

def get_cases_collection():
    db = get_database()
    return None if db is None else db[CASES_COLLECTION]


def get_update_collection():
    db = get_database()
    return None if db is None else db[UPDATE_COLLECTION]


def ensure_indexes(update_col) -> None:
    try:
        update_col.create_index("doc_id", unique=True, name="doc_id_unique")
        logger.info("Indexes ensured on %s", UPDATE_COLLECTION)
    except Exception as exc:
        logger.warning("Could not ensure indexes: %s", exc)


def doc_id_exists(update_col, doc_id: str) -> bool:
    if update_col is None or not doc_id:
        return False
    return update_col.find_one({"doc_id": str(doc_id)}, {"_id": 1}) is not None


def fetch_open_taiwan_cases(cases_col) -> List[Dict[str, Any]]:
    if cases_col is None:
        return []
    return list(
        cases_col.find(
            {"is_open": True},
            {
                "forum_id": 1,
                "title": 1,
                "title_en": 1,
                "deal_id": 1,
                "match_type": 1,
                "period_raw": 1,
                "period_en": 1,
                "detail_url": 1,
            },
        )
    )


# ---------------------------------------------------------------------------
# HTTP + listing parse
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
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if page_num <= 1:
                logger.info(
                    "  GET news page %d (attempt %d/%d)",
                    page_num, attempt, MAX_RETRIES,
                )
                resp = _http_session.get(LIST_URL, timeout=REQUEST_TIMEOUT)
            else:
                if not aspnet_fields or not aspnet_fields.get("__VIEWSTATE"):
                    logger.error(
                        "  Missing ViewState for news page %d", page_num)
                    return None
                data = {
                    "__EVENTTARGET": "ctl00$ContentPlaceHolder1$dl_toPage",
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": aspnet_fields.get("__VIEWSTATE", ""),
                    "__VIEWSTATEGENERATOR": aspnet_fields.get(
                        "__VIEWSTATEGENERATOR", ""),
                    "__EVENTVALIDATION": aspnet_fields.get(
                        "__EVENTVALIDATION", ""),
                    "ctl00$ContentPlaceHolder1$dl_toPage": str(page_num),
                }
                logger.info(
                    "  POST news page %d (attempt %d/%d)",
                    page_num, attempt, MAX_RETRIES,
                )
                resp = _http_session.post(
                    LIST_URL, data=data, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text, _extract_aspnet_fields(resp.text)
        except Exception as exc:
            logger.warning(
                "  News list fetch failed page=%d attempt=%d/%d: %s",
                page_num, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(5)
    return None


def parse_news_list(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    records: List[Dict[str, Any]] = []
    for li in soup.select("div.news-list ul li"):
        a = li.select_one("a[href]")
        if not a:
            continue
        href = (a.get("href") or "").strip()
        date_el = a.select_one("span.date_block")
        title_el = a.select_one("p")
        date_raw = (
            re.sub(r"\s+", " ", date_el.get_text(" ", strip=True)).strip()
            if date_el else ""
        )
        title = (
            re.sub(r"\s+", " ", title_el.get_text(" ", strip=True)).strip()
            if title_el
            else re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        )
        if date_raw and title.startswith(date_raw):
            title = title[len(date_raw):].strip()
        doc_id = extract_doc_id(href)
        if not doc_id or not title:
            continue
        detail_url = urljoin(LIST_URL, href.split()[0])
        records.append({
            "doc_id": doc_id,
            "date_raw": date_raw,
            "date": parse_news_date(date_raw),
            "title": title,
            "detail_url": detail_url,
        })
    return records


def scrape_news_pages(
    max_pages: int,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    all_records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    aspnet_fields: Dict[str, str] = {}

    for page_num in range(1, max_pages + 1):
        logger.info("[Page %d] Fetching news listing", page_num)
        result = fetch_list_page(page_num, aspnet_fields)
        if not result:
            if error_items is not None:
                collect_error(
                    error_items,
                    f"Failed to fetch Taiwan FTC news page {page_num}",
                    step="fetch_news_list",
                    context={"page": page_num, "url": LIST_URL},
                )
            break
        html, aspnet_fields = result
        page_records = parse_news_list(html)
        logger.info("[Page %d] Parsed %d news item(s)",
                    page_num, len(page_records))
        if not page_records:
            break
        for rec in page_records:
            if rec["doc_id"] in seen:
                continue
            seen.add(rec["doc_id"])
            all_records.append(rec)
        time.sleep(REQUEST_DELAY)

    logger.info("Scraped %d unique news item(s)", len(all_records))
    return all_records


def fetch_news_detail_text(
    detail_url: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    doc_id: str = "",
) -> Tuple[str, str]:
    """Return (final_url, body_text)."""
    if not detail_url:
        return "", ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(
                "  GET news detail (attempt %d/%d): %s",
                attempt, MAX_RETRIES, detail_url,
            )
            resp = _http_session.get(detail_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
            el = soup.select_one("#ContentPlaceHolder1_lb_content")
            body = el.get_text("\n", strip=True).strip() if el else ""
            if not body:
                body = soup.get_text("\n", strip=True)[:8000]
            return resp.url or detail_url, body
        except Exception as exc:
            logger.warning(
                "  News detail fetch failed attempt=%d/%d: %s",
                attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(5)
    if error_items is not None:
        collect_error(
            error_items,
            f"Failed to fetch news detail: {detail_url}",
            step="fetch_news_detail",
            context={"doc_id": doc_id, "url": detail_url},
        )
    return detail_url, ""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def translate_headline_zh_to_en(
    text: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    doc_id: str = "",
) -> str:
    """Translate news headline Traditional Chinese → English (one GPT call)."""
    if not text or not isinstance(text, str) or not text.strip():
        return text
    if len(text) > 1500:
        logger.warning(
            "  Headline translation skipped: text too long (%d chars)", len(text))
        if error_items is not None:
            collect_error(
                error_items,
                f"Headline translation skipped: too long ({len(text)} chars)",
                step="translate_headline",
                context={"doc_id": doc_id},
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
                        "for Taiwan FTC (Fair Trade Commission) merger news headlines.\n"
                        "Rules:\n"
                        "1. Return ONLY the translated English headline.\n"
                        "2. Use well-known official English company names where possible.\n"
                        "3. Do NOT explain or add alternatives.\n"
                        "4. Preserve regulatory meaning (approved, cleared, green light, etc.)."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Translate this Taiwan FTC merger news headline to English:\n{text}"
                    ),
                },
            ],
        )
        translated = (response.choices[0].message.content or "").strip()
        if translated:
            logger.info(
                "  Translated headline OK (OpenAI %s): %.120s",
                TRANSLATE_MODEL, translated,
            )
            return translated
        logger.warning("  OpenAI headline translation empty — using original")
        if error_items is not None:
            collect_error(
                error_items,
                "OpenAI headline translation returned empty text",
                step="translate_headline",
                context={"doc_id": doc_id},
            )
    except Exception as exc:
        logger.warning(
            "  OpenAI headline translation failed: %s — using original", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"OpenAI headline translation failed: {exc}",
                step="translate_headline",
                context={"doc_id": doc_id},
            )
    return text


def llm_link_open_case(
    headline: str,
    headline_en: str,
    news_date: str,
    open_cases: List[Dict[str, Any]],
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    doc_id: str = "",
) -> Optional[str]:
    """Return matched forum_id or None."""
    if not open_cases:
        return None
    lines = []
    valid_ids = set()
    for c in open_cases:
        fid = str(c.get("forum_id") or "").strip()
        if not fid:
            continue
        valid_ids.add(fid)
        title_zh = (c.get("title") or "").strip()
        title_en = (c.get("title_en") or "").strip()
        lines.append(f"{fid} | {title_zh} | {title_en}")
    if not lines:
        return None

    prompt = CASE_LINK_PROMPT.format(
        cases_block="\n".join(lines),
        headline=headline,
        headline_en=headline_en or headline,
        news_date=news_date or "N/A",
    )
    try:
        response = _get_openai_client().chat.completions.create(
            model=TRANSLATE_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You link Taiwan FTC merger news headlines to open consultation "
                        "cases. Reply with exactly 'Match: FORUM_ID' or 'None'."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        logger.info("  Case-link LLM raw: %s", raw[:200])
        if raw.lower() == "none" or not raw.lower().startswith("match:"):
            return None
        forum_id = raw.split(":", 1)[1].strip().split()[0].strip()
        if forum_id in valid_ids:
            return forum_id
        logger.warning(
            "  Case-link LLM returned unknown forum_id=%s", forum_id)
        if error_items is not None:
            collect_error(
                error_items,
                f"Case-link LLM returned unknown forum_id={forum_id}",
                step="llm_link_open_case",
                context={"doc_id": doc_id, "headline": headline[:200]},
            )
        return None
    except Exception as exc:
        logger.error("  Case-link LLM error: %s", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"Case-link LLM error: {exc}",
                step="llm_link_open_case",
                context={"doc_id": doc_id, "headline": headline[:200]},
            )
        return None


def llm_extract_status(
    headline: str,
    body: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    doc_id: str = "",
) -> Optional[Dict[str, Any]]:
    parts = [
        f"Headline: {headline}" if headline else "",
        f"Article text:\n{body.strip()}" if (body or "").strip() else "",
    ]
    case_details = "\n\n".join(
        p for p in parts if p).strip() or "(no case details)"
    if len(case_details) > 12000:
        case_details = case_details[:12000]
    prompt = STATUS_PROMPT.replace("{{CASE_DETAILS}}", case_details)
    try:
        response = _get_openai_client().chat.completions.create(
            model=TRANSLATE_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract Taiwan FTC merger status and a short English summary. "
                        "Return only valid JSON. status must be exactly one of: "
                        "Approved, Rejected, Pending, Withdrawn."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(_strip_json_fences(raw))
        if not isinstance(parsed, dict):
            raise ValueError("status JSON root is not an object")

        status_raw = str(parsed.get("status") or "").strip()
        # Normalize common variants to the four allowed words
        status_map = {
            "approved": "Approved",
            "cleared": "Approved",
            "not prohibited": "Approved",
            "conditional approval": "Approved",
            "rejected": "Rejected",
            "prohibited": "Rejected",
            "pending": "Pending",
            "withdrawn": "Withdrawn",
        }
        status = status_map.get(status_raw.lower(), status_raw)
        if status not in ALLOWED_STATUSES:
            logger.warning(
                "  Status LLM returned unexpected status=%r — defaulting to Pending",
                status_raw,
            )
            status = "Pending"
        parsed["status"] = status
        parsed["summary_en"] = str(parsed.get("summary_en") or "").strip()
        logger.info(
            "  Status LLM OK: status=%s | summary=%.120s",
            status, parsed["summary_en"],
        )
        return parsed
    except Exception as exc:
        logger.warning("  Status LLM failed: %s", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"Status LLM failed: {exc}",
                step="llm_extract_status",
                context={"doc_id": doc_id},
            )
        return None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_update_email_html(
    news: Dict[str, Any],
    parent: Dict[str, Any],
    status_info: Optional[Dict[str, Any]],
    deal_match: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    subject = build_subject("taiwan_ftc", "update", deal_match)
    status_info = status_info or {}

    headline = news.get("title") or "N/A"
    headline_en = news.get("title_en") or headline
    status = status_info.get("status") or "N/A"
    summary = status_info.get("summary_en") or ""
    news_date = news.get("date") or news.get("date_raw") or "N/A"
    url = news.get("detail_url") or ""
    forum_id = parent.get("forum_id") or ""
    case_title = parent.get("title_en") or parent.get("title") or ""

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
  <strong>USA-Related Update</strong> — No deal match; appears USA-related.
</div>"""

    def _row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;vertical-align:top;">'
            f'{escape_html(label)}:</td>'
            f'<td style="padding:6px 0 6px 12px;font-size:14px;white-space:pre-line;">'
            f'{escape_html(value)}</td></tr>'
        )

    doc_link = (
        f'<a href="{escape_html(url)}" target="_blank" '
        f'style="color:#0ea5e9;font-size:14px;font-weight:600;">View news &rarr;</a>'
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
    {_row("Status", str(status))}
    {_row("Summary (EN)", summary if summary else "—")}
    {_row("News date", str(news_date))}
    {_row("Headline (ZH)", headline)}
    {_row("Headline (EN)", headline_en)}
    {_row("Linked case (forum_id)", str(forum_id))}
    {_row("Linked case title", case_title or "—")}
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Link:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{doc_link}</td>
    </tr>
  </table>
  <p style="color:#999;font-size:12px;margin-top:24px;">
    Automated email — Taiwan FTC merger news update monitor.
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
# Per-item processing
# ---------------------------------------------------------------------------

def process_news_item(
    news: Dict[str, Any],
    cases_col,
    update_col,
    open_cases: List[Dict[str, Any]],
    open_deals: List[Dict[str, Any]],
    open_cases_by_id: Dict[str, Dict[str, Any]],
    error_items: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
    test_mode: bool = False,
) -> str:
    """
    Returns: skipped | saved_unmatched | updated_matched | updated_usa | updated_no_email
    """
    doc_id = str(news.get("doc_id") or "").strip()
    title = (news.get("title") or "").strip()
    detail_url = (news.get("detail_url") or "").strip()

    if not doc_id or not title:
        collect_error(
            error_items,
            "News item missing doc_id or title",
            step="process_news_item",
            context={"doc_id": doc_id, "title": title},
        )
        return "skipped"

    logger.info("-" * 50)
    logger.info("  NEWS | doc_id=%s | date=%s", doc_id, news.get("date"))
    logger.info("  Title: %s", title)

    if doc_id_exists(update_col, doc_id):
        logger.info("  Already in taiwan_update — skip")
        return "skipped"

    # Translate headline first, then use ZH+EN to match open taiwan_cases
    title_en = translate_headline_zh_to_en(
        title, error_items=error_items, doc_id=doc_id,
    )
    news["title_en"] = title_en
    logger.info("  Title (EN): %s", title_en)

    forum_id = llm_link_open_case(
        title,
        title_en,
        news.get("date") or news.get("date_raw") or "",
        open_cases,
        error_items=error_items,
        doc_id=doc_id,
    )

    now = utc_now_iso()

    if not forum_id:
        logger.info("  No open-case match — silent save to taiwan_update")
        doc = {
            "doc_id": doc_id,
            "date": news.get("date") or "",
            "date_raw": news.get("date_raw") or "",
            "title": title,
            "title_en": title_en,
            "detail_url": detail_url,
            "matched_forum_id": None,
            "status": None,
            "deal_id": None,
            "match_type": None,
            "created_at": now,
            "updated_at": now,
        }
        if dry_run:
            logger.info("  DRY-RUN — skip unmatched insert")
            return "saved_unmatched"
        try:
            update_col.insert_one(doc)
            logger.info("  Inserted unmatched doc_id=%s", doc_id)
        except Exception as exc:
            logger.error("  Insert unmatched failed: %s", exc)
            collect_error(
                error_items, str(exc), step="insert_unmatched",
                context={"doc_id": doc_id},
            )
            return "skipped"
        return "saved_unmatched"

    parent = open_cases_by_id.get(forum_id)
    if not parent:
        # Refresh from DB in case list is stale
        parent = cases_col.find_one({"forum_id": forum_id}) or {}
    if not parent:
        logger.warning(
            "  Matched forum_id=%s missing in DB — save unmatched", forum_id)
        return "saved_unmatched"

    logger.info("  Linked open case forum_id=%s", forum_id)

    final_url, body = fetch_news_detail_text(
        detail_url, error_items=error_items, doc_id=doc_id,
    )
    if final_url:
        news["detail_url"] = final_url

    status_info = llm_extract_status(
        title, body, error_items=error_items, doc_id=doc_id,
    ) or {}

    existing_deal_id = parent.get("deal_id")
    matched_deal_id: Optional[str] = (
        str(existing_deal_id) if existing_deal_id else None
    )
    match_type: Optional[str] = parent.get("match_type")
    matched_by_regex = False
    deal_match: Optional[Dict[str, Any]] = None

    if matched_deal_id:
        deal_match = get_deal_by_id(matched_deal_id)
        if deal_match:
            if "deal_id" not in deal_match:
                deal_match["deal_id"] = matched_deal_id
            logger.info(
                "  Reusing parent deal_id=%s (skip deal LLM)", matched_deal_id)
        else:
            logger.warning(
                "  Parent deal_id=%s missing in deals — re-matching", matched_deal_id)
            matched_deal_id = None
            match_type = None

    if not matched_deal_id:
        title_en_for_match = title_en or parent.get("title_en") or title
        try:
            matched_deal_id = llm_match_deal_id(
                regulator_name="Taiwan FTC",
                case_sections={
                    "NEWS HEADLINE (ZH)": title,
                    "NEWS HEADLINE (EN)": title_en_for_match,
                    "STATUS": str(status_info.get("status") or ""),
                    "SUMMARY (EN)": str(status_info.get("summary_en") or ""),
                    "ORIGINAL CASE TITLE (EN)": parent.get("title_en") or "",
                },
                source_label="the Taiwan FTC news headline and case title",
                deals=open_deals,
            )
            if matched_deal_id:
                match_type = "llm"
                logger.info("  Deal LLM hit deal_id=%s", matched_deal_id)
        except Exception as exc:
            logger.error("  Deal LLM error: %s", exc)
            collect_error(
                error_items, str(exc), step="llm_match_deal_id",
                context={"doc_id": doc_id, "forum_id": forum_id},
            )

        if not matched_deal_id:
            matched_deal_id = regex_match_taiwan_deal(
                title_en or title, open_deals)
            if matched_deal_id:
                match_type = "regex"
                matched_by_regex = True
                logger.info("  Deal regex hit deal_id=%s", matched_deal_id)

        if matched_deal_id:
            deal_match = get_deal_by_id(matched_deal_id)
            if deal_match and "deal_id" not in deal_match:
                deal_match["deal_id"] = matched_deal_id
            if not deal_match:
                logger.warning(
                    "  deal_id=%s not found in deals", matched_deal_id)
                matched_deal_id = None
                match_type = None

    update_doc = {
        "doc_id": doc_id,
        "date": news.get("date") or "",
        "date_raw": news.get("date_raw") or "",
        "title": title,
        "title_en": title_en,
        "detail_url": news.get("detail_url") or detail_url,
        "matched_forum_id": forum_id,
        "status": status_info.get("status"),
        "summary_en": status_info.get("summary_en") or "",
        "deal_id": matched_deal_id,
        "match_type": match_type,
        "created_at": now,
        "updated_at": now,
    }

    parent_update = {
        "is_open": False,
        "status": status_info.get("status"),
        "status_summary_en": status_info.get("summary_en") or "",
        "last_update_doc_id": doc_id,
        "last_update_url": news.get("detail_url") or detail_url,
        "last_update_date": news.get("date") or "",
        "closed_at": now,
        "updated_at": now,
    }
    if matched_deal_id:
        parent_update["deal_id"] = matched_deal_id
        parent_update["match_type"] = match_type

    if dry_run:
        logger.info(
            "  DRY-RUN — skip DB/email | forum_id=%s deal_id=%s status=%s",
            forum_id, matched_deal_id, status_info.get("status"),
        )
        if deal_match and matched_deal_id:
            return "updated_matched"
        return "updated_no_email"

    try:
        update_col.insert_one(update_doc)
        cases_col.update_one({"forum_id": forum_id}, {"$set": parent_update})
        # Keep in-memory open list consistent for later items in this run
        open_cases_by_id.pop(forum_id, None)
        open_cases[:] = [c for c in open_cases if str(
            c.get("forum_id")) != forum_id]
        logger.info(
            "  Closed forum_id=%s | saved update doc_id=%s", forum_id, doc_id)
    except Exception as exc:
        logger.error("  DB write failed: %s", exc)
        collect_error(
            error_items, str(exc), step="db_write",
            context={"doc_id": doc_id, "forum_id": forum_id},
        )
        return "skipped"

    if deal_match and matched_deal_id:
        subject, html = build_update_email_html(
            news, parent, status_info, deal_match)
        if matched_by_regex:
            subject = apply_regex_match_subject(subject, True)
        payload = {
            "subject": subject,
            "html": html,
            "url": news.get("detail_url") or detail_url,
            "source": "taiwan_ftc_update",
            "is_new_case": False,
            "deal_id": matched_deal_id,
            "forum_id": forum_id,
            "doc_id": doc_id,
            "status": status_info.get("status"),
        }
        _send_email(payload, subject, test_mode, error_items=error_items)
        logger.info("  EMAIL sent | kind=deal_match | subject=%s", subject)
        return "updated_matched"

    # USA check only when no deal match
    is_usa = False
    try:
        usa_text = (
            status_info.get("summary_en")
            or parent.get("title_en")
            or title
        )
        is_usa = bool(
            verify_usa_relation(usa_text, case_type="TAIWAN_FTC_UPDATE")
        )
        logger.info("  USA check: %s", is_usa)
    except Exception as exc:
        logger.error("  USA check error: %s", exc)
        collect_error(
            error_items, str(exc), step="verify_usa_relation",
            context={"doc_id": doc_id, "forum_id": forum_id},
        )

    if is_usa:
        subject, html = build_update_email_html(
            news, parent, status_info, None)
        payload = {
            "subject": subject,
            "html": html,
            "url": news.get("detail_url") or detail_url,
            "source": "taiwan_ftc_update",
            "is_new_case": False,
            "is_unmatched": True,
            "forum_id": forum_id,
            "doc_id": doc_id,
            "status": status_info.get("status"),
        }
        _send_email(payload, subject, test_mode, error_items=error_items)
        logger.info("  EMAIL sent | kind=usa_unmatched | subject=%s", subject)
        return "updated_usa"

    logger.info(
        "  OUTCOME: updated_no_email | forum_id=%s | deal_id=None | usa=False",
        forum_id,
    )
    return "updated_no_email"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_taiwan_ftc_update_monitor(
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
        "news_scraped": 0,
        "skipped": 0,
        "saved_unmatched": 0,
        "updated_matched": 0,
        "updated_usa": 0,
        "updated_no_email": 0,
    }

    logger.info("=" * 60)
    logger.info(
        "START: Taiwan FTC Update Monitor | dry_run=%s | test_email=%s | "
        "backfill=%s | max_pages=%d",
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

        cases_col = get_cases_collection()
        update_col = get_update_collection()
        if cases_col is None or update_col is None:
            collect_error(
                error_items, "Could not get collections", step="get_collection")
            return stats
        ensure_indexes(update_col)

        open_cases = fetch_open_taiwan_cases(cases_col)
        open_cases_by_id = {
            str(c.get("forum_id")): c
            for c in open_cases
            if c.get("forum_id")
        }
        logger.info("Loaded %d is_open=true taiwan_cases", len(open_cases))

        open_deals = fetch_open_deals()
        logger.info("Loaded %d open deals", len(open_deals))

        news_items = scrape_news_pages(max_pages, error_items=error_items)
        stats["news_scraped"] = len(news_items)

        for idx, news in enumerate(news_items, start=1):
            logger.info(
                "==== [%d/%d] doc_id=%s %s ====",
                idx, len(news_items), news.get("doc_id"),
                (news.get("title") or "")[:80],
            )
            try:
                result = process_news_item(
                    news,
                    cases_col,
                    update_col,
                    open_cases,
                    open_deals,
                    open_cases_by_id,
                    error_items,
                    dry_run=dry_run,
                    test_mode=test_mode,
                )
            except Exception as exc:
                logger.exception("  Unhandled news error: %s", exc)
                collect_error(
                    error_items,
                    f"Unhandled news error: {exc}",
                    step="process_news_item",
                    context={
                        "doc_id": news.get("doc_id"),
                        "title": (news.get("title") or "")[:200],
                    },
                )
                result = "skipped"

            stat_key = {
                "skipped": "skipped",
                "saved_unmatched": "saved_unmatched",
                "updated_matched": "updated_matched",
                "updated_usa": "updated_usa",
                "updated_no_email": "updated_no_email",
            }.get(result, "skipped")
            stats[stat_key] += 1
            logger.info("  RESULT [%d/%d]: %s", idx, len(news_items), result)
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
        description="Taiwan FTC merger news → taiwan_update / close taiwan_cases")
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
    run_taiwan_ftc_update_monitor(
        backfill=args.backfill,
        dry_run=args.dry_run,
        test_mode=args.test_email,
    )
