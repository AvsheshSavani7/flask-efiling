"""
FTC Early Termination Cases Scraper
====================================
Scrapes the FTC early-termination-notices list page (no detail pages),
stores each case in the ``ftc_cases`` MongoDB collection, matches with
deals via LLM, and sends email notifications through the n8n webhook.

Flow follows the same architecture as accc_waiver_register.py.
"""

import json
import os
import re
import sys
import logging
import time
from datetime import date, datetime, timezone, timedelta
from html import escape as escape_html
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from mongodb_connection import (
    get_database,
    get_deals_collection,
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from deal_match_llm import llm_match_deal_id, fetch_open_deals
from llm_verification_service import verify_usa_relation
from scraper_error_utils import collect_error, send_error_summary
from log_utils import cleanup_old_logs, refresh_log_file
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# Timestamps in IST (UTC+5:30)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "ftc_cases"
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
logger = logging.getLogger("ftc_cases")
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


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ENV_PATH = ".env"

LIST_URL = (
    "https://www.ftc.gov/legal-library/browse/early-termination-notices"
    "?sort_by=field_date&items_per_page=50"
)
BACKUP_JSON = "ftc_cases_backup.json"

CUTOFF_DATE = (datetime.now() - timedelta(days=2)).replace(
    hour=0, minute=0, second=0, microsecond=0
)

LIST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def notice_datetime_for_mongo(date_parsed: Optional[datetime]) -> Optional[datetime]:
    """Calendar notice date as BSON Date (UTC midnight)."""
    if date_parsed is None:
        return None
    if isinstance(date_parsed, datetime):
        cal = date_parsed.date()
    elif isinstance(date_parsed, date):
        cal = date_parsed
    else:
        return None
    return datetime(cal.year, cal.month, cal.day, tzinfo=timezone.utc)


def format_notice_date_for_display(value: Any) -> str:
    """Human-readable notice date for emails / LLM when ``date`` is a datetime."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        v = value.astimezone(timezone.utc) if value.tzinfo else value
        return v.strftime("%B %d, %Y")
    if isinstance(value, date):
        return value.strftime("%B %d, %Y")
    return str(value)


def title_without_case_id_prefix(case_id: str, title: str) -> str:
    """Drop leading ``CASE_ID: `` from title when it matches ``case_id`` (avoids duplication in subjects)."""
    if not title or title == "N/A":
        return title
    cid = str(case_id).strip()
    if not cid or cid == "N/A":
        return title.strip()
    m = re.match(rf"^{re.escape(cid)}:\s*(.+)$", title.strip(), re.DOTALL)
    if m:
        return m.group(1).strip()
    return title.strip()


def get_ftc_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["ftc_cases"]


def page_url(page_number: int) -> str:
    return f"{LIST_URL}&page={page_number}"


# ---------------------------------------------------------------------------
# HTML parsing (list page only — no detail page)
# ---------------------------------------------------------------------------

def parse_ftc_date(date_str: str) -> Optional[datetime]:
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


def parse_list_items(html_content: str) -> List[Dict[str, Any]]:
    """Parse the FTC early-termination list HTML into a list of item dicts."""
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select(".views-row")

    items: List[Dict[str, Any]] = []
    for row in rows:
        try:
            item: Dict[str, Any] = {}

            title_elem = row.select_one("h3.node-title a")
            if title_elem:
                item["title"] = title_elem.get_text(strip=True)
                href = title_elem.get("href", "")
                if href and not href.startswith("http"):
                    item["detail_url"] = "https://www.ftc.gov" + href
                else:
                    item["detail_url"] = href

            if item.get("title"):
                m = re.match(r"^(\d+):\s*(.+)", item["title"])
                if m:
                    item["case_id"] = m.group(1)
                    item["parties_text"] = m.group(2).strip()

            date_elem = row.select_one(".field--name-field-date time")
            if date_elem:
                datetime_attr = date_elem.get("datetime")
                if datetime_attr:
                    try:
                        dt = datetime.fromisoformat(
                            datetime_attr.replace("Z", "+00:00"))
                        item["date"] = dt.strftime("%B %d, %Y")
                        item["date_parsed"] = dt.replace(
                            tzinfo=None) if dt.tzinfo else dt
                    except (ValueError, TypeError):
                        item["date"] = date_elem.get_text(strip=True)
                        item["date_parsed"] = parse_ftc_date(item["date"])
                else:
                    item["date"] = date_elem.get_text(strip=True)
                    item["date_parsed"] = parse_ftc_date(item["date"])

            acquiring_elem = row.select_one(
                ".field--name-field-acquiring-party .field__item"
            )
            if acquiring_elem:
                item["acquiring_party"] = acquiring_elem.get_text(strip=True)

            acquired_elem = row.select_one(
                ".field--name-field-acquired-party .field__item"
            )
            if acquired_elem:
                item["acquired_party"] = acquired_elem.get_text(strip=True)

            entities_elems = row.select(
                ".field--name-field-other-entities .field__item"
            )
            if entities_elems:
                item["acquired_entities"] = [
                    e.get_text(strip=True) for e in entities_elems
                ]

            if not item.get("case_id"):
                continue

            items.append(item)
        except Exception as e:
            logger.warning(f"Error parsing list item: {e}")
            continue

    logger.info(f"Parsed {len(items)} items from list page")
    return items


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_list_page(url: str, attempt_label: str = "") -> Optional[str]:
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=LIST_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            label = f"{attempt_label} " if attempt_label else ""
            logger.warning(
                f"{label}Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                return None
            time.sleep(5 * attempt)
    return None


# ---------------------------------------------------------------------------
# LLM deal matching (ACCC pattern)
# ---------------------------------------------------------------------------

def prepare_case_payload_for_llm(case_info: Dict[str, Any]) -> str:
    case_id = case_info.get("case_id", "")
    title = case_info.get("title", "")
    date_str = format_notice_date_for_display(case_info.get("date"))
    acquiring = case_info.get("acquiring_party", "")
    acquired = case_info.get("acquired_party", "")
    entities = case_info.get("acquired_entities", [])
    entities_str = ", ".join(entities) if entities else ""
    url = case_info.get("detail_url", "")

    parts = []
    if acquiring:
        parts.append(f"Acquiring Party: {acquiring}")
    if acquired:
        parts.append(f"Acquired Party: {acquired}")
    if entities_str:
        parts.append(f"Acquired Entities: {entities_str}")

    parties_str = "\n".join(parts)

    return f"""
Case ID: {case_id}
Title: {title}
Date: {date_str}
URL: {url}
{parties_str}
""".strip()


def match_case_to_deal(title: str, deals: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """
    Use LLM to match FTC case title against deals.

    Args:
        title: FTC Early Termination Notice title string.
        deals: Pre-loaded deal list. Fetched from MongoDB if None.

    Returns deal_id string or None.
    """
    return llm_match_deal_id(
        regulator_name="FTC Early Termination",
        case_sections={"FTC EARLY TERMINATION NOTICE TITLE TO MATCH": title},
        source_label="the FTC title",
        source_label_step1="the FTC title (both acquirer and target)",
        deals=deals,
    )
# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _post_email_payload(payload: Dict[str, Any]) -> bool:
    return post_email_payload(payload)


def send_new_case_email(case_info: Dict[str, Any], deal_id: Optional[str], deal_match: Optional[Dict[str, Any]] = None) -> bool:
    case_id = case_info.get("case_id", "N/A")
    title = case_info.get("title", "N/A")
    title_clean = title_without_case_id_prefix(case_id, title)
    subject = build_subject("ftc", "new", deal_match)
    detail_url = case_info.get("detail_url", "")
    date_str = format_notice_date_for_display(case_info.get("date"))
    acquiring = case_info.get("acquiring_party", "")
    acquired = case_info.get("acquired_party", "")
    entities = case_info.get("acquired_entities", [])
    entities_str = ", ".join(entities) if entities else "N/A"

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:600px;">
  <div style="border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px;">
    <div style="font-size:18px;font-weight:700;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #e2e8f0;">{escape_html(title_clean)}</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Case ID:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(case_id)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Date:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(date_str)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Acquiring Party:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(acquiring)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Acquired Party:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(acquired)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Acquired Entities:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(entities_str)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Deal ID:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(str(deal_id or 'N/A'))}</td>
      </tr>
    </table>
  </div>
  {'<div style="padding-top:4px;"><a href="'+escape_html(detail_url)+'" target="_blank" style="color:#0ea5e9;font-size:14px;text-decoration:none;">View FTC Notice &rarr;</a></div>' if detail_url else ''}
</div>
""".strip()

    payload = {
        "subject": subject,
        "html": html,
        "case_id": case_id,
        "title": title_clean,
        "case_url": detail_url,
        "deal_id": deal_id,
        "is_new_case": True,
    }
    return _post_email_payload(payload)


def send_unmatched_usa_related_email(case_info: Dict[str, Any]) -> bool:
    case_id = case_info.get("case_id", "N/A")
    title = case_info.get("title", "N/A")
    title_clean = title_without_case_id_prefix(case_id, title)
    subject = build_subject("ftc", "new")
    detail_url = case_info.get("detail_url", "")
    date_str = format_notice_date_for_display(case_info.get("date"))
    acquiring = case_info.get("acquiring_party", "")
    acquired = case_info.get("acquired_party", "")
    entities = case_info.get("acquired_entities", [])
    entities_str = ", ".join(entities) if entities else "N/A"

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:600px;">
  <div style="border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px;">
    <div style="font-size:18px;font-weight:700;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #e2e8f0;">{escape_html(title_clean)}</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Case ID:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(case_id)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Date:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(date_str)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Acquiring Party:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(acquiring)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Acquired Party:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(acquired)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#64748b;font-size:14px;">Acquired Entities:</td>
        <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(entities_str)}</td>
      </tr>
    </table>
  </div>
  {'<div style="padding-top:4px;"><a href="'+escape_html(detail_url)+'" target="_blank" style="color:#0ea5e9;font-size:14px;text-decoration:none;">View FTC Notice &rarr;</a></div>' if detail_url else ''}
</div>
""".strip()

    payload = {
        "subject": subject,
        "html": html,
        "case_id": case_id,
        "title": title_clean,
        "case_url": detail_url,
        "deal_id": None,
        "is_unmatched": True,
        "is_new_case": True,
    }
    return _post_email_payload(payload)


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def ftc_case_exists(collection, case_id: str) -> bool:
    try:
        return collection.count_documents({"case_id": case_id}, limit=1) > 0
    except Exception as e:
        logger.warning(f"Error checking existing FTC case {case_id}: {e}")
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    try:
        result = collection.insert_one(case_info)
        return str(result.inserted_id)
    except Exception as e:
        logger.warning(f"Error inserting case {case_info.get('case_id')}: {e}")
        return None


# ---------------------------------------------------------------------------
# Process a single FTC case
# ---------------------------------------------------------------------------

def _process_ftc_case(
    collection,
    case_info: Dict[str, Any],
    error_items: List[Dict[str, Any]],
    test_mode: bool = False,
    open_deals: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Handle a new FTC early termination case.

    Test mode  — directly insert, no LLM calls or emails.
    Live mode  — deal match -> USA check -> email -> insert.

    Returns the backup-ready case dict on success, or None on failure.
    """
    case_id = case_info.get("case_id", "")
    title = case_info.get("parties_text") or case_info.get("title", "")

    if not test_mode:
        try:
            matched_deal_id = match_case_to_deal(title, deals=open_deals)
        except Exception as e:
            logger.exception(f"  Error during deal matching: {e}")
            collect_error(
                error_items,
                str(e),
                step="match_case_to_deal",
                context={"case_id": case_id},
            )
            matched_deal_id = None

        if matched_deal_id:
            case_info["deal_id"] = matched_deal_id
            logger.info(
                f"  Deal match found (deal_id={matched_deal_id}); sending email")
            deal_match = get_deal_by_id(matched_deal_id)
            if not send_new_case_email(case_info, matched_deal_id, deal_match):
                collect_error(
                    error_items,
                    "Failed to send new-case email",
                    step="send_email",
                    context={
                        "case_id": case_id,
                        "detail_url": case_info.get("detail_url"),
                    },
                )
        else:
            try:
                case_details_str = prepare_case_payload_for_llm(case_info)
                is_usa = bool(
                    verify_usa_relation(
                        company_details=case_details_str, case_type="FTC"
                    )
                )
            except Exception as e:
                logger.exception(f"Error verifying USA relation: {e}")
                collect_error(
                    error_items,
                    str(e),
                    step="verify_usa_relation",
                    context={"case_id": case_id},
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
                        context={
                            "case_id": case_id,
                            "detail_url": case_info.get("detail_url"),
                        },
                    )

    inserted_id = insert_case(collection, case_info)
    if inserted_id:
        label = "[TEST MODE] " if test_mode else ""
        logger.info(
            f"  {label}Inserted new case into ftc_cases (id={inserted_id})")
        backup = dict(case_info)
        backup.pop("_id", None)
        return backup

    collect_error(
        error_items,
        "Insert failed",
        step="insert_case",
        context={"case_id": case_id},
    )
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_ftc_cases_scraper(test_mode: bool = False):
    """
    Main entry point for scraping FTC early termination notices.

    Test mode  — paginate ALL pages; directly insert into DB
                 (no LLM calls, no emails).
    Live mode  — first page only; deal match -> USA check -> email -> insert.
    """
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    new_cases: List[Dict[str, Any]] = []
    all_items: List[Dict[str, Any]] = []
    unique_items: List[Dict[str, Any]] = []
    filtered_items: List[Dict[str, Any]] = []
    mode_label = "TEST MODE" if test_mode else "LIVE MODE"

    logger.info("=" * 60)
    logger.info(f"[STEP 1] Starting FTC Cases Scraper ({mode_label})")
    logger.info(f"Cutoff date: {CUTOFF_DATE.strftime('%Y-%m-%d')}")
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

        collection = get_ftc_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "Could not access 'ftc_cases' collection. Exiting.",
                step="get_collection",
            )
            return

        if test_mode:
            logger.info("[STEP 2] TEST MODE — paginating all pages")
            page_num = 0
            while True:
                url = page_url(page_num)
                logger.info(f"  Fetching page {page_num}: {url}")
                html = fetch_list_page(url, attempt_label=f"[page={page_num}]")
                if not html:
                    collect_error(
                        error_items,
                        f"Failed to fetch FTC list page {page_num}",
                        step="fetch_list_page",
                        context={"url": url, "page": page_num},
                    )
                    break
                items = parse_list_items(html)
                if not items:
                    logger.info(
                        f"  Page {page_num} returned 0 items — end of list")
                    break
                all_items.extend(items)
                logger.info(
                    f"  Page {page_num}: {len(items)} items (total so far: {len(all_items)})"
                )
                page_num += 1
                time.sleep(2)
        else:
            logger.info("[STEP 2] LIVE MODE — fetching first page only")
            list_url = page_url(0)
            html = fetch_list_page(list_url, attempt_label="[page=0]")
            if not html:
                collect_error(
                    error_items,
                    "Failed to fetch first FTC list page",
                    step="fetch_list_page",
                    context={"url": list_url},
                )
                return
            all_items = parse_list_items(html)
            if not all_items:
                logger.info("No items found on first page; nothing to process")
                return

        logger.info(f"Total list items fetched: {len(all_items)}")

        seen_ids: set = set()
        for item in all_items:
            cid = item.get("case_id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                unique_items.append(item)

        logger.info(f"Unique items after dedup: {len(unique_items)}")

        for item in unique_items:
            date_parsed = item.get("date_parsed")
            if date_parsed is None:
                filtered_items.append(item)
                continue
            try:
                d = date_parsed.date() if hasattr(date_parsed, "date") else date_parsed
                cutoff = CUTOFF_DATE.date() if hasattr(CUTOFF_DATE, "date") else CUTOFF_DATE
                if d >= cutoff:
                    filtered_items.append(item)
            except (AttributeError, TypeError):
                filtered_items.append(item)

        logger.info(
            f"Items with date >= {CUTOFF_DATE.strftime('%Y-%m-%d')}: {len(filtered_items)}"
        )

        if not filtered_items:
            logger.info("No records for current date window. Done.")
            return

        open_deals = fetch_open_deals()

        for idx, item in enumerate(filtered_items, 1):
            try:
                case_id = item.get("case_id", "")
                title = item.get("title", "")

                logger.info(
                    f"[{idx}/{len(filtered_items)}] Case {case_id}: {title}")

                if not case_id:
                    logger.warning("  Missing case_id; skipping")
                    continue

                if ftc_case_exists(collection, case_id):
                    logger.info(
                        f"  Case already in DB (case_id={case_id}); skipping")
                    continue

                now_iso = utc_now_iso()
                case_info: Dict[str, Any] = {
                    "case_id": case_id,
                    "title": item.get("title", ""),
                    "parties_text": item.get("parties_text", ""),
                    "date": notice_datetime_for_mongo(item.get("date_parsed")),
                    "acquiring_party": item.get("acquiring_party", ""),
                    "acquired_party": item.get("acquired_party", ""),
                    "acquired_entities": item.get("acquired_entities", []),
                    "detail_url": item.get("detail_url", ""),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }

                backup = _process_ftc_case(
                    collection=collection,
                    case_info=case_info,
                    error_items=error_items,
                    test_mode=test_mode,
                    open_deals=open_deals,
                )
                if backup:
                    new_cases.append(backup)

            except Exception as e:
                logger.exception(f"Error processing item #{idx}: {e}")
                collect_error(
                    error_items,
                    str(e),
                    step="process_list_item",
                    context={"case_id": item.get("case_id", "N/A")},
                )
                continue

        if new_cases:
            try:
                serializable: List[Dict[str, Any]] = []
                for c in new_cases:
                    d = dict(c)
                    if "_id" in d:
                        d["_id"] = str(d["_id"])
                    serializable.append(d)

                with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                    json.dump(
                        serializable,
                        f,
                        indent=2,
                        ensure_ascii=False,
                        default=str,
                    )
                logger.info(
                    f"Saved {len(serializable)} new cases to backup JSON: {BACKUP_JSON}")
            except Exception as e:
                logger.warning(f"Error writing backup JSON: {e}")

    except Exception as e:
        logger.exception(f"Unhandled error in run_ftc_cases_scraper(): {e}")
        collect_error(
            error_items,
            f"Unhandled error in run_ftc_cases_scraper(): {e}",
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"Total items from list      : {len(all_items)}")
        logger.info(f"Unique items               : {len(unique_items)}")
        logger.info(f"Items in date window       : {len(filtered_items)}")
        logger.info(f"New cases inserted         : {len(new_cases)}")
        logger.info(f"Errors encountered         : {len(error_items)}")
        logger.info(f"Total time                 : {elapsed}s")
        logger.info("=" * 60)
        logger.info("FTC Cases Scraper finished")


if __name__ == "__main__":
    env_flag = os.getenv("FTC_CASES_TEST_MODE", "").lower()
    test_mode_env = env_flag in ("1", "true", "yes", "y")
    run_ftc_cases_scraper(test_mode=test_mode_env)
