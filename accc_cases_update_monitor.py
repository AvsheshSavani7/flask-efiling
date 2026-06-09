import os
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from logging.handlers import RotatingFileHandler

from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from mongodb_connection import (
    get_database,
    get_deals_collection,
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from llm_verification_service import verify_usa_relation
from accc_cases_register import match_case_to_deal
from log_utils import cleanup_old_logs, refresh_log_file
from scraper_error_utils import collect_error, send_error_summary
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload, resolve_webhook_url


load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# Timestamps in IST (UTC+5:30)
# -----------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "australia_cases_update_monitor"
LOGGER_NAME = "accc_cases_update_monitor"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

# Create module logger
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Avoid adding handlers multiple times if module is reloaded
if not logger.handlers:
    class _ISTFormatter(logging.Formatter):
        def converter(self, timestamp):
            return datetime.fromtimestamp(timestamp, tz=IST)

        def formatTime(self, record, datefmt=None):
            ct = self.converter(record.created)
            if datefmt:
                return ct.strftime(datefmt)
            return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")

    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


# OpenAI client for LLM matching
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants
ENV_PATH = ".env"

# Residential proxy configuration
PROXY_HOST = "108.59.242.138"
PROXY_PORT = 46885
PROXY_USERNAME = "GSenAgrfKhuNWkd"
PROXY_PASSWORD = "8lmVa5yl0pKp9MI"

def get_accc_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["accc_cases"]


def extract_text(element) -> str:
    if not element:
        return ""
    try:
        return element.inner_text().strip()
    except Exception:
        return ""


def fetch_current_case_from_page(page, url: str) -> Optional[Dict[str, Any]]:
    """
    Reuse the same parsing logic as in accc_cases_register to build
    a fresh snapshot of the case from the detail page.
    """
    try:
        logger.info(f"    Fetching detail page: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        case: Dict[str, Any] = {"url": url}

        # Title
        try:
            title_elem = page.query_selector(
                "h1.page-title span.field--name-title")
            if title_elem:
                case["title"] = extract_text(title_elem)
        except Exception:
            pass

        # Summary fields
        try:
            status_elem = page.query_selector(
                ".field--name-field-acccgov-merger-status .field__item"
            )
            if status_elem:
                case["acquisition_status"] = extract_text(status_elem)

            case_number_elem = page.query_selector(
                ".field--name-dynamic-token-fieldnode-acccgov-merger-id .field__item"
            )
            if not case_number_elem:
                page.wait_for_timeout(5000)
                case_number_elem = page.query_selector(
                    ".field--name-dynamic-token-fieldnode-acccgov-merger-id .field__item"
                )
            if case_number_elem:
                case["case_number"] = extract_text(case_number_elem)

            type_elem = page.query_selector(
                ".field--acccgov-type .field__item")
            if type_elem:
                case["type"] = extract_text(type_elem)

            date_elem = page.query_selector(
                ".field--name-field-acccgov-pub-reg-date .field__item time"
            )
            if date_elem:
                case["effective_notification_date"] = extract_text(date_elem)
        except Exception as e:
            logger.warning(f"    Error extracting summary fields: {e}")

        # Status block
        status_info: Dict[str, Any] = {}
        try:
            stage_elem = page.query_selector(
                ".field--name-field-acquisition-stage .field__item"
            )
            if stage_elem:
                status_info["stage"] = extract_text(stage_elem)

            end_period_elem = page.query_selector(
                ".field--name-field-acccgov-end-determination .field__item time"
            )
            if end_period_elem:
                status_info["end_of_determination_period"] = extract_text(
                    end_period_elem
                )

            determination_elem = page.query_selector(
                ".field--name-field-acccgov-acquisition-deter .field__item"
            )
            if determination_elem:
                status_info["accc_determination"] = extract_text(
                    determination_elem)

            pub_date_elem = page.query_selector(
                ".field--name-field-acccgov-pub-reg-end-date .field__item time"
            )
            if pub_date_elem:
                status_info["determination_publication_date"] = extract_text(
                    pub_date_elem
                )
        except Exception as e:
            logger.warning(f"    Error extracting status section: {e}")

        if status_info:
            case["status"] = status_info

        # About the acquisition
        about: Dict[str, Any] = {}
        try:
            # Acquirers
            acquirers: List[Dict[str, Any]] = []
            acq_section = page.query_selector(
                ".field--name-field-acccgov-applicants")
            if acq_section:
                company_elements = acq_section.query_selector_all(
                    ".paragraph--type--acccgov-trader"
                )
                for elem in company_elements:
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    company: Dict[str, Any] = {}
                    if name_elem:
                        company["name"] = extract_text(name_elem)
                    if reg_elem:
                        company["registration"] = extract_text(reg_elem)
                    if company:
                        acquirers.append(company)
            if acquirers:
                about["acquirers"] = acquirers

            # Targets
            targets: List[Dict[str, Any]] = []
            tgt_section = page.query_selector(
                ".field--name-field-acccgov-pub-reg-targets"
            )
            if tgt_section:
                company_elements = tgt_section.query_selector_all(
                    ".paragraph--type--acccgov-trader"
                )
                for elem in company_elements:
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    company = {}
                    if name_elem:
                        company["name"] = extract_text(name_elem)
                    if reg_elem:
                        company["registration"] = extract_text(reg_elem)
                    if company:
                        targets.append(company)
            if targets:
                about["targets"] = targets

            # Other parties
            others: List[Dict[str, Any]] = []
            other_section = page.query_selector(
                ".field--name-field-acccgov-other-parties"
            )
            if other_section:
                company_elements = other_section.query_selector_all(
                    ".paragraph--type--acccgov-trader"
                )
                for elem in company_elements:
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    company = {}
                    if name_elem:
                        company["name"] = extract_text(name_elem)
                    if reg_elem:
                        company["registration"] = extract_text(reg_elem)
                    if company:
                        others.append(company)
            if others:
                about["other_parties"] = others

            # ANZSIC
            anzsic_elem = page.query_selector(
                ".field--name-field-acquisition-anzsic-code .field__item"
            )
            if anzsic_elem:
                about["anzsic_codes"] = extract_text(anzsic_elem)

            # Description
            desc_elem = page.query_selector(
                ".field--name-field-accc-body .full-text, "
                ".field--name-field-accc-body .summary-text"
            )
            if desc_elem:
                try:
                    read_more = page.query_selector(
                        ".field--name-field-accc-body .read-toggle"
                    )
                    if read_more:
                        read_more.click()
                        page.wait_for_timeout(500)
                        desc_elem = page.query_selector(
                            ".field--name-field-accc-body .full-text"
                        ) or desc_elem
                except Exception:
                    pass
                about["description"] = extract_text(desc_elem)
        except Exception as e:
            logger.warning(f"    Error extracting About the acquisition: {e}")

        if about:
            case["about_the_acquisition"] = about

        # Decisions and key events + consultation
        events: List[Dict[str, Any]] = []
        try:
            consult_rows = page.query_selector_all(
                ".field--name-field-acccgov-consultations table tbody tr"
            )
            for row in consult_rows:
                try:
                    date_elem = row.query_selector("time")
                    desc_elem = row.query_selector("td:nth-child(2)")
                    link_elem = row.query_selector(
                        "a[href$='.docx'], a[href$='.pdf'], a[href$='.doc']"
                    )
                    ev: Dict[str, Any] = {}
                    if date_elem:
                        ev["date"] = extract_text(date_elem)
                    if desc_elem:
                        ev["description"] = extract_text(desc_elem)
                    if link_elem:
                        href = link_elem.get_attribute("href")
                        if href and not href.startswith("http"):
                            href = "https://www.accc.gov.au" + href
                        ev["attachment_url"] = href
                        size_elem = link_elem.query_selector("span.badge")
                        if size_elem:
                            ev["attachment_size"] = extract_text(size_elem)
                    if ev.get("description"):
                        events.append(ev)
                except Exception:
                    continue

            event_rows = page.query_selector_all(
                ".field--name-field-acccgov-merger-events table tbody tr"
            )
            for row in event_rows:
                try:
                    date_elem = row.query_selector(
                        "td.acccgov-timeline__date time")
                    desc_elem = row.query_selector("td:nth-child(2)")
                    link_elem = row.query_selector(
                        "td.acccgov-timeline__file-link a")
                    ev = {}
                    if date_elem:
                        ev["date"] = extract_text(date_elem)
                    if desc_elem:
                        ev["description"] = extract_text(desc_elem)
                    if link_elem:
                        href = link_elem.get_attribute("href")
                        if href and not href.startswith("http"):
                            href = "https://www.accc.gov.au" + href
                        ev["attachment_url"] = href
                        size_elem = link_elem.query_selector("span.badge")
                        if size_elem:
                            ev["attachment_size"] = extract_text(size_elem)
                    if ev.get("description"):
                        events.append(ev)
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"    Error extracting decisions/events: {e}")

        if events:
            case["decisions_and_key_events"] = events

        if not case.get("case_number"):
            logger.warning("    No case_number on detail page; skipping")
            return None

        return case
    except Exception as e:
        logger.error(f"    Error fetching detail page {url}: {e}")
        return None


def normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def detect_changes(
    old_case: Dict[str, Any], new_case: Dict[str, Any]
) -> List[Tuple[str, Any, Any, str]]:
    """
    Detect key changes between stored case (from accc_cases) and freshly scraped case.

    Returns list of (field_label, old_value, new_value, change_type).
    """
    changes: List[Tuple[str, Any, Any, str]] = []
    logger.info(f"[STEP 1.8] Detect changes: old_case: {old_case}")
    logger.info(f"[STEP 1.9] Detect changes: new_case: {new_case}")

    # Recursive diff for arbitrary nested structures (dicts/lists/scalars)
    def diff_recursive(path: str, old: Any, new: Any):
        # Normalize scalars
        o_norm = normalize_value(old)
        n_norm = normalize_value(new)

        # Dict vs dict
        if isinstance(o_norm, dict) and isinstance(n_norm, dict):
            old_keys = set(o_norm.keys())
            new_keys = set(n_norm.keys())

            # New keys
            for k in sorted(new_keys - old_keys):
                key_path = f"{path}.{k}" if path else k
                changes.append((key_path, None, n_norm.get(k), "new"))

            # Removed keys
            for k in sorted(old_keys - new_keys):
                key_path = f"{path}.{k}" if path else k
                changes.append((key_path, o_norm.get(k), None, "removed"))

            # Common keys: recurse
            for k in sorted(old_keys & new_keys):
                key_path = f"{path}.{k}" if path else k
                diff_recursive(key_path, o_norm.get(k), n_norm.get(k))
            return

        # List vs list
        if isinstance(o_norm, list) and isinstance(n_norm, list):
            max_len = max(len(o_norm), len(n_norm))
            for i in range(max_len):
                idx_path = f"{path}[{i}]" if path else f"[{i}]"
                if i >= len(o_norm):
                    # New item
                    changes.append((idx_path, None, n_norm[i], "new"))
                elif i >= len(n_norm):
                    # Removed item
                    changes.append((idx_path, o_norm[i], None, "removed"))
                else:
                    # Recurse into item
                    diff_recursive(idx_path, o_norm[i], n_norm[i])
            return

        # Scalars or type-changed values
        if o_norm != n_norm:
            label = path or "value"
            change_type = "updated"
            if o_norm is None and n_norm is not None:
                change_type = "new"
            elif o_norm is not None and n_norm is None:
                change_type = "removed"
            changes.append((label, o_norm, n_norm, change_type))

    # Only compare selected top-level keys; everything else is ignored.
    allowed_keys = {
        "acquisition_status",
        "type",
        "effective_notification_date",
        "status",
        "about_the_acquisition",
        "decisions_and_key_events",
    }

    old_filtered = {k: v for k, v in old_case.items() if k in allowed_keys}
    new_filtered = {k: v for k, v in new_case.items() if k in allowed_keys}

    # Start recursive diff within the allowed subset
    diff_recursive("", old_filtered, new_filtered)

    return changes


def build_change_summary(changes: List[Tuple[str, Any, Any, str]]) -> str:
    lines = []
    for label, old_val, new_val, change_type in changes:
        if label == "Decisions and key events":
            count = len(new_val) if isinstance(new_val, list) else 0
            lines.append(f"- {label}: {count} new event(s)")
        else:
            old_disp = "N/A" if old_val is None else str(old_val)
            new_disp = "N/A" if new_val is None else str(new_val)
            tag = "NEW" if change_type == "new" else "UPDATED"
            lines.append(f"- {label}: {old_disp} → {new_disp} ({tag})")
    return "\n".join(lines)


def generate_update_email_html(
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any, str]],
    is_usa: bool = False,
) -> str:
    """
    Generate HTML email for ACCC case update, mirroring the rich layout from
    accc_case_update_monitor.py but reading from the accc_cases schema.
    """
    # Basic case info
    case_number = new_case.get(
        "case_number", old_case.get("case_number", "N/A"))
    title = new_case.get("title", old_case.get("title", "N/A"))
    acquisition_status = new_case.get("acquisition_status", "N/A")
    case_type = new_case.get("type", "N/A")
    notification_date = new_case.get("effective_notification_date", "N/A")
    detail_url = new_case.get("url", "")

    status = new_case.get("status", {}) or {}
    stage = status.get("stage", "N/A")
    determination_pub_date = status.get("determination_publication_date", "")
    accc_determination = status.get("accc_determination", "")

    about = new_case.get("about_the_acquisition", {}) or {}
    acquirers = about.get("acquirers", [])
    targets = about.get("targets", [])
    other_parties = about.get("other_parties", [])
    anzsic = about.get("anzsic_codes", "")
    description = about.get("description", "")

    decisions_events = new_case.get("decisions_and_key_events", [])

    # Determine status badge color
    status_color = "#1e1b4b"
    if isinstance(acquisition_status, str):
        lower = acquisition_status.lower()
        if "assessment completed" in lower:
            status_color = "#14b8a6"
        elif "under assessment" in lower:
            status_color = "#1e1b4b"
        elif "not opposed" in lower:
            status_color = "#059669"
        elif "withdrawn" in lower:
            status_color = "#6b7280"

    # Build change map: field_name/path -> (change_type, new_value)
    changed_fields: Dict[str, Tuple[str, Any]] = {}
    change_summary: List[str] = []

    for field_path, _old, new_val, change_type in changes:
        # Store raw path
        changed_fields[field_path] = (change_type, new_val)
        change_summary.append(field_path)

        # Also register friendly aliases for known fields so change_flag()
        # can decorate the detailed sections.
        # Top-level
        if field_path == "acquisition_status":
            changed_fields["Acquisition status"] = (change_type, new_val)
        elif field_path == "title":
            changed_fields["Title"] = (change_type, new_val)
        elif field_path == "type":
            changed_fields["Type"] = (change_type, new_val)
        elif field_path == "effective_notification_date":
            changed_fields["Effective notification date"] = (
                change_type, new_val)

        # Status block
        elif field_path == "status.stage":
            changed_fields["Stage"] = (change_type, new_val)
        elif field_path == "status.end_of_determination_period":
            changed_fields["End of determination period"] = (
                change_type, new_val)
        elif field_path == "status.accc_determination":
            changed_fields["ACCC Determination"] = (change_type, new_val)
        elif field_path == "status.determination_publication_date":
            changed_fields["Determination publication date"] = (
                change_type, new_val)

        # About the acquisition
        elif field_path.startswith("about_the_acquisition.acquirers"):
            changed_fields["Acquirer(s)"] = (change_type, new_val)
        elif field_path.startswith("about_the_acquisition.targets"):
            changed_fields["Target(s) or Vendor(s)"] = (change_type, new_val)
        elif field_path.startswith("about_the_acquisition.other_parties"):
            changed_fields["Other party(ies)"] = (change_type, new_val)
        elif field_path == "about_the_acquisition.anzsic_codes":
            changed_fields["ANZSIC code(s)"] = (change_type, new_val)
        elif field_path == "about_the_acquisition.description":
            changed_fields["Description"] = (change_type, new_val)

    # Deal / USA-related info
    acquirer = ""
    target = ""
    deal_id_str = ""
    if deal:
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        target = deal.get("target") or deal.get("target_name", "N/A")
        # Prefer native _id, fall back to any stored deal_id string
        if deal.get("_id"):
            try:
                deal_id_str = str(deal["_id"])
            except Exception:
                deal_id_str = str(deal.get("_id"))
        else:
            deal_id_str = str(deal.get("deal_id", ""))
    # usa_related = bool(new_case.get("usa_related")
    #                    or old_case.get("usa_related"))

    # Helper to display change flags for high-level fields
    def change_flag(field_label: str) -> str:
        if field_label in changed_fields:
            change_type, _ = changed_fields[field_label]
            if change_type == "new":
                return ' <span style="color:#10b981;font-size:0.85em;font-weight:700;margin-left:6px;">(new)</span>'
            if change_type == "updated":
                return ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;margin-left:6px;">(updated)</span>'
        return ""

    # Helper to display change flag for a specific nested path prefix
    def path_change_flag(path_prefix: str) -> str:
        for key, (change_type, _val) in changed_fields.items():
            if isinstance(key, str) and key.startswith(path_prefix):
                if change_type == "new":
                    return ' <span style="color:#10b981;font-size:0.8em;font-weight:700;margin-left:6px;">(new)</span>'
                if change_type == "updated":
                    return ' <span style="color:#f59e0b;font-size:0.8em;font-weight:700;margin-left:6px;">(updated)</span>'
                if change_type == "removed":
                    return ' <span style="color:#ef4444;font-size:0.8em;font-weight:700;margin-left:6px;">(removed)</span>'
        return ""

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ACCC Acquisition Update - {case_number}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- Update Banner -->
<div style="background:#fef2f2;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #ef4444;">
  <div style="font-size:16px;font-weight:800;color:#dc2626;margin-bottom:8px;">⚠️ ACCC Case Updated</div>
  <div style="font-size:14px;color:#991b1b;">
    This case has been updated. Changed fields: {', '.join(change_summary)}
  </div>
</div>
"""

    # Deal / link banner
    if deal:
        html += f"""
<!-- Deal Match Info Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;">
    <span style="font-weight:700;">Acquirer:</span> {acquirer} <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Target:</span> {target}"""
        if deal_id_str:
            html += f""" <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Deal ID:</span> {deal_id_str}"""
        html += """
  </div>"""
        if detail_url:
            html += f"""
  <div style="margin-top:10px;">
    <a href="{detail_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View ACCC Case →</a>
  </div>"""
        html += """
    </div>"""
    elif is_usa:
        # USA-related but no matched deal
        html += """
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">🇺🇸 USA-Related ACCC Case</div>
  <div style="font-size:14px;color:#1e3a8a;">
    This ACCC case appears to involve USA-related parties or markets.
  </div>"""
        if detail_url:
            html += f"""
  <div style="margin-top:10px;">
    <a href="{detail_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View ACCC Case →</a>
  </div>"""
        html += """
</div>"""
    elif detail_url:
        html += f"""
<div style="background:#e5e7eb;border-radius:6px;padding:14px 18px;margin-bottom:20px;border-left:4px solid #6b7280;">
  <div style="font-size:14px;color:#374151;">
    <a href="{detail_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:600;">View ACCC Case →</a>
  </div>
</div>"""

    # Top summary
    html += f"""
<!-- Top summary panel -->
<div style="background:#f3f4f6;border-radius:2px;padding:22px 26px;">
  <div style="display:grid;grid-template-columns:260px 1fr;row-gap:16px;column-gap:18px;align-items:center;">

    <div style="font-weight:700;">Acquisition status:</div>
    <div>
      <span style="display:inline-block;padding:8px 14px;border-radius:6px;background:{status_color};color:#ffffff;font-weight:800;font-size:14px;">
        {acquisition_status}{change_flag("Acquisition status")}
      </span>
    </div>

    <div style="font-weight:700;">Acquisition case number:</div>
    <div>{case_number}</div>

    <div style="font-weight:700;">Type:</div>
    <div>{case_type}{change_flag("Type")}</div>"""

    # Waiver vs effective notification date
    if isinstance(case_type, str) and "waiver" in case_type.lower():
        html += f"""
    <div style="font-weight:700;">Waiver application date:</div>
    <div>{notification_date}{change_flag("Effective notification date")}</div>"""
    else:
        html += f"""
    <div style="font-weight:700;">Effective notification date:</div>
    <div>{notification_date}{change_flag("Effective notification date")}</div>"""

    html += """
  </div>
</div>

<!-- Status -->
<div style="margin-top:36px;">
  <div style="font-size:22px;font-weight:800;margin-bottom:14px;">Status</div>
  <div style="height:1px;background:#e5e7eb;"></div>

  <div style="display:grid;grid-template-columns:240px 1fr;row-gap:14px;column-gap:18px;padding-top:18px;">"""

    # Stage
    if stage and stage != "N/A":
        html += f"""
    <div>Stage:</div>
    <div>{stage}{change_flag("Stage")}</div>"""

    # ACCC Determination
    if accc_determination and accc_determination != "N/A":
        html += f"""
    <div>ACCC Determination:</div>
    <div>{accc_determination}{change_flag("ACCC Determination")}</div>"""

    # Determination publication date
    if determination_pub_date:
        html += f"""
    <div>Determination publication date:</div>
    <div>{determination_pub_date}{change_flag("Determination publication date")}</div>"""

    html += """
  </div>
</div>
"""

    # About the acquisition (acquirers, targets, etc.)
    if acquirers or targets or other_parties or anzsic or description:
        html += """
<!-- About the acquisition -->
<div style="margin-top:34px;">
  <div style="font-size:22px;font-weight:800;margin-bottom:14px;">About the acquisition</div>
  <div style="height:1px;background:#e5e7eb;"></div>

  <div style="display:grid;grid-template-columns:240px 1fr;row-gap:18px;column-gap:18px;padding-top:18px;">"""

        # Acquirers
        if acquirers:
            html += """
    <div>Acquirer(s):</div>
    <div>"""
            for i, acq in enumerate(acquirers):
                mb = "8px" if i < len(acquirers) - 1 else "0"
                name = acq.get("name", "N/A")
                reg = acq.get("registration")
                row_prefix = f"about_the_acquisition.acquirers[{i}]"
                html += f"""
      <div style="margin-bottom:{mb};">
        <span style="font-weight:800;">{name}{path_change_flag(row_prefix + '.name')}</span>"""
                if reg:
                    html += f"""
        <span style="float:right;">{reg}{path_change_flag(row_prefix + '.registration')}</span>"""
                html += """
        <div style="clear:both;"></div>
      </div>"""
            html += """
    </div>"""

        # Targets
        if targets:
            html += """
    <div>Target(s) or Vendor(s):</div>
    <div>"""
            for i, tgt in enumerate(targets):
                mb = "8px" if i < len(targets) - 1 else "0"
                name = tgt.get("name", "N/A")
                reg = tgt.get("registration")
                row_prefix = f"about_the_acquisition.targets[{i}]"
                html += f"""
      <div style="margin-bottom:{mb};">
        <span style="font-weight:800;">{name}{path_change_flag(row_prefix + '.name')}</span>"""
                if reg:
                    html += f"""
        <span style="float:right;">{reg}{path_change_flag(row_prefix + '.registration')}</span>"""
                html += """
        <div style="clear:both;"></div>
      </div>"""
            html += """
    </div>"""

        # Other parties
        if other_parties:
            html += """
    <div>Other party(ies):</div>
    <div>"""
            for i, party in enumerate(other_parties):
                mb = "8px" if i < len(other_parties) - 1 else "0"
                name = party.get("name", "N/A")
                reg = party.get("registration")
                row_prefix = f"about_the_acquisition.other_parties[{i}]"
                html += f"""
      <div style="margin-bottom:{mb};">
        <span style="font-weight:800;">{name}{path_change_flag(row_prefix + '.name')}</span>"""
                if reg:
                    html += f"""
        <span style="float:right;">{reg}{path_change_flag(row_prefix + '.registration')}</span>"""
                html += """
        <div style="clear:both;"></div>
      </div>"""
            html += """
    </div>"""

        # ANZSIC codes
        if anzsic:
            html += f"""
    <div>ANZSIC code(s):</div>
    <div>{anzsic}{change_flag("ANZSIC code(s)")}</div>"""

        # Description
        if description:
            html += f"""
    <div>Description:</div>
    <div style="line-height:1.55;">{description}{change_flag("Description")}</div>"""

        html += """
  </div>
</div>
"""

    # Decisions and key events table
    if decisions_events:
        # Determine which events are newly added
        new_events_list: List[Dict[str, Any]] = []
        if "Decisions and key events" in changed_fields:
            _ctype, new_events_list = changed_fields["Decisions and key events"]

        html += """
<!-- Decisions and key events -->
<div style="margin-top:36px;">
  <div style="font-size:22px;font-weight:800;margin-bottom:14px;">Decisions and key events</div>
  <div style="height:1px;background:#e5e7eb;"></div>

  <div style="padding-top:18px;">
    <table style="width:100%;border-collapse:collapse;">
      <tbody>"""

        for idx, ev in enumerate(decisions_events):
            ev_date = ev.get("date", "N/A")
            ev_desc = ev.get("description", "N/A")
            ev_url = ev.get("attachment_url", "")
            ev_size = ev.get("attachment_size", "")

            # "New" event detection (based on date+description, as before)
            is_new = any(
                e.get("date") == ev_date and e.get("description") == ev_desc
                for e in new_events_list or []
            )
            base_flag = (
                ' <span style="color:#10b981;font-size:0.85em;font-weight:700;margin-left:6px;">(new)</span>'
                if is_new
                else ""
            )

            # Also show "(updated)" if any field within this event row changed
            row_prefix = f"decisions_and_key_events[{idx}]"
            updated_flag = path_change_flag(row_prefix)
            row_flag = base_flag or updated_flag

            html += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:12px 8px 12px 0;vertical-align:top;width:120px;color:#6b7280;font-size:14px;">{ev_date}</td>
          <td style="padding:12px 8px;vertical-align:top;font-weight:600;">{ev_desc}{row_flag}</td>"""

            if ev_url:
                html += f"""
          <td style="padding:12px 0 12px 8px;vertical-align:top;text-align:right;width:180px;">
            <a href="{ev_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-size:14px;">
              📄 Attachment"""
                if ev_size:
                    html += f""" <span style="color:#6b7280;font-size:12px;">({ev_size})</span>"""
                html += """
            </a>
          </td>"""
            else:
                html += """
          <td></td>"""

            html += """
        </tr>"""

        html += """
      </tbody>
    </table>
  </div>
</div>
"""

    html += """
</div>
</body>
</html>
"""
    return html


def send_update_email(
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any, str]],
    is_usa: bool = False,
) -> bool:
    try:
        html = generate_update_email_html(
            old_case, new_case, deal, changes, is_usa)
        case_number = old_case.get("case_number", "N/A")
        title = old_case.get("title", "N/A")
        deal_id = str(deal.get("_id")) if deal and deal.get("_id") else None

        subject = build_subject("accc", "update", deal)

        payload = {
            "subject": subject,
            "html": html,
            "case_number": case_number,
            "title": title,
            "changed_fields": [c[0] for c in changes],
            "case_url": new_case.get("url", ""),
            "deal_id": deal_id,
            "is_usa": is_usa,
        }

        webhook_url = resolve_webhook_url(subject)
        logger.info(f"    Sending email via n8n webhook: {webhook_url}")
        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.warning(f"    Error sending email: {e}")
        return False


def update_case_document(
    collection, case_doc: Dict[str, Any], new_case: Dict[str, Any]
) -> bool:
    try:
        _id = case_doc.get("_id")
        if not _id:
            logger.warning("    Case document has no _id; cannot update")
            return False

        # Preserve existing linkage fields such as deal_id and usa_related
        updated = dict(new_case)
        if "deal_id" in case_doc and "deal_id" not in updated:
            updated["deal_id"] = case_doc["deal_id"]

        # Preserve created_at; always bump updated_at
        if "created_at" in case_doc and "created_at" not in updated:
            updated["created_at"] = case_doc["created_at"]
        updated["updated_at"] = utc_now_iso()

        result = collection.update_one({"_id": _id}, {"$set": updated})
        if result.modified_count > 0:
            logger.info("    Updated case document in accc_cases")
        else:
            logger.info("    No DB changes made (document already up to date)")
        return True
    except Exception as e:
        logger.exception(f"Error updating case document: {e}")
        return False


def process_accc_cases_updates():
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    logger.info("[STEP 1] Starting ACCC Cases Update Monitor")
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    total_checked = 0
    total_changed = 0
    logger.info("=" * 60)
    logger.info("[STEP 1] Starting ACCC Cases Update Monitor")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)
    logger.info("[STEP 1.1] Starting ACCC Cases Register update monitor")

    try:
        logger.info("[STEP 1.2] Initializing MongoDB connection...")
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            collect_error(
                error_items,
                f"MongoDB connection failed: {message}",
                step="mongodb_connect",
            )
            return
        logger.info(f"[STEP 1.3] {message}")

        if not is_connected():
            collect_error(
                error_items,
                "MongoDB not connected. Exiting.",
                step="mongodb_connect",
            )
            return

        cases_collection = get_accc_cases_collection()
        if cases_collection is None:
            collect_error(
                error_items,
                "Could not access 'accc_cases' collection. Exiting.",
                step="get_collection",
            )
            return

        deals_collection = get_deals_collection()

        cursor = cases_collection.find(
            {"acquisition_status": {"$regex": "^under assessment$", "$options": "i"}}
        )
        cases = list(cursor)
        if not cases:
            print(
                "[STEP 1.4] No ACCC cases with acquisition_status 'Under Assessment' found.")
            return

        logger.info(
            f"[STEP 1.5] Found {len(cases)} ACCC cases with status 'Under Assessment'")
        logger.info(f"[STEP 1.6] Existing Cases: {cases}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    f"--proxy-server=http://{PROXY_HOST}:{PROXY_PORT}",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                proxy={
                    "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
                    "username": PROXY_USERNAME,
                    "password": PROXY_PASSWORD,
                },
            )
            page = context.new_page()
            logger.info(f"[STEP 1.7] Page: {page}")

            for idx, case_doc in enumerate(cases, 1):
                try:
                    total_checked += 1
                    case_number = case_doc.get("case_number", "N/A")
                    title = case_doc.get("title", "N/A")
                    url = case_doc.get("url")

                    logger.info(f"[{idx}/{len(cases)}] Case {case_number}: {title}")
                    if not url:
                        logger.warning("  No URL stored for this case; skipping")
                        continue

                    current_case = fetch_current_case_from_page(page, url)
                    logger.info(
                        f"Accc daily monitor check: current_case:1 {current_case}")

                    if not current_case:
                        collect_error(
                            error_items,
                            "Could not fetch current case info from detail page",
                            step="scrape_detail_page",
                            case_number=case_number,
                            context={"url": url},
                        )
                        continue

                    logger.info(f"  Case doc: {case_doc}")
                    logger.info(f"  Current case: {current_case}")

                    allowed_keys = {
                        "acquisition_status",
                        "type",
                        "effective_notification_date",
                        "status",
                        "about_the_acquisition",
                        "decisions_and_key_events",
                    }
                    for k in allowed_keys:
                        if k not in current_case and k in case_doc:
                            current_case[k] = case_doc.get(k)
                    if (
                        "decisions_and_key_events" in current_case
                        and isinstance(current_case.get("decisions_and_key_events"), list)
                        and not current_case.get("decisions_and_key_events")
                        and case_doc.get("decisions_and_key_events")
                    ):
                        logger.info(
                            "  decisions_and_key_events missing/empty; preserving existing")
                        current_case["decisions_and_key_events"] = case_doc.get(
                            "decisions_and_key_events"
                        )

                    changes = detect_changes(case_doc, current_case)
                    if not changes:
                        logger.info("  No changes detected")
                        continue

                    total_changed += 1
                    logger.info(f"  Changes detected ({len(changes)} fields)")
                    for label, old_val, new_val, change_type in changes:
                        if label == "Decisions and key events":
                            count = len(new_val) if isinstance(new_val, list) else 0
                            logger.info(f"    - {label}: {count} new event(s) (NEW)")
                        else:
                            old_disp = "N/A" if old_val is None else str(old_val)
                            new_disp = "N/A" if new_val is None else str(new_val)
                            tag = "NEW" if change_type == "new" else "UPDATED"
                            logger.info(
                                f"    - {label}: {old_disp} → {new_disp} ({tag})")

                    deal = None
                    deal_id = case_doc.get("deal_id")
                    if deal_id and deals_collection is not None:
                        try:
                            oid = ObjectId(deal_id)
                            deal = deals_collection.find_one({"_id": oid})
                        except Exception as e:
                            logger.warning(f"  Invalid deal_id on case: {e}")

                        if deal:
                            logger.info(
                                "  Case already linked to a deal; sending email and updating")
                            if not send_update_email(case_doc, current_case, deal, changes):
                                collect_error(
                                    error_items,
                                    "Failed to send update email",
                                    step="send_email",
                                    case_number=case_number,
                                )
                            if not update_case_document(
                                cases_collection, case_doc, current_case
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to update case document",
                                    step="update_case",
                                    case_number=case_number,
                                )
                            continue

                    try:
                        matched_deal_id = match_case_to_deal(title)
                    except Exception as e:
                        logger.exception(f"Error during deal matching: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="match_case_to_deal",
                            case_number=case_number,
                        )
                        matched_deal_id = None

                    logger.info(f"  Matched: {matched_deal_id}")
                    if matched_deal_id:
                        deal_id_str = matched_deal_id
                        logger.info(f"  LLM matched case to deal {deal_id_str}")

                        current_case["deal_id"] = deal_id_str

                        logger.info(f"  Deals collection: {deals_collection}")
                        if deals_collection is not None:
                            try:
                                oid = ObjectId(deal_id_str)
                                deal = deals_collection.find_one({"_id": oid})
                            except Exception as e:
                                logger.warning(
                                    f"  Invalid or unresolved deal_id from LLM: {e}")
                                deal = None
                        else:
                            deal = None

                        if not send_update_email(case_doc, current_case, deal, changes):
                            collect_error(
                                error_items,
                                "Failed to send update email",
                                step="send_email",
                                case_number=case_number,
                            )
                        if not update_case_document(
                            cases_collection, case_doc, current_case
                        ):
                            collect_error(
                                error_items,
                                "Failed to update case document",
                                step="update_case",
                                case_number=case_number,
                            )
                        continue

                    try:
                        case_details_str = f"""
Case number: {case_number}
Title: {title}
Acquisition status: {current_case.get("acquisition_status", "")}
Type: {current_case.get("type", "")}
URL: {url}
"""
                        is_usa = verify_usa_relation(
                            company_details=case_details_str,
                            case_type="ACCC",
                        )
                    except Exception as e:
                        logger.exception(f"Error verifying USA relation: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="verify_usa_relation",
                            case_number=case_number,
                        )
                        is_usa = False

                    if is_usa:
                        logger.info(
                            "  Case appears USA-related; sending email and updating")
                        if not send_update_email(
                            case_doc, current_case, None, changes, is_usa=True
                        ):
                            collect_error(
                                error_items,
                                "Failed to send update email",
                                step="send_email",
                                case_number=case_number,
                            )
                        if not update_case_document(
                            cases_collection, case_doc, current_case
                        ):
                            collect_error(
                                error_items,
                                "Failed to update case document",
                                step="update_case",
                                case_number=case_number,
                            )
                    else:
                        logger.info(
                            "  Not USA-related and no deal match; updating case only")
                        if not update_case_document(
                            cases_collection, case_doc, current_case
                        ):
                            collect_error(
                                error_items,
                                "Failed to update case document",
                                step="update_case",
                                case_number=case_number,
                            )
                except Exception as e:
                    logger.exception(f"Error processing case #{idx}: {e}")
                    collect_error(
                        error_items,
                        str(e),
                        step="process_case",
                        case_number=case_doc.get("case_number", "N/A"),
                    )
                    continue

            browser.close()

        logger.info("=" * 60)
        logger.info("Summary:")
        logger.info(f"   Total cases checked: {total_checked}")
        logger.info(f"   Cases with changes: {total_changed}")
        logger.info("=" * 60)
        logger.info("Done!")

    except Exception as e:
        logger.exception(f"Unhandled error in process_accc_cases_updates(): {e}")
        collect_error(
            error_items,
            f"Unhandled error in process_accc_cases_updates(): {e}",
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Total cases checked          : {total_checked}")
        logger.info(f"  Cases with changes           : {total_changed}")
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    process_accc_cases_updates()
