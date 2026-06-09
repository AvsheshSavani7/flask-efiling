"""
Bundeskartellamt Laufende Verfahren (Ongoing Proceedings) scraper — Proxy version.

Workflow:
1. Fetch deals from MongoDB (Open/Unknown/null/missing status)
2. Fetch all german_cases file_numbers for dedup (any is_open — avoids re-processing closed rows)
3. Fetch HTML via German residential proxy, paginate until cutoff date
4. Extract table rows (raw — no translation yet)
5. For each row: skip if file_number already in german_cases
6. New file_numbers only: translate to English, determine is_open from Abschluss column
7. LLM match against deals; upsert to german_cases by file_number (preserves created_at on updates)
8. On newly inserted doc only: matched → [FRMD] email; else USA-related → [FRUD] email
"""
# dummay comment for deployment check in hostinger

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
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple, Set
from bson import ObjectId
from mongodb_connection import (
    get_deals_collection, get_database, is_connected, init_mongodb_connection
)
from html import escape as escape_html
from llm_verification_service import verify_country_relation
from scraper_error_utils import collect_error, send_error_summary
from log_utils import cleanup_old_logs, refresh_log_file
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BASE__SCRAPER_URL = "https://www.bundeskartellamt.de/SiteGlobals/Forms/Suche/LaufendeVerfahren/LaufendeVerfahren_Formular.html"
LAUFENDE_VERFAHREN_URL = f"{BASE__SCRAPER_URL}?resultsPerPage=50"

EXTRACTED_RECORDS_JSON = "bundeskartellamt_laufende_verfahren_extracted.json"
SOURCE_INITIAL_FILING = "initial_filing"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "germany_cases_register"
IST = timezone(timedelta(hours=5, minutes=30))

CUTOFF_DATE = (datetime.now() - timedelta(days=15)
               ).replace(hour=0, minute=0, second=0, microsecond=0)

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

logger = logging.getLogger("bundeskartellamt_initial_proxy")
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


# ---------------------------------------------------------------------------
# Proxy config
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
    """Fetch page via DE residential proxy, fallback to direct. Retries on transient failures."""
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


def fetch_existing_german_case_file_numbers(collection) -> Set[str]:
    """All non-empty file_number values in german_cases (open or closed) for dedup."""
    try:
        cursor = collection.find(
            {"file_number": {"$exists": True, "$nin": [None, ""]}},
            {"file_number": 1, "_id": 0},
        )
        out: Set[str] = set()
        for doc in cursor:
            fn = doc.get("file_number")
            if isinstance(fn, str) and fn.strip():
                out.add(fn.strip())
        logger.info(f"Loaded {len(out)} german_cases file_numbers for dedup")
        return out
    except Exception as e:
        logger.warning(f"Error fetching german_cases file_numbers: {e}")
        return set()


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


def upsert_german_case(collection, doc: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Upsert by file_number. Returns (MongoDB _id as str, inserted_new)."""
    fn = doc.get("file_number")
    if not fn or not str(fn).strip():
        logger.warning("upsert_german_case: missing file_number")
        return None, False
    fn = str(fn).strip()
    now = utc_now_iso()
    payload = {
        k: v for k, v in doc.items()
        if k not in ("_id", "created_at")
    }
    payload["file_number"] = fn
    payload["updated_at"] = now
    try:
        result = collection.update_one(
            {"file_number": fn},
            {"$set": payload, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        inserted_new = result.upserted_id is not None
        if inserted_new:
            oid = result.upserted_id
        else:
            row = collection.find_one({"file_number": fn}, {"_id": 1})
            oid = row["_id"] if row else None
        return (str(oid) if oid is not None else None), inserted_new
    except Exception as e:
        logger.warning(f"Error upserting german_case: {e}")
        return None, False


def utc_now_iso() -> str:
    from datetime import timezone
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


# ---------------------------------------------------------------------------
# Table extraction (raw — no translation)
# ---------------------------------------------------------------------------

def parse_table_date(date_str: str):
    if not date_str or not date_str.strip():
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def extract_raw_table_rows(html_content: str) -> List[Dict[str, str]]:
    """Extract raw table rows (no translation). Returns list of dicts with German text."""
    soup = BeautifulSoup(html_content, "html.parser")
    records = []
    tables = soup.find_all("table")
    if not tables:
        logger.warning("No table found in HTML")
        return records

    for table in tables:
        rows = table.find_all("tr")[1:]
        for row in rows:
            try:
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue
                record = {
                    "date": re.sub(r"\s+", " ", cells[0].get_text(separator=" ", strip=True)).strip(),
                    "file_number": re.sub(r"\s+", " ", cells[1].get_text(separator=" ", strip=True)).strip(),
                    "pursue": re.sub(r"\s+", " ", cells[2].get_text(separator=" ", strip=True)).strip(),
                    "product_area": re.sub(r"\s+", " ", cells[3].get_text(separator=" ", strip=True)).strip(),
                    "diploma": re.sub(r"\s+", " ", cells[4].get_text(separator=" ", strip=True)).strip(),
                }
                records.append(record)
            except Exception as e:
                logger.warning(f"Error extracting row: {e}")
                continue
    return records


def filter_by_cutoff(records: List[Dict], cutoff: date) -> Tuple[List[Dict], bool]:
    """
    Filter records by cutoff date. Returns (filtered_records, reached_cutoff).
    reached_cutoff=True means we saw a record older than cutoff → stop paginating.
    """
    filtered = []
    reached_cutoff = False
    for r in records:
        d = parse_table_date(r.get("date", ""))
        if d is not None and d < cutoff:
            reached_cutoff = True
            continue
        filtered.append(r)
    return filtered, reached_cutoff


def determine_is_open(diploma: str) -> bool:
    """is_open = True if diploma (Abschluss) is '-' or empty."""
    if not diploma or not diploma.strip() or diploma.strip() == "-":
        return True
    return False


# ---------------------------------------------------------------------------
# Pagination: fetch all pages until cutoff date exceeded
# ---------------------------------------------------------------------------

def _page_url(page_num: int) -> str:
    """Build the URL for a given page number.
    Page 1 has no gtp param; page N uses gtp=83488_list%253D{N} (double-encoded %3D)."""
    if page_num <= 1:
        return LAUFENDE_VERFAHREN_URL
    return f"{LAUFENDE_VERFAHREN_URL}&gtp=83488_list%253D{page_num}#pagination-83488"


def fetch_all_records_with_pagination(
    cutoff: date,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict]:
    """Fetch pages from the Bundeskartellamt until all records newer than cutoff are collected."""
    all_records = []
    seen_file_numbers: Set[str] = set()
    page_num = 1
    max_pages = 30

    while page_num <= max_pages:
        url = _page_url(page_num)
        logger.info(f"   Page {page_num}: fetching...")
        try:
            html = fetch_html_with_proxy(url)
            logger.info(f"HTML: {html}")
        except RuntimeError as e:
            logger.error(f"   Failed to fetch page {page_num}: {e}")
            if error_items is not None:
                collect_error(
                    error_items,
                    str(e),
                    step="fetch_listing_page",
                    context={"page": page_num, "url": url},
                )
            break

        raw_rows = extract_raw_table_rows(html)
        logger.info(f"Raw rows: {raw_rows}")
        if not raw_rows:
            logger.info(f"   No rows on page {page_num}, stopping")
            break

        # Detect duplicate page (same rows = pagination URL not working)
        page_fns = {r.get("file_number", "") for r in raw_rows}
        new_fns = page_fns - seen_file_numbers
        if not new_fns:
            logger.info(
                f"   Page {page_num} returned duplicate rows, stopping pagination")
            break
        seen_file_numbers.update(page_fns)

        logger.info(
            f"   Page {page_num}: {len(raw_rows)} rows ({len(new_fns)} new)")

        filtered, reached_cutoff = filter_by_cutoff(raw_rows, cutoff)
        all_records.extend(filtered)

        if reached_cutoff:
            logger.info(
                f"   Reached cutoff date on page {page_num}, stopping pagination")
            break

        page_num += 1
        time.sleep(2)

    return all_records


# ---------------------------------------------------------------------------
# LLM deal matching
# ---------------------------------------------------------------------------

def match_deal_with_llm(pursue_en: str, deals: List[Dict]) -> Optional[str]:
    if not pursue_en or pursue_en == "[Translation failed]":
        return None

    deals_list = []
    for deal in deals:
        target = deal.get("target") or deal.get("target_name", "")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")
        if not target and not acquirer:
            continue
        deal_info = {"deal_id": deal.get(
            "deal_id", ""), "target": target, "acquirer": acquirer}
        for field in ("target_aliases", "parent_aliases"):
            aliases = deal.get(field) or []
            if isinstance(aliases, list) and aliases:
                deal_info[field] = aliases
        deals_list.append(deal_info)

    if not deals_list:
        return "None"

    lines = []
    for d in deals_list:
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target', 'N/A')} | Acquirer: {d.get('acquirer', 'N/A')}"
        for field in ("target_aliases", "parent_aliases"):
            aliases = d.get(field, [])
            if aliases:
                line += f" | {field.replace('_', ' ').title()}: {', '.join(str(a) for a in aliases)}"
        lines.append(line)

    prompt = f"""You are an M&A deal analyst. Given the translated text about a German merger case (Laufende Verfahren), determine whether it explicitly relates to any of the deals listed below.

DEALS TO MATCH:
{chr(10).join(lines)}

TRANSLATED TEXT:
{pursue_en}

INSTRUCTIONS:
1.  Extract only the company names that are explicitly and directly mentioned in the German case text (pursue_en).
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the German case text.  
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the German case text.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the German case text:
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
8. If the German case text does not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
- If match found: Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
- If no match: None
"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition. Return Match: DEAL_ID|COMPANY|target|acquirer or None."},
                {"role": "user", "content": prompt},
            ]
        )
        result = response.choices[0].message.content.strip()
        logger.info(f"   LLM match: {result}")
        return result
    except Exception as e:
        logger.warning(f"LLM Error: {e}")
        raise


def parse_llm_match(result: str, deal_by_id: Dict) -> Tuple[Optional[Dict], str, str]:
    """Parse LLM match result. Returns (deal_match, company, role) or (None, '', '')."""
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


def _build_case_rows_html(record: Dict) -> str:
    cell = "padding:8px; color:#333; word-wrap:break-word; white-space:normal; max-width:600px;"
    rows = [
        ("File Number", record.get("file_number")),
        ("Date", record.get("date")),
        ("Unternehmen (German)", record.get("pursue")),
        ("Undertaking (English)", record.get("pursue_en")),
        ("Produktbereich (German)", record.get("product_area")),
        ("Product Area (English)", record.get("product_area_en")),
        ("Abschluss (German)", record.get("diploma")),
        ("Diploma (English)", record.get("diploma_en")),
        ("Status", "Open" if record.get("is_open") else "Closed"),
    ]
    html = ""
    for i, (label, value) in enumerate(rows):
        bg = ' style="background-color:#f9f9f9;"' if i % 2 == 1 else ""
        html += f'<tr{bg}><td style="padding:8px; font-weight:bold; width:200px; color:#555;">{label}:</td><td style="{cell}">{_safe(value)}</td></tr>\n'
    return html


def generate_matched_email(record: Dict, deal: Dict) -> Tuple[str, str]:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = deal.get("deal_id", "N/A")
    file_number = record.get("file_number", "N/A")

    subject = build_subject("bundeskartellamt", "new", deal)

    deal_banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
  <strong>Matched Deal:</strong> {_safe(target)} / {_safe(acquirer)}<br>
  <strong>Deal ID:</strong> {_safe(deal_id)}
</div>"""

    case_rows = _build_case_rows_html(record)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #2563eb;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Laufende Verfahren (initial filing)</p>
  {deal_banner}
  <p><strong>View online:</strong> <a href="{escape_html(LAUFENDE_VERFAHREN_URL)}" style="color:#2563eb;" target="_blank">Laufende Verfahren</a></p>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_rows}</table>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    <p>Automated email from Bundeskartellamt scraper.</p>
  </div>
</div></body></html>"""
    return subject, html


def generate_usa_related_email(record: Dict) -> Tuple[str, str]:
    fn = record.get("file_number", "N/A")
    pursue_en = record.get("pursue_en", "N/A")
    file_number = record.get("file_number", "N/A")

    subject = build_subject("bundeskartellamt", "new")

    usa_banner = """
<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
  <strong>🇺🇸 USA-Related Case</strong> — No deal match found, but this case appears related to the United States.
</div>"""

    case_rows = _build_case_rows_html(record)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #f59e0b;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Laufende Verfahren (initial filing)</p>
  {usa_banner}
  <p><strong>View online:</strong> <a href="{escape_html(LAUFENDE_VERFAHREN_URL)}" style="color:#f59e0b;" target="_blank">Laufende Verfahren</a></p>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_rows}</table>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    <p>Automated email from Bundeskartellamt scraper.</p>
  </div>
</div></body></html>"""
    return subject, html


def send_email_via_webhook(subject: str, html: str, file_number: str = "",
                           deal_id: str = None) -> bool:
    try:
        payload = {
            "subject": subject,
            "html": html,
            "file_number": file_number,
            "source": SOURCE_INITIAL_FILING,
            "view_url": LAUFENDE_VERFAHREN_URL,
        }
        if deal_id:
            payload["deal_id"] = deal_id
        return post_email_payload(payload, subject=subject)
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
    all_raw_records: List[Dict] = []
    stats = {"new": 0, "skipped": 0, "matched": 0,
             "usa_related": 0, "saved": 0}
    cutoff = CUTOFF_DATE.date() if isinstance(
        CUTOFF_DATE, datetime) else CUTOFF_DATE
    logger.info("=" * 60)
    logger.info("[STEP 1] Starting Germany Cases Register")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)
    logger.info("BUNDESKARTELLAMT LAUFENDE VERFAHREN (Proxy)")

    try:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(
                error_items,
                f"MongoDB init failed: {message}",
                step="init_mongodb_connection",
            )
            return {"success": False, "error": message}

        deals = fetch_deals()
        deal_by_id = {str(d.get("deal_id", ""))
                          : d for d in deals if d.get("deal_id")}

        gc_collection = get_german_cases_collection()
        if gc_collection is None:
            collect_error(
                error_items,
                "german_cases collection not available",
                step="get_german_cases_collection",
            )
            return {"success": False, "error": "german_cases collection unavailable"}

        existing_file_numbers = fetch_existing_german_case_file_numbers(
            gc_collection)

        logger.info(f"Fetching records (cutoff >= {cutoff})...")
        all_raw_records = fetch_all_records_with_pagination(
            cutoff, error_items=error_items)
        logger.info(f"All raw records: {all_raw_records}")
        logger.info(f"   Total records after cutoff: {len(all_raw_records)}")

        try:
            with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
                json.dump(all_raw_records, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved raw records to {EXTRACTED_RECORDS_JSON}")
        except Exception as e:
            logger.warning(f"Could not save JSON: {e}")
            collect_error(
                error_items,
                f"Could not save JSON: {e}",
                step="write_extracted_json",
            )

        logger.info(f"Processing {len(all_raw_records)} records...")

        for idx, raw in enumerate(all_raw_records, 1):
            try:
                fn = (raw.get("file_number") or "").strip()
                logger.info(
                    f"[{idx}/{len(all_raw_records)}] {fn} — {raw.get('pursue', '')[:60]}...")

                if not fn:
                    logger.warning("  Missing file_number, skipping row")
                    continue

                if fn in existing_file_numbers:
                    logger.info(f"  Already in german_cases, skipping")
                    stats["skipped"] += 1
                    continue

                pursue_en = translate_to_english(
                    raw["pursue"]) if raw.get("pursue") else ""
                product_area_en = translate_to_english(
                    raw["product_area"]) if raw.get("product_area") else ""
                diploma_en = translate_to_english(
                    raw["diploma"]) if raw.get("diploma") else ""

                is_open = determine_is_open(raw.get("diploma", ""))

                record = {
                    "file_number": fn,
                    "date": raw.get("date", ""),
                    "pursue": raw.get("pursue", ""),
                    "pursue_en": pursue_en,
                    "product_area": raw.get("product_area", ""),
                    "product_area_en": product_area_en,
                    "diploma": raw.get("diploma", ""),
                    "diploma_en": diploma_en,
                    "is_open": is_open,
                    "deal_id": None,
                }

                logger.info(
                    f"  {fn}: pursue_en={pursue_en[:50]}... | is_open={is_open}")

                deal_match = None
                matched_company = ""
                matched_role = ""

                if pursue_en and pursue_en != "[Translation failed]":
                    try:
                        match_result = match_deal_with_llm(pursue_en, deals)
                    except Exception as e:
                        logger.exception(f"LLM match failed: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="match_deal_with_llm",
                            context={"file_number": fn},
                        )
                        match_result = None
                    if match_result:
                        deal_match, matched_company, matched_role = parse_llm_match(
                            match_result, deal_by_id)

                is_usa = False
                if deal_match:
                    record["deal_id"] = deal_match.get("deal_id")
                    logger.info(f"  Matched: {matched_company} ({matched_role})")
                else:
                    logger.info(f"  No deal match")
                    try:
                        company_details = {
                            "today_date": datetime.now().strftime("%Y-%m-%d"),
                            "record": record,
                        }
                        is_usa = verify_country_relation(
                            company_details=company_details, country="USA", case_type="GERMANY"
                        )
                    except Exception as e:
                        logger.exception(f"USA check failed: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="verify_country_relation",
                            context={"file_number": fn},
                        )
                        is_usa = False

                    if is_usa:
                        logger.info(f"  USA-related (notify only if new insert)")
                    else:
                        logger.info(f"  Not USA-related → silent save")

                doc_id, inserted_new = upsert_german_case(gc_collection, record)
                if doc_id:
                    stats["saved"] += 1
                    existing_file_numbers.add(fn)
                    logger.info(
                        f"  Saved to german_cases (id={doc_id}, new_insert={inserted_new})")
                    if inserted_new:
                        stats["new"] += 1
                        if deal_match:
                            subject, html = generate_matched_email(record, deal_match)
                            stats["matched"] += 1
                            if not send_email_via_webhook(
                                subject, html, fn, deal_id=deal_match.get("deal_id")
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to send matched-case email",
                                    step="send_email",
                                    context={"file_number": fn},
                                )
                        elif is_usa:
                            logger.info(f"  Sending [FRUD] email (first insert)")
                            subject, html = generate_usa_related_email(record)
                            stats["usa_related"] += 1
                            if not send_email_via_webhook(subject, html, fn):
                                collect_error(
                                    error_items,
                                    "Failed to send USA-related email",
                                    step="send_email",
                                    context={"file_number": fn},
                                )
                else:
                    logger.error(f"  Failed to save to german_cases")
                    collect_error(
                        error_items,
                        "Failed to save to german_cases",
                        step="upsert_german_case",
                        context={"file_number": fn},
                    )
            except Exception as e:
                logger.exception(f"Error processing record #{idx}: {e}")
                collect_error(
                    error_items,
                    str(e),
                    step="process_record",
                    context={"file_number": (raw.get("file_number") or "").strip()},
                )

        return {
            "success": True,
            "extraction_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_extracted": len(all_raw_records),
            "skipped": stats["skipped"],
            "new_saved": stats["new"],
            "matched": stats["matched"],
            "usa_related": stats["usa_related"],
            "cutoff_date": cutoff.isoformat(),
        }

    except Exception as e:
        logger.exception(f"Unhandled error in main: {e}")
        collect_error(
            error_items,
            f"Unhandled error in main: {e}",
            step="run_main",
        )
        return {"success": False, "error": str(e)}

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Total extracted              : {len(all_raw_records)}")
        logger.info(f"  Skipped (existing)           : {stats['skipped']}")
        logger.info(f"  New records saved            : {stats['new']}")
        logger.info(f"  Deal matches                 : {stats['matched']}")
        logger.info(f"  USA-related                  : {stats['usa_related']}")
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()
