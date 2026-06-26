from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import datetime
import time
import logging
import requests
import json
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from openai import OpenAI
from deal_match_llm import llm_match_deal_id, fetch_open_deals
from deal_match_regex import apply_regex_match_subject, regex_match_samr_deal
import anthropic
from bs4 import BeautifulSoup
import re
from mongodb_connection import (
    get_deals_collection,
    get_database,
    init_mongodb_connection,
)
from html import escape as escape_html
from llm_verification_service import verify_usa_relation
from scraper_error_utils import collect_error, send_error_summary
from log_utils import cleanup_old_logs, refresh_log_file
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload, resolve_webhook_url
from typing import Any

# Configuration
# CUTOFF_DATE = datetime.datetime.now().replace(
#     hour=0, minute=0, second=0, microsecond=0)
# Configuration
CUTOFF_DATE = (datetime.datetime.now() - datetime.timedelta(days=5)).replace(
    hour=0, minute=0, second=0, microsecond=0)

# One-time fixed date range for bulk DB entry (set both to None to disable)
ONE_TIME_START_DATE = None
ONE_TIME_END_DATE = None
# ONE_TIME_START_DATE = datetime.datetime(2025, 11, 1)
# ONE_TIME_END_DATE = datetime.datetime(2026, 4, 7)

BASE__SCRAPER_URL = "https://www.samr.gov.cn/fldes/ajgs/jyaj/"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "samr_html_pages"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "samr-cases-public"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


logger = logging.getLogger(SCRIPT_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

if not logger.handlers:
    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                             backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


def _goto_with_retry(page, url, max_retries=2):
    """Navigate to a URL with retries and fallback wait strategies."""
    strategies = [
        ("networkidle", 90000),
        ("domcontentloaded", 90000),
        ("domcontentloaded", 120000),
    ]
    for attempt in range(max_retries):
        wait_until, timeout = strategies[min(attempt, len(strategies) - 1)]
        try:
            logger.info(
                f"   Attempt {attempt + 1}/{max_retries} (wait_until={wait_until}, timeout={timeout}ms)")
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:
            logger.warning(f"   Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                delay = 5 * (attempt + 1)
                logger.info(f"   Waiting {delay}s before retry...")
                time.sleep(delay)
            else:
                raise


os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)

load_dotenv(ENV_PATH)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Global state
deals = []
all_extracted_records = []
matched_data = []


# ---------------------------------------------------------------------------
# MongoDB helpers – samr_cases collection
# ---------------------------------------------------------------------------

def get_samr_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["samr_cases"]


def record_exists_in_samr_cases(url):
    """Return True if a record with this URL is already in samr_cases."""
    try:
        collection = get_samr_cases_collection()
        if collection is None:
            return False
        return collection.find_one({"url": url}) is not None
    except Exception as e:
        logger.warning(f"Error checking samr_cases: {e}")
        return False


def save_to_samr_cases(record):
    """
    Insert or update a record in samr_cases collection.

    Expected document shape:
    {
        "url": str,          # unique key
        "title_cn": str,
        "title_en": str,
        "date": str,
        "deal_id": str|None,
        "processed_at": str,
        "is_open": bool,
    }
    """
    try:
        collection = get_samr_cases_collection()
        if collection is None:
            logger.warning("samr_cases collection not available")
            return False

        record["processed_at"] = datetime.datetime.now().isoformat()
        record["is_open"] = True
        collection.update_one(
            {"url": record["url"]},
            {"$set": record},
            upsert=True,
        )
        logger.info(f"Saved to samr_cases: {record['url'][:80]}...")
        return True
    except Exception as e:
        logger.warning(f"Error saving to samr_cases: {e}")
        return False


# ---------------------------------------------------------------------------
# Deals – load from MongoDB
# ---------------------------------------------------------------------------

def get_deals_from_mongodb():
    """Fetch deals from the deals collection."""
    try:
        collection = get_deals_collection()
        if collection is None:
            logger.warning(
                "MongoDB connection not available. Deals collection not accessible.")
            return []

        query = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        all_deals = list(collection.find(query))
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        logger.info(f"Fetched {len(all_deals)} deals from MongoDB")
        return all_deals

    except Exception as e:
        logger.exception(f"Error fetching deals from MongoDB: {e}")
        return []


def load_deals():
    global deals
    deals = fetch_open_deals()
    logger.info(f"Loaded {len(deals)} deals from MongoDB")
    return deals


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate_to_english(text):
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "zh-CN",
            "tl": "en",
            "dt": "t",
            "q": text,
        }
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            return response.json()[0][0][0]
    except Exception as e:
        logger.warning(f"Translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


def translate_with_openai(text):
    """Translate Chinese text to English using GPT-4o mini."""
    try:
        response = client.responses.create(
            model="gpt-5.2",
            tools=[
                {"type": "web_search"}
            ],
            input=[
                {
                    "role": "system",
                    "content": """
You are a professional Chinese-to-English translator for merger control and regulatory case titles.

Rules:
1. Return ONLY the translated English title.
2. Use web search to identify official English company names when possible.
3. Do NOT explain.
4. Do NOT provide alternatives.
5. Do NOT invent company names.
6. If the official English company name cannot be verified, use a simple transliteration.
7. Preserve legal/regulatory meaning naturally.
"""
                },
                {
                    "role": "user",
                    "content": f"Translate this Simplified Chinese regulatory title to English:\n{text}"
                }
            ],
        )

        return response.output_text.strip()
    except Exception as e:
        logger.warning(f"OpenAI translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


def translate_with_claude(text):
    """Translate Chinese text to English using Claude opus-4-0."""
    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": f"You are a translator. Translate the following Simplified Chinese text to English. Return ONLY the translated text, nothing else.\n\n{text}",
                },
            ],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"Claude translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


# ---------------------------------------------------------------------------
# Listing-page HTML parsing
# ---------------------------------------------------------------------------

def extract_records_from_html(html_content):
    """
    Extract all records from a listing page HTML (parse only — no translation).
    Returns list of dicts: title_cn, url, date
    """
    records = []
    soup = BeautifulSoup(html_content, "html.parser")
    items = soup.select("div.page-content ul li.content-3-left-text")

    for item in items:
        try:
            link = item.find("a")
            if not link:
                continue

            title_cn_raw = link.get_text(strip=True)
            title_cn = re.sub(r'\s+', ' ', title_cn_raw).strip()

            href = link.get("href", "")
            if href and not href.startswith("http"):
                base_domain = "https://www.samr.gov.cn"
                href = requests.compat.urljoin(base_domain, href)

            date_div = item.find("div", class_="contentRight01time")
            date_str = date_div.get_text(strip=True) if date_div else ""

            record = {
                "title_cn": title_cn,
                "url": href,
                "date": date_str,
            }
            records.append(record)
            logger.info(f"Parsed: {date_str} - {title_cn}")

        except Exception as e:
            logger.warning(f"Error extracting record: {e}")
            continue

    return records


def extract_page_records(page, page_num=1):
    """
    Extract records from the current listing page.
    Returns (records_list, should_stop).
    """
    logger.info(f"{'='*60}")
    logger.info(f"PAGE {page_num}: Extracting records...")
    logger.info(f"{'='*60}")

    page.wait_for_selector("div.page-content ul li", timeout=60000)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    listing_html_filename = f"listing_page_{page_num}_{timestamp}.html"
    listing_html_filepath = os.path.join(
        HTML_OUTPUT_DIR, listing_html_filename)

    html_content = page.content()
    logger.info(f"HTML content: {html_content}")
    with open(listing_html_filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"Saved HTML: {listing_html_filename}")

    page_records = extract_records_from_html(html_content)
    logger.info(f"Found {len(page_records)} records on page")

    filtered_records = []
    should_stop = False

    for record in page_records:
        try:
            record_date = datetime.datetime.strptime(
                record["date"], "%Y-%m-%d")

            if ONE_TIME_START_DATE and ONE_TIME_END_DATE:
                if ONE_TIME_START_DATE <= record_date <= ONE_TIME_END_DATE:
                    filtered_records.append(record)
                elif record_date < ONE_TIME_START_DATE:
                    logger.info(
                        f"Record {record_date.date()} is before range start {ONE_TIME_START_DATE.date()}, stopping extraction")
                    should_stop = True
                    break
                else:
                    logger.info(
                        f"Skipping record {record_date.date()} — after range end {ONE_TIME_END_DATE.date()}")
            elif record_date >= CUTOFF_DATE:
                filtered_records.append(record)
            else:
                logger.info(
                    f"Found record older than cutoff: {record_date.date()} < {CUTOFF_DATE.date()}, stopping extraction")
                should_stop = True
                break
        except Exception as e:
            logger.warning(f"Error parsing date for record: {e}")
            filtered_records.append(record)

    logger.info(
        f"Kept {len(filtered_records)} records (filtered out {len(page_records) - len(filtered_records)} old records)")
    return filtered_records, should_stop


# ---------------------------------------------------------------------------
# LLM deal matching
# ---------------------------------------------------------------------------

def match_deal_with_llm(title_en, title_cn):
    """
    Use LLM to match SAMR public notice title against deals.
    Returns deal_id string or None.
    """
    global deals
    if not deals:
        logger.warning("Deals list is empty, reloading from MongoDB...")
        load_deals()
    if not deals:
        logger.warning("No deals with company names found")
        return None
    return llm_match_deal_id(
        regulator_name="SAMR China Public Notice",
        case_sections={
            "TITLE (English translation)": title_en,
            "TITLE (Original Chinese)": title_cn,
        },
        source_label="the public notice title",
        deals=deals,
    )

def convert_datetime_to_string(obj):
    """Recursively convert datetime objects to strings for JSON serialization."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    elif isinstance(obj, datetime.date):
        return obj.isoformat()
    elif hasattr(obj, 'isoformat') and callable(getattr(obj, 'isoformat')):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    elif isinstance(obj, dict):
        return {key: convert_datetime_to_string(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetime_to_string(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Email – matched deal
# ---------------------------------------------------------------------------

def generate_samr_email_html(samr_data, deal_match):
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    title_cn = samr_data.get("title_cn", "N/A")
    title_en = samr_data.get("title_en", "N/A")
    date = samr_data.get("date", "N/A")
    url = samr_data.get("url", "")

    title_text = (
        f"SAMR China – {target} / {acquirer}"
        if target != "N/A" and acquirer != "N/A"
        else f"SAMR China Match – {title_en[:50]}"
    )
    subject = build_subject("samr_public", "new", deal_match)

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #e74c3c;">
      {escape_html(title_text)}
    </h2>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Deal ID:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(deal_id))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Target:</td>
        <td style="padding:8px; color:#333;">{escape_html(target)}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Acquirer:</td>
        <td style="padding:8px; color:#333;">{escape_html(acquirer)}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_cn))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_en))}</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">
            View SAMR Detail Page
          </a>
        </td>
      </tr>"""

    html_email += """
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China regulatory notice matches.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_samr_email_via_webhook(samr_data, deal_match, matched_by_regex: bool = False):
    try:
        subject, html_email = generate_samr_email_html(samr_data, deal_match)
        subject = apply_regex_match_subject(
            subject, matched_by_regex=matched_by_regex)
        logger.info(f"Generated email subject: {subject}")

        webhook_url = resolve_webhook_url(subject)
        logger.info(f"Sending email via n8n webhook: {webhook_url}")

        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")

        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': deal_id,
            'target': target,
            'acquirer': acquirer,
            'title_cn': samr_data.get("title_cn", "N/A"),
            'title_en': samr_data.get("title_en", "N/A"),
            'date': samr_data.get("date", "N/A"),
            'url': samr_data.get("url", ""),
        }

        return post_email_payload(payload, subject=subject)

    except requests.exceptions.RequestException as e:
        logger.warning(f"Error sending email via webhook: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error generating/sending email: {e}")
        return False


# ---------------------------------------------------------------------------
# Email – unmatched USA-related
# ---------------------------------------------------------------------------

def generate_unmatched_samr_email_html(record: dict) -> tuple:
    title_cn = record.get("title_cn", "N/A")
    title_en = record.get("title_en", "N/A")
    date_str = record.get("date", "N/A")
    url = record.get("url", "")

    subject = build_subject("samr_public", "new")

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #f59e0b;">
      SAMR China Public Notice (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">🇺🇸 USA-RELATED</div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date_str))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_cn))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_en))}</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">
            View SAMR Detail Page
          </a>
        </td>
      </tr>"""

    html_email += """
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China public notice monitoring.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_unmatched_samr_email_via_webhook(record: dict) -> bool:
    try:
        subject, html_email = generate_unmatched_samr_email_html(record)
        logger.info(f"Generated email subject: {subject}")

        webhook_url = resolve_webhook_url(subject)
        logger.info(f"Sending email via n8n webhook: {webhook_url}")

        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': 'N/A',
            'target': 'N/A',
            'acquirer': 'N/A',
            'title_cn': record.get("title_cn", "N/A"),
            'title_en': record.get("title_en", "N/A"),
            'date': record.get("date", "N/A"),
            'url': record.get("url", ""),
            'is_unmatched': True,
            'usa_related': True,
        }

        return post_email_payload(payload, subject=subject)

    except requests.exceptions.RequestException as e:
        logger.warning(f"Error sending email via webhook: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error generating/sending email: {e}")
        return False


# ---------------------------------------------------------------------------
# Save matched data to the deals collection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(headless=True):
    """
    Main execution function.

    Returns:
        dict with success, extraction_date, total_extracted, total_matched, etc.
    """
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    global all_extracted_records, matched_data, deals
    run_start = datetime.datetime.now()
    error_items: list[dict[str, Any]] = []
    all_extracted_records = []
    matched_data = []
    new_records: list[dict] = []
    skipped = 0
    translated = 0
    llm_match_count = 0
    regex_match_count = 0
    logger.info("=" * 60)
    logger.info("[STEP 1] Starting SAMR Public Notice Register")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    try:
        success, msg = init_mongodb_connection(ENV_PATH)
        if success:
            logger.info(msg)
        else:
            collect_error(
                error_items,
                f"MongoDB initialization failed: {msg}",
                step="init_mongodb_connection",
            )
            return {"success": False, "error": msg}

        logger.info("[STEP 1.1] Loading deals from MongoDB...")
        load_deals()

        logger.info("[STEP 1.2] PHASE 1: EXTRACT ALL RECORDS (parse only)")
        logger.info("[STEP 1.3] Mode: Scraping SAMR website")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            logger.info(f"Page: {page}")

            try:
                logger.info(f"[STEP 2] Calling BASE_URL: {BASE__SCRAPER_URL}")
                _goto_with_retry(page, BASE__SCRAPER_URL)
                logger.info("   Loaded")

                page_num = 1
                while True:
                    page_records, should_stop = extract_page_records(
                        page, page_num)
                    logger.info(
                        f"Page records: {len(page_records)}, should_stop: {should_stop}")
                    logger.info(f"Page records: {page_records}")
                    all_extracted_records.extend(page_records)

                    if should_stop:
                        logger.info("Stopped: Cutoff date reached")
                        break

                    try:
                        next_btn = page.get_by_text("下一页")
                        next_class = next_btn.get_attribute("class")

                        if next_class and "disabled" in next_class:
                            logger.info("Stopped: No more pages")
                            break

                        logger.info(f"Navigating to page {page_num + 1}...")
                        next_btn.click()
                        page.wait_for_timeout(2000)
                        page_num += 1

                    except Exception as e:
                        logger.exception(f"Pagination error: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="pagination",
                            context={"page": page_num},
                        )
                        break

            except Exception as e:
                logger.exception(f"Scraping error: {e}")
                collect_error(
                    error_items,
                    f"Scraping error: {e}",
                    step="scrape_listing",
                    context={"url": BASE__SCRAPER_URL},
                )
            finally:
                browser.close()

        logger.info(
            f"Total records extracted from listing pages: {len(all_extracted_records)}")

        logger.info(
            "PHASE 2: FILTER ALREADY-PROCESSED RECORDS AND TRANSLATE NEW ONES")
        logger.info("Checking which records are already in samr_cases...")

        for record in all_extracted_records:
            detail_url = record.get("url")
            date_str = record.get("date", "")
            title_cn = record.get("title_cn", "")

            if detail_url and record_exists_in_samr_cases(detail_url):
                skipped += 1
                continue

            if not detail_url:
                logger.warning(
                    f"Skipping record with no URL: {title_cn[:80]}")
                continue

            title_en = translate_with_claude(title_cn)
            translated += 1
            record["title_en"] = title_en
            new_records.append(record)
            logger.info(f"Title EN: {title_en}")
            logger.info(f"Extracted: {date_str} - {title_en}")

        if skipped > 0:
            logger.info(f"Skipped {skipped} already-processed records")
        logger.info(f"Translated {translated} new records")
        logger.info(f"{len(new_records)} new records to process")

        logger.info("PHASE 3: MATCH RECORDS WITH DEALS")

        for idx, record in enumerate(new_records, 1):
            try:
                title_en = record.get("title_en", "")
                title_cn = record.get("title_cn", "")
                date_str = record.get("date", "")
                url = record.get("url", "")

                logger.info(
                    f"[{idx}/{len(new_records)}] {date_str} - {title_en[:70]}...")

                samr_case_doc = {
                    "url": url,
                    "title_cn": title_cn,
                    "title_en": title_en,
                    "date": date_str,
                    "deal_id": None,
                }

                if title_en == "[Translation failed]":
                    logger.info("  Skipped (translation failed)")
                    save_to_samr_cases(samr_case_doc)
                    continue

                matched_deal_id = None
                matched_by_regex = False
                try:
                    matched_deal_id = match_deal_with_llm(title_en, title_cn)
                except Exception as e:
                    logger.exception(f"  LLM match failed: {e}")
                    collect_error(
                        error_items,
                        str(e),
                        step="match_deal_with_llm",
                        context={"title": title_en[:80], "url": url},
                    )
                    matched_deal_id = None

                if matched_deal_id:
                    llm_match_count += 1
                    logger.info(f"  LLM match: deal_id={matched_deal_id}")
                else:
                    matched_deal_id = regex_match_samr_deal(title_en, deals)
                    if matched_deal_id:
                        matched_by_regex = True
                        regex_match_count += 1
                        logger.info(
                            f"  Regex fallback matched deal_id={matched_deal_id}")
                    else:
                        logger.info("  No match (LLM + regex both returned None)")

                if matched_deal_id:
                    deal_id = matched_deal_id
                    deal_match = None
                    for deal in deals:
                        if deal.get("deal_id") == deal_id:
                            deal_match = deal
                            logger.info(f"  Found deal by ID: {deal_id}")
                            break

                    if deal_match:
                        matched_result = {
                            "deal_id": deal_match.get("deal_id", ""),
                            "title_cn": title_cn,
                            "title_en": title_en,
                            "url": url,
                            "date": date_str,
                            "matched_deal": deal_match,
                        }
                        matched_data.append(matched_result)
                        logger.info("  Match added to results!")

                        samr_case_doc["deal_id"] = deal_match.get(
                            "deal_id", "")

                        try:
                            samr_data = {
                                k: v for k, v in matched_result.items() if k != "matched_deal"}
                            send_samr_email_via_webhook(
                                samr_data, deal_match,
                                matched_by_regex=matched_by_regex)
                        except Exception as e:
                            logger.exception(
                                f"  Error sending email notification: {e}")
                            collect_error(
                                error_items,
                                str(e),
                                step="send_email",
                                context={"title": title_en[:80], "url": url},
                            )
                    else:
                        logger.warning(
                            f"  Match returned deal_id={deal_id} but deal not found in loaded deals")
                else:
                    try:
                        company_details = title_en if title_en and title_en != "[Translation failed]" else title_cn
                        is_usa_related = verify_usa_relation(
                            company_details=company_details,
                            case_type="CHINA",
                        )
                        if is_usa_related:
                            logger.info(
                                "  USA-related case detected - sending email notification")
                            try:
                                send_unmatched_samr_email_via_webhook(record)
                            except Exception as e:
                                logger.exception(f"  Error sending USA email: {e}")
                                collect_error(
                                    error_items,
                                    str(e),
                                    step="send_email",
                                    context={"title": title_en[:80], "url": url},
                                )
                        else:
                            logger.info("  Not USA-related - no action taken")
                    except Exception as e:
                        logger.exception(f"  Error verifying USA relation: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="verify_usa_relation",
                            context={"title": title_en[:80], "url": url},
                        )

                save_to_samr_cases(samr_case_doc)
            except Exception as e:
                logger.exception(f"Error processing record #{idx}: {e}")
                collect_error(
                    error_items,
                    str(e),
                    step="process_record",
                    context={
                        "url": record.get("url", ""),
                        "title": (record.get("title_en") or "")[:80],
                    },
                )

        return {
            "success": True,
            "extraction_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_extracted": len(all_extracted_records),
            "total_new": len(new_records),
            "total_matched": len(matched_data),
            "matched_results": convert_datetime_to_string(matched_data),
        }

    except Exception as e:
        logger.exception(f"Unhandled error in main: {e}")
        collect_error(
            error_items,
            f"Unhandled error in main: {e}",
            step="run_main",
        )
        return {"success": False, "error": str(e)}

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        logger.info("ALL DONE!")
        logger.info(f"Total records extracted: {len(all_extracted_records)}")
        logger.info(f"New records processed: {len(new_records)}")
        logger.info(f"Total matches found: {len(matched_data)}")
        elapsed = round((datetime.datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(
            f"  Total records extracted      : {len(all_extracted_records)}")
        logger.info(f"  Skipped (already in DB)      : {skipped}")
        logger.info(f"  Translated (new)             : {translated}")
        logger.info(f"  New records processed        : {len(new_records)}")
        logger.info(f"  Total matches found          : {len(matched_data)}")
        logger.info(f"  LLM deal matches             : {llm_match_count}")
        logger.info(f"  Regex fallback matches       : {regex_match_count}")
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    import sys

    headless_mode = False

    if len(sys.argv) > 1:
        if sys.argv[1] == "--headed":
            headless_mode = False
            logger.info("Mode: Running with visible browser")
        elif sys.argv[1] == "--help":
            logger.info("Usage: python new_samr_public_notice_db.py [OPTIONS]")
            logger.info(
                "Options: --headed (visible browser), --help (this message)")
            logger.info(
                "Default: Scrape new pages from SAMR website in headless mode")
            sys.exit(0)

    logger.info("Mode: Scrape new pages from SAMR website")
    main(headless=headless_mode)
