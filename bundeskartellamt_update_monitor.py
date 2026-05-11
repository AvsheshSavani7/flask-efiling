"""
Bundeskartellamt Laufende Verfahren — Update Monitor.

Monitors all german_cases records where is_open=True.
Fetches ALL pages from the listing to build a file_number → live_row lookup,
then compares each stored record against the live data.

Change detection:
  - pursue (Unternehmen), product_area (Produktbereich), diploma (Abschluss), date

Branching on change:
  1. deal_id present on stored record → send [FRMD] update email (old→new)
  2. deal_id empty → LLM match deals
     → matched → send [FRMD], set deal_id
  3. Not matched → LLM USA check
     → USA-related → send [FRUD] email
  4. Not USA-related → no email, silent update

All paths update the german_cases record in MongoDB.
If diploma changes from '-'/empty to a real value → set is_open=False.
"""

import json
import os
import re
import sys
import time
import traceback
import logging
from logging.handlers import RotatingFileHandler
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set
from bson import ObjectId
from mongodb_connection import (
    get_deals_collection, get_database, is_connected, init_mongodb_connection
)
from html import escape as escape_html
from llm_verification_service import verify_country_relation
from bundeskartellamt_initial_proxy import match_deal_with_llm
from error_email_service import send_error_email
from log_utils import cleanup_old_logs, refresh_log_file

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BASE_URL = "https://www.bundeskartellamt.de/SiteGlobals/Forms/Suche/LaufendeVerfahren/LaufendeVerfahren_Formular.html"
LAUFENDE_VERFAHREN_URL = f"{BASE_URL}?resultsPerPage=50"

COMPARED_FIELDS = ["date", "pursue", "product_area", "diploma"]
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "germany_cases_update_monitor"
IST = timezone(timedelta(hours=5, minutes=30))
FIELD_LABELS = {
    "date": "Datum (Date)",
    "pursue": "Unternehmen (Companies)",
    "product_area": "Produktbereich (Product Area)",
    "diploma": "Abschluss (Conclusion)",
}

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger("bundeskartellamt_update_monitor")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


if not logger.handlers:
    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


def _log_critical_error_and_email(msg: str, context: Optional[dict] = None):
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context or {},
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


# ---------------------------------------------------------------------------
# Proxy config (same as register script)
# ---------------------------------------------------------------------------

PC_USERNAME = "pcmIxC35qD-res-de"
PC_PASSWORD = "PC_145YhLBkUZV7Ottjy"
PC_HOST = "proxy-eu.proxy-cheap.com"
PC_PORT = "5959"

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Referer": "https://www.bundeskartellamt.de/",
}


def _build_proxy_dict():
    proxy_url = f"http://{PC_USERNAME}-country-de:{PC_PASSWORD}@{PC_HOST}:{PC_PORT}"
    return {"http": proxy_url, "https": proxy_url}


def fetch_html_with_proxy(url, max_retries=3):
    for attempt in range(1, max_retries + 1):
        for label, proxies in [("DE residential proxy", _build_proxy_dict()), ("Direct (no proxy)", None)]:
            try:
                if attempt > 1:
                    logger.info(f"   [{attempt}/{max_retries}] {label}...")
                else:
                    logger.info(f"   Strategy: {label}...")
                resp = requests.get(url, headers=FETCH_HEADERS,
                                    proxies=proxies, timeout=45)
                logger.info(
                    f"   HTTP {resp.status_code}, {len(resp.text):,} chars")
                if resp.status_code == 200 and len(resp.text) > 500:
                    logger.info(f"   Success via {label}")
                    return resp.text
                logger.warning(
                    f"   Got HTTP {resp.status_code} — trying next strategy...")
            except Exception as e:
                logger.error(f"   {label} failed: {e}")
        if attempt < max_retries:
            logger.info(f"   Retrying in 5s...")
            time.sleep(5)
    raise RuntimeError(
        "All fetch strategies failed — could not reach Bundeskartellamt")


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_german_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["german_cases"]


def fetch_open_german_cases(collection) -> List[Dict[str, Any]]:
    """Return list of all is_open=True records from german_cases."""
    try:
        docs = list(collection.find({"is_open": True}))
        for doc in docs:
            if "_id" in doc:
                doc["_doc_id"] = doc["_id"]
        logger.info(f"Loaded {len(docs)} open german_cases to monitor")
        return docs
    except Exception as e:
        logger.warning(f"Error fetching german_cases: {e}")
        return []


def fetch_deals() -> List[Dict[str, Any]]:
    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            return []
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(deals_collection.find(status_filter))
        for d in deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])
        logger.info(f"Fetched {len(deals)} open/unknown deals from MongoDB")
        return deals
    except Exception as e:
        logger.warning(f"Error fetching deals: {e}")
        return []


def update_german_case(collection, doc_id, update_fields: Dict) -> bool:
    try:
        update_fields["updated_at"] = datetime.now(tz=__import__(
            'datetime').timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        result = collection.update_one(
            {"_id": doc_id},
            {"$set": update_fields},
        )
        return result.modified_count > 0
    except Exception as e:
        logger.warning(f"   Error updating german_case: {e}")
        return False


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate_to_english(text: str) -> str:
    if not text or not text.strip():
        return ""
    text = text.strip()
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "de",
                  "tl": "en", "dt": "t", "q": text}
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            segments = data[0] if data and isinstance(data[0], list) else []
            parts = [seg[0].strip() for seg in segments
                     if isinstance(seg, (list, tuple)) and seg and seg[0]]
            if parts:
                return " ".join(parts).strip()
    except Exception as e:
        logger.warning(f"Translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


def determine_is_open(diploma: str) -> bool:
    if not diploma or not diploma.strip() or diploma.strip() == "-":
        return True
    return False


# ---------------------------------------------------------------------------
# Table extraction
# ---------------------------------------------------------------------------

def extract_raw_table_rows(html_content: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_content, "html.parser")
    records = []
    tables = soup.find_all("table")
    if not tables:
        return records
    for table in tables:
        rows = table.find_all("tr")[1:]
        for row in rows:
            try:
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue
                records.append({
                    "date": re.sub(r"\s+", " ", cells[0].get_text(separator=" ", strip=True)).strip(),
                    "file_number": re.sub(r"\s+", " ", cells[1].get_text(separator=" ", strip=True)).strip(),
                    "pursue": re.sub(r"\s+", " ", cells[2].get_text(separator=" ", strip=True)).strip(),
                    "product_area": re.sub(r"\s+", " ", cells[3].get_text(separator=" ", strip=True)).strip(),
                    "diploma": re.sub(r"\s+", " ", cells[4].get_text(separator=" ", strip=True)).strip(),
                })
            except Exception:
                continue
    return records


# ---------------------------------------------------------------------------
# Pagination: fetch ALL pages to build complete lookup
# ---------------------------------------------------------------------------

def _page_url(page_num: int) -> str:
    if page_num <= 1:
        return LAUFENDE_VERFAHREN_URL
    return f"{LAUFENDE_VERFAHREN_URL}&gtp=83488_list%253D{page_num}#pagination-83488"


def fetch_all_listing_rows() -> Dict[str, Dict[str, str]]:
    """Fetch every page and return {file_number: live_row} lookup."""
    lookup: Dict[str, Dict[str, str]] = {}
    seen: Set[str] = set()
    page_num = 1
    max_pages = 50

    while page_num <= max_pages:
        url = _page_url(page_num)
        logger.info(f"   Page {page_num}: fetching...")
        try:
            html = fetch_html_with_proxy(url)
            logger.info(f"HTML: {html}")
        except RuntimeError as e:
            logger.error(f"   Failed to fetch page {page_num}: {e}")
            break

        rows = extract_raw_table_rows(html)
        logger.info(f"Rows: {rows}")
        if not rows:
            logger.info(f"   No rows on page {page_num}, stopping")
            break

        page_fns = {r.get("file_number", "") for r in rows}
        new_fns = page_fns - seen
        if not new_fns:
            logger.info(
                f"   Page {page_num} returned duplicate rows, stopping")
            break
        seen.update(page_fns)

        for r in rows:
            fn = r.get("file_number", "")
            if fn:
                lookup[fn] = r

        logger.info(
            f"   Page {page_num}: {len(rows)} rows ({len(new_fns)} new)")
        page_num += 1
        time.sleep(2)

    logger.info(f"   Total listing rows: {len(lookup)}")
    return lookup


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(stored: Dict, live: Dict) -> List[Tuple[str, str, str]]:
    """Compare stored record vs live row. Returns [(field, old_value, new_value), ...]."""
    changes = []
    logger.info(f"Stored: {stored}")
    logger.info(f"Live: {live}")
    for field in COMPARED_FIELDS:
        old_val = (stored.get(field) or "").strip()
        new_val = (live.get(field) or "").strip()
        if old_val != new_val:
            changes.append((field, old_val, new_val))
    return changes


def parse_llm_match(result: str, deal_by_id: Dict) -> Tuple[Optional[Dict], str, str]:
    if not result or result.strip().lower() == "none":
        return None, "", ""
    stripped = result.strip()
    if not stripped.lower().startswith("match:"):
        return None, "", ""
    parts = stripped[6:].strip().split("|")
    if len(parts) < 3:
        return None, "", ""
    llm_deal_id = parts[0].strip()
    company = parts[1].strip()
    role = parts[2].strip().lower().replace("(", "").replace(")", "")
    if role not in ("target", "acquirer"):
        role = "acquirer"
    deal = deal_by_id.get(llm_deal_id)
    return deal, company, role


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _safe(val):
    if val is None or (isinstance(val, str) and not val.strip()):
        return "N/A"
    return escape_html(str(val).strip())


def _build_changes_html(changes: List[Tuple[str, str, str]]) -> str:
    """Build an HTML table showing old → new for each changed field."""
    rows_html = ""
    for i, (field, old_val, new_val) in enumerate(changes):
        label = FIELD_LABELS.get(field, field)
        bg = ' style="background-color:#fff8f0;"' if i % 2 == 0 else ' style="background-color:#fff3e0;"'
        rows_html += (
            f'<tr{bg}>'
            f'<td style="padding:8px;font-weight:bold;color:#555;width:200px;">{escape_html(label)}</td>'
            f'<td style="padding:8px;color:#c62828;text-decoration:line-through;">{_safe(old_val)}</td>'
            f'<td style="padding:8px;color:#2e7d32;font-weight:bold;">{_safe(new_val)}</td>'
            f'</tr>\n'
        )
    return f"""
<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
  <thead><tr style="background:#f5f5f5;">
    <th style="padding:8px;text-align:left;border-bottom:2px solid #ddd;">Field</th>
    <th style="padding:8px;text-align:left;border-bottom:2px solid #ddd;">Old Value</th>
    <th style="padding:8px;text-align:left;border-bottom:2px solid #ddd;">New Value</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""


def _build_case_info_html(stored: Dict) -> str:
    cell = "padding:8px; color:#333;"
    rows = [
        ("File Number", stored.get("file_number")),
        ("Date", stored.get("date")),
        ("Unternehmen (German)", stored.get("pursue")),
        ("Undertaking (English)", stored.get("pursue_en")),
        ("Produktbereich (German)", stored.get("product_area")),
        ("Product Area (English)", stored.get("product_area_en")),
        ("Abschluss (German)", stored.get("diploma")),
        ("Status", "Open" if stored.get("is_open") else "Closed"),
    ]
    html = ""
    for i, (label, value) in enumerate(rows):
        bg = ' style="background-color:#f9f9f9;"' if i % 2 == 1 else ""
        html += f'<tr{bg}><td style="padding:8px;font-weight:bold;width:200px;color:#555;">{label}:</td><td style="{cell}">{_safe(value)}</td></tr>\n'
    return html


def generate_update_email(stored: Dict, changes: List[Tuple[str, str, str]],
                          deal: Optional[Dict]) -> Tuple[str, str]:
    fn = stored.get("file_number", "N/A")
    change_summary = ", ".join(FIELD_LABELS.get(f, f) for f, _, _ in changes)

    if deal:
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        deal_id = deal.get("deal_id", "N/A")
        prefix = "[FRMD]"
        subject = f"{prefix} German Bundeskartellamt-{fn} (Updated) – {target} / {acquirer}"
        banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
  <div style="font-weight:800;color:#1e40af;margin-bottom:4px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;"><b>Acquirer:</b> {_safe(acquirer)} | <b>Target:</b> {_safe(target)} | <b>Deal ID:</b> {_safe(deal_id)}</div>
</div>"""
        border_color = "#2563eb"
    else:
        prefix = "[FRUD]"
        pursue_en = stored.get("pursue_en", "N/A")
        subject = f"{prefix} German Bundeskartellamt-{fn} (Updated, USA-Related) – {pursue_en[:60]}"
        banner = """
<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
  <div style="font-weight:800;color:#92400e;">USA-Related (Unmatched)</div>
</div>"""
        border_color = "#f59e0b"

    changes_html = _build_changes_html(changes)
    case_info = _build_case_info_html(stored)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid {border_color};">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Laufende Verfahren — Update Monitor</p>
  <div style="background:#fef2f2;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #ef4444;">
    <div style="font-weight:800;color:#dc2626;margin-bottom:4px;">Changes Detected</div>
    <div style="font-size:14px;color:#991b1b;">{escape_html(change_summary)}</div>
  </div>
  {banner}
  <h3 style="margin-top:18px;">Case Details</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_info}</table>
  <h3 style="margin-top:18px;">Changed Fields (Old → New)</h3>
  {changes_html}
  <p><strong>View online:</strong> <a href="{escape_html(LAUFENDE_VERFAHREN_URL)}" style="color:{border_color};" target="_blank">Laufende Verfahren</a></p>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    <p>Automated email from Bundeskartellamt Update Monitor.</p>
  </div>
</div></body></html>"""
    return subject, html


def send_email_via_webhook(subject: str, html: str, file_number: str = "",
                           deal_id: str = None, changed_fields: List[str] = None) -> bool:
    try:
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f"
        )
        payload = {
            "subject": subject,
            "html": html,
            "file_number": file_number,
            "source": "update_monitor",
            "view_url": LAUFENDE_VERFAHREN_URL,
        }
        if deal_id:
            payload["deal_id"] = deal_id
        if changed_fields:
            payload["changed_fields"] = changed_fields
        resp = requests.post(webhook_url, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=30)
        resp.raise_for_status()
        logger.info(f"   Email sent ({resp.status_code})")
        return True
    except Exception as e:
        logger.warning(f"   Email failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    logger.info("=" * 60)
    logger.info("[STEP 1] Starting Germany Cases Update Monitor")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)
    logger.info("BUNDESKARTELLAMT UPDATE MONITOR")

    success, message = init_mongodb_connection(".env")
    if not success:
        _log_critical_error_and_email(f"MongoDB init failed: {message}", {
                                      "step": "init_mongodb_connection"})
        return {"success": False, "error": message}

    # 1. Fetch deals
    deals = fetch_deals()
    deal_by_id = {str(d.get("deal_id", "")): d for d in deals if d.get("deal_id")}

    # 2. Fetch german_cases (is_open=True)
    gc_collection = get_german_cases_collection()
    if gc_collection is None:
        _log_critical_error_and_email("german_cases collection not available", {
                                      "step": "get_german_cases_collection"})
        return {"success": False, "error": "german_cases collection unavailable"}

    open_cases = fetch_open_german_cases(gc_collection)
    logger.info(f"Open cases: {open_cases}")
    if not open_cases:
        logger.info("No open german_cases to monitor")
        return {"success": True, "message": "No open cases"}

    # 3. Fetch ALL listing pages → build file_number → live_row lookup
    logger.info("Fetching complete listing for comparison...")
    live_lookup = fetch_all_listing_rows()

    # 4. Compare each stored case against live data
    stats = {"checked": 0, "unchanged": 0, "updated": 0, "not_found": 0,
             "email_sent": 0, "matched_new": 0, "usa_related": 0}

    logger.info(f"Checking {len(open_cases)} open cases for updates...")

    for idx, stored in enumerate(open_cases, 1):
        fn = stored.get("file_number", "")
        doc_id = stored.get("_doc_id") or stored.get("_id")
        stats["checked"] += 1

        logger.info(f"[{idx}/{len(open_cases)}] {fn}")

        # Find live row
        live = live_lookup.get(fn)
        if live is None:
            logger.info(f"  Not found in listing — skipping")
            stats["not_found"] += 1
            continue

        # Detect changes
        changes = detect_changes(stored, live)
        logger.info(f"Changes: {changes}")
        if not changes:
            logger.info(f"  No changes")
            stats["unchanged"] += 1
            continue

        changed_field_names = [f for f, _, _ in changes]
        logger.info(f"  Changes: {', '.join(changed_field_names)}")

        # Build update dict with new values + translations for changed fields
        update_fields: Dict[str, Any] = {}
        for field, old_val, new_val in changes:
            update_fields[field] = new_val
            en_key = f"{field}_en"
            if field in ("pursue", "product_area", "diploma") and new_val:
                update_fields[en_key] = translate_to_english(new_val)

        # Recalculate is_open from diploma
        new_diploma = update_fields.get("diploma", stored.get("diploma", ""))
        new_is_open = determine_is_open(new_diploma)
        if new_is_open != stored.get("is_open"):
            update_fields["is_open"] = new_is_open
            logger.info(f"  is_open: {stored.get('is_open')} → {new_is_open}")

        # Update stored dict in-memory for email generation
        merged = {**stored, **update_fields}

        # Determine email path
        deal = None
        stored_deal_id = stored.get("deal_id")

        if stored_deal_id:
            # Path A: deal_id already present → resolve deal, send [FRMD]
            deal = deal_by_id.get(str(stored_deal_id))
            if not deal:
                try:
                    deals_coll = get_deals_collection()
                    deal_doc = deals_coll.find_one(
                        {"_id": ObjectId(stored_deal_id)})
                    if deal_doc:
                        deal_doc["deal_id"] = str(deal_doc["_id"])
                        deal = deal_doc
                except Exception:
                    pass
            if deal:
                logger.info(f"  Has deal_id → sending [FRMD] update email")
                subject, html = generate_update_email(merged, changes, deal)
                send_email_via_webhook(subject, html, fn,
                                       deal_id=str(stored_deal_id),
                                       changed_fields=changed_field_names)
                stats["email_sent"] += 1
            else:
                logger.warning(
                    f"  deal_id {stored_deal_id} not found in deals — treating as unmatched")
                stored_deal_id = None

        if not stored_deal_id:
            # Path B: no deal_id → LLM match
            pursue_en = merged.get("pursue_en") or stored.get("pursue_en", "")
            if not pursue_en or pursue_en == "[Translation failed]":
                pursue_text = merged.get("pursue", "")
                if pursue_text:
                    pursue_en = translate_to_english(pursue_text)

            match_result = match_deal_with_llm(pursue_en, deals)
            deal_match, matched_company, matched_role = parse_llm_match(
                match_result, deal_by_id)

            if deal_match:
                # Matched → send [FRMD], set deal_id
                deal = deal_match
                update_fields["deal_id"] = deal_match.get("deal_id")
                logger.info(
                    f"  New match: {matched_company} → sending [FRMD] update email")
                subject, html = generate_update_email(merged, changes, deal)
                send_email_via_webhook(subject, html, fn,
                                       deal_id=deal_match.get("deal_id"),
                                       changed_fields=changed_field_names)
                stats["email_sent"] += 1
                stats["matched_new"] += 1
            else:
                # Path C: no match → USA check
                logger.info(f"  No deal match")
                try:
                    company_details = {
                        "today_date": datetime.now().strftime("%Y-%m-%d"),
                        "record": merged,
                    }
                    is_usa = verify_country_relation(
                        company_details=company_details, country="USA", case_type="GERMANY"
                    )
                except Exception as e:
                    logger.exception(f"USA check failed: {e}")
                    error_items.append({
                        "file_number": fn,
                        "error": str(e),
                        "step": "verify_country_relation",
                    })
                    is_usa = False

                if is_usa:
                    # Path C1: USA-related → send [FRUD]
                    logger.info(f"  USA-related → sending [FRUD] update email")
                    subject, html = generate_update_email(
                        merged, changes, None)
                    send_email_via_webhook(subject, html, fn,
                                           changed_fields=changed_field_names)
                    stats["email_sent"] += 1
                    stats["usa_related"] += 1
                else:
                    # Path C2: not USA → silent update
                    logger.info(f"  Not USA-related → silent update")

        # Always update DB
        if update_fields and doc_id:
            ok = update_german_case(gc_collection, doc_id, update_fields)
            if ok:
                stats["updated"] += 1
                logger.info(f"  DB updated")
            else:
                logger.error(f"  DB update failed")

    if error_items:
        send_error_email(
            script_name=SCRIPT_NAME,
            error_message=f"{len(error_items)} errors occurred during run",
            context={
                "error_count": len(error_items),
                "errors": error_items[:20],
            },
            traceback_str=None,
        )

    # Summary
    elapsed = round(time.time() - run_start, 1)
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Checked                      : {stats['checked']}")
    logger.info(f"  Unchanged                    : {stats['unchanged']}")
    logger.info(f"  Not found in listing         : {stats['not_found']}")
    logger.info(f"  Updated                      : {stats['updated']}")
    logger.info(f"  Emails sent                  : {stats['email_sent']}")
    logger.info(f"  New deal matches             : {stats['matched_new']}")
    logger.info(f"  USA-related                  : {stats['usa_related']}")
    logger.info(f"  Total time                   : {elapsed}s")
    logger.info("=" * 60)

    return {
        "success": True,
        "checked": stats["checked"],
        "unchanged": stats["unchanged"],
        "updated": stats["updated"],
        "email_sent": stats["email_sent"],
    }


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log_critical_error_and_email(
            f"Unhandled error in main: {e}", {"step": "main"})
        raise
