"""
FTC Early Termination Notices Scraper
=====================================
Scrapes early termination notices from FTC legal library (page 0 and 1),
filters by current date, matches with deals in MongoDB via LLM,
saves matched records to deals under 'ftc_early_termination' node,
and sends email notifications via n8n webhook.
For unmatched records, checks if USA-related and sends email if true.
"""

import json
import os
import re
from datetime import datetime
from html import escape as escape_html

import requests
from bs4 import BeautifulSoup
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI

from llm_verification_service import verify_usa_relation
from mongodb_connection import (
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# URLs to scrape
FTC_URLS = [
    "https://www.ftc.gov/legal-library/browse/early-termination-notices?page=0",
    "https://www.ftc.gov/legal-library/browse/early-termination-notices?page=1",
]

# Filter: only process records with date >= CUTOFF_DATE (today)
CUTOFF_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.strptime(
#     "2026-02-09", "%Y-%m-%d")

OUTPUT_PATH = "ftc_early_termination_matched_deals.json"
ENV_PATH = ".env"

# Global deals list - loaded from MongoDB
deals = []
matched_data = []
matched_count = 0


def get_deals_from_mongodb(include_ftc=False):
    """
    Fetch deals from MongoDB. Optionally exclude deals that already have ftc_early_termination.
    """
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        query = {}
        if not include_ftc:
            query = {
                "$or": [
                    {"ftc_early_termination": {"$exists": False}},
                    {"ftc_early_termination": None},
                    {"ftc_early_termination": []},
                    {"ftc_early_termination": {}}
                ]
            }

        all_deals = list(collection.find(query))
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'ftc_early_termination' node" if not include_ftc else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_ftc=False):
    """Load deals from MongoDB."""
    global deals
    deals = get_deals_from_mongodb(include_ftc=include_ftc)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def match_with_llm(title):
    """Use LLM to match case title with a deal. Returns match string or None."""
    deals_text = "\n".join([
        f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target_name', d.get('target', 'N/A'))} | Acquirer: {d.get('acquire_name', d.get('acquirer', 'N/A'))}"
        for d in deals
    ])

    prompt = f"""You are an expert M&A deal matcher. Determine if ANY company mentioned in the FTC early termination notice appears in our deals database.

DEALS DATABASE:
{deals_text}

FTC EARLY TERMINATION NOTICE (Acquiring Party; Acquired Party):
{title}

INSTRUCTIONS:
1. Extract ALL company names from the notice (acquiring party and acquired party).
2. Check if ANY of these names appears as either Target OR Acquirer in the deals database.
3. Consider variations, abbreviations, and partial matches (e.g., "Coursera, Inc." matches "Coursera").
4. Match on a SINGLE company name - you don't need both companies to match.

RESPONSE FORMAT:
- If you find ANY match, respond EXACTLY:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|Coursera, Inc.|acquirer

- If NO match, respond with:
  None"""

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert M&A deal matcher. Respond only with Match: DEAL_ID|COMPANY|target|acquirer or None."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=150,
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return f"LLM Error: {e}"


def parse_ftc_date(date_str):
    """Parse date from FTC format (e.g., 'February 9, 2026' or datetime attr)."""
    try:
        return datetime.strptime(date_str, "%B %d, %Y")
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    return None


def extract_records_from_html(html_content, base_url="https://www.ftc.gov"):
    """Extract early termination records from FTC HTML."""
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select(".views-row")

    records = []
    for row in rows:
        try:
            item = {}

            # Title / case ID from h3.node-title
            title_elem = row.select_one("h3.node-title a")
            if title_elem:
                item["title"] = title_elem.get_text(strip=True)
                href = title_elem.get("href", "")
                if href and not href.startswith("http"):
                    item["detail_url"] = base_url.rstrip("/") + href
                else:
                    item["detail_url"] = href

            # Extract case ID from title (e.g., "20260676: TRF Sagebrush...")
            if item.get("title"):
                match = re.match(r"^(\d+):\s*(.+)", item["title"])
                if match:
                    print(f"🔍 Case ID: {match.group(1)}")
                    item["case_id"] = match.group(1)
                    item["parties_text"] = match.group(2).strip()

            # Date
            date_elem = row.select_one(".field--name-field-date time")
            if date_elem:
                datetime_attr = date_elem.get("datetime")
                if datetime_attr:
                    try:
                        dt = datetime.fromisoformat(
                            datetime_attr.replace("Z", "+00:00"))
                        item["date"] = dt.strftime("%B %d, %Y")
                        # Use naive date for comparison with CUTOFF_DATE
                        item["date_parsed"] = dt.replace(
                            tzinfo=None) if dt.tzinfo else dt
                    except (ValueError, TypeError):
                        item["date"] = date_elem.get_text(strip=True)
                        item["date_parsed"] = parse_ftc_date(item["date"])
                else:
                    item["date"] = date_elem.get_text(strip=True)
                    item["date_parsed"] = parse_ftc_date(item["date"])

            # Acquiring Party
            acquiring_elem = row.select_one(
                ".field--name-field-acquiring-party .field__item")
            if acquiring_elem:
                item["acquiring_party"] = acquiring_elem.get_text(strip=True)

            # Acquired Party
            acquired_elem = row.select_one(
                ".field--name-field-acquired-party .field__item")
            if acquired_elem:
                item["acquired_party"] = acquired_elem.get_text(strip=True)

            # Acquired Entities (can have multiple)
            entities_elems = row.select(
                ".field--name-field-other-entities .field__item")
            if entities_elems:
                item["acquired_entities"] = [e.get_text(
                    strip=True) for e in entities_elems]

            # Only include rows that look like early termination notices:
            # must have numeric case_id (e.g. "20260676: ...") and date/parties fields
            if not item.get("title"):
                continue
            if not item.get("case_id"):
                # Skip non-notice rows (e.g. "New HSR thresholds and filing fees for 2026")
                continue
            records.append(item)

        except Exception as e:
            print(f"⚠️ Error extracting record: {e}")
            continue

    return records


def fetch_ftc_page(url):
    """Fetch FTC page HTML."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"⚠️ Error fetching {url}: {e}")
        return None


def save_ftc_data_to_deal(deal_match, ftc_data):
    """Save matched FTC early termination data to deal under 'ftc_early_termination' node."""
    try:
        print("💾 Saving FTC early termination data to deal...")

        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available")
            return False

        # Remove matched_deal from data to avoid circular ref
        ftc_save = {k: v for k, v in ftc_data.items() if k != "matched_deal"}

        query = {}
        if deal_match.get("deal_id"):
            try:
                query["_id"] = ObjectId(deal_match["deal_id"])
            except Exception as e:
                print(f"⚠️ Invalid deal_id: {e}")

        if not query:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name")
            target = deal_match.get("target") or deal_match.get("target_name")
            or_conds = []
            if acquirer:
                or_conds.extend(
                    [{"acquirer": acquirer}, {"acquire_name": acquirer}])
            if target:
                or_conds.extend([{"target": target}, {"target_name": target}])
            if or_conds:
                query = {"$or": or_conds}

        if not query:
            print("⚠️ Cannot identify deal")
            return False

        result = collection.update_one(
            query,
            {"$set": {"ftc_early_termination": ftc_save}},
        )

        if result.modified_count > 0:
            print("✅ Saved FTC early termination data to deal")
            return True
        elif result.matched_count > 0:
            print("ℹ️ Deal found but no changes made")
            return True
        else:
            print("⚠️ Deal not found in MongoDB")
            return False

    except Exception as e:
        print(f"❌ Error saving to MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return False


def generate_ftc_match_email_html(ftc_data, deal_match):
    """Generate HTML email for matched FTC early termination."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    case_id = ftc_data.get("case_id", "N/A")
    date_str = ftc_data.get("date", "N/A")
    acquiring = ftc_data.get("acquiring_party", "N/A")
    acquired = ftc_data.get("acquired_party", "N/A")
    entities = ftc_data.get("acquired_entities", [])
    entities_str = ", ".join(entities) if entities else "N/A"
    detail_url = ftc_data.get("detail_url", "")

    subject = f"FTC Early Termination – {target} / {acquirer}"

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #2563eb;">
      FTC Early Termination Notice – Matched Deal
    </h2>

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:8px;font-weight:bold;width:170px;color:#555;">Deal ID:</td><td style="padding:8px;">{escape_html(str(deal_id))}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">Target:</td><td style="padding:8px;">{escape_html(target)}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Acquirer:</td><td style="padding:8px;">{escape_html(acquirer)}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">FTC Case ID:</td><td style="padding:8px;">{escape_html(case_id)}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Date:</td><td style="padding:8px;">{escape_html(date_str)}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">Acquiring Party:</td><td style="padding:8px;">{escape_html(acquiring)}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Acquired Party:</td><td style="padding:8px;">{escape_html(acquired)}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">Acquired Entities:</td><td style="padding:8px;">{escape_html(entities_str)}</td></tr>"""

    if detail_url:
        html += f"""
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Detail URL:</td><td style="padding:8px;"><a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;">View FTC Notice</a></td></tr>"""

    html += """
    </table>
    <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
      <p>Automated email from FTC Early Termination scraper.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html


def generate_ftc_unmatched_email_html(ftc_data):
    """Generate HTML email for unmatched USA-related FTC early termination."""
    case_id = ftc_data.get("case_id", "N/A")
    date_str = ftc_data.get("date", "N/A")
    acquiring = ftc_data.get("acquiring_party", "N/A")
    acquired = ftc_data.get("acquired_party", "N/A")
    entities = ftc_data.get("acquired_entities", [])
    entities_str = ", ".join(entities) if entities else "N/A"
    detail_url = ftc_data.get("detail_url", "")
    title = ftc_data.get("title", "N/A")

    subject = f"🇺🇸 USA-Related FTC Early Termination – {case_id}"

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #f59e0b;">
      FTC Early Termination Notice (USA-Related)
    </h2>
    <div style="text-align:center;margin-bottom:20px;">
      <div style="background:#f59e0b;color:white;padding:8px 16px;border-radius:4px;display:inline-block;font-weight:bold;">
        🇺🇸 USA-RELATED – Unmatched Deal
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:8px;font-weight:bold;width:170px;color:#555;">Case ID:</td><td style="padding:8px;">{escape_html(case_id)}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">Date:</td><td style="padding:8px;">{escape_html(date_str)}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Title:</td><td style="padding:8px;">{escape_html(title)}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">Acquiring Party:</td><td style="padding:8px;">{escape_html(acquiring)}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Acquired Party:</td><td style="padding:8px;">{escape_html(acquired)}</td></tr>
      <tr style="background:#f9f9f9;"><td style="padding:8px;font-weight:bold;color:#555;">Acquired Entities:</td><td style="padding:8px;">{escape_html(entities_str)}</td></tr>"""

    if detail_url:
        html += f"""
      <tr><td style="padding:8px;font-weight:bold;color:#555;">Detail URL:</td><td style="padding:8px;"><a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;">View FTC Notice</a></td></tr>"""

    html += """
    </table>
    <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
      <p>Automated email – FTC Early Termination (USA-related, unmatched).</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html


def send_ftc_match_email_via_webhook(ftc_data, deal_match):
    """Send email via n8n webhook for matched FTC early termination."""
    try:
        subject, html_email = generate_ftc_match_email_html(
            ftc_data, deal_match)
        print(f"📝 Generated email subject: {subject}")

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": deal_id,
            "target": target,
            "acquirer": acquirer,
            "case_id": ftc_data.get("case_id", "N/A"),
            "date": ftc_data.get("date", "N/A"),
            "url": ftc_data.get("detail_url", ""),
        }

        response = requests.post(webhook_url, json=payload, headers={
                                 "Content-Type": "application/json"}, timeout=30)
        response.raise_for_status()
        print(f"✅ Email sent successfully! Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    global deals, matched_data, matched_count

    print("=" * 60)
    print("FTC Early Termination Notices Scraper")
    print("=" * 60)

    # Initialize MongoDB
    ok, msg = init_mongodb_connection(ENV_PATH)
    if not ok:
        print(f"⚠️ {msg}")
    else:
        print(f"✅ {msg}")

    load_deals(include_ftc=True)

    all_records = []
    for url in FTC_URLS:
        print(f"\n📄 Fetching: {url}")
        html = fetch_ftc_page(url)
        if html:
            records = extract_records_from_html(html)
            all_records.extend(records)
            print(f"   Found {len(records)} records")

    # Deduplicate by case_id
    seen_ids = set()
    unique_records = []
    for r in all_records:
        cid = r.get("case_id") or r.get("title", "")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            unique_records.append(r)

    # Filter by current date (only process records dated today or newer)
    today_records = []
    for r in unique_records:
        date_parsed = r.get("date_parsed")
        if date_parsed is None:
            today_records.append(r)
        else:
            try:
                d = date_parsed.date() if hasattr(date_parsed, "date") else date_parsed
                cutoff = CUTOFF_DATE.date() if hasattr(CUTOFF_DATE, "date") else CUTOFF_DATE
                if d == cutoff:
                    today_records.append(r)
            except (AttributeError, TypeError):
                today_records.append(r)

    print(
        f"\n📅 Records with date == {CUTOFF_DATE.strftime('%Y-%m-%d')}: {len(today_records)}")

    if not today_records:
        print("✅ No records for current date. Done.")
        return

    for idx, record in enumerate(today_records):
        try:
            title = record.get("parties_text") or record.get("title", "")
            case_id = record.get("case_id", "N/A")
            date_str = record.get("date", "N/A")

            print(f"\n🔍 [{idx + 1}] {case_id}: {title}")
            print(f"   📅 {date_str}")

            deal_match = None
            matched_company = None
            matched_role = None

            result = match_with_llm(title)
            print(f"   🧠 LLM Result: {result}")

            if result and result.lower().startswith("match"):
                try:
                    match_pattern = r"Match:\s*([^|]+)\|([^|]+)\|(target|acquirer)"
                    m = re.search(match_pattern, result, re.IGNORECASE)
                    if m:
                        deal_id = m.group(1).strip()
                        matched_company = m.group(2).strip()
                        matched_role = m.group(3).strip().lower()

                        for d in deals:
                            if d.get("deal_id") == deal_id:
                                deal_match = d
                                acquirer = deal_match.get(
                                    "acquirer") or deal_match.get("acquire_name", "N/A")
                                target = deal_match.get("target") or deal_match.get(
                                    "target_name", "N/A")
                                print(
                                    f"   🎯 Match: {acquirer} / {target} (on {matched_role})")
                                break

                        if not deal_match:
                            print(
                                f"   ⚠️ Deal ID {deal_id} not found in deals list")
                except Exception as e:
                    print(f"   ⚠️ Error parsing LLM result: {e}")

            if deal_match:
                print("   ✅ Matched! Saving to deal and sending email...")
                ftc_data = {
                    "case_id": case_id,
                    "date": date_str,
                    "title": record.get("title", ""),
                    "acquiring_party": record.get("acquiring_party", ""),
                    "acquired_party": record.get("acquired_party", ""),
                    "acquired_entities": record.get("acquired_entities", []),
                    "detail_url": record.get("detail_url", ""),
                    "matched_company": matched_company or "",
                    "matched_role": matched_role or "",
                }
                if save_ftc_data_to_deal(deal_match, ftc_data):
                    send_ftc_match_email_via_webhook(ftc_data, deal_match)
                    matched_count += 1
                    matched_data.append(
                        {"record": record, "deal_match": deal_match, "ftc_data": ftc_data})
            else:
                print("   ⏭️ No match ")

        except Exception as e:
            print(f"❌ Error processing record: {e}")
            import traceback
            traceback.print_exc()
            continue

    # Save JSON backup
    print(f"\n💾 Saving matched data to: {OUTPUT_PATH}")
    try:
        with open(OUTPUT_PATH, "w") as f:
            json.dump(matched_data, f, indent=2, default=str)
        print(f"✅ Saved {len(matched_data)} matches")
    except Exception as e:
        print(f"⚠️ Error saving JSON: {e}")

    print("\n🎉 Done!")
    if is_connected():
        print("   💾 Matched records saved to MongoDB deals (ftc_early_termination)")
    print(f"   📁 JSON backup → {OUTPUT_PATH}")
    print(f"   🎯 Total matches: {matched_count}")


if __name__ == "__main__":
    main()
