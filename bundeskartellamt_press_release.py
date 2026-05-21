"""
Bundeskartellamt Press Release scraper.

Fetches the Expertensuche press releases URL, scrapes the search result list
(section#searchResults / .l-searchresult-list), extracts title, url, date, category.
Applies cutoff date, matches headline to deal companies via LLM, and appends
entries to the deal's german_scrap array with source: press_release.
Saves to DB and sends email via n8n webhook. No USA-related verification.
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

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Press releases search URL (Pressemeldungen & Aktuelles, sorted by date desc)
PRESS_RELEASE_BASE = "https://www.bundeskartellamt.de/SiteGlobals/Forms/Suche/Expertensuche_Formular.html"
PRESS_RELEASE_PARAMS = "cl2Categories_CategorizedFormat=pressemeldungen_aktuelles&pageLocale=de&resultsPerPage=15&sortOrder=dateOfIssue_dt+desc"
PRESS_RELEASE_URL = f"{PRESS_RELEASE_BASE}?{PRESS_RELEASE_PARAMS}#resultsperpage-51534"

EXTRACTED_RECORDS_JSON = "bundeskartellamt_press_release_extracted.json"

SOURCE_PRESS_RELEASE = "press_release"

BASE_URL = os.getenv("BASE_URL")
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_INTERNAL_WITH_JOSH",
    f"{BASE_URL}/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

# CUTOFF_DATE: Only process records with date >= this date.
CUTOFF_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.strptime("2026-01-25", "%Y-%m-%d")

deals = []


def get_deals_from_mongodb():
    """Fetch all deals from MongoDB, restricted to active/open statuses."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Only fetch deals where deal_status is Open, Unknown, null, or not set
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        all_deals = list(collection.find(status_filter))
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
    deals = get_deals_from_mongodb()
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
        params = {"client": "gtx", "sl": "auto",
                  "tl": "en", "dt": "t", "q": text}
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
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


def parse_press_date(date_str):
    """Parse date from press release topline, e.g. 'January 30, 2026' or 'December 22, 2025'. Returns date or None."""
    if not date_str or not date_str.strip():
        return None
    s = date_str.strip()
    # English month names
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def extract_press_results(html_content):
    """
    Extract press release items from search results HTML.
    Structure: section#searchResults or .l-searchresult-list, items .l-searchresult-list__item.
    Each item: h3.c-searchresult__headline > a (title, href), p.c-topline (category, date).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    records = []

    section = soup.find("section", id="searchResults") or soup.find(
        "div", class_=re.compile(r"l-searchresult-list"))
    if not section:
        # Fallback: any container with list items
        items = soup.find_all("div", class_=re.compile(
            r"l-searchresult-list__item"))
    else:
        items = section.find_all(
            "div", class_=re.compile(r"l-searchresult-list__item"))

    for item in items:
        try:
            headline_el = item.find(
                "h3", class_=re.compile(r"c-searchresult__headline"))
            if not headline_el:
                continue
            link = headline_el.find("a", href=True)
            if not link:
                continue
            title = link.get_text(separator=" ", strip=True)
            title = re.sub(r"\s+", " ", title).strip()
            url = link.get("href", "").strip()
            if url and not url.startswith("http"):
                url = requests.compat.urljoin(
                    "https://www.bundeskartellamt.de", url)

            topline = item.find("p", class_=re.compile(
                r"c-searchresult__topline"))
            category = ""
            date_str = ""
            if topline:
                spans = topline.find_all(
                    "span", class_=re.compile(r"c-topline__item"))
                if len(spans) >= 1:
                    category = spans[0].get_text(strip=True)
                if len(spans) >= 2:
                    date_str = spans[1].get_text(strip=True)

            # Title in German (as scraped from site) and English (translated)
            title_german = title
            title_english = translate_to_english(title) if title else ""

            record = {
                "title": title,
                "title_german": title_german,
                "title_english": title_english,
                "url": url,
                "date_str": date_str,
                "date": parse_press_date(date_str),
                "category": category,
            }
            records.append(record)
            print(f"📋 Extracted: {date_str} – {title[:60]}...")
        except Exception as e:
            print(f"⚠️ Error extracting item: {e}")
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
        d = r.get("date")
        if d is not None and d >= cutoff:
            filtered.append(r)
        elif d is None:
            filtered.append(r)
    return filtered


def match_deal_with_llm(headline_text, all_companies):
    """Match press release headline with deal companies using LLM."""
    if not headline_text or not headline_text.strip():
        return None
    prompt = f"""
You are an M&A deal analyst. Given a Bundeskartellamt press release headline (in English), determine whether it explicitly relates to any of the companies listed below.

- Match only if the company name or a well-known alias appears in the headline.
- Ignore similar-sounding names or partial matches.
- Accept suffix variations (Inc., Ltd., PLC, GmbH, AG, SE).

Companies:
{', '.join(sorted(all_companies))}

Headline:
{headline_text.strip()}

If there's a match, return in this format:
Match: COMPANY_NAME (acquirer|target)

If not, return:
None
"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition."},
                {"role": "user", "content": prompt},
            ]
        )
        return response.choices[0].message.content.strip()
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
    if val is None or (isinstance(val, str) and not val.strip()):
        return "N/A"
    return escape_html(str(val).strip())


def generate_press_release_email_html(record_data, deal_match, updated_fields=None):
    """Generate HTML email for press release match."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    title_german = record_data.get(
        "title_german") or record_data.get("title") or "N/A"
    title_english = record_data.get("title_english") or "N/A"
    url = record_data.get("url") or "N/A"
    date_str = record_data.get("date_str") or "N/A"
    category = record_data.get("category") or "N/A"

    if updated_fields:
        title_text = f"[FRMD] German Bundeskartellamt Press Release (Updated) – {target} / {acquirer}"
        update_note = f"<p style='color:#e74c3c; font-weight:bold; padding:10px; background-color:#ffe6e6; border-radius:4px;'>⚠️ This record was updated. Changed fields: {', '.join(updated_fields)}</p>"
    else:
        title_text = f"[FRMD] German Bundeskartellamt Press Release (New) – {target} / {acquirer}"
        update_note = "<p style='color:#27ae60; font-weight:bold; padding:10px; background-color:#e6ffe6; border-radius:4px;'>✅ New press release added</p>"

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
    <p style="color:#666; text-align:center;">Source: Press release</p>
    {update_note}
    <p style="margin-bottom:16px;">
      <strong>View press release:</strong>
      <a href="{escape_html(url)}" style="color:#e74c3c; text-decoration:underline;" target="_blank">Open in browser</a>
    </p>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr><td style="padding:8px; font-weight:bold; width:170px; color:#555;">Deal ID:</td><td style="{cell_style}">{_safe_email_text(deal_id)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Target:</td><td style="{cell_style}">{_safe_email_text(target)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Acquirer:</td><td style="{cell_style}">{_safe_email_text(acquirer)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Date:</td><td style="{cell_style}">{_safe_email_text(date_str)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Category:</td><td style="{cell_style}">{_safe_email_text(category)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">Title (German):</td><td style="{cell_style}">{_safe_email_text(title_german)}</td></tr>
      <tr><td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td><td style="{cell_style}">{_safe_email_text(title_english)}</td></tr>
      <tr style="background-color:#f9f9f9;"><td style="padding:8px; font-weight:bold; color:#555;">URL:</td><td style="{cell_style}"><a href="{escape_html(url)}" target="_blank" style="color:#e74c3c;">{_safe_email_text(url)}</a></td></tr>
    </table>
    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>Automated email from Bundeskartellamt Press Release scraper.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_press_release_email_via_webhook(record_data, deal_match, updated_fields=None):
    """Send email via n8n webhook for press release match."""
    try:
        subject, html_email = generate_press_release_email_html(
            record_data, deal_match, updated_fields)
        print(f"📝 Generated email subject: {subject}")

        webhook_url = N8N_WEBHOOK_URL

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
            "title": (record_data.get("title") or "").strip(),
            "title_german": (record_data.get("title_german") or record_data.get("title") or "").strip(),
            "title_english": (record_data.get("title_english") or "").strip(),
            "url": (record_data.get("url") or "").strip(),
            "date_str": (record_data.get("date_str") or "").strip(),
            "category": (record_data.get("category") or "").strip(),
            "source": SOURCE_PRESS_RELEASE,
            "updated_fields": updated_fields if updated_fields else [],
            "view_url": record_data.get("url", ""),
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
    """Ensure german_scrap is a list. If legacy object, convert to single-element list."""
    if german_scrap is None:
        return []
    if isinstance(german_scrap, list):
        return german_scrap
    item = dict(german_scrap)
    if "source" not in item:
        item["source"] = "hauptpruefverfahren"
    return [item]


def append_or_update_press_release_to_deal(deal_match, record, matched_company_raw, matched_role):
    """
    Append or update a press_release entry in the deal's german_scrap array.
    If an entry with same url and source==SOURCE_PRESS_RELEASE exists, update it; else append.
    """
    try:
        print("💾 Saving to deal german_scrap array (press_release)...")
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available")
            return False

        record_url = (record.get("url") or "").strip()
        german_scrap_entry = {
            "source": SOURCE_PRESS_RELEASE,
            "title": (record.get("title") or "").strip(),
            "title_german": (record.get("title_german") or record.get("title") or "").strip(),
            "title_english": (record.get("title_english") or "").strip(),
            "url": record_url,
            "date_str": (record.get("date_str") or "").strip(),
            "date": record.get("date").isoformat() if isinstance(record.get("date"), date) else (record.get("date") or ""),
            "category": (record.get("category") or "").strip(),
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
            if isinstance(el, dict) and (el.get("url") or "").strip() == record_url and el.get("source") == SOURCE_PRESS_RELEASE:
                found_index = i
                updated_fields = []
                for key, new_value in german_scrap_entry.items():
                    if key == "url":
                        continue
                    old_value = el.get(key)
                    if new_value == "":
                        new_value = None
                    if old_value == "":
                        old_value = None
                    if new_value != old_value:
                        updated_fields.append(key)
                break

        if found_index is not None:
            arr[found_index] = german_scrap_entry
        else:
            arr.append(german_scrap_entry)
            updated_fields = None

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
                    send_press_release_email_via_webhook(
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
    """Match extracted press releases with deals via LLM and append/update german_scrap array (press_release)."""
    print(
        f"\n{'='*60}\n🔍 Matching {len(records)} press releases with deals...\n{'='*60}\n")

    global deals
    if not deals:
        load_deals()

    all_companies = set()
    for deal in deals:
        if deal.get("acquirer") or deal.get("acquire_name"):
            all_companies.add(normalize_company(
                deal.get("acquirer") or deal.get("acquire_name", "")))
        if deal.get("target") or deal.get("target_name"):
            all_companies.add(normalize_company(
                deal.get("target") or deal.get("target_name", "")))

    matched_count = 0

    for idx, record in enumerate(records, 1):
        title = record.get("title", "")
        print(f"[{idx}/{len(records)}] {title[:70]}...")

        if not title or not title.strip():
            print("  ⏩ Skipped (no title)")
            continue

        match_result = match_deal_with_llm(title, all_companies)

        if match_result and match_result.lower() != "none" and "match:" in match_result.lower():
            try:
                match_pattern = r"Match:\s*([^(]+)\s*\((\w+)\)"
                match_obj = re.search(
                    match_pattern, match_result, re.IGNORECASE)
                if match_obj:
                    matched_company_raw = match_obj.group(1).strip()
                    matched_role = match_obj.group(2).strip().lower()
                    matched_company_normalized = normalize_company(
                        matched_company_raw)
                    print(f"  🎯 Match: {matched_company_raw} ({matched_role})")

                    deal_found = None
                    for deal in deals:
                        acquirer = deal.get("acquirer") or deal.get(
                            "acquire_name", "")
                        target = deal.get("target") or deal.get(
                            "target_name", "")
                        if normalize_company(acquirer) == matched_company_normalized or normalize_company(target) == matched_company_normalized:
                            deal_found = deal
                            break

                    if deal_found:
                        save_ok = append_or_update_press_release_to_deal(
                            deal_found, record, matched_company_raw, matched_role
                        )
                        if save_ok:
                            matched_count += 1
                        else:
                            print("  ⚠️ Failed to save to MongoDB")
                    else:
                        print("  ⚠️ Deal not found in list")
                else:
                    print(f"  ⚠️ Could not parse match: {match_result}")
            except Exception as e:
                print(f"  ⚠️ Error processing match: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("  ➖ No match")

    print(f"\n{'='*60}\n✅ Matching complete: {matched_count} new/updated in german_scrap\n{'='*60}\n")
    return matched_count


def main():
    """
    Fetch press release search URL, extract list, filter by cutoff date,
    match with deals via LLM, append/update german_scrap array (press_release).
    """
    global deals

    print(f"\n{'='*60}\n🚀 BUNDESKARTELLAMT PRESS RELEASE SCRAPER\n{'='*60}\n")

    success, message = init_mongodb_connection(".env")
    if not success:
        print(f"❌ {message}")
        return {"success": False, "error": message}

    load_deals()

    print(f"📍 Fetching HTML from {PRESS_RELEASE_URL}")
    html_content = None
    max_retries = 3
    wait_seconds = 5
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(PRESS_RELEASE_URL, timeout=30)
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

    print("📍 Extracting press release list...")
    records = extract_press_results(html_content)
    print(f"   ✅ Extracted {len(records)} items\n")

    print(f"📍 Applying cutoff date (>= {CUTOFF_DATE.date()})...")
    records = filter_by_cutoff_date(records)
    print(f"   ✅ After cutoff: {len(records)} records\n")

    # Serialize for JSON (date -> str)
    records_serializable = []
    for r in records:
        rec = dict(r)
        if isinstance(rec.get("date"), date):
            rec["date"] = rec["date"].isoformat()
        records_serializable.append(rec)

    print(f"📍 Saving extracted records to {EXTRACTED_RECORDS_JSON}")
    try:
        with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
            json.dump(records_serializable, f, ensure_ascii=False, indent=2)
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
