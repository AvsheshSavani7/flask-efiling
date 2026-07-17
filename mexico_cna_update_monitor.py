"""
mexico_cna_update_monitor.py
=============================
Update monitor for Mexico CNA concentration cases.

For each record in mexico_cna_cases where is_open=True:
  1. POST to resoluciones.antimonopolio.gob.mx with the expediente number
  2. Check Tbl_Concentraciones for any result row
  3. If 0 results → still pending, skip
  4. If result found:
       → extract companies, decision, dates, subsector, PDF URL
       → translate companies Spanish → English
       → update DB (companies_resolved, decision, dates, is_open=False, ...)
       → email:
           deal_id already set → send [FRMD] update email
           else → LLM match → regex [FRRMD] → USA check [FRUD] → silent

Concurrency:
  All is_open=True records are fetched at once. A ThreadPoolExecutor
  processes them in parallel — each worker owns its own requests.Session()
  so ViewState and cookies never collide between workers.

Usage:
  python mexico_cna_update_monitor.py                      # live run
  python mexico_cna_update_monitor.py --dry-run            # no DB writes, no emails
  python mexico_cna_update_monitor.py --test-email         # emails to TEST_RECIPIENT only
  python mexico_cna_update_monitor.py --max-workers 5      # more parallel workers
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import threading
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Suppress SSL warnings for the portal (self-signed / untrusted CA)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_NAME = "mexico_cna_update_monitor"
PORTAL_URL = "https://resoluciones.antimonopolio.gob.mx/"
COLLECTION_NAME = "mexico_cna_cases"
DEFAULT_MAX_WORKERS = 3   # parallel portal lookups (each owns its own Session)
REQUEST_DELAY = 2.0       # seconds between GET and POST within one lookup
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

TEST_RECIPIENT = "avshesh.savani@teqnodux.com"

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
    """Translate Spanish text to English via Google Translate free endpoint."""
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


# ---------------------------------------------------------------------------
# Portal scraping (ASP.NET WebForms POST)
# ---------------------------------------------------------------------------

def _session_headers() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (compatible; MexicoCNA-Monitor/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _extract_viewstate(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract ASP.NET hidden form fields required for POST."""
    fields = {}
    for field_id in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", {"id": field_id})
        if el:
            fields[field_id] = el.get("value", "")
    return fields


def lookup_expediente(
    http_session: requests.Session,
    expediente: str,
) -> Optional[Dict[str, Any]]:
    """
    Search the resolution portal for a given expediente.

    Returns a dict with the first Concentrations result row, or None if
    no results found or on any error.

    Dict keys:
        expediente, companies, start_date, resolution_date,
        decision, subsector, public_version_url
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Step 1: GET to capture ViewState
            resp = http_session.get(
                PORTAL_URL, timeout=REQUEST_TIMEOUT, verify=False,
                headers=_session_headers(),
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            viewstate = _extract_viewstate(soup)

            if not viewstate.get("__VIEWSTATE"):
                logger.warning(
                    "  [%s] Missing __VIEWSTATE (attempt %d/%d)",
                    expediente, attempt, MAX_RETRIES,
                )
                time.sleep(3)
                continue

            # Step 2: POST with the expediente number
            post_data = {
                **viewstate,
                "__EVENTTARGET": "LB_Buscar",
                "__EVENTARGUMENT": "",
                "txt_buscar": "",
                "customRadio": "rb_cualquier1",
                "txt_expediente": expediente.strip().upper(),
                "txt_empresa": "",
                "ddl_sector1": "0",
                "ddl_subsector1": "0",
                "ddl_rama": "0",
                "txt_del_1": "",
                "txt_al_1": "",
            }
            r = http_session.post(
                PORTAL_URL, data=post_data, timeout=REQUEST_TIMEOUT, verify=False,
                headers={
                    **_session_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            r.raise_for_status()

            # Step 3: Parse results
            soup2 = BeautifulSoup(r.text, "html.parser")

            # Quick count check
            count_el = soup2.find("span", {"id": "Lbl_Num_Concentraciones"})
            count_str = (count_el.get_text(strip=True)
                         if count_el else "0") or "0"
            try:
                count = int(count_str)
            except ValueError:
                count = 0

            if count == 0:
                logger.info(
                    "  [%s] Portal: 0 results — still pending", expediente)
                return None

            # Parse the Concentrations results table
            tbl = soup2.find("table", {"id": "Tbl_Concentraciones"})
            if not tbl:
                logger.warning(
                    "  [%s] Tbl_Concentraciones not found in response", expediente)
                return None

            rows = tbl.find_all("tr")
            # Row 0 = header, Row 1+ = data
            data_rows = [r for r in rows[1:] if r.find_all("td")]
            if not data_rows:
                logger.info(
                    "  [%s] Portal: no data rows — still pending", expediente)
                return None

            first = data_rows[0]
            tds = first.find_all("td")

            def _cell(idx: int) -> str:
                if idx < len(tds):
                    return tds[idx].get_text(strip=True)
                return ""

            def _cell_link(idx: int) -> str:
                if idx < len(tds):
                    a = tds[idx].find("a", href=True)
                    if a:
                        return a["href"].strip()
                return ""

            return {
                "expediente": _cell(0),
                "companies": _cell(1),
                "start_date": _cell(2),
                "resolution_date": _cell(3),
                "decision": _cell(4),
                "subsector": _cell(5),
                "public_version_url": _cell_link(6),
            }

        except Exception as exc:
            logger.warning(
                "  [%s] Portal lookup error (attempt %d/%d): %s",
                expediente, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(5)

    return None


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def build_update_email_html(
    record: Dict[str, Any],
    resolution: Dict[str, Any],
    deal_match: Optional[Dict[str, Any]],
    companies_en: str,
    decision_en: str = "",
) -> Tuple[str, str]:
    subject = build_subject("mexico_cna", "update", deal_match)
    expediente = record.get("expediente", "N/A")
    asunto = record.get("asunto", "N/A")
    agentes_es = record.get("agentes", "N/A")
    session_title = record.get("session_title", "N/A")
    session_url = record.get("session_url", "")

    companies_es = resolution.get("companies", "N/A")
    decision = resolution.get("decision", "N/A")
    decision_en_display = decision_en if decision_en else decision
    start_date = resolution.get("start_date", "N/A")
    resolution_date = resolution.get("resolution_date", "N/A")
    subsector = resolution.get("subsector", "N/A")
    pdf_url = resolution.get("public_version_url", "")

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

    session_link = (
        f'<a href="{escape_html(session_url)}" target="_blank" style="color:#0ea5e9;">View session page &rarr;</a>'
        if session_url else ""
    )
    pdf_link = (
        f'<a href="{escape_html(pdf_url)}" target="_blank" style="color:#0ea5e9;">View public version PDF &rarr;</a>'
        if pdf_url else "N/A"
    )

    companies_en_row = ""
    if companies_en and companies_en.strip() and companies_en.strip() != companies_es.strip():
        companies_en_row = (
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:14px;">Companies (EN):</td>'
            f'<td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(companies_en)}</td></tr>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;">

  <h2 style="color:#333;margin-top:0;border-bottom:3px solid #16a34a;padding-bottom:12px;">
    {escape_html(subject)}
  </h2>

  <div style="background:#f0fdf4;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #16a34a;">
    <strong>Resolution Found</strong> — This case has received a decision from the CNA.<br>
    <strong>Session:</strong> {escape_html(session_title)}<br>
    {session_link}
  </div>

  {banner}

  <h3 style="color:#334155;font-size:16px;margin-bottom:8px;">Resolution Details</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Expediente:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(expediente)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Decision (ES):</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(decision)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Decision (EN):</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;color:#16a34a;">{escape_html(decision_en_display)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Companies (ES):</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(companies_es)}</td>
    </tr>
    {companies_en_row}
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Start Date:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(start_date)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Resolution Date:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(resolution_date)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Subsector:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(subsector)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Public Version:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{pdf_link}</td>
    </tr>
  </table>

  <h3 style="color:#334155;font-size:16px;margin-bottom:8px;">Original Filing</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Asunto:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(asunto)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Original Parties (ES):</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(agentes_es)}</td>
    </tr>
  </table>

  <p style="color:#999;font-size:12px;margin-top:24px;">
    Automated email — Mexico CNA update monitor.
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
    """Route email: test → TEST_RECIPIENT / live → org-aware routing."""
    if test_mode:
        webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
        if not webhook_url:
            logger.warning(
                "  N8N_WEBHOOK_ONLY_ME not set — test email skipped")
            return False
        logger.info(
            "  [TEST] Sending to %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
        return send_direct_email([TEST_RECIPIENT], payload, webhook_url=webhook_url)
    return post_email_payload(payload, subject=subject)


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------

def process_record(
    record: Dict[str, Any],
    resolution: Dict[str, Any],
    collection,
    open_deals: List[Dict[str, Any]],
    error_items: List[Dict[str, Any]],
    dry_run: bool = False,
    test_mode: bool = False,
) -> str:
    """
    Handle a record that has a resolution result.
    Translates companies, updates DB, sends email.

    Returns: "updated_matched" | "updated_usa" | "updated_no_email"
    """
    expediente = record.get("expediente", "")
    companies_es = resolution.get("companies", "")
    decision_es = resolution.get("decision", "")

    # Translate companies
    companies_en = companies_es
    if companies_es:
        try:
            translated = translate_es_to_en(companies_es)
            if translated and translated.strip():
                companies_en = translated
                if companies_en != companies_es:
                    logger.info(
                        "  [%s] Translated companies: %.150s", expediente, companies_en)
        except Exception as exc:
            logger.warning("  [%s] Translation error (companies): %s", expediente, exc)

    # Translate decision
    decision_en = decision_es
    if decision_es:
        try:
            translated = translate_es_to_en(decision_es)
            if translated and translated.strip():
                decision_en = translated
                if decision_en != decision_es:
                    logger.info("  [%s] Decision translated: %s → %s", expediente, decision_es, decision_en)
        except Exception as exc:
            logger.warning("  [%s] Translation error (decision): %s", expediente, exc)

    now_iso = utc_now_iso()
    existing_deal_id = record.get("deal_id")
    matched_deal_id: Optional[str] = existing_deal_id
    match_type: Optional[str] = record.get("match_type")
    deal_match: Optional[Dict[str, Any]] = None

    if existing_deal_id:
        deal_match = get_deal_by_id(str(existing_deal_id))
        if not deal_match:
            logger.warning(
                "  [%s] Stored deal_id=%s not found — re-matching",
                expediente, existing_deal_id,
            )
            existing_deal_id = None
            matched_deal_id = None

    if not existing_deal_id:
        # LLM match on resolved companies
        match_text = companies_en or companies_es
        try:
            matched_deal_id = llm_match_deal_id(
                regulator_name="Mexico CNA",
                case_sections={
                    "EXPEDIENTE": expediente,
                    "COMPANIES (Spanish)": companies_es,
                    "COMPANIES (English translation)": companies_en,
                },
                source_label="the companies text",
                deals=open_deals,
            )
            if matched_deal_id:
                match_type = "llm"
                logger.info("  [%s] LLM matched deal_id=%s",
                            expediente, matched_deal_id)
        except Exception as exc:
            logger.error("  [%s] LLM match error: %s", expediente, exc)
            collect_error(
                error_items, str(exc), step="llm_match_deal_id",
                context={"expediente": expediente},
            )

        # Regex fallback
        if not matched_deal_id and match_text:
            try:
                matched_deal_id = regex_match_flat_scan(
                    companies_en, open_deals, suffixes=MEXICO_SUFFIXES
                )
                if matched_deal_id:
                    match_type = "regex"
                    logger.info("  [%s] Regex matched deal_id=%s",
                                expediente, matched_deal_id)
            except Exception as exc:
                logger.warning("  [%s] Regex match error: %s", expediente, exc)

        if matched_deal_id:
            deal_match = get_deal_by_id(matched_deal_id)
            if not deal_match:
                logger.warning(
                    "  [%s] deal_id=%s not found in deals collection",
                    expediente, matched_deal_id,
                )
                matched_deal_id = None
                match_type = None

    # Build DB update
    update_fields: Dict[str, Any] = {
        "companies_resolved": companies_es,
        "companies_resolved_en": companies_en,
        "decision": decision_es,
        "decision_en": decision_en,
        "start_date": resolution.get("start_date"),
        "resolution_date": resolution.get("resolution_date"),
        "subsector": resolution.get("subsector"),
        "public_version_url": resolution.get("public_version_url"),
        "is_open": False,
        "resolved_at": now_iso,
        "updated_at": now_iso,
    }
    if matched_deal_id:
        update_fields["deal_id"] = matched_deal_id
        update_fields["match_type"] = match_type

    if deal_match:
        subject, html = build_update_email_html(
            record, resolution, deal_match, companies_en, decision_en
        )
        if match_type == "regex":
            subject = subject.replace("[FRMD]", "[FRRMD]")

        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        logger.info(
            "  [%s] %s match: %s / %s",
            expediente, (match_type or "existing").upper(), target, acquirer,
        )

        if not dry_run:
            collection.update_one(
                {"expediente": expediente},
                {"$set": update_fields},
            )
            payload: Dict[str, Any] = {
                "subject": subject,
                "html": html,
                "expediente": expediente,
                "source": "mexico_cna_update_monitor",
                "is_new_case": False,
                "deal_id": matched_deal_id,
            }
            _send_email(payload, subject, test_mode)
        else:
            logger.info(
                "  [DRY-RUN] Would update + send %s for %s",
                subject.split(" - ")[-1].strip(), expediente,
            )
        return "updated_matched"

    # USA relation check
    is_usa = False
    try:
        is_usa = bool(
            verify_usa_relation(
                company_details=companies_en or companies_es,
                case_type="MEXICO_CNA",
            )
        )
        logger.info("  [%s] USA check: %s", expediente, is_usa)
    except Exception as exc:
        logger.error("  [%s] USA check error: %s", expediente, exc)
        collect_error(
            error_items, str(exc), step="verify_usa_relation",
            context={"expediente": expediente},
        )

    if is_usa:
        subject, html = build_update_email_html(
            record, resolution, None, companies_en, decision_en
        )
        if not dry_run:
            collection.update_one(
                {"expediente": expediente},
                {"$set": update_fields},
            )
            payload = {
                "subject": subject,
                "html": html,
                "expediente": expediente,
                "source": "mexico_cna_update_monitor",
                "is_new_case": False,
                "is_unmatched": True,
            }
            _send_email(payload, subject, test_mode)
        else:
            logger.info(
                "  [DRY-RUN] Would update + send [FRUD] for %s", expediente)
        return "updated_usa"

    # No match, not USA — update silently
    logger.info(
        "  [%s] No match, not USA-related — updating silently", expediente)
    if not dry_run:
        collection.update_one(
            {"expediente": expediente},
            {"$set": update_fields},
        )
    else:
        logger.info("  [DRY-RUN] Would update silently for %s", expediente)
    return "updated_no_email"


# ---------------------------------------------------------------------------
# Core run function
# ---------------------------------------------------------------------------

def run_mexico_cna_update_monitor(
    max_workers: int = DEFAULT_MAX_WORKERS,
    dry_run: bool = False,
    test_mode: bool = False,
) -> Dict[str, Any]:
    """
    Run the Mexico CNA update monitor.

    Fetches ALL is_open=True records and processes them in parallel using
    a ThreadPoolExecutor. Each worker owns its own requests.Session() so
    ASP.NET ViewState and cookies never collide between workers.

    Args:
        max_workers: Number of parallel portal lookups (default 3).
        dry_run:     No DB writes, no emails.
        test_mode:   Emails to TEST_RECIPIENT via N8N_WEBHOOK_ONLY_ME.

    Returns:
        Stats dict with run summary.
    """
    refresh_script_log(logger, _get_log_file)

    run_start = datetime.datetime.now()
    all_error_items: List[Dict[str, Any]] = []

    stats: Dict[str, int] = {
        "records_fetched": 0,
        "records_pending": 0,
        "records_resolved": 0,
        "updated_matched": 0,
        "updated_usa": 0,
        "updated_no_email": 0,
        "errors": 0,
    }
    stats_lock = threading.Lock()

    logger.info("=" * 60)
    logger.info(
        "START: Mexico CNA Update Monitor | dry_run=%s | test_email=%s | max_workers=%d",
        dry_run, test_mode, max_workers,
    )
    if dry_run:
        logger.info("DRY-RUN: DB writes and emails are suppressed")
    if test_mode:
        logger.info(
            "TEST-EMAIL: emails → %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
    logger.info("=" * 60)

    try:
        ok, msg = init_mongodb_connection()
        if not ok:
            collect_error(all_error_items, f"MongoDB: {msg}", step="mongodb_connect")
            return stats
        logger.info("MongoDB: %s", msg)

        collection = get_collection()
        if collection is None:
            collect_error(all_error_items, "Could not get collection", step="get_collection")
            return stats

        # Fetch ALL open records — no limit, workers handle the concurrency
        open_records = list(collection.find({"is_open": True}))
        stats["records_fetched"] = len(open_records)
        logger.info(
            "Fetched %d is_open=True record(s) | max_workers=%d",
            len(open_records), max_workers,
        )

        if not open_records:
            logger.info("No open records to check — done")
            return stats

        # Load open deals once — shared read-only across all workers (safe)
        logger.info("Loading open deals from MongoDB...")
        open_deals = fetch_open_deals()
        logger.info("Loaded %d open deals", len(open_deals))

        total = len(open_records)

        # -----------------------------------------------------------------------
        # Worker function — each invocation owns its own requests.Session()
        # -----------------------------------------------------------------------
        def _worker(args: Tuple[int, Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
            """
            Process one record: portal lookup → DB update → email.
            Returns (result_str, local_error_items).
            """
            idx, record = args
            local_errors: List[Dict[str, Any]] = []
            expediente = record.get("expediente", "")

            if not expediente:
                logger.info("[%d/%d] No expediente — skipping", idx, total)
                return "skipped", local_errors

            logger.info("[%d/%d] Checking: %s", idx, total, expediente)

            try:
                # Each worker gets its own HTTP session — ViewState is isolated
                session = requests.Session()
                time.sleep(REQUEST_DELAY)
                resolution = lookup_expediente(session, expediente)

                if resolution is None:
                    return "pending", local_errors

                logger.info(
                    "  [%s] Resolution found | Decision: %s | Companies: %.80s",
                    expediente,
                    resolution.get("decision", "N/A"),
                    resolution.get("companies", "N/A"),
                )

                result = process_record(
                    record, resolution, collection, open_deals, local_errors,
                    dry_run=dry_run, test_mode=test_mode,
                )
                return result, local_errors

            except Exception as exc:
                logger.exception("[%d/%d] Error for %s: %s", idx, total, expediente, exc)
                collect_error(
                    local_errors, str(exc), step="process_record",
                    context={"expediente": expediente},
                )
                return "error", local_errors

        # -----------------------------------------------------------------------
        # Dispatch all records to the thread pool
        # -----------------------------------------------------------------------
        logger.info(
            "Dispatching %d record(s) to %d worker(s)...", total, max_workers
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_worker, (idx, record)): record
                for idx, record in enumerate(open_records, 1)
            }
            for future in as_completed(futures):
                record = futures[future]
                expediente = record.get("expediente", "?")
                try:
                    result, local_errors = future.result()
                    all_error_items.extend(local_errors)

                    with stats_lock:
                        if result == "pending":
                            stats["records_pending"] += 1
                        elif result == "error" or result == "skipped":
                            if result == "error":
                                stats["errors"] += 1
                        else:
                            stats["records_resolved"] += 1
                            stats[result] = stats.get(result, 0) + 1

                except Exception as exc:
                    logger.exception("Future result error for %s: %s", expediente, exc)
                    collect_error(
                        all_error_items, str(exc), step="future_result",
                        context={"expediente": expediente},
                    )
                    with stats_lock:
                        stats["errors"] += 1

    except Exception as exc:
        logger.exception("Unhandled error in run: %s", exc)
        collect_error(all_error_items, f"Unhandled error: {exc}", step="main")

    finally:
        send_error_summary(all_error_items, SCRIPT_NAME)
        elapsed = round((datetime.datetime.now() - run_start).total_seconds(), 1)

        logger.info("=" * 60)
        logger.info("SUMMARY  [workers=%d]", max_workers)
        logger.info("  Records fetched        : %d", stats["records_fetched"])
        logger.info("  Records pending        : %d (no decision yet)", stats["records_pending"])
        logger.info("  Records resolved       : %d (decision found)", stats["records_resolved"])
        logger.info("  Updated [FRMD/FRRMD]   : %d", stats["updated_matched"])
        logger.info("  Updated [FRUD]         : %d", stats["updated_usa"])
        logger.info("  Updated silently       : %d", stats["updated_no_email"])
        logger.info("  Errors                 : %d", stats["errors"])
        logger.info("  Total time             : %ss", elapsed)
        logger.info("=" * 60)

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mexico CNA update monitor — checks resolution portal for decisions"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="No DB writes or emails",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help=f"Send emails to {TEST_RECIPIENT} via N8N_WEBHOOK_ONLY_ME",
    )
    parser.add_argument(
        "--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
        help=f"Parallel portal lookups (default: {DEFAULT_MAX_WORKERS})",
    )
    args = parser.parse_args()
    run_mexico_cna_update_monitor(
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        test_mode=args.test_email,
    )


if __name__ == "__main__":
    main()
