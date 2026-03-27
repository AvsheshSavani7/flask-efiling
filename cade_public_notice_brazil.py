from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv
import datetime
from datetime import date
import time
import requests
import json
import os
from bs4 import BeautifulSoup
import re
from openai import OpenAI
import base64
from pymongo import MongoClient
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection
from html import escape as escape_html
from llm_verification_service import verify_usa_relation

# Configuration
BASE_URL = "https://sei.cade.gov.br/sei/modulos/pesquisa/md_pesq_processo_pesquisar.php?acao_externa=protocolo_pesquisar&acao_origem_externa=protocolo_pesquisar&id_orgao_acesso_externo=0"
BASE_PESQUISA_URL = "https://sei.cade.gov.br/sei/modulos/pesquisa"
MATCHED_OUTPUT_JSON = "cade_matched_deals.json"
ENV_PATH = ".env"
RECAPTCHA_SITE_KEY = "6Le2a7gqAAAAAAVxMYQ-mn7GyO8lcWAQq4Hxm-2G"

# 2captcha API endpoints
CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"

# Load environment variables
load_dotenv(ENV_PATH)
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Store matched results
matched_data = []


def _load_env_file(env_path: str) -> None:
    """Load environment variables from .env file"""
    if not os.path.exists(env_path):
        return

    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value


def get_deals_from_mongodb(include_brazil=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_brazil: If False, only return deals that don't have a 'brazil' node

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

        # Optionally also exclude deals with existing 'brazil' node
        if not include_brazil:
            brazil_filter = {
                "$or": [
                    {"brazil": {"$exists": False}},
                    {"brazil": None},
                    {"brazil": []},
                    {"brazil": {}},
                ]
            }
            query = {"$and": [status_filter, brazil_filter]}
        else:
            query = status_filter

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'brazil' node" if not include_brazil else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


# Global deals list - will be loaded when needed
deals = []


def detail_url_exists_in_brazil_data(detail_url):
    """
    Check if detail_url already exists in any deal's brazil data.

    Args:
        detail_url: The detail URL to check

    Returns:
        bool: True if detail_url exists, False otherwise
    """
    try:
        collection = get_deals_collection()
        if collection is None:
            return False

        # Search for deals where brazil.detail_url matches
        query = {"brazil.detail_url": detail_url}
        existing_deal = collection.find_one(query)

        if existing_deal:
            print(f"⏭️ Detail URL already processed: {detail_url[:80]}...")
            return True

        return False
    except Exception as e:
        print(f"⚠️ Error checking detail_url existence: {e}")
        return False


def load_deals(include_brazil=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_brazil: If False, only load deals that don't have a 'brazil' node
    """
    global deals
    deals = get_deals_from_mongodb(include_brazil=include_brazil)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def translate_to_english(text):
    """Translate Portuguese text to English."""
    if not text or not isinstance(text, str) or text.strip() == "":
        return text

    # Skip translation for very long text (likely to timeout or be slow)
    if len(text) > 500:
        print(f"⚠️ Skipping translation for long text ({len(text)} chars)")
        return text

    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "pt",
                  "tl": "en", "dt": "t", "q": text}
        response = requests.get(
            url, params=params, timeout=5)  # Reduced timeout
        if response.status_code == 200:
            return response.json()[0][0][0]
    except requests.Timeout:
        print(f"⚠️ Translation timeout for text: {text[:50]}...")
        return text  # Return original if timeout
    except Exception as e:
        print(f"⚠️ Translation failed: {e}")
        return text  # Return original on error
    return "[Translation failed]"


def translate_specific_fields_only(data):
    """
    Translate only specific fields: type, interested_parties, and document_type in table_records.
    This is optimized for MongoDB saving to save time.

    Args:
        data: Dictionary to translate

    Returns:
        Dictionary with only specified fields translated
    """
    if not isinstance(data, dict):
        return data

    translated = data.copy()

    # Translate 'type' field if it exists
    if "type" in translated and isinstance(translated["type"], str) and translated["type"].strip():
        print(f"🔄 Translating 'type' field...")
        translated["type"] = translate_to_english(translated["type"])

    # Translate 'interessados' field and rename key to 'interested_parties'
    if "interessados" in translated and isinstance(translated["interessados"], str) and translated["interessados"].strip():
        print(f"🔄 Translating 'interessados' field...")
        translated["interested_parties"] = translate_to_english(
            translated["interessados"])
        # Remove original key to avoid duplication
        translated.pop("interessados", None)

    # Translate 'document_type' in table_records
    if "table_records" in translated and isinstance(translated["table_records"], list):
        print(
            f"🔄 Translating 'document_type' in {len(translated['table_records'])} table records...")
        translated["table_records"] = []
        for record in data.get("table_records", []):
            if isinstance(record, dict):
                translated_record = record.copy()
                # Translate 'tipo_documento' to 'document_type' and translate its value
                if "tipo_documento" in translated_record:
                    doc_type = translated_record["tipo_documento"]
                    if isinstance(doc_type, str) and doc_type.strip():
                        translated_record["document_type"] = translate_to_english(
                            doc_type)
                        translated_record.pop("tipo_documento", None)
                translated["table_records"].append(translated_record)
            else:
                translated["table_records"].append(record)

    return translated


def translate_dict_keys_and_values(data, key_translations=None, skip_translation_keys=None):
    """
    Translate dictionary keys and values from Portuguese to English.
    Optimized to skip translation for URLs, dates, IDs, and other non-text fields.

    Args:
        data: Dictionary or list to translate
        key_translations: Dict mapping Portuguese keys to English keys
        skip_translation_keys: Set of keys to skip translation for (URLs, dates, IDs)

    Returns:
        Translated dictionary or list
    """
    if key_translations is None:
        key_translations = {
            "deal_id": "deal_id",
            "process": "process",
            "type": "type",
            "registration_date": "registration_date",
            "interessados": "interested_parties",
            "detail_url": "detail_url",
            "table_records": "table_records",
            "matched_deal": "matched_deal",
            "documento_processo": "document_process",
            "document_url": "document_url",
            "tipo_documento": "document_type",
            "data_documento": "document_date",
            "data_registro": "registration_date",
            "unidade": "unit"
        }

    # Keys that don't need translation (URLs, dates, IDs, numbers)
    if skip_translation_keys is None:
        skip_translation_keys = {
            "deal_id", "detail_url", "document_url", "registration_date",
            "data_documento", "data_registro", "documento_processo", "process"
        }

    if isinstance(data, dict):
        translated = {}
        for key, value in data.items():
            # Translate key
            translated_key = key_translations.get(key, key)

            # Translate value based on type
            if isinstance(value, str) and value.strip():
                # Skip translation for URLs, dates, IDs, and process numbers
                if translated_key in skip_translation_keys or key in skip_translation_keys:
                    translated[translated_key] = value
                # Skip translation for URLs
                elif value.startswith("http://") or value.startswith("https://"):
                    translated[translated_key] = value
                # Skip translation for date-like strings (DD/MM/YYYY or YYYY-MM-DD)
                elif re.match(r'^\d{2}/\d{2}/\d{4}$', value) or re.match(r'^\d{4}-\d{2}-\d{2}', value):
                    translated[translated_key] = value
                # Skip translation for very short strings or numbers
                elif len(value) < 10 or value.replace(".", "").replace("/", "").isdigit():
                    translated[translated_key] = value
                else:
                    # Only translate meaningful text fields
                    translated[translated_key] = translate_to_english(value)
            elif isinstance(value, (dict, list)):
                translated[translated_key] = translate_dict_keys_and_values(
                    value, key_translations, skip_translation_keys)
            else:
                translated[translated_key] = value

        return translated
    elif isinstance(data, list):
        return [translate_dict_keys_and_values(item, key_translations, skip_translation_keys) for item in data]
    else:
        return data


def convert_datetime_to_string(obj):
    """
    Recursively convert datetime objects to strings for JSON serialization.
    Handles datetime.datetime objects and MongoDB date objects.

    Args:
        obj: Object that may contain datetime objects

    Returns:
        Object with all datetime objects converted to ISO format strings
    """
    # Handle datetime objects
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    # Handle date objects
    elif isinstance(obj, date):
        return obj.isoformat()
    # Handle MongoDB date objects (which are datetime.datetime instances)
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


def generate_brazil_email_html(brazil_data, deal_match):
    """
    Generate HTML email for CADE Brazil regulatory notice match.

    Args:
        brazil_data: The Brazil data dictionary (translated)
        deal_match: The matched deal object

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    # Extract Brazil data
    process = brazil_data.get("process", "N/A")
    type_text = brazil_data.get("type", "N/A")
    registration_date = brazil_data.get("registration_date", "N/A")
    interested_parties = brazil_data.get(
        "interested_parties", brazil_data.get("interessados", "N/A"))
    detail_url = brazil_data.get("detail_url", "")
    table_records = brazil_data.get("table_records", [])

    # Count table records
    table_records_count = len(table_records) if table_records else 0

    # Generate table records HTML
    table_records_html = ""
    if table_records and len(table_records) > 0:
        table_records_html = """
    <table style="width:100%; border-collapse:collapse; margin-top:10px;">
      <thead>
        <tr style="background-color:#f5f5f5;">
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Document Process</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Document Type</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Document Date</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Registration Date</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Unit</th>
        </tr>
      </thead>
      <tbody>
"""
        for idx, record in enumerate(table_records):
            bg = "#ffffff" if idx % 2 == 0 else "#f9f9f9"
            doc_process = escape_html(
                str(record.get("document_process", record.get("documento_processo", ""))))
            doc_type = escape_html(
                str(record.get("document_type", record.get("tipo_documento", ""))))
            doc_date = escape_html(
                str(record.get("document_date", record.get("data_documento", ""))))
            reg_date = escape_html(
                str(record.get("registration_date", record.get("data_registro", ""))))
            unit = escape_html(
                str(record.get("unit", record.get("unidade", ""))))

            # Build document URL if available
            doc_url = record.get("document_url", "")
            if doc_url:
                doc_process_html = f'<a href="{escape_html(doc_url)}" style="color:#4a90e2; text-decoration:none;" target="_blank">{doc_process}</a>'
            else:
                doc_process_html = doc_process

            table_records_html += f"""
      <tr style="background-color:{bg};">
        <td style="padding:8px; border:1px solid #ddd;">{doc_process_html}</td>
        <td style="padding:8px; border:1px solid #ddd;">{doc_type}</td>
        <td style="padding:8px; border:1px solid #ddd;">{doc_date}</td>
        <td style="padding:8px; border:1px solid #ddd;">{reg_date}</td>
        <td style="padding:8px; border:1px solid #ddd;">{unit}</td>
      </tr>
"""
        table_records_html += """
      </tbody>
    </table>
"""
    else:
        table_records_html = "<p><em>No table records found.</em></p>"

    title_text = f"CADE Brazil – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"CADE Brazil Match – Process {process}"
    subject = f"[FRMD] CADE Brazil Regulatory (New) – {target} / {acquirer}"

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #4a90e2;">
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
        <td style="padding:8px; font-weight:bold; color:#555;">Process:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(process))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Type:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(type_text))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Registration Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(registration_date))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Interested Parties:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(interested_parties))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Table Records Count:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(table_records_count))}</td>
      </tr>
"""

    if detail_url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(detail_url)}" style="color:#4a90e2; text-decoration:none;" target="_blank">
            View CADE Detail Page
          </a>
        </td>
      </tr>
"""

    html_email += f"""
    </table>

    <h3 style="color:#333; margin-top:20px; margin-bottom:10px;">Table Records (Documentos)</h3>
    {table_records_html}

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from CADE Brazil regulatory notice matches.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_brazil_email_via_webhook(brazil_data, deal_match):
    """
    Send email notification via n8n webhook after saving Brazil data.

    Args:
        brazil_data: The Brazil data dictionary (translated)
        deal_match: The matched deal object

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_brazil_email_html(
            brazil_data, deal_match)
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
            'process': brazil_data.get("process", "N/A"),
            'type': brazil_data.get("type", "N/A"),
            'registration_date': brazil_data.get("registration_date", "N/A"),
            'detail_url': brazil_data.get("detail_url", "")
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


def generate_unmatched_brazil_email_html(brazil_data: dict) -> tuple:
    """
    Generate HTML email for unmatched CADE Brazil notice that is USA-related.

    Args:
        brazil_data: dict containing case/notice data

    Returns:
        Tuple of (subject, html_email)
    """
    process = brazil_data.get("process", "N/A")
    notice_type = brazil_data.get("type", "N/A")
    registration_date = brazil_data.get("registration_date", "N/A")
    interessados = brazil_data.get("interessados", "N/A")
    translated = brazil_data.get("interessados_en", "")
    detail_url = brazil_data.get("detail_url", "")

    subject = f"FRUD: CADE Brazil (USA-Related) – {process}"

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
      CADE Brazil (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">
        🇺🇸 USA-RELATED
      </div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Process:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(process))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Type:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(notice_type))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Registration Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(registration_date))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Interested Parties (PT):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(interessados))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Interested Parties (EN):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(translated))}</td>
      </tr>"""

    if detail_url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(detail_url)}" style="color:#0066cc; text-decoration:none;" target="_blank">
            View CADE Detail Page
          </a>
        </td>
      </tr>"""

    html_email += """
    </table>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_unmatched_brazil_email_via_webhook(brazil_data: dict) -> bool:
    """
    Send email notification via n8n webhook for unmatched CADE Brazil notice that is USA-related.
    """
    try:
        subject, html_email = generate_unmatched_brazil_email_html(brazil_data)
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
            "process": brazil_data.get("process", "N/A"),
            "type": brazil_data.get("type", "N/A"),
            "registration_date": brazil_data.get("registration_date", "N/A"),
            "detail_url": brazil_data.get("detail_url", ""),
            "interessados": brazil_data.get("interessados", "N/A"),
            "interessados_en": brazil_data.get("interessados_en", ""),
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


def save_brazil_data_to_deal(deal_match, matched_result):
    """
    Save matched result to MongoDB deal record under 'brazil' node.

    Args:
        deal_match: The matched deal object (must have acquirer/target to identify)
        matched_result: The matched result object to save
    """
    try:
        print(f"Saving Brazil data to deal...")

        # Use global MongoDB connection
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save to MongoDB")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available, skipping save to MongoDB")
            return False

        # Translate only specific fields: type, interested_parties, document_type in table_records
        print(f"🔄 Translating specific fields only (type, interested_parties, document_type)...")
        translated_result = translate_specific_fields_only(matched_result)
        print(f"✅ Translation completed")

        # Remove matched_deal from the result to avoid circular reference
        # Keep only the matched data (process, type, registration_date, etc.)
        brazil_data = {k: v for k, v in translated_result.items()
                       if k != "matched_deal"}

        print(f"📝 Preparing Brazil data with keys: {list(brazil_data.keys())}")

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

        # Convert datetime objects in brazil_data to strings for MongoDB
        brazil_data_serializable = convert_datetime_to_string(brazil_data)

        print(f"🔍 Searching for deal with query: {query}")

        # Update the deal document with brazil data
        # Use $set to replace/update the brazil node with the matched object
        update_result = collection.update_one(
            query,
            {
                "$set": {
                    "brazil": brazil_data_serializable
                }
            }
        )

        print(
            f"📊 Update result: matched={update_result.matched_count}, modified={update_result.modified_count}")

        # No need to close - using global connection

        if update_result.modified_count > 0:
            print(f"✅ Saved Brazil data to deal record in MongoDB")

            # Send email notification via n8n webhook
            try:
                send_brazil_email_via_webhook(
                    brazil_data_serializable, deal_match)
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
        if "DNS" in error_msg or "timeout" in error_msg.lower() or "resolution" in error_msg.lower() or "No route to host" in error_msg:
            print(
                f"⚠️ MongoDB connection timeout/network issue. Data saved to JSON file only.")
        else:
            print(f"❌ Error saving to MongoDB: {error_msg[:300]}")
        # Don't print full traceback for network issues to reduce noise
        if "DNS" not in error_msg and "timeout" not in error_msg.lower():
            import traceback
            traceback.print_exc()
        return False


def match_with_llm(interessados_text, translated):
    """Match interessados text with deals using LLM."""
    global deals

    # Reload deals if list is empty (connection might not have been ready earlier)
    # Only load deals without 'brazil' node to avoid re-processing
    if not deals:
        print("⚠️ Deals list is empty, reloading from MongoDB (excluding deals with 'brazil' node)...")
        load_deals(include_brazil=False)

    # Build deals list with all relevant information (including aliases)
    deals_list = []
    print("match_with llm starting...")
    print(f"📊 Total deals available: {len(deals)}")

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

    print(f"deals_text: {deals_text}")

    prompt = f"""
You're an expert in M&A deals. Analyze the following "interessados" (interested parties) text from a Brazilian regulatory notice and determine if it matches any of the deals below.

DEALS TO MATCH:
{deals_text}

INTERESSADOS TEXT (translated to English):
{translated}

ORIGINAL TEXT (Portuguese):
{interessados_text}

INSTRUCTIONS:
1. Compare the interessados text with BOTH Target and Acquirer names in the deals list.
2. When matching, also consider target_aliases and parent_aliases - if the interessados text matches an alias, treat it as a match for that deal.
3. Look for EXACT matches, partial matches, or variations of company names.
4. Consider that the interessados text might be:
   - The full company name
   - A department/division name that matches the company
   - A translated version of the company name
   - An alias (target_aliases or parent_aliases)
5. If the interessados text appears in ANY form in a deal's Target, Acquirer, or aliases, it's a match.
6. Be thorough - check if the interessados text is contained within or matches any company name.

MATCHING EXAMPLES:
- "General Coordination of Information Technology" matches "General Coordination of Information Technology" (exact match)
- "Vibra Residencial" matches "Vibra Residencial Ltda." (partial match)
- "Compass" matches "Compass Digital Acquisition Corp." (partial match)

RESPONSE FORMAT:
- If you find a match, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|General Coordination of Information Technology|acquirer

- If NO match is found after thorough checking, respond with:
  None

IMPORTANT: Check carefully - if the interessados text matches or is contained in any Target, Acquirer, or alias name, return the match.
"""
    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You're an M&A deal identifier for Brazilian regulatory notices. Your job is to find matches between interessados text and deal companies. If the interessados text matches or is contained in any Target or Acquirer name, return the match. Be thorough and check all possibilities."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,  # Slightly higher temperature for more flexible matching
            max_tokens=200
        )

        result = res.choices[0].message.content.strip()
        print(f"🧠 LLM Response: {result}")
        return result
    except Exception as e:
        print(f"❌ LLM Error: {e}")
        return f"LLM Error: {e}"


def match_with_deals(interessados_text):
    """Simple string matching with deals."""
    interessados_lower = interessados_text.lower()
    for deal in deals:
        # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
        target = deal.get("target") or deal.get("target_name", "")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")

        # Check if target or acquirer name appears in interessados text
        if target and target.lower() in interessados_lower:
            return deal
        if acquirer and acquirer.lower() in interessados_lower:
            return deal
    return None


def solve_image_captcha(page, api_key=None):
    """Solve image-based CAPTCHA using 2captcha."""
    if not api_key:
        print("⚠️ No CAPTCHA API key found for image CAPTCHA")
        return None

    try:
        # Find the CAPTCHA image
        captcha_img = None

        # Method 1: Look for images with "captcha" in attributes
        img_src = None
        try:
            captcha_locator = page.locator(
                "img[src*='captcha' i], img[alt*='captcha' i], img[id*='captcha' i]")
            if captcha_locator.count() > 0:
                img_src = captcha_locator.first.get_attribute("src")
        except:
            pass

        # Method 2: Look for small images (CAPTCHAs are usually small)
        if not img_src:
            try:
                all_imgs = page.locator("img").all()
                for img in all_imgs:
                    try:
                        box = img.bounding_box()
                        if box and 50 < box["width"] < 300 and 30 < box["height"] < 150:
                            src = img.get_attribute("src") or ""
                            if "data:image" in src or "captcha" in src.lower():
                                img_src = src
                                break
                    except:
                        continue
            except:
                pass

        if not img_src:
            print("⚠️ Could not find CAPTCHA image")
            return None
        if not img_src:
            print("⚠️ CAPTCHA image has no source")
            return None

        # Handle data URLs or download regular URLs
        if img_src.startswith("data:image"):
            img_base64 = img_src.split(",")[1] if "," in img_src else None
            if not img_base64:
                print("⚠️ Could not extract base64 from data URL")
                return None
            print("📥 Using CAPTCHA image from data URL...")
        else:
            print("📥 Downloading CAPTCHA image...")
            try:
                # Convert relative URL to absolute URL if needed
                if not img_src.startswith("http://") and not img_src.startswith("https://"):
                    # Get the page's current URL to build absolute URL
                    page_url = page.url
                    # Use urljoin to properly combine base URL with relative path
                    from urllib.parse import urljoin
                    img_src = urljoin(page_url, img_src)
                    print(f"📝 Converted to absolute URL: {img_src[:100]}...")

                img_response = requests.get(img_src, timeout=10)
                if img_response.status_code != 200:
                    print(
                        f"❌ Failed to download CAPTCHA image: {img_response.status_code}")
                    return None
                img_base64 = base64.b64encode(
                    img_response.content).decode('utf-8')
            except Exception as e:
                print(f"❌ Error downloading CAPTCHA image: {e}")
                return None

        # Submit to 2captcha
        print("🔐 Solving image CAPTCHA with 2captcha...")
        data = {
            "key": api_key,
            "method": "base64",
            "body": img_base64,
            "json": 1,
        }

        resp = requests.post(CAPTCHA_SOLVER_URL, data=data)
        result = resp.json()

        if result.get("status") != 1:
            print(
                f"❌ 2Captcha image CAPTCHA submission failed: {result.get('request', resp.text)}")
            return None

        task_id = result.get("request")
        print(f"📝 2Captcha task id: {task_id} - waiting for solution...")

        # Poll for solution
        solution = None
        for attempt in range(30):
            time.sleep(5)
            params = {
                "key": api_key,
                "action": "get",
                "id": task_id,
                "json": 1,
            }
            r = requests.get(CAPTCHA_RESULT_URL, params=params)
            result = r.json()

            if result.get("status") == 1:
                solution = result.get("request")
                print(f"✅ Image CAPTCHA solved: {solution}")
                break
            elif result.get("request") != "CAPCHA_NOT_READY":
                print(f"❌ 2Captcha error: {result}")
                break
            if attempt % 3 == 0:
                print(
                    f"⏳ Waiting for image CAPTCHA solution... (attempt {attempt + 1}/30)")

        return solution

    except Exception as e:
        print(f"❌ Error solving image CAPTCHA: {e}")
        import traceback
        traceback.print_exc()
        return None


def handle_image_captcha_if_present(page):
    """Check for and solve image-based CAPTCHA on the current page."""
    try:
        # Find CAPTCHA image
        captcha_img = None
        try:
            captcha_img = page.locator(
                "img[src*='captcha' i], img[alt*='captcha' i]").first
            if captcha_img.count() == 0:
                captcha_img = None
        except:
            pass

        if not captcha_img:
            # Try finding small images
            try:
                all_imgs = page.locator("img").all()
                for img in all_imgs:
                    try:
                        box = img.bounding_box()
                        if box and 50 < box["width"] < 300:
                            src = img.get_attribute("src") or ""
                            if "captcha" in src.lower() or "data:image" in src:
                                captcha_img = img
                                break
                    except:
                        continue
            except:
                pass

        if not captcha_img:
            return False

        print("🖼️ Image CAPTCHA detected, solving...")

        # Find input field and submit button
        captcha_input = None
        submit_button = None

        try:
            # Look for text input near CAPTCHA
            inputs = page.locator("input[type='text']").all()
            for inp in inputs:
                try:
                    box = inp.bounding_box()
                    if box and box["width"] < 200:
                        captcha_input = inp
                        break
                except:
                    continue
        except:
            pass

        # Find submit button
        try:
            submit_button = page.locator(
                "button:has-text('Enviar'), input[type='submit'][value*='Enviar' i]").first
            if submit_button.count() == 0:
                submit_button = None
        except:
            pass

        if not captcha_input:
            print("⚠️ Could not find CAPTCHA input field")
            return False

        # Solve CAPTCHA
        solution = solve_image_captcha(page, CAPTCHA_API_KEY)

        if solution:
            captcha_input.fill(solution)
            time.sleep(1)

            if submit_button:
                submit_button.click()
            else:
                captcha_input.press("Enter")

            time.sleep(3)
            print("✅ Image CAPTCHA solved and submitted")
            return True
        else:
            print("⚠️ Failed to solve image CAPTCHA")
            return False

    except Exception as e:
        print(f"⚠️ Error handling image CAPTCHA: {e}")
        import traceback
        traceback.print_exc()
        return False


def extract_table_data_from_detail_page(page, context, url):
    """Extract table data (tblDocumentos) from detail page."""
    try:
        # Open new page
        detail_page = context.new_page()
        detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # Check for image CAPTCHA
        captcha_solved = handle_image_captcha_if_present(detail_page)
        if captcha_solved:
            time.sleep(4)

        # Wait for page to load
        time.sleep(2)

        # Get page content
        html = detail_page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Find the table with id "tblDocumentos"
        table = soup.find("table", id="tblDocumentos")
        table_data = []

        if table:
            # Get caption to see total records
            caption = table.find("caption")
            caption_text = caption.get_text(strip=True) if caption else ""

            # Extract all data rows (skip header row)
            rows = table.find_all("tr")
            for row in rows:
                # Skip header row (has th elements)
                if row.find("th"):
                    continue

                # Extract data from each cell
                cells = row.find_all("td")
                if len(cells) >= 5:  # Should have at least 5 columns
                    row_data = {}

                    # Find the document link by class "ancoraPadraoAzul" (most reliable)
                    doc_link = row.find("a", class_="ancoraPadraoAzul")
                    if doc_link:
                        # Get document number from link text
                        doc_number = doc_link.get_text(strip=True)
                        if doc_number and re.match(r'^\d+$', doc_number):
                            row_data["documento_processo"] = doc_number

                        # Extract URL from onclick attribute
                        onclick = doc_link.get("onclick", "")
                        if onclick and "window.open" in onclick:
                            match = re.search(
                                r"window\.open\('([^']+)'\)", onclick)
                            if match:
                                doc_url = match.group(1)
                                if not doc_url.startswith("http"):
                                    doc_url = requests.compat.urljoin(
                                        BASE_PESQUISA_URL, doc_url)
                                row_data["document_url"] = doc_url

                        # Get document type from alt/title attribute
                        doc_type = doc_link.get(
                            "alt") or doc_link.get("title") or ""
                        if doc_type and doc_type.strip():
                            row_data["tipo_documento"] = doc_type.strip()

                    # Extract dates from all cells (dates have format DD/MM/YYYY)
                    dates_found = []
                    for cell in cells:
                        cell_text = cell.get_text(strip=True)
                        if re.match(r'^\d{2}/\d{2}/\d{4}$', cell_text):
                            dates_found.append(cell_text)

                    # Assign dates (first is data_documento, second is data_registro)
                    if len(dates_found) >= 1:
                        row_data["data_documento"] = dates_found[0]
                    if len(dates_found) >= 2:
                        row_data["data_registro"] = dates_found[1]

                    # Find unidade by class "ancoraSigla" (unit codes like PROT, SEI, CGAA5)
                    unidade_link = row.find("a", class_="ancoraSigla")
                    if unidade_link:
                        unidade_text = unidade_link.get_text(strip=True)
                        if unidade_text:
                            row_data["unidade"] = unidade_text

                    # Clean up: remove empty string values
                    row_data = {k: v for k, v in row_data.items()
                                if v and str(v).strip()}

                    # Only add row if we have at least documento_processo
                    if row_data and row_data.get("documento_processo"):
                        table_data.append(row_data)

            print(
                f"📊 Extracted {len(table_data)} table records from tblDocumentos")
        else:
            print("⚠️ Table tblDocumentos not found on detail page")

        detail_page.close()
        return table_data

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Failed to extract table data from {url}: {error_msg}")
        try:
            detail_page.close()
        except:
            pass
        return []


def extract_autuacao_info_from_detail_page(page, context, url):
    """Fetch detail page and extract Autuação (filing) information."""
    try:
        # Open new page
        detail_page = context.new_page()
        detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # Check for image CAPTCHA
        captcha_solved = handle_image_captcha_if_present(detail_page)
        if captcha_solved:
            time.sleep(4)

        # Wait for page to load
        time.sleep(2)

        # Get page content
        html = detail_page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Initialize result dictionary
        autuacao_info = {
            "process": "",
            "type": "",
            "registration_date": "",
            "interessados": ""
        }

        # Find the Autuação table
        # Look for table or tbody containing "Autuação" header
        autuacao_found = False
        all_tables = soup.find_all("table")

        for table in all_tables:
            # Check if this table has Autuação header
            header = table.find("th", string=re.compile(r"Autuação", re.I))
            if header:
                autuacao_found = True
                # Found the Autuação table, now extract fields
                rows = table.find_all("tr")

                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if len(cells) == 2:
                        label = cells[0].get_text(strip=True)
                        value = cells[1].get_text(separator=" ", strip=True)

                        if "Processo" in label:
                            autuacao_info["process"] = value
                        elif "Tipo" in label:
                            autuacao_info["type"] = value
                        elif "Data de Registro" in label:
                            autuacao_info["registration_date"] = value
                        elif "Interessados" in label:
                            autuacao_info["interessados"] = value

                break

        # If Autuação table not found, try alternative method
        if not autuacao_found:
            # Look for tbody containing "Autuação"
            all_tbody = soup.find_all("tbody")
            for tbody in all_tbody:
                header = tbody.find("th", string=re.compile(r"Autuação", re.I))
                if header:
                    rows = tbody.find_all("tr")

                    for row in rows:
                        cells = row.find_all(["td", "th"])
                        if len(cells) == 2:
                            label = cells[0].get_text(strip=True)
                            value = cells[1].get_text(
                                separator=" ", strip=True)

                            if "Processo" in label:
                                autuacao_info["process"] = value
                            elif "Tipo" in label:
                                autuacao_info["type"] = value
                            elif "Data de Registro" in label:
                                autuacao_info["registration_date"] = value
                            elif "Interessados" in label:
                                autuacao_info["interessados"] = value

                    break

        print(f"📄 Extracted Autuação info:")
        print(f"   Process: {autuacao_info['process']}")
        print(f"   Type: {autuacao_info['type'][:80]}...")
        print(f"   Registration Date: {autuacao_info['registration_date']}")
        print(f"   Interessados: {autuacao_info['interessados'][:100]}...")

        detail_page.close()
        return autuacao_info

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Failed to extract Autuação info from {url}: {error_msg}")
        try:
            detail_page.close()
        except:
            pass
        return {
            "process": "",
            "type": "",
            "registration_date": "",
            "interessados": ""
        }


def solve_recaptcha_v2(site_key, page_url, api_key=None):
    """Solve reCAPTCHA v2 using 2captcha service."""
    if not api_key:
        print("⚠️ No CAPTCHA API key found.")
        return None

    print("🔐 Solving reCAPTCHA with 2captcha...")

    data = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }

    try:
        resp = requests.post(CAPTCHA_SOLVER_URL, data=data)
        result = resp.json()

        if result.get("status") != 1:
            print(
                f"❌ 2Captcha task creation failed: {result.get('request', resp.text)}")
            return None

        task_id = result.get("request")
        if not task_id:
            print("❌ Failed to get task ID from 2captcha")
            return None

        print(f"📝 2Captcha task id: {task_id} - waiting for solution...")

        token = None
        for attempt in range(30):
            time.sleep(5)
            params = {
                "key": api_key,
                "action": "get",
                "id": task_id,
                "json": 1,
            }
            r = requests.get(CAPTCHA_RESULT_URL, params=params)
            result = r.json()

            if result.get("status") == 1:
                token = result.get("request")
                print(
                    f"✅ reCAPTCHA solved! Token: {token[:20]}... (truncated)")
                break
            elif result.get("request") != "CAPCHA_NOT_READY":
                print(f"❌ 2Captcha error: {result}")
                break
            if attempt % 3 == 0:
                print(
                    f"⏳ Waiting for captcha solution... (attempt {attempt + 1}/30)")

        if not token:
            print("⏱️ Captcha was not solved in time.")
            return None

        return token

    except Exception as e:
        print(f"❌ Error solving captcha: {e}")
        import traceback
        traceback.print_exc()
        return None


def fill_recaptcha_token(page, token):
    """Inject the solved reCAPTCHA token into the page."""
    if not token:
        return False

    try:
        time.sleep(1)

        # Inject token via JavaScript - Playwright uses function parameters, not arguments
        page.evaluate("""
            (token) => {
                var textarea = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                               document.querySelector('#g-recaptcha-response') ||
                               document.querySelector('.g-recaptcha-response');
                
                if (textarea) {
                    textarea.value = token;
                    textarea.innerHTML = token;
                    
                    var origDisplay = textarea.style.display;
                    textarea.style.display = 'block';
                    textarea.value = token;
                    textarea.style.display = origDisplay;
                    
                    var evt1 = new Event('input', {bubbles: true});
                    var evt2 = new Event('change', {bubbles: true});
                    textarea.dispatchEvent(evt1);
                    textarea.dispatchEvent(evt2);
                }
                
                var verificaField = document.getElementById('verificaRecaptcha');
                if (verificaField) {
                    verificaField.value = 'true';
                }
                
                if (typeof recaptchaCallback === 'function') {
                    try {
                        recaptchaCallback();
                    } catch(e) {
                        console.warn('Callback error:', e);
                    }
                }
            }
        """, token)

        time.sleep(1)

        # Verify token was set
        for attempt in range(3):
            verification = page.evaluate("""
                () => {
                    var textarea = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                                   document.querySelector('#g-recaptcha-response');
                    var verificaField = document.getElementById('verificaRecaptcha');
                    return {
                        textareaExists: !!textarea,
                        tokenSet: textarea ? textarea.value.length > 0 : false,
                        tokenLength: textarea ? textarea.value.length : 0,
                        verificaValue: verificaField ? verificaField.value : 'none'
                    };
                }
            """)

            if verification['tokenSet']:
                print(f"✅ Token verified: length={verification['tokenLength']}, "
                      f"verificaRecaptcha={verification['verificaValue']}")
                if verification['verificaValue'] != 'true':
                    page.evaluate("""
                        () => {
                            var field = document.getElementById('verificaRecaptcha');
                            if (field) field.value = 'true';
                        }
                    """)
                return True

            if attempt < 2:
                print(
                    f"⏳ Retry {attempt + 1}/3: Token not set yet, retrying...")
                page.evaluate("""
                    (token) => {
                        var textarea = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                                       document.querySelector('#g-recaptcha-response');
                        if (textarea) {
                            textarea.value = token;
                            var field = document.getElementById('verificaRecaptcha');
                            if (field) field.value = 'true';
                            if (typeof recaptchaCallback === 'function') recaptchaCallback();
                        }
                    }
                """, token)
                time.sleep(1)

        verification = page.evaluate("""
            () => {
                var textarea = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                               document.querySelector('#g-recaptcha-response');
                return textarea ? textarea.value.length > 0 : false;
            }
        """)

        if verification:
            print("✅ Token is set (final check passed)")
            return True
        else:
            print("❌ Token injection failed after all retries")
            return False

    except Exception as e:
        print(f"⚠️ Error injecting token: {e}")
        import traceback
        traceback.print_exc()
        return False


def submit_search_form(page, start_date, end_date):
    """Fill the form and submit it."""
    try:
        print(f"📅 Setting date range: {start_date} to {end_date}")

        start_formatted = start_date.strftime("%d/%m/%Y")
        end_formatted = end_date.strftime("%d/%m/%Y")

        # Fill date fields
        start_input = page.locator("#txtDataInicio")
        start_input.fill(start_formatted)

        end_input = page.locator("#txtDataFim")
        end_input.fill(end_formatted)

        # Ensure checkbox is checked
        processos_checkbox = page.locator("#chkSinProcessos")
        if not processos_checkbox.is_checked():
            processos_checkbox.check()

        # Solve reCAPTCHA
        current_url = page.url

        # Check if reCAPTCHA is visible
        try:
            recaptcha_div = page.locator("#g-recaptcha")
            is_visible = recaptcha_div.is_visible()
            print(f"🔐 reCAPTCHA visible: {is_visible}")
        except:
            is_visible = False
            print("⚠️ Could not find reCAPTCHA element")

        if is_visible:
            token = solve_recaptcha_v2(
                RECAPTCHA_SITE_KEY, current_url, CAPTCHA_API_KEY)

            if token:
                max_retries = 3
                for retry in range(max_retries):
                    if fill_recaptcha_token(page, token):
                        print("✅ reCAPTCHA token successfully injected")
                        break
                    else:
                        if retry < max_retries - 1:
                            print(
                                f"⚠️ Retry {retry + 1}/{max_retries}: Attempting token injection again...")
                            time.sleep(2)
                        else:
                            print(
                                "❌ Failed to inject token after all retries. Proceeding anyway...")
            else:
                print("❌ Failed to solve reCAPTCHA. Skipping captcha...")
        else:
            print("ℹ️ reCAPTCHA not visible (may be hidden for first few attempts)")

        # Submit form
        submit_button = page.locator("#sbmPesquisar")
        submit_button.click()

        print("⏳ Waiting for search results...")
        time.sleep(5)

        return True

    except Exception as e:
        print(f"❌ Error submitting form: {e}")
        import traceback
        traceback.print_exc()
        return False


def handle_alert_if_present(page):
    """Handle browser dialogs - this is handled by context.on('dialog') in main."""
    # Dialogs are handled automatically by the context dialog handler
    return True


def check_and_solve_recaptcha_if_needed(page):
    """Check if reCAPTCHA is required and solve it if needed."""
    try:
        # Check if reCAPTCHA is visible
        try:
            recaptcha_div = page.locator("#g-recaptcha")
            is_visible = recaptcha_div.is_visible()

            if is_visible:
                print("🔐 reCAPTCHA detected during pagination, solving...")
                current_url = page.url
                token = solve_recaptcha_v2(
                    RECAPTCHA_SITE_KEY, current_url, CAPTCHA_API_KEY)

                if token:
                    max_retries = 3
                    for retry in range(max_retries):
                        if fill_recaptcha_token(page, token):
                            print("✅ reCAPTCHA token successfully injected")
                            return True
                        else:
                            if retry < max_retries - 1:
                                print(
                                    f"⚠️ Retry {retry + 1}/{max_retries}: Attempting token injection again...")
                                time.sleep(2)
                    print("⚠️ Failed to inject token after retries")
                else:
                    print("⚠️ Failed to solve reCAPTCHA")
                return False
        except:
            pass

        return True
    except Exception as e:
        print(f"⚠️ Error checking reCAPTCHA: {e}")
        return False


def parse_search_results(page):
    """Parse the HTML results from the search page to extract detail URLs."""
    try:
        time.sleep(3)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        results_list = []

        # Find all tables with results
        tables = soup.find_all("table")
        all_rows = []

        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                if row.find_all("td"):
                    all_rows.append(row)

        if not all_rows:
            print("⚠️ No table rows found")
            return []

        print(f"📊 Found {len(all_rows)} table rows")

        # Group consecutive rows: title row + metadata row
        idx = 0
        record_index = 1

        while idx < len(all_rows):
            row = all_rows[idx]
            is_title_row = "pesquisaTituloRegistro" in row.get("class", [])

            if is_title_row:
                title_row = row
                metadata_row = all_rows[idx + 1] if idx + \
                    1 < len(all_rows) else None

                try:
                    # Extract title for logging
                    title_text = title_row.get_text(
                        separator=" | ", strip=True)

                    # Extract all links from both rows
                    all_links = title_row.find_all("a", href=True)
                    if metadata_row:
                        all_links.extend(metadata_row.find_all("a", href=True))

                    # Get detail URL (first link)
                    detail_url = None
                    for link in all_links:
                        href = link.get("href", "")
                        if href:
                            if not href.startswith("http"):
                                href = requests.compat.urljoin(BASE_URL, href)
                            detail_url = href
                            break

                    if detail_url:
                        result_item = {
                            "index": record_index,
                            "title": title_text,
                            "detail_url": detail_url
                        }
                        results_list.append(result_item)
                        record_index += 1

                    idx += 2
                except Exception as e:
                    print(f"⚠️ Error parsing record at row {idx}: {e}")
                    idx += 1
            else:
                idx += 1

        print(f"✅ Parsed {len(results_list)} complete records")
        return results_list

    except Exception as e:
        print(f"❌ Error parsing results: {e}")
        import traceback
        traceback.print_exc()
        return []


def main(start_date=None, end_date=None, headless=True):
    """
    Main execution function.

    Args:
        start_date: datetime object for start date (default: 30 days ago)
        end_date: datetime object for end date (default: today)
        headless: bool, whether to run browser in headless mode (default: True)

    Returns:
        dict: {
            "success": bool,
            "search_date": str,
            "date_range": {"start": str, "end": str},
            "total_matched": int,
            "matched_results": list,
            "error": str (if failed)
        }
    """
    # Reset matched_data for each run
    global matched_data, deals
    matched_data = []

    # Initialize MongoDB connection for this script run
    mongodb_ok, mongodb_msg = init_mongodb_connection(ENV_PATH)
    if mongodb_ok:
        print(f"✅ {mongodb_msg}")
    else:
        print(f"⚠️ {mongodb_msg}")

    # Load deals from MongoDB when main() is called (connection should be ready by then)
    # Only load deals without 'brazil' node to avoid re-processing
    print("📊 Loading deals from MongoDB (excluding deals with 'brazil' node)...")
    load_deals(include_brazil=False)

    # Set default date range if not provided
    if end_date is None:
        end_date = datetime.datetime.now()
    if start_date is None:
        start_date = end_date - datetime.timedelta(days=10)

    with sync_playwright() as p:
        # Launch browser with options similar to Selenium
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Set up dialog handler
        context.on("dialog", lambda dialog: dialog.accept())

        page = context.new_page()

        try:
            print(f"🌐 Navigating to: {BASE_URL}")
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            print(
                f"🔍 Searching from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

            # Submit the form
            if submit_search_form(page, start_date, end_date):
                all_parsed_results = []
                page_num = 1

                while True:
                    print(f"\n📄 Processing page {page_num}...")

                    check_and_solve_recaptcha_if_needed(page)

                    parsed_results = parse_search_results(page)
                    all_parsed_results.extend(parsed_results)

                    print(
                        f"✅ Found {len(parsed_results)} results on page {page_num} (total: {len(all_parsed_results)})")

                    # Check for next page
                    try:
                        # First, determine the current page from the HTML
                        current_page_indicator = page.locator(
                            ".pesquisaPaginaSelecionada")
                        current_displayed_page = page_num
                        if current_page_indicator.count() > 0:
                            current_page_text = current_page_indicator.inner_text().strip()
                            if current_page_text.isdigit():
                                current_displayed_page = int(current_page_text)
                                if current_displayed_page != page_num:
                                    print(
                                        f"⚠️ Page mismatch: expected {page_num}, found {current_displayed_page}")
                                    page_num = current_displayed_page

                        # Method 1: Look for "Próxima" (Next) link
                        next_locator = page.locator(
                            "a:has-text('Próxima'), a:has-text('Próximo')")
                        has_next_link = next_locator.count() > 0

                        # Method 2: Check if there's a page number link for the next page
                        next_page_num = current_displayed_page + 1
                        next_page_link = None

                        if not has_next_link:
                            # Look for page number links in pagination area
                            page_links = page.locator(
                                ".pesquisaPaginas a").all()
                            for link in page_links:
                                link_text = link.inner_text().strip()
                                # Check if it's a number and matches next page
                                if link_text.isdigit() and int(link_text) == next_page_num:
                                    # Make sure it's not disabled
                                    class_attr = link.get_attribute(
                                        "class") or ""
                                    if "disabled" not in class_attr.lower():
                                        next_page_link = link
                                        has_next_link = True
                                        break

                        # Method 2: Check if there's a page number link for the next page
                        # (but not the current page which might be shown as selected)
                        next_page_link = None

                        # Method 3: Check pagination structure to see if we're on last page
                        # Look for the selected page indicator
                        current_page_indicator = page.locator(
                            ".pesquisaPaginaSelecionada")
                        current_displayed_page = page_num
                        if current_page_indicator.count() > 0:
                            current_page_text = current_page_indicator.inner_text().strip()
                            if current_page_text.isdigit():
                                current_displayed_page = int(current_page_text)
                                # If we're on a different page than expected, adjust
                                if current_displayed_page != page_num:
                                    print(
                                        f"⚠️ Page mismatch: expected {page_num}, found {current_displayed_page}")
                                    page_num = current_displayed_page

                        # Check if there are any page links higher than current page
                        all_page_links = page.locator(
                            ".pesquisaPaginas a").all()
                        found_higher_page = False
                        max_page_found = current_displayed_page
                        for link in all_page_links:
                            link_text = link.inner_text().strip()
                            if link_text.isdigit():
                                page_num_from_link = int(link_text)
                                if page_num_from_link > max_page_found:
                                    max_page_found = page_num_from_link
                                if page_num_from_link > current_displayed_page:
                                    found_higher_page = True
                                    break

                        # If no higher page found and no "Próxima" link, we're on last page
                        if not found_higher_page and not has_next_link:
                            print(
                                f"✅ Reached last page (current: {current_displayed_page}, max in links: {max_page_found})")
                            break

                        if has_next_link:
                            if next_page_link:
                                next_button = next_page_link
                            else:
                                next_button = next_locator.first

                            # Check if disabled
                            is_disabled = (
                                "disabled" in (next_button.get_attribute("class") or "").lower() or
                                (next_button.get_attribute(
                                    "aria-disabled") or "").lower() == "true"
                            )

                            if is_disabled:
                                print(
                                    "✅ Reached last page (next button is disabled)")
                                break

                            # Click next page
                            try:
                                print(f"🔄 Clicking next page button...")
                                next_button.scroll_into_view_if_needed()
                                time.sleep(0.5)
                                next_button.click()
                                time.sleep(4)

                                check_and_solve_recaptcha_if_needed(page)
                                # Verify we actually moved to the next page
                                time.sleep(1)  # Wait a bit for page to update
                                new_page_indicator = page.locator(
                                    ".pesquisaPaginaSelecionada")
                                if new_page_indicator.count() > 0:
                                    new_page_text = new_page_indicator.inner_text().strip()
                                    if new_page_text.isdigit():
                                        page_num = int(new_page_text)
                                        print(
                                            f"✅ Navigated to page {page_num}")
                                    else:
                                        page_num = next_page_num
                                        print(
                                            f"✅ Navigated to page {page_num}")
                                else:
                                    page_num = next_page_num
                                    print(f"✅ Navigated to page {page_num}")
                            except Exception as e:
                                print(f"⚠️ Could not click next page: {e}")
                                break
                        else:
                            print(
                                "✅ No more pages found (no next button or page link)")
                            break

                    except Exception as e:
                        print(f"⚠️ Error checking pagination: {e}")
                        import traceback
                        traceback.print_exc()
                        print("⚠️ Assuming last page due to error")
                        break

                parsed_results = all_parsed_results

                print(
                    f"\n✅ Total found: {len(parsed_results)} results across {page_num} page(s)")

                # Filter out results that are already processed (detail_url exists in brazil data)
                print(f"🔍 Checking which results are already processed...")
                filtered_results = []
                skipped_count = 0
                for result in parsed_results:
                    detail_url = result.get('detail_url')
                    if detail_url and detail_url_exists_in_brazil_data(detail_url):
                        skipped_count += 1
                    else:
                        filtered_results.append(result)

                if skipped_count > 0:
                    print(
                        f"⏭️ Skipped {skipped_count} already processed results")

                parsed_results = filtered_results
                print(
                    f"🔍 Processing {len(parsed_results)} new results to extract interessados and match with deals...")

                # Process each result
                for idx, result in enumerate(parsed_results):
                    print(
                        f"\n📋 Processing result {idx + 1}/{len(parsed_results)}: {result.get('title', 'N/A')[:80]}...")

                    detail_url = result.get('detail_url')

                    if not detail_url:
                        print("⚠️ No detail URL found, skipping...")
                        continue

                    # Double-check if detail_url exists (safety check)
                    if detail_url_exists_in_brazil_data(detail_url):
                        print(f"⏭️ Detail URL already processed, skipping...")
                        continue

                    # Extract Autuação information from detail page
                    autuacao_info = extract_autuacao_info_from_detail_page(
                        page, context, detail_url)

                    interessados_text = autuacao_info.get("interessados", "")

                    if not interessados_text or interessados_text.strip() == "":
                        print(
                            "⚠️ No interessados found or field is empty, skipping...")
                        continue

                    translated = translate_to_english(interessados_text)
                    print(f"🌐 Translated: {translated[:200]}...")

                    deal_match = match_with_deals(interessados_text)
                    if deal_match:
                        target = deal_match.get(
                            "target") or deal_match.get("target_name", "")
                        acquirer = deal_match.get(
                            "acquirer") or deal_match.get("acquire_name", "")
                        print(
                            f"🎯 String match found: Target: {target} / Acquirer: {acquirer}")
                    else:
                        llm_result = match_with_llm(
                            interessados_text, translated)
                        print(f"🧠 LLM Result: {llm_result}")

                        # Parse new format: "Match: DEAL_ID|COMPANY_NAME|(target|acquirer)"
                        if llm_result and llm_result.lower() != "none" and llm_result.lower().startswith("match"):
                            try:
                                # Remove "Match: " prefix
                                match_data = llm_result.replace(
                                    "Match: ", "").strip()

                                # Split by pipe
                                parts = match_data.split("|")
                                if len(parts) >= 3:
                                    deal_id = parts[0].strip()
                                    company_name = parts[1].strip()
                                    match_type = parts[2].strip().lower().replace(
                                        "(", "").replace(")", "")

                                    # Find deal by deal_id (most reliable)
                                    for deal in deals:
                                        if deal.get("deal_id") == deal_id:
                                            deal_match = deal
                                            print(
                                                f"✅ Found deal by ID: {deal_id}")
                                            break

                                    # Fallback: find by company name if deal_id didn't work
                                    if not deal_match:
                                        for deal in deals:
                                            target = deal.get("target") or deal.get(
                                                "target_name", "")
                                            acquirer = deal.get("acquirer") or deal.get(
                                                "acquire_name", "")

                                            if match_type == "target" and target and target.lower() == company_name.lower():
                                                deal_match = deal
                                                print(
                                                    f"✅ Found deal by target name: {company_name}")
                                                break
                                            elif match_type == "acquirer" and acquirer and acquirer.lower() == company_name.lower():
                                                deal_match = deal
                                                print(
                                                    f"✅ Found deal by acquirer name: {company_name}")
                                                break

                                    if not deal_match:
                                        print(
                                            f"⚠️ LLM found match but deal not found: {deal_id} / {company_name}")
                                        deal_match = {
                                            "llm_match": llm_result, "deal_id": deal_id, "company_name": company_name}
                                else:
                                    # Old format fallback
                                    match_text = match_data.split(
                                        "|")[0] if "|" in match_data else match_data.split(" (")[0].strip()
                                    match_type = "target"
                                    if "(acquirer)" in llm_result.lower() or "|acquirer" in llm_result.lower():
                                        match_type = "acquirer"

                                    for deal in deals:
                                        target = deal.get("target") or deal.get(
                                            "target_name", "")
                                        acquirer = deal.get("acquirer") or deal.get(
                                            "acquire_name", "")

                                        if match_type == "acquirer" and acquirer and acquirer.lower() == match_text.lower():
                                            deal_match = deal
                                            break
                                        elif match_type == "target" and target and target.lower() == match_text.lower():
                                            deal_match = deal
                                            break

                                    if not deal_match:
                                        deal_match = {"llm_match": llm_result}
                            except Exception as e:
                                print(f"⚠️ Error parsing LLM result: {e}")
                                deal_match = {"llm_match": llm_result}
                        else:
                            # No deal match found - verify if USA-related and email if True
                            try:
                                company_details = f"""
Process: {autuacao_info.get("process", "")}
Type: {autuacao_info.get("type", "")}
Registration Date: {autuacao_info.get("registration_date", "")}
Interested Parties (PT): {interessados_text}
Interested Parties (EN): {translated}
Detail URL: {detail_url}
""".strip()

                                is_usa_related = verify_usa_relation(
                                    company_details=company_details,
                                    case_type="BRAZIL",
                                )

                                if is_usa_related:
                                    print(
                                        "🇺🇸 USA-related CADE record detected - sending email")
                                    unmatched_data = {
                                        "process": autuacao_info.get("process", ""),
                                        "type": autuacao_info.get("type", ""),
                                        "registration_date": autuacao_info.get("registration_date", ""),
                                        "interessados": interessados_text,
                                        "interessados_en": translated,
                                        "detail_url": detail_url,
                                    }
                                    send_unmatched_brazil_email_via_webhook(
                                        unmatched_data)
                                else:
                                    print("ℹ️ Not USA-related - no action taken")
                            except Exception as e:
                                print(f"⚠️ Error verifying USA relation: {e}")
                                import traceback
                                traceback.print_exc()

                    if deal_match:
                        # Extract table data from detail page
                        print("📊 Extracting table data from detail page...")
                        table_data = extract_table_data_from_detail_page(
                            page, context, detail_url)

                        # Build the matched result object with ONLY required fields
                        # Get deal_id - handle both direct match and llm_match cases
                        deal_id = deal_match.get("deal_id", "")
                        if not deal_id and "llm_match" in deal_match:
                            deal_id = deal_match.get("deal_id", "")

                        matched_result = {
                            # Add deal ID for identification
                            "deal_id": deal_id,
                            "process": autuacao_info.get("process", ""),
                            "type": autuacao_info.get("type", ""),
                            "registration_date": autuacao_info.get("registration_date", ""),
                            "interessados": interessados_text,
                            "detail_url": detail_url,
                            "table_records": table_data,  # Array of table records
                            "matched_deal": deal_match
                        }

                        matched_data.append(matched_result)
                        print(
                            f"✅ Match found and added to results! (with {len(table_data)} table records)")

                        # Save to MongoDB under 'brazil' node in the deal record
                        # Check if we have enough info to identify the deal
                        has_deal_id = deal_match.get("deal_id")
                        has_acquirer = deal_match.get(
                            "acquirer") or deal_match.get("acquire_name")
                        has_target = deal_match.get(
                            "target") or deal_match.get("target_name")

                        if has_deal_id or has_acquirer or has_target:
                            save_result = save_brazil_data_to_deal(
                                deal_match, matched_result)
                            if save_result:
                                print(
                                    f"✅ Saved Brazil data to deal record in MongoDB")
                            else:
                                print(f"⚠️ Failed to save Brazil data to MongoDB")
                        else:
                            print(
                                f"⚠️ Cannot save to MongoDB: deal has no identifiable fields (deal_id, acquirer, or target)")

                # Prepare output - convert all datetime objects to strings
                matched_data_serializable = convert_datetime_to_string(
                    matched_data)

                matched_output = {
                    "success": True,
                    "search_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date_range": {
                        "start": start_date.strftime("%Y-%m-%d"),
                        "end": end_date.strftime("%Y-%m-%d")
                    },
                    "total_matched": len(matched_data),
                    "matched_results": matched_data_serializable
                }

                # Save to file as well
                with open(MATCHED_OUTPUT_JSON, "w", encoding="utf-8") as f:
                    json.dump(matched_output, f, ensure_ascii=False,
                              indent=2, default=str)

                print(f"💾 Matched results saved to: {MATCHED_OUTPUT_JSON}")
                print(
                    f"✅ Found {len(matched_data)} matches out of {len(parsed_results)} results")

                browser.close()
                # Return serializable version (datetime objects converted to strings)
                return matched_output
            else:
                print("❌ Failed to submit form")
                browser.close()
                return {
                    "success": False,
                    "error": "Failed to submit search form",
                    "search_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date_range": {
                        "start": start_date.strftime("%Y-%m-%d"),
                        "end": end_date.strftime("%Y-%m-%d")
                    },
                    "total_matched": 0,
                    "matched_results": []
                }

        except Exception as e:
            error_msg = str(e)
            print(f"❌ Error in main execution: {error_msg}")
            import traceback
            traceback.print_exc()
            browser.close()
            return {
                "success": False,
                "error": error_msg,
                "search_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "date_range": {
                    "start": start_date.strftime("%Y-%m-%d") if start_date else "",
                    "end": end_date.strftime("%Y-%m-%d") if end_date else ""
                },
                "total_matched": 0,
                "matched_results": []
            }


if __name__ == "__main__":
    main()
