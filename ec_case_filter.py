import json
import requests
from datetime import datetime, date
from typing import Dict, Any, List
import os
from dotenv import load_dotenv
from openai import OpenAI
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection
from html import escape as escape_html
from datetime import datetime, timedelta

# Constants
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-M.json"
# Temporary: use local file
LOCAL_DATA_PATH = "/Users/joshuatackel/Downloads/case-data-M.json"
# CUTOFF_DATE = datetime.strptime("2026-01-01", "%Y-%m-%d")
CUTOFF_DATE = datetime.now().replace(hour=0, minute=0, second=0,
                                     microsecond=0) - timedelta(days=1)
OUTPUT_PATH = "ec_filtered_cases.json"
MATCHED_DEALS_OUTPUT = "ec_matched_deals.json"
ENV_PATH = ".env"

# Load OpenAI API Key
load_dotenv(ENV_PATH)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Global deals list - will be loaded from MongoDB
deals = []


def download_json(url: str) -> Dict[str, Any]:
    """Download JSON data from the given URL."""
    print(f"📥 Downloading data from: {url}")
    try:
        response = requests.get(url, timeout=60, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()
        data = response.json()
        print(f"✅ Successfully downloaded JSON data")
        return data
    except requests.exceptions.RequestException as e:
        print(f"❌ Error downloading data: {e}")
        raise
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing JSON: {e}")
        print(f"   Response content (first 500 chars): {response.text[:500]}")
        raise


def load_json_from_file(file_path: str) -> Dict[str, Any]:
    """Load JSON data from a local file."""
    print(f"📂 Loading data from local file: {file_path}")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"✅ Successfully loaded JSON data from local file")
        return data
    except FileNotFoundError:
        print(f"❌ Error: File not found at {file_path}")
        raise
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing JSON: {e}")
        raise
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        raise


def parse_date(date_str: str) -> datetime:
    """Parse date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def matches_criteria(case_data: Dict[str, Any]) -> bool:
    """
    Check if a case matches all filtering criteria:
    - caseInitiationDate > cutoff date
    - caseInstrument: ["Merger"]
    - caseInvestigationPhase: ["1"]
    - caseCartel: ["Antitrust"]
    - decisions: [] (empty array)
    """
    metadata = case_data.get("metadata", {})

    # Check caseInstrument
    case_instrument = metadata.get("caseInstrument", [])
    if "Merger" not in case_instrument:
        return False

    # Check caseInvestigationPhase
    # case_investigation_phase = metadata.get("caseInvestigationPhase", [])
    # if "1" not in case_investigation_phase:
    #     return False

    # Check caseCartel
    case_cartel = metadata.get("caseCartel", [])
    if "Antitrust" not in case_cartel:
        return False

    # Check decisions (must be empty array)
    # decisions = case_data.get("decisions", [])
    # if decisions:
    #     return False

    # Check caseInitiationDate > cutoff date
    case_initiation_dates = metadata.get("caseInitiationDate", [])
    if not case_initiation_dates:
        return False

    # Get the first date (assuming there's typically one)
    initiation_date_str = case_initiation_dates[0] if isinstance(
        case_initiation_dates, list) else case_initiation_dates
    initiation_date = parse_date(initiation_date_str)

    if initiation_date is None:
        return False

    if initiation_date < CUTOFF_DATE:
        return False

    return True


def filter_cases(data: Dict[str, Any]) -> Dict[str, Any]:
    """Filter cases based on the specified criteria."""
    print(
        f"\n🔍 Filtering cases with cutoff date: {CUTOFF_DATE.strftime('%Y-%m-%d')}")
    print(f"   Criteria:")
    print(f"   - caseInstrument: ['Merger']")
    print(f"   - caseInvestigationPhase: ['1']")
    print(f"   - caseCartel: ['Antitrust']")
    print(f"   - decisions: [] (empty)")
    print(f"   - caseInitiationDate > {CUTOFF_DATE.strftime('%Y-%m-%d')}")

    filtered_cases = {}
    total_cases = len(data)
    matched_count = 0

    for case_number, case_data in data.items():
        if matches_criteria(case_data):
            filtered_cases[case_number] = case_data
            matched_count += 1

            # Print match info
            metadata = case_data.get("metadata", {})
            initiation_date = metadata.get("caseInitiationDate", ["N/A"])[0]
            case_title = metadata.get("caseTitle", ["N/A"])[0]
            print(f"   ✅ {case_number}: {case_title} ({initiation_date})")

    print(
        f"\n📊 Filtered {matched_count} cases out of {total_cases} total cases")
    return filtered_cases


def save_filtered_data(filtered_cases: Dict[str, Any], output_path: str):
    """Save filtered cases to a JSON file."""
    print(f"\n💾 Saving filtered data to: {output_path}")
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(filtered_cases, f, indent=2, ensure_ascii=False)
        print(f"✅ Successfully saved {len(filtered_cases)} filtered cases")
    except Exception as e:
        print(f"❌ Error saving data: {e}")
        raise


def normalize_company(name: str) -> str:
    """Normalize company name for matching."""
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").replace(" corporation", "").replace(" corp.", "").replace(" ag", "").replace(" sa", "").replace(" nv", "").strip()


def get_deals_from_mongodb(include_ec_cases=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_ec_cases: If False, only return deals that don't have an 'ec_cases' node

    Returns:
        List of deal dictionaries
    """
    try:
        # Use global MongoDB connection
        collection = get_deals_collection()

        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Build query - exclude deals with 'ec_cases' node if include_ec_cases is False
        query = {}
        if not include_ec_cases:
            query = {"ec_cases": {"$exists": False}}

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'ec_cases' node" if not include_ec_cases else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_ec_cases=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_ec_cases: If False, only load deals that don't have an 'ec_cases' node
    """
    global deals
    deals = get_deals_from_mongodb(include_ec_cases=include_ec_cases)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


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


def match_deal_with_llm(case_companies: List[str], all_companies: set) -> str:
    """Match case companies with deals using LLM."""
    # Join case companies into a single string
    companies_str = " / ".join(case_companies)

    prompt = f"""
You are an M&A deal analyst. Given the company names from an EC merger case, determine whether any of these companies match any of the companies listed below.

- Match only if the company name or a well-known alias appears in the case companies.
- Ignore similar-sounding names or partial matches.
- Accept suffix variations (Inc., Ltd., PLC, AG, SA, NV, Corporation, Corp.).
- The case companies may be separated by "/" or " / ".

Case Companies:
{companies_str}

Deal Companies:
{', '.join(sorted(all_companies))}

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


def format_date(date_str: str) -> str:
    """Format date from YYYY-MM-DD to DD.MM.YYYY"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except:
        return date_str


def parse_json_field(field_value: str) -> Dict[str, Any]:
    """Parse JSON string field."""
    try:
        if isinstance(field_value, str) and field_value.startswith("{"):
            return json.loads(field_value)
        return {}
    except:
        return {}


def get_oj_prior_publication(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Get OJ prior publication info from decisions where decisionOjPriorPublication is true."""
    for decision in decisions:
        metadata = decision.get("metadata", {})
        oj_prior = metadata.get("decisionOjPriorPublication", [])
        if oj_prior and len(oj_prior) > 0 and str(oj_prior[0]).lower() == "true":
            oj_pubs = metadata.get("decisionOfficialJournalPublications", [])
            if oj_pubs and len(oj_pubs) > 0:
                try:
                    pub_data = json.loads(oj_pubs[0]) if isinstance(
                        oj_pubs[0], str) else oj_pubs[0]
                    items = pub_data.get("items", [])
                    if items:
                        return {
                            "reference": items[0].get("reference", ""),
                            "publishedDate": items[0].get("publishedDate", ""),
                            "priorPublication": items[0].get("priorPublication", "true")
                        }
                except:
                    pass
            pub_dates = metadata.get(
                "decisionOfficialJournalPublicationsPublishedDates", [])
            if pub_dates and len(pub_dates) > 0:
                return {
                    "reference": "",
                    "publishedDate": pub_dates[0],
                    "priorPublication": "true"
                }
    return {}


def generate_ec_case_email_html(case_data: Dict[str, Any], deal_match: Dict[str, Any]) -> tuple:
    """
    Generate HTML email for EC case match using the same style as ec_case_update_monitor.py.

    Args:
        case_data: The EC case data dictionary
        deal_match: The matched deal object

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")

    # Extract case metadata
    metadata = case_data.get("metadata", {})
    case_num = metadata.get("caseNumber", ["N/A"])[0]
    case_title = metadata.get("caseTitle", ["N/A"])[0]
    case_instrument = metadata.get("caseInstrument", ["Merger"])[0]
    case_simplified = metadata.get("caseSimplified", [""])[0]
    case_companies = metadata.get("caseCompanies", [])
    last_decision_date = metadata.get("caseLastDecisionDate", [])
    case_regulation = metadata.get("caseRegulation", [])
    notification_date = metadata.get("caseNotificationDate", [])
    deadline_date = metadata.get("caseDeadlineDate", [])
    case_sectors = metadata.get("caseSectors", [])
    decisions = case_data.get("decisions", [])
    case_attachments = case_data.get("caseAttachments", [])

    # Build subject
    subject = f"EC Merger Case Match – {target} / {acquirer}"

    # Build HTML with inline styles (same as ec_case_update_monitor.py)
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC Case Match - {case_num}</title>
</head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #ef4444;color:#ef4444;background:#fff;">{case_instrument}</span>'''

    if case_simplified:
        html += f'<div style="margin-left:2px;font-size:14px;color:#6b7280;font-style:italic;">{case_simplified}</div>'

    html += f'''</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>
<div style="margin-top:18px;">'''

    # 1. Companies - ALWAYS SHOW
    companies_str = case_companies[0] if case_companies else ""
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Companies:</span> '
    if companies_str:
        company_list = [c.strip()
                        for c in companies_str.split("/") if c.strip()]
        for i, company in enumerate(company_list):
            html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=M&caseTitleOrCompanyName={company}" style="color:#2563eb;text-decoration:none;font-weight:700;">{company}</a>'
            if i < len(company_list) - 1:
                html += '<span style="color:#9ca3af;margin:0 8px;">|</span>'
    else:
        html += 'N/A'
    html += '</div>'

    # Case URL
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="https://competition-cases.ec.europa.eu/cases/{case_num}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">https://competition-cases.ec.europa.eu/cases/{case_num}</a><span style="color:#9ca3af;margin-left:6px;">↗</span>'
    html += '</div>'

    # 2. Last decision date - ALWAYS SHOW
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Last decision date:</span> '
    html += '<span style="font-weight:800;">'
    html += format_date(last_decision_date[0]) if last_decision_date else 'N/A'
    html += '</span></div>'

    # 3. Regulation - ALWAYS SHOW
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Regulation:</span> '
    html += case_regulation[0] if case_regulation else 'N/A'
    html += '</div>'

    # 4. Notification date - ALWAYS SHOW
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Notification date:</span> '
    html += format_date(notification_date[0]) if notification_date else 'N/A'
    html += '</div>'

    # 5. Provisional deadline - ALWAYS SHOW
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Provisional deadline:</span> '
    html += format_date(deadline_date[0]) if deadline_date else 'N/A'
    html += '</div>'

    # 6. Economic activities - ALWAYS SHOW
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Economic activities:</span> '
    if case_sectors:
        for sector_str in case_sectors:
            sector_data = parse_json_field(sector_str)
            if sector_data:
                code = sector_data.get("code", "")
                label = sector_data.get("label", "")
                if code and label:
                    # Transform code from "NaceV2Sector_M_68.2" to "*M_68.2"
                    sector_code = code.replace("NaceV2Sector_", "*")
                    html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=M&caseSectors={sector_code}&sortField=caseLastDecisionDate&sortOrder=DESC" style="color:#2563eb;text-decoration:none;font-weight:700;">{label}</a>'
                    html += f'<span style="color:#6b7280;"> (NACE Rev. 2.1)</span>'
    else:
        html += 'N/A'
    html += '</div>'

    # Prior publication in OJ - SHOW IF AVAILABLE
    oj_info = get_oj_prior_publication(decisions)

    if oj_info:
        html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
        html += '<span style="color:#6b7280;">Prior publication in OJ:</span> '
        if oj_info.get("reference"):
            ref = oj_info["reference"]
            ref_number = ref.replace("C", "").replace("c", "")
            year = oj_info["publishedDate"][:4] if oj_info.get(
                "publishedDate") else ""
            if ref_number and year:
                html += f'<a href="https://eur-lex.europa.eu/eli/C/{year}/{ref_number}/oj" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">OJEU {ref}</a>'
                html += f'<span style="color:#9ca3af;margin:0 6px;">↗</span>'
            else:
                html += f'OJEU {ref}'
        if oj_info.get("publishedDate"):
            html += f'<span style="color:#111827;"> of {format_date(oj_info["publishedDate"])}</span>'
        html += '</div>'

    html += '</div></div>'

    # Decisions section - ALWAYS SHOW if there are decisions
    if decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += f'<div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Decisions</div>'

        for decision in decisions:
            decision_metadata = decision.get("metadata", {})
            decision_types = decision_metadata.get("decisionTypes", [])
            decision_adoption_date = decision_metadata.get(
                "decisionAdoptionDate", [])

            if decision_types or decision_adoption_date:
                html += '<div style="padding:14px 0;"><div style="font-size:14px;color:#111827;">'

                if decision_types:
                    decision_type_data = parse_json_field(decision_types[0])
                    if decision_type_data:
                        html += f'<span style="font-weight:900;">{decision_type_data.get("label", "")}</span>'

                if decision_adoption_date:
                    html += f'<span style="color:#6b7280;"> of {format_date(decision_adoption_date[0])}</span>'

                html += '</div>'

                # Decision details
                decision_attachments = decision.get("decisionAttachments", [])
                press_releases = decision_metadata.get(
                    "decisionPressReleases", [])

                if decision_attachments or press_releases:
                    html += '<div style="margin-top:10px;">'

                    if decision_attachments:
                        html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                        html += '<span style="color:#6b7280;">Decision text(s):</span> '

                        for idx, attachment in enumerate(decision_attachments):
                            att_metadata = attachment.get("metadata", {})
                            att_link = att_metadata.get(
                                "attachmentLink", [""])[0]
                            att_lang = att_metadata.get(
                                "attachmentLanguage", ["EN"])[0]
                            att_pub_date = att_metadata.get(
                                "attachmentPublicationBusinessDate", [""])[0]

                            if att_link:
                                html += '<span style="display:inline-flex;align-items:center;gap:6px;margin-right:12px;">'
                                html += '<span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border:1px solid #ef4444;border-radius:3px;color:#ef4444;font-size:9px;font-weight:900;">PDF</span>'
                                html += f'<a href="{att_link}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{att_lang}</a>'
                                if att_pub_date:
                                    html += f'<span style="color:#6b7280;font-size:13px;">published on {format_date(att_pub_date)}</span>'
                                html += '</span>'

                        html += '</div>'

                    if press_releases:
                        html += '<div style="font-size:14px;color:#111827;">'
                        html += '<span style="color:#6b7280;">Press communication:</span> '
                        try:
                            pr_data = json.loads(press_releases[0]) if isinstance(
                                press_releases[0], str) else press_releases[0]
                            items = pr_data.get("items", [])
                            for idx, item in enumerate(items):
                                ref = item.get("reference", "")
                                if idx > 0:
                                    html += ' '
                                html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={ref}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{ref}</a>'
                                html += '<span style="color:#9ca3af;margin-left:4px;">↗</span>'
                        except:
                            pass
                        html += '</div>'

                    html += '</div>'
                html += '</div>'

        html += '</div>'

    # Other case related information - ALWAYS SHOW if there are attachments
    if case_attachments:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += f'<div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Other case related information</div>'

        for attachment in case_attachments:
            att_metadata = attachment.get("metadata", {})
            att_category = att_metadata.get("attachmentCategory", [""])[0]
            att_sent_date = att_metadata.get("attachmentSentDate", [""])[0]
            att_link = att_metadata.get("attachmentLink", [""])[0]
            att_pub_date = att_metadata.get(
                "attachmentPublicationBusinessDate", [""])[0]
            att_lang = att_metadata.get("attachmentLanguage", ["EN"])[0]

            if att_category:
                html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                html += '<span style="color:#6b7280;">'
                html += att_category
                if att_sent_date:
                    html += f' of {format_date(att_sent_date)}'
                html += ':</span> '

                if att_link:
                    html += '<span style="display:inline-flex;align-items:center;gap:6px;">'
                    html += '<span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border:1px solid #ef4444;border-radius:3px;color:#ef4444;font-size:9px;font-weight:900;">PDF</span>'
                    html += f'<a href="{att_link}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{att_lang}</a>'
                    if att_pub_date:
                        html += f'<span style="color:#6b7280;font-size:13px;">published on {format_date(att_pub_date)}</span>'
                    html += '</span>'
                html += '</div>'

        html += '</div>'

    html += '''</div>
</body>
</html>'''

    return subject, html


def send_ec_case_email_via_webhook(case_data: Dict[str, Any], deal_match: Dict[str, Any]) -> bool:
    """
    Send email notification via n8n webhook after saving EC case data.

    Args:
        case_data: The EC case data dictionary
        deal_match: The matched deal object

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_ec_case_email_html(
            case_data, deal_match)
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

        # Extract case metadata
        metadata = case_data.get("metadata", {})
        case_number = metadata.get("caseNumber", ["N/A"])[0]
        case_title = metadata.get("caseTitle", ["N/A"])[0]

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


def save_ec_case_to_deal(deal_match, case_data, matched_company, matched_role):
    """
    Save matched EC case to MongoDB deal record under 'ec_cases' array.

    Args:
        deal_match: The matched deal object (must have deal_id to identify)
        case_data: The case data to save
        matched_company: The matched company name
        matched_role: The matched role (acquirer or target)
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

        # Create a copy of the case data and add matching metadata
        case_record = json.loads(json.dumps(case_data))  # Deep copy
        case_record["matched_company"] = matched_company
        case_record["matched_role"] = matched_role

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
        metadata = case_data.get("metadata", {})
        case_number = metadata.get("caseNumber", [None])[0]

        # Check if case_number already exists in this deal's ec_cases
        existing_deal = collection.find_one(query)
        if existing_deal and "ec_cases" in existing_deal:
            for existing_case in existing_deal["ec_cases"]:
                existing_metadata = existing_case.get("metadata", {})
                existing_case_number = existing_metadata.get(
                    "caseNumber", [None])[0]
                if existing_case_number == case_number:
                    print(
                        f"   ⏩ Skipped (case {case_number} already exists in deal)")
                    return False

        # Update the deal document by adding to ec_cases array
        update_result = collection.update_one(
            query,
            {
                "$push": {
                    "ec_cases": case_record
                }
            }
        )

        if update_result.modified_count > 0:
            print(f"   ✅ Saved EC case to deal record in MongoDB")

            # Send email notification via n8n webhook
            try:
                send_ec_case_email_via_webhook(case_data, deal_match)
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


def match_cases_with_deals(filtered_cases: Dict[str, Any], deals: List[Dict[str, Any]]) -> int:
    """
    Match filtered cases with deals using LLM.
    Adds matched cases to the corresponding deal objects and saves to MongoDB.
    """
    print(f"\n{'='*60}")
    print(f"🔍 Matching {len(filtered_cases)} cases with deals...")
    print(f"{'='*60}\n")

    # Normalize company names from deals
    all_companies = set()
    for deal in deals:
        # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")
        target = deal.get("target") or deal.get("target_name", "")
        if acquirer:
            all_companies.add(normalize_company(acquirer))
        if target:
            all_companies.add(normalize_company(target))

    matched_count = 0

    for idx, (case_number, case_data) in enumerate(filtered_cases.items(), 1):
        metadata = case_data.get("metadata", {})
        case_companies = metadata.get("caseCompanies", [])
        case_title = metadata.get("caseTitle", ["N/A"])[0]
        initiation_date = metadata.get("caseInitiationDate", ["N/A"])[0]

        if not case_companies:
            print(f"[{idx}/{len(filtered_cases)}] {case_number}: No companies found")
            continue

        companies_str = " / ".join(case_companies)
        print(
            f"[{idx}/{len(filtered_cases)}] {case_number}: {case_title} ({initiation_date})")
        print(f"   Companies: {companies_str}")

        # Match with LLM
        match_result = match_deal_with_llm(case_companies, all_companies)

        if match_result and match_result.lower() != "none" and "match:" in match_result.lower():
            # Extract company name and role from match result
            try:
                match_parts = match_result.replace(
                    "Match:", "").replace("match:", "").strip()
                if "(" in match_parts:
                    matched_company = match_parts.split("(")[0].strip().lower()
                    role_part = match_parts.split(
                        "(")[1].replace(")", "").strip().lower()
                else:
                    matched_company = match_parts.lower()
                    role_part = ""

                print(f"   🎯 Match found: {matched_company} ({role_part})")

                # Find the deal and add the case
                for deal in deals:
                    # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
                    acquirer = deal.get("acquirer") or deal.get(
                        "acquire_name", "")
                    target = deal.get("target") or deal.get("target_name", "")

                    normalized_acquirer = normalize_company(
                        acquirer) if acquirer else ""
                    normalized_target = normalize_company(
                        target) if target else ""

                    if normalized_acquirer == matched_company or normalized_target == matched_company:
                        # Determine matched role
                        if normalized_acquirer == matched_company:
                            matched_role = "acquirer"
                        else:
                            matched_role = "target"

                        # Initialize ec_cases if not exists (for local list)
                        if "ec_cases" not in deal:
                            deal["ec_cases"] = []

                        # Check if case_number already exists in this deal's ec_cases (local check)
                        existing_case_numbers = []
                        for existing_case in deal["ec_cases"]:
                            existing_metadata = existing_case.get(
                                "metadata", {})
                            existing_case_number = existing_metadata.get(
                                "caseNumber", [None])[0]
                            if existing_case_number:
                                existing_case_numbers.append(
                                    existing_case_number)

                        if case_number in existing_case_numbers:
                            print(
                                f"   ⏩ Skipped (case {case_number} already exists in deal)")
                            break

                        # Create a copy of the full case data and add matching metadata (for local list)
                        case_record = json.loads(
                            json.dumps(case_data))  # Deep copy
                        case_record["matched_company"] = matched_company
                        case_record["matched_role"] = matched_role

                        deal["ec_cases"].append(case_record)

                        # Save to MongoDB
                        save_result = save_ec_case_to_deal(
                            deal, case_data, matched_company, matched_role)
                        if save_result:
                            print(
                                f"   ✅ Added to deal: {acquirer} / {target}")
                            matched_count += 1
                        else:
                            print(
                                f"   ⚠️ Failed to save to MongoDB, but added to local list")
                            matched_count += 1
                        break
            except Exception as e:
                print(f"   ⚠️ Error processing match: {e}")
                import traceback
                traceback.print_exc()
        else:
            print(f"   ➖ No match")

    print(f"\n{'='*60}")
    print(f"✅ Matching complete: {matched_count} cases matched with deals")
    print(f"{'='*60}\n")

    return matched_count


def main():
    """Main function to download, filter, match with deals, and save data."""
    print("🚀 Starting EC Case Filter Script\n")

    try:
        # Initialize MongoDB connection
        print("🔌 Initializing MongoDB connection...")
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            print(f"❌ {message}")
            print("   MongoDB connection is required. Exiting.")
            return {
                "success": False,
                "error": message,
                "total_filtered": 0,
                "total_matched": 0
            }

        print(f"✅ {message}\n")

        # Load deals from MongoDB
        # Only load deals without 'ec_cases' node to avoid re-processing
        print("📊 Loading deals from MongoDB (excluding deals with 'ec_cases' node)...")
        load_deals(include_ec_cases=False)

        if not deals:
            print("⚠️ No deals found in MongoDB. Exiting.")
            return {
                "success": False,
                "error": "No deals found in MongoDB",
                "total_filtered": 0,
                "total_matched": 0
            }

        # Download JSON data
        data = download_json(DATA_URL)

        # Load JSON data from local file (temporary)
        # data = load_json_from_file(LOCAL_DATA_PATH)

        # Filter cases
        filtered_cases = filter_cases(data)

        # Save filtered data
        save_filtered_data(filtered_cases, OUTPUT_PATH)

        # Match cases with deals
        matched_count = match_cases_with_deals(filtered_cases, deals)

        # Save matched deals to JSON file as backup (only if we have deals loaded)
        if deals:
            print(
                f"\n💾 Saving matched deals to JSON backup: {MATCHED_DEALS_OUTPUT}")
            try:
                # Convert datetime objects to strings for JSON serialization
                deals_serializable = convert_datetime_to_string(deals)
                with open(MATCHED_DEALS_OUTPUT, 'w', encoding='utf-8') as f:
                    json.dump(deals_serializable, f,
                              indent=2, ensure_ascii=False)
                print(
                    f"✅ Successfully saved deals with {matched_count} matched cases to JSON backup")
            except Exception as e:
                print(f"⚠️ Error saving matched deals to JSON: {e}")

        print(f"\n🎉 Done!")
        print(f"   📁 Filtered cases → {OUTPUT_PATH}")
        if is_connected():
            print(f"   💾 Matched cases saved to MongoDB deals collection")
        print(f"   📁 Matched deals (JSON backup) → {MATCHED_DEALS_OUTPUT}")

        return {
            "success": True,
            "total_filtered": len(filtered_cases),
            "total_matched": matched_count,
            "filtered_cases_file": OUTPUT_PATH,
            "matched_deals_file": MATCHED_DEALS_OUTPUT
        }

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "total_filtered": 0,
            "total_matched": 0
        }


if __name__ == "__main__":
    main()
