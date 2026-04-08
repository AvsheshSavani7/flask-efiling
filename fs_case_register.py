# isort: skip_file  (sys.path must be set before parent-directory imports)
"""
FS (Foreign Subsidies) Case Register
=====================================

Downloads the EC Foreign Subsidies cases JSON and registers
new cases in the dedicated 'fs_cases' MongoDB collection.

Flow:
1. Download + normalize case JSON from EC FS portal
2. Filter cases (Foreign Subsidies + empty decisions)
3. Fetch all open/unknown deals from MongoDB
4. For each filtered case:
   - Skip if case_number already exists in fs_cases collection
   - LLM call #1: Try to match with existing deals
   - If matched: send [FRMD] email, add deal_id, insert into fs_cases collection
   - LLM call #2 (if no match): Check if USA-related
   - If USA-related: send [FRUD] email, add usa_related=True, insert into fs_cases collection
"""

import sys
import os


import json
import logging
import builtins
from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import requests
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI

from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from llm_verification_service import verify_usa_relation

load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging setup (stdout + file)
# -----------------------------------------------------------------------------
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
LOG_FILE = "fs_case_register.log"
logger = logging.getLogger("fs_case_register")
logger.setLevel(LOG_LEVEL)


if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    """Replacement for print that also logs via the module logger."""
    msg = " ".join(str(a) for a in args)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)
    builtins.print(*args, **kwargs)


print = _logged_print  # type: ignore

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants
ENV_PATH = ".env"
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-FS.json"
CASE_BASE_URL = "https://competition-cases.ec.europa.eu/cases"
BACKUP_JSON = "fs_case_register_backup.json"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
)
CUTOFF_DATE = datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)


# -----------------------------------------------------------------------------
# FS data helpers (copied from fs_case_filter.py — do not import from there)
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


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def matches_criteria(case_data: Dict[str, Any]) -> bool:
    """
    Filter FS cases:
    - caseInstrument contains "Foreign Subsidies"
    - decisions == [] (empty array)
    - caseInitiationDate present and parseable
    """
    metadata = case_data.get("metadata", {})

    if "Foreign Subsidies" not in metadata.get("caseInstrument", []):
        return False

    if case_data.get("decisions", []):
        return False

    case_initiation_dates = metadata.get("caseInitiationDate", [])
    if not case_initiation_dates:
        return False

    initiation_date_str = (
        case_initiation_dates[0]
        if isinstance(case_initiation_dates, list)
        else case_initiation_dates
    )

    initiation_date = parse_date(initiation_date_str)
    if initiation_date is None:
        return False

    # if initiation_date is None or initiation_date < CUTOFF_DATE:
    #     return False

    return True


def get_companies_from_case_title(case_data: Dict[str, Any]) -> List[str]:
    """Build company list from caseTitle only, split by ' / '."""
    metadata = case_data.get("metadata", {})
    case_title_list = metadata.get("caseTitle", [])
    title = case_title_list[0] if case_title_list else ""
    if not title or not isinstance(title, str):
        return []
    return [c.strip() for c in title.split(" / ") if c.strip()]


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


def html_matched_deal_block(deal: Dict[str, Any]) -> str:
    """Banner HTML summarizing the matched MongoDB deal ([FRMD] emails)."""
    target = deal.get("target") or deal.get("target_name") or "N/A"
    acquirer = deal.get("acquirer") or deal.get("acquire_name") or "N/A"
    deal_id = deal.get("deal_id")
    if not deal_id and deal.get("_id") is not None:
        deal_id = str(deal["_id"])
    if not deal_id:
        deal_id = "N/A"

    extra_lines = []
    status = deal.get("deal_status")
    if status is not None and str(status).strip():
        extra_lines.append(
            f'<div style="font-size:13px;color:#1e3a8a;margin-top:8px;">'
            f'<span style="font-weight:700;">Deal status:</span> {escape_html(str(status))}</div>'
        )

    return (
        '<div style="background:#dbeafe;border-radius:8px;padding:16px 18px;margin-bottom:18px;'
        'border-left:4px solid #2563eb;">'
        '<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:8px;">Matched deal</div>'
        '<div style="font-size:14px;color:#1e3a8a;line-height:1.55;">'
        f'<span style="font-weight:700;">Acquirer:</span> {escape_html(str(acquirer))} '
        f'<span style="color:#94a3b8;margin:0 8px;">|</span> '
        f'<span style="font-weight:700;">Target:</span> {escape_html(str(target))} '
        f'<span style="color:#94a3b8;margin:0 8px;">|</span> '
        f'<span style="font-weight:700;">Deal ID:</span> {escape_html(str(deal_id))}'
        "</div>"
        f'{"".join(extra_lines)}'
        "</div>"
    )


def generate_fs_case_email_html(case_data: Dict[str, Any], deal_match: Dict[str, Any]) -> tuple:
    """Generate HTML email for a matched FS case. Returns (subject, html)."""
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

    subject = f"[FRMD] EC Foreign Subsidies Case (New) \u2013 {target} / {acquirer}"
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

    html += html_matched_deal_block(deal_match)

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Companies (case title):</span> '
    if company_list:
        for i, company in enumerate(company_list):
            html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS&caseTitleOrCompanyName={escape_html(company)}" style="color:#2563eb;text-decoration:none;font-weight:700;">{escape_html(company)}</a>'
            if i < len(company_list) - 1:
                html += '<span style="color:#9ca3af;margin:0 8px;">|</span>'
    else:
        html += 'N/A'
    html += '</div>'

    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="{CASE_BASE_URL}/{case_num}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{CASE_BASE_URL}/{case_num}</a><span style="color:#9ca3af;margin-left:6px;">\u2197</span></div>'

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


def generate_unmatched_fs_case_email_html(case_data: Dict[str, Any]) -> tuple:
    """Generate HTML email for an unmatched USA-related FS case. Returns (subject, html)."""
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

    company_list = get_companies_from_case_title(case_data)
    companies_str = " / ".join(company_list) if company_list else "N/A"
    subject = f"[FRUD] EC Foreign Subsidies Case (USA-Related) \u2013 {case_num}: {companies_str}"

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>EC FS Case (USA-Related) - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #059669;color:#059669;background:#fff;">{case_instrument}</span>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #2563eb;color:#2563eb;background:#fff;">FS</span>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #f59e0b;color:#f59e0b;background:#fff;">USA-Related</span>
</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>
<div style="margin-top:18px;">'''

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Companies (case title):</span> '
    if company_list:
        for i, company in enumerate(company_list):
            html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS&caseTitleOrCompanyName={escape_html(company)}" style="color:#2563eb;text-decoration:none;font-weight:700;">{escape_html(company)}</a>'
            if i < len(company_list) - 1:
                html += '<span style="color:#9ca3af;margin:0 8px;">|</span>'
    else:
        html += 'N/A'
    html += '</div>'

    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="{CASE_BASE_URL}/{case_num}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{CASE_BASE_URL}/{case_num}</a><span style="color:#9ca3af;margin-left:6px;">\u2197</span></div>'

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


# -----------------------------------------------------------------------------
# Register-specific helpers
# -----------------------------------------------------------------------------

def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_fs_cases_collection():
    """Get or create the 'fs_cases' collection in the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["fs_cases"]


def filter_cases(data: Dict[str, Any]) -> Dict[str, Any]:
    """Filter FS cases using Foreign Subsidies + empty decisions criteria."""
    filtered: Dict[str, Any] = {}
    for case_number, case_data in data.items():
        if matches_criteria(case_data):
            filtered[case_number] = case_data
    print(f"📊 Filtered {len(filtered)} cases out of {len(data)} total")
    print(f" Length of filtered cases: {len(filtered)}")

    return filtered


def case_exists(collection, case_number: str) -> bool:
    """Check if a case with this case_number already exists in fs_cases."""
    try:
        return collection.count_documents({"case_number": case_number}, limit=1) > 0
    except Exception as e:
        print(f"⚠️ Error checking existing case: {e}", level="warning")
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

        print(f"✅ Fetched {len(deals)} open/unknown deals from MongoDB")
        return deals
    except Exception as e:
        print(f"⚠️ Error fetching deals: {e}", level="warning")
        return []


def match_case_to_deal(
    case_companies: List[str], deals: List[Dict[str, Any]]
) -> Optional[Tuple[str, str, str]]:
    """
    Use LLM to match case companies (from caseTitle) against deals.
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
        res = client.chat.completions.create(
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


def send_email_via_webhook(
    subject: str,
    html_content: str,
    case_number: str,
    case_title: str,
    deal_id: Optional[str] = None,
    usa_related: bool = False,
) -> bool:
    """Send email notification via n8n webhook."""
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "case_number": case_number,
            "case_title": case_title,
            "deal_id": deal_id,
            "usa_related": usa_related,
            "is_new_case": True,
            "case_instrument": "FS",
            "source": "ec_foreign_subsidies_cases",
        }
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  ✅ Email sent! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"  ⚠️ Error sending email: {e}", level="warning")
        return False


def insert_case(collection, case_doc: Dict[str, Any]) -> Optional[str]:
    """Insert a new case document into the fs_cases collection."""
    try:
        result = collection.insert_one(case_doc)
        return str(result.inserted_id)
    except Exception as e:
        print(f"  ⚠️ Error inserting case: {e}", level="error")
        return None


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run_fs_case_register():
    """Main entrypoint for FS cases registration."""
    print("🚀 Starting FS (Foreign Subsidies) Case Register\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}", level="error")
        print("   MongoDB connection is required. Exiting.")
        return
    print(f"✅ {message}\n")

    if not is_connected():
        print("❌ MongoDB not connected. Exiting.", level="error")
        return

    collection = get_fs_cases_collection()
    if collection is None:
        print("❌ Could not access 'fs_cases' collection. Exiting.", level="error")
        return

    # Step 1: Download + normalize JSON
    raw = download_json(DATA_URL)
    data = normalize_fs_data(raw)
    if not data:
        print("❌ No case data after normalization. Exiting.", level="error")
        return

    # Step 2: Filter cases
    filtered_cases = filter_cases(data)
    if not filtered_cases:
        print("⚠️ No cases matched the filter criteria. Exiting.", level="warning")
        return

    # Step 3: Fetch deals
    print("\n📊 Loading deals from MongoDB...")
    deals = fetch_deals()
    if not deals:
        print("⚠️ No open/unknown deals found in MongoDB. Exiting.", level="warning")
        return

    deal_by_id: Dict[str, Dict[str, Any]] = {
        (d.get("deal_id") or str(d.get("_id", ""))): d
        for d in deals
        if d.get("deal_id") or d.get("_id")
    }

    new_cases: List[Dict[str, Any]] = []

    print(f"\n📋 Processing {len(filtered_cases)} filtered FS cases...\n")

    # Step 4: Iterate each filtered case
    for idx, (case_number, case_data) in enumerate(filtered_cases.items(), 1):
        metadata = case_data.get("metadata", {})
        case_title = (metadata.get("caseTitle") or ["N/A"])[0]
        companies_list = get_companies_from_case_title(case_data)
        companies_str = " / ".join(companies_list) if companies_list else ""

        print(f"[{idx}/{len(filtered_cases)}] {case_number}: {case_title}")
        if companies_str:
            print(f"   Companies (case title): {companies_str}")

        # Skip if already in fs_cases collection
        if case_exists(collection, case_number):
            print(f"  ⏩ Case {case_number} already in fs_cases; skipping\n")
            continue

        now_iso = utc_now_iso()

        # LLM Call #1: Try to match with deals
        print(f"  🔍 LLM Call #1: Checking for deal match...")
        match_result = match_case_to_deal(companies_list, deals)

        if match_result:
            matched_deal_id, matched_company, matched_role = match_result
            deal = deal_by_id.get(matched_deal_id)

            if not deal:
                try:
                    deals_coll = get_deals_collection()
                    if deals_coll:
                        raw_deal = deals_coll.find_one(
                            {"_id": ObjectId(matched_deal_id)})
                        if raw_deal:
                            raw_deal["deal_id"] = str(raw_deal["_id"])
                            deal = raw_deal
                except Exception:
                    pass

            if deal:
                print(
                    f"  🎯 Match found: deal_id={matched_deal_id} | {matched_company} ({matched_role})")

                subject, html_email = generate_fs_case_email_html(
                    case_data, deal)
                send_email_via_webhook(
                    subject, html_email,
                    case_number, case_title,
                    deal_id=matched_deal_id,
                )

                case_doc: Dict[str, Any] = {
                    "case_number": case_number,
                    **case_data,
                    "deal_id": matched_deal_id,
                    "matched_company": matched_company,
                    "matched_role": matched_role,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
                inserted_id = insert_case(collection, case_doc)
                if inserted_id:
                    print(f"  ✅ Inserted into fs_cases (id={inserted_id})\n")
                    new_cases.append(
                        {k: v for k, v in case_doc.items() if k != "_id"})
                continue
            else:
                print(
                    f"  ⚠️ LLM returned deal_id={matched_deal_id} but deal not found; proceeding to USA check")

        # LLM Call #2: Check if USA-related
        print(f"  🔍 LLM Call #2: Checking if USA-related...")
        try:
            is_usa = verify_usa_relation(
                company_details=companies_list,
                case_type="FS",
            )
        except Exception as e:
            print(f"  ⚠️ USA relation check error: {e}", level="warning")
            is_usa = False

        if is_usa:
            print(f"  🇺🇸 USA-related case detected")

            subject, html_email = generate_unmatched_fs_case_email_html(
                case_data)
            send_email_via_webhook(
                subject, html_email,
                case_number, case_title,
                usa_related=True,
            )

            case_doc = {
                "case_number": case_number,
                **case_data,
                "usa_related": True,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            inserted_id = insert_case(collection, case_doc)
            if inserted_id:
                print(f"  ✅ Inserted into fs_cases (id={inserted_id})\n")
                new_cases.append(
                    {k: v for k, v in case_doc.items() if k != "_id"})
        else:
            print(f"  ℹ️ Not matched and not USA-related; skipping\n")

    # Save backup JSON
    if new_cases:
        try:
            with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                json.dump(new_cases, f, indent=2,
                          ensure_ascii=False, default=str)
            print(
                f"\n💾 Saved {len(new_cases)} new cases to backup: {BACKUP_JSON}")
        except Exception as e:
            print(f"⚠️ Error writing backup JSON: {e}", level="warning")

    print(
        f"\n🎉 FS Case Register finished — {len(new_cases)} new case(s) inserted")


if __name__ == "__main__":
    run_fs_case_register()
