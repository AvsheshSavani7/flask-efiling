"""
EC Case Register
================

Downloads the European Commission Competition cases JSON and registers
new merger cases in the dedicated 'ec_cases' MongoDB collection.

Flow:
1. Download case JSON from EC Competition Cases portal
2. Filter cases (Merger + Antitrust + empty decisions)
3. Fetch all open/unknown deals from MongoDB
4. For each filtered case:
   - Skip if case_number already exists in ec_cases collection
   - LLM call #1: Try to match with existing deals
   - If matched: send [FRMD] email, add deal_id, insert into ec_cases collection
   - LLM call #2 (if no match): Check if USA-related
   - If USA-related: send [FRUD] email, add usa_related=True, insert into ec_cases collection
"""

# isort: skip_file  (sys.path must be set before parent-directory imports)

from llm_verification_service import verify_usa_relation
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
from datetime import datetime, timedelta
import json
import logging
import builtins
import sys
import os


load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging setup (stdout + file)
# -----------------------------------------------------------------------------
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
LOG_FILE = "ec_case_register.log"

logger = logging.getLogger("ec_case_register")
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
DATA_URL = "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-M.json"
BACKUP_JSON = "ec_case_register_backup.json"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
)

CUTOFF_DATE = datetime.now() - timedelta(days=2)

# -----------------------------------------------------------------------------
# EC data helpers (copied from ec_case_filter.py — do not import from there)
# -----------------------------------------------------------------------------


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
        print(f"❌ Error downloading data: {e}", level="error")
        raise
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing JSON: {e}", level="error")
        raise


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def matches_criteria(case_data: Dict[str, Any]) -> bool:
    """
    Check if a case matches all filtering criteria:
    - caseInstrument: ["Merger"]
    - caseCartel: ["Antitrust"]
    - decisions: [] (empty array)
    - caseInitiationDate present and parseable
    """
    metadata = case_data.get("metadata", {})

    if "Merger" not in metadata.get("caseInstrument", []):
        return False

    if "Antitrust" not in metadata.get("caseCartel", []):
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

    if initiation_date < CUTOFF_DATE:
        return False

    return True


def format_date(date_str: str) -> str:
    """Format date from YYYY-MM-DD to DD.MM.YYYY."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return date_str


def parse_json_field(field_value: str) -> Dict[str, Any]:
    """Parse a JSON string field into a dict."""
    try:
        if isinstance(field_value, str) and field_value.startswith("{"):
            return json.loads(field_value)
        return {}
    except Exception:
        return {}


def get_oj_prior_publication(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Get OJ prior publication info from decisions where decisionOjPriorPublication is true."""
    for decision in decisions:
        metadata = decision.get("metadata", {})
        oj_prior = metadata.get("decisionOjPriorPublication", [])
        if oj_prior and str(oj_prior[0]).lower() == "true":
            oj_pubs = metadata.get("decisionOfficialJournalPublications", [])
            if oj_pubs:
                try:
                    pub_data = json.loads(oj_pubs[0]) if isinstance(
                        oj_pubs[0], str) else oj_pubs[0]
                    items = pub_data.get("items", [])
                    if items:
                        return {
                            "reference": items[0].get("reference", ""),
                            "publishedDate": items[0].get("publishedDate", ""),
                            "priorPublication": items[0].get("priorPublication", "true"),
                        }
                except Exception:
                    pass
            pub_dates = metadata.get(
                "decisionOfficialJournalPublicationsPublishedDates", [])
            if pub_dates:
                return {"reference": "", "publishedDate": pub_dates[0], "priorPublication": "true"}
    return {}


def generate_ec_case_email_html(case_data: Dict[str, Any], deal_match: Dict[str, Any]) -> tuple:
    """Generate HTML email for a matched EC case. Returns (subject, html)."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")

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

    deal_id = deal_match.get("deal_id") or str(deal_match.get("_id", "N/A"))
    case_url = f"https://competition-cases.ec.europa.eu/cases/{case_num if case_num != 'N/A' else ''}"

    subject = f"[FRMD] EC Merger Case (New) \u2013 {target} / {acquirer}"

    deal_banner = (
        '<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;'
        'margin:20px 28px 0 28px;border-left:4px solid #2563eb;">'
        '<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>'
        '<div style="font-size:14px;color:#1e3a8a;">'
        f'<span style="font-weight:700;">Acquirer:</span> {acquirer}'
        '<span style="color:#94a3b8;margin:0 8px;">|</span>'
        f'<span style="font-weight:700;">Target:</span> {target}'
        '<span style="color:#94a3b8;margin:0 8px;">|</span>'
        f'<span style="font-weight:700;">Deal ID:</span> {deal_id}'
        '</div>'
        '<div style="margin-top:10px;">'
        f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;'
        'font-weight:700;font-size:14px;">View EC Case \u2192</a>'
        '</div></div>'
    )

    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC Case Match - {case_num}</title>
</head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
{deal_banner}
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #ef4444;color:#ef4444;background:#fff;">{case_instrument}</span>'''

    if case_simplified:
        html += f'<div style="margin-left:2px;font-size:14px;color:#6b7280;font-style:italic;">{case_simplified}</div>'

    html += f'''</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>
<div style="margin-top:18px;">'''

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

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="https://competition-cases.ec.europa.eu/cases/{case_num}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">https://competition-cases.ec.europa.eu/cases/{case_num}</a><span style="color:#9ca3af;margin-left:6px;">\u2197</span>'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Last decision date:</span> '
    html += '<span style="font-weight:800;">'
    html += format_date(last_decision_date[0]) if last_decision_date else 'N/A'
    html += '</span></div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Regulation:</span> '
    html += case_regulation[0] if case_regulation else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Notification date:</span> '
    html += format_date(notification_date[0]) if notification_date else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Provisional deadline:</span> '
    html += format_date(deadline_date[0]) if deadline_date else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Economic activities:</span> '
    if case_sectors:
        for sector_str in case_sectors:
            sector_data = parse_json_field(sector_str)
            if sector_data:
                code = sector_data.get("code", "")
                label = sector_data.get("label", "")
                if code and label:
                    sector_code = code.replace("NaceV2Sector_", "*")
                    html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=M&caseSectors={sector_code}&sortField=caseLastDecisionDate&sortOrder=DESC" style="color:#2563eb;text-decoration:none;font-weight:700;">{label}</a>'
                    html += '<span style="color:#6b7280;"> (NACE Rev. 2.1)</span>'
    else:
        html += 'N/A'
    html += '</div>'

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
                html += '<span style="color:#9ca3af;margin:0 6px;">\u2197</span>'
            else:
                html += f'OJEU {ref}'
        if oj_info.get("publishedDate"):
            html += f'<span style="color:#111827;"> of {format_date(oj_info["publishedDate"])}</span>'
        html += '</div>'

    html += '</div></div>'

    if decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += '<div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Decisions</div>'
        for decision in decisions:
            decision_metadata = decision.get("metadata", {})
            decision_types = decision_metadata.get("decisionTypes", [])
            decision_adoption_date = decision_metadata.get(
                "decisionAdoptionDate", [])
            if decision_types or decision_adoption_date:
                html += '<div style="padding:14px 0;"><div style="font-size:14px;color:#111827;">'
                if decision_types:
                    dt_data = parse_json_field(decision_types[0])
                    if dt_data:
                        html += f'<span style="font-weight:900;">{dt_data.get("label", "")}</span>'
                if decision_adoption_date:
                    html += f'<span style="color:#6b7280;"> of {format_date(decision_adoption_date[0])}</span>'
                html += '</div>'
                decision_attachments = decision.get("decisionAttachments", [])
                press_releases = decision_metadata.get(
                    "decisionPressReleases", [])
                if decision_attachments or press_releases:
                    html += '<div style="margin-top:10px;">'
                    if decision_attachments:
                        html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                        html += '<span style="color:#6b7280;">Decision text(s):</span> '
                        for attachment in decision_attachments:
                            att_meta = attachment.get("metadata", {})
                            att_link = att_meta.get("attachmentLink", [""])[0]
                            att_lang = att_meta.get(
                                "attachmentLanguage", ["EN"])[0]
                            att_pub_date = att_meta.get(
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
                            for i, item in enumerate(pr_data.get("items", [])):
                                ref = item.get("reference", "")
                                if i > 0:
                                    html += ' '
                                html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={ref}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{ref}</a>'
                                html += '<span style="color:#9ca3af;margin-left:4px;">\u2197</span>'
                        except Exception:
                            pass
                        html += '</div>'
                    html += '</div>'
                html += '</div>'
        html += '</div>'

    if case_attachments:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += '<div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Other case related information</div>'
        for attachment in case_attachments:
            att_meta = attachment.get("metadata", {})
            att_category = att_meta.get("attachmentCategory", [""])[0]
            att_sent_date = att_meta.get("attachmentSentDate", [""])[0]
            att_link = att_meta.get("attachmentLink", [""])[0]
            att_pub_date = att_meta.get(
                "attachmentPublicationBusinessDate", [""])[0]
            att_lang = att_meta.get("attachmentLanguage", ["EN"])[0]
            if att_category:
                html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                html += f'<span style="color:#6b7280;">{att_category}'
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

    html += '</div>\n</body>\n</html>'
    return subject, html


def generate_unmatched_ec_case_email_html(case_data: Dict[str, Any]) -> tuple:
    """Generate HTML email for an unmatched USA-related EC case. Returns (subject, html)."""
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

    companies_str = " / ".join(case_companies) if case_companies else "N/A"
    subject = f"[FRUD] EC Merger Case (USA-Related) \u2013 {case_num}: {companies_str}"

    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC Case (USA-Related) - {case_num}</title>
</head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #ef4444;color:#ef4444;background:#fff;">{case_instrument}</span>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #f59e0b;color:#f59e0b;background:#fff;">USA-Related</span>'''

    if case_simplified:
        html += f'<div style="margin-left:2px;font-size:14px;color:#6b7280;font-style:italic;">{case_simplified}</div>'

    html += f'''</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>
<div style="margin-top:18px;">'''

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

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Case URL:</span> '
    html += f'<a href="https://competition-cases.ec.europa.eu/cases/{case_num}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">https://competition-cases.ec.europa.eu/cases/{case_num}</a><span style="color:#9ca3af;margin-left:6px;">\u2197</span>'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Last decision date:</span> '
    html += '<span style="font-weight:800;">'
    html += format_date(last_decision_date[0]) if last_decision_date else 'N/A'
    html += '</span></div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Regulation:</span> '
    html += case_regulation[0] if case_regulation else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Notification date:</span> '
    html += format_date(notification_date[0]) if notification_date else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Provisional deadline:</span> '
    html += format_date(deadline_date[0]) if deadline_date else 'N/A'
    html += '</div>'

    html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
    html += '<span style="color:#6b7280;">Economic activities:</span> '
    if case_sectors:
        for sector_str in case_sectors:
            sector_data = parse_json_field(sector_str)
            if sector_data:
                code = sector_data.get("code", "")
                label = sector_data.get("label", "")
                if code and label:
                    sector_code = code.replace("NaceV2Sector_", "*")
                    html += f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=M&caseSectors={sector_code}&sortField=caseLastDecisionDate&sortOrder=DESC" style="color:#2563eb;text-decoration:none;font-weight:700;">{label}</a>'
                    html += '<span style="color:#6b7280;"> (NACE Rev. 2.1)</span>'
    else:
        html += 'N/A'
    html += '</div>'

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
                html += '<span style="color:#9ca3af;margin:0 6px;">\u2197</span>'
            else:
                html += f'OJEU {ref}'
        if oj_info.get("publishedDate"):
            html += f'<span style="color:#111827;"> of {format_date(oj_info["publishedDate"])}</span>'
        html += '</div>'

    html += '</div></div>'

    if decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += '<div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Decisions</div>'
        for decision in decisions:
            decision_metadata = decision.get("metadata", {})
            decision_types = decision_metadata.get("decisionTypes", [])
            decision_adoption_date = decision_metadata.get(
                "decisionAdoptionDate", [])
            if decision_types or decision_adoption_date:
                html += '<div style="padding:14px 0;"><div style="font-size:14px;color:#111827;">'
                if decision_types:
                    dt_data = parse_json_field(decision_types[0])
                    if dt_data:
                        html += f'<span style="font-weight:900;">{dt_data.get("label", "")}</span>'
                if decision_adoption_date:
                    html += f'<span style="color:#6b7280;"> of {format_date(decision_adoption_date[0])}</span>'
                html += '</div>'
                decision_attachments = decision.get("decisionAttachments", [])
                press_releases = decision_metadata.get(
                    "decisionPressReleases", [])
                if decision_attachments or press_releases:
                    html += '<div style="margin-top:10px;">'
                    if decision_attachments:
                        html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                        html += '<span style="color:#6b7280;">Decision text(s):</span> '
                        for attachment in decision_attachments:
                            att_meta = attachment.get("metadata", {})
                            att_link = att_meta.get("attachmentLink", [""])[0]
                            att_lang = att_meta.get(
                                "attachmentLanguage", ["EN"])[0]
                            att_pub_date = att_meta.get(
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
                            for i, item in enumerate(pr_data.get("items", [])):
                                ref = item.get("reference", "")
                                if i > 0:
                                    html += ' '
                                html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={ref}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{ref}</a>'
                                html += '<span style="color:#9ca3af;margin-left:4px;">\u2197</span>'
                        except Exception:
                            pass
                        html += '</div>'
                    html += '</div>'
                html += '</div>'
        html += '</div>'

    if case_attachments:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += '<div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Other case related information</div>'
        for attachment in case_attachments:
            att_meta = attachment.get("metadata", {})
            att_category = att_meta.get("attachmentCategory", [""])[0]
            att_sent_date = att_meta.get("attachmentSentDate", [""])[0]
            att_link = att_meta.get("attachmentLink", [""])[0]
            att_pub_date = att_meta.get(
                "attachmentPublicationBusinessDate", [""])[0]
            att_lang = att_meta.get("attachmentLanguage", ["EN"])[0]
            if att_category:
                html += '<div style="font-size:14px;color:#111827;margin-bottom:10px;">'
                html += f'<span style="color:#6b7280;">{att_category}'
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

    html += '</div>\n</body>\n</html>'
    return subject, html


# -----------------------------------------------------------------------------
# Register-specific helpers
# -----------------------------------------------------------------------------

def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_ec_cases_collection():
    """Get or create the 'ec_cases' collection in the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["ec_cases"]


def filter_cases(data: Dict[str, Any]) -> Dict[str, Any]:
    """Filter EC cases using Merger + Antitrust + empty decisions criteria."""
    filtered: Dict[str, Any] = {}
    for case_number, case_data in data.items():
        if matches_criteria(case_data):
            filtered[case_number] = case_data
    print(f"📊 Filtered {len(filtered)} cases out of {len(data)} total")
    return filtered


def case_exists(collection, case_number: str) -> bool:
    """Check if a case with this case_number already exists in ec_cases."""
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

        # Add deal_id string field so email generators can use it
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
    Use LLM to match case companies against deals.
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

    prompt = f"""You are an M&A deal analyst. Given the company names from an EC merger case, determine whether any of these companies match any of the deals listed below.

DEALS TO MATCH:
{chr(10).join(lines)}

CASE COMPANIES:
{companies_str}

INSTRUCTIONS:
1. Compare the case companies with BOTH Target and Acquirer names in the deals list.
2. When matching, also consider target_aliases and parent_aliases.
3. Look for EXACT matches, partial matches, or name variations.
4. Accept suffix variations (Inc., Ltd., PLC, AG, SA, NV, Corporation, Corp.).

RESPONSE FORMAT:
- If you find a match, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|General Motors|acquirer

- If NO match is found, respond with:
  None
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
            "source": "ec_competition_cases",
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
    """Insert a new case document into the ec_cases collection."""
    try:
        result = collection.insert_one(case_doc)
        return str(result.inserted_id)
    except Exception as e:
        print(f"  ⚠️ Error inserting case: {e}", level="error")
        return None


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run_ec_case_register():
    """Main entrypoint for EC cases registration."""
    print("🚀 Starting EC Case Register\n")

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

    collection = get_ec_cases_collection()
    if collection is None:
        print("❌ Could not access 'ec_cases' collection. Exiting.", level="error")
        return

    # Step 1: Download JSON
    data = download_json(DATA_URL)
    if not data:
        print("❌ Failed to download case data. Exiting.", level="error")
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

    print(f"\n📋 Processing {len(filtered_cases)} filtered cases...\n")

    # Step 4: Iterate each filtered case
    for idx, (case_number, case_data) in enumerate(filtered_cases.items(), 1):
        metadata = case_data.get("metadata", {})
        case_title = (metadata.get("caseTitle") or ["N/A"])[0]
        case_companies_raw = metadata.get("caseCompanies") or []
        companies_str = case_companies_raw[0] if case_companies_raw else ""
        companies_list = [c.strip()
                          for c in companies_str.split("/") if c.strip()]

        print(f"[{idx}/{len(filtered_cases)}] {case_number}: {case_title}")
        if companies_str:
            print(f"   Companies: {companies_str}")
            print(
                f"   Initiation date: {metadata.get('caseInitiationDate', ['N/A'])[0]},CUTOFF_DATE: {CUTOFF_DATE}")

        # Skip if already in ec_cases collection
        if case_exists(collection, case_number):
            print(f"  ⏩ Case {case_number} already in ec_cases; skipping\n")
            continue

        now_iso = utc_now_iso()

        # LLM Call #1: Try to match with deals
        print(f"  🔍 LLM Call #1: Checking for deal match...")
        match_result = match_case_to_deal(companies_list, deals)

        if match_result:
            matched_deal_id, matched_company, matched_role = match_result
            deal = deal_by_id.get(matched_deal_id)

            # If LLM returned an id not in cached list, fetch directly from MongoDB
            if not deal:
                try:
                    deals_coll = get_deals_collection()
                    if deals_coll:
                        raw = deals_coll.find_one(
                            {"_id": ObjectId(matched_deal_id)})
                        if raw:
                            raw["deal_id"] = str(raw["_id"])
                            deal = raw
                except Exception:
                    pass

            if deal:
                print(
                    f"  🎯 Match found: deal_id={matched_deal_id} | {matched_company} ({matched_role})")

                subject, html_email = generate_ec_case_email_html(
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
                    print(f"  ✅ Inserted into ec_cases (id={inserted_id})\n")
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
                case_type="EC",
            )
        except Exception as e:
            print(f"  ⚠️ USA relation check error: {e}", level="warning")
            is_usa = False

        if is_usa:
            print(f"  🇺🇸 USA-related case detected")

            subject, html_email = generate_unmatched_ec_case_email_html(
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
                print(f"  ✅ Inserted into ec_cases (id={inserted_id})\n")
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
        f"\n🎉 EC Case Register finished — {len(new_cases)} new case(s) inserted")


if __name__ == "__main__":
    run_ec_case_register()
