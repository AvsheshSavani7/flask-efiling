import json
import requests
import os
import re
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, date
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected
from html import escape as escape_html
from llm_verification_service import verify_country_relation

# Load OpenAI Key
load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants
URL = "https://www.bundeskartellamt.de/DE/Aufgaben/Fusionen/Hauptpruefverfahren/hauptpruefverfahren_node.html"
EXTRACTED_RECORDS_JSON = "bundeskartellamt_extracted_records.json"

# Global deals list - will be loaded from MongoDB
deals = []


def get_deals_from_mongodb(include_german_scrap=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_german_scrap: If False, only return deals that don't have a 'german_scrap' node

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

        # Optionally also exclude deals with existing german_scrap
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

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'german_scrap' node" if not include_german_scrap else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_german_scrap=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_german_scrap: If False, only load deals that don't have a 'german_scrap' node
    """
    global deals
    deals = get_deals_from_mongodb(include_german_scrap=include_german_scrap)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def normalize_company(name):
    """Normalize company name for matching"""
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").replace(" limited", "").replace(" corporation", "").replace(" corp.", "").replace(" gmbh", "").replace(" ag", "").replace(" se", "").strip()


def translate_to_english(text):
    """Translate German text to English using Google Translate API"""
    if not text or not text.strip():
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "de",
            "tl": "en",
            "dt": "t",
            "q": text
        }
        response = requests.get(url, params=params)
        if response.status_code == 200:
            result = response.json()[0][0][0]
            return result
    except Exception as e:
        print(f"⚠️ Translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


def extract_table_data(html_content):
    """Extract table data from HTML"""
    soup = BeautifulSoup(html_content, "html.parser")
    records = []

    # Find the table
    table = soup.find("table")
    if not table:
        print("⚠️ No table found in HTML")
        return records

    # Find all rows (skip header)
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
        try:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            # Extract data from each column - handle multi-line text
            date_cell = cells[0].get_text(separator=" ", strip=True)
            file_number_cell = cells[1].get_text(separator=" ", strip=True)
            pursue_cell = cells[2].get_text(separator=" ", strip=True)
            product_area_cell = cells[3].get_text(separator=" ", strip=True)
            diploma_cell = cells[4].get_text(separator=" ", strip=True)
            documents_cell = cells[5]

            # Clean up text - remove extra whitespace
            date_cell = re.sub(r'\s+', ' ', date_cell).strip()
            file_number_cell = re.sub(r'\s+', ' ', file_number_cell).strip()
            pursue_cell = re.sub(r'\s+', ' ', pursue_cell).strip()
            product_area_cell = re.sub(r'\s+', ' ', product_area_cell).strip()
            diploma_cell = re.sub(r'\s+', ' ', diploma_cell).strip()

            # Extract document links
            document_links = []
            links = documents_cell.find_all("a")
            for link in links:
                href = link.get("href", "")
                text = link.get_text(strip=True)
                title = link.get("title", "")
                # Convert relative URLs to absolute
                if href and not href.startswith("http"):
                    base_domain = "https://www.bundeskartellamt.de"
                    href = requests.compat.urljoin(base_domain, href)
                if text or href:  # Only add if there's content
                    document_links.append({
                        "text": text,
                        "url": href,
                        "title": title
                    })

            # Translate German text to English
            pursue_en = translate_to_english(
                pursue_cell) if pursue_cell else ""
            product_area_en = translate_to_english(
                product_area_cell) if product_area_cell else ""
            diploma_en = translate_to_english(
                diploma_cell) if diploma_cell else ""

            record = {
                "date": date_cell,
                "file_number": file_number_cell,
                "pursue": pursue_cell,
                "pursue_en": pursue_en,
                "product_area": product_area_cell,
                "product_area_en": product_area_en,
                "diploma": diploma_cell,
                "diploma_en": diploma_en,
                "documents": document_links
            }

            records.append(record)
            print(
                f"📋 Extracted: {file_number_cell} - {pursue_en[:60] if pursue_en else pursue_cell[:60]}...")

        except Exception as e:
            print(f"⚠️ Error extracting row: {e}")
            continue

    return records


def match_deal_with_llm(pursue_text_en, all_companies):
    """Match pursue text with deals using LLM"""
    if not pursue_text_en or pursue_text_en == "[Translation failed]":
        return None

    prompt = f"""
You are an M&A deal analyst. Given the translated text about a German merger case, determine whether it explicitly relates to any of the companies listed below.

- Match only if the company name or a well-known alias appears in the translated text.
- Ignore similar-sounding names or partial matches.
- Accept suffix variations (Inc., Ltd., PLC, GmbH, AG, SE).

Companies:
{', '.join(sorted(all_companies))}

Translated text:
{pursue_text_en}

If there's a match, return in this format:
Match: COMPANY_NAME (acquirer|target)

If not, return:
None
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=100,
        )
        result = response.choices[0].message.content.strip()
        return result
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return "None"


def convert_datetime_to_string(obj):
    """
    Recursively convert datetime objects to strings for JSON serialization.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, date):
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


def generate_german_scrap_email_html(german_scrap_data, deal_match, updated_fields=None):
    """
    Generate HTML email for Bundeskartellamt German scrap match.

    Args:
        german_scrap_data: The German scrap data dictionary
        deal_match: The matched deal object
        updated_fields: List of fields that were updated (None for new records)

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    # Extract German scrap data
    file_number = german_scrap_data.get("file_number", "N/A")
    date = german_scrap_data.get("date", "N/A")
    pursue = german_scrap_data.get("pursue", "N/A")
    pursue_en = german_scrap_data.get("pursue_en", "N/A")
    product_area = german_scrap_data.get("product_area", "N/A")
    product_area_en = german_scrap_data.get("product_area_en", "N/A")
    diploma = german_scrap_data.get("diploma", "N/A")
    diploma_en = german_scrap_data.get("diploma_en", "N/A")
    documents = german_scrap_data.get("documents", [])

    # Determine email type
    if updated_fields:
        title_text = f"Bundeskartellamt Update – {target} / {acquirer}"
        update_note = f"<p style='color:#e74c3c; font-weight:bold; padding:10px; background-color:#ffe6e6; border-radius:4px;'>⚠️ This record was updated. Changed fields: {', '.join(updated_fields)}</p>"
    else:
        title_text = f"Bundeskartellamt New Match – {target} / {acquirer}"
        update_note = "<p style='color:#27ae60; font-weight:bold; padding:10px; background-color:#e6ffe6; border-radius:4px;'>✅ New record added</p>"

    subject = title_text

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

    {update_note}

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
        <td style="padding:8px; font-weight:bold; color:#555;">File Number:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(file_number))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Pursue (German):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(pursue))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Pursue (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(pursue_en))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Product Area (German):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(product_area))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Product Area (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(product_area_en))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Diploma (German):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(diploma))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Diploma (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(diploma_en))}</td>
      </tr>"""

    if documents:
        html_email += """
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Documents:</td>
        <td style="padding:8px; color:#333;">"""
        for doc in documents:
            doc_url = doc.get("url", "")
            doc_text = doc.get("text", "Link")
            if doc_url:
                html_email += f'<a href="{escape_html(doc_url)}" style="color:#e74c3c; text-decoration:none; margin-right:10px;" target="_blank">{escape_html(doc_text)}</a>'
        html_email += """
        </td>
      </tr>"""

    html_email += f"""
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from Bundeskartellamt German merger case matches.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_german_scrap_email_via_webhook(german_scrap_data, deal_match, updated_fields=None):
    """
    Send email notification via n8n webhook after saving German scrap data.

    Args:
        german_scrap_data: The German scrap data dictionary
        deal_match: The matched deal object
        updated_fields: List of fields that were updated (None for new records)

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_german_scrap_email_html(
            german_scrap_data, deal_match, updated_fields)
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
            'file_number': german_scrap_data.get("file_number", "N/A"),
            'date': german_scrap_data.get("date", "N/A"),
            'pursue': german_scrap_data.get("pursue", "N/A"),
            'pursue_en': german_scrap_data.get("pursue_en", "N/A"),
            'product_area': german_scrap_data.get("product_area", "N/A"),
            'product_area_en': german_scrap_data.get("product_area_en", "N/A"),
            'diploma': german_scrap_data.get("diploma", "N/A"),
            'diploma_en': german_scrap_data.get("diploma_en", "N/A"),
            'updated_fields': updated_fields if updated_fields else []
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


def german_file_number_exists(file_number: str) -> bool:
    """
    Check whether this Bundeskartellamt file_number already exists in MongoDB.
    Used to gate "new record" vs "update" for unmatched USA verification emails.
    """
    try:
        if not file_number:
            return False
        collection = get_deals_collection()
        if collection is None:
            return False
        existing = collection.find_one(
            {"german_scrap.file_number": file_number})
        return existing is not None
    except Exception:
        return False


def generate_unmatched_german_scrap_email_html(record: dict) -> tuple:
    """
    Generate HTML email for unmatched Bundeskartellamt record that is USA-related.
    """
    file_number = record.get("file_number", "N/A")
    date_val = record.get("date", "")
    pursue_en = record.get("pursue_en", "")
    pursue = record.get("pursue", "")
    product_area_en = record.get("product_area_en", "")
    product_area = record.get("product_area", "")

    subject = f"Bundeskartellamt (USA-Related) – {file_number}"

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
      Bundeskartellamt (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">
        🇺🇸 USA-RELATED
      </div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">File number:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(file_number))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date_val))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Pursue (EN):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(pursue_en))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Pursue (DE):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(pursue))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Product areas (EN):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(product_area_en))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Product areas (DE):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(product_area))}</td>
      </tr>
    </table>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_unmatched_german_scrap_email_via_webhook(record: dict) -> bool:
    """
    Send email notification via n8n webhook for unmatched Bundeskartellamt record that is USA-related.
    """
    try:
        subject, html_email = generate_unmatched_german_scrap_email_html(
            record)
        print(f"📝 Generated email subject: {subject}")

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
        )
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": "N/A",
            "target": "N/A",
            "acquirer": "N/A",
            "file_number": record.get("file_number", "N/A"),
            "date": record.get("date", ""),
            "pursue_en": record.get("pursue_en", ""),
            "product_area_en": record.get("product_area_en", ""),
            "usa_related": True,
            "is_unmatched": True,
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


def save_german_scrap_data_to_deal(deal_match, german_scrap_data, updated_fields=None):
    """
    Save matched result to MongoDB deal record under 'german_scrap' node.

    Args:
        deal_match: The matched deal object (must have deal_id to identify)
        german_scrap_data: The German scrap data object to save
        updated_fields: List of fields that were updated (None for new records)

    Returns:
        bool: True if saved successfully, False otherwise
    """
    try:
        print(f"💾 Saving German scrap data to deal...")

        # Use global MongoDB connection
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save to MongoDB")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available, skipping save to MongoDB")
            return False

        # Convert datetime objects in german_scrap_data to strings for MongoDB
        german_scrap_data_serializable = convert_datetime_to_string(
            german_scrap_data)

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

        print(f"🔍 Searching for deal with query: {query}")

        # Update the deal document with german_scrap data
        update_result = collection.update_one(
            query,
            {
                "$set": {
                    "german_scrap": german_scrap_data_serializable
                }
            }
        )

        print(
            f"📊 Update result: matched={update_result.matched_count}, modified={update_result.modified_count}")

        if update_result.matched_count > 0:
            if update_result.modified_count > 0:
                print(f"✅ Saved German scrap data to deal record in MongoDB")
            else:
                print(f"ℹ️ Deal found but no changes made (data may be identical)")

            # Only send email if:
            # 1. It's a new record (updated_fields is None) - no existing record found
            # 2. OR there are actual updates (updated_fields is not None and not empty)
            should_send_email = False
            if updated_fields is None:
                # New record - no existing record was found
                should_send_email = True
                print(f"📧 Sending email for new record")
            elif updated_fields and len(updated_fields) > 0:
                # Existing record with actual updates
                should_send_email = True
                print(
                    f"📧 Sending email for updated record (fields changed: {', '.join(updated_fields)})")
            else:
                # Existing record with no changes
                print(f"⏭️ Skipping email - record exists with no changes")

            # Send email notification via n8n webhook only if needed
            if should_send_email:
                try:
                    send_german_scrap_email_via_webhook(
                        german_scrap_data_serializable, deal_match, updated_fields)
                except Exception as e:
                    print(f"⚠️ Error sending email notification: {e}")
                    # Don't fail the save operation if email fails

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


def match_records_with_deals(records):
    """Match extracted records with deals using LLM and save to MongoDB"""
    print(f"\n{'='*60}")
    print(f"🔍 Matching {len(records)} records with deals...")
    print(f"{'='*60}\n")

    # Reload deals if list is empty
    global deals
    if not deals:
        print("⚠️ Deals list is empty, reloading from MongoDB...")
        load_deals(include_german_scrap=False)

    # Get all company names from deals
    all_companies = set()
    for deal in deals:
        if deal.get("acquirer") or deal.get("acquire_name"):
            all_companies.add(normalize_company(
                deal.get("acquirer") or deal.get("acquire_name", "")))
        if deal.get("target") or deal.get("target_name"):
            all_companies.add(normalize_company(
                deal.get("target") or deal.get("target_name", "")))

    matched_count = 0
    updated_count = 0

    for idx, record in enumerate(records, 1):
        file_number = record.get("file_number", "")
        pursue_en = record.get("pursue_en", "")

        print(f"[{idx}/{len(records)}] {file_number} - {pursue_en[:70]}...")

        # Skip if translation failed
        if pursue_en == "[Translation failed]":
            print("  ⏩ Skipped (translation failed)")
            continue

        # Match with LLM
        match_result = match_deal_with_llm(pursue_en, all_companies)

        if match_result and match_result.lower() != "none" and "match:" in match_result.lower():
            # Extract company name and role from match result
            try:
                # Format: "Match: COMPANY_NAME (acquirer|target)"
                match_pattern = r"Match:\s*([^(]+)\s*\((\w+)\)"
                match_obj = re.search(
                    match_pattern, match_result, re.IGNORECASE)

                if match_obj:
                    matched_company_raw = match_obj.group(1).strip()
                    matched_role = match_obj.group(2).strip().lower()
                    matched_company_normalized = normalize_company(
                        matched_company_raw)

                    print(
                        f"  🎯 Match found: {matched_company_raw} ({matched_role})")

                    # Find the deal
                    deal_found = None
                    for deal in deals:
                        acquirer = deal.get("acquirer") or deal.get(
                            "acquire_name", "")
                        target = deal.get("target") or deal.get(
                            "target_name", "")

                        if normalize_company(acquirer) == matched_company_normalized:
                            deal_found = deal
                            break
                        elif normalize_company(target) == matched_company_normalized:
                            deal_found = deal
                            break

                    if deal_found:
                        # Check if record with this file_number already exists in MongoDB
                        collection = get_deals_collection()
                        existing_german_scrap = None
                        updated_fields = None

                        if collection is not None and deal_found.get("deal_id"):
                            try:
                                deal_doc = collection.find_one(
                                    {"_id": ObjectId(deal_found["deal_id"])})
                                if deal_doc and "german_scrap" in deal_doc:
                                    existing_german_scrap = deal_doc["german_scrap"]
                                    if existing_german_scrap.get("file_number") == file_number:
                                        # Record exists, check for updates
                                        updated_fields = []
                                        for key, new_value in record.items():
                                            if key == "file_number":
                                                continue
                                            old_value = existing_german_scrap.get(
                                                key)

                                            # Normalize empty strings and None
                                            if new_value == "":
                                                new_value = None
                                            if old_value == "":
                                                old_value = None

                                            # Compare values (handle lists/dicts specially)
                                            if isinstance(new_value, list) and isinstance(old_value, list):
                                                try:
                                                    new_str = json.dumps(
                                                        new_value, sort_keys=True, ensure_ascii=False)
                                                    old_str = json.dumps(
                                                        old_value, sort_keys=True, ensure_ascii=False)
                                                    if new_str != old_str:
                                                        updated_fields.append(
                                                            key)
                                                except:
                                                    if new_value != old_value:
                                                        updated_fields.append(
                                                            key)
                                            elif new_value != old_value:
                                                updated_fields.append(key)
                            except Exception as e:
                                print(
                                    f"  ⚠️ Error checking existing record: {e}")

                        # Create German scrap record
                        german_scrap_data = {
                            "date": record.get("date", ""),
                            "file_number": file_number,
                            "pursue": record.get("pursue", ""),
                            "pursue_en": pursue_en,
                            "product_area": record.get("product_area", ""),
                            "product_area_en": record.get("product_area_en", ""),
                            "diploma": record.get("diploma", ""),
                            "diploma_en": record.get("diploma_en", ""),
                            "documents": record.get("documents", []),
                            "matched_company": matched_company_raw,
                            "matched_role": matched_role
                        }

                        # Save to MongoDB
                        save_result = save_german_scrap_data_to_deal(
                            deal_found, german_scrap_data, updated_fields)

                        if save_result:
                            if updated_fields:
                                print(
                                    f"  ✅ Updated existing record in deal: {deal_found.get('acquirer', 'N/A')} / {deal_found.get('target', 'N/A')}")
                                print(
                                    f"     Changed fields: {', '.join(updated_fields)}")
                                updated_count += 1
                            else:
                                print(
                                    f"  ✅ Added new record to deal: {deal_found.get('acquirer', 'N/A')} / {deal_found.get('target', 'N/A')}")
                                matched_count += 1
                        else:
                            print(f"  ⚠️ Failed to save to MongoDB")
                else:
                    print(f"  ⚠️ Could not parse match result: {match_result}")
            except Exception as e:
                print(f"  ⚠️ Error processing match: {e}")
                import traceback
                traceback.print_exc()
        else:
            print(f"  ➖ No match")
            # If no deal match, verify USA-related AND new-record only, then email
            try:
                company_details = {
                    # Provide today's date so LLM can judge "new vs update"
                    "today_date": datetime.now().strftime("%Y-%m-%d"),
                    "record": record,
                }

                is_usa_related_and_new = verify_country_relation(
                    company_details=company_details,
                    country="USA",
                    case_type="GERMANY",
                )

                if is_usa_related_and_new:
                    print("   🇺🇸 USA-related NEW record detected - sending email")
                    send_unmatched_german_scrap_email_via_webhook(record)
                else:
                    print("   ℹ️ Not USA-related and new - no action taken")
            except Exception as e:
                print(f"   ⚠️ Error verifying USA relation: {e}")
                import traceback
                traceback.print_exc()

    print(f"\n{'='*60}")
    print(
        f"✅ Matching complete: {matched_count} new records matched, {updated_count} records updated")
    print(f"{'='*60}\n")

    return matched_count + updated_count


def main():
    """
    Main function to scrape Bundeskartellamt website, match with deals, and save to MongoDB.

    Returns:
        dict: {
            "success": bool,
            "extraction_date": str,
            "total_extracted": int,
            "total_matched": int,
            "error": str (if failed)
        }
    """
    global deals

    print(f"\n{'='*60}")
    print(f"🚀 BUNDESKARTELLAMT SCRAPER")
    print(f"{'='*60}\n")

    # Load deals from MongoDB when main() is called (connection should be ready by then)
    # Only load deals without 'german_scrap' node to avoid re-processing
    print("📊 Loading deals from MongoDB (excluding deals with 'german_scrap' node)...")
    load_deals(include_german_scrap=True)

    # Step 1: Fetch HTML
    print(f"📍 Step 1: Fetching HTML from {URL}")
    try:
        response = requests.get(URL, timeout=30)
        response.raise_for_status()
        html_content = response.text
        print(f"   ✅ HTML fetched successfully ({len(html_content)} bytes)\n")
    except Exception as e:
        print(f"   ❌ Error fetching HTML: {e}")
        return {
            "success": False,
            "error": f"Error fetching HTML: {str(e)}"
        }

    # Step 2: Extract table data
    print(f"📍 Step 2: Extracting table data...")
    records = extract_table_data(html_content)
    print(f"   ✅ Extracted {len(records)} records\n")

    # Step 3: Save extracted records to JSON (for backup/debugging)
    print(f"📍 Step 3: Saving extracted records to {EXTRACTED_RECORDS_JSON}")
    try:
        with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"   ✅ Saved!\n")
    except Exception as e:
        print(f"   ⚠️ Warning: Could not save to JSON file: {e}\n")

    # Step 4: Match records with deals and save to MongoDB
    print(f"📍 Step 4: Matching records with deals...")
    total_matched = match_records_with_deals(records)

    # Final summary
    result = {
        "success": True,
        "extraction_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_extracted": len(records),
        "total_matched": total_matched
    }

    print(f"\n{'='*60}")
    print(f"✅ ALL DONE!")
    print(f"{'='*60}")
    print(f"📊 Total records extracted: {len(records)}")
    print(f"🎯 Total matches/updates: {total_matched}")
    print(f"📁 Extracted records → {EXTRACTED_RECORDS_JSON}")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    main()
