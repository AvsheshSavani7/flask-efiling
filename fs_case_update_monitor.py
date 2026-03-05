"""
FS (Foreign Subsidies) case update monitor: compare latest FS case data with stored fs_ec_cases on deals,
detect meaningful changes, generate HTML diff, send email via n8n, and update MongoDB fs_ec_cases.
"""
import json
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from html import escape
import os
from dotenv import load_dotenv
from bson import ObjectId
from mongodb_connection import get_deals_collection, is_connected, init_mongodb_connection

# Constants
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-FS.json"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "fs_case_updates"
CASE_BASE_URL = "https://competition-cases.ec.europa.eu/cases"


def download_json(url: str) -> Any:
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


def get_deals_with_fs_ec_cases() -> List[Dict[str, Any]]:
    """Fetch deals from MongoDB that have non-empty 'fs_ec_cases' array."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available.")
            return []

        query = {"fs_ec_cases": {"$exists": True, "$ne": [], "$type": "array"}}
        all_deals = list(collection.find(query))

        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        print(f"✅ Fetched {len(all_deals)} deals with fs_ec_cases from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        return []


def normalize_for_comparison(data: Any) -> Any:
    """Normalize data for comparison (exclude matched_company, matched_role)."""
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
        except Exception:
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
                differences.append((new_path, None, new_data.get(key) if isinstance(new_data, dict) else None))
            elif key not in new_normalized:
                differences.append((new_path, old_data.get(key) if isinstance(old_data, dict) else None, None))
            else:
                differences.extend(deep_compare(old_normalized[key], new_normalized[key], new_path))
    elif isinstance(old_normalized, list):
        if len(old_normalized) != len(new_normalized):
            differences.append((path, old_data, new_data))
        else:
            # For lists of primitives we already sorted in normalize_for_comparison; compare by index.
            # For lists of dicts (e.g. decisions) set() would fail; compare by index.
            try:
                for i, (old_item, new_item) in enumerate(zip(old_normalized, new_normalized)):
                    new_path = f"{path}[{i}]" if path else f"[{i}]"
                    differences.extend(deep_compare(old_item, new_item, new_path))
            except (TypeError, ValueError):
                for i, (old_item, new_item) in enumerate(zip(old_normalized, new_normalized)):
                    new_path = f"{path}[{i}]" if path else f"[{i}]"
                    differences.extend(deep_compare(old_item, new_item, new_path))
    else:
        if old_normalized != new_normalized:
            differences.append((path, old_data, new_data))

    return differences


def has_meaningful_changes(differences: List[Tuple[str, Any, Any]]) -> bool:
    """Check if there are meaningful changes (ignore matched_company, matched_role)."""
    if not differences:
        return False
    ignored_paths = ["matched_company", "matched_role"]
    for path, old_val, new_val in differences:
        if any(ignored in path for ignored in ignored_paths):
            continue
        return True
    return False


def format_date(date_str: str) -> str:
    """Format date from YYYY-MM-DD to DD.MM.YYYY."""
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


def get_companies_from_case_title(case_data: Dict[str, Any]) -> List[str]:
    """Build company list from caseTitle only, split by ' / '."""
    metadata = case_data.get("metadata", {})
    case_title_list = metadata.get("caseTitle", [])
    title = case_title_list[0] if case_title_list else ""
    if not title or not isinstance(title, str):
        return []
    return [c.strip() for c in title.split(" / ") if c.strip()]


def get_oj_prior_publication(decisions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Get OJ prior publication info from decisions where decisionOjPriorPublication is true."""
    for decision in decisions:
        metadata = decision.get("metadata", {})
        oj_prior = metadata.get("decisionOjPriorPublication", [])
        if oj_prior and len(oj_prior) > 0 and str(oj_prior[0]).lower() == "true":
            oj_pubs = metadata.get("decisionOfficialJournalPublications", [])
            if oj_pubs and len(oj_pubs) > 0:
                try:
                    pub_data = json.loads(oj_pubs[0]) if isinstance(oj_pubs[0], str) else oj_pubs[0]
                    items = pub_data.get("items", [])
                    if items:
                        return {
                            "reference": items[0].get("reference", ""),
                            "publishedDate": items[0].get("publishedDate", ""),
                            "priorPublication": items[0].get("priorPublication", "true")
                        }
                except Exception:
                    pass
            pub_dates = metadata.get("decisionOfficialJournalPublicationsPublishedDates", [])
            if pub_dates and len(pub_dates) > 0:
                return {"reference": "", "publishedDate": pub_dates[0], "priorPublication": "true"}
    return None


def get_field_changed_status(field_path: str, differences: List[Tuple[str, Any, Any]]) -> str:
    """Check if a field was changed. Returns: 'updated', 'added', 'removed', or 'unchanged'."""
    for diff_path, old_val, new_val in differences:
        if field_path in diff_path:
            if old_val is None:
                return 'added'
            elif new_val is None:
                return 'removed'
            else:
                return 'updated'
    return 'unchanged'


def escape_html(s: Any) -> str:
    """Safe HTML escape for template output."""
    return "" if s is None else escape(str(s))


def generate_html_for_changes(
    case_number: str,
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    differences: List[Tuple[str, Any, Any]]
) -> str:
    """Generate HTML showing changes between old and new FS case data. Companies from caseTitle; FS labels and links."""
    new_metadata = new_case.get("metadata", {})
    old_metadata = old_case.get("metadata", {})

    case_num = new_metadata.get("caseNumber", [case_number])[0]
    case_title = new_metadata.get("caseTitle", ["N/A"])[0]
    case_instrument = (new_metadata.get("caseInstrument") or ["Foreign Subsidies"])[0]

    # FS: use caseTitle for "Companies (case title)" change detection
    case_title_changed = get_field_changed_status("metadata.caseTitle", differences)
    last_decision_changed = get_field_changed_status("metadata.caseLastDecisionDate", differences)
    regulation_changed = get_field_changed_status("metadata.caseRegulation", differences)
    notification_changed = get_field_changed_status("metadata.caseNotificationDate", differences)
    deadline_changed = get_field_changed_status("metadata.caseDeadlineDate", differences)
    sectors_changed = get_field_changed_status("metadata.caseSectors", differences)

    changed_fields = []
    if case_title_changed != 'unchanged':
        changed_fields.append("Companies (case title)")
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

    decisions_changed = any("decisions" in d[0] for d in differences)
    attachments_changed = any("caseAttachments" in d[0] for d in differences)
    if decisions_changed:
        changed_fields.append("Decisions")
    if attachments_changed:
        changed_fields.append("Other case related information")

    def get_highlight_style(status: str) -> str:
        if status == 'updated':
            return 'background-color:#fef3c7;padding:3px 8px;border-radius:4px;border-left:3px solid #f59e0b;'
        if status == 'added':
            return 'background-color:#d1fae5;padding:3px 8px;border-radius:4px;border-left:3px solid #10b981;'
        return ''

    def get_label_suffix(status: str) -> str:
        if status != 'unchanged':
            return ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;margin-left:4px;">(Updated)</span>'
        return ''

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>EC FS Case Update - {escape_html(case_num)}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{escape_html(case_num)}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #059669;color:#059669;background:#fff;">{escape_html(case_instrument)}</span>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #2563eb;color:#2563eb;background:#fff;">FS</span>
</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{escape_html(case_title)}</div>
<div style="margin-top:18px;">'''

    # 1. Companies (case title)
    company_list = get_companies_from_case_title(new_case)
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(case_title_changed)}">'
    html += f'<span style="color:#6b7280;">Companies (case title){get_label_suffix(case_title_changed)}:</span> '
    if company_list:
        for i, company in enumerate(company_list):
            html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS&caseTitleOrCompanyName={escape_html(company)}" style="color:#2563eb;text-decoration:none;font-weight:700;">{escape_html(company)}</a>'
            if i < len(company_list) - 1:
                html += '<span style="color:#9ca3af;margin:0 8px;">|</span>'
    else:
        html += 'N/A'
    html += '</div>'

    # Case URL
    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += f'<span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="{CASE_BASE_URL}/{escape_html(case_num)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{CASE_BASE_URL}/{escape_html(case_num)}</a><span style="color:#9ca3af;margin-left:6px;">↗</span>'
    html += '</div>'

    # 2. Last decision date
    last_decision_date = new_metadata.get("caseLastDecisionDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(last_decision_changed)}">'
    html += f'<span style="color:#6b7280;">Last decision date{get_label_suffix(last_decision_changed)}:</span> '
    html += f'<span style="font-weight:800;">{format_date(last_decision_date[0]) if last_decision_date else "N/A"}</span></div>'

    # 3. Regulation
    case_regulation = new_metadata.get("caseRegulation", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(regulation_changed)}">'
    html += f'<span style="color:#6b7280;">Regulation{get_label_suffix(regulation_changed)}:</span> '
    if case_regulation:
        reg_data = parse_json_field(case_regulation[0])
        reg_label = reg_data.get("label", case_regulation[0]) if reg_data else case_regulation[0]
        html += escape_html(reg_label) + '</div>'
    else:
        html += 'N/A</div>'

    # 4. Notification date
    notification_date = new_metadata.get("caseNotificationDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(notification_changed)}">'
    html += f'<span style="color:#6b7280;">Notification date{get_label_suffix(notification_changed)}:</span> '
    html += (format_date(notification_date[0]) if notification_date else 'N/A') + '</div>'

    # 5. Provisional deadline
    deadline_date = new_metadata.get("caseDeadlineDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(deadline_changed)}">'
    html += f'<span style="color:#6b7280;">Provisional deadline{get_label_suffix(deadline_changed)}:</span> '
    html += (format_date(deadline_date[0]) if deadline_date else 'N/A') + '</div>'

    # 6. Economic activities (FS sector codes: NaceSectors + NaceV2Sector_)
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
                    sector_code = code.replace("NaceV2Sector_", "*").replace("NaceSectors", "*")
                    html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS&caseSectors={sector_code}&sortField=caseLastDecisionDate&sortOrder=DESC" style="color:#2563eb;text-decoration:none;font-weight:700;">{escape_html(label)}</a> '
    else:
        html += 'N/A'
    html += '</div>'

    # Prior publication in OJ
    new_decisions = new_case.get("decisions", [])
    old_decisions = old_case.get("decisions", [])
    oj_info = get_oj_prior_publication(new_decisions)
    old_oj_info = get_oj_prior_publication(old_decisions)
    if oj_info:
        oj_changed = not old_oj_info or (old_oj_info.get("reference") != oj_info.get("reference") or old_oj_info.get("publishedDate") != oj_info.get("publishedDate"))
        html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style("updated" if oj_changed else "unchanged")}">'
        html += f'<span style="color:#6b7280;">Prior publication in OJ{get_label_suffix("updated" if oj_changed else "unchanged")}:</span> '
        if oj_info.get("reference"):
            ref = oj_info["reference"]
            ref_number = ref.replace("C", "").replace("c", "")
            year = (oj_info.get("publishedDate") or "")[:4]
            if ref_number and year:
                html += f'<a href="https://eur-lex.europa.eu/eli/C/{year}/{ref_number}/oj" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">OJEU {escape_html(ref)}</a><span style="color:#9ca3af;margin:0 6px;">↗</span>'
            else:
                html += f'OJEU {escape_html(ref)}'
        if oj_info.get("publishedDate"):
            html += f'<span style="color:#111827;"> of {format_date(oj_info["publishedDate"])}</span>'
        html += '</div>'

    html += '</div></div>'

    if changed_fields:
        html += f'<div style="padding:14px 18px;margin:18px 28px;border-radius:6px;font-size:14px;font-weight:600;color:#dc2626;background-color:#fef2f2;border-left:4px solid #ef4444;">⚠️ This case was updated. Changed fields: {escape_html(", ".join(changed_fields))}</div>'

    # Decisions section
    if new_decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        section_title = f'Decisions{get_label_suffix("updated" if decisions_changed else "unchanged")}'
        html += f'<div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">{section_title}</div>'
        for decision in new_decisions:
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
                html += '</div>'
                decision_attachments = decision.get("decisionAttachments", [])
                press_releases = dmeta.get("decisionPressReleases", [])
                if decision_attachments or press_releases:
                    html += '<div style="margin-top:10px;">'
                    if decision_attachments:
                        html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;"><span style="color:#6b7280;">Decision text(s):</span> '
                        for att in decision_attachments:
                            ameta = att.get("metadata", {})
                            att_link = ameta.get("attachmentLink", [""])[0]
                            att_lang = ameta.get("attachmentLanguage", ["EN"])[0]
                            att_pub = ameta.get("attachmentPublicationBusinessDate", [""])[0]
                            if att_link:
                                html += f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:12px;">'
                                html += '<span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border:1px solid #ef4444;border-radius:3px;color:#ef4444;font-size:9px;font-weight:900;">PDF</span>'
                                html += f'<a href="{escape_html(att_link)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{escape_html(att_lang)}</a>'
                                if att_pub:
                                    html += f'<span style="color:#6b7280;font-size:13px;">published on {format_date(att_pub)}</span>'
                                html += '</span>'
                        html += '</div>'
                    if press_releases:
                        html += '<div style="font-size:14px;color:#111827;"><span style="color:#6b7280;">Press communication:</span> '
                        try:
                            pr_data = json.loads(press_releases[0]) if isinstance(press_releases[0], str) else press_releases[0]
                            for idx, item in enumerate(pr_data.get("items", [])):
                                ref = item.get("reference", "")
                                if idx > 0:
                                    html += ' '
                                html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={escape_html(ref)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{escape_html(ref)}</a><span style="color:#9ca3af;margin-left:4px;">↗</span>'
                        except Exception:
                            pass
                        html += '</div>'
                    html += '</div>'
                html += '</div>'
        html += '</div>'

    # Other case related information
    new_attachments = new_case.get("caseAttachments", [])
    if new_attachments:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        section_title = f'Other case related information{get_label_suffix("updated" if attachments_changed else "unchanged")}'
        html += f'<div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">{section_title}</div>'
        for att in new_attachments:
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

    html += '</div></body></html>'
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


def send_fs_case_email_via_webhook(
    case_number: str,
    case_title: str,
    html_content: str,
    changed_fields: List[str],
    deal_id: Optional[str] = None
) -> bool:
    """Send email via n8n webhook for FS case updates."""
    try:
        subject = f"EC Foreign Subsidies Case Update – {case_number}: {case_title}"
        print(f"   📝 Generated email subject: {subject}")
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
        )
        print(f"   📤 Sending email via n8n webhook: {webhook_url}")
        payload = {
            'subject': subject,
            'html': html_content,
            'case_number': case_number,
            'case_title': case_title,
            'deal_id': deal_id if deal_id else "N/A",
            'changed_fields': changed_fields if changed_fields else [],
            'case_url': f"{CASE_BASE_URL}/{case_number}",
            'case_instrument': 'FS',
        }
        response = requests.post(webhook_url, json=payload, headers={'Content-Type': 'application/json'}, timeout=30)
        response.raise_for_status()
        print(f"   ✅ Email sent successfully! Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"   ⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"   ⚠️ Error sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def update_case_in_db(deal_id: str, case_number: str, new_case_data: Dict[str, Any]) -> bool:
    """Update the case in the database (fs_ec_cases array)."""
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
        if not deal or "fs_ec_cases" not in deal:
            print("   ⚠️ Deal not found or has no fs_ec_cases")
            return False

        updated = False
        for i, existing_case in enumerate(deal["fs_ec_cases"]):
            existing_metadata = existing_case.get("metadata", {})
            existing_case_number = existing_metadata.get("caseNumber", [None])[0]
            if existing_case_number == case_number:
                if "matched_company" in existing_case:
                    new_case_data["matched_company"] = existing_case["matched_company"]
                if "matched_role" in existing_case:
                    new_case_data["matched_role"] = existing_case["matched_role"]
                deal["fs_ec_cases"][i] = new_case_data
                updated = True
                break

        if not updated:
            print(f"   ⚠️ Case {case_number} not found in deal's fs_ec_cases")
            return False

        update_result = collection.update_one(
            {"_id": deal_id_obj},
            {"$set": {"fs_ec_cases": deal["fs_ec_cases"]}}
        )
        if update_result.modified_count > 0:
            print("   ✅ Updated case in database (fs_ec_cases)")
            return True
        print("   ℹ️ No DB changes made (data identical)")
        return True
    except Exception as e:
        print(f"   ❌ Error updating database: {e}")
        return False


def process_case_updates() -> None:
    """Main: fetch latest FS data, compare with fs_ec_cases on deals, send emails and update DB."""
    print("🚀 Starting FS (Foreign Subsidies) Case Update Monitor\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        return
    print(f"✅ {message}\n")

    print("📥 Fetching latest FS case data...")
    raw = download_json(DATA_URL)
    latest_data = normalize_fs_data(raw)
    print(f"✅ Loaded {len(latest_data)} cases from data source\n")

    print("📊 Loading deals with fs_ec_cases from MongoDB...")
    deals = get_deals_with_fs_ec_cases()
    if not deals:
        print("⚠️ No deals with fs_ec_cases found. Exiting.")
        return
    print(f"✅ Found {len(deals)} deals with fs_ec_cases\n")

    total_cases_checked = 0
    total_cases_updated = 0

    for deal_idx, deal in enumerate(deals, 1):
        deal_id = deal.get("deal_id", "")
        fs_cases = deal.get("fs_ec_cases", [])
        print(f"[{deal_idx}/{len(deals)}] Processing deal {deal_id} ({len(fs_cases)} FS cases)")

        for case_idx, existing_case in enumerate(fs_cases, 1):
            total_cases_checked += 1
            case_metadata = existing_case.get("metadata", {})
            case_number = case_metadata.get("caseNumber", [None])[0]

            if not case_number:
                print(f"   [{case_idx}/{len(fs_cases)}] ⚠️ No caseNumber, skipping")
                continue

            if case_number not in latest_data:
                print(f"   [{case_idx}/{len(fs_cases)}] {case_number}: ⚠️ Not found in latest data")
                continue

            new_case = latest_data[case_number]
            differences = deep_compare(existing_case, new_case)

            if not has_meaningful_changes(differences):
                print(f"   [{case_idx}/{len(fs_cases)}] {case_number}: ✅ No meaningful changes")
                update_case_in_db(deal_id, case_number, new_case)
                continue

            meaningful_diffs = [d for d in differences if not any(
                ignored in d[0] for ignored in ["matched_company", "matched_role"])]
            print(f"   [{case_idx}/{len(fs_cases)}] {case_number}: 🔄 Changes detected ({len(meaningful_diffs)} differences)")

            changed_fields = set()
            for path, old_val, new_val in meaningful_diffs:
                field_name = path.split('.')[-1] if '.' in path else path
                changed_fields.add(field_name)
            if changed_fields:
                print(f"      Changed fields: {', '.join(sorted(changed_fields))}")

            html_content = generate_html_for_changes(case_number, existing_case, new_case, meaningful_diffs)
            save_html_file(case_number, html_content)

            new_metadata = new_case.get("metadata", {})
            case_title = new_metadata.get("caseTitle", ["N/A"])[0]
            field_names = []
            for diff_path, _, _ in meaningful_diffs:
                field_name = diff_path.split('.')[-1] if '.' in diff_path else diff_path
                if field_name not in field_names:
                    field_names.append(field_name)

            send_fs_case_email_via_webhook(
                case_number, case_title, html_content, field_names, deal_id
            )

            if update_case_in_db(deal_id, case_number, new_case):
                total_cases_updated += 1

    print(f"\n{'='*60}")
    print("📊 Summary:")
    print(f"   Total cases checked: {total_cases_checked}")
    print(f"   Cases with changes: {total_cases_updated}")
    print(f"   HTML files saved to: {HTML_OUTPUT_DIR}/")
    print(f"{'='*60}\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_case_updates()
