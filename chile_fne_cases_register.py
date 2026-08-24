"""
chile_fne_cases_register.py
===========================
Scraper for Chile FNE (Fiscalía Nacional Económica) merger filings.

Source:
  https://www.fne.gob.cl/search/operaciones_resultados.php?pagina=N&...

Pipeline:
  1. Fetch paginated Fusiones listing (live=1 page, backfill=5 pages)
  2. Dedup against chili_cases by url
  3. Translate title ES → EN
  4. LLM Prompt 1 — link to existing is_new=true parent record (same transaction)
  5. LLM deal match → regex fallback → USA check → email
  6. Insert is_new=true (new case) or is_new=false + push to parent.updates

Usage:
  python chile_fne_cases_register.py
  python chile_fne_cases_register.py --backfill
  python chile_fne_cases_register.py --dry-run --test-email
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import time
from datetime import timezone
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI

from deal_match_llm import call_llm, fetch_open_deals, llm_match_deal_id, parse_deal_id
from deal_match_regex import apply_regex_match_subject, regex_match_chile_deal
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

SCRIPT_NAME = "chile_fne_cases_register"
COLLECTION_NAME = "chili_cases"
BASE_URL = "https://www.fne.gob.cl"
LIST_URL_TEMPLATE = (
    "https://www.fne.gob.cl/search/operaciones_resultados.php"
    "?pagina={page}&select1=0&Conducta=&Mercado=&Partes=&select2=&Clave="
    "&AnoIni=0&AnoFin=2026"
)

MAX_PAGES_LIVE = 1
MAX_PAGES_BACKFILL = 11
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
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
}

_http_session = requests.Session()
_http_session.headers.update(HTTP_HEADERS)

logger, _get_log_file = ensure_script_logger(SCRIPT_NAME)


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


TRANSLATE_MODEL = "gpt-5.2"
_openai_client: Optional[OpenAI] = None


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def translate_es_to_en(
    text: str,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    url: str = "",
    title: str = "",
) -> str:
    """Translate Spanish FNE title to English via OpenAI (same approach as SAMR)."""
    if not text or not isinstance(text, str) or not text.strip():
        return text
    if len(text) > 1500:
        logger.warning(
            "  Translation skipped: text too long (%d chars)", len(text))
        if error_items is not None:
            collect_error(
                error_items,
                f"Translation skipped: text too long ({len(text)} chars)",
                step="translate",
                context={"url": url, "title": title[:200]},
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
                        "You are a professional Spanish-to-English translator for Chile FNE "
                        "merger-control filing titles.\n"
                        "Rules:\n"
                        "1. Return ONLY the translated English title.\n"
                        "2. Use well-known official English company names where possible.\n"
                        "3. Do NOT explain or add alternatives.\n"
                        "4. Preserve regulatory meaning (resolution, report, investigation, etc.)."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Translate this Chile FNE filing title to English:\n{text}",
                },
            ],
        )
        translated = (response.choices[0].message.content or "").strip()
        if translated:
            logger.info("  Translated OK (OpenAI %s): %.120s",
                        TRANSLATE_MODEL, translated)
            return translated
        logger.warning("  OpenAI translation empty — using original Spanish")
        if error_items is not None:
            collect_error(
                error_items,
                "OpenAI translation returned empty text",
                step="translate",
                context={"url": url, "title": (title or text)[:200]},
            )
    except Exception as exc:
        logger.warning(
            "  OpenAI translation failed: %s — using original Spanish", exc)
        if error_items is not None:
            collect_error(
                error_items,
                f"OpenAI translation failed: {exc}",
                step="translate",
                context={"url": url, "title": (title or text)[:200]},
            )
    return text


def parse_fne_date(raw: str) -> str:
    """Convert DD/MM/YYYY → YYYY-MM-DD."""
    raw = (raw or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        day, month, year = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return raw


# FNE "Tipo" dropdown values (Spanish) + English labels for emails
PHASE_OTROS = "otros"
PHASE_ES_TO_EN: Dict[str, str] = {
    "Fiscalización de medidas op. concentración": (
        "Monitoring of concentration operation measures"
    ),
    "Informe de aprobación FASE 1": "Phase 1 Approval Report",
    "Informe de aprobación FASE 2": "Phase 2 Approval Report",
    "Informe de prohibición operaciones de concentración": (
        "Report on the prohibition of concentration operations"
    ),
    "otros": "Other",
    "Res. Art. 54 a)": "Resolution Art. 54 a)",
    "Res. Art. 54 b)": "Resolution Art. 54 b)",
    "Res. Art. 54 c)": "Resolution Art. 54 c)",
    "Res. Art. 57 a)": "Resolution Art. 57 a)",
    "Res. Art. 57 b)": "Resolution Art. 57 b)",
    "Res. Art. 57 c)": "Resolution Art. 57 c)",
    "Resolucion de inicio de investigaciones de concentración": (
        "Resolution to initiate concentration investigations"
    ),
    "Resoluciones que responden preguntas en Pre-Notificación": (
        "Resolutions answering questions in Pre-Notification"
    ),
}


def _strip_accents(text: str) -> str:
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ü": "u", "ñ": "n",
        "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
        "Ü": "U", "Ñ": "N",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def _normalize_phase_text(text: str) -> str:
    text = _strip_accents((text or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_phase(title: str, url: str = "") -> Tuple[str, str]:
    """
    Identify FNE Tipo/phase from Spanish title and/or PDF URL.

    Returns (phase_es, phase_en). Emails should use phase_en.
    """
    title_n = _normalize_phase_text(title)
    url_n = _normalize_phase_text(url)
    filename = url_n.rsplit("/", 1)[-1] if url_n else ""

    # 1) Prefer filename markers from FNE upload names
    if re.search(r"\binic[_-]|\binicio[_-]", filename):
        phase_es = "Resolucion de inicio de investigaciones de concentración"
    elif re.search(r"54\s*c|54c|exten54c|exten[_-]?54c", filename):
        phase_es = "Res. Art. 54 c)"
    elif re.search(r"54\s*b|54b|aprob54b", filename):
        phase_es = "Res. Art. 54 b)"
    elif re.search(r"54\s*a|54a|aprob54a", filename):
        phase_es = "Res. Art. 54 a)"
    elif re.search(r"57\s*c|57c", filename):
        phase_es = "Res. Art. 57 c)"
    elif re.search(r"57\s*b|57b", filename):
        phase_es = "Res. Art. 57 b)"
    elif re.search(r"57\s*a|57a", filename):
        phase_es = "Res. Art. 57 a)"
    elif re.search(r"\binap\d*[_-]|\binforme[_-]?aprob", filename):
        if "fase 2" in title_n or "fase2" in title_n:
            phase_es = "Informe de aprobación FASE 2"
        else:
            phase_es = "Informe de aprobación FASE 1"
    else:
        phase_es = None

    # 2) Title keyword fallback / override when filename is ambiguous
    if phase_es is None:
        if "pre-notificacion" in title_n or "prenotificacion" in title_n:
            phase_es = "Resoluciones que responden preguntas en Pre-Notificación"
        elif "prohibicion" in title_n:
            phase_es = "Informe de prohibición operaciones de concentración"
        elif "fiscalizacion" in title_n or (
            "medidas" in title_n and "concentracion" in title_n
        ):
            phase_es = "Fiscalización de medidas op. concentración"
        elif "inicio de investigacion" in title_n:
            phase_es = "Resolucion de inicio de investigaciones de concentración"
        elif re.search(r"art\.?\s*54\s*c\)?|54\s*c\)", title_n):
            phase_es = "Res. Art. 54 c)"
        elif re.search(r"art\.?\s*54\s*b\)?|54\s*b\)", title_n):
            phase_es = "Res. Art. 54 b)"
        elif re.search(r"art\.?\s*54\s*a\)?|54\s*a\)", title_n):
            phase_es = "Res. Art. 54 a)"
        elif re.search(r"art\.?\s*57\s*c\)?|57\s*c\)", title_n):
            phase_es = "Res. Art. 57 c)"
        elif re.search(r"art\.?\s*57\s*b\)?|57\s*b\)", title_n):
            phase_es = "Res. Art. 57 b)"
        elif re.search(r"art\.?\s*57\s*a\)?|57\s*a\)", title_n):
            phase_es = "Res. Art. 57 a)"
        elif "extension de investigacion" in title_n:
            phase_es = "Res. Art. 54 c)"
        elif "informe de aprobacion" in title_n:
            if "fase 2" in title_n or "fase2" in title_n:
                phase_es = "Informe de aprobación FASE 2"
            else:
                phase_es = "Informe de aprobación FASE 1"
        elif "resolucion de aprobacion" in title_n or "resolucion aprobacion" in title_n:
            # Approval resolution without explicit article → Art. 54 a) default
            phase_es = "Res. Art. 54 a)"
        else:
            phase_es = PHASE_OTROS

    phase_en = PHASE_ES_TO_EN.get(phase_es, "Other")
    return phase_es, phase_en


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
        collection.create_index("url", unique=True, name="url_unique")
        collection.create_index("is_new", name="is_new_idx")
        logger.info("Indexes ensured on %s", COLLECTION_NAME)
    except Exception as exc:
        logger.warning("Could not ensure indexes: %s", exc)


def url_exists(collection, url: str) -> bool:
    if collection is None or not url:
        return False
    return collection.find_one({"url": url.strip()}, {"_id": 1}) is not None


def fetch_is_new_records(collection) -> List[Dict[str, Any]]:
    if collection is None:
        return []
    return list(collection.find({"is_new": True}))


def get_record_by_id(collection, record_id: str) -> Optional[Dict[str, Any]]:
    if collection is None or not record_id:
        return None
    try:
        return collection.find_one({"_id": ObjectId(record_id)})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP + parsing
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> Optional[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("  GET %s (attempt %d/%d)", url, attempt, MAX_RETRIES)
            resp = _http_session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "iso-8859-1"
            return resp.text
        except Exception as exc:
            logger.warning("  Request failed (attempt %d/%d): %s",
                           attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(5)
    return None


def parse_list_page(html: str) -> List[Dict[str, str]]:
    """Extract rows from FNE Fusiones results table."""
    soup = BeautifulSoup(html, "html.parser")
    records: List[Dict[str, str]] = []

    for tr in soup.select("table.results tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        link = tds[0].find("a", href=True)
        if not link:
            continue
        url = (link.get("href") or "").strip()
        title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
        if not url or not title:
            continue
        if not url.startswith("http"):
            url = BASE_URL + \
                url if url.startswith("/") else f"{BASE_URL}/{url}"
        date_raw = re.sub(
            r"\s+", " ", tds[1].get_text(" ", strip=True)).strip()
        records.append({
            "title": title,
            "title_en": "",
            "url": url,
            "date": parse_fne_date(date_raw),
            "date_raw": date_raw,
        })

    return records


def scrape_all_pages(
    max_pages: int,
    *,
    oldest_first: bool = False,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    all_records: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    for page_num in range(1, max_pages + 1):
        list_url = LIST_URL_TEMPLATE.format(page=page_num)
        logger.info("[Page %d] Fetching: %s", page_num, list_url)
        html = fetch_html(list_url)
        if not html:
            logger.warning(
                "[Page %d] Fetch failed — stopping pagination", page_num)
            if error_items is not None:
                collect_error(
                    error_items,
                    f"Failed to fetch FNE listing page {page_num}",
                    step="fetch_list_page",
                    context={"url": list_url, "page": page_num},
                )
            break

        page_records = parse_list_page(html)
        logger.info("[Page %d] Parsed %d record(s)",
                    page_num, len(page_records))
        if not page_records:
            logger.info("[Page %d] Empty — stopping pagination", page_num)
            if page_num == 1 and error_items is not None:
                collect_error(
                    error_items,
                    "No records parsed from FNE listing page 1",
                    step="parse_list_page",
                    context={"url": list_url, "page": page_num},
                )
            break

        for rec in page_records:
            url = rec["url"]
            if url in seen_urls:
                logger.debug(
                    "  [Page %d] Duplicate URL in scrape — skip: %s", page_num, url)
                continue
            seen_urls.add(url)
            all_records.append(rec)
            logger.info(
                "  [Page %d] %s | %s | %s",
                page_num, rec.get("date") or rec.get(
                    "date_raw"), rec["title"][:80], url,
            )

        if page_num < max_pages:
            time.sleep(REQUEST_DELAY)

    # Listing is newest-first. Reverse so the last/oldest row is processed
    # first and the latest row last (live page 1 and backfill).
    if oldest_first:
        all_records.reverse()
        logger.info(
            "Total unique records scraped (oldest-first / last→first): %d",
            len(all_records),
        )
    else:
        logger.info("Total unique records scraped (listing order): %d",
                    len(all_records))
    return all_records


# ---------------------------------------------------------------------------
# LLM Prompt 1 — same-case linker
# ---------------------------------------------------------------------------

def _format_existing_records_text(records: List[Dict[str, Any]]) -> str:
    lines = []
    for rec in records:
        rid = str(rec["_id"])
        date = rec.get("date") or "N/A"
        title_en = rec.get("title_en") or rec.get("title") or "N/A"
        lines.append(f"Record ID: {rid} | Date: {date} | Title: {title_en}")
    return "\n".join(lines)


def llm_match_existing_record(
    title_en: str,
    existing_records: List[Dict[str, Any]],
) -> Optional[str]:
    """Return parent record _id string if new filing belongs to an existing case."""
    if not existing_records or not title_en:
        return None

    valid_ids = {str(r["_id"]) for r in existing_records}
    records_text = _format_existing_records_text(existing_records)

    prompt = f"""You are an expert at matching Chile FNE merger filings.

EXISTING RECORDS (is_new=true parent cases):
{records_text}

NEW RECORD (English title):
{title_en}

INSTRUCTIONS:
1. Determine whether the NEW RECORD refers to the SAME transaction/case as one of the EXISTING RECORDS.
2. Same transaction means the same parties (acquirer and target) even if the document type differs
   (e.g. Resolución vs Informe vs extensión vs inicio de investigación).
3. Do NOT match different transactions that merely involve similar company names.
4. If the new record clearly belongs to an existing case, respond EXACTLY: Match: RECORD_ID
5. If no existing record matches, respond exactly: None"""

    logger.info(
        "Calling LLM same-case linker (%d existing records) for: %.100s",
        len(existing_records), title_en,
    )
    raw = call_llm(prompt)
    logger.info("  Same-case linker raw LLM response: %s", (raw or "")[:300])
    record_id = parse_deal_id(raw)
    if record_id and record_id in valid_ids:
        logger.info("Same-case linker matched parent record_id=%s", record_id)
        return record_id
    if record_id:
        logger.warning(
            "Same-case linker returned invalid record id: %s (not in is_new set)",
            record_id,
        )
    else:
        logger.info("Same-case linker: no parent match (None)")
    return None


# ---------------------------------------------------------------------------
# Deal matching
# ---------------------------------------------------------------------------

def match_to_deal(
    title: str,
    title_en: str,
    open_deals: List[Dict[str, Any]],
    parent_record: Optional[Dict[str, Any]] = None,
    *,
    error_items: Optional[List[Dict[str, Any]]] = None,
    url: str = "",
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    LLM → regex deal match. Reuse parent deal_id when child match fails.

    Returns (deal_id, match_type, matched_by_regex).
    """
    matched_deal_id: Optional[str] = None
    match_type: Optional[str] = None
    matched_by_regex = False

    try:
        matched_deal_id = llm_match_deal_id(
            regulator_name="Chile FNE",
            case_sections={
                "TITLE (original Spanish)": title,
                "TITLE (English translation)": title_en,
            },
            source_label="the FNE filing title",
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
                context={"url": url, "title": title[:200]},
            )

    if not matched_deal_id:
        matched_deal_id = regex_match_chile_deal(title_en or title, open_deals)
        if matched_deal_id:
            match_type = "regex"
            matched_by_regex = True
            logger.info("  Deal match regex hit deal_id=%s", matched_deal_id)
        else:
            logger.info("  Deal match regex: no match")

    if not matched_deal_id and parent_record:
        parent_deal_id = parent_record.get("deal_id")
        if parent_deal_id:
            matched_deal_id = str(parent_deal_id)
            match_type = parent_record.get("match_type") or "parent"
            logger.info("  Reusing parent deal_id=%s", matched_deal_id)

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
                    context={"url": url, "title": title[:200], "deal_id": matched_deal_id},
                )
            return None, None, False

    return matched_deal_id, match_type, matched_by_regex


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(
    record: Dict[str, Any],
    deal_match: Optional[Dict[str, Any]],
    *,
    is_update: bool = False,
) -> Tuple[str, str]:
    event_type = "update" if is_update else "new"
    subject = build_subject("chile_fne", event_type, deal_match)

    title = record.get("title") or "N/A"
    title_en = record.get("title_en") or title
    date_val = record.get("date") or record.get("date_raw") or "N/A"
    url = record.get("url") or ""
    phase_en = record.get("phase_en") or PHASE_ES_TO_EN.get(
        record.get("phase") or "", "Other"
    )

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

    translation_row = ""
    if title_en.strip() and title_en.strip() != title.strip():
        translation_row = (
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">Title (EN):</td>'
            f'<td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(title_en)}</td></tr>'
        )

    doc_link = (
        f'<a href="{escape_html(url)}" target="_blank" '
        f'style="color:#0ea5e9;font-size:14px;font-weight:600;">View document &rarr;</a>'
        if url else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;">
  <h2 style="color:#333;margin-top:0;border-bottom:3px solid #dc2626;padding-bottom:12px;">
    {escape_html(subject)}
  </h2>
  {banner}
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Title (ES):</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(title)}</td>
    </tr>
    {translation_row}
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Phase:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(str(phase_en))}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Date:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(str(date_val))}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Document:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{doc_link}</td>
    </tr>
  </table>
  <p style="color:#999;font-size:12px;margin-top:24px;">
    Automated email — Chile FNE merger filings.
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
                logger.warning("N8N_WEBHOOK_ONLY_ME not set — test email skipped")
                if error_items is not None:
                    collect_error(
                        error_items,
                        "N8N_WEBHOOK_ONLY_ME not set — test email skipped",
                        step="send_email",
                        context={"url": payload.get("url"), "subject": subject},
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
    is_new_records: List[Dict[str, Any]],
    error_items: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
    test_mode: bool = False,
) -> str:
    """
    Full pipeline for one scraped row.

    Returns: skipped | new_matched | update_matched | new_usa | update_usa | new_no_email | update_no_email
    """
    url = (record.get("url") or "").strip()
    title = (record.get("title") or "").strip()

    if not url or not title:
        logger.warning(
            "  Skip — missing url or title: url=%s title=%s", url, title)
        collect_error(
            error_items,
            "Record missing url or title",
            step="process_record",
            context={"url": url, "title": title},
        )
        return "skipped"

    date_val = record.get("date") or record.get("date_raw") or "N/A"
    logger.info("-" * 50)
    logger.info(
        "  RECORD | date=%s | url=%s", date_val, url)
    logger.info("  Title (ES): %s", title)

    if url_exists(collection, url):
        logger.info("  Already in DB — skip")
        return "skipped"

    logger.info("  New — processing")

    title_en = translate_es_to_en(
        title, error_items=error_items, url=url, title=title
    )
    record["title_en"] = title_en or title
    translated = bool(title_en and title_en.strip() != title.strip())
    logger.info(
        "  Title (EN): %s | translated=%s",
        record["title_en"], translated,
    )

    phase_es, phase_en = extract_phase(title, url)
    record["phase"] = phase_es
    record["phase_en"] = phase_en
    logger.info("  Phase: %s | %s", phase_es, phase_en)

    now_iso = utc_now_iso()
    parent_id: Optional[str] = None
    parent_record: Optional[Dict[str, Any]] = None
    is_update = False

    try:
        parent_id = llm_match_existing_record(
            record["title_en"], is_new_records)
    except Exception as exc:
        logger.error("  Same-case linker error: %s", exc)
        collect_error(error_items, str(exc),
                      step="llm_match_existing_record", context={"url": url})

    if parent_id:
        parent_record = get_record_by_id(collection, parent_id)
        if parent_record:
            is_update = True
            logger.info(
                "  Linked to parent | parent_id=%s | parent_title=%s | parent_deal_id=%s",
                parent_id,
                (parent_record.get("title_en")
                 or parent_record.get("title") or "")[:80],
                parent_record.get("deal_id"),
            )
        else:
            logger.warning(
                "  Parent record %s not found — treating as new case", parent_id)
            parent_id = None
    else:
        logger.info("  No parent link — will insert as is_new=true")

    matched_deal_id: Optional[str] = None
    match_type: Optional[str] = None
    matched_by_regex = False

    # If this row is linked to an existing parent that already has a deal_id,
    # reuse it directly and skip LLM/regex matching.
    if is_update and parent_record and parent_record.get("deal_id"):
        matched_deal_id = str(parent_record.get("deal_id"))
        match_type = parent_record.get("match_type") or "parent"
        logger.info(
            "  Parent already has deal_id=%s — skipping deal matching",
            matched_deal_id,
        )
    else:
        matched_deal_id, match_type, matched_by_regex = match_to_deal(
            title,
            record["title_en"],
            open_deals,
            parent_record=parent_record,
            error_items=error_items,
            url=url,
        )

    deal_match: Optional[Dict[str, Any]] = None
    if matched_deal_id:
        deal_match = get_deal_by_id(matched_deal_id)
        if deal_match:
            logger.info(
                "  Deal resolved | deal_id=%s | match_type=%s | target=%s | acquirer=%s",
                matched_deal_id,
                match_type,
                deal_match.get("target") or deal_match.get("target_name"),
                deal_match.get("acquirer") or deal_match.get("acquire_name"),
            )
        else:
            # Keep matched_deal_id/match_type so parent.deal_id can still be set
            # (for future linked records), even if we can't load the full deal doc.
            logger.warning(
                "  deal_id=%s found from match (%s) but deal doc not found — will store deal_id but skip email",
                matched_deal_id,
                match_type,
            )
            collect_error(
                error_items,
                f"deal_id={matched_deal_id} matched but deal document not found",
                step="get_deal_by_id",
                context={
                    "url": url,
                    "title": title[:200],
                    "deal_id": matched_deal_id,
                    "match_type": match_type,
                },
            )
    else:
        logger.info("  No deal match after LLM + regex (+ parent reuse)")

    base_doc: Dict[str, Any] = {
        "url": url,
        "title": title,
        "title_en": record["title_en"],
        "date": record.get("date"),
        "date_raw": record.get("date_raw"),
        "phase": record.get("phase"),
        "phase_en": record.get("phase_en"),
        "deal_id": matched_deal_id,
        "match_type": match_type,
        "updated_at": now_iso,
    }

    outcome_prefix = "update" if is_update else "new"

    if dry_run:
        logger.info(
            "  [DRY-RUN] Would insert is_new=%s url=%s deal_id=%s match_type=%s phase=%s",
            not is_update, url, matched_deal_id, match_type, record.get(
                "phase_en"),
        )
        if deal_match:
            return f"{outcome_prefix}_matched"
        return f"{outcome_prefix}_no_email"

    inserted_id = None
    try:
        if is_update and parent_id and parent_record:
            # Parent-level deal_id is set only when we got an LLM match.
            if match_type == "llm" and matched_deal_id:
                collection.update_one(
                    {"_id": ObjectId(parent_id)},
                    {
                        "$set": {
                            "deal_id": matched_deal_id,
                            "match_type": "llm",
                            "updated_at": now_iso,
                        }
                    },
                )
                parent_record["deal_id"] = matched_deal_id
                parent_record["match_type"] = "llm"
                # Keep the in-memory is_new_records list in sync for the rest
                # of this run (so later linked filings can reuse it without
                # re-matching).
                for r in is_new_records:
                    try:
                        if str(r.get("_id")) == str(parent_id):
                            r["deal_id"] = matched_deal_id
                            r["match_type"] = "llm"
                            break
                    except Exception:
                        continue
                logger.info(
                    "  Parent deal_id set from LLM | parent_id=%s | deal_id=%s",
                    parent_id, matched_deal_id,
                )

            update_entry = {
                **base_doc,
                "created_at": now_iso,
            }
            collection.update_one(
                {"_id": ObjectId(parent_id)},
                {
                    "$push": {"updates": update_entry},
                    "$set": {"updated_at": now_iso},
                },
            )
            child_doc = {
                **base_doc,
                "is_new": False,
                "parent_record_id": ObjectId(parent_id),
                "created_at": now_iso,
            }
            result = collection.insert_one(child_doc)
            inserted_id = result.inserted_id
            logger.info(
                "  DB insert child | _id=%s | is_new=false | parent_id=%s | deal_id=%s",
                inserted_id, parent_id, matched_deal_id,
            )
        else:
            # For is_new=true parent rows, persist deal_id only when match came from LLM.
            parent_deal_id = matched_deal_id if match_type == "llm" else None
            parent_match_type = "llm" if match_type == "llm" else None
            parent_doc = {
                **base_doc,
                "deal_id": parent_deal_id,
                "match_type": parent_match_type,
                "is_new": True,
                "parent_record_id": None,
                "updates": [],
                "created_at": now_iso,
            }
            result = collection.insert_one(parent_doc)
            inserted_id = result.inserted_id
            is_new_records.append({**parent_doc, "_id": inserted_id})
            logger.info(
                "  DB insert parent | _id=%s | is_new=true | deal_id=%s",
                inserted_id, matched_deal_id,
            )

    except Exception as exc:
        logger.error("  DB insert/update failed: %s", exc)
        collect_error(error_items, str(exc),
                      step="insert_record", context={"url": url})
        return "skipped"

    if deal_match:
        subject, html = build_email_html(
            record, deal_match, is_update=is_update)
        if matched_by_regex:
            subject = apply_regex_match_subject(subject, True)
        payload: Dict[str, Any] = {
            "subject": subject,
            "html": html,
            "url": url,
            "source": "chile_fne",
            "is_new_case": not is_update,
            "deal_id": matched_deal_id,
            "phase": record.get("phase"),
            "phase_en": record.get("phase_en"),
        }
        _send_email(payload, subject, test_mode, error_items=error_items)
        logger.info(
            "  EMAIL sent | kind=deal_match | is_new=%s | deal_id=%s | subject=%s",
            not is_update, matched_deal_id, subject,
        )
        logger.info("  OUTCOME: %s_matched", outcome_prefix)
        return f"{outcome_prefix}_matched"

    # Deal ID was matched, but we couldn't load the deal document details.
    # Skip USA check/email so we don't send a wrong [FRUD] for an actually-matched deal.
    if matched_deal_id and not deal_match:
        logger.info(
            "  OUTCOME: %s_no_email (deal_id=%s matched, deal doc missing)",
            outcome_prefix,
            matched_deal_id,
        )
        return f"{outcome_prefix}_no_email"

    is_usa = False
    try:
        is_usa = bool(
            verify_usa_relation(
                company_details=record["title_en"],
                case_type="CHILE_FNE",
            )
        )
        logger.info("  USA check: %s", is_usa)
    except Exception as exc:
        logger.error("  USA check error: %s", exc)
        collect_error(
            error_items,
            str(exc),
            step="verify_usa_relation",
            context={"url": url, "title": title[:200]},
        )

    if is_usa:
        subject, html = build_email_html(record, None, is_update=is_update)
        payload = {
            "subject": subject,
            "html": html,
            "url": url,
            "source": "chile_fne",
            "is_new_case": not is_update,
            "is_unmatched": True,
            "phase": record.get("phase"),
            "phase_en": record.get("phase_en"),
        }
        _send_email(payload, subject, test_mode, error_items=error_items)
        logger.info(
            "  EMAIL sent | kind=usa_unmatched | is_new=%s | subject=%s",
            not is_update, subject,
        )
        logger.info("  OUTCOME: %s_usa", outcome_prefix)
        return f"{outcome_prefix}_usa"

    logger.info(
        "  OUTCOME: %s_no_email | _id=%s | deal_id=%s | usa=False",
        outcome_prefix, inserted_id, matched_deal_id,
    )
    return f"{outcome_prefix}_no_email"


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_chile_fne_cases_register(
    backfill: bool = False,
    dry_run: bool = False,
    test_mode: bool = False,
) -> Dict[str, int]:
    refresh_script_log(logger, _get_log_file)

    max_pages = MAX_PAGES_BACKFILL if backfill else MAX_PAGES_LIVE
    # Backfill emails go only to TEST_RECIPIENT, never org routing.
    if backfill:
        test_mode = True
    run_start = datetime.datetime.now()
    error_items: List[Dict[str, Any]] = []

    stats: Dict[str, int] = {
        "records_scraped": 0,
        "records_skipped": 0,
        "new_matched": 0,
        "new_usa": 0,
        "new_no_email": 0,
        "update_matched": 0,
        "update_usa": 0,
        "update_no_email": 0,
    }

    logger.info("=" * 60)
    logger.info(
        "START: Chile FNE Scraper | dry_run=%s | test_email=%s | backfill=%s | max_pages=%d",
        dry_run, test_mode, backfill, max_pages,
    )
    if backfill:
        logger.info(
            "BACKFILL: emails → %s only (test mode)", TEST_RECIPIENT)
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
            collect_error(error_items, "Could not get collection",
                          step="get_collection")
            return stats
        ensure_indexes(collection)

        open_deals = fetch_open_deals()
        logger.info("Loaded %d open deals", len(open_deals))

        is_new_records = fetch_is_new_records(collection)
        logger.info("Loaded %d is_new=true parent records",
                    len(is_new_records))

        records = scrape_all_pages(
            max_pages, oldest_first=True, error_items=error_items
        )
        stats["records_scraped"] = len(records)
        logger.info("Processing %d record(s) oldest-first", len(records))

        for idx, record in enumerate(records, start=1):
            logger.info(
                "==== [%d/%d] %s ====",
                idx, len(records), (record.get("title") or "")[:80],
            )
            try:
                result = process_record(
                    record,
                    collection,
                    open_deals,
                    is_new_records,
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
                        "url": record.get("url"),
                        "title": (record.get("title") or "")[:200],
                    },
                )
                result = "skipped"
            stat_key = {
                "skipped": "records_skipped",
                "new_matched": "new_matched",
                "new_usa": "new_usa",
                "new_no_email": "new_no_email",
                "update_matched": "update_matched",
                "update_usa": "update_usa",
                "update_no_email": "update_no_email",
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
        elapsed = round((datetime.datetime.now() -
                        run_start).total_seconds(), 1)
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
        description="Chile FNE merger filings → chili_cases")
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
    run_chile_fne_cases_register(
        backfill=args.backfill,
        dry_run=args.dry_run,
        test_mode=args.test_email,
    )
