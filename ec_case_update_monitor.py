import json
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import os
from dotenv import load_dotenv
from bson import ObjectId
from ec_case_filter import load_json_from_file
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection

# Constants
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-M.json"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "ec_case_updates"
LOCAL_DATA_PATH = "/Users/joshuatackel/Downloads/case-data-M.json"


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


def get_deals_with_ec_cases() -> List[Dict[str, Any]]:
    """Fetch deals from MongoDB that have 'ec_cases' node."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available.")
            return []

        query = {"ec_cases": {"$exists": True, "$ne": [], "$type": "array"}}
        all_deals = list(collection.find(query))

        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        print(f"✅ Fetched {len(all_deals)} deals with ec_cases from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        return []


def normalize_for_comparison(data: Any) -> Any:
    """Normalize data for comparison."""
    if isinstance(data, dict):
        normalized = {}
        for k, v in data.items():
            if k in ["matched_company", "matched_role"]:
                continue
            normalized[k] = normalize_for_comparison(v)
        return normalized
    elif isinstance(data, list):
        normalized_list = [normalize_for_comparison(item) for item in data]
        try:
            if all(isinstance(item, (str, int, float)) for item in normalized_list):
                normalized_list = sorted(normalized_list)
        except:
            pass
        return normalized_list
    elif isinstance(data, str):
        return data.strip() if data else None
    else:
        return data


def deep_compare(old_data: Any, new_data: Any, path: str = "") -> List[Tuple[str, Any, Any]]:
    """Deeply compare two data structures."""
    differences = []
    old_normalized = normalize_for_comparison(old_data)
    new_normalized = normalize_for_comparison(new_data)

    if old_normalized is None and new_normalized is None:
        return differences
    if old_normalized is None:
        differences.append((path, None, new_data))
        return differences
    if new_normalized is None:
        differences.append((path, old_data, None))
        return differences

    if type(old_normalized) != type(new_normalized):
        differences.append((path, old_data, new_data))
        return differences

    if isinstance(old_normalized, dict):
        all_keys = set(old_normalized.keys()) | set(new_normalized.keys())
        for key in all_keys:
            new_path = f"{path}.{key}" if path else key
            if key not in old_normalized:
                differences.append((new_path, None, new_data.get(
                    key) if isinstance(new_data, dict) else None))
            elif key not in new_normalized:
                differences.append((new_path, old_data.get(
                    key) if isinstance(old_data, dict) else None, None))
            else:
                differences.extend(deep_compare(
                    old_normalized[key], new_normalized[key], new_path))
    elif isinstance(old_normalized, list):
        if len(old_normalized) != len(new_normalized):
            differences.append((path, old_data, new_data))
        else:
            try:
                old_set = set(old_normalized)
                new_set = set(new_normalized)
                if old_set != new_set:
                    for i, (old_item, new_item) in enumerate(zip(old_normalized, new_normalized)):
                        new_path = f"{path}[{i}]" if path else f"[{i}]"
                        differences.extend(deep_compare(
                            old_item, new_item, new_path))
            except (TypeError, ValueError):
                for i, (old_item, new_item) in enumerate(zip(old_normalized, new_normalized)):
                    new_path = f"{path}[{i}]" if path else f"[{i}]"
                    differences.extend(deep_compare(
                        old_item, new_item, new_path))
    else:
        if old_normalized != new_normalized:
            differences.append((path, old_data, new_data))

    return differences


def has_meaningful_changes(differences: List[Tuple[str, Any, Any]]) -> bool:
    """Check if there are meaningful changes."""
    if not differences:
        return False

    ignored_paths = ["matched_company", "matched_role"]

    for path, old_val, new_val in differences:
        if any(ignored in path for ignored in ignored_paths):
            continue
        return True

    return False


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


def get_oj_prior_publication(decisions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
    return None


def get_field_changed_status(field_path: str, differences: List[Tuple[str, Any, Any]]) -> str:
    """Check if a field was changed. Returns: 'updated', 'added', or 'unchanged'"""
    for diff_path, old_val, new_val in differences:
        if field_path in diff_path:
            if old_val is None:
                return 'added'
            elif new_val is None:
                return 'removed'
            else:
                return 'updated'
    return 'unchanged'


def generate_html_for_changes(case_number: str, old_case: Dict[str, Any], new_case: Dict[str, Any], differences: List[Tuple[str, Any, Any]]) -> str:
    """Generate HTML showing the changes between old and new case data."""
    old_metadata = old_case.get("metadata", {})
    new_metadata = new_case.get("metadata", {})

    case_num = new_metadata.get("caseNumber", [case_number])[0]
    case_title = new_metadata.get("caseTitle", ["N/A"])[0]
    case_instrument = new_metadata.get("caseInstrument", ["Merger"])[0]
    case_simplified = new_metadata.get("caseSimplified", [""])[0]

    # Determine which fields changed
    companies_changed = get_field_changed_status(
        "metadata.caseCompanies", differences)
    last_decision_changed = get_field_changed_status(
        "metadata.caseLastDecisionDate", differences)
    regulation_changed = get_field_changed_status(
        "metadata.caseRegulation", differences)
    notification_changed = get_field_changed_status(
        "metadata.caseNotificationDate", differences)
    deadline_changed = get_field_changed_status(
        "metadata.caseDeadlineDate", differences)
    sectors_changed = get_field_changed_status(
        "metadata.caseSectors", differences)

    changed_fields = []
    if companies_changed != 'unchanged':
        changed_fields.append("Companies")
    if last_decision_changed != 'unchanged':
        changed_fields.append("Last decision date")
    if regulation_changed != 'unchanged':
        changed_fields.append("Regulation")
    if notification_changed != 'unchanged':
        changed_fields.append("Notification date")
    if deadline_changed != 'unchanged':
        changed_fields.append("Provisional deadline")
    if sectors_changed != 'unchanged':
        changed_fields.append("Economic activities")

    decisions_changed = any("decisions" in diff[0] for diff in differences)
    attachments_changed = any(
        "caseAttachments" in diff[0] for diff in differences)

    if decisions_changed:
        changed_fields.append("Decisions")
    if attachments_changed:
        changed_fields.append("Other case related information")

    # Helper function for inline highlight styles
    def get_highlight_style(status):
        if status == 'updated':
            return 'background-color:#fef3c7;padding:3px 8px;border-radius:4px;border-left:3px solid #f59e0b;'
        elif status == 'added':
            return 'background-color:#d1fae5;padding:3px 8px;border-radius:4px;border-left:3px solid #10b981;'
        return ''

    def get_label_suffix(status):
        if status != 'unchanged':
            return ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;margin-left:4px;">(Updated)</span>'
        return ''

    # Build HTML with inline styles
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC Case Update - {case_num}</title>
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

    # 6 Required fields - ALWAYS SHOW (Single-row format)
    # 1. Companies
    case_companies = new_metadata.get("caseCompanies", [])
    companies_str = case_companies[0] if case_companies else ""

    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(companies_changed)}">'
    html += f'<span style="color:#6b7280;">Companies{get_label_suffix(companies_changed)}:</span> '
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

    # 2. Last decision date
    last_decision_date = new_metadata.get("caseLastDecisionDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(last_decision_changed)}">'
    html += f'<span style="color:#6b7280;">Last decision date{get_label_suffix(last_decision_changed)}:</span> '
    html += '<span style="font-weight:800;">'
    html += format_date(last_decision_date[0]) if last_decision_date else 'N/A'
    html += '</span></div>'

    # 3. Regulation
    case_regulation = new_metadata.get("caseRegulation", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(regulation_changed)}">'
    html += f'<span style="color:#6b7280;">Regulation{get_label_suffix(regulation_changed)}:</span> '
    html += case_regulation[0] if case_regulation else 'N/A'
    html += '</div>'

    # 4. Notification date
    notification_date = new_metadata.get("caseNotificationDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(notification_changed)}">'
    html += f'<span style="color:#6b7280;">Notification date{get_label_suffix(notification_changed)}:</span> '
    html += format_date(notification_date[0]) if notification_date else 'N/A'
    html += '</div>'

    # 5. Provisional deadline
    deadline_date = new_metadata.get("caseDeadlineDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(deadline_changed)}">'
    html += f'<span style="color:#6b7280;">Provisional deadline{get_label_suffix(deadline_changed)}:</span> '
    html += format_date(deadline_date[0]) if deadline_date else 'N/A'
    html += '</div>'

    # 6. Economic activities
    case_sectors = new_metadata.get("caseSectors", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(sectors_changed)}">'
    html += f'<span style="color:#6b7280;">Economic activities{get_label_suffix(sectors_changed)}:</span> '
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
    new_decisions = new_case.get("decisions", [])
    old_decisions = old_case.get("decisions", [])
    oj_info = get_oj_prior_publication(new_decisions)
    old_oj_info = get_oj_prior_publication(old_decisions)

    if oj_info:
        # Check if OJ info actually changed by comparing the values
        oj_changed = False
        if old_oj_info:
            # Both exist, check if they're different
            if (old_oj_info.get("reference") != oj_info.get("reference") or
                    old_oj_info.get("publishedDate") != oj_info.get("publishedDate")):
                oj_changed = True
        else:
            # New OJ info was added
            oj_changed = True

        html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style("updated" if oj_changed else "unchanged")}">'
        html += f'<span style="color:#6b7280;">Prior publication in OJ{get_label_suffix("updated" if oj_changed else "unchanged")}:</span> '
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

    # Update banner
    if changed_fields:
        html += f'<div style="padding:14px 18px;margin:18px 28px;border-radius:6px;font-size:14px;font-weight:600;color:#dc2626;background-color:#fef2f2;border-left:4px solid #ef4444;">⚠️ This case was updated. Changed fields: {", ".join(changed_fields)}</div>'

    # Decisions section - ALWAYS SHOW if there are decisions
    if new_decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        section_title = f'Decisions{get_label_suffix("updated" if decisions_changed else "unchanged")}'
        html += f'<div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">{section_title}</div>'

        for decision in new_decisions:
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

                # Decision details (Single-row format)
                decision_attachments = decision.get("decisionAttachments", [])
                press_releases = decision_metadata.get(
                    "decisionPressReleases", [])

                if decision_attachments or press_releases:
                    html += '<div style="margin-top:10px;">'

                    if decision_attachments:
                        html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                        html += '<span style="color:#6b7280;">Decision text(s):</span> '

                        for attachment in decision_attachments:
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

    # Other case related information - ALWAYS SHOW if there are attachments (Single-row format)
    new_attachments = new_case.get("caseAttachments", [])
    if new_attachments:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        section_title = f'Other case related information{get_label_suffix("updated" if attachments_changed else "unchanged")}'
        html += f'<div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">{section_title}</div>'

        for attachment in new_attachments:
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

    return html


def save_html_file(case_number: str, html_content: str) -> str:
    """Save HTML content to a file."""
    os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)
    safe_case_number = case_number.replace(".", "_").replace("/", "_")
    filename = f"{safe_case_number}_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(HTML_OUTPUT_DIR, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"   💾 Saved HTML to: {filepath}")
        return filepath
    except Exception as e:
        print(f"   ❌ Error saving HTML file: {e}")
        return ""


def send_ec_case_email_via_webhook(case_number: str, case_title: str, html_content: str, changed_fields: List[str], deal_id: str = None) -> bool:
    """
    Send email notification via n8n webhook for EC case updates.

    Args:
        case_number: The EC case number (e.g., "M.12259")
        case_title: The case title (e.g., "GIM / MGX / ALIGNED")
        html_content: The HTML content to send in email
        changed_fields: List of fields that were updated
        deal_id: The MongoDB deal ID (optional)

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email subject
        subject = f"EC Case Update – {case_number}: {case_title}"
        print(f"   📝 Generated email subject: {subject}")

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
        )
        print(f"   📤 Sending email via n8n webhook: {webhook_url}")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_content,
            'case_number': case_number,
            'case_title': case_title,
            'deal_id': deal_id if deal_id else "N/A",
            'changed_fields': changed_fields if changed_fields else [],
            'case_url': f"https://competition-cases.ec.europa.eu/cases/{case_number}"
        }

        # Send POST request to n8n webhook
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        response.raise_for_status()

        print(f"   ✅ Email sent successfully! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"   ⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"   ⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def update_case_in_db(deal_id: str, case_number: str, new_case_data: Dict[str, Any]) -> bool:
    """Update the case in the database."""
    try:
        if not is_connected():
            print("   ⚠️ MongoDB connection not available")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("   ⚠️ Deals collection not available")
            return False

        deal_id_obj = ObjectId(deal_id)
        deal = collection.find_one({"_id": deal_id_obj})
        if not deal or "ec_cases" not in deal:
            print(f"   ⚠️ Deal not found or has no ec_cases")
            return False

        # Find and replace the case
        updated = False
        for i, existing_case in enumerate(deal["ec_cases"]):
            existing_metadata = existing_case.get("metadata", {})
            existing_case_number = existing_metadata.get(
                "caseNumber", [None])[0]
            if existing_case_number == case_number:
                # Preserve matched fields
                if "matched_company" in existing_case:
                    new_case_data["matched_company"] = existing_case["matched_company"]
                if "matched_role" in existing_case:
                    new_case_data["matched_role"] = existing_case["matched_role"]
                deal["ec_cases"][i] = new_case_data
                updated = True
                break

        if not updated:
            print(f"   ⚠️ Case {case_number} not found in deal's ec_cases")
            return False

        # Update the database
        update_result = collection.update_one(
            {"_id": deal_id_obj},
            {"$set": {"ec_cases": deal["ec_cases"]}}
        )

        if update_result.modified_count > 0:
            print(f"   ✅ Updated case in database")
            return True
        else:
            print(f"   ℹ️ No DB changes made (data identical)")
            return True
    except Exception as e:
        print(f"   ❌ Error updating database: {e}")
        return False


def process_case_updates():
    """Main function to process case updates."""
    print("🚀 Starting EC Case Update Monitor\n")

    # Initialize MongoDB
    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        return
    print(f"✅ {message}\n")

    # Download latest data
    print("📥 Fetching latest case data...")
    # latest_data = load_json_from_file(LOCAL_DATA_PATH)
    latest_data = download_json(DATA_URL)
    print(f"✅ Loaded {len(latest_data)} cases from data source\n")

    # Get deals with ec_cases
    print("📊 Loading deals with ec_cases from MongoDB...")
    deals = get_deals_with_ec_cases()
    if not deals:
        print("⚠️ No deals with ec_cases found. Exiting.")
        return
    print(f"✅ Found {len(deals)} deals with ec_cases\n")

    # Process each deal
    total_cases_checked = 0
    total_cases_updated = 0

    for deal_idx, deal in enumerate(deals, 1):
        deal_id = deal.get("deal_id", "")
        ec_cases = deal.get("ec_cases", [])
        print(
            f"[{deal_idx}/{len(deals)}] Processing deal {deal_id} ({len(ec_cases)} cases)")

        for case_idx, existing_case in enumerate(ec_cases, 1):
            total_cases_checked += 1

            case_metadata = existing_case.get("metadata", {})
            case_number = case_metadata.get("caseNumber", [None])[0]

            if not case_number:
                print(
                    f"   [{case_idx}/{len(ec_cases)}] ⚠️ No caseNumber, skipping")
                continue

            if case_number not in latest_data:
                print(
                    f"   [{case_idx}/{len(ec_cases)}] {case_number}: ⚠️ Not found in latest data")
                continue

            new_case = latest_data[case_number]
            differences = deep_compare(existing_case, new_case)

            # Check if there are meaningful changes
            if not has_meaningful_changes(differences):
                print(
                    f"   [{case_idx}/{len(ec_cases)}] {case_number}: ✅ No meaningful changes")
                # Still update DB to ensure sync
                update_case_in_db(deal_id, case_number, new_case)
                continue

            # Meaningful changes found!
            meaningful_diffs = [d for d in differences if not any(
                ignored in d[0] for ignored in ["matched_company", "matched_role"])]
            print(
                f"   [{case_idx}/{len(ec_cases)}] {case_number}: 🔄 Changes detected ({len(meaningful_diffs)} differences)")

            # Log changed fields
            changed_fields = set()
            for path, old_val, new_val in meaningful_diffs:
                field_name = path.split('.')[-1] if '.' in path else path
                changed_fields.add(field_name)
            if changed_fields:
                print(
                    f"      Changed fields: {', '.join(sorted(changed_fields))}")

            # Generate HTML
            html_content = generate_html_for_changes(
                case_number, existing_case, new_case, meaningful_diffs)
            save_html_file(case_number, html_content)

            # Send email notification
            case_metadata = new_case.get("metadata", {})
            case_title = case_metadata.get("caseTitle", ["N/A"])[0]
            field_names = []
            for diff_path, old_val, new_val in meaningful_diffs:
                field_name = diff_path.split(
                    '.')[-1] if '.' in diff_path else diff_path
                if field_name not in field_names:
                    field_names.append(field_name)

            send_ec_case_email_via_webhook(
                case_number,
                case_title,
                html_content,
                field_names,
                deal_id
            )

            # Update database
            update_success = update_case_in_db(deal_id, case_number, new_case)
            if update_success:
                total_cases_updated += 1

    print(f"\n{'='*60}")
    print(f"📊 Summary:")
    print(f"   Total cases checked: {total_cases_checked}")
    print(f"   Cases with changes: {total_cases_updated}")
    print(f"   HTML files saved to: {HTML_OUTPUT_DIR}/")
    print(f"{'='*60}\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_case_updates()
