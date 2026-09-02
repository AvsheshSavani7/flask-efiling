from mongodb_connection import (
    get_database,
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from llm_verification_service import verify_usa_relation
from accc_cases_register import match_case_to_deal
from deal_match_regex import apply_regex_match_subject, regex_match_deal_by_title
from deal_match_llm import fetch_open_deals
from log_utils import cleanup_old_logs, refresh_log_file
from scraper_error_utils import collect_error, send_error_summary
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload
import os
import json
import sys
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import time

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# Timestamps in IST (UTC+5:30)
# -----------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "australia_waiver_register"
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
logger = logging.getLogger("accc_waiver_register")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


if not logger.handlers:
    formatter = _ISTFormatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


ENV_PATH = ".env"

# Waiver-specific list URL (filter by acquisition type = waiver, 50 items/page)
LIST_URL = (
    "https://www.accc.gov.au/public-registers/acquisitions-and-mergers-registers/"
    "acquisitions-register"
    "?items_per_page=50&f%5B0%5D=acccgov_acquisition_type%3Aacccgov_acquisition_waiver"
)
BACKUP_JSON = "accc_waiver_register_backup.json"

PROXY_HOST = "108.59.242.138"
PROXY_PORT = 46885
PROXY_USERNAME = "GSenAgrfKhuNWkd"
PROXY_PASSWORD = "8lmVa5yl0pKp9MI"

BASE_URL = os.getenv("BASE_URL")
# Webhook specifically for waiver cases that are still "Under assessment"
UNDER_ASSESSMENT_WEBHOOK_URL = (
    f"{BASE_URL}/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f"
)


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_accc_cases_collection():
    """Get the 'accc_cases' collection from the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["accc_cases"]


def page_url(page_number: int) -> str:
    """Build a paginated URL for the waiver register list."""
    return f"{LIST_URL}&page={page_number}"


def parse_list_items(html_content: str) -> List[Dict[str, Any]]:
    """Parse the acquisitions register list HTML into a list of item dicts."""
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select(".views-row")

    items: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        try:
            item: Dict[str, Any] = {}

            title_elem = row.select_one("h3")
            if title_elem:
                item["title"] = title_elem.get_text(strip=True)

            link_elem = row.select_one(
                "a[href*='/acquisitions-register/']"
            )
            if link_elem and link_elem.get("href"):
                href = link_elem["href"]
                if href and not href.startswith("http"):
                    item["url"] = "https://www.accc.gov.au" + href
                else:
                    item["url"] = href

            status_elem = row.select_one(
                ".field--name-field-acccgov-merger-status .field__item"
            )
            if status_elem:
                item["acquisition_status"] = status_elem.get_text(strip=True)

            type_elem = row.select_one(".field--acccgov-type .field__item")
            if type_elem:
                item["type"] = type_elem.get_text(strip=True)

            case_number_elem = row.select_one(
                ".field--name-field-acccgov-mcmsmergermatterno .field__item"
            )
            if case_number_elem:
                item["case_number"] = case_number_elem.get_text(strip=True)

            date_elem = row.select_one(
                ".field--name-field-acccgov-pub-reg-date .field__item time"
            )
            if date_elem:
                item["effective_notification_date"] = date_elem.get_text(
                    strip=True)

            if "case_number" in item and "url" in item:
                items.append(item)
        except Exception as e:
            logger.warning(f"Error parsing list item #{idx + 1}: {e}")
            continue

    logger.info(f"Parsed {len(items)} items from list page")
    return items


def fetch_list_page(page, url: str, attempt_label: str = "") -> Optional[str]:
    """Fetch a single list page via Playwright (Akamai-safe). Returns HTML or None."""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(".views-row", timeout=15000)
            except Exception:
                page.wait_for_timeout(5000)
            html = page.content()
            if "Access Denied" in html:
                raise Exception("Akamai Access Denied (403)")
            return html
        except Exception as e:
            label = f"{attempt_label} " if attempt_label else ""
            logger.warning(
                f"{label}Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                return None
            time.sleep(5 * attempt)
    return None


def extract_text(element) -> str:
    """Safely get inner text from a Playwright element handle."""
    if not element:
        return ""
    try:
        return element.inner_text().strip()
    except Exception:
        return ""


def prepare_case_payload_for_llm(case_info: Dict[str, Any]) -> str:
    """Prepare a compact case summary string for LLM calls."""
    case_number = case_info.get("case_number", "")
    title = case_info.get("title", "")
    status = case_info.get("acquisition_status", "")
    case_type = case_info.get("type", "")
    url = case_info.get("url", "")

    about = case_info.get("about_the_acquisition", {}) or {}
    acquirers = about.get("acquirers", []) or []
    targets = about.get("targets", []) or []
    others = about.get("other_parties", []) or []
    description = about.get("description", "")

    def _names(items: Any) -> str:
        if not isinstance(items, list):
            return ""
        names = []
        for it in items:
            if isinstance(it, dict):
                n = (it.get("name") or "").strip()
                if n:
                    names.append(n)
            elif isinstance(it, str):
                s = it.strip()
                if s:
                    names.append(s)
        return ", ".join(names)

    parties = []
    acq = _names(acquirers)
    tgt = _names(targets)
    oth = _names(others)
    if acq:
        parties.append(f"Acquirer(s): {acq}")
    if tgt:
        parties.append(f"Target(s): {tgt}")
    if oth:
        parties.append(f"Other party(ies): {oth}")

    parts_str = "\n".join(parties)
    desc_snippet = description.strip()
    if len(desc_snippet) > 1500:
        desc_snippet = desc_snippet[:1500] + "…"

    return f"""
Case number: {case_number}
Title: {title}
Acquisition status: {status}
Type: {case_type}
URL: {url}
{parts_str}
Description: {desc_snippet}
""".strip()


def _post_email_payload(
    payload: Dict[str, Any], webhook_url: Optional[str] = None
) -> bool:
    return post_email_payload(payload, webhook_url=webhook_url)


def send_new_case_email(
    case_info: Dict[str, Any],
    deal_id: Optional[str],
    deal_match: Optional[Dict[str, Any]] = None,
    matched_by_regex: bool = False,
) -> bool:
    case_number = case_info.get("case_number", "N/A")
    title = case_info.get("title", "N/A")
    subject = build_subject("accc_waiver", "new", deal_match)
    subject = apply_regex_match_subject(subject, matched_by_regex)
    url = case_info.get("url", "")
    notification_date = case_info.get("effective_notification_date", "")
    acquisition_status = case_info.get("acquisition_status", "")
    case_type = case_info.get("type", "")

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:600px;">
  <div style="border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px;">
    <div style="font-size:18px;font-weight:700;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #e2e8f0;">{title}</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;white-space:nowrap;vertical-align:middle;">Acquisition status:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;vertical-align:middle;">
          <span style="background:#14b8a6;color:#fff;padding:4px 14px;border-radius:4px;font-size:13px;font-weight:600;">{acquisition_status}</span>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Type:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{case_type}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Case number:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{case_number}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Waiver application date:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{notification_date}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Deal ID:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{deal_id or "N/A"}</td>
      </tr>
    </table>
  </div>
  {'<div style="padding-top:4px;"><a href="'+url+'" target="_blank" style="color:#0ea5e9;font-size:14px;text-decoration:none;">View ACCC Waiver Case &rarr;</a></div>' if url else ''}
</div>
""".strip()

    payload = {
        "subject": subject,
        "html": html,
        "case_number": case_number,
        "title": title,
        "case_url": url,
        "deal_id": deal_id,
        "is_new_case": True,
    }
    return _post_email_payload(payload)


def send_unmatched_usa_related_email(case_info: Dict[str, Any]) -> bool:
    case_number = case_info.get("case_number", "N/A")
    title = case_info.get("title", "N/A")
    subject = build_subject("accc_waiver", "new")
    url = case_info.get("url", "")
    notification_date = case_info.get("effective_notification_date", "")
    acquisition_status = case_info.get("acquisition_status", "")
    case_type = case_info.get("type", "")

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:600px;">
  <div style="border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px;">
    <div style="font-size:18px;font-weight:700;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #e2e8f0;">{title}</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;white-space:nowrap;vertical-align:middle;">Acquisition status:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;vertical-align:middle;">
          <span style="background:#14b8a6;color:#fff;padding:4px 14px;border-radius:4px;font-size:13px;font-weight:600;">{acquisition_status}</span>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Type:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{case_type}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Case number:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{case_number}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Waiver application date:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{notification_date}</td>
      </tr>
    </table>
  </div>
  {'<div style="padding-top:4px;"><a href="'+url+'" target="_blank" style="color:#0ea5e9;font-size:14px;text-decoration:none;">View ACCC Waiver Case &rarr;</a></div>' if url else ''}
</div>
""".strip()

    payload = {
        "subject": subject,
        "html": html,
        "case_number": case_number,
        "title": title,
        "case_url": url,
        "deal_id": None,
        "is_unmatched": True,
        "is_new_case": True,
    }
    return _post_email_payload(payload)


def send_under_assessment_waiver_email(case_info: Dict[str, Any]) -> bool:
    """
    Send a notification for a waiver case that is still 'Under assessment'.
    Uses the dedicated under-assessment webhook. The case is NOT inserted into
    the DB so it will be re-evaluated on every run until it becomes completed.
    """
    case_number = case_info.get("case_number", "N/A")
    title = case_info.get("title", "N/A")
    subject = build_subject("accc_waiver", "under_assessment")
    url = case_info.get("url", "")
    notification_date = case_info.get("effective_notification_date", "")
    acquisition_status = case_info.get("acquisition_status", "")

    case_type = case_info.get("type", "")

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:600px;">
  <div style="border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px;">
    <div style="font-size:18px;font-weight:700;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #e2e8f0;">{title}</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;white-space:nowrap;vertical-align:middle;">Acquisition status:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;vertical-align:middle;">
          <span style="background:#f59e0b;color:#fff;padding:4px 14px;border-radius:4px;font-size:13px;font-weight:600;">{acquisition_status}</span>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Type:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{case_type}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Case number:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{case_number}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Waiver application date:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{notification_date}</td>
      </tr>
    </table>
  </div>
  {'<div style="padding-top:4px;"><a href="'+url+'" target="_blank" style="color:#0ea5e9;font-size:14px;text-decoration:none;">View ACCC Waiver Case &rarr;</a></div>' if url else ''}
</div>
""".strip()

    payload = {
        "subject": subject,
        "html": html,
        "case_number": case_number,
        "title": title,
        "case_url": url,
        "deal_id": None,
        "is_new_case": True,
        "is_under_assessment_waiver": True,
    }
    return _post_email_payload(payload, webhook_url=UNDER_ASSESSMENT_WEBHOOK_URL)


def extract_detail_page_case(page, url: str) -> Optional[Dict[str, Any]]:
    """Open the detail URL and parse it into a structured case_info dict."""
    try:
        logger.info(f"  Fetching detail page: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        case: Dict[str, Any] = {"url": url}

        try:
            title_elem = page.query_selector(
                "h1.page-title span.field--name-title")
            if title_elem:
                case["title"] = extract_text(title_elem)
        except Exception:
            pass

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
            logger.warning(f"  Error extracting summary fields: {e}")

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
                    end_period_elem)

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
                    pub_date_elem)
        except Exception as e:
            logger.warning(f"  Error extracting status section: {e}")

        if status_info:
            case["status"] = status_info

        about: Dict[str, Any] = {}
        try:
            acquirers: List[Dict[str, Any]] = []
            acq_section = page.query_selector(
                ".field--name-field-acccgov-applicants")
            if acq_section:
                for elem in acq_section.query_selector_all(".paragraph--type--acccgov-trader"):
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    c: Dict[str, Any] = {}
                    if name_elem:
                        c["name"] = extract_text(name_elem)
                    if reg_elem:
                        c["registration"] = extract_text(reg_elem)
                    if c:
                        acquirers.append(c)
            if acquirers:
                about["acquirers"] = acquirers

            targets: List[Dict[str, Any]] = []
            tgt_section = page.query_selector(
                ".field--name-field-acccgov-pub-reg-targets")
            if tgt_section:
                for elem in tgt_section.query_selector_all(".paragraph--type--acccgov-trader"):
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    c = {}
                    if name_elem:
                        c["name"] = extract_text(name_elem)
                    if reg_elem:
                        c["registration"] = extract_text(reg_elem)
                    if c:
                        targets.append(c)
            if targets:
                about["targets"] = targets

            others: List[Dict[str, Any]] = []
            other_section = page.query_selector(
                ".field--name-field-acccgov-other-parties")
            if other_section:
                for elem in other_section.query_selector_all(".paragraph--type--acccgov-trader"):
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    c = {}
                    if name_elem:
                        c["name"] = extract_text(name_elem)
                    if reg_elem:
                        c["registration"] = extract_text(reg_elem)
                    if c:
                        others.append(c)
            if others:
                about["other_parties"] = others

            anzsic_elem = page.query_selector(
                ".field--name-field-acquisition-anzsic-code .field__item"
            )
            if anzsic_elem:
                about["anzsic_codes"] = extract_text(anzsic_elem)

            desc_elem = page.query_selector(
                ".field--name-field-accc-body .full-text, "
                ".field--name-field-accc-body .summary-text"
            )
            if desc_elem:
                try:
                    read_more = page.query_selector(
                        ".field--name-field-accc-body .read-toggle")
                    if read_more:
                        read_more.click()
                        page.wait_for_timeout(500)
                        desc_elem = (
                            page.query_selector(
                                ".field--name-field-accc-body .full-text")
                            or desc_elem
                        )
                except Exception:
                    pass
                about["description"] = extract_text(desc_elem)
        except Exception as e:
            logger.warning(f"  Error extracting 'About the acquisition': {e}")

        if about:
            case["about_the_acquisition"] = about

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
            logger.warning(f"  Error extracting decisions/consultation: {e}")

        if events:
            case["decisions_and_key_events"] = events

        if not case.get("case_number"):
            logger.warning("  No case_number found on detail page; skipping")
            return None

        return case
    except Exception as e:
        logger.error(f"  Error extracting detail page {url}: {e}")
        return None


def waiver_case_exists(collection, case_number: str, acquisition_status: str) -> bool:
    """
    Return True if a waiver record with this case_number AND acquisition_status
    already exists in accc_cases.
    """
    try:
        count = collection.count_documents(
            {"case_number": case_number, "acquisition_status": acquisition_status},
            limit=1,
        )
        return count > 0
    except Exception as e:
        logger.warning(
            f"Error checking existing waiver case {case_number}: {e}")
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    """Insert a new case document into the accc_cases collection."""
    try:
        result = collection.insert_one(case_info)
        return str(result.inserted_id)
    except Exception as e:
        logger.warning(
            f"Error inserting case {case_info.get('case_number')}: {e}")
        return None


def _process_waiver_case(
    collection,
    case_info: Dict[str, Any],
    title: str,
    error_items: List[Dict[str, Any]],
    test_mode: bool = False,
    open_deals: Optional[List[Dict[str, Any]]] = None,
    counters: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Handle a completed waiver case and insert into DB.

    Test mode  — directly scrape and insert, no LLM calls or emails.
    Live mode  — deal match → USA check → email → insert.

    Returns the backup-ready case dict on success, or None on failure.
    """
    case_number = case_info.get("case_number", "")

    if not test_mode:
        try:
            matched_deal_id = match_case_to_deal(
                case_info.get("title", "") or title, deals=open_deals)
        except Exception as e:
            logger.exception(f"  Error during deal matching: {e}")
            collect_error(
                error_items,
                str(e),
                step="match_case_to_deal",
                case_number=case_number,
            )
            matched_deal_id = None

        # Regex fallback — only when LLM found nothing
        matched_by_regex = False
        if matched_deal_id:
            if counters is not None:
                counters["llm_match_count"] += 1
        else:
            matched_deal_id = regex_match_deal_by_title(
                case_info.get("title", "") or title, open_deals
            )
            if matched_deal_id:
                matched_by_regex = True
                if counters is not None:
                    counters["regex_match_count"] += 1
                logger.info(
                    f"  Regex fallback matched deal_id={matched_deal_id}")
            else:
                logger.info("  No match (LLM + regex both returned None)")

        if matched_deal_id:
            case_info["deal_id"] = matched_deal_id
            logger.info(
                f"  Deal match found (deal_id={matched_deal_id}); sending email")
            deal_match = get_deal_by_id(matched_deal_id)
            if not send_new_case_email(case_info, matched_deal_id, deal_match, matched_by_regex=matched_by_regex):
                collect_error(
                    error_items,
                    "Failed to send new-case email",
                    step="send_email",
                    case_number=case_number,
                    context={"url": case_info.get("url")},
                )
        else:
            try:
                case_details_str = prepare_case_payload_for_llm(case_info)
                is_usa = bool(
                    verify_usa_relation(
                        company_details=case_details_str, case_type="ACCC")
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
                    "  Case appears USA-related (unmatched); sending email")
                if not send_unmatched_usa_related_email(case_info):
                    collect_error(
                        error_items,
                        "Failed to send USA-related email",
                        step="send_email",
                        case_number=case_number,
                        context={"url": case_info.get("url")},
                    )

    inserted_id = insert_case(collection, case_info)
    if inserted_id:
        label = "[TEST MODE] " if test_mode else ""
        logger.info(
            f"  {label}Inserted new waiver into accc_cases (id={inserted_id})")
        backup = dict(case_info)
        backup.pop("_id", None)
        return backup

    collect_error(
        error_items,
        "Insert failed",
        step="insert_case",
        case_number=case_number,
    )
    return None


def run_accc_waiver_register(test_mode: bool = False):
    """
    Main entrypoint for scraping the ACCC acquisitions register (waiver type).

    Both modes:
    - Skip a record if it already exists in accc_cases with the same
      case_number AND acquisition_status (continue to next item).
    - Under-assessment waivers: send notification via the dedicated webhook;
      do NOT insert so they are re-evaluated every run.
    - Completed waivers: see below per mode.

    Test mode  — paginate ALL pages; directly scrape and insert into DB
                 (no LLM calls, no emails).
    Live mode  — first page only; deal match → USA check → email → insert.
    """
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    new_cases: List[Dict[str, Any]] = []
    all_items: List[Dict[str, Any]] = []
    counters = {"llm_match_count": 0, "regex_match_count": 0}
    mode_label = "TEST MODE" if test_mode else "LIVE MODE"

    logger.info("=" * 60)
    logger.info(f"[STEP 1] Starting ACCC Waiver Register ({mode_label})")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    try:
        logger.info("[STEP 1.1] Initializing MongoDB connection...")
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            collect_error(
                error_items,
                f"MongoDB connection failed: {message}",
                step="mongodb_connect",
            )
            return
        logger.info(f"[STEP 1.2] {message}")

        if not is_connected():
            collect_error(
                error_items,
                "MongoDB not connected. Exiting.",
                step="mongodb_connect",
            )
            return

        collection = get_accc_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "Could not access 'accc_cases' collection. Exiting.",
                step="get_collection",
            )
            return

        logger.info(f"Using residential proxy: {PROXY_HOST}:{PROXY_PORT}")

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
            try:
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
                pw_page = context.new_page()

                if test_mode:
                    logger.info(
                        "[STEP 2] TEST MODE — paginating all pages of the waiver register")
                    page_num = 0
                    while True:
                        list_url = page_url(page_num)
                        logger.info(f"  Fetching page {page_num}: {list_url}")
                        html = fetch_list_page(
                            pw_page, list_url, attempt_label=f"[page={page_num}]")
                        if not html:
                            collect_error(
                                error_items,
                                f"Failed to fetch waiver list page {page_num}",
                                step="fetch_list_page",
                                context={"url": list_url, "page": page_num},
                            )
                            break
                        items = parse_list_items(html)
                        if not items:
                            logger.info(
                                f"  Page {page_num} returned 0 items — end of list")
                            break
                        all_items.extend(items)
                        logger.info(
                            f"  Page {page_num}: {len(items)} items (total so far: {len(all_items)})")
                        page_num += 1
                        time.sleep(2)
                else:
                    logger.info("[STEP 2] LIVE MODE — fetching first page only")
                    list_url = page_url(0)
                    html = fetch_list_page(
                        pw_page, list_url, attempt_label="[page=0]")
                    if not html:
                        collect_error(
                            error_items,
                            "Failed to fetch first waiver list page",
                            step="fetch_list_page",
                            context={"url": list_url},
                        )
                        return
                    all_items = parse_list_items(html)
                    if not all_items:
                        logger.info(
                            "No items found on first page; nothing to process")
                        return

                logger.info(f"Total list items to process: {len(all_items)}")

                open_deals = fetch_open_deals()

                for idx, item in enumerate(all_items, 1):
                    try:
                        case_number = item.get("case_number")
                        url = item.get("url")
                        title = item.get("title", "")

                        logger.info(
                            f"[{idx}/{len(all_items)}] Case {case_number}: {title}")

                        if not case_number or not url:
                            logger.warning(
                                "  Missing case_number or url; skipping")
                            continue

                        list_status = item.get("acquisition_status", "")
                        if waiver_case_exists(collection, case_number, list_status):
                            logger.info(
                                f"  Case already in DB (status='{list_status}'); skipping"
                            )
                            continue

                        case_info = extract_detail_page_case(pw_page, url)
                        logger.info(f"  case_info: {case_info}")
                        if not case_info:
                            collect_error(
                                error_items,
                                "Could not extract case info from detail page",
                                step="scrape_detail_page",
                                case_number=case_number,
                                context={"url": url},
                            )
                            continue

                        for key in ["acquisition_status", "type", "effective_notification_date", "title"]:
                            if key not in case_info and key in item:
                                case_info[key] = item[key]

                        now_iso = utc_now_iso()
                        case_info.setdefault("created_at", now_iso)
                        case_info["updated_at"] = now_iso

                        detail_status = (case_info.get(
                            "acquisition_status") or "").strip()

                        if detail_status.lower() == "under assessment":
                            if test_mode:
                                logger.info(
                                    "  [TEST MODE] Waiver is Under Assessment; skipping"
                                )
                            else:
                                logger.info(
                                    "  Waiver is Under Assessment — sending notification "
                                    "(not inserting into DB)"
                                )
                                if not send_under_assessment_waiver_email(case_info):
                                    collect_error(
                                        error_items,
                                        "Failed to send under-assessment waiver email",
                                        step="send_email",
                                        case_number=case_number,
                                        context={"url": url},
                                    )
                            continue

                        backup = _process_waiver_case(
                            collection=collection,
                            case_info=case_info,
                            title=title,
                            error_items=error_items,
                            test_mode=test_mode,
                            open_deals=open_deals,
                            counters=counters,
                        )
                        if backup:
                            new_cases.append(backup)

                    except Exception as e:
                        logger.exception(f"Error processing list item #{idx}: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="process_list_item",
                            case_number=item.get("case_number", "N/A"),
                        )
                        continue

            finally:
                browser.close()

        if new_cases:
            try:
                serializable: List[Dict[str, Any]] = []
                for c in new_cases:
                    d = dict(c)
                    if "_id" in d:
                        d["_id"] = str(d["_id"])
                    serializable.append(d)

                with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                    json.dump(serializable, f, indent=2, ensure_ascii=False)
                logger.info(
                    f"Saved {len(serializable)} new cases to backup JSON: {BACKUP_JSON}"
                )
            except Exception as e:
                logger.warning(f"Error writing backup JSON: {e}")

    except Exception as e:
        logger.exception(f"Unhandled error in run_accc_waiver_register(): {e}")
        collect_error(
            error_items,
            f"Unhandled error in run_accc_waiver_register(): {e}",
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Total items from list        : {len(all_items)}")
        logger.info(f"  LLM deal matches             : {counters['llm_match_count']}")
        logger.info(f"  Regex fallback matches       : {counters['regex_match_count']}")
        logger.info(f"  New/updated cases            : {len(new_cases)}")
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)
        logger.info("ACCC Waiver Register scraper finished")


if __name__ == "__main__":
    # Enable test mode (full backfill) via env var:
    #   ACCC_WAIVER_TEST_MODE=1 python accc_waiver_register.py
    env_flag = os.getenv("ACCC_WAIVER_TEST_MODE", "").lower()
    test_mode_env = env_flag in ("1", "true", "yes", "y")
    run_accc_waiver_register(test_mode=test_mode_env)
