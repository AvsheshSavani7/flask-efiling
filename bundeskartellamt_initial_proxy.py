"""
Bundeskartellamt Laufende Verfahren (Ongoing Proceedings) scraper — Proxy version.

Workflow:
1. Fetch deals from MongoDB (Open/Unknown/null/missing status)
2. Fetch german_cases collection (is_open=True) for dedup by file_number
3. Fetch HTML via German residential proxy, paginate until cutoff date
4. Extract table rows (raw — no translation yet)
5. For each row: skip if file_number already in german_cases
6. New records only: translate to English, determine is_open from Abschluss column
7. LLM match against deals → matched: send [FRMD] email, save with deal_id
8. Not matched → LLM check USA-related → true: send [FRUD] email
9. All records saved to german_cases collection
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set
from bson import ObjectId
from mongodb_connection import (
    get_deals_collection, get_database, is_connected, init_mongodb_connection
)
from html import escape as escape_html
from llm_verification_service import verify_country_relation

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BASE_URL = "https://www.bundeskartellamt.de/SiteGlobals/Forms/Suche/LaufendeVerfahren/LaufendeVerfahren_Formular.html"
LAUFENDE_VERFAHREN_URL = f"{BASE_URL}?resultsPerPage=50"

EXTRACTED_RECORDS_JSON = "bundeskartellamt_laufende_verfahren_extracted.json"
SOURCE_INITIAL_FILING = "initial_filing"

CUTOFF_DATE = (datetime.now() - timedelta(days=15)
               ).replace(hour=0, minute=0, second=0, microsecond=0)


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
                    print(f"   🌐 [{attempt}/{max_retries}] {label}...")
                else:
                    print(f"   🌐 Strategy: {label}...")
                resp = requests.get(url, headers=FETCH_HEADERS,
                                    proxies=proxies, timeout=45)
                print(
                    f"   📃 HTTP {resp.status_code}, {len(resp.text):,} chars")
                if resp.status_code == 200 and len(resp.text) > 500:
                    print(f"   ✅ Success via {label}\n")
                    return resp.text
                print(
                    f"   ⚠️ Got HTTP {resp.status_code} — trying next strategy...")
            except Exception as e:
                print(f"   ❌ {label} failed: {e}")
        if attempt < max_retries:
            print(f"   ⏳ Retrying in 5s...")
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


def fetch_open_german_cases(collection) -> Dict[str, dict]:
    """Return {file_number: doc} for all is_open=True records in german_cases."""
    try:
        docs = list(collection.find({"is_open": True}))
        lookup = {}
        for doc in docs:
            fn = doc.get("file_number", "")
            if fn:
                lookup[fn] = doc
        print(f"✅ Loaded {len(lookup)} open german_cases for dedup")
        return lookup
    except Exception as e:
        print(f"⚠️ Error fetching german_cases: {e}")
        return {}


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
        print(f"✅ Fetched {len(deals)} open/unknown deals from MongoDB")
        return deals
    except Exception as e:
        print(f"⚠️ Error fetching deals: {e}")
        return []


def insert_german_case(collection, doc: Dict[str, Any]) -> Optional[str]:
    try:
        result = collection.insert_one(doc)
        return str(result.inserted_id)
    except Exception as e:
        print(f"⚠️ Error inserting german_case: {e}")
        return None


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
        print(f"⚠️ Translation failed for: {text[:50]}... → {e}")
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
        print("⚠️ No table found in HTML")
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
                print(f"⚠️ Error extracting row: {e}")
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


def fetch_all_records_with_pagination(cutoff: date) -> List[Dict]:
    """Fetch pages from the Bundeskartellamt until all records newer than cutoff are collected."""
    all_records = []
    seen_file_numbers: Set[str] = set()
    page_num = 1
    max_pages = 30

    while page_num <= max_pages:
        url = _page_url(page_num)
        print(f"   📄 Page {page_num}: fetching...")
        try:
            html = fetch_html_with_proxy(url)
        except RuntimeError as e:
            print(f"   ❌ Failed to fetch page {page_num}: {e}")
            break

        raw_rows = extract_raw_table_rows(html)
        if not raw_rows:
            print(f"   🏁 No rows on page {page_num}, stopping")
            break

        # Detect duplicate page (same rows = pagination URL not working)
        page_fns = {r.get("file_number", "") for r in raw_rows}
        new_fns = page_fns - seen_file_numbers
        if not new_fns:
            print(
                f"   🏁 Page {page_num} returned duplicate rows, stopping pagination")
            break
        seen_file_numbers.update(page_fns)

        print(
            f"   📋 Page {page_num}: {len(raw_rows)} rows ({len(new_fns)} new)")

        filtered, reached_cutoff = filter_by_cutoff(raw_rows, cutoff)
        all_records.extend(filtered)

        if reached_cutoff:
            print(
                f"   🏁 Reached cutoff date on page {page_num}, stopping pagination")
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
   - “Inc.” vs “Incorporated”
   - “Corp.” vs “Corporation”
   - “Ltd” vs “Limited”
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
        print(f"   🧠 LLM match: {result}")
        return result
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return "None"


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

    subject = f"[FRMD] German Bundeskartellamt- {file_number} (New) – {target} / {acquirer}"

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

    subject = f"[FRUD] German Bundeskartellamt- {file_number} (USA-Related) – {fn}: {pursue_en[:60]}"

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
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f"
        )
        payload = {
            "subject": subject,
            "html": html,
            "file_number": file_number,
            "source": SOURCE_INITIAL_FILING,
            "view_url": LAUFENDE_VERFAHREN_URL,
        }
        if deal_id:
            payload["deal_id"] = deal_id
        resp = requests.post(webhook_url, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=30)
        resp.raise_for_status()
        print(f"   ✅ Email sent ({resp.status_code})")
        return True
    except Exception as e:
        print(f"   ⚠️ Email failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*60}\n🚀 BUNDESKARTELLAMT LAUFENDE VERFAHREN (Proxy)\n{'='*60}\n")

    # Step 1: MongoDB init
    success, message = init_mongodb_connection(".env")
    if not success:
        print(f"❌ {message}")
        return {"success": False, "error": message}

    # Step 1a: Fetch deals
    deals = fetch_deals()
    deal_by_id = {str(d.get("deal_id", ""))
                      : d for d in deals if d.get("deal_id")}

    # Step 2: Fetch german_cases (is_open=True) for dedup
    gc_collection = get_german_cases_collection()
    if gc_collection is None:
        print("❌ german_cases collection not available")
        return {"success": False, "error": "german_cases collection unavailable"}

    existing_cases = fetch_open_german_cases(gc_collection)
    existing_file_numbers: Set[str] = set(existing_cases.keys())

    # Step 3+4+5: Fetch HTML, extract rows, paginate until cutoff
    cutoff = CUTOFF_DATE.date() if isinstance(
        CUTOFF_DATE, datetime) else CUTOFF_DATE
    print(f"📍 Fetching records (cutoff >= {cutoff})...")
    all_raw_records = fetch_all_records_with_pagination(cutoff)
    print(f"   ✅ Total records after cutoff: {len(all_raw_records)}\n")

    # Save raw extracted records
    try:
        with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
            json.dump(all_raw_records, f, ensure_ascii=False, indent=2)
        print(f"📁 Saved raw records to {EXTRACTED_RECORDS_JSON}\n")
    except Exception as e:
        print(f"⚠️ Could not save JSON: {e}\n")

    # Step 6–10: Process each record
    stats = {"new": 0, "skipped": 0, "matched": 0,
             "usa_related": 0, "saved": 0}

    print(f"{'='*60}\n🔍 Processing {len(all_raw_records)} records...\n{'='*60}\n")

    for idx, raw in enumerate(all_raw_records, 1):
        fn = raw.get("file_number", "")
        print(
            f"[{idx}/{len(all_raw_records)}] {fn} — {raw.get('pursue', '')[:60]}...")

        # Step 6: Dedup — skip if file_number exists in german_cases
        if fn in existing_file_numbers:
            print(f"  ⏩ Already in german_cases, skipping")
            stats["skipped"] += 1
            continue

        # Step 7: Translate new record
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
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

        print(f"  📋 {fn}: pursue_en={pursue_en[:50]}... | is_open={is_open}")

        # Step 8: LLM match against deals
        deal_match = None
        matched_company = ""
        matched_role = ""

        if pursue_en and pursue_en != "[Translation failed]":
            match_result = match_deal_with_llm(pursue_en, deals)
            deal_match, matched_company, matched_role = parse_llm_match(
                match_result, deal_by_id)

        if deal_match:
            # Deal matched → send [FRMD] email, save with deal_id
            record["deal_id"] = deal_match.get("deal_id")

            print(f"  🎯 Matched: {matched_company} ({matched_role})")

            subject, html = generate_matched_email(record, deal_match)
            send_email_via_webhook(
                subject, html, fn, deal_id=deal_match.get("deal_id"))
            stats["matched"] += 1

        else:
            # Step 9: Not matched → check USA-related
            print(f"  ➖ No deal match")
            try:
                company_details = {
                    "today_date": datetime.now().strftime("%Y-%m-%d"),
                    "record": record,
                }
                is_usa = verify_country_relation(
                    company_details=company_details, country="USA", case_type="GERMANY"
                )
            except Exception as e:
                print(f"  ⚠️ USA check failed: {e}")
                is_usa = False

            if is_usa:
                print(f"  🇺🇸 USA-related → sending [FRUD] email")
                subject, html = generate_usa_related_email(record)
                send_email_via_webhook(subject, html, fn)
                stats["usa_related"] += 1
            else:
                print(f"  💾 Not USA-related → silent save")

        # Step 10: Save to german_cases
        inserted_id = insert_german_case(gc_collection, record)
        if inserted_id:
            stats["saved"] += 1
            stats["new"] += 1
            existing_file_numbers.add(fn)
            print(f"  ✅ Saved to german_cases (id={inserted_id})")
        else:
            print(f"  ❌ Failed to save to german_cases")

    # Summary
    print(f"\n{'='*60}\n✅ DONE\n{'='*60}")
    print(f"📊 Total extracted: {len(all_raw_records)}")
    print(f"⏩ Skipped (existing): {stats['skipped']}")
    print(f"🆕 New records saved: {stats['new']}")
    print(f"🎯 Deal matches: {stats['matched']}")
    print(f"🇺🇸 USA-related: {stats['usa_related']}")
    print(f"📁 JSON: {EXTRACTED_RECORDS_JSON}\n")

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


if __name__ == "__main__":
    main()
