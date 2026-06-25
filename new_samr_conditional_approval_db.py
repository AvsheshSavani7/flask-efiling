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
from bs4 import BeautifulSoup
import re
from bson import ObjectId
from mongodb_connection import (
    get_deals_collection,
    get_database,
    init_mongodb_connection,
    is_connected,
)
from html import escape as escape_html
from llm_verification_service import verify_usa_relation
from scraper_error_utils import collect_error, send_error_summary
from log_utils import cleanup_old_logs, refresh_log_file
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload
from typing import Any

# Configuration
CUTOFF_DATE = (datetime.datetime.now() - datetime.timedelta(days=15)).replace(
    hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.datetime.now().replace(
#     hour=0, minute=0, second=0, microsecond=0)
BASE__SCRAPER_URL = "https://www.samr.gov.cn/fldes/tzgg/ftj/"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "samr_conditional_html_pages"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "samr-cases-conditional"
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

# Global state
deals = []
all_extracted_records = []
matched_data = []


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_samr_conditional_collection():
    db = get_database()
    if db is None:
        return None
    return db["samr_conditional"]


def get_samr_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["samr_cases"]


def record_exists_in_samr_conditional(url):
    """Return True if this record URL was already processed."""
    try:
        col = get_samr_conditional_collection()
        if col is None:
            return False
        return col.find_one({"url": url}) is not None
    except Exception as e:
        logger.warning(f"Error checking samr_conditional: {e}")
        return False


def save_to_samr_conditional(record):
    """
    Save a processed record to samr_conditional collection.

    Document shape:
    {
        "url": str,
        "title_cn": str,
        "title_en": str,
        "date": str,
        "processed_at": str,
    }
    """
    try:
        col = get_samr_conditional_collection()
        if col is None:
            logger.warning("samr_conditional collection not available")
            return False

        record["processed_at"] = datetime.datetime.now().isoformat()
        col.update_one({"url": record["url"]}, {"$set": record}, upsert=True)
        logger.info(f"Saved to samr_conditional: {record['url'][:80]}...")
        return True
    except Exception as e:
        logger.warning(f"Error saving to samr_conditional: {e}")
        return False


def get_all_samr_cases():
    """Fetch samr_cases documents where is_open is true (or not set)."""
    try:
        col = get_samr_cases_collection()
        if col is None:
            logger.warning("samr_cases collection not available")
            return []
        query = {
            "$or": [
                {"is_open": True},
                {"is_open": {"$exists": False}},
            ]
        }
        docs = list(col.find(query))
        for doc in docs:
            if "_id" in doc:
                doc["_id_str"] = str(doc["_id"])
        logger.info(f"Fetched {len(docs)} open samr_cases records")
        return docs
    except Exception as e:
        logger.warning(f"Error fetching samr_cases: {e}")
        return []


def update_samr_case_conditional(samr_case, conditional_data, deal_id=None):
    """
    Push a conditional object to the samr_cases record's 'condition' array
    and set is_open=false.
    Optionally update deal_id if provided.
    """
    try:
        col = get_samr_cases_collection()
        if col is None:
            logger.warning("samr_cases collection not available")
            return False

        update_ops = {
            "$push": {"condition": conditional_data},
            "$set": {"is_open": False},
        }
        if deal_id:
            update_ops["$set"]["deal_id"] = deal_id

        col.update_one({"_id": samr_case["_id"]}, update_ops)
        title = samr_case.get("title_en", samr_case.get("title_cn", ""))[:60]
        logger.info(f"Updated samr_case with conditional node: {title}...")
        return True
    except Exception as e:
        logger.warning(f"Error updating samr_case: {e}")
        return False


# ---------------------------------------------------------------------------
# Deals – load from MongoDB
# ---------------------------------------------------------------------------

def get_deals_from_mongodb():
    """Fetch open/unknown deals from the deals collection."""
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
        params = {"client": "gtx", "sl": "zh-CN",
                  "tl": "en", "dt": "t", "q": text}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            return response.json()[0][0][0]
    except Exception as e:
        logger.warning(f"Translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


def translate_with_openai(text):
    """Translate Chinese text to English using GPT-5.2 with web search."""
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


# ---------------------------------------------------------------------------
# Listing-page HTML parsing
# ---------------------------------------------------------------------------

def extract_records_from_html(html_content):
    """
    Extract listing records from HTML (parse only — no translation).
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
    """Extract records from the current listing page. Returns (records_list, should_stop)."""
    logger.info(f"{'='*60}")
    logger.info(f"PAGE {page_num}: Extracting records...")
    logger.info(f"{'='*60}")

    page.wait_for_selector("div.page-content ul li", timeout=60000)

    html_content = page.content()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(
        HTML_OUTPUT_DIR, f"listing_page_{page_num}_{timestamp}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"Saved HTML: {os.path.basename(html_path)}")

    page_records = extract_records_from_html(html_content)
    logger.info(f"Found {len(page_records)} records on page")

    filtered_records = []
    should_stop = False

    for record in page_records:
        try:
            record_date = datetime.datetime.strptime(
                record["date"], "%Y-%m-%d")
            if record_date >= CUTOFF_DATE:
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
# LLM: match a conditional record against samr_cases titles
# ---------------------------------------------------------------------------

def match_record_to_samr_cases(record, samr_cases_list):
    """
    Ask LLM whether this conditional-approval record matches any samr_cases
    record title. Returns the matched samr_case dict or None.
    """
    cases_lines = []
    for sc in samr_cases_list:
        sc_id = sc.get("_id_str", str(sc.get("_id", "")))
        title_en = sc.get("title_en", "")
        title_cn = sc.get("title_cn", "")
        cases_lines.append(
            f"ID: {sc_id} | Title (EN): {title_en} | Title (CN): {title_cn}")

    if not cases_lines:
        return None

    cases_text = "\n".join(cases_lines)

    prompt = f"""
You are an M&A analyst. Below is a SAMR conditional-approval notice title and a list of SAMR public-notice case titles.

CONDITIONAL APPROVAL RECORD:
- Title (CN): {record['title_cn']}
- Title (EN): {record['title_en']}
- Date: {record['date']}
- URL: {record['url']}

SAMR PUBLIC NOTICE CASES:
{cases_text}

TASK:
Determine if the conditional-approval record refers to the SAME deal/transaction as any of the public-notice cases listed above.

RULES:
1. Match only if both titles clearly refer to the same transaction (same acquirer AND same target).
2. Allow normal name variations (Inc./Incorporated, Ltd/Limited, spacing/casing).
3. Do NOT match on partial overlap, industry similarity, or single-company match.

RESPONSE:
- If matched, respond exactly: Match: <ID>
- If no match, respond exactly: None
"""

    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": "You match M&A case records. Reply with 'Match: <ID>' or 'None'."},
                {"role": "user", "content": prompt},
            ]
        )
        result = response.choices[0].message.content.strip()
        logger.info(f"  samr_cases match LLM: {result}")

        if result.lower().startswith("match"):
            matched_id = result.replace(
                "Match:", "").replace("match:", "").strip()
            for sc in samr_cases_list:
                sc_id = sc.get("_id_str", str(sc.get("_id", "")))
                if sc_id == matched_id:
                    return sc
        return None
    except Exception as e:
        logger.warning(f"  LLM Error (samr_cases match): {e}")
        raise


# ---------------------------------------------------------------------------
# LLM: match a samr_case title against deals
# ---------------------------------------------------------------------------

def normalize_company(name):
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").strip()


def match_samr_case_to_deals(samr_case):
    """
    Use LLM to match SAMR case title against deals.
    Returns deal_match dict or None.
    """
    global deals
    if not deals:
        load_deals()
    if not deals:
        return None

    title_en = samr_case.get("title_en", "")
    title_cn = samr_case.get("title_cn", "")
    logger.info(f"samr_case title: {title_en} {title_cn}")

    deal_id = llm_match_deal_id(
        regulator_name="SAMR China",
        case_sections={
            "TITLE (English)": title_en,
            "TITLE (Chinese)": title_cn,
        },
        source_label="the public notice title",
        deals=deals,
    )
    if not deal_id:
        return None

    for deal in deals:
        if deal.get("deal_id") == deal_id:
            return deal

    logger.warning(f"  LLM returned deal_id '{deal_id}' but not found in loaded deals")
    return None

def convert_datetime_to_string(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    elif isinstance(obj, datetime.date):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: convert_datetime_to_string(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetime_to_string(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Email – matched deal (conditional closed)
# ---------------------------------------------------------------------------

def generate_conditional_email_html(samr_case, deal_match, conditional_data):
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    case_title_en = samr_case.get("title_en", "N/A")
    case_title_cn = samr_case.get("title_cn", "N/A")
    case_date = samr_case.get("date", "N/A")
    case_url = samr_case.get("url", "")
    cond_title_en = conditional_data.get("title_en", "N/A")
    cond_url = conditional_data.get("url", "")
    cond_date = conditional_data.get("date", "N/A")

    title_text = (
        f"SAMR China Conditional Approval – {target} / {acquirer}"
        if target != "N/A" and acquirer != "N/A"
        else f"SAMR China Conditional Approval – {cond_title_en[:50]}"
    )
    subject = build_subject("samr_conditional", "new", deal_match)

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
        <td style="padding:8px; font-weight:bold; color:#555;">Conditional Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(cond_date))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Public Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(case_date))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(case_title_cn))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(case_title_en))}</td>
      </tr>"""

    if cond_url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Conditional Approval URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(cond_url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">View Conditional Approval</a>
        </td>
      </tr>"""

    if case_url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Public Notice URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(case_url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">View Public Notice</a>
        </td>
      </tr>"""

    html_email += """
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China conditional approval notice matches.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_conditional_email_via_webhook(samr_case, deal_match, conditional_data):
    try:
        subject, html_email = generate_conditional_email_html(
            samr_case, deal_match, conditional_data)
        logger.info(f"Generated email subject: {subject}")

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
            'title_cn': samr_case.get("title_cn", "N/A"),
            'title_en': samr_case.get("title_en", "N/A"),
            'date': conditional_data.get("date", "N/A"),
            'url': conditional_data.get("url", ""),
        }

        return post_email_payload(payload, subject=subject, timeout=60)
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error sending email via webhook: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error generating/sending email: {e}")
        return False


# ---------------------------------------------------------------------------
# Email – unmatched USA-related
# ---------------------------------------------------------------------------

def generate_unmatched_conditional_email_html(samr_case, conditional_data):
    title_cn = samr_case.get(
        "title_cn", conditional_data.get("title_cn", "N/A"))
    title_en = samr_case.get(
        "title_en", conditional_data.get("title_en", "N/A"))
    date_str = samr_case.get("date", "N/A")
    case_url = samr_case.get("url", "")
    cond_url = conditional_data.get("url", "")
    cond_date = conditional_data.get("date", "N/A")

    subject = build_subject("samr_conditional", "new")

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
      SAMR China Conditional Approval (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; font-weight:bold;">🇺🇸 USA-RELATED</div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Conditional Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(cond_date))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Public Notice Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(date_str))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Title (Chinese):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_cn))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Title (English):</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title_en))}</td>
      </tr>"""

    if cond_url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Conditional Approval URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(cond_url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">View Conditional Approval</a>
        </td>
      </tr>"""

    if case_url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Public Notice URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(case_url)}" style="color:#e74c3c; text-decoration:none;" target="_blank">View Public Notice</a>
        </td>
      </tr>"""

    html_email += """
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from SAMR China conditional approval monitoring.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_unmatched_conditional_email_via_webhook(samr_case, conditional_data):
    try:
        subject, html_email = generate_unmatched_conditional_email_html(
            samr_case, conditional_data)
        logger.info(f"Generated email subject: {subject}")

        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': 'N/A',
            'target': 'N/A',
            'acquirer': 'N/A',
            'title_cn': samr_case.get("title_cn", "N/A"),
            'title_en': samr_case.get("title_en", "N/A"),
            'date': conditional_data.get("date", "N/A"),
            'url': conditional_data.get("url", ""),
            'is_unmatched': True,
            'usa_related': True,
        }

        return post_email_payload(payload, subject=subject, timeout=60)
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error sending email via webhook: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error generating/sending email: {e}")
        return False


# ---------------------------------------------------------------------------
# Save conditional data to deals collection
# ---------------------------------------------------------------------------

def save_conditional_data_to_deal(deal_match, conditional_data):
    """Save conditional approval data to the deal under 'samr_conditional' node."""
    try:
        if not is_connected():
            logger.warning("MongoDB connection not available")
            return False

        collection = get_deals_collection()
        if collection is None:
            logger.warning("Deals collection not available")
            return False

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
            or_conds = []
            if acquirer:
                or_conds.extend(
                    [{"acquirer": acquirer}, {"acquire_name": acquirer}])
            if target:
                or_conds.extend([{"target": target}, {"target_name": target}])
            if or_conds:
                query = {"$or": or_conds}

        if not query:
            logger.warning("Cannot identify deal, skipping save")
            return False

        data = convert_datetime_to_string(conditional_data)

        result = collection.update_one(
            query,
            {"$set": {"samr_conditional": data, "conditionally_approved": True}},
        )
        logger.info(
            f"Update deals: matched={result.matched_count}, modified={result.modified_count}")

        if result.modified_count > 0 or result.matched_count > 0:
            logger.info("Saved samr_conditional to deal record")
            return True
        else:
            logger.warning("Deal not found in MongoDB")
            return False
    except Exception as e:
        logger.exception(f"Error saving conditional to deal: {e}")
        return False


# ---------------------------------------------------------------------------
# Process a single conditional record
# ---------------------------------------------------------------------------

def process_record(record, samr_cases_list, error_items: list[dict[str, Any]] | None = None):
    """
    For one conditional-approval record:
    1. Match against samr_cases titles via LLM
    2. If matched → set is_open=false, push to condition[] array
       a. If samr_case has deal_id → email "conditionally closed"
       b. If no deal_id → LLM match against deals → email if matched
       c. If no deal match → check USA-related → email if yes
    3. If not matched → skip (just save to samr_conditional)
    """
    logger.info(f"Processing record: {record}")
    logger.info(f"Samr cases list: {samr_cases_list}")
    title_en = record.get("title_en", "")
    title_cn = record.get("title_cn", "")
    record_label = f"{title_en}"
    logger.info(f"  Matching against samr_cases: {record_label}")

    conditional_data = {
        "title_cn": title_cn,
        "title_en": title_en,
        "url": record.get("url", ""),
        "date": record.get("date", ""),
    }

    try:
        matched_case = match_record_to_samr_cases(record, samr_cases_list)
    except Exception as e:
        logger.exception(f"  samr_cases match failed: {e}")
        if error_items is not None:
            collect_error(
                error_items,
                str(e),
                step="match_record_to_samr_cases",
                context={
                    "title": title_en[:80],
                    "url": record.get("url", ""),
                },
            )
        return

    if not matched_case:
        logger.info("  No samr_cases match")
        return

    case_title = matched_case.get("title_en", matched_case.get("title_cn", ""))
    logger.info(f"  Matched samr_case: {case_title}")

    existing_deal_id = matched_case.get("deal_id")

    if existing_deal_id:
        # Case A: samr_case already has a deal_id
        logger.info(f"  samr_case has deal_id: {existing_deal_id}")
        update_samr_case_conditional(matched_case, conditional_data)

        deal_match = None
        for deal in deals:
            if deal.get("deal_id") == existing_deal_id:
                deal_match = deal
                break

        if deal_match:
            save_conditional_data_to_deal(deal_match, conditional_data)
            send_conditional_email_via_webhook(
                matched_case, deal_match, conditional_data)
            matched_data.append({
                "deal_id": existing_deal_id,
                "samr_case_title": case_title,
                "conditional": conditional_data,
            })
        else:
            logger.warning(
                f"  deal_id {existing_deal_id} not found in loaded deals (may be closed)")
    else:
        # Case B: no deal_id → try LLM deal matching
        logger.info("  No deal_id on samr_case, trying LLM deal match...")
        logger.info(f"  samr_case title: {title_en} {title_cn}")
        try:
            deal_match = match_samr_case_to_deals(matched_case)
        except Exception as e:
            logger.exception(f"  Deal match failed: {e}")
            if error_items is not None:
                collect_error(
                    error_items,
                    str(e),
                    step="match_samr_case_to_deals",
                    context={"title": case_title[:80], "url": record.get("url", "")},
                )
            deal_match = None

        if deal_match:
            deal_id = deal_match.get("deal_id", "")
            logger.info(f"  Deal matched: {deal_id}")
            update_samr_case_conditional(
                matched_case, conditional_data, deal_id=deal_id)
            save_conditional_data_to_deal(deal_match, conditional_data)
            send_conditional_email_via_webhook(
                matched_case, deal_match, conditional_data)
            matched_data.append({
                "deal_id": deal_id,
                "samr_case_title": case_title,
                "conditional": conditional_data,
            })
        else:
            # Case C: no deal match → check USA-related
            logger.info("  No deal match. Checking USA relation...")
            update_samr_case_conditional(matched_case, conditional_data)

            try:
                company_details = title_en if title_en and title_en != "[Translation failed]" else title_cn
                is_usa_related = verify_usa_relation(
                    company_details=company_details,
                    case_type="CHINA",
                )
                if is_usa_related:
                    logger.info("  USA-related – sending email")
                    send_unmatched_conditional_email_via_webhook(
                        matched_case, conditional_data)
                else:
                    logger.info("  Not USA-related – no email")
            except Exception as e:
                logger.exception(f"  Error verifying USA relation: {e}")
                if error_items is not None:
                    collect_error(
                        error_items,
                        str(e),
                        step="verify_usa_relation",
                        context={"title": case_title[:80], "url": record.get("url", "")},
                    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(headless=True):
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
    logger.info("=" * 60)
    logger.info(" Starting SAMR Conditional Cases Register")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    try:
        ok, msg = init_mongodb_connection(ENV_PATH)
        if ok:
            logger.info(msg)
        else:
            collect_error(
                error_items,
                f"MongoDB initialization failed: {msg}",
                step="init_mongodb_connection",
            )
            return {"success": False, "error": msg}

        logger.info("Loading deals from MongoDB...")
        load_deals()

        logger.info("Loading samr_cases from MongoDB...")
        samr_cases_list = get_all_samr_cases()

        logger.info("PHASE 1: EXTRACT CONDITIONAL APPROVAL RECORDS")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            logger.info(f"Page: {page}")

            try:
                logger.info(f"Calling BASE_URL: {BASE__SCRAPER_URL}")
                _goto_with_retry(page, BASE__SCRAPER_URL)
                logger.info("   Loaded")

                page_num = 1
                while True:
                    page_records, should_stop = extract_page_records(
                        page, page_num)
                    all_extracted_records.extend(page_records)
                    logger.info(f"Page records: {page_records}")

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

        logger.info(f"Total records extracted: {len(all_extracted_records)}")

        logger.info(
            "PHASE 2: FILTER ALREADY-PROCESSED RECORDS AND TRANSLATE NEW ONES")

        for rec in all_extracted_records:
            url = rec.get("url")
            date_str = rec.get("date", "")
            title_cn = rec.get("title_cn", "")

            if url and record_exists_in_samr_conditional(url):
                skipped += 1
                continue

            if not url:
                logger.warning(
                    f"Skipping record with no URL: {title_cn[:80]}")
                continue

            title_en = translate_with_openai(title_cn)
            translated += 1
            rec["title_en"] = title_en
            new_records.append(rec)
            logger.info(f"Extracted: {date_str} - {title_en}")

        if skipped:
            logger.info(f"Skipped {skipped} already-processed records")
        logger.info(f"Translated {translated} new records")
        logger.info(f"{len(new_records)} new records to process")

        logger.info("PHASE 3: MATCH RECORDS AGAINST samr_cases & DEALS")

        for idx, record in enumerate(new_records, 1):
            try:
                title_en = record.get("title_en", "")
                date_str = record.get("date", "")

                logger.info(f"[{idx}/{len(new_records)}] {date_str} - {title_en[:70]}")

                if title_en == "[Translation failed]":
                    logger.info("  Skipped (translation failed)")
                    save_to_samr_conditional({
                        "url": record.get("url", ""),
                        "title_cn": record.get("title_cn", ""),
                        "title_en": title_en,
                        "date": date_str,
                    })
                    continue

                process_record(record, samr_cases_list, error_items=error_items)

                save_to_samr_conditional({
                    "url": record.get("url", ""),
                    "title_cn": record.get("title_cn", ""),
                    "title_en": title_en,
                    "date": date_str,
                })
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
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    import sys

    headless_mode = True

    if len(sys.argv) > 1:
        if sys.argv[1] == "--headed":
            headless_mode = False
            logger.info("Mode: Running with visible browser")
        elif sys.argv[1] == "--help":
            logger.info(
                "Usage: python new_samr_conditional_approval_db.py [OPTIONS]")
            logger.info(
                "Options: --headed (visible browser), --help (this message)")
            logger.info(
                "Default: Scrape new pages from SAMR website in headless mode")
            sys.exit(0)

    logger.info("Mode: Scrape SAMR conditional approval pages")
    main(headless=headless_mode)
