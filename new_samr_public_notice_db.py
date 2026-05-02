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
from bs4 import BeautifulSoup
import re
from mongodb_connection import (
    get_deals_collection,
    get_database,
    init_mongodb_connection,
)
from html import escape as escape_html
from llm_verification_service import verify_usa_relation
from error_email_service import send_error_email
from log_utils import cleanup_old_logs

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

BASE_URL = "https://www.samr.gov.cn/fldes/ajgs/jyaj/"
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
    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


def _log_critical_error_and_email(msg: str, context: dict | None = None):
    """Immediate error email — use ONLY for critical startup / fatal failures."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context or {},
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )

def _goto_with_retry(page, url, max_retries=3):
    """Navigate to a URL with retries and fallback wait strategies."""
    strategies = [
        ("networkidle", 60000),
        ("domcontentloaded", 60000),
        ("domcontentloaded", 90000),
    ]
    for attempt in range(max_retries):
        wait_until, timeout = strategies[min(attempt, len(strategies) - 1)]
        try:
            print(f"   Attempt {attempt + 1}/{max_retries} (wait_until={wait_until}, timeout={timeout}ms)")
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:
            print(f"   ⚠️ Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                delay = 5 * (attempt + 1)
                print(f"   ⏳ Waiting {delay}s before retry...")
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
        print(f"⚠️ Error checking samr_cases: {e}")
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
            print("⚠️ samr_cases collection not available")
            return False

        record["processed_at"] = datetime.datetime.now().isoformat()
        record["is_open"] = True
        collection.update_one(
            {"url": record["url"]},
            {"$set": record},
            upsert=True,
        )
        print(f"💾 Saved to samr_cases: {record['url'][:80]}...")
        return True
    except Exception as e:
        print(f"⚠️ Error saving to samr_cases: {e}")
        return False


# ---------------------------------------------------------------------------
# Deals – load from MongoDB
# ---------------------------------------------------------------------------

def get_deals_from_mongodb():
    """Fetch deals from the deals collection."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
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

        print(f"✅ Fetched {len(all_deals)} deals from MongoDB")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals():
    global deals
    deals = get_deals_from_mongodb()
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
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
        print(f"⚠️ Translation failed for: {text[:50]}... → {e}")
    return "[Translation failed]"


# ---------------------------------------------------------------------------
# Listing-page HTML parsing
# ---------------------------------------------------------------------------

def extract_records_from_html(html_content):
    """
    Extract all records from a listing page HTML.
    Returns list of dicts: title_cn, title_en, url, date
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

            title_en = translate_to_english(title_cn)

            record = {
                "title_cn": title_cn,
                "title_en": title_en,
                "url": href,
                "date": date_str,
            }
            records.append(record)
            print(f"📋 Extracted: {date_str} - {title_en}")

        except Exception as e:
            print(f"⚠️ Error extracting record: {e}")
            continue

    return records


def extract_page_records(page, page_num=1):
    """
    Extract records from the current listing page.
    Returns (records_list, should_stop).
    """
    print(f"\n{'='*60}")
    print(f"📄 PAGE {page_num}: Extracting records...")
    print(f"{'='*60}")

    page.wait_for_selector("div.page-content ul li", timeout=60000)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    listing_html_filename = f"listing_page_{page_num}_{timestamp}.html"
    listing_html_filepath = os.path.join(
        HTML_OUTPUT_DIR, listing_html_filename)

    html_content = page.content()
    with open(listing_html_filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"💾 Saved HTML: {listing_html_filename}")

    page_records = extract_records_from_html(html_content)
    print(f"📊 Found {len(page_records)} records on page")

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
                    print(
                        f"🛑 Record {record_date.date()} is before range start {ONE_TIME_START_DATE.date()}")
                    print(f"   Stopping extraction")
                    should_stop = True
                    break
                else:
                    print(
                        f"⏭️ Skipping record {record_date.date()} — after range end {ONE_TIME_END_DATE.date()}")
            elif record_date >= CUTOFF_DATE:
                filtered_records.append(record)
            else:
                print(
                    f"🛑 Found record older than cutoff: {record_date.date()} < {CUTOFF_DATE.date()}")
                print(f"   Stopping extraction")
                should_stop = True
                break
        except Exception as e:
            print(f"⚠️ Error parsing date for record: {e}")
            filtered_records.append(record)

    print(f"✅ Kept {len(filtered_records)} records (filtered out {len(page_records) - len(filtered_records)} old records)")
    return filtered_records, should_stop


# ---------------------------------------------------------------------------
# LLM deal matching
# ---------------------------------------------------------------------------

def match_deal_with_llm(title_en, title_cn):
    """Match an English translated title with deals using LLM."""
    global deals

    if not deals:
        print("⚠️ Deals list is empty, reloading from MongoDB...")
        load_deals()

    deals_list = []
    for deal in deals:
        deal_info = {"deal_id": deal.get("deal_id", "")}
        target = deal.get("target") or deal.get("target_name", "")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")
        if target:
            deal_info["target"] = target
        if acquirer:
            deal_info["acquirer"] = acquirer

        target_aliases = deal.get("target_aliases") or []
        parent_aliases = deal.get("parent_aliases") or []
        if isinstance(target_aliases, list) and target_aliases:
            deal_info["target_aliases"] = target_aliases
        if isinstance(parent_aliases, list) and parent_aliases:
            deal_info["parent_aliases"] = parent_aliases

        if target or acquirer:
            deals_list.append(deal_info)

    if not deals_list:
        print("⚠️ No deals with company names found")
        return "None"

    lines = []
    for d in deals_list:
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target', 'N/A')} | Acquirer: {d.get('acquirer', 'N/A')}"
        t_aliases = d.get("target_aliases", []) or []
        p_aliases = d.get("parent_aliases", []) or []
        if t_aliases:
            line += f" | Target aliases: {', '.join(str(a) for a in t_aliases)}"
        if p_aliases:
            line += f" | Parent aliases: {', '.join(str(a) for a in p_aliases)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""
You are an M&A deal analyst. Given the translated title of a Chinese public notice, determine whether it explicitly relates to any of the companies listed below.

DEALS TO MATCH:
{deals_text}

TITLE (English translation):
{title_en}

TITLE (Original Chinese):
{title_cn}

INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned in the public notice title.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the public notice title.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the public notice title.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the public notice title:
   - one match for the Acquirer side
   - one match for the Target side
5. Do not return a match if only one side is present, even if that single company is an exact match.
6. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - "Inc." vs "Incorporated"
   - "Corp." vs "Corporation"
   - "Ltd" vs "Limited"
   - obvious spacing/casing differences
7. Do not match based only on sector, business type, article topic, indirect association, or partial deal overlap.
8. If the public notice title does not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
- If you find BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY in this format:
  Match: DEAL_ID
  Example: Match: 69665014d0bb42af1044aecd

- If no deal satisfies this rule, respond exactly:
  None
"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition. Your job is to find matches between public notice titles and deal companies. If the title matches or is contained in any Target and Acquirer name, return the match. Be thorough and check all possibilities."},
                {"role": "user", "content": prompt},
            ],
        )
        result = response.choices[0].message.content.strip()
        print(f"🧠 LLM Response: {result}")
        return result
    except Exception as e:
        print(f"⚠️ LLM Error: {e}")
        return "None"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

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
    subject = f"[FRMD] SAMR China Regulatory (New) – {target} / {acquirer}"

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


def send_samr_email_via_webhook(samr_data, deal_match):
    try:
        subject, html_email = generate_samr_email_html(samr_data, deal_match)
        print(f"📝 Generated email subject: {subject}")

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
        )
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

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

        response = requests.post(
            webhook_url, json=payload,
            headers={'Content-Type': 'application/json'}, timeout=30,
        )
        response.raise_for_status()
        print(
            f"✅ Email sent successfully via n8n webhook! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Email – unmatched USA-related
# ---------------------------------------------------------------------------

def generate_unmatched_samr_email_html(record: dict) -> tuple:
    title_cn = record.get("title_cn", "N/A")
    title_en = record.get("title_en", "N/A")
    date_str = record.get("date", "N/A")
    url = record.get("url", "")

    subject = f"[FRUD] SAMR China Public Notice (USA-Related) – {title_en[:60]}"

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
        print(f"📝 Generated email subject: {subject}")

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
        )
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

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

        response = requests.post(
            webhook_url, json=payload,
            headers={'Content-Type': 'application/json'}, timeout=30,
        )
        response.raise_for_status()
        print(
            f"✅ Email sent successfully via n8n webhook! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
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
    global all_extracted_records, matched_data, deals
    run_start = datetime.datetime.now()
    all_extracted_records = []
    matched_data = []
    logger.info("=" * 60)
    logger.info("Starting SAMR Public Notice Register")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    # Initialize MongoDB connection
    success, msg = init_mongodb_connection(ENV_PATH)
    if success:
        print(f"✅ {msg}")
    else:
        _log_critical_error_and_email(f"MongoDB initialization failed: {msg}", {"step": "init_mongodb_connection"})

    print("📊 Loading deals from MongoDB...")
    load_deals()

    # ------------------------------------------------------------------
    # PHASE 1: Scrape listing pages and extract records
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"🚀 PHASE 1: EXTRACT ALL RECORDS")
    print(f"{'='*60}\n")
    print("🌐 Mode: Scraping SAMR website\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            print(f"📍 Step 1: Calling BASE_URL")
            print(f"   URL: {BASE_URL}")
            _goto_with_retry(page, BASE_URL)
            print(f"   ✅ Loaded\n")

            page_num = 1
            while True:
                page_records, should_stop = extract_page_records(
                    page, page_num)
                all_extracted_records.extend(page_records)

                if should_stop:
                    print(f"\n✅ Stopped: Cutoff date reached")
                    break

                try:
                    next_btn = page.get_by_text("下一页")
                    next_class = next_btn.get_attribute("class")

                    if next_class and "disabled" in next_class:
                        print(f"\n✅ Stopped: No more pages")
                        break

                    print(f"\n➡️  Navigating to page {page_num + 1}...")
                    next_btn.click()
                    page.wait_for_timeout(2000)
                    page_num += 1

                except Exception as e:
                    logger.exception(f"Pagination error: {e}")
                    break

        except Exception as e:
            _log_critical_error_and_email(
                f"Scraping error: {e}",
                {"step": "scrape_listing", "base_url": BASE_URL},
            )
        finally:
            browser.close()

    print(
        f"\n📊 Total records extracted from listing pages: {len(all_extracted_records)}")

    # ------------------------------------------------------------------
    # PHASE 2: Filter out already-processed records via samr_cases
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"🚀 PHASE 2: FILTER ALREADY-PROCESSED RECORDS")
    print(f"{'='*60}\n")

    print(f"🔍 Checking which records are already in samr_cases...")
    new_records = []
    skipped_count = 0
    for record in all_extracted_records:
        detail_url = record.get("url")
        if detail_url and record_exists_in_samr_cases(detail_url):
            skipped_count += 1
        else:
            new_records.append(record)

    if skipped_count > 0:
        print(f"⏭️ Skipped {skipped_count} already-processed records")
    print(f"🔍 {len(new_records)} new records to process\n")

    # ------------------------------------------------------------------
    # PHASE 3: Match each new record → save to deals & samr_cases
    # ------------------------------------------------------------------
    print(f"{'='*60}")
    print(f"🚀 PHASE 3: MATCH RECORDS WITH DEALS")
    print(f"{'='*60}\n")

    for idx, record in enumerate(new_records, 1):
        title_en = record.get("title_en", "")
        title_cn = record.get("title_cn", "")
        date_str = record.get("date", "")
        url = record.get("url", "")

        print(f"\n[{idx}/{len(new_records)}] {date_str} - {title_en[:70]}...")

        # Prepare the samr_cases document (will be saved at end regardless)
        samr_case_doc = {
            "url": url,
            "title_cn": title_cn,
            "title_en": title_en,
            "date": date_str,
            "deal_id": None,
        }

        if title_en == "[Translation failed]":
            print("  ⏩ Skipped (translation failed)")
            save_to_samr_cases(samr_case_doc)
            continue

        match_result = match_deal_with_llm(title_en, title_cn)

        if match_result and match_result.lower() != "none" and match_result.lower().startswith("match"):
            try:
                deal_id = match_result.replace(
                    "Match:", "").replace("match:", "").strip()

                deal_match = None
                for deal in deals:
                    if deal.get("deal_id") == deal_id:
                        deal_match = deal
                        print(f"  ✅ Found deal by ID: {deal_id}")
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
                    print(f"  ✅ Match added to results!")

                    samr_case_doc["deal_id"] = deal_match.get(
                        "deal_id", "")

                    try:
                        samr_data = {
                            k: v for k, v in matched_result.items() if k != "matched_deal"}
                        send_samr_email_via_webhook(samr_data, deal_match)
                    except Exception as e:
                        logger.exception(f"  Error sending email notification: {e}")
                else:
                    print(
                        f"  ⚠️ LLM found match but deal not found in loaded deals: {deal_id}")

            except Exception as e:
                logger.exception(f"  Error processing match: {e}")
        else:
            print(f"  ➖ No match")
            try:
                company_details = title_en if title_en and title_en != "[Translation failed]" else title_cn
                is_usa_related = verify_usa_relation(
                    company_details=company_details,
                    case_type="CHINA",
                )
                if is_usa_related:
                    print(
                        f"   🇺🇸 USA-related case detected - sending email notification")
                    send_unmatched_samr_email_via_webhook(record)
                else:
                    print(f"   ℹ️ Not USA-related - no action taken")
            except Exception as e:
                logger.exception(f"  Error verifying USA relation: {e}")

        # Always persist the record to samr_cases
        save_to_samr_cases(samr_case_doc)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    matched_data_serializable = convert_datetime_to_string(matched_data)

    matched_output = {
        "success": True,
        "extraction_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_extracted": len(all_extracted_records),
        "total_new": len(new_records),
        "total_matched": len(matched_data),
        "matched_results": matched_data_serializable,
    }

    print(f"\n{'='*60}")
    print(f"✅ ALL DONE!")
    print(f"{'='*60}")
    print(f"📊 Total records extracted: {len(all_extracted_records)}")
    print(f"🆕 New records processed: {len(new_records)}")
    print(f"🎯 Total matches found: {len(matched_data)}")
    print(f"{'='*60}\n")
    elapsed = round((datetime.datetime.now() - run_start).total_seconds(), 1)
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Total records extracted      : {len(all_extracted_records)}")
    logger.info(f"  New records processed        : {len(new_records)}")
    logger.info(f"  Total matches found          : {len(matched_data)}")
    logger.info(f"  Total time                   : {elapsed}s")
    logger.info("=" * 60)

    return matched_output


if __name__ == "__main__":
    import sys

    headless_mode = True

    if len(sys.argv) > 1:
        if sys.argv[1] == "--headed":
            headless_mode = False
            print("🖥️  Mode: Running with visible browser")
        elif sys.argv[1] == "--help":
            print("\nUsage: python new_samr_public_notice_db.py [OPTIONS]")
            print("\nOptions:")
            print("  --headed          Run browser in headed mode (visible)")
            print("  --help            Show this help message")
            print("\nDefault: Scrape new pages from SAMR website in headless mode\n")
            sys.exit(0)

    logger.info("Mode: Scrape new pages from SAMR website")
    try:
        main(headless=headless_mode)
    except Exception as e:
        _log_critical_error_and_email(f"Unhandled error in main: {e}", {"step": "main"})
        raise
