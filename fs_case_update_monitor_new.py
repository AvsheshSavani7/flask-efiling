# isort: skip_file  (sys.path must be set before parent-directory imports)
"""
FS (Foreign Subsidies) Case Update Monitor
============================================

Monitors cases in the 'fs_cases' MongoDB collection for changes against
fresh data from the EC Foreign Subsidies portal.

Flow:
1. Download + normalize fresh case JSON from EC FS portal
2. Fetch all cases from the fs_cases MongoDB collection
3. For each DB case, find the matching fresh record by case_number and compare
4. If changes detected:
   - If deal_id present: fetch deal, send [FRMD] matched deal update email
   - If no deal_id: LLM re-match attempt, then send [FRMD] or [FRUD] email
5. Update the case record in the fs_cases collection
"""

from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from openai import OpenAI
from dotenv import load_dotenv
from bson import ObjectId
import requests
from typing import Any, Dict, List, Optional, Tuple
from html import escape
from datetime import datetime
import builtins
import logging
import json
import sys
import os

# Must be first — adds parent flask/ directory to path before any local imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


load_dotenv(".env")

# OpenAI client for deal matching
_openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# -----------------------------------------------------------------------------
# Logging setup (stdout + file)
# -----------------------------------------------------------------------------
LOGGER_NAME = "fs_case_update_monitor"
LOG_FILE = "fs_case_update_monitor_new.log"

logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    """Replacement for print that also logs to a file via the module logger."""
    msg = " ".join(str(a) for a in args)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)
    builtins.print(*args, **kwargs)


print = _logged_print  # type: ignore

# Constants
ENV_PATH = ".env"
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-FS.json"
CASE_BASE_URL = "https://competition-cases.ec.europa.eu/cases"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
)

# Fields added by the register script — excluded from case data comparison
_TRACKING_FIELDS = frozenset({
    "_id", "case_number", "deal_id", "usa_related",
    "matched_company", "matched_role", "created_at", "updated_at",
})


# -----------------------------------------------------------------------------
# FS data helpers (copied from fs_case_filter / fs_case_update_monitor — no imports)
# -----------------------------------------------------------------------------

def escape_html(s: Any) -> str:
    """Safe HTML escape for template output."""
    return "" if s is None else escape(str(s))


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
        print(f"❌ Error downloading data: {e}", level="error")
        raise
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing JSON: {e}", level="error")
        raise


def normalize_fs_data(data: Any) -> Dict[str, Any]:
    """
    Normalize FS JSON to a single dict keyed by case number.
    Handles:
      - Array of objects: [ { "FS.100081": {...} }, ... ]
      - Single flat object: { "FS.100081": {...}, ... }
      - Wrapped response: { "data": [...] } or { "cases": {...} }
    """
    if data is None:
        return {}
    if isinstance(data, list):
        out: Dict[str, Any] = {}
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


def get_companies_from_case_title(case_data: Dict[str, Any]) -> List[str]:
    """Build company list from caseTitle only, split by ' / '."""
    metadata = case_data.get("metadata", {})
    case_title_list = metadata.get("caseTitle", [])
    title = case_title_list[0] if case_title_list else ""
    if not title or not isinstance(title, str):
        return []
    return [c.strip() for c in title.split(" / ") if c.strip()]


# -----------------------------------------------------------------------------
# Comparison logic (copied as-is from fs_case_update_monitor.py)
# -----------------------------------------------------------------------------

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
    differences: List[Tuple[str, Any, Any]] = []
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
            # For lists of primitives we already sorted in normalize_for_comparison; compare by index.
            # For lists of dicts (e.g. decisions) set() would fail; compare by index.
            try:
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
    """Check if there are meaningful changes (ignore matched_company, matched_role)."""
    if not differences:
        return False
    ignored_paths = ["matched_company", "matched_role"]
    for path, old_val, new_val in differences:
        if any(ignored in path for ignored in ignored_paths):
            continue
        return True
    return False


# -----------------------------------------------------------------------------
# HTML / email helpers (copied from fs_case_update_monitor.py — FS-specific style)
# -----------------------------------------------------------------------------

def format_date(date_str: str) -> str:
    """Format YYYY-MM-DD to DD.MM.YYYY."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return date_str


def parse_json_field(field_value: Any) -> Dict[str, Any]:
    """Parse a JSON string field into a dict."""
    try:
        if isinstance(field_value, str) and field_value.strip().startswith("{"):
            return json.loads(field_value)
        return {}
    except Exception:
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
                except Exception:
                    pass
            pub_dates = metadata.get(
                "decisionOfficialJournalPublicationsPublishedDates", [])
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
    case_instrument = (new_metadata.get("caseInstrument")
                       or ["Foreign Subsidies"])[0]

    # FS: use caseTitle for "Companies (case title)" change detection
    case_title_changed = get_field_changed_status(
        "metadata.caseTitle", differences)
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
    html += f'<a href="{CASE_BASE_URL}/{escape_html(case_num)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{CASE_BASE_URL}/{escape_html(case_num)}</a><span style="color:#9ca3af;margin-left:6px;">\u2197</span>'
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
        reg_label = reg_data.get(
            "label", case_regulation[0]) if reg_data else case_regulation[0]
        html += escape_html(reg_label) + '</div>'
    else:
        html += 'N/A</div>'

    # 4. Notification date
    notification_date = new_metadata.get("caseNotificationDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(notification_changed)}">'
    html += f'<span style="color:#6b7280;">Notification date{get_label_suffix(notification_changed)}:</span> '
    html += (format_date(notification_date[0])
             if notification_date else 'N/A') + '</div>'

    # 5. Provisional deadline
    deadline_date = new_metadata.get("caseDeadlineDate", [])
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style(deadline_changed)}">'
    html += f'<span style="color:#6b7280;">Provisional deadline{get_label_suffix(deadline_changed)}:</span> '
    html += (format_date(deadline_date[0])
             if deadline_date else 'N/A') + '</div>'

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
                    sector_code = code.replace(
                        "NaceV2Sector_", "*").replace("NaceSectors", "*")
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
        oj_changed = not old_oj_info or (old_oj_info.get("reference") != oj_info.get(
            "reference") or old_oj_info.get("publishedDate") != oj_info.get("publishedDate"))
        html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{get_highlight_style("updated" if oj_changed else "unchanged")}">'
        html += f'<span style="color:#6b7280;">Prior publication in OJ{get_label_suffix("updated" if oj_changed else "unchanged")}:</span> '
        if oj_info.get("reference"):
            ref = oj_info["reference"]
            ref_number = ref.replace("C", "").replace("c", "")
            year = (oj_info.get("publishedDate") or "")[:4]
            if ref_number and year:
                html += f'<a href="https://eur-lex.europa.eu/eli/C/{year}/{ref_number}/oj" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">OJEU {escape_html(ref)}</a><span style="color:#9ca3af;margin:0 6px;">\u2197</span>'
            else:
                html += f'OJEU {escape_html(ref)}'
        if oj_info.get("publishedDate"):
            html += f'<span style="color:#111827;"> of {format_date(oj_info["publishedDate"])}</span>'
        html += '</div>'

    html += '</div></div>'

    if changed_fields:
        html += f'<div style="padding:14px 18px;margin:18px 28px;border-radius:6px;font-size:14px;font-weight:600;color:#dc2626;background-color:#fef2f2;border-left:4px solid #ef4444;">\u26a0\ufe0f This case was updated. Changed fields: {escape_html(", ".join(changed_fields))}</div>'

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
                            att_lang = ameta.get(
                                "attachmentLanguage", ["EN"])[0]
                            att_pub = ameta.get(
                                "attachmentPublicationBusinessDate", [""])[0]
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
                            pr_data = json.loads(press_releases[0]) if isinstance(
                                press_releases[0], str) else press_releases[0]
                            for idx, item in enumerate(pr_data.get("items", [])):
                                ref = item.get("reference", "")
                                if idx > 0:
                                    html += ' '
                                html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={escape_html(ref)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{escape_html(ref)}</a><span style="color:#9ca3af;margin-left:4px;">\u2197</span>'
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


# -----------------------------------------------------------------------------
# Update monitor helpers
# -----------------------------------------------------------------------------

def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_fs_cases_collection():
    """Get the 'fs_cases' collection from MongoDB."""
    db = get_database()
    if db is None:
        return None
    return db["fs_cases"]


def extract_case_data(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return only FS case data fields from a DB document (strips tracking fields)."""
    return {k: v for k, v in doc.items() if k not in _TRACKING_FIELDS}


def _build_deal_banner(deal: Optional[Dict[str, Any]], case_number: str) -> str:
    """Return an HTML snippet for the matched-deal or USA-related banner."""
    case_url = f"{CASE_BASE_URL}/{case_number}"
    if deal:
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        deal_id = deal.get("deal_id") or str(deal.get("_id", "N/A"))
        return (
            '<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;'
            'margin:20px 28px 0 28px;border-left:4px solid #2563eb;">'
            '<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>'
            '<div style="font-size:14px;color:#1e3a8a;">'
            f'<span style="font-weight:700;">Acquirer:</span> {escape_html(acquirer)}'
            '<span style="color:#94a3b8;margin:0 8px;">|</span>'
            f'<span style="font-weight:700;">Target:</span> {escape_html(target)}'
            '<span style="color:#94a3b8;margin:0 8px;">|</span>'
            f'<span style="font-weight:700;">Deal ID:</span> {escape_html(deal_id)}'
            '</div>'
            '<div style="margin-top:10px;">'
            f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;'
            'font-weight:700;font-size:14px;">View FS Case \u2192</a>'
            '</div></div>'
        )
    return (
        '<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;'
        'margin:20px 28px 0 28px;border-left:4px solid #3b82f6;">'
        '<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">'
        '\U0001f1fa\U0001f1f8 USA-Related Case</div>'
        '<div style="font-size:14px;color:#1e3a8a;">'
        'This EC Foreign Subsidies case appears to involve USA-related parties or markets.'
        '</div>'
        '<div style="margin-top:10px;">'
        f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;'
        'font-weight:700;font-size:14px;">View FS Case \u2192</a>'
        '</div></div>'
    )


def _inject_banner(html: str, banner_html: str) -> str:
    """Inject a banner before the case header inside the generated HTML card."""
    marker = '<div style="padding:28px 28px 12px 28px;">'
    return html.replace(marker, f"{banner_html}\n{marker}", 1)


def send_update_email(
    case_doc: Dict[str, Any],
    new_case_data: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    differences: List[Tuple[str, Any, Any]],
) -> bool:
    """Send update email via n8n webhook.

    deal=None  ->  USA-related update email ([FRUD]).
    deal given ->  matched deal update email ([FRMD]).
    """
    try:
        case_number = case_doc.get("case_number", "N/A")
        new_metadata = new_case_data.get("metadata", {})
        case_title = (new_metadata.get("caseTitle") or ["N/A"])[0]

        meaningful_diffs = [
            d for d in differences
            if not any(tf in d[0] for tf in _TRACKING_FIELDS)
        ]

        changed_fields: List[str] = []
        for path, _, _ in meaningful_diffs:
            name = path.split(".")[-1] if "." in path else path
            if name not in changed_fields:
                changed_fields.append(name)

        old_case_data = extract_case_data(case_doc)
        html_content = generate_html_for_changes(
            case_number, old_case_data, new_case_data, meaningful_diffs
        )

        banner = _build_deal_banner(deal, case_number)
        html_content = _inject_banner(html_content, banner)

        if deal:
            subject = f"[FRMD] EC Foreign Subsidies Case (Updated) \u2013 {case_number}: {case_title}"
            deal_id = deal.get("deal_id") or str(deal.get("_id", "N/A"))
        else:
            subject = f"[FRUD] EC Foreign Subsidies Case (USA-Related Update) \u2013 {case_number}: {case_title}"
            deal_id = None

        payload = {
            "subject": subject,
            "html": html_content,
            "case_number": case_number,
            "case_title": case_title,
            "deal_id": deal_id,
            "changed_fields": changed_fields,
            "case_instrument": "FS",
            "source": "ec_foreign_subsidies_cases_update",
        }

        print(f"    📤 Sending update email: {subject}")
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"    ✅ Email sent! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"    ⚠️ Error sending update email: {e}", level="warning")
        return False


def fetch_deals() -> List[Dict[str, Any]]:
    """Fetch all open/unknown deals from MongoDB."""
    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            return []
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(deals_collection.find(status_filter))
        for d in deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])
        print(f"  📋 Fetched {len(deals)} open/unknown deals for matching")
        return deals
    except Exception as e:
        print(f"  ⚠️ Error fetching deals: {e}", level="warning")
        return []


def match_case_to_deal(
    case_companies: List[str], deals: List[Dict[str, Any]]
) -> Optional[Tuple[str, str, str]]:
    """Use LLM to match case companies (from caseTitle) against deals.

    Returns (deal_id, matched_company, matched_role) or None.
    """
    if not case_companies or not deals:
        return None

    companies_str = " / ".join(case_companies)
    lines = []
    for d in deals:
        deal_id = d.get("deal_id") or str(d.get("_id", ""))
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {deal_id} | Target: {target} | Acquirer: {acquirer}"
        for alias_field in ("target_aliases", "parent_aliases"):
            aliases = d.get(alias_field) or []
            if aliases:
                line += f" | {alias_field.replace('_', ' ').title()}: {', '.join(str(a) for a in aliases)}"
        lines.append(line)

    prompt = f"""You are an M&A deal analyst. Given the company names from an EC Foreign Subsidies case (case title), determine whether any match any of the deals below.

DEALS TO MATCH:
{chr(10).join(lines)}

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
        res = _openai_client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert in M&A deal recognition. Return Match: DEAL_ID|COMPANY|target|acquirer or None.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = (res.choices[0].message.content or "").strip()
        print(f"  🤖 LLM match response: {content}")

        if not content.lower().startswith("match:"):
            return None

        parts = content[6:].strip().split("|")
        if len(parts) < 3:
            return None

        deal_id = parts[0].strip()
        matched_company = parts[1].strip()
        role_raw = parts[2].strip().lower().replace("(", "").replace(")", "")
        matched_role = role_raw if role_raw in (
            "target", "acquirer") else "acquirer"
        return (deal_id, matched_company, matched_role)
    except Exception as e:
        print(f"  ⚠️ LLM match error: {e}", level="warning")
        return None


def update_case_document(
    collection,
    case_doc: Dict[str, Any],
    new_case_data: Dict[str, Any],
    extra_fields: Optional[Dict[str, Any]] = None,
) -> bool:
    """Update the fs_cases document with fresh case data, preserving tracking fields.

    extra_fields: optional dict of additional fields to set (e.g. newly discovered deal_id).
    """
    try:
        _id = case_doc.get("_id")
        if not _id:
            print("    ⚠️ Case document has no _id; cannot update", level="warning")
            return False

        updated: Dict[str, Any] = {**new_case_data}

        for field in ("case_number", "deal_id", "usa_related", "matched_company",
                      "matched_role", "created_at"):
            if field in case_doc:
                updated[field] = case_doc[field]

        if extra_fields:
            updated.update(extra_fields)

        updated["updated_at"] = utc_now_iso()

        result = collection.update_one({"_id": _id}, {"$set": updated})
        if result.modified_count > 0:
            print("    ✅ Updated case document in fs_cases")
        else:
            print("    ℹ️ No DB changes made (document already up to date)")
        return True
    except Exception as e:
        print(f"    ❌ Error updating case document: {e}", level="error")
        return False


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def process_fs_case_updates():
    """Main entrypoint for FS case update monitoring."""
    print("🚀 Starting FS (Foreign Subsidies) Case Update Monitor\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}", level="error")
        return
    print(f"✅ {message}\n")

    if not is_connected():
        print("❌ MongoDB not connected. Exiting.", level="error")
        return

    cases_collection = get_fs_cases_collection()
    if cases_collection is None:
        print("❌ Could not access 'fs_cases' collection. Exiting.", level="error")
        return

    deals_collection = get_deals_collection()

    # Step 1: Download + normalize fresh JSON
    print("📥 Downloading fresh FS case data...")
    raw = download_json(DATA_URL)
    fresh_data = normalize_fs_data(raw)
    if not fresh_data:
        print("❌ No case data after normalization. Exiting.", level="error")
        return
    print(f"✅ Loaded {len(fresh_data)} cases from FS portal\n")

    # Step 2: Fetch all cases from fs_cases collection
    cases = list(cases_collection.find())
    if not cases:
        print("⚠️ No cases found in fs_cases collection. Exiting.", level="warning")
        return
    print(f"📊 Found {len(cases)} cases in fs_cases collection\n")

    total_checked = 0
    total_changed = 0

    # Step 3: Iterate each DB case
    for idx, case_doc in enumerate(cases, 1):
        total_checked += 1
        case_number = case_doc.get("case_number", "")
        case_metadata = case_doc.get("metadata", {})
        case_title = (case_metadata.get("caseTitle") or ["N/A"])[0]

        print(f"[{idx}/{len(cases)}] Checking {case_number}: {case_title[:60]}")

        if not case_number:
            print("  ⚠️ No case_number field; skipping")
            continue

        if case_number not in fresh_data:
            print(f"  ⚠️ {case_number} not found in fresh data; skipping")
            continue

        new_case_data: Dict[str, Any] = fresh_data[case_number]

        old_case_data = extract_case_data(case_doc)
        differences = deep_compare(old_case_data, new_case_data)

        if not has_meaningful_changes(differences):
            print("  ✅ No changes detected")
            continue

        total_changed += 1
        meaningful_diffs = [
            d for d in differences
            if not any(tf in d[0] for tf in _TRACKING_FIELDS)
        ]
        changed_fields = {
            (p.split(".")[-1] if "." in p else p) for p, _, _ in meaningful_diffs
        }
        print(f"  🔄 Changes detected: {', '.join(sorted(changed_fields))}")

        # Step 4: Determine email type based on deal_id
        deal_id = case_doc.get("deal_id")
        deal: Optional[Dict[str, Any]] = None
        extra_db_fields: Dict[str, Any] = {}

        if deal_id:
            print(f"  🔗 Case linked to deal_id={deal_id}")
            if deals_collection is not None:
                try:
                    raw_deal = deals_collection.find_one(
                        {"_id": ObjectId(deal_id)})
                    if raw_deal:
                        raw_deal["deal_id"] = str(raw_deal["_id"])
                        deal = raw_deal
                except Exception as e:
                    print(f"  ⚠️ Could not fetch deal: {e}", level="warning")

            send_update_email(case_doc, new_case_data, deal, differences)
        else:
            # No deal_id — try LLM deal matching before falling back to USA email
            print("  🔍 No deal_id — attempting LLM deal match...")
            companies_list = get_companies_from_case_title(new_case_data)

            all_deals = fetch_deals()
            match_result = match_case_to_deal(companies_list, all_deals)

            if match_result:
                matched_deal_id, matched_company, matched_role = match_result
                deal_by_id = {
                    (d.get("deal_id") or str(d.get("_id", ""))): d
                    for d in all_deals
                }
                deal = deal_by_id.get(matched_deal_id)

                if not deal and deals_collection is not None:
                    try:
                        raw_deal = deals_collection.find_one(
                            {"_id": ObjectId(matched_deal_id)})
                        if raw_deal:
                            raw_deal["deal_id"] = str(raw_deal["_id"])
                            deal = raw_deal
                    except Exception as e:
                        print(
                            f"  ⚠️ Could not fetch matched deal: {e}", level="warning")

            if deal:
                print(
                    f"  🎯 Deal match found: deal_id={deal.get('deal_id')} | "
                    f"{matched_company} ({matched_role})"
                )
                extra_db_fields = {
                    "deal_id": deal.get("deal_id"),
                    "matched_company": matched_company,
                    "matched_role": matched_role,
                }
                send_update_email(case_doc, new_case_data, deal, differences)
            else:
                print("  🇺🇸 No deal match found — sending USA-related update email")
                send_update_email(case_doc, new_case_data, None, differences)

        # Step 5: Update record in fs_cases collection
        update_case_document(cases_collection, case_doc,
                             new_case_data, extra_db_fields or None)

    print("\n" + "=" * 60)
    print("📊 Summary:")
    print(f"   Total cases checked : {total_checked}")
    print(f"   Cases with changes  : {total_changed}")
    print("=" * 60 + "\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_fs_case_updates()
