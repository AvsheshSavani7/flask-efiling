import json
import time
import requests
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
from openai import OpenAI
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection
from llm_verification_service import verify_usa_relation

# Load OpenAI Key
load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants
URL = "https://www.accc.gov.au/public-registers/mergers-and-acquisitions-registers/acquisitions-register"
# CUTOFF_DATE = datetime.strptime("2026-01-20", "%Y-%m-%d")
CUTOFF_DATE = datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)
OUTPUT_PATH = "accc_matched_deals.json"
ENV_PATH = ".env"

# Global deals list - will be loaded from MongoDB
deals = []
matched_data = []
matched_count = 0


def normalize_company(name):
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").replace(" limited", "").replace(" corporation", "").replace(" corp.", "").strip()


def get_deals_from_mongodb(include_accc_cases=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_accc_cases: If False, only return deals that don't have an 'accc_cases' node

    Returns:
        List of deal dictionaries
    """
    try:
        # Use global MongoDB connection
        collection = get_deals_collection()

        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Build query - exclude deals with 'accc_cases' node if include_accc_cases is False
        query = {}
        if not include_accc_cases:
            # query = {"accc_cases": {"$exists": False}}
            query = {
                "$or": [
                    {"accc_cases": {"$exists": False}},
                    {"accc_cases": None},
                    {"accc_cases": []}
                ]
            }

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'accc_cases' node" if not include_accc_cases else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_accc_cases=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_accc_cases: If False, only load deals that don't have an 'accc_cases' node
    """
    global deals
    deals = get_deals_from_mongodb(include_accc_cases=include_accc_cases)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def match_with_llm(title):
    """Use LLM to match deal with Deal ID included in response"""
    # Build deals list with Deal ID, Target, Acquirer, and aliases
    lines = []
    for d in deals:
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {target} | Acquirer: {acquirer}"
        target_aliases = d.get("target_aliases", []) or []
        parent_aliases = d.get("parent_aliases", []) or []
        if target_aliases:
            line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
        if parent_aliases:
            line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""You are an expert M&A deal matcher. Your task is to determine if ANY company mentioned in the acquisition title appears in our deals database.

DEALS DATABASE:
{deals_text}

ACQUISITION TITLE TO MATCH:
{title}

MATCHING INSTRUCTIONS:
1. Extract ALL company names from the acquisition title (both acquirer and target)
2. Check if ANY of these company names appears as either a Target OR Acquirer in the deals database
3. When matching, also consider target_aliases and parent_aliases - if the title matches an alias, treat it as a match for that deal
4. Consider variations, abbreviations, and partial matches (e.g., "Warburg Pincus" matches "Warburg Pincus LLC")
5. Match on a SINGLE company name - you don't need both companies to match
6. IMPORTANT: Even if the deal structure is different, match if you find the company name

RESPONSE FORMAT:
- If you find ANY match, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|Warburg Pincus|acquirer

- If NO match is found after thorough checking, respond with:
  None

CRITICAL: Match if you see ANY company name from the title in the database (including Target, Acquirer, or alias), regardless of whether it's the same deal or different target/acquirer combination."""

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system",
                    "content": "You are an expert M&A deal identifier and matcher."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=150
        )
        print(f"🔍 Prompt: {prompt}")
        print(f"🔍 Response: {res}")

        return res.choices[0].message.content.strip()
    except Exception as e:
        return f"LLM Error: {e}"


def parse_date(date_str):
    """Parse date string from ACCC format (e.g., '30 Jan 2026')"""
    try:
        return datetime.strptime(date_str, "%d %b %Y")
    except:
        return None


def extract_list_items_from_html(html_content):
    """Extract all list items from the HTML using BeautifulSoup"""
    soup = BeautifulSoup(html_content, 'html.parser')
    rows = soup.select(".views-row")

    all_items = []
    for idx, row in enumerate(rows):
        try:
            item_info = {}

            # Extract title
            title_elem = row.select_one("h3")
            if title_elem:
                item_info["title"] = title_elem.get_text(strip=True)

            # Extract href for detail page
            link_elem = row.select_one(
                "a[href*='/public-registers/mergers-and-acquisitions-registers/acquisitions-register/']")
            if link_elem and link_elem.get("href"):
                href = link_elem["href"]
                if href and not href.startswith("http"):
                    item_info["detail_url"] = "https://www.accc.gov.au" + href
                else:
                    item_info["detail_url"] = href

            # Extract acquisition status
            status_elem = row.select_one(
                ".field--name-field-acccgov-merger-status .field__item")
            if status_elem:
                item_info["acquisition_status"] = status_elem.get_text(
                    strip=True)

            # Extract type
            type_elem = row.select_one(".field--acccgov-type .field__item")
            if type_elem:
                item_info["type"] = type_elem.get_text(strip=True)

            # Extract case number
            case_number_elem = row.select_one(
                ".field--name-field-acccgov-mcmsmergermatterno .field__item")
            if case_number_elem:
                item_info["case_number"] = case_number_elem.get_text(
                    strip=True)

            # Extract stage
            stage_elem = row.select_one(
                ".field--name-field-acquisition-stage .field__item")
            if stage_elem:
                item_info["stage"] = stage_elem.get_text(strip=True)

            # Extract effective notification date
            date_elem = row.select_one(
                ".field--name-field-acccgov-pub-reg-date .field__item time")
            if date_elem:
                date_text = date_elem.get_text(strip=True)
                item_info["effective_notification_date"] = date_text
                item_info["notification_date_parsed"] = parse_date(date_text)

            if "title" in item_info:
                all_items.append(item_info)

        except Exception as e:
            print(f"⚠️ Error extracting item #{idx + 1}: {e}")
            continue

    return all_items


def format_date(date_str: str) -> str:
    """Format date from 'DD Mon YYYY' to 'DD.MM.YYYY'"""
    try:
        dt = datetime.strptime(date_str, "%d %b %Y")
        return dt.strftime("%d.%m.%Y")
    except:
        return date_str


def generate_accc_case_email_html(case_info: dict, deal_match: dict) -> tuple:
    """
    Generate HTML email for ACCC case match - ACCC website style.

    Args:
        case_info: The ACCC case data dictionary
        deal_match: The matched deal object

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")

    # Extract case information
    case_number = case_info.get("case_number", "N/A")
    title = case_info.get("title", "N/A")
    acquisition_status = case_info.get("acquisition_status", "N/A")
    case_type = case_info.get("type", "N/A")
    stage = case_info.get("stage", "N/A")
    notification_date = case_info.get("effective_notification_date", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("details", {})

    # Build subject
    subject = f"ACCC Acquisition Match – {target} / {acquirer}"

    # Determine status badge color
    status_color = "#1e1b4b"  # default dark blue
    if "under assessment" in acquisition_status.lower():
        status_color = "#1e1b4b"
    elif "not opposed" in acquisition_status.lower():
        status_color = "#059669"
    elif "withdrawn" in acquisition_status.lower():
        status_color = "#6b7280"

    # Build HTML matching ACCC website style
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ACCC Acquisition Match - {case_number}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- Deal Match Info Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
<div style="font-size:14px;color:#1e3a8a;">
<span style="font-weight:700;">Acquirer:</span> {acquirer} <span style="color:#94a3b8;margin:0 8px;">|</span>
<span style="font-weight:700;">Target:</span> {target}
</div>'''

    if detail_url:
        html += f'''
<div style="margin-top:10px;">
<a href="{detail_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View ACCC Case →</a>
</div>'''

    html += f'''
</div>

<!-- Top summary panel -->
<div style="background:#f3f4f6;border-radius:2px;padding:22px 26px;">
<div style="display:grid;grid-template-columns:260px 1fr;row-gap:16px;column-gap:18px;align-items:center;">
<div style="font-weight:700;color:#111827;">Acquisition status:</div>
<div>
<span style="display:inline-block;padding:8px 14px;border-radius:6px;background:{status_color};color:#ffffff;font-weight:700;font-size:14px;line-height:1;">
{acquisition_status}
</span>
</div>

<div style="font-weight:700;color:#111827;">Acquisition case number:</div>
<div style="color:#111827;">{case_number}</div>

<div style="font-weight:700;color:#111827;">Type:</div>
<div style="color:#111827;">{case_type}</div>

<div style="font-weight:700;color:#111827;">Effective notification date:</div>
<div style="color:#111827;">{notification_date}</div>
</div>
</div>'''

    # Status section
    end_period = details.get("end_of_determination_period", "")
    if stage or end_period:
        html += '''
<!-- Section: Status -->
<div style="margin-top:36px;">
<div style="font-size:22px;font-weight:800;color:#0f172a;margin:0 0 14px 0;">Status</div>
<div style="height:1px;background:#e5e7eb;"></div>

<div style="display:grid;grid-template-columns:240px 1fr;row-gap:14px;column-gap:18px;padding-top:18px;">'''

        if stage:
            html += f'''
<div style="color:#111827;">Stage:</div>
<div style="color:#111827;">{stage}</div>'''

        if end_period:
            html += f'''
<div style="color:#111827;">End of determination period:</div>
<div style="color:#111827;">{end_period}</div>'''

        html += '''
</div>
</div>'''

    # About the acquisition section
    acquirers = details.get("acquirers", [])
    targets = details.get("targets", [])
    other_parties = details.get("other_parties", [])
    anzsic = details.get("anzsic_codes", "")
    description = details.get("description", "")

    if acquirers or targets or other_parties or anzsic or description:
        html += '''
<!-- Section: About the acquisition -->
<div style="margin-top:34px;">
<div style="font-size:22px;font-weight:800;color:#0f172a;margin:0 0 14px 0;">About the acquisition</div>
<div style="height:1px;background:#e5e7eb;"></div>

<div style="display:grid;grid-template-columns:240px 1fr;column-gap:18px;row-gap:18px;padding-top:18px;">'''

        # Acquirers
        if acquirers:
            html += '''
<div style="color:#111827;">Acquirer(s):</div>
<div>'''
            for i, acq in enumerate(acquirers):
                margin_bottom = "8px" if i < len(acquirers) - 1 else "0"
                html += f'''
<div style="margin:0 0 {margin_bottom} 0;">
<span style="font-weight:800;color:#111827;">{acq.get("name", "N/A")}</span>'''
                if acq.get("registration"):
                    html += f'''
<span style="float:right;color:#111827;">{acq["registration"]}</span>'''
                html += '''
<div style="clear:both;"></div>
</div>'''
            html += '''
</div>'''

        # Targets
        if targets:
            html += '''
<div style="color:#111827;">Target(s) or Vendor(s):</div>
<div>'''
            for i, tgt in enumerate(targets):
                margin_bottom = "8px" if i < len(targets) - 1 else "0"
                html += f'''
<div style="margin:0 0 {margin_bottom} 0;">
<span style="font-weight:800;color:#111827;">{tgt.get("name", "N/A")}</span>'''
                if tgt.get("registration"):
                    html += f'''
<span style="float:right;color:#111827;">{tgt["registration"]}</span>'''
                html += '''
<div style="clear:both;"></div>
</div>'''
            html += '''
</div>'''

        # Other parties
        if other_parties:
            html += '''
<div style="color:#111827;">Other party(ies):</div>
<div>'''
            for i, party in enumerate(other_parties):
                margin_bottom = "10px" if i < len(other_parties) - 1 else "0"
                html += f'''
<div style="margin:0 0 {margin_bottom} 0;">
<span style="font-weight:800;color:#111827;">{party.get("name", "N/A")}</span>'''
                if party.get("registration"):
                    html += f'''
<span style="float:right;color:#111827;">{party["registration"]}</span>'''
                html += '''
<div style="clear:both;"></div>
</div>'''
            html += '''
</div>'''

        # ANZSIC codes
        if anzsic:
            html += f'''
<div style="color:#111827;">ANZSIC code(s):</div>
<div style="color:#111827;">{anzsic}</div>'''

        # Description
        if description:
            html += f'''
<div style="color:#111827;">Description:</div>
<div style="color:#111827;line-height:1.55;">{description}</div>'''

        html += '''
</div>
</div>'''

    # Consultation section
    consultation_text = details.get("consultation_text", "")
    consultation_docs = details.get("consultation_documents", [])

    if consultation_text or consultation_docs:
        html += '''
<!-- Section: Consultation -->
<div style="margin-top:36px;">
<div style="font-size:22px;font-weight:800;color:#0f172a;margin:0 0 14px 0;">Consultation</div>
<div style="height:1px;background:#e5e7eb;"></div>'''

        if consultation_text:
            html += f'''
<div style="padding-top:18px;color:#111827;line-height:1.6;">
{consultation_text}
</div>'''

        if consultation_docs:
            for doc in consultation_docs:
                doc_date = doc.get("date", "")
                doc_title = doc.get("title", "")
                doc_url = doc.get("document_url", "")
                if doc_url:
                    html += f'''
<!-- Attachment row -->
<div style="margin-top:18px;background:#f3f4f6;border-radius:2px;padding:12px 14px;">
<div style="display:flex;align-items:center;gap:18px;">
<div style="min-width:110px;color:#111827;">{doc_date}</div>

<div style="flex:1;color:#111827;">
{doc_title}
</div>

<div style="display:flex;align-items:center;gap:10px;">
<svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="display:block;">
<path d="M7 3h7l3 3v15a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3z" stroke="#2563eb" stroke-width="2"/>
<path d="M14 3v4a2 2 0 0 0 2 2h4" stroke="#2563eb" stroke-width="2"/>
</svg>

<a href="{doc_url}" style="color:#2563eb;text-decoration:none;font-weight:800;">Attachment</a>
</div>
</div>
</div>'''

        html += '''
</div>'''

    html += '''
</div>
</body>
</html>'''

    return subject, html


def send_accc_case_email_via_webhook(case_info: dict, deal_match: dict) -> bool:
    """
    Send email notification via n8n webhook after saving ACCC case data.

    Args:
        case_info: The ACCC case data dictionary
        deal_match: The matched deal object

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_accc_case_email_html(
            case_info, deal_match)
        print(f"📝 Generated email subject: {subject}")
        with open("accc_case_email.html", "w") as f:
            f.write(html_email)

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

        # Extract case information
        case_number = case_info.get("case_number", "N/A")
        case_title = case_info.get("title", "N/A")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': deal_id,
            'target': target,
            'acquirer': acquirer,
            'case_number': case_number,
            'case_title': case_title,
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


def generate_unmatched_accc_email_html(accc_data: dict) -> tuple:
    """
    Generate HTML email for unmatched USA-related ACCC acquisition.

    Args:
        accc_data: The ACCC acquisition data dictionary

    Returns:
        Tuple of (subject, html_email)
    """
    case_number = accc_data.get("case_number", "N/A")
    title = accc_data.get("title", "N/A")
    acquisition_status = accc_data.get("acquisition_status", "N/A")
    case_type = accc_data.get("type", "N/A")
    stage = accc_data.get("stage", "N/A")
    notification_date = accc_data.get("effective_notification_date", "N/A")
    detail_url = accc_data.get("detail_url", "")

    # Build subject
    subject = f"🇺🇸 USA-Related ACCC Acquisition – {case_number}"

    # Determine status badge color
    status_color = "#1e1b4b"
    if "assessment completed" in acquisition_status.lower():
        status_color = "#14b8a6"
    elif "under assessment" in acquisition_status.lower():
        status_color = "#1e1b4b"
    elif "not opposed" in acquisition_status.lower():
        status_color = "#059669"

    # Build HTML
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>USA-Related ACCC Acquisition (Unmatched) - {case_number}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- USA-Related Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
<div style="font-size:16px;font-weight:800;color:#1e40af;margin-bottom:6px;">🇺🇸 USA-Related ACCC Acquisition Detected</div>
<div style="font-size:14px;color:#1e3a8a;">
This ACCC acquisition appears to involve USA-related companies.
</div>'''

    if detail_url:
        html += f'''
<div style="margin-top:10px;">
<a href="{detail_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View ACCC Case →</a>
</div>'''

    html += f'''
</div>

<!-- Case Title -->
<div style="margin-bottom:24px;">
<div style="font-size:24px;font-weight:900;color:#111827;margin-bottom:8px;">{title}</div>
</div>

<!-- Top summary panel -->
<div style="background:#f3f4f6;border-radius:2px;padding:22px 26px;">
<div style="display:grid;grid-template-columns:260px 1fr;row-gap:16px;column-gap:18px;align-items:center;">

<div style="font-weight:700;">Acquisition status:</div>
<div>
<span style="display:inline-block;padding:8px 14px;border-radius:6px;background:{status_color};color:#ffffff;font-weight:800;font-size:14px;">
{acquisition_status}
</span>
</div>

<div style="font-weight:700;">Acquisition case number:</div>
<div>{case_number}</div>

<div style="font-weight:700;">Type:</div>
<div>{case_type}</div>

<div style="font-weight:700;">Effective notification date:</div>
<div>{notification_date}</div>'''

    if stage:
        html += f'''

<div style="font-weight:700;">Stage:</div>
<div>{stage}</div>'''

    html += '''
</div>
</div>

</div>
</body>
</html>'''

    return subject, html


def send_unmatched_accc_email_via_webhook(accc_data: dict) -> bool:
    """
    Send email notification via n8n webhook for unmatched ACCC acquisition that is USA-related.

    Args:
        accc_data: The ACCC acquisition data dictionary

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        subject, html_email = generate_unmatched_accc_email_html(accc_data)
        print(f"📝 Generated email subject: {subject}")
        with open("unmatched_accc_case_email.html", "w") as f:
            f.write(html_email)

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
        )
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": "N/A",
            "target": "N/A",
            "acquirer": "N/A",
            "case_number": accc_data.get("case_number", "N/A"),
            "title": accc_data.get("title", "N/A"),
            "acquisition_status": accc_data.get("acquisition_status", "N/A"),
            "type": accc_data.get("type", "N/A"),
            "stage": accc_data.get("stage", "N/A"),
            "notification_date": accc_data.get("effective_notification_date", "N/A"),
            "detail_url": accc_data.get("detail_url", ""),
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


def save_accc_case_to_deal(deal_match, case_info):
    """
    Save matched ACCC case to MongoDB deal record under 'accc_cases' array.

    Args:
        deal_match: The matched deal object (must have deal_id to identify)
        case_info: The case info to save

    Returns:
        bool: True if saved successfully, False otherwise
    """
    try:
        # Use global MongoDB connection
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save to MongoDB")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available, skipping save to MongoDB")
            return False

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

        # Get case number to check if it already exists
        case_number = case_info.get("case_number")

        # Check if case_number already exists in this deal's accc_cases
        existing_deal = collection.find_one(query)
        if existing_deal and "accc_cases" in existing_deal:
            for existing_case in existing_deal["accc_cases"]:
                existing_case_number = existing_case.get("case_number")
                if existing_case_number == case_number:
                    print(
                        f"   ⏩ Skipped (case {case_number} already exists in deal)")
                    return False

        # Update the deal document by adding to accc_cases array
        update_result = collection.update_one(
            query,
            {
                "$push": {
                    "accc_cases": case_info
                }
            }
        )

        if update_result.modified_count > 0:
            print(f"   ✅ Saved ACCC case to deal record in MongoDB")

            # Send email notification via n8n webhook
            try:
                send_accc_case_email_via_webhook(case_info, deal_match)
            except Exception as e:
                print(f"   ⚠️ Error sending email notification: {e}")
                # Don't fail the save operation if email fails

            return True
        elif update_result.matched_count > 0:
            print(f"   ℹ️ Deal found but no changes made (case may already exist)")
            return True
        else:
            print(f"   ⚠️ Deal not found in MongoDB: {query}")
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


def extract_detail_page_info(page, url):
    """Extract detailed info from the acquisition detail page"""
    try:
        print(f"  📄 Fetching detail page: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        details = {}

        # Extract end of determination period
        try:
            end_period_elem = page.query_selector(
                ".field--name-field-acccgov-end-determination .field__item time")
            if end_period_elem:
                details["end_of_determination_period"] = end_period_elem.inner_text(
                ).strip()
        except:
            pass

        # Extract acquirer(s)
        try:
            acquirers = []
            acquirer_section = page.query_selector(
                ".field--name-field-acccgov-applicants")
            if acquirer_section:
                company_elements = acquirer_section.query_selector_all(
                    ".paragraph--type--acccgov-trader")
                for company_elem in company_elements:
                    name_elem = company_elem.query_selector(
                        ".field_acccgov_name")
                    acn_elem = company_elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')")

                    company_data = {}
                    if name_elem:
                        company_data["name"] = name_elem.inner_text().strip()
                    if acn_elem:
                        company_data["registration"] = acn_elem.inner_text(
                        ).strip()

                    if company_data:
                        acquirers.append(company_data)

            if acquirers:
                details["acquirers"] = acquirers
        except Exception as e:
            print(f"⚠️ Error extracting acquirers: {e}")

        # Extract target(s)/vendor(s)
        try:
            targets = []
            target_section = page.query_selector(
                ".field--name-field-acccgov-pub-reg-targets")
            if target_section:
                company_elements = target_section.query_selector_all(
                    ".paragraph--type--acccgov-trader")
                for company_elem in company_elements:
                    name_elem = company_elem.query_selector(
                        ".field_acccgov_name")
                    acn_elem = company_elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')")

                    company_data = {}
                    if name_elem:
                        company_data["name"] = name_elem.inner_text().strip()
                    if acn_elem:
                        company_data["registration"] = acn_elem.inner_text(
                        ).strip()

                    if company_data:
                        targets.append(company_data)

            if targets:
                details["targets"] = targets
        except Exception as e:
            print(f"⚠️ Error extracting targets: {e}")

        # Extract other parties
        try:
            other_parties = []
            other_section = page.query_selector(
                ".field--name-field-acccgov-other-parties")
            if other_section:
                company_elements = other_section.query_selector_all(
                    ".paragraph--type--acccgov-trader")
                for company_elem in company_elements:
                    name_elem = company_elem.query_selector(
                        ".field_acccgov_name")
                    acn_elem = company_elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')")

                    company_data = {}
                    if name_elem:
                        company_data["name"] = name_elem.inner_text().strip()
                    if acn_elem:
                        company_data["registration"] = acn_elem.inner_text(
                        ).strip()

                    if company_data:
                        other_parties.append(company_data)

            if other_parties:
                details["other_parties"] = other_parties
        except Exception as e:
            print(f"⚠️ Error extracting other parties: {e}")

        # Extract ANZSIC codes
        try:
            anzsic_elem = page.query_selector(
                ".field--name-field-acquisition-anzsic-code .field__item")
            if anzsic_elem:
                details["anzsic_codes"] = anzsic_elem.inner_text().strip()
        except:
            pass

        # Extract description
        try:
            desc_elem = page.query_selector(
                ".field--name-field-accc-body .full-text, .field--name-field-accc-body .summary-text")
            if desc_elem:
                # Try to expand "read more" if present
                try:
                    read_more = page.query_selector(
                        ".field--name-field-accc-body .read-toggle")
                    if read_more:
                        read_more.click()
                        page.wait_for_timeout(500)
                        # Re-query for full text
                        desc_elem = page.query_selector(
                            ".field--name-field-accc-body .full-text")
                except:
                    pass

                if desc_elem:
                    details["description"] = desc_elem.inner_text().strip()
        except Exception as e:
            print(f"⚠️ Error extracting description: {e}")

        # Extract consultation information
        try:
            consultation_text_elem = page.query_selector(
                ".field--name-field-acccgov-consultation-text .full-text, .field--name-field-acccgov-consultation-text .summary-text")
            if consultation_text_elem:
                # Try to expand "read more" if present
                try:
                    read_more = page.query_selector(
                        ".field--name-field-acccgov-consultation-text .read-toggle")
                    if read_more:
                        read_more.click()
                        page.wait_for_timeout(500)
                        consultation_text_elem = page.query_selector(
                            ".field--name-field-acccgov-consultation-text .full-text")
                except:
                    pass

                if consultation_text_elem:
                    details["consultation_text"] = consultation_text_elem.inner_text(
                    ).strip()

            # Extract consultation documents
            consultations = []
            consultation_rows = page.query_selector_all(
                ".field--name-field-acccgov-consultations table tbody tr")
            for row in consultation_rows:
                try:
                    date_elem = row.query_selector("time")
                    title_elem = row.query_selector("td:nth-child(2)")
                    link_elem = row.query_selector(
                        "a[href$='.docx'], a[href$='.pdf'], a[href$='.doc']")

                    consultation_data = {}
                    if date_elem:
                        consultation_data["date"] = date_elem.inner_text(
                        ).strip()
                    if title_elem:
                        consultation_data["title"] = title_elem.inner_text(
                        ).strip()
                    if link_elem:
                        href = link_elem.get_attribute("href")
                        if href and not href.startswith("http"):
                            href = "https://www.accc.gov.au" + href
                        consultation_data["document_url"] = href

                    if consultation_data:
                        consultations.append(consultation_data)
                except:
                    continue

            if consultations:
                details["consultation_documents"] = consultations
        except Exception as e:
            print(f"⚠️ Error extracting consultation info: {e}")

        return details
    except Exception as e:
        print(f"⚠️ Error extracting detail page: {e}")
        return {}


def main():
    global matched_count
    matched_count = 0

    print("🚀 Starting ACCC Acquisitions Scraper\n")

    try:
        # Initialize MongoDB connection
        print("🔌 Initializing MongoDB connection...")
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            print(f"❌ {message}")
            print("   MongoDB connection is required. Exiting.")
            return
        print(f"✅ {message}\n")

        # Load deals from MongoDB
        print("📊 Loading deals from MongoDB (excluding deals with 'accc_cases' node)...")
        load_deals(include_accc_cases=False)

        if not deals:
            print("⚠️ No deals found in MongoDB. Exiting.")
            return

    except Exception as e:
        print(f"❌ Error during initialization: {e}")
        import traceback
        traceback.print_exc()
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"\n📄 Fetching ACCC Acquisitions Register: {URL}")
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # First pass: Extract all basic info from list view using BeautifulSoup
        print("📋 Extracting basic information from all entries...")

        try:
            page.wait_for_selector(".views-row", timeout=10000)
            html_content = page.content()
            all_items = extract_list_items_from_html(html_content)
            print(f"✅ Found {len(all_items)} acquisition entries\n")
        except Exception as e:
            print(f"⚠️ No acquisition entries found: {e}")
            browser.close()
            return

        # Second pass: Process each item and fetch details for matches
        for idx, item_info in enumerate(all_items):
            try:
                title = item_info["title"]
                notification_date = item_info.get("notification_date_parsed")

                print(f"🔍 [{idx + 1}] {title}")
                if notification_date:
                    print(f"   📅 {notification_date.strftime('%Y-%m-%d')}")

                # Check cutoff date
                if notification_date and notification_date < CUTOFF_DATE:
                    print("✅ Reached cutoff date. Stopping.")
                    break

                # Use LLM matching for all cases
                deal_match = None
                matched_company = None
                matched_role = None

                result = match_with_llm(title)
                print(f"🧠 LLM Result: {result}")

                if result and result.lower().startswith("match"):
                    try:
                        # Parse new format: Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
                        match_pattern = r"Match:\s*([^|]+)\|([^|]+)\|(target|acquirer)"
                        match_obj = re.search(
                            match_pattern, result, re.IGNORECASE)
                        if match_obj:
                            deal_id = match_obj.group(1).strip()
                            matched_company = match_obj.group(2).strip()
                            matched_role = match_obj.group(3).strip().lower()

                            # Find the deal by deal_id
                            for deal in deals:
                                if deal.get("deal_id") == deal_id:
                                    deal_match = deal
                                    # Handle both naming conventions
                                    acquirer_name = deal_match.get(
                                        'acquirer') or deal_match.get('acquire_name', 'N/A')
                                    target_name = deal_match.get(
                                        'target') or deal_match.get('target_name', 'N/A')
                                    print(
                                        f"🎯 LLM Match: {acquirer_name} / {target_name} (matched on {matched_role})")
                                    break

                            if not deal_match:
                                print(
                                    f"⚠️ Deal ID {deal_id} not found in deals list")
                    except Exception as e:
                        print(f"⚠️ Error parsing LLM result: {e}")

                # If we have a match, fetch detail page
                if deal_match and "detail_url" in item_info:
                    print(f"  ✅ Matched! Fetching detailed information...")

                    # Extract detailed info from detail page
                    detail_info = extract_detail_page_info(
                        page, item_info["detail_url"])

                    case_info = {
                        "title": title,
                        "case_number": item_info.get("case_number", ""),
                        "acquisition_status": item_info.get("acquisition_status", ""),
                        "type": item_info.get("type", ""),
                        "stage": item_info.get("stage", ""),
                        "effective_notification_date": item_info.get("effective_notification_date", ""),
                        "detail_url": item_info.get("detail_url", ""),
                        "matched_company": matched_company or "",
                        "matched_role": matched_role or "",
                        "details": detail_info
                    }

                    # Save to MongoDB
                    save_result = save_accc_case_to_deal(deal_match, case_info)

                    if save_result:
                        # Add to local list for this session (for backward compatibility)
                        if "accc_cases" not in deal_match:
                            deal_match["accc_cases"] = []
                        deal_match["accc_cases"].append(case_info)

                        acquirer = deal_match.get(
                            "acquirer") or deal_match.get("acquire_name", "N/A")
                        target = deal_match.get("target") or deal_match.get(
                            "target_name", "N/A")
                        print(f"  ✅ Added to deal: {acquirer} / {target}")
                        matched_count += 1

                    # Also keep for backward compatibility
                    matched_data.append({
                        "title": title,
                        "case_number": item_info.get("case_number", ""),
                        "matched_deal": {
                            "acquirer": deal_match.get("acquirer") or deal_match.get("acquire_name", ""),
                            "target": deal_match.get("target") or deal_match.get("target_name", "")
                        },
                        "matched_company": matched_company,
                        "matched_role": matched_role,
                        "case_info": case_info
                    })
                else:
                    # No deal match found - verify if USA-related and email if True
                    print("  ⏭️  No match found")
                    try:
                        accc_details = f"""
Case Number: {item_info.get("case_number", "")}
Title: {title}
Type: {item_info.get("type", "")}
Acquisition Status: {item_info.get("acquisition_status", "")}
Stage: {item_info.get("stage", "")}
Notification Date: {item_info.get("effective_notification_date", "")}
Detail URL: {item_info.get("detail_url", "")}
""".strip()

                        is_usa_related = verify_usa_relation(
                            company_details=accc_details,
                            case_type="ACCC",
                        )

                        if is_usa_related:
                            print(
                                "  🇺🇸 USA-related ACCC acquisition detected - sending email")
                            unmatched_data = {
                                "case_number": item_info.get("case_number", ""),
                                "title": title,
                                "type": item_info.get("type", ""),
                                "acquisition_status": item_info.get("acquisition_status", ""),
                                "stage": item_info.get("stage", ""),
                                "effective_notification_date": item_info.get("effective_notification_date", ""),
                                "detail_url": item_info.get("detail_url", ""),
                            }
                            send_unmatched_accc_email_via_webhook(
                                unmatched_data)
                        else:
                            print("  ℹ️  Not USA-related - no action taken")
                    except Exception as e:
                        print(f"  ⚠️ Error verifying USA relation: {e}")
                        import traceback
                        traceback.print_exc()

                print()  # Empty line for readability

            except Exception as e:
                print(f"❌ Error processing item #{idx + 1}: {e}")
                continue

        browser.close()

    # Save matched data for reference (JSON backup)
    print(f"\n💾 Saving matched data to JSON backup: {OUTPUT_PATH}")
    try:
        with open(OUTPUT_PATH, "w") as f:
            json.dump(matched_data, f, indent=2)
        print(
            f"✅ Successfully saved {len(matched_data)} matched entries to JSON backup")
    except Exception as e:
        print(f"⚠️ Error saving matched data to JSON: {e}")

    print(f"\n🎉 Done!")
    if is_connected():
        print(f"   💾 Matched cases saved to MongoDB deals collection")
    print(f"   📁 Matched data (JSON backup) → {OUTPUT_PATH}")
    print(f"   🎯 Total matches: {matched_count}")


if __name__ == "__main__":
    main()
