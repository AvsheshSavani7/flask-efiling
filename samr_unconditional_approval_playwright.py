from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import datetime
import time
import requests
import json
import os
from openai import OpenAI
from bs4 import BeautifulSoup
from tqdm import tqdm
import re
from bson import ObjectId
from mongodb_connection import (
    get_deals_collection,
    get_mongo_client,
    init_mongodb_connection,
    is_connected,
)
from html import escape as escape_html
from llm_verification_service import verify_usa_relation

# Configuration
# CUTOFF_DATE: Extract records == this date. Stop when records are < this date.
# Example: If CUTOFF_DATE = 2025-02-15, extract 2025-02-15 and newer, stop at 2025-02-14
CUTOFF_DATE = datetime.datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.datetime.strptime(
#     "2026-01-26", "%Y-%m-%d")
BASE_URL = "https://www.samr.gov.cn/fldes/ajgs/wtjjz/"
OUTPUT_JSON = "deals_with_unconditional.json"
EXTRACTED_RECORDS_JSON = "samr_unconditional_extracted_records.json"
MATCHED_OUTPUT_JSON = "samr_unconditional_matched_deals.json"
DEALS_PATH = "deals.json"
PROMPT_LOG_PATH = "gpt_prompts.log"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "samr_unconditional_html_pages"

# Create HTML output directory if it doesn't exist
os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)

# Initialize extracted records list
all_extracted_records = []

# Load OpenAI API Key
load_dotenv(ENV_PATH)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Global deals list - will be loaded from MongoDB
deals = []

# Store matched results
matched_data = []

# Normalize company names
all_companies = set()


def normalize_company(name):
    """Normalize company name for matching."""
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").strip()


def get_deals_from_mongodb(include_samr_unconditional=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_samr_unconditional: If False, only return deals that don't have a 'samr_unconditional' node

    Returns:
        List of deal dictionaries
    """
    try:
        # Use global MongoDB connection
        collection = get_deals_collection()

        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Build query - exclude deals with 'samr_unconditional' node if include_samr_unconditional is False
        query = {}
        if not include_samr_unconditional:
            query = {"samr_unconditional": {"$exists": False}}

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'samr_unconditional' node" if not include_samr_unconditional else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_samr_unconditional=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_samr_unconditional: If False, only load deals that don't have a 'samr_unconditional' node
    """
    global deals, all_companies
    deals = get_deals_from_mongodb(
        include_samr_unconditional=include_samr_unconditional)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")

    # Update all_companies set
    all_companies = set()
    for d in deals:
        acquirer = d.get("acquirer") or d.get("acquire_name", "")
        target = d.get("target") or d.get("target_name", "")
        if acquirer:
            all_companies.add(normalize_company(acquirer))
        if target:
            all_companies.add(normalize_company(target))

    return deals


def detail_url_exists_in_samr_data(detail_url):
    """
    Check if detail_url already exists in any deal's samr_unconditional data.

    Args:
        detail_url: The detail URL to check

    Returns:
        bool: True if detail_url exists, False otherwise
    """
    try:
        collection = get_deals_collection()
        if collection is None:
            return False

        # Search for deals where samr_unconditional.url or samr_unconditional.approval_link matches
        query = {
            "$or": [
                {"samr_unconditional.url": detail_url},
                {"samr_unconditional.approval_link": detail_url}
            ]
        }
        existing_deal = collection.find_one(query)

        if existing_deal:
            print(f"⏭️ Detail URL already processed: {detail_url[:80]}...")
            return True

        return False
    except Exception as e:
        print(f"⚠️ Error checking detail_url existence: {e}")
        return False

# Translation helper


def translate_to_english(text):
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "zh-CN",
            "tl": "en",
            "dt": "t",
            "q": text,
        }
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json()[0][0][0]
    except Exception as e:
        print(f"⚠️ Translation failed for: {text} → {e}")
    return "[Translation failed]"

# Extract records from HTML


def extract_records_from_html(html_content):
    """
    Extract all records from a listing page HTML.
    Returns a list of dicts with: title, title_en, url, date
    """
    records = []
    soup = BeautifulSoup(html_content, "html.parser")

    # Find all list items in the page content
    items = soup.select("div.page-content ul li")

    for item in items:
        try:
            # Extract date from first div
            divs = item.find_all("div")
            if not divs:
                continue

            date_text = divs[0].get_text(strip=True)
            if not date_text:
                continue

            # Extract link and title
            link = item.find("a")
            if not link:
                continue

            title_cn_raw = link.get_text(strip=True)
            # Clean up title: replace multiple newlines/spaces with single space
            title_cn = re.sub(r'\s+', ' ', title_cn_raw).strip()

            url = link.get("href", "")

            # Convert relative URL to full URL
            if url and not url.startswith("http"):
                # Use the base domain from BASE_URL
                base_domain = "https://www.samr.gov.cn"
                url = requests.compat.urljoin(base_domain, url)

            # Translate title (using cleaned version)
            title_en = translate_to_english(title_cn)

            record = {
                "title_cn": title_cn,
                "title_en": title_en,
                "url": url,
                "date": date_text
            }

            records.append(record)
            print(f"📋 Extracted: {date_text} - {title_en}")

        except Exception as e:
            print(f"⚠️ Error extracting record: {e}")
            continue

    return records


def convert_datetime_to_string(obj):
    """
    Recursively convert datetime objects to strings for JSON serialization.
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    elif isinstance(obj, datetime.date):
        return obj.isoformat()
    elif hasattr(obj, 'isoformat') and callable(getattr(obj, 'isoformat')):
        try:
            return obj.isoformat()
        except:
            return str(obj)
    elif isinstance(obj, dict):
        return {key: convert_datetime_to_string(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetime_to_string(item) for item in obj]
    else:
        return obj


def generate_samr_unconditional_email_html(samr_data, deal_match):
    """
    Generate HTML email for SAMR China unconditional approval match.

    Args:
        samr_data: The SAMR data dictionary
        deal_match: The matched deal object

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    # Extract SAMR data
    title_cn = samr_data.get("title_cn", "N/A")
    title_en = samr_data.get("title_en", "N/A")
    date = samr_data.get("date", "N/A")
    url = samr_data.get("url") or samr_data.get("approval_link", "")
    approval_date = samr_data.get("approval_date", date)

    title_text = f"SAMR China Unconditional Approval – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"SAMR China Unconditional Approval – {title_en[:50]}"
    subject = f"SAMR China Unconditional Approval – {target} / {acquirer}"

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #27ae60;">
      {escape_html(title_text)}
    </h2>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Deal ID:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(deal_id))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Target:</td>
        <td style="padding:8px; color:#333;">{escape_html(target)}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Acquirer:</td>
        <td style="padding:8px; color:#333;">{escape_html(acquirer)}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Approval Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(approval_date))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_cn))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_en))}</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#27ae60; text-decoration:none;" target="_blank">
            View SAMR Detail Page
          </a>
        </td>
      </tr>"""

    html_email += f"""
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China unconditional approval matches.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_samr_unconditional_email_via_webhook(samr_data, deal_match):
    """
    Send email notification via n8n webhook after saving SAMR unconditional data.

    Args:
        samr_data: The SAMR data dictionary
        deal_match: The matched deal object

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_samr_unconditional_email_html(
            samr_data, deal_match)
        print(f"📝 Generated email subject: {subject}")

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        # Extract deal information for payload
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': deal_id,
            'target': target,
            'acquirer': acquirer,
            'title_cn': samr_data.get("title_cn", "N/A"),
            'title_en': samr_data.get("title_en", "N/A"),
            'date': samr_data.get("date", "N/A"),
            'approval_date': samr_data.get("approval_date", samr_data.get("date", "N/A")),
            'url': samr_data.get("url") or samr_data.get("approval_link", "")
        }

        # Send POST request to n8n webhook
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        response.raise_for_status()

        # Log response for debugging
        try:
            response_data = response.json() if response.content else {}
            print(f"📧 Webhook response: {response_data}")
        except:
            print(
                f"📧 Webhook response status: {response.status_code}, content: {response.text[:200]}")

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


def generate_unmatched_samr_unconditional_email_html(record: dict, usa_company: str, translated_table: str) -> tuple:
    """
    Generate HTML email for unmatched SAMR China unconditional approval case that is USA-related.

    Args:
        record: Extracted record dict (title_en/title_cn/url/date)
        usa_company: Company identified as USA-related
        translated_table: Translated table/details extracted from the page

    Returns:
        Tuple of (subject, html_email)
    """
    title_cn = record.get("title_cn", "N/A")
    title_en = record.get("title_en", "N/A")
    date_str = record.get("date", "N/A")
    url = record.get("url", "")

    subject = f"SAMR China Unconditional (USA-Related) – {usa_company}"

    safe_table = escape_html(translated_table or "")

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #f59e0b;">
      SAMR China Unconditional Approval (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">
        🇺🇸 USA-RELATED COMPANY: {escape_html(usa_company)}
      </div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date_str))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_cn))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_en))}</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#27ae60; text-decoration:none;" target="_blank">
            View SAMR Detail Page
          </a>
        </td>
      </tr>"""

    html_email += f"""
    </table>

   
  </div>
</body>
</html>
"""
    return subject, html_email


def send_unmatched_samr_unconditional_email_via_webhook(record: dict, usa_company: str, translated_table: str) -> bool:
    """
    Send email notification via n8n webhook for unmatched SAMR unconditional approval case that is USA-related.
    Sends ONE email per usa_company.
    """
    try:
        subject, html_email = generate_unmatched_samr_unconditional_email_html(
            record, usa_company, translated_table)
        print(f"📝 Generated email subject: {subject}")

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": "N/A",
            "target": "N/A",
            "acquirer": "N/A",
            "title_cn": record.get("title_cn", "N/A"),
            "title_en": record.get("title_en", "N/A"),
            "date": record.get("date", "N/A"),
            "url": record.get("url", ""),
            "usa_related": True,
            "is_unmatched": True,
            "usa_related_company": usa_company,
        }

        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()

        print(f"✅ Email sent successfully! Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_samr_unconditional_data_to_deal(deal_match, matched_result):
    """
    Save matched result to MongoDB deal record under 'samr_unconditional' node.

    Args:
        deal_match: The matched deal object (must have deal_id to identify)
        matched_result: The matched result object to save
    """
    try:
        print(f"💾 Saving SAMR unconditional data to deal...")

        # Use global MongoDB connection
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save to MongoDB")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available, skipping save to MongoDB")
            return False

        # Remove matched_deal from the result to avoid circular reference
        # Keep only the matched data
        samr_data = {k: v for k, v in matched_result.items() if k !=
                     "matched_deal"}

        print(
            f"📝 Preparing SAMR unconditional data with keys: {list(samr_data.keys())}")

        # Find the deal by deal_id (preferred) or by acquirer and target
        query = {}
        if deal_match.get("deal_id"):
            # Try to find by deal_id first (convert back to ObjectId)
            try:
                query["_id"] = ObjectId(deal_match["deal_id"])
            except Exception as e:
                print(
                    f"⚠️ Invalid deal_id format: {e}, falling back to acquirer/target")
                query = {}

        # Fallback to acquirer/target if no deal_id or if deal_id lookup failed
        if not query:
            # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name")
            target = deal_match.get("target") or deal_match.get("target_name")

            # Build query with $or to handle both field name formats
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
            print(
                "⚠️ Cannot identify deal (no deal_id, acquirer, or target), skipping MongoDB save")
            return False

        # Convert datetime objects in samr_data to strings for MongoDB
        samr_data_serializable = convert_datetime_to_string(samr_data)

        print(f"🔍 Searching for deal with query: {query}")

        # Update the deal document with samr_unconditional data
        # Use $set to replace/update the samr_unconditional node with the matched object
        update_result = collection.update_one(
            query,
            {
                "$set": {
                    "samr_unconditional": samr_data_serializable
                }
            }
        )

        print(
            f"📊 Update result: matched={update_result.matched_count}, modified={update_result.modified_count}")

        if update_result.modified_count > 0:
            print(f"✅ Saved SAMR unconditional data to deal record in MongoDB")

            # Send email notification via n8n webhook
            # try:
            #     send_samr_unconditional_email_via_webhook(
            #         samr_data_serializable, deal_match)
            # except Exception as e:
            #     print(f"⚠️ Error sending email notification: {e}")
            # Don't fail the save operation if email fails

            return True
        elif update_result.matched_count > 0:
            print(f"ℹ️ Deal found but no changes made (data may be identical)")
            return True
        else:
            print(f"⚠️ Deal not found in MongoDB: {query}")
            return False

    except Exception as e:
        error_msg = str(e)
        # Check if it's a DNS/network timeout issue
        if "DNS" in error_msg or "timeout" in error_msg.lower() or "resolution" in error_msg.lower():
            print(
                f"⚠️ MongoDB connection timeout/network issue. Data saved to JSON file only.")
        else:
            print(f"❌ Error saving to MongoDB: {error_msg[:300]}")
        # Don't print full traceback for network issues to reduce noise
        if "DNS" not in error_msg and "timeout" not in error_msg.lower():
            import traceback
            traceback.print_exc()
        return False

# Extract approval info from detail page


def extract_approval_info(page, url):
    """
    Extract approval information from a detail page.
    Returns: (matches, translated_table)
    """
    try:
        # Open new page context
        context = page.context
        new_page = context.new_page()
        new_page.goto(url, wait_until="domcontentloaded")
        new_page.wait_for_timeout(2000)

        html = new_page.content()

        soup = BeautifulSoup(html, "html.parser")

        table = soup.find("table")
        rows = table.find_all("tr") if table else []

        raw_rows = []
        approval_dates = []
        for row in rows[1:]:  # skip header
            cols = [col.get_text(strip=True)
                    for col in row.find_all(["td", "th"])]
            if cols:
                raw_rows.append(" | ".join(cols))
                # Extract date if present (last column or date-like string)
                for col in reversed(cols):
                    if re.match(r"\d{4}年\d{1,2}月\d{1,2}日", col):
                        try:
                            parsed_date = datetime.datetime.strptime(
                                col, "%Y年%m月%d日").strftime("%Y-%m-%d")
                            approval_dates.append(parsed_date)
                            break
                        except:
                            continue

        # Translate row by row
        translated_rows = []
        for raw_row in raw_rows:
            translated = translate_to_english(raw_row)
            translated_rows.append(translated)
            time.sleep(0.2)

        translated_table = "\n".join(translated_rows)

        print("📄 Translated Table Preview:\n", translated_table[:800])

        prompt = f"""
You are a professional M&A analyst.

Below is a translated table of unconditional merger approvals issued by SAMR.
Each row includes the case name, the parties involved, and the approval date.

Your task is:
- Match any company involved in these cases to the known set below using partial or fuzzy match.
- Return only matched companies, using their original names from the table, and attach the approval date.

Known companies:
{', '.join(all_companies)}

Translated Table:
{translated_table}

Return a JSON array of matched companies using fuzzy or partial name matching.
Each match must include:
- "company": full party name from the table
- "matched_known_name": matched name from known set
- "approval_date": approval date in YYYY-MM-DD (from the row)
Respond strictly as:
[
  {{ "company": "...", "matched_known_name": "...", "approval_date": "YYYY-MM-DD" }}
]
If none, return []
"""

        with open(PROMPT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"\n{'='*80}\n{datetime.datetime.now()} - Prompt for: {url}\n{prompt}\n")

        try:
            res = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": "You identify unconditional M&A approvals."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                max_tokens=700
            )

            print("GPT response:", res.choices[0].message.content)
            breakpoint()
            content = res.choices[0].message.content.strip()

            if content.startswith("```"):
                content = re.sub(r"^```json|^```|```$", "", content).strip()

            json_start = content.find("[")
            json_str = content[json_start:] if json_start != -1 else "[]"

            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError as e:
                print("❌ GPT parsing error:", e)
                parsed = []

            new_page.close()
            return parsed, translated_table
        except Exception as e:
            print("❌ GPT failed:", e)
            new_page.close()
            return [], translated_table
    except Exception as e:
        print(f"❌ Failed to extract from {url}: {e}")
        try:
            new_page.close()
        except:
            pass
        return [], ""

# Extract records from current page


def extract_page_records(page, page_num=1):
    """
    Step: Extract all records (URL, title, date) from current page.
    Returns: (records_list, should_stop)
    """
    print(f"\n{'='*60}")
    print(f"📄 PAGE {page_num}: Extracting records...")
    print(f"{'='*60}")

    # Wait for items to load
    page.wait_for_selector("div.page-content ul li", timeout=10000)

    html_content = page.content()

    # Extract all records from the HTML
    page_records = extract_records_from_html(html_content)
    print(f"📊 Found {len(page_records)} records on page")

    # Filter records: only keep records == CUTOFF_DATE
    filtered_records = []
    should_stop = False

    for record in page_records:
        try:
            record_date = datetime.datetime.strptime(
                record["date"], "%Y-%m-%d")

            if record_date == CUTOFF_DATE:
                # Keep this record (date is == cutoff)
                filtered_records.append(record)
            else:
                # This record is older than cutoff - don't include it and stop
                print(
                    f"🛑 Found record older than cutoff: {record_date.date()} < {CUTOFF_DATE.date()}")
                print(f"   Stopping extraction")
                should_stop = True
                break  # Stop processing more records from this page

        except Exception as e:
            print(f"⚠️ Error parsing date for record: {e}")
            # If we can't parse the date, include the record to be safe
            filtered_records.append(record)

    print(f"✅ Kept {len(filtered_records)} records (filtered out {len(page_records) - len(filtered_records)} old records)")

    return filtered_records, should_stop


# Match records with deals
def match_records_with_deals(records):
    """
    Match extracted records with deals by extracting approval info from detail pages.
    Saves matched records to MongoDB under 'samr_unconditional' node.
    """
    global matched_data, deals

    print(f"\n{'='*60}")
    print(f"🔍 Matching {len(records)} records with deals...")
    print(f"{'='*60}\n")

    matched_count = 0

    # Reload deals if list is empty (connection might not have been ready earlier)
    # Only load deals without 'samr_unconditional' node to avoid re-processing
    if not deals:
        print("⚠️ Deals list is empty, reloading from MongoDB (excluding deals with 'samr_unconditional' node)...")
        load_deals(include_samr_unconditional=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            for idx, record in enumerate(records, 1):
                title_en = record.get("title_en", "")
                title_cn = record.get("title_cn", "")
                date_str = record.get("date", "")
                url = record.get("url", "")

                print(f"[{idx}/{len(records)}] {date_str} - {title_en[:70]}...")

                if not url:
                    print("  ⏩ Skipped (no URL)")
                    continue

                # Extract approval info from detail page
                matches, translated_table = extract_approval_info(
                    page, url)

                if matches:
                    for m in matches:
                        company = normalize_company(m["matched_known_name"])
                        print(f"  🎯 Match found: {company}")

                        # Find the deal
                        deal_match = None
                        for deal in deals:
                            acquirer = deal.get("acquirer") or deal.get(
                                "acquire_name", "")
                            target = deal.get("target") or deal.get(
                                "target_name", "")

                            if normalize_company(acquirer) == company or normalize_company(target) == company:
                                deal_match = deal
                                print(
                                    f"  ✅ Found deal: {acquirer} / {target}")
                                break

                        if deal_match:
                            # Build the matched result object
                            matched_result = {
                                "deal_id": deal_match.get("deal_id", ""),
                                "title_cn": title_cn,
                                "title_en": title_en,
                                "url": url,
                                "approval_link": url,
                                "date": date_str,
                                "approval_date": m.get("approval_date", date_str),
                                "translated_table": translated_table,
                                "matched_company": m.get("company", ""),
                                "matched_known_name": m.get("matched_known_name", ""),
                                "matched_deal": deal_match
                            }

                            matched_data.append(matched_result)
                            breakpoint()
                            print(f"  ✅ Match added to results!")

                            # Save to MongoDB under 'samr_unconditional' node in the deal record
                            save_result = save_samr_unconditional_data_to_deal(
                                deal_match, matched_result)
                            if save_result:
                                print(
                                    f"  ✅ Saved SAMR unconditional data to deal record in MongoDB")
                            else:
                                print(
                                    f"  ⚠️ Failed to save SAMR unconditional data to MongoDB")

                            matched_count += 1
                        else:
                            print(
                                f"  ⚠️ Match found but deal not found: {company}")
                else:
                    print(f"  ➖ No match")
                    # For unconditional: when no matched deals found, check USA-related companies via LLM
                    try:
                        # Use translated_table as the primary signal, per your request
                        company_details = f"""
Title (EN): {title_en}
Title (CN): {title_cn}
Date: {date_str}
URL: {url}

Translated Table:
{translated_table}
""".strip()

                        usa_companies = verify_usa_relation(
                            company_details=company_details,
                            case_type="CHINA-UNCONDITIONAL",
                        )

                        # Defensive: ensure we have a list of companies
                        if isinstance(usa_companies, bool):
                            usa_companies = []
                        elif not isinstance(usa_companies, list):
                            usa_companies = []

                        if usa_companies:
                            print(
                                f"   🇺🇸 USA-related companies detected: {usa_companies}")
                            # for usa_company in usa_companies:
                            #     send_unmatched_samr_unconditional_email_via_webhook(
                            #         record, usa_company, translated_table
                            #     )
                        else:
                            print("   ℹ️ Not USA-related - no action taken")
                    except Exception as e:
                        print(f"   ⚠️ Error verifying USA relation: {e}")
                        import traceback
                        traceback.print_exc()

        finally:
            browser.close()

    print(f"\n{'='*60}")
    print(f"✅ Matching complete: {matched_count} records matched with deals")
    print(f"{'='*60}\n")

    return matched_count


# Extract from existing HTML files
def extract_from_existing_html_files():
    """
    Extract records from already saved HTML files in samr_unconditional_html_pages directory.
    Returns list of extracted records.
    """
    from glob import glob

    records = []
    html_files = sorted(
        glob(os.path.join(HTML_OUTPUT_DIR, "listing_page_*.html")))

    if not html_files:
        print(f"⚠️ No HTML files found in {HTML_OUTPUT_DIR}/")
        return records

    print(f"\n{'='*60}")
    print(f"📂 Found {len(html_files)} existing HTML files")
    print(f"{'='*60}\n")

    for idx, html_file in enumerate(html_files, 1):
        filename = os.path.basename(html_file)
        print(f"[{idx}/{len(html_files)}] Processing: {filename}")

        try:
            with open(html_file, "r", encoding="utf-8") as f:
                html_content = f.read()

            page_records = extract_records_from_html(html_content)
            records.extend(page_records)
            print(f"  ✅ Extracted {len(page_records)} records\n")

        except Exception as e:
            print(f"  ❌ Error: {e}\n")
            continue

    return records


# Main execution
def main(use_existing_html=False, headless=True):
    """
    EXECUTION FLOW:
    ================
    Phase 1: Extract Records
        1. Call BASE_URL
        2. Extract all records from page 1 (URL, title, date, etc.)
        3. Call page 2 and extract records
        4. Continue until cutoff date is reached
        5. Save all records to JSON

    Phase 2: Match with Deals  
        6. Load all extracted records
        7. For each record, extract approval info from detail page
        8. Match companies using LLM
        9. If match found, save to MongoDB under 'samr_unconditional' node
        10. Save matched results to JSON

    Args:
        use_existing_html: If True, extract from existing HTML files instead of scraping
        headless: bool, whether to run browser in headless mode (default: True)

    Returns:
        dict: {
            "success": bool,
            "extraction_date": str,
            "total_extracted": int,
            "total_matched": int,
            "matched_results": list,
            "error": str (if failed)
        }
    """
    global all_extracted_records, matched_data, deals
    all_extracted_records = []
    matched_data = []

    # Initialize MongoDB connection (required before loading deals)
    ok, msg = init_mongodb_connection(ENV_PATH)
    if not ok:
        print(f"⚠️ {msg}")
        print("   Continuing without MongoDB; no deals will be available for matching.")
    else:
        print(f"✅ {msg}")

    # Load deals from MongoDB when main() is called (connection should be ready by then)
    # Only load deals without 'samr_unconditional' node to avoid re-processing
    print("📊 Loading deals from MongoDB (excluding deals with 'samr_unconditional' node)...")
    load_deals(include_samr_unconditional=False)

    print(f"\n{'='*60}")
    print(f"🚀 PHASE 1: EXTRACT ALL UNCONDITIONAL APPROVAL RECORDS")
    print(f"{'='*60}\n")

    if use_existing_html:
        # Extract from existing HTML files
        print("📂 Mode: Using existing HTML files\n")
        all_extracted_records = extract_from_existing_html_files()
    else:
        # Scrape new pages
        print("🌐 Mode: Scraping SAMR website\n")

        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()

            try:
                # Step 1: Call BASE_URL
                print(f"📍 Step 1: Calling BASE_URL")
                print(f"   URL: {BASE_URL}")
                page.goto(BASE_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                print(f"   ✅ Loaded\n")

                # Start from page 1 and extract records sequentially
                page_num = 1
                while True:
                    # Extract records from current page
                    page_records, should_stop = extract_page_records(
                        page, page_num)
                    all_extracted_records.extend(page_records)

                    # Check if we should stop
                    if should_stop:
                        print(f"\n✅ Stopped: Cutoff date reached")
                        break

                    # Try to navigate to next page
                    try:
                        next_btn = page.get_by_text("下一页")
                        next_class = next_btn.get_attribute("class")

                        if next_class and "disabled" in next_class:
                            print(f"\n✅ Stopped: No more pages")
                            break

                        print(f"\n➡️  Navigating to page {page_num + 1}...")
                        next_btn.click()
                        page.wait_for_timeout(2000)
                        page_num += 1

                    except Exception as e:
                        print(f"\n⚠️ Pagination error: {e}")
                        break

            except Exception as e:
                print(f"\n❌ Scraping error: {e}")
            finally:
                browser.close()

    # Step 5: Save all extracted records to JSON
    print(f"\n{'='*60}")
    print(f"📍 Step 5: SAVE EXTRACTED RECORDS TO JSON")
    print(f"{'='*60}")
    print(f"   Total records extracted: {len(all_extracted_records)}")
    print(f"   Saving to: {EXTRACTED_RECORDS_JSON}")

    with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
        json.dump(all_extracted_records, f, ensure_ascii=False, indent=2)
    print(f"   ✅ Saved!\n")

    # Phase 2: Match records with deals
    print(f"\n{'='*60}")
    print(f"🚀 PHASE 2: MATCH RECORDS WITH DEALS")
    print(f"{'='*60}\n")

    # Filter out records that are already processed (url exists in samr_unconditional data)
    print(f"🔍 Checking which records are already processed...")
    filtered_records = []
    skipped_count = 0
    for record in all_extracted_records:
        detail_url = record.get('url')
        if detail_url and detail_url_exists_in_samr_data(detail_url):
            skipped_count += 1
        else:
            filtered_records.append(record)

    if skipped_count > 0:
        print(f"⏭️ Skipped {skipped_count} already processed records")

    all_extracted_records = filtered_records
    print(
        f"🔍 Processing {len(all_extracted_records)} new records to match with deals...\n")

    matched_count = match_records_with_deals(all_extracted_records)

    # Prepare output - convert all datetime objects to strings
    matched_data_serializable = convert_datetime_to_string(matched_data)

    matched_output = {
        "success": True,
        "extraction_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_extracted": len(all_extracted_records),
        "total_matched": len(matched_data),
        "matched_results": matched_data_serializable
    }

    # Save to file as well
    with open(MATCHED_OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(matched_output, f, ensure_ascii=False, indent=2, default=str)

    # Final summary
    print(f"\n{'='*60}")
    print(f"✅ ALL DONE!")
    print(f"{'='*60}")
    print(f"📊 Total records extracted: {len(all_extracted_records)}")
    print(f"🎯 Total matches found: {len(matched_data)}")
    print(f"📁 All extracted records → {EXTRACTED_RECORDS_JSON}")
    print(f"📁 Matched deals → {MATCHED_OUTPUT_JSON}")
    print(f"{'='*60}\n")

    return matched_output


if __name__ == "__main__":
    import sys

    # Check command line arguments
    use_existing = False
    headless_mode = True

    if len(sys.argv) > 1:
        if sys.argv[1] in ["--use-html", "-h", "--html"]:
            use_existing = True
            print("📂 Mode: Extract from existing HTML files")
        elif sys.argv[1] in ["--headed"]:
            headless_mode = False
            print("🖥️  Mode: Running with visible browser")
        elif sys.argv[1] in ["--help"]:
            print(
                "\nUsage: python samr_unconditional_approval_playwright.py [OPTIONS]")
            print("\nOptions:")
            print("  --use-html, -h    Extract from existing HTML files (no scraping)")
            print("  --headed           Run browser in headed mode (visible)")
            print("  --help             Show this help message")
            print("\nDefault: Scrape new pages from SAMR website in headless mode\n")
            sys.exit(0)

    if not use_existing:
        print("🌐 Mode: Scrape new pages from SAMR website")

    main(use_existing_html=use_existing, headless=headless_mode)
