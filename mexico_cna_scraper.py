"""
mexico_cna_scraper.py
=====================
Scraper for Mexico's Comisión Nacional Antimonopolio (CNA) plenary session agendas.

Source URL:
  https://www.gob.mx/antimonopolio/es/archivo/articulos
  ?category=992&filter_origin=archive&idiom=es&order=DESC&page=N

Pipeline:
  1. Fetch paginated list of plenary session articles
  2. Skip "Extraordinaria" / "Excepcional" sessions — keep "ordinaria" only
  3. Fetch each new session's detail page and parse all cases (any Asunto)
  4. Skip cases where Agentes is "Reservado" (no party info available)
  5. Dedup against mexico_cna_cases MongoDB collection by expediente
  6. Translate agentes (Spanish → English)
  7. LLM deal match → regex fallback [FRRMD] → USA check [FRUD] → skip
  8. Insert into DB and send email

Usage:
  python mexico_cna_scraper.py              # live run
  python mexico_cna_scraper.py --dry-run    # scrape only, no DB writes or emails
  python mexico_cna_scraper.py --test-email --backfill  # Test-email + backfill (5 pages)

"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
from datetime import timezone
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from deal_match_llm import llm_match_deal_id, fetch_open_deals
from deal_match_regex import regex_match_flat_scan
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_NAME = "mexico_cna_scraper"

LIST_URL_TEMPLATE = (
    "https://www.gob.mx/antimonopolio/es/archivo/articulos"
    "?category=992&filter_origin=archive&idiom=es&order=DESC&page={page}"
)
DETAIL_BASE = "https://www.gob.mx"
COLLECTION_NAME = "mexico_cna_cases"

MAX_PAGES_LIVE = 1          # normal scheduled run — only latest page
MAX_PAGES_BACKFILL = 5      # --backfill run — historical fill
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30    # seconds
REQUEST_DELAY = 1.5     # seconds between HTTP requests (be polite)

# gob.mx/Akamai 403s scraper UAs; curl-style UA returns the real payload.
HTTP_HEADERS = {
    "User-Agent": "curl/8.4.0",
    "Accept": "*/*",
}
_http_session = requests.Session()
_http_session.headers.update(HTTP_HEADERS)

SESSION_EXCLUDE_KEYWORDS = ("extraordinaria", "excepcional")

# Test-mode email — used with --test-email flag before going live
TEST_RECIPIENT = "avshesh.savani@teqnodux.com"

# Spanish + common English corporate-suffix strip for regex matching
MEXICO_SUFFIXES = re.compile(
    r"\b(s\.?\s*a\.?\s*p\.?\s*i\.?|s\.?\s*a\.?\s*b\.?|s\.?\s*de\s*r\.?\s*l\.?|"
    r"de\s*c\.?\s*v\.?|s\.?\s*a\.?|a\.?\s*c\.?|s\.?\s*c\.?|"
    r"inc|incorporated|corp|corporation|plc|llc|lp|l\.p|ltd|limited|"
    r"holdings|group|co|company|nv|ag|se|gmbh|spa|sa|"
    r"trust|fund|partners|foundation|pbc|pty)\b",
    re.IGNORECASE,
)

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


def translate_es_to_en(text: str) -> str:
    """
    Translate Spanish text to English via Google Translate (free endpoint).
    Returns the original text on failure or if text is empty.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return text
    if len(text) > 1000:
        logger.warning(
            "  Translation skipped: text too long (%d chars)", len(text))
        return text
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "es",
                  "tl": "en", "dt": "t", "q": text}
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code == 200:
            return resp.json()[0][0][0]
        logger.warning("  Translation HTTP %d for: %s",
                       resp.status_code, text[:60])
    except requests.Timeout:
        logger.warning("  Translation timeout for: %s", text[:60])
    except Exception as exc:
        logger.warning("  Translation failed: %s", exc)
    return text


def get_collection():
    db = get_database()
    if db is None:
        return None
    return db[COLLECTION_NAME]


def ensure_indexes(collection) -> None:
    try:
        collection.create_index(
            "expediente", unique=True, name="expediente_unique"
        )
        logger.info("Index ensured on mexico_cna_cases.expediente")
    except Exception as exc:
        logger.warning("Could not ensure index: %s", exc)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> Optional[str]:
    """GET a URL with retry logic; returns raw HTML text or None on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("  GET %s (attempt %d/%d)", url, attempt, MAX_RETRIES)
            resp = _http_session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.warning("  Request failed (attempt %d/%d): %s",
                           attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(5)
    return None


# ---------------------------------------------------------------------------
# List page parsing
# ---------------------------------------------------------------------------

def _parse_pub_date(date_attr: str) -> Optional[datetime.date]:
    """Convert '2026-07-14 17:22:00' → date(2026, 7, 14)."""
    if not date_attr:
        return None
    try:
        return datetime.datetime.strptime(date_attr.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_session_excluded(title: str) -> bool:
    """Return True for Extraordinaria / Excepcional sessions."""
    lower = title.lower()
    return any(kw in lower for kw in SESSION_EXCLUDE_KEYWORDS)


def _extract_html_from_js(js_text: str) -> str:
    """
    The CNA list endpoint returns JavaScript, not HTML.
    Each row of articles is injected via:  $('#prensa').append('...html...');
    This pulls all HTML fragments out of those calls and joins them.
    """
    matches = re.findall(
        r"\$\('#prensa'\)\.append\('(.*?)'\s*\);",
        js_text,
        re.DOTALL,
    )
    if not matches:
        return js_text  # fallback: treat as plain HTML
    fragments = [m.replace('\\"', '"').replace('\\/', '/') for m in matches]
    return "\n".join(fragments)


def parse_list_page(html: str) -> List[Dict[str, Any]]:
    """
    Extract plenary session entries from a CNA list-page response.
    The endpoint returns JavaScript with jQuery append() calls — extract
    the embedded HTML first, then parse with BeautifulSoup.
    Returns [{title, detail_url, pub_date}, ...].
    """
    if "$('#prensa').append(" in html:
        html = _extract_html_from_js(html)

    soup = BeautifulSoup(html, "html.parser")
    sessions: List[Dict[str, Any]] = []

    for article in soup.select("article"):
        h2 = article.find("h2")
        if not h2:
            continue
        title = h2.get_text(strip=True)

        link = article.find("a", class_="small-link")
        if not link or not link.get("href"):
            continue
        href = link["href"].strip()
        if not href.startswith("http"):
            href = DETAIL_BASE + href

        time_tag = article.find("time")
        pub_date = _parse_pub_date(
            time_tag.get("date", "") if time_tag else "")

        sessions.append(
            {"title": title, "detail_url": href, "pub_date": pub_date})

    return sessions


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------
_SEPARATOR_RE = re.compile(r"^[_\s]{5,}$")
_ASUNTO_RE = re.compile(r"^Asunto:\s*(.+)", re.IGNORECASE)
_EXPEDIENTE_RE = re.compile(r"^Expediente:\s*(.+)", re.IGNORECASE)
_AGENTES_RE = re.compile(r"^Agentes:\s*(.+)", re.IGNORECASE)
_FECHA_RE = re.compile(r"^Fecha de Publicaci[oó]n:\s*(.+)", re.IGNORECASE)
# Agentes values that indicate no matchable party names — save to DB without matching
_NO_PARTY_RE = re.compile(
    r"^(Reservado|INFORMACIÓN IDENTIFICADA COMO CONFIDENCIAL|EN PREVENCIÓN DE CONFIDENCIALIDAD)",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_detail_page(
    html: str, session_title: str, session_url: str
) -> List[Dict[str, Any]]:
    """
    Parse the session detail page and extract individual cases.
    The body is a series of <p> blocks separated by underscore lines,
    each block containing Asunto, Expediente, Agentes, Fecha fields.
    Returns a list of case dicts.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("div", class_="article-body")
    if not body:
        logger.warning("  No article-body on: %s", session_url)
        return []

    cases: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    for p in body.find_all("p"):
        text = _clean(p.get_text(separator=" "))

        if _SEPARATOR_RE.match(text):
            if current.get("expediente"):
                cases.append(current)
            current = {}
            continue

        m = _ASUNTO_RE.match(text)
        if m:
            current["asunto"] = _clean(m.group(1))
            continue

        m = _EXPEDIENTE_RE.match(text)
        if m:
            current["expediente"] = _clean(m.group(1))
            continue

        m = _AGENTES_RE.match(text)
        if m:
            current["agentes"] = _clean(m.group(1))
            continue

        m = _FECHA_RE.match(text)
        if m:
            current["fecha_publicacion"] = _clean(m.group(1))
            continue

    # Flush last block if it has an expediente
    if current.get("expediente"):
        cases.append(current)

    for case in cases:
        case["session_title"] = session_title
        case["session_url"] = session_url

    logger.info("  Parsed %d case(s)", len(cases))
    return cases


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def case_exists(collection, expediente: str) -> bool:
    return collection.find_one({"expediente": expediente}, {"_id": 1}) is not None


def insert_case(collection, doc: Dict[str, Any]) -> bool:
    try:
        collection.insert_one(doc)
        logger.info("  Inserted: %s", doc.get("expediente"))
        return True
    except Exception as exc:
        logger.error("  Insert failed for %s: %s", doc.get("expediente"), exc)
        return False


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def build_email_html(
    case: Dict[str, Any],
    deal_match: Optional[Dict[str, Any]],
    agentes_en: str,
) -> Tuple[str, str]:
    subject = build_subject("mexico_cna", "new", deal_match)
    expediente = case.get("expediente", "N/A")
    asunto = case.get("asunto", "N/A")
    agentes_es = case.get("agentes", "N/A")
    fecha = case.get("fecha_publicacion", "N/A")
    session_title = case.get("session_title", "N/A")
    session_url = case.get("session_url", "")

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

    session_link_html = (
        f'<a href="{escape_html(session_url)}" target="_blank" '
        f'style="color:#0ea5e9;font-size:14px;font-weight:600;">View session page &rarr;</a>'
        if session_url else ""
    )

    # Only show English translation row when it differs from the original
    translation_row = ""
    if agentes_en and agentes_en.strip() and agentes_en.strip() != agentes_es.strip():
        translation_row = (
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">Parties (EN):</td>'
            f'<td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(agentes_en)}</td></tr>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;">

  <h2 style="color:#333;margin-top:0;border-bottom:3px solid #dc2626;padding-bottom:12px;">
    {escape_html(subject)}
  </h2>

  <div style="background:#f1f5f9;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #64748b;">

    <strong>Session:</strong> {escape_html(session_title)}<br>
    {session_link_html}
  </div>

  {banner}

  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Expediente:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(expediente)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Subject:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(asunto)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Parties (ES):</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(agentes_es)}</td>
    </tr>
    {translation_row}
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Publication Date:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(fecha)}</td>
    </tr>
  </table>

  <p style="color:#999;font-size:12px;margin-top:24px;">
    Automated email — Mexico CNA plenary.
  </p>
</div>
</body>
</html>"""
    return subject, html


# ---------------------------------------------------------------------------
# Email dispatch helper
# ---------------------------------------------------------------------------

def _send_email(
    payload: Dict[str, Any],
    subject: str,
    test_mode: bool,
) -> bool:
    """
    Route email to the right destination:
      test_mode=True  → direct send to TEST_RECIPIENT via N8N_WEBHOOK_ONLY_ME
      test_mode=False → normal org-aware routing via post_email_payload
    """
    if test_mode:
        webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
        if not webhook_url:
            logger.warning(
                "  N8N_WEBHOOK_ONLY_ME not set in .env — test email skipped")
            return False
        logger.info(
            "  [TEST] Sending to %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
        return send_direct_email([TEST_RECIPIENT], payload, webhook_url=webhook_url)
    return post_email_payload(payload, subject=subject)


# ---------------------------------------------------------------------------
# Per-case processing
# ---------------------------------------------------------------------------

def process_case(
    case: Dict[str, Any],
    collection,
    open_deals: List[Dict[str, Any]],
    error_items: List[Dict[str, Any]],
    dry_run: bool = False,
    test_mode: bool = False,
) -> str:
    """
    Full pipeline for a single case record:
      dedup → translate → LLM → regex [FRRMD] → USA [FRUD] → DB + email

    test_mode=True  → email goes to TEST_RECIPIENT via N8N_WEBHOOK_ONLY_ME
    test_mode=False → normal org-aware routing

    Returns one of: "skipped" | "new_matched" | "new_usa" | "new_no_email"
    """
    expediente = (case.get("expediente") or "").strip()
    agentes_es = (case.get("agentes") or "").strip()

    if not expediente:
        logger.info("  Case missing expediente — skipping")
        return "skipped"

    # Skip only when agentes is completely absent
    if not agentes_es:
        logger.info("  [%s] No agentes value — skipping", expediente)
        return "skipped"

    # Dedup check
    if case_exists(collection, expediente):
        logger.info("  [%s] Already in DB — skipping", expediente)
        return "skipped"

    logger.info(
        "  [%s] New | Asunto: %s | Agentes: %.120s",
        expediente,
        case.get("asunto", "N/A"),
        agentes_es,
    )

    # No matchable party names — translate then save directly to DB, skip LLM/USA/email
    if _NO_PARTY_RE.match(agentes_es):
        agentes_en = agentes_es
        try:
            translated = translate_es_to_en(agentes_es)
            if translated and translated.strip():
                agentes_en = translated
                if agentes_en != agentes_es:
                    logger.info("  [%s] Translated: %.150s",
                                expediente, agentes_en)
        except Exception as exc:
            logger.warning("  [%s] Translation error: %s", expediente, exc)

        logger.info(
            "  [%s] No party names available — saving silently", expediente)
        if not dry_run:
            insert_case(collection, {
                "expediente": expediente,
                "asunto": case.get("asunto"),
                "agentes": agentes_es,
                "agentes_en": agentes_en,
                "fecha_publicacion": case.get("fecha_publicacion"),
                "session_title": case.get("session_title"),
                "session_url": case.get("session_url"),
                "deal_id": None,
                "match_type": None,
                "is_open": True,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            })
        else:
            logger.info(
                "  [DRY-RUN] Would insert (no party info) for %s", expediente)
        return "new_no_email"

    # -----------------------------------------------------------------------
    # Translate agentes Spanish → English
    # -----------------------------------------------------------------------
    agentes_en = agentes_es
    if agentes_es:
        try:
            translated = translate_es_to_en(agentes_es)
            if translated and translated.strip():
                agentes_en = translated
                if agentes_en != agentes_es:
                    logger.info("  [%s] Translated: %.150s",
                                expediente, agentes_en)
        except Exception as exc:
            logger.warning("  [%s] Translation error: %s", expediente, exc)

    now_iso = utc_now_iso()
    matched_deal_id: Optional[str] = None
    match_type: Optional[str] = None  # "llm" | "regex"

    # -----------------------------------------------------------------------
    # Step 1 — LLM deal match
    # -----------------------------------------------------------------------
    try:
        matched_deal_id = llm_match_deal_id(
            regulator_name="Mexico CNA",
            case_sections={
                "EXPEDIENTE": expediente,
                "ASUNTO": case.get("asunto") or "",
                "AGENTES (original Spanish)": agentes_es,
                "AGENTES (English translation)": agentes_en,
            },
            source_label="the agentes (parties) text",
            deals=open_deals,
        )
        if matched_deal_id:
            match_type = "llm"
            logger.info("  [%s] LLM matched deal_id=%s",
                        expediente, matched_deal_id)
    except Exception as exc:
        logger.error("  [%s] LLM match error: %s", expediente, exc)
        collect_error(
            error_items,
            str(exc),
            step="llm_match_deal_id",
            context={"expediente": expediente},
        )

    # -----------------------------------------------------------------------
    # Step 2 — Regex fallback [FRRMD]
    # -----------------------------------------------------------------------
    if not matched_deal_id:
        try:
            matched_deal_id = regex_match_flat_scan(
                agentes_en, open_deals, suffixes=MEXICO_SUFFIXES
            )
            if matched_deal_id:
                match_type = "regex"
                logger.info(
                    "  [%s] Regex matched deal_id=%s", expediente, matched_deal_id
                )
        except Exception as exc:
            logger.warning("  [%s] Regex match error: %s", expediente, exc)

    # Resolve deal document
    deal_match: Optional[Dict[str, Any]] = None
    if matched_deal_id:
        deal_match = get_deal_by_id(matched_deal_id)
        if not deal_match:
            logger.warning(
                "  [%s] deal_id=%s not found in deals collection",
                expediente,
                matched_deal_id,
            )
            matched_deal_id = None
            match_type = None

    # -----------------------------------------------------------------------
    # Build the DB document (common for all outcomes)
    # -----------------------------------------------------------------------
    doc: Dict[str, Any] = {
        "expediente": expediente,
        "asunto": case.get("asunto"),
        "agentes": agentes_es,
        "agentes_en": agentes_en,
        "fecha_publicacion": case.get("fecha_publicacion"),
        "session_title": case.get("session_title"),
        "session_url": case.get("session_url"),
        "deal_id": matched_deal_id,
        "match_type": match_type,
        "is_open": True,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    # -----------------------------------------------------------------------
    # Matched deal path → [FRMD] or [FRRMD]
    # -----------------------------------------------------------------------
    if deal_match:
        subject, html = build_email_html(case, deal_match, agentes_en)
        if match_type == "regex":
            subject = subject.replace("[FRMD]", "[FRRMD]")

        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        logger.info(
            "  [%s] %s match: %s / %s",
            expediente, match_type.upper(), target, acquirer,
        )

        if not dry_run:
            if not insert_case(collection, doc):
                collect_error(
                    error_items,
                    "DB insert failed",
                    step="insert_case",
                    context={"expediente": expediente},
                )
                return "skipped"
            payload: Dict[str, Any] = {
                "subject": subject,
                "html": html,
                "expediente": expediente,
                "source": "mexico_cna",
                "is_new_case": True,
                "deal_id": matched_deal_id,
            }
            _send_email(payload, subject, test_mode)
        else:
            logger.info(
                "  [DRY-RUN] Would insert + send %s for %s",
                subject.split(" - ")[-1].strip(),
                expediente,
            )

        return "new_matched"

    # -----------------------------------------------------------------------
    # Step 3 — USA relation check → [FRUD]
    # -----------------------------------------------------------------------
    is_usa = False
    try:
        is_usa = bool(
            verify_usa_relation(company_details=agentes_en,
                                case_type="MEXICO_CNA")
        )
        logger.info("  [%s] USA check: %s", expediente, is_usa)
    except Exception as exc:
        logger.error("  [%s] USA check error: %s", expediente, exc)
        collect_error(
            error_items,
            str(exc),
            step="verify_usa_relation",
            context={"expediente": expediente},
        )

    if is_usa:
        subject, html = build_email_html(case, None, agentes_en)

        if not dry_run:
            if not insert_case(collection, doc):
                collect_error(
                    error_items,
                    "DB insert failed",
                    step="insert_case",
                    context={"expediente": expediente},
                )
                return "skipped"
            payload = {
                "subject": subject,
                "html": html,
                "expediente": expediente,
                "source": "mexico_cna",
                "is_new_case": True,
                "is_unmatched": True,
            }
            _send_email(payload, subject, test_mode)
        else:
            logger.info(
                "  [DRY-RUN] Would insert + send [FRUD] for %s", expediente)

        return "new_usa"

    # -----------------------------------------------------------------------
    # No match, not USA-related — save to DB but no email
    # -----------------------------------------------------------------------
    logger.info(
        "  [%s] No match, not USA-related — saving silently", expediente)
    if not dry_run:
        insert_case(collection, doc)
    else:
        logger.info("  [DRY-RUN] Would insert (no email) for %s", expediente)

    return "new_no_email"


# ---------------------------------------------------------------------------
# Core run function (called by Flask endpoint and by main())
# ---------------------------------------------------------------------------

def run_mexico_cna_scraper(
    backfill: bool = False,
    dry_run: bool = False,
    test_mode: bool = False,
) -> Dict[str, Any]:
    """
    Execute the Mexico CNA scraper.

    Args:
        backfill:  True → fetch up to MAX_PAGES_BACKFILL pages.
        dry_run:   True → no DB writes, no emails.
        test_mode: True → emails to TEST_RECIPIENT via N8N_WEBHOOK_ONLY_ME.

    Returns:
        stats dict with run summary.
    """
    refresh_script_log(logger, _get_log_file)

    max_pages = MAX_PAGES_BACKFILL if backfill else MAX_PAGES_LIVE
    run_start = datetime.datetime.now()
    error_items: List[Dict[str, Any]] = []

    stats: Dict[str, int] = {
        "sessions_found": 0,
        "sessions_excluded": 0,
        "sessions_skipped_db": 0,
        "sessions_processed": 0,
        "cases_found": 0,
        "cases_skipped": 0,
        "cases_matched": 0,
        "cases_usa": 0,
        "cases_no_email": 0,
    }

    logger.info("=" * 60)
    logger.info(
        "START: Mexico CNA Scraper | dry_run=%s | test_email=%s | backfill=%s | max_pages=%d",
        dry_run, test_mode, backfill, max_pages,
    )
    if dry_run:
        logger.info("DRY-RUN: DB writes and emails are suppressed")
    if test_mode:
        logger.info(
            "TEST-EMAIL: emails → %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
    logger.info("=" * 60)

    try:
        # --- MongoDB ---
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

        # --- Load open deals once ---
        logger.info("Loading open deals from MongoDB...")
        open_deals = fetch_open_deals()
        logger.info("Loaded %d open deals", len(open_deals))

        # --- Paginate list pages ---
        for page_num in range(1, max_pages + 1):
            list_url = LIST_URL_TEMPLATE.format(page=page_num)
            logger.info("[Page %d] Fetching: %s", page_num, list_url)

            html = fetch_html(list_url)
            if not html:
                collect_error(
                    error_items,
                    f"Failed to fetch list page {page_num}",
                    step="fetch_list_page",
                    context={"url": list_url, "page": page_num},
                )
                break

            sessions = parse_list_page(html)
            logger.info("[Page %d] Found %d session(s)",
                        page_num, len(sessions))

            if not sessions:
                logger.info("[Page %d] Empty — stopping pagination", page_num)
                break

            stats["sessions_found"] += len(sessions)

            for session in sessions:
                title = session["title"]
                detail_url = session["detail_url"]

                # Skip Extraordinaria / Excepcional
                if is_session_excluded(title):
                    logger.info("  Excluded: %s", title[:80])
                    stats["sessions_excluded"] += 1
                    continue

                # Session-level dedup — skip detail fetch if records exist
                if not dry_run and collection.find_one(
                    {"session_url": detail_url}, {"_id": 1}
                ):
                    logger.info(
                        "  Session already in DB — skipping: %s", title[:70]
                    )
                    stats["sessions_skipped_db"] += 1
                    continue

                logger.info("  Processing: %s", title[:80])
                stats["sessions_processed"] += 1

                time.sleep(REQUEST_DELAY)
                detail_html = fetch_html(detail_url)
                if not detail_html:
                    collect_error(
                        error_items,
                        f"Failed to fetch detail page: {title}",
                        step="fetch_detail_page",
                        context={"url": detail_url},
                    )
                    continue

                cases = parse_detail_page(detail_html, title, detail_url)
                stats["cases_found"] += len(cases)

                for case in cases:
                    result = process_case(
                        case, collection, open_deals, error_items,
                        dry_run=dry_run, test_mode=test_mode,
                    )
                    stats[{
                        "skipped":      "cases_skipped",
                        "new_matched":  "cases_matched",
                        "new_usa":      "cases_usa",
                        "new_no_email": "cases_no_email",
                    }.get(result, "cases_skipped")] += 1

            time.sleep(REQUEST_DELAY)

    except Exception as exc:
        logger.exception("Unhandled error: %s", exc)
        collect_error(error_items, f"Unhandled error: {exc}", step="main")

    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round((datetime.datetime.now() -
                        run_start).total_seconds(), 1)

        logger.info("=" * 60)
        logger.info("SUMMARY  [mode: %s | pages: %d]",
                    "backfill" if backfill else "live", max_pages)
        logger.info("  Sessions found         : %d", stats["sessions_found"])
        logger.info("  Sessions excluded      : %d (Extraordinaria/Excepcional)",
                    stats["sessions_excluded"])
        logger.info("  Sessions skipped (DB)  : %d",
                    stats["sessions_skipped_db"])
        logger.info("  Sessions processed     : %d",
                    stats["sessions_processed"])
        logger.info("  Cases found            : %d", stats["cases_found"])
        logger.info("  Cases skipped (dup/res): %d", stats["cases_skipped"])
        logger.info("  Cases matched [FRMD/FRRMD]: %d", stats["cases_matched"])
        logger.info("  Cases USA [FRUD]       : %d", stats["cases_usa"])
        logger.info("  Cases no email         : %d", stats["cases_no_email"])
        logger.info("  Errors                 : %d", len(error_items))
        logger.info("  Total time             : %ss", elapsed)
        logger.info("=" * 60)

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mexico CNA plenary session scraper")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scrape + parse only — no DB writes, no emails",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help=f"Fetch up to {MAX_PAGES_BACKFILL} pages to fill historical records",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help=f"Send emails to {TEST_RECIPIENT} via N8N_WEBHOOK_ONLY_ME",
    )
    args = parser.parse_args()
    run_mexico_cna_scraper(
        backfill=args.backfill,
        dry_run=args.dry_run,
        test_mode=args.test_email,
    )


if __name__ == "__main__":
    main()
