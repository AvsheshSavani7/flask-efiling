"""
Foreign Subsidies (FS) case filter: download FS case data, filter by caseInstrument + empty decisions + cutoff date,
match cases to deals via LLM (company names from caseTitle), save to MongoDB fs_ec_cases, send email via n8n.
"""
import json
import requests
from datetime import datetime, date
from typing import Dict, Any, List
import os
from dotenv import load_dotenv
from openai import OpenAI
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection
from html import escape


def escape_html(s: Any) -> str:
    """Safe HTML escape for template output."""
    return "" if s is None else escape(str(s))


# Constants
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-FS.json"
CUTOFF_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.strptime("2026-01-01", "%Y-%m-%d")
OUTPUT_PATH = "fs_filtered_cases.json"
MATCHED_DEALS_OUTPUT = "fs_matched_deals.json"
ENV_PATH = ".env"
CASE_BASE_URL = "https://competition-cases.ec.europa.eu/cases"

# Load OpenAI API Key
load_dotenv(ENV_PATH)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

deals: List[Dict[str, Any]] = []


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


def normalize_fs_data(data: Any) -> Dict[str, Any]:
    """
    Normalize FS JSON to a single dict keyed by case number.
    Handles:
      - Array of objects: [ { "FS.100081": {...}, "FS.100068": {...} }, { "FS.100143": {...} }, ... ]
      - Single flat object: { "FS.100081": {...}, ... }
      - Wrapped response: { "data": [...] } or { "cases": {...} } (unwraps then normalizes)
    """
    if data is None:
        return {}
    if isinstance(data, list):
        out = {}
        for item in data:
            if isinstance(item, dict):
                for case_number, case_data in item.items():
                    out[case_number] = case_data
        return out
    if isinstance(data, dict):
        for wrapper in ("data", "cases", "results", "case-data-FS"):
            if wrapper in data:
                return normalize_fs_data(data[wrapper])
        return data
    return {}


def parse_date(date_str: str) -> datetime:
    """Parse date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def matches_criteria(case_data: Dict[str, Any]) -> bool:
    """
    Filter: caseInstrument contains "Foreign Subsidies", decisions == [], caseInitiationDate >= CUTOFF_DATE.
    """
    metadata = case_data.get("metadata", {})

    case_instrument = metadata.get("caseInstrument", [])
    if "Foreign Subsidies" not in case_instrument:
        return False

    decisions = case_data.get("decisions", [])
    if decisions:
        return False

    case_initiation_dates = metadata.get("caseInitiationDate", [])
    if not case_initiation_dates:
        return False

    initiation_date_str = case_initiation_dates[0] if isinstance(
        case_initiation_dates, list) else case_initiation_dates
    initiation_date = parse_date(initiation_date_str)
    if initiation_date is None or initiation_date < CUTOFF_DATE:
        return False

    return True


def filter_cases(data: Dict[str, Any]) -> Dict[str, Any]:
    """Filter cases by FS criteria."""
    print(
        f"\n🔍 Filtering FS cases with cutoff date: {CUTOFF_DATE.strftime('%Y-%m-%d')}")
    print("   Criteria: caseInstrument 'Foreign Subsidies', decisions == [], caseInitiationDate >= cutoff")

    filtered_cases = {}
    total_cases = len(data)
    matched_count = 0

    for case_number, case_data in data.items():
        if matches_criteria(case_data):
            filtered_cases[case_number] = case_data
            matched_count += 1
            metadata = case_data.get("metadata", {})
            initiation_date = metadata.get("caseInitiationDate", ["N/A"])[0]
            case_title = metadata.get("caseTitle", ["N/A"])[0]
            print(f"   ✅ {case_number}: {case_title} ({initiation_date})")

    print(
        f"\n📊 Filtered {matched_count} cases out of {total_cases} total cases")
    return filtered_cases


def save_filtered_data(filtered_cases: Dict[str, Any], output_path: str) -> None:
    """Save filtered cases to a JSON file."""
    print(f"\n💾 Saving filtered data to: {output_path}")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(filtered_cases, f, indent=2, ensure_ascii=False)
    print(f"✅ Successfully saved {len(filtered_cases)} filtered cases")


def get_companies_from_case_title(case_data: Dict[str, Any]) -> List[str]:
    """Build company list from caseTitle only, split by ' / '."""
    metadata = case_data.get("metadata", {})
    case_title_list = metadata.get("caseTitle", [])
    title = case_title_list[0] if case_title_list else ""
    if not title or not isinstance(title, str):
        return []
    return [c.strip() for c in title.split(" / ") if c.strip()]


def get_deals_from_mongodb(include_fs_ec_cases: bool = False) -> List[Dict[str, Any]]:
    """
    Fetch deals from MongoDB. If include_fs_ec_cases is False, only return deals
    that do not have a non-empty fs_ec_cases array (so we don't re-process).
    """
    try:
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

        # Optionally also exclude deals with existing 'fs_ec_cases'
        if not include_fs_ec_cases:
            fs_filter = {
                "$or": [
                    {"fs_ec_cases": {"$exists": False}},
                    {"fs_ec_cases": None},
                    {"fs_ec_cases": []},
                ]
            }
            query = {"$and": [status_filter, fs_filter]}
        else:
            query = status_filter

        all_deals = list(collection.find(query))
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'fs_ec_cases'" if not include_fs_ec_cases else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_fs_ec_cases: bool = False) -> List[Dict[str, Any]]:
    """Load deals from MongoDB into global list."""
    global deals
    deals = get_deals_from_mongodb(include_fs_ec_cases=include_fs_ec_cases)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def convert_datetime_to_string(obj: Any) -> Any:
    """Recursively convert datetime/date to string for JSON."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: convert_datetime_to_string(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_datetime_to_string(item) for item in obj]
    return obj


def match_deal_with_llm(case_companies: List[str], deals_list: List[Dict[str, Any]]) -> str:
    """Match case companies (from caseTitle) with deals using LLM. Returns Match: DEAL_ID|COMPANY|(target|acquirer) or None."""
    companies_str = " / ".join(case_companies)

    deals_list_serialized = []
    for deal in deals_list:
        deal_info = {
            "deal_id": deal.get("deal_id", ""),
            "target": deal.get("target") or deal.get("target_name", ""),
            "acquirer": deal.get("acquirer") or deal.get("acquire_name", ""),
        }
        for k in ["target_aliases", "parent_aliases"]:
            v = deal.get(k) or []
            if isinstance(v, list) and v:
                deal_info[k] = v
        if deal_info.get("target") or deal_info.get("acquirer"):
            deals_list_serialized.append(deal_info)

    if not deals_list_serialized:
        return "None"

    lines = []
    for d in deals_list_serialized:
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target', 'N/A')} | Acquirer: {d.get('acquirer', 'N/A')}"
        for key in ["target_aliases", "parent_aliases"]:
            if d.get(key):
                line += f" | {key}: {', '.join(str(a) for a in d[key])}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""
You are an M&A deal analyst. Given the company names from an EC Foreign Subsidies case (case title), determine whether any match any of the deals below.

DEALS TO MATCH:
{deals_text}

CASE COMPANIES (from case title):
{companies_str}

INSTRUCTIONS:
1. Compare the case companies with BOTH Target and Acquirer (and target_aliases, parent_aliases if present).
2. Match only if the company name or a well-known alias appears in the case companies.
3. Accept exact, partial, or suffix variations (Inc., Ltd., PLC, AG, SA, NV, Corporation, Corp.).
4. Case companies may be separated by " / ".

RESPONSE FORMAT:
- If match: respond EXACTLY: Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
- If no match: None
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition. Return Match: DEAL_ID|COMPANY|target|acquirer or None."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        result = response.choices[0].message.content.strip()
        print(f"   🧠 LLM Response: {result}")
        return result
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return "None"


def format_date(date_str: str) -> str:
    """Format YYYY-MM-DD to DD.MM.YYYY."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return date_str


def parse_json_field(field_value: Any) -> Dict[str, Any]:
    """Parse JSON string field."""
    try:
        if isinstance(field_value, str) and field_value.strip().startswith("{"):
            return json.loads(field_value)
        return {}
    except Exception:
        return {}


def generate_fs_case_email_html(case_data: Dict[str, Any], deal_match: Dict[str, Any]) -> tuple:
    """Generate HTML email for FS case match. Case URL: competition-cases.ec.europa.eu/cases/{case_number}."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")

    metadata = case_data.get("metadata", {})
    case_num = metadata.get("caseNumber", ["N/A"])[0]
    case_title = metadata.get("caseTitle", ["N/A"])[0]
    case_instrument = (metadata.get("caseInstrument")
                       or ["Foreign Subsidies"])[0]
    last_decision_date = metadata.get("caseLastDecisionDate", [])
    case_regulation = metadata.get("caseRegulation", [])
    notification_date = metadata.get("caseNotificationDate", [])
    deadline_date = metadata.get("caseDeadlineDate", [])
    case_sectors = metadata.get("caseSectors", [])
    case_attachments = case_data.get("caseAttachments", [])
    decisions = case_data.get("decisions", [])

    subject = f"FRMD: EC Foreign Subsidies Case (New) – {target} / {acquirer}"

    company_list = get_companies_from_case_title(case_data)

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>EC FS Case - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #059669;color:#059669;background:#fff;">{case_instrument}</span>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #2563eb;color:#2563eb;background:#fff;">FS</span>
</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>
<div style="margin-top:18px;">'''

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Companies (case title):</span> '
    if company_list:
        for i, company in enumerate(company_list):
            html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS&caseTitleOrCompanyName={company}" style="color:#2563eb;text-decoration:none;font-weight:700;">{escape_html(company)}</a>'
            if i < len(company_list) - 1:
                html += '<span style="color:#9ca3af;margin:0 8px;">|</span>'
    else:
        html += 'N/A'
    html += '</div>'

    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="{CASE_BASE_URL}/{case_num}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{CASE_BASE_URL}/{case_num}</a><span style="color:#9ca3af;margin-left:6px;">↗</span></div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Last decision date:</span> '
    html += format_date(last_decision_date[0]) if last_decision_date else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Regulation:</span> '
    if case_regulation:
        reg_data = parse_json_field(case_regulation[0])
        reg_label = reg_data.get(
            "label", case_regulation[0]) if reg_data else case_regulation[0]
        html += escape_html(reg_label) + '</div>'
    else:
        html += 'N/A</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Notification date:</span> '
    html += (format_date(notification_date[0])
             if notification_date else 'N/A') + '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Provisional deadline:</span> '
    html += (format_date(deadline_date[0])
             if deadline_date else 'N/A') + '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Economic activities:</span> '
    if case_sectors:
        for sector_str in case_sectors:
            sector_data = parse_json_field(sector_str)
            if sector_data:
                code = sector_data.get("code", "")
                label = sector_data.get("label", "")
                if code and label:
                    sector_code = code.replace(
                        "NaceV2Sector_", "*").replace("NaceSectors", "*")
                    html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS&caseSectors={sector_code}&sortField=caseLastDecisionDate&sortOrder=DESC" style="color:#2563eb;text-decoration:none;font-weight:700;">{escape_html(label)}</a> '
    else:
        html += 'N/A'
    html += '</div>'

    if decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div><div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Decisions</div>'
        for decision in decisions:
            dmeta = decision.get("metadata", {})
            dtypes = dmeta.get("decisionTypes", [])
            dadopt = dmeta.get("decisionAdoptionDate", [])
            if dtypes or dadopt:
                html += '<div style="padding:14px 0;"><div style="font-size:14px;color:#111827;">'
                if dtypes:
                    ddata = parse_json_field(dtypes[0])
                    if ddata:
                        html += f'<span style="font-weight:900;">{escape_html(ddata.get("label", ""))}</span>'
                if dadopt:
                    html += f'<span style="color:#6b7280;"> of {format_date(dadopt[0])}</span>'
                html += '</div></div>'
        html += '</div>'

    if case_attachments:
        html += '<div style="height:1px;background:#e5e7eb;"></div><div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Other case related information</div>'
        for att in case_attachments:
            ameta = att.get("metadata", {})
            cat = ameta.get("attachmentCategory", [""])[0]
            sent = ameta.get("attachmentSentDate", [""])[0]
            link = ameta.get("attachmentLink", [""])[0]
            pub = ameta.get("attachmentPublicationBusinessDate", [""])[0]
            lang = ameta.get("attachmentLanguage", ["EN"])[0]
            if cat:
                html += f'<div style="font-size:14px;color:#111827;margin-bottom:10px;"><span style="color:#6b7280;">{escape_html(cat)}'
                if sent:
                    html += f' of {format_date(sent)}'
                html += ':</span> '
                if link:
                    html += f'<a href="{escape_html(link)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{escape_html(lang)}</a>'
                    if pub:
                        html += f' <span style="color:#6b7280;font-size:13px;">published on {format_date(pub)}</span>'
                html += '</div>'
        html += '</div>'

    html += '</div></div></body></html>'
    return subject, html


def send_fs_case_email_via_webhook(case_data: Dict[str, Any], deal_match: Dict[str, Any]) -> bool:
    """Send FS case match email via n8n webhook."""
    try:
        subject, html_email = generate_fs_case_email_html(
            case_data, deal_match)
        print(f"📝 Generated email subject: {subject}")

        # webhook_url = os.getenv(
        #     "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        metadata = case_data.get("metadata", {})
        case_number = metadata.get("caseNumber", ["N/A"])[0]
        case_title = metadata.get("caseTitle", ["N/A"])[0]

        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': deal_match.get("deal_id", "N/A"),
            'target': deal_match.get("target") or deal_match.get("target_name", "N/A"),
            'acquirer': deal_match.get("acquirer") or deal_match.get("acquire_name", "N/A"),
            'case_number': case_number,
            'case_title': case_title,
            'case_instrument': 'FS',
        }

        response = requests.post(webhook_url, json=payload, headers={
                                 'Content-Type': 'application/json'}, timeout=30)
        response.raise_for_status()
        print(
            f"✅ Email sent successfully via n8n webhook! Status: {response.status_code}")
        return True
    except Exception as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_fs_case_to_deal(deal_match: Dict[str, Any], case_data: Dict[str, Any], matched_company: str, matched_role: str) -> bool:
    """Save matched FS case to MongoDB deal record under fs_ec_cases array."""
    try:
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save to MongoDB")
            return False

        collection = get_deals_collection()
        if collection is None:
            return False

        case_record = json.loads(json.dumps(case_data))
        case_record["matched_company"] = matched_company
        case_record["matched_role"] = matched_role

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
                or_conditions.extend(
                    [{"acquirer": acquirer}, {"acquire_name": acquirer}])
            if target:
                or_conditions.extend(
                    [{"target": target}, {"target_name": target}])
            if or_conditions:
                query = {"$or": or_conditions}

        if not query:
            print("⚠️ Cannot identify deal, skipping MongoDB save")
            return False

        metadata = case_data.get("metadata", {})
        case_number = metadata.get("caseNumber", [None])[0]

        existing_deal = collection.find_one(query)
        if existing_deal and existing_deal.get("fs_ec_cases"):
            for existing_case in existing_deal["fs_ec_cases"]:
                emeta = existing_case.get("metadata", {})
                if emeta.get("caseNumber", [None])[0] == case_number:
                    print(f"   ⏩ Skipped (case {case_number} already in deal)")
                    return False

        update_result = collection.update_one(
            query,
            {"$push": {"fs_ec_cases": case_record}}
        )

        if update_result.modified_count > 0:
            print("   ✅ Saved FS case to deal record in MongoDB (fs_ec_cases)")
            try:
                send_fs_case_email_via_webhook(case_data, deal_match)
            except Exception as e:
                print(f"   ⚠️ Error sending email: {e}")
            return True
        elif update_result.matched_count > 0:
            print("   ℹ️ Deal found but no changes made")
            return True
        else:
            print("   ⚠️ Deal not found in MongoDB")
            return False
    except Exception as e:
        print(f"❌ Error saving to MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return False


def match_cases_with_deals(filtered_cases: Dict[str, Any], deals_list: List[Dict[str, Any]]) -> int:
    """Match filtered FS cases with deals using LLM (companies from caseTitle). Save to fs_ec_cases and send email."""
    print(
        f"\n{'='*60}\n🔍 Matching {len(filtered_cases)} FS cases with deals...\n{'='*60}\n")

    deal_by_id = {str(d.get("deal_id", ""))                  : d for d in deals_list if d.get("deal_id")}
    matched_count = 0

    for idx, (case_number, case_data) in enumerate(filtered_cases.items(), 1):
        case_companies = get_companies_from_case_title(case_data)
        metadata = case_data.get("metadata", {})
        case_title = metadata.get("caseTitle", ["N/A"])[0]
        initiation_date = metadata.get("caseInitiationDate", ["N/A"])[0]

        if not case_companies:
            print(
                f"[{idx}/{len(filtered_cases)}] {case_number}: No companies from caseTitle")
            continue

        companies_str = " / ".join(case_companies)
        print(
            f"[{idx}/{len(filtered_cases)}] {case_number}: {case_title} ({initiation_date})")
        print(f"   Companies (case title): {companies_str}")

        match_result = match_deal_with_llm(case_companies, deals_list)

        deal_match = None
        matched_company = ""
        matched_role = ""
        if match_result and str(match_result).strip().lower() != "none":
            stripped = str(match_result).strip()
            if stripped.lower().startswith("match:"):
                parts = stripped[6:].strip().split("|")
                if len(parts) >= 3:
                    llm_deal_id = parts[0].strip()
                    matched_company = parts[1].strip()
                    role_raw = parts[2].strip().lower().replace(
                        "(", "").replace(")", "")
                    matched_role = role_raw if role_raw in (
                        "target", "acquirer") else "acquirer"
                    if llm_deal_id in deal_by_id:
                        deal_match = deal_by_id[llm_deal_id]

        if deal_match and matched_company and matched_role:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name", "")
            target = deal_match.get(
                "target") or deal_match.get("target_name", "")
            print(f"   🎯 Match: {matched_company} ({matched_role})")

            if "fs_ec_cases" not in deal_match:
                deal_match["fs_ec_cases"] = []

            existing_case_numbers = [
                c.get("metadata", {}).get("caseNumber", [None])[0]
                for c in deal_match["fs_ec_cases"]
                if c.get("metadata", {}).get("caseNumber")
            ]
            if case_number in existing_case_numbers:
                print(f"   ⏩ Skipped (case {case_number} already in deal)")
            else:
                case_record = json.loads(json.dumps(case_data))
                case_record["matched_company"] = matched_company
                case_record["matched_role"] = matched_role
                deal_match["fs_ec_cases"].append(case_record)

                if save_fs_case_to_deal(deal_match, case_data, matched_company, matched_role):
                    print(f"   ✅ Added to deal: {acquirer} / {target}")
                    matched_count += 1
                else:
                    deal_match["fs_ec_cases"].pop()

        else:
            print("   ❌ No match")

    print(f"\n{'='*60}\n✅ Matching complete: {matched_count} FS cases matched with deals\n{'='*60}\n")
    return matched_count


def main() -> Dict[str, Any]:
    """Download FS data, normalize, filter, match with deals, save to fs_ec_cases and email."""
    print("🚀 Starting FS (Foreign Subsidies) Case Filter Script\n")

    try:
        print("🔌 Initializing MongoDB connection...")
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            print(f"❌ {message}\n   MongoDB connection is required. Exiting.")
            return {"success": False, "error": message, "total_filtered": 0, "total_matched": 0}

        print(f"✅ {message}\n")

        print("📊 Loading deals from MongoDB (excluding deals with 'fs_ec_cases')...")
        load_deals(include_fs_ec_cases=False)

        if not deals:
            print("⚠️ No deals found in MongoDB. Exiting.")
            return {"success": False, "error": "No deals found in MongoDB", "total_filtered": 0, "total_matched": 0}

        raw = download_json(DATA_URL)
        data = normalize_fs_data(raw)
        if not data:
            print("⚠️ No case data after normalization. Exiting.")
            return {"success": False, "error": "No case data", "total_filtered": 0, "total_matched": 0}

        filtered_cases = filter_cases(data)
        save_filtered_data(filtered_cases, OUTPUT_PATH)

        matched_count = match_cases_with_deals(filtered_cases, deals)

        if deals:
            print(
                f"\n💾 Saving matched deals to JSON backup: {MATCHED_DEALS_OUTPUT}")
            try:
                serializable = convert_datetime_to_string(deals)
                with open(MATCHED_DEALS_OUTPUT, 'w', encoding='utf-8') as f:
                    json.dump(serializable, f, indent=2, ensure_ascii=False)
                print(
                    f"✅ Saved deals with {matched_count} matched FS cases to JSON backup")
            except Exception as e:
                print(f"⚠️ Error saving matched deals JSON: {e}")

        print("\n🎉 Done!")
        print(f"   📁 Filtered cases → {OUTPUT_PATH}")
        if is_connected():
            print("   💾 Matched cases saved to MongoDB (fs_ec_cases)")
        print(f"   📁 Matched deals backup → {MATCHED_DEALS_OUTPUT}")

        return {
            "success": True,
            "total_filtered": len(filtered_cases),
            "total_matched": matched_count,
            "filtered_cases_file": OUTPUT_PATH,
            "matched_deals_file": MATCHED_DEALS_OUTPUT,
        }
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e), "total_filtered": 0, "total_matched": 0}


if __name__ == "__main__":
    main()
