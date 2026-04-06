"""
Bundeskartellamt Laufende Verfahren (Ongoing Proceedings) scraper.

Scrapes the Laufende Verfahren form URL, extracts the HTML table (Datum, Aktenzeichen,
Unternehmen, Produktbereich, Abschluss), applies a cutoff date filter, matches
"Unternehmen" (pursue) to deal companies via LLM, and appends/updates entries
in the deal's german_scrap array (source: initial_filing). Future press releases
can be added to the same array with source: press_release.
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, date
from bson import ObjectId
from mongodb_connection import get_deals_collection, is_connected, init_mongodb_connection
from html import escape as escape_html
from llm_verification_service import verify_country_relation

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Laufende Verfahren form URL (updated link)
BASE_URL = "https://www.bundeskartellamt.de/SiteGlobals/Forms/Suche/LaufendeVerfahren/LaufendeVerfahren_Formular.html"
URL_PARAMS = "resourceId=83476&pageLocale=de&input_=86272&submit=Send&resultsPerPage=15"
LAUFENDE_VERFAHREN_URL = f"{BASE_URL}?{URL_PARAMS}#resultsperpage-83488"

EXTRACTED_RECORDS_JSON = "bundeskartellamt_laufende_verfahren_extracted.json"

# Source type for this scraper; future press releases use "press_release"
SOURCE_INITIAL_FILING = "initial_filing"

# CUTOFF_DATE: Only process records with date >= this date.
# Example: If CUTOFF_DATE = 2026-01-15, keep 2026-01-15 and newer.
CUTOFF_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.strptime("2026-01-28", "%Y-%m-%d")

deals = []


def get_deals_from_mongodb(include_german_scrap=True):
    """Fetch deals from MongoDB (all deals for matching)."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Base filter: only include deals with deal_status Open/Unknown/null/missing
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        # Optional filter: exclude deals that already have german_scrap
        if not include_german_scrap:
            german_filter = {
                "$or": [
                    {"german_scrap": {"$exists": False}},
                    {"german_scrap": None},
                    {"german_scrap": []},
                    {"german_scrap": {}},
                ]
            }
            query = {"$and": [status_filter, german_filter]}
        else:
            query = status_filter

        all_deals = list(collection.find(query))
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals():
    """Load deals from MongoDB."""
    global deals
    deals = get_deals_from_mongodb(include_german_scrap=True)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def normalize_company(name):
    """Normalize company name for matching."""
    if not name:
        return ""
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").replace(" limited", "").replace(" corporation", "").replace(" corp.", "").replace(" gmbh", "").replace(" ag", "").replace(" se", "").strip()


def translate_to_english(text):
    """Translate German text to English using Google Translate API. Returns full text (all segments concatenated)."""
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
            # API can return multiple segments for long text; concatenate all for complete translation
            segments = data[0] if data and isinstance(data[0], list) else []
            parts = []
            for seg in segments:
                if isinstance(seg, (list, tuple)) and seg and seg[0]:
                    parts.append(seg[0].strip())
            if parts:
                return " ".join(parts).strip()
            try:
                return (data[0][0][0] or "").strip() if data and len(data) and data[0] and len(data[0]) and data[0][0] else ""
            except (IndexError, TypeError, KeyError):
                return ""
    except Exception as e:
        print(f"⚠️ Translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


def parse_table_date(date_str):
    """Parse date from table format DD.MM.YYYY. Returns date or None."""
    if not date_str or not date_str.strip():
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def extract_table_data(html_content):
    """
    Extract table data from Laufende Verfahren HTML.
    Table columns: Datum, Aktenzeichen, Unternehmen, Produktbereich, Abschluss (5 cols, no documents).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    records = []

    table = soup.find("table")
    if not table:
        print("⚠️ No table found in HTML")
        return records

    rows = table.find_all("tr")[1:]  # skip header

    for row in rows:
        try:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            date_cell = cells[0].get_text(separator=" ", strip=True)
            file_number_cell = cells[1].get_text(separator=" ", strip=True)
            pursue_cell = cells[2].get_text(separator=" ", strip=True)
            product_area_cell = cells[3].get_text(separator=" ", strip=True)
            abschluss_cell = cells[4].get_text(separator=" ", strip=True)

            date_cell = re.sub(r"\s+", " ", date_cell).strip()
            file_number_cell = re.sub(r"\s+", " ", file_number_cell).strip()
            pursue_cell = re.sub(r"\s+", " ", pursue_cell).strip()
            product_area_cell = re.sub(r"\s+", " ", product_area_cell).strip()
            abschluss_cell = re.sub(r"\s+", " ", abschluss_cell).strip()

            pursue_en = translate_to_english(
                pursue_cell) if pursue_cell else ""
            product_area_en = translate_to_english(
                product_area_cell) if product_area_cell else ""
            abschluss_en = translate_to_english(
                abschluss_cell) if abschluss_cell else ""

            record = {
                "date": date_cell,
                "file_number": file_number_cell,
                "pursue": pursue_cell,
                "pursue_en": pursue_en,
                "product_area": product_area_cell,
                "product_area_en": product_area_en,
                "diploma": abschluss_cell,
                "diploma_en": abschluss_en,
                "documents": [],
            }
            records.append(record)
            print(
                f"📋 Extracted: {file_number_cell} - {pursue_en[:60] if pursue_en else pursue_cell[:60]}...")
        except Exception as e:
            print(f"⚠️ Error extracting row: {e}")
            continue

    return records


def filter_by_cutoff_date(records, cutoff_date=None):
    """Keep only records with date >= CUTOFF_DATE."""
    if cutoff_date is None:
        cutoff_date = CUTOFF_DATE
    cutoff = cutoff_date.date() if isinstance(
        cutoff_date, datetime) else cutoff_date
    filtered = []
    for r in records:
        d = parse_table_date(r.get("date", ""))
        if d is not None and d >= cutoff:
            filtered.append(r)
        elif d is None:
            filtered.append(r)  # keep if unparseable
    return filtered


def match_deal_with_llm(pursue_text_en, deals):
    """Match pursue text with deal companies using LLM. Returns Match: DEAL_ID|COMPANY_NAME|(target|acquirer) or None."""
    if not pursue_text_en or pursue_text_en == "[Translation failed]":
        return None

    # Build deals list with all relevant information (including aliases)
    deals_list = []
    for deal in deals:
        deal_info = {
            "deal_id": deal.get("deal_id", ""),
        }
        target = deal.get("target") or deal.get("target_name", "")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")

        if target:
            deal_info["target"] = target
        if acquirer:
            deal_info["acquirer"] = acquirer

        target_aliases = deal.get("target_aliases") or []
        parent_aliases = deal.get("parent_aliases") or []
        if isinstance(target_aliases, list) and target_aliases:
            deal_info["target_aliases"] = target_aliases
        if isinstance(parent_aliases, list) and parent_aliases:
            deal_info["parent_aliases"] = parent_aliases

        if target or acquirer:
            deals_list.append(deal_info)

    if not deals_list:
        return "None"

    # Build structured prompt with deal information (including aliases)
    lines = []
    for d in deals_list:
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target', 'N/A')} | Acquirer: {d.get('acquirer', 'N/A')}"
        target_aliases = d.get("target_aliases", []) or []
        parent_aliases = d.get("parent_aliases", []) or []
        if target_aliases:
            line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
        if parent_aliases:
            line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""
You are an M&A deal analyst. Given the translated text about a German merger case (Laufende Verfahren), determine whether it explicitly relates to any of the deals listed below.

DEALS TO MATCH:
{deals_text}

TRANSLATED TEXT:
{pursue_text_en}

INSTRUCTIONS:
1. Compare the translated text with BOTH Target and Acquirer names in the deals list.
2. When matching, also consider target_aliases and parent_aliases - if the text matches an alias, treat it as a match for that deal.
3. Match only if the company name or a well-known alias appears in the translated text.
4. Look for EXACT matches, partial matches, or variations of company names.
5. Accept suffix variations (Inc., Ltd., PLC, GmbH, AG, SE).

RESPONSE FORMAT:
- If you find a match, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|General Motors|acquirer

- If NO match is found, respond with:
  None

IMPORTANT: For each match, include the exact Deal ID from the DEALS TO MATCH list.
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
        print(f"   🧠 LLM Response: {result}")
        return result
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return "None"


def convert_datetime_to_string(obj):
    """Recursively convert datetime/date to strings for JSON/MongoDB."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if hasattr(obj, "isoformat") and callable(getattr(obj, "isoformat")):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    if isinstance(obj, dict):
        return {k: convert_datetime_to_string(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_datetime_to_string(item) for item in obj]
    return obj


def _safe_email_text(val):
    """Return full value for email display; no truncation. Use N/A for missing/empty."""
    if val is None or (isinstance(val, str) and not val.strip()):
        return "N/A"
    return escape_html(str(val).strip())


def generate_initial_filing_email_html(german_scrap_data, deal_match, updated_fields=None):
    """Generate HTML email for Laufende Verfahren (initial filing) match."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    file_number = german_scrap_data.get("file_number") or "N/A"
    date_val = german_scrap_data.get("date") or "N/A"
    pursue = german_scrap_data.get("pursue") or "N/A"
    pursue_en = german_scrap_data.get("pursue_en") or "N/A"
    product_area = german_scrap_data.get("product_area") or "N/A"
    product_area_en = german_scrap_data.get("product_area_en") or "N/A"
    diploma = german_scrap_data.get("diploma") or "N/A"
    diploma_en = german_scrap_data.get("diploma_en") or "N/A"
    view_url = LAUFENDE_VERFAHREN_URL

    if updated_fields:
        title_text = f"[FRMD] German Bundeskartellamt Initial Filing (Updated) – {target} / {acquirer}"
        update_note = f"<p style='color:#e74c3c; font-weight:bold; padding:10px; background-color:#ffe6e6; border-radius:4px;'>⚠️ This record was updated. Changed fields: {', '.join(updated_fields)}</p>"
    else:
        title_text = f"[FRMD] German Bundeskartellamt Initial Filing (New) – {target} / {acquirer}"
        update_note = "<p style='color:#27ae60; font-weight:bold; padding:10px; background-color:#e6ffe6; border-radius:4px;'>✅ New initial filing added</p>"

    subject = title_text
    cell_style = "padding:8px; color:#333; word-wrap:break-word; white-space:normal; max-width:600px;"
    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #e74c3c;">
      {escape_html(title_text)}
    </h2>
    <p style="color:#666; text-align:center;">Source: Laufende Verfahren (initial filing)</p>
    {update_note}
    <p style="margin-bottom:16px;">
      <strong>View update online:</strong>
      <a href="{escape_html(view_url)}" style="color:#e74c3c; text-decoration:underline;" target="_blank">Laufende Verfahren – open in browser</a>
    </p>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr><td style="padding:8px; font-weight:bold; width:170px; color:#555;">Deal ID:</td><td style="{cell_style}">{_safe_email_text(deal_id)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Target:</td><td style="{cell_style}">{_safe_email_text(target)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Acquirer:</td><td style="{cell_style}">{_safe_email_text(acquirer)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">File Number:</td><td style="{cell_style}">{_safe_email_text(file_number)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Date:</td><td style="{cell_style}">{_safe_email_text(date_val)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Unternehmen (German):</td><td style="{cell_style}">{_safe_email_text(pursue)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">undertaking (English):</td><td style="{cell_style}">{_safe_email_text(pursue_en)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Produktbereich (German):</td><td style="{cell_style}">{_safe_email_text(product_area)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Product area (English):</td><td style="{cell_style}">{_safe_email_text(product_area_en)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Abschluss (German):</td><td style="{cell_style}">{_safe_email_text(diploma)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Diploma (English):</td><td style="{cell_style}">{_safe_email_text(diploma_en)}</td></tr>
    </table>
    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>Automated email from Bundeskartellamt Laufende Verfahren scraper.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_initial_filing_email_via_webhook(german_scrap_data, deal_match, updated_fields=None):
    """Send email via n8n webhook for initial filing match."""
    try:
        subject, html_email = generate_initial_filing_email_html(
            german_scrap_data, deal_match, updated_fields)
        print(f"📝 Generated email subject: {subject}")
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        # Test webhook
        # webhook_url = os.getenv(
        #     "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/80830c6d-ff5b-45e3-9ef3-a061db1fbf0c")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": deal_match.get("deal_id", "N/A"),
            "target": target,
            "acquirer": acquirer,
            "file_number": german_scrap_data.get("file_number", "N/A"),
            "date": german_scrap_data.get("date", "N/A"),
            "pursue": (german_scrap_data.get("pursue") or "").strip(),
            "pursue_en": (german_scrap_data.get("pursue_en") or "").strip(),
            "product_area": (german_scrap_data.get("product_area") or "").strip(),
            "product_area_en": (german_scrap_data.get("product_area_en") or "").strip(),
            "diploma": (german_scrap_data.get("diploma") or "").strip(),
            "diploma_en": (german_scrap_data.get("diploma_en") or "").strip(),
            "source": SOURCE_INITIAL_FILING,
            "updated_fields": updated_fields if updated_fields else [],
            "view_url": LAUFENDE_VERFAHREN_URL,
        }
        response = requests.post(webhook_url, json=payload, headers={
                                 "Content-Type": "application/json"}, timeout=30)
        response.raise_for_status()
        print(
            f"✅ Email sent successfully via n8n webhook! Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def _ensure_german_scrap_array(german_scrap):
    """Ensure german_scrap is a list. If legacy object, convert to single-element list with source."""
    if german_scrap is None:
        return []
    if isinstance(german_scrap, list):
        return german_scrap
    # Legacy: single object → list with one item, add source if missing
    item = dict(german_scrap)
    if "source" not in item:
        item["source"] = "hauptpruefverfahren"  # legacy main page
    return [item]


def append_or_update_initial_filing_to_deal(deal_match, record, matched_company_raw, matched_role):
    """
    Append or update an initial_filing entry in the deal's german_scrap array.
    If an entry with same file_number and source==SOURCE_INITIAL_FILING exists, update it; else append.
    """
    try:
        print("💾 Saving to deal german_scrap array (initial_filing)...")
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available")
            return False

        file_number = record.get("file_number", "")
        german_scrap_entry = {
            "source": SOURCE_INITIAL_FILING,
            "date": record.get("date", ""),
            "file_number": file_number,
            "pursue": record.get("pursue", ""),
            "pursue_en": record.get("pursue_en", ""),
            "product_area": record.get("product_area", ""),
            "product_area_en": record.get("product_area_en", ""),
            "diploma": record.get("diploma", ""),
            "diploma_en": record.get("diploma_en", ""),
            "documents": record.get("documents", []),
            "matched_company": matched_company_raw,
            "matched_role": matched_role,
        }
        german_scrap_entry = convert_datetime_to_string(german_scrap_entry)

        query = {}
        if deal_match.get("deal_id"):
            try:
                query["_id"] = ObjectId(deal_match["deal_id"])
            except Exception:
                query = {}
        if not query:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name")
            target = deal_match.get("target") or deal_match.get("target_name")
            or_conditions = []
            if acquirer:
                or_conditions.append({"acquirer": acquirer})
                or_conditions.append({"acquire_name": acquirer})
            if target:
                or_conditions.append({"target": target})
                or_conditions.append({"target_name": target})
            if or_conditions:
                query = {"$or": or_conditions}
        if not query:
            print("⚠️ Cannot identify deal, skipping MongoDB save")
            return False

        deal_doc = collection.find_one(query)
        if not deal_doc:
            print(f"⚠️ Deal not found in MongoDB: {query}")
            return False

        current = deal_doc.get("german_scrap")
        arr = _ensure_german_scrap_array(current)

        updated_fields = None
        found_index = None
        for i, el in enumerate(arr):
            if isinstance(el, dict) and el.get("file_number") == file_number and el.get("source") == SOURCE_INITIAL_FILING:
                found_index = i
                updated_fields = []
                for key, new_value in german_scrap_entry.items():
                    if key == "file_number":
                        continue
                    old_value = el.get(key)
                    if new_value == "":
                        new_value = None
                    if old_value == "":
                        old_value = None
                    if isinstance(new_value, list) and isinstance(old_value, list):
                        try:
                            if json.dumps(new_value, sort_keys=True) != json.dumps(old_value, sort_keys=True):
                                updated_fields.append(key)
                        except Exception:
                            if new_value != old_value:
                                updated_fields.append(key)
                    elif new_value != old_value:
                        updated_fields.append(key)
                break

        if found_index is not None:
            arr[found_index] = german_scrap_entry
        else:
            arr.append(german_scrap_entry)
            updated_fields = None  # new entry

        update_result = collection.update_one(
            {"_id": deal_doc["_id"]},
            {"$set": {"german_scrap": arr}},
        )

        if update_result.modified_count > 0:
            print("✅ Updated deal german_scrap array in MongoDB")
            should_send = updated_fields is None or (
                updated_fields and len(updated_fields) > 0)
            if should_send:
                try:
                    send_initial_filing_email_via_webhook(
                        german_scrap_entry, deal_match, updated_fields)
                except Exception as e:
                    print(f"⚠️ Error sending email: {e}")
            return True
        else:
            print("ℹ️ No changes written (data may be identical)")
            return True
    except Exception as e:
        print(f"❌ Error saving to MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return False


def match_records_with_deals(records):
    """Match extracted records with deals via LLM and append/update german_scrap array (initial_filing)."""
    print(f"\n{'='*60}\n🔍 Matching {len(records)} records with deals...\n{'='*60}\n")

    global deals
    if not deals:
        load_deals()

    # Build deal_id -> deal lookup for direct identification
    deal_by_id = {str(d.get("deal_id", ""))                  : d for d in deals if d.get("deal_id")}

    matched_count = 0

    for idx, record in enumerate(records, 1):
        file_number = record.get("file_number", "")
        pursue_en = record.get("pursue_en", "")

        print(f"[{idx}/{len(records)}] {file_number} - {pursue_en[:70]}...")

        if pursue_en == "[Translation failed]":
            print("  ⏩ Skipped (translation failed)")
            continue

        match_result = match_deal_with_llm(pursue_en, deals)

        # Parse "Match: DEAL_ID|COMPANY_NAME|(target|acquirer)" or "None"
        deal_match = None
        matched_company_raw = ""
        matched_role = ""
        if match_result and str(match_result).strip().lower() != "none":
            stripped = str(match_result).strip()
            if stripped.lower().startswith("match:"):
                parts = stripped[6:].strip().split("|")  # Remove "Match:"
                if len(parts) >= 3:
                    llm_deal_id = parts[0].strip()
                    matched_company_raw = parts[1].strip()
                    role_raw = parts[2].strip().lower().replace(
                        "(", "").replace(")", "")
                    matched_role = role_raw if role_raw in (
                        "target", "acquirer") else "acquirer"
                    if llm_deal_id in deal_by_id:
                        deal_match = deal_by_id[llm_deal_id]

        if deal_match and matched_company_raw and matched_role:
            print(f"  🎯 Match: {matched_company_raw} ({matched_role})")
            save_ok = append_or_update_initial_filing_to_deal(
                deal_match, record, matched_company_raw, matched_role
            )
            if save_ok:
                matched_count += 1
            else:
                print("  ⚠️ Failed to save to MongoDB")

        else:
            print("  ➖ No match")
            try:
                company_details = {
                    "today_date": datetime.now().strftime("%Y-%m-%d"),
                    "record": record,
                }
                if verify_country_relation(company_details=company_details, country="USA", case_type="GERMANY"):
                    print("   🇺🇸 USA-related – could send unmatched email (optional)")
            except Exception as e:
                print(f"   ⚠️ Error verifying USA relation: {e}")

    print(f"\n{'='*60}\n✅ Matching complete: {matched_count} new/updated in german_scrap\n{'='*60}\n")
    return matched_count


def main():
    """
    Scrape Laufende Verfahren URL, extract table, filter by cutoff date,
    match with deals via LLM, append/update german_scrap array (initial_filing).
    """
    global deals

    print(f"\n{'='*60}\n🚀 BUNDESKARTELLAMT LAUFENDE VERFAHREN\n{'='*60}\n")

    success, message = init_mongodb_connection(".env")
    if not success:
        print(f"❌ {message}")
        return {"success": False, "error": message}

    load_deals()

    print(f"📍 Fetching HTML from {LAUFENDE_VERFAHREN_URL}")
    html_content = None
    max_retries = 3
    wait_seconds = 5
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(LAUFENDE_VERFAHREN_URL, timeout=30)
            response.raise_for_status()
            html_content = response.text
            print(f"   ✅ HTML fetched ({len(html_content)} bytes)\n")
            break
        except Exception as e:
            print(f"   ❌ Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                print(f"   ⏳ Waiting {wait_seconds} sec before retry...")
                time.sleep(wait_seconds)
            else:
                print(f"   ❌ All {max_retries} attempts failed.")
                return {"success": False, "error": str(e)}

    print("📍 Extracting table data (5 columns)...")
    records = extract_table_data(html_content)
    print(f"   ✅ Extracted {len(records)} rows\n")

    print(f"📍 Applying cutoff date (>= {CUTOFF_DATE.date()})...")
    records = filter_by_cutoff_date(records)
    print(f"   ✅ After cutoff: {len(records)} records\n")

    print(f"📍 Saving extracted records to {EXTRACTED_RECORDS_JSON}")
    try:
        with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print("   ✅ Saved\n")
    except Exception as e:
        print(f"   ⚠️ Could not save JSON: {e}\n")

    print("📍 Matching records with deals and updating german_scrap array...")
    total_matched = match_records_with_deals(records)

    result = {
        "success": True,
        "extraction_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_extracted": len(records),
        "total_matched": total_matched,
        "cutoff_date": CUTOFF_DATE.strftime("%Y-%m-%d"),
    }
    print(f"\n{'='*60}\n✅ DONE\n{'='*60}")
    print(f"📊 Records (after cutoff): {len(records)}")
    print(f"🎯 Matches/updates in german_scrap: {total_matched}")
    print(f"📁 JSON: {EXTRACTED_RECORDS_JSON}\n")
    return result


if __name__ == "__main__":
    main()
