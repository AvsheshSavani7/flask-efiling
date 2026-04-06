from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import datetime
import time
import requests
import json
import os
from openai import OpenAI
from bs4 import BeautifulSoup
import re
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected
from html import escape as escape_html
from llm_verification_service import verify_usa_relation

# Configuration
# CUTOFF_DATE: Extract records >= this date. Stop when records are < this date.
# Example: If CUTOFF_DATE = 2026-01-15, extract 2026-01-15 and newer, stop at 2026-01-14
CUTOFF_DATE = datetime.datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.datetime.strptime("2026-01-16", "%Y-%m-%d")

BASE_URL = "https://www.samr.gov.cn/fldes/tzgg/ftj/"
EXTRACTED_RECORDS_JSON = "samr_conditional_extracted_records.json"
MATCHED_OUTPUT_JSON = "samr_conditional_matched_deals.json"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "samr_conditional_html_pages"

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


def normalize_company(name):
    """Normalize company name for matching."""
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").strip()


def get_deals_from_mongodb(include_conditional=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_conditional: If False, only return deals that don't have a 'samr_conditional' node

    Returns:
        List of deal dictionaries
    """
    try:
        # Use global MongoDB connection
        collection = get_deals_collection()

        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Base status filter - only include Open/Unknown/null/missing deals
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        # Optionally also exclude deals with existing 'samr_conditional' node
        if not include_conditional:
            samr_filter = {
                "$or": [
                    {"samr_conditional": {"$exists": False}},
                    {"samr_conditional": None},
                    {"samr_conditional": []},
                    {"samr_conditional": {}},
                ]
            }
            query = {"$and": [status_filter, samr_filter]}
        else:
            query = status_filter

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'samr_conditional' node" if not include_conditional else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_conditional=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_conditional: If False, only load deals that don't have a 'samr_conditional' node
    """
    global deals
    deals = get_deals_from_mongodb(include_conditional=include_conditional)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def detail_url_exists_in_conditional_data(detail_url):
    """
    Check if detail_url already exists in any deal's samr_conditional data.

    Args:
        detail_url: The detail URL to check

    Returns:
        bool: True if detail_url exists, False otherwise
    """
    try:
        collection = get_deals_collection()
        if collection is None:
            return False

        # Search for deals where samr_conditional.url matches
        query = {"samr_conditional.url": detail_url}
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
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            return response.json()[0][0][0]
    except Exception as e:
        print(f"⚠️ Translation failed for: {text[:50]}... → {e}")
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
    items = soup.select("div.page-content ul li.content-3-left-text")

    for item in items:
        try:
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

            # Extract date
            date_div = item.find("div", class_="contentRight01time")
            date_str = date_div.get_text(strip=True) if date_div else ""

            # Translate title (using cleaned version)
            title_en = translate_to_english(title_cn)

            record = {
                "title_cn": title_cn,
                "title_en": title_en,
                "url": url,
                "date": date_str
            }

            records.append(record)
            print(f"📋 Extracted: {date_str} - {title_en}")

        except Exception as e:
            print(f"⚠️ Error extracting record: {e}")
            continue

    return records

# Match company with LLM


def match_deal_with_llm(title_en, title_cn):
    """Match an English translated title with deals using LLM"""
    global deals

    # Reload deals if list is empty (connection might not have been ready earlier)
    # Only load deals without 'samr_conditional' node to avoid re-processing
    if not deals:
        print("⚠️ Deals list is empty, reloading from MongoDB (excluding deals with 'samr_conditional' node)...")
        load_deals(include_conditional=False)

    # Build deals list with all relevant information (including aliases)
    deals_list = []
    for deal in deals:
        deal_info = {
            "deal_id": deal.get("deal_id", ""),
        }

        # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
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
        print("⚠️ No deals with company names found")
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
You are an M&A deal analyst. Given the translated title of a Chinese conditional approval notice, determine whether it explicitly relates to any of the companies listed below.

DEALS TO MATCH:
{deals_text}

TITLE (English translation):
{title_en}

TITLE (Original Chinese):
{title_cn}

INSTRUCTIONS:
1. Compare the title text with BOTH Target and Acquirer names in the deals list.
2. When matching, also consider target_aliases and parent_aliases - if the title matches an alias, treat it as a match for that deal.
3. Look for EXACT matches, partial matches, or variations of company names.
4. Consider that the title might be:
   - The full company name
   - A department/division name that matches the company
   - A translated version of the company name
   - An alias (target_aliases or parent_aliases)
5. If the title text appears in ANY form in a deal's Target, Acquirer, or aliases, it's a match.
6. Be thorough - check if the title is contained within or matches any company name.
7. Accept suffix variations (Inc., Ltd., PLC).

MATCHING EXAMPLES:
- "General Motors" matches "General Motors Corporation" (partial match)
- "Vibra Residencial" matches "Vibra Residencial Ltda." (partial match)
- "Compass" matches "Compass Digital Acquisition Corp." (partial match)

RESPONSE FORMAT:
- If you find a match, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|General Motors|acquirer

- If NO match is found after thorough checking, respond with:
  None

IMPORTANT: Check carefully - if the title matches or is contained in any Target, Acquirer, or alias name, return the match.
"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition. Your job is to find matches between conditional approval notice titles and deal companies. If the title matches or is contained in any Target or Acquirer name, return the match. Be thorough and check all possibilities."},
                {"role": "user", "content": prompt},
            ],

        )
        result = response.choices[0].message.content.strip()
        print(f"🧠 LLM Response: {result}")
        return result
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return "None"


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


def generate_conditional_approval_email_html(conditional_data, deal_match):
    """
    Generate HTML email for SAMR China conditional approval notice match.

    Args:
        conditional_data: The conditional approval data dictionary
        deal_match: The matched deal object

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    # Extract conditional approval data
    title_cn = conditional_data.get("title_cn", "N/A")
    title_en = conditional_data.get("title_en", "N/A")
    date = conditional_data.get("date", "N/A")
    url = conditional_data.get("url", "")
    approval_date = conditional_data.get("approval_date", "N/A")

    title_text = f"SAMR China Conditional Approval – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"SAMR China Conditional Approval – {title_en[:50]}"
    subject = f"[FRMD] SAMR China Conditional Approval (New) – {target} / {acquirer}"

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
      </tr>
      """

    if url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">
            View SAMR Detail Page
          </a>
        </td>
      </tr>"""

    html_email += f"""
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China conditional approval notice matches.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_conditional_approval_email_via_webhook(conditional_data, deal_match):
    """
    Send email notification via n8n webhook after saving conditional approval data.

    Args:
        conditional_data: The conditional approval data dictionary
        deal_match: The matched deal object

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_conditional_approval_email_html(
            conditional_data, deal_match)
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
            'title_cn': conditional_data.get("title_cn", "N/A"),
            'title_en': conditional_data.get("title_en", "N/A"),
            'date': conditional_data.get("date", "N/A"),
            'url': conditional_data.get("url", ""),
            'approval_date': conditional_data.get("approval_date", "N/A"),

        }

        # Send POST request to n8n webhook
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
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


def generate_unmatched_conditional_approval_email_html(record: dict) -> tuple:
    """
    Generate HTML email for unmatched China SAMR conditional approval case that is USA-related.

    Args:
        record: The SAMR conditional approval record dictionary

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract record data
    title_cn = record.get("title_cn", "N/A")
    title_en = record.get("title_en", "N/A")
    date_str = record.get("date", "N/A")
    url = record.get("url", "")
    approval_date = record.get("approval_date", "N/A")

    # Build subject
    subject = f"[FRUD] SAMR China Conditional Approval (USA-Related) – {title_en[:60]}"

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
      SAMR China Conditional Approval (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">🇺🇸 USA-RELATED</div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date_str))}</td>
      </tr>"""

    if approval_date and approval_date != "N/A":
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Approval Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(approval_date))}</td>
      </tr>"""

    html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_cn))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_en))}</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">
            View SAMR Detail Page
          </a>
        </td>
      </tr>"""

    html_email += """
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China conditional approval notice monitoring.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_unmatched_conditional_approval_email_via_webhook(record: dict) -> bool:
    """
    Send email notification via n8n webhook for unmatched China SAMR conditional approval case that is USA-related.

    Args:
        record: The SAMR conditional approval record dictionary

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_unmatched_conditional_approval_email_html(
            record)
        print(f"📝 Generated email subject: {subject}")

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        # Extract record information
        title_cn = record.get("title_cn", "N/A")
        title_en = record.get("title_en", "N/A")
        date_str = record.get("date", "N/A")
        url = record.get("url", "")
        approval_date = record.get("approval_date", "N/A")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': 'N/A',  # No deal match
            'target': 'N/A',  # No deal match
            'acquirer': 'N/A',  # No deal match
            'title_cn': title_cn,
            'title_en': title_en,
            'date': date_str,
            'url': url,
            'approval_date': approval_date,
            'is_unmatched': True,
            'usa_related': True,
        }

        # Send POST request to n8n webhook
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
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


def save_conditional_approval_data_to_deal(deal_match, matched_result):
    """
    Save matched result to MongoDB deal record under 'samr_conditional' node.

    Args:
        deal_match: The matched deal object (must have deal_id to identify)
        matched_result: The matched result object to save
    """
    try:
        print(f"💾 Saving conditional approval data to deal...")

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
        conditional_data = {k: v for k, v in matched_result.items() if k !=
                            "matched_deal"}

        print(
            f"📝 Preparing conditional approval data with keys: {list(conditional_data.keys())}")

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

        # Convert datetime objects in conditional_data to strings for MongoDB
        conditional_data_serializable = convert_datetime_to_string(
            conditional_data)

        print(f"🔍 Searching for deal with query: {query}")

        # Update the deal document with samr_conditional data
        # Also mark as conditionally_approved
        update_result = collection.update_one(
            query,
            {
                "$set": {
                    "samr_conditional": conditional_data_serializable,
                    "conditionally_approved": True
                }
            }
        )

        print(
            f"📊 Update result: matched={update_result.matched_count}, modified={update_result.modified_count}")

        if update_result.modified_count > 0:
            print(f"✅ Saved conditional approval data to deal record in MongoDB")

            # Send email notification via n8n webhook
            try:
                send_conditional_approval_email_via_webhook(
                    conditional_data_serializable, deal_match)
            except Exception as e:
                print(f"⚠️ Error sending email notification: {e}")
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

    # Save listing page HTML
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    listing_html_filename = f"listing_page_{page_num}_{timestamp}.html"
    listing_html_filepath = os.path.join(
        HTML_OUTPUT_DIR, listing_html_filename)

    html_content = page.content()
    with open(listing_html_filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"💾 Saved HTML: {listing_html_filename}")

    # Extract all records from the HTML
    page_records = extract_records_from_html(html_content)
    print(f"📊 Found {len(page_records)} records on page")

    # Filter records: only keep records >= CUTOFF_DATE
    filtered_records = []
    should_stop = False

    for record in page_records:
        try:
            record_date = datetime.datetime.strptime(
                record["date"], "%Y-%m-%d")

            if record_date >= CUTOFF_DATE:
                # Keep this record (date is >= cutoff)
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


# Extract from existing HTML files


def extract_from_existing_html_files():
    """
    Extract records from already saved HTML files in samr_conditional_html_pages directory.
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
        7. Match each record with deals using LLM
        8. If match found, save to MongoDB and send email notification
        9. Save matched results to JSON

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

    # Load deals from MongoDB when main() is called (connection should be ready by then)
    # Only load deals without 'samr_conditional' node to avoid re-processing
    print("📊 Loading deals from MongoDB (excluding deals with 'samr_conditional' node)...")
    load_deals(include_conditional=False)

    print(f"\n{'='*60}")
    print(f"🚀 PHASE 1: EXTRACT ALL CONDITIONAL APPROVAL RECORDS")
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

    # Save all extracted records to JSON
    print(f"\n{'='*60}")
    print(f"📍 SAVE EXTRACTED RECORDS TO JSON")
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

    # Filter out records that are already processed (url exists in samr_conditional data)
    print(f"🔍 Checking which records are already processed...")
    filtered_records = []
    skipped_count = 0
    for record in all_extracted_records:
        detail_url = record.get('url')
        if detail_url and detail_url_exists_in_conditional_data(detail_url):
            skipped_count += 1
        else:
            filtered_records.append(record)

    if skipped_count > 0:
        print(f"⏭️ Skipped {skipped_count} already processed records")

    all_extracted_records = filtered_records
    print(
        f"🔍 Processing {len(all_extracted_records)} new records to match with deals...\n")

    for idx, record in enumerate(all_extracted_records, 1):
        title_en = record.get("title_en", "")
        title_cn = record.get("title_cn", "")
        date_str = record.get("date", "")
        url = record.get("url", "")

        print(
            f"\n[{idx}/{len(all_extracted_records)}] {date_str} - {title_en[:70]}...")

        # Skip if translation failed
        if title_en == "[Translation failed]":
            print("  ⏩ Skipped (translation failed)")
            continue

        # Match with LLM
        match_result = match_deal_with_llm(title_en, title_cn)

        if match_result and match_result.lower() != "none" and match_result.lower().startswith("match"):
            try:
                # Remove "Match: " prefix
                match_data = match_result.replace(
                    "Match:", "").replace("match:", "").strip()

                # Split by pipe
                parts = match_data.split("|")
                if len(parts) >= 3:
                    deal_id = parts[0].strip()
                    company_name = parts[1].strip()
                    match_type = parts[2].strip().lower().replace(
                        "(", "").replace(")", "")

                    # Find deal by deal_id (most reliable)
                    deal_match = None
                    for deal in deals:
                        if deal.get("deal_id") == deal_id:
                            deal_match = deal
                            print(f"  ✅ Found deal by ID: {deal_id}")
                            break

                    # Fallback: find by company name if deal_id didn't work
                    if not deal_match:
                        for deal in deals:
                            target = deal.get("target") or deal.get(
                                "target_name", "")
                            acquirer = deal.get("acquirer") or deal.get(
                                "acquire_name", "")

                            if match_type == "target" and target and normalize_company(target) == normalize_company(company_name):
                                deal_match = deal
                                print(
                                    f"  ✅ Found deal by target name: {company_name}")
                                break
                            elif match_type == "acquirer" and acquirer and normalize_company(acquirer) == normalize_company(company_name):
                                deal_match = deal
                                print(
                                    f"  ✅ Found deal by acquirer name: {company_name}")
                                break

                    if deal_match:
                        # Optionally extract additional info from detail page
                        # For now, use basic record data
                        conditional_data = {
                            "title_cn": title_cn,
                            "title_en": title_en,
                            "url": url,
                            "date": date_str
                        }

                        # Build the matched result object
                        matched_result = {
                            "deal_id": deal_match.get("deal_id", ""),
                            "title_cn": title_cn,
                            "title_en": title_en,
                            "url": url,
                            "date": date_str,
                            "matched_deal": deal_match
                        }

                        # Add any additional extracted fields if available
                        if "approval_date" in record:
                            conditional_data["approval_date"] = record["approval_date"]
                            matched_result["approval_date"] = record["approval_date"]

                        matched_data.append(matched_result)
                        print(f"  ✅ Match added to results!")

                        # Save to MongoDB under 'samr_conditional' node in the deal record
                        save_result = save_conditional_approval_data_to_deal(
                            deal_match, matched_result)
                        if save_result:
                            print(
                                f"  ✅ Saved conditional approval data to deal record in MongoDB")
                        else:
                            print(
                                f"  ⚠️ Failed to save conditional approval data to MongoDB")
                    else:
                        print(
                            f"  ⚠️ LLM found match but deal not found: {deal_id} / {company_name}")

            except Exception as e:
                print(f"  ⚠️ Error processing match: {e}")
                import traceback
                traceback.print_exc()
        else:
            print(f"  ➖ No match")
            # Verify if case title is USA-related
            try:
                # Use title_en for verification as it's in English
                company_details = title_en if title_en and title_en != "[Translation failed]" else title_cn
                is_usa_related = verify_usa_relation(
                    company_details=company_details,
                    case_type="CHINA"
                )
                if is_usa_related:
                    print(
                        f"   🇺🇸 USA-related case detected - sending email notification")
                    send_unmatched_conditional_approval_email_via_webhook(
                        record)
                else:
                    print(f"   ℹ️ Not USA-related - no action taken")
            except Exception as e:
                print(f"   ⚠️ Error verifying USA relation: {e}")
                import traceback
                traceback.print_exc()

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
                "\nUsage: python samr_conditional_approval_playwright.py [OPTIONS]")
            print("\nOptions:")
            print("  --use-html, -h    Extract from existing HTML files (no scraping)")
            print("  --headed          Run browser in headed mode (visible)")
            print("  --help            Show this help message")
            print("\nDefault: Scrape new pages from SAMR website in headless mode\n")
            sys.exit(0)

    if not use_existing:
        print("🌐 Mode: Scrape new pages from SAMR website")

    main(use_existing_html=use_existing, headless=headless_mode)
