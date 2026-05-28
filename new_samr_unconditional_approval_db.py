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
from typing import Any

# Configuration
CUTOFF_DATE = (datetime.datetime.now() - datetime.timedelta(days=6)).replace(
    hour=0, minute=0, second=0, microsecond=0)
BASE__SCRAPER_URL = "https://www.samr.gov.cn/fldes/ajgs/wtjjz/"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "samr_unconditional_html_pages"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "samr-cases-unconditional"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

BASE_URL = os.getenv("BASE_URL")
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_INTERNAL_WITH_JOSH",
    f"{BASE_URL}/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)


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
        ("domcontentloaded", 12000),
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

def get_samr_unconditional_collection():
    db = get_database()
    if db is None:
        return None
    return db["samr_unconditional"]


def get_samr_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["samr_cases"]


def record_exists_in_samr_unconditional(url):
    """Return True if this listing-page URL was already processed."""
    try:
        col = get_samr_unconditional_collection()
        if col is None:
            return False
        return col.find_one({"url": url}) is not None
    except Exception as e:
        logger.warning(f"Error checking samr_unconditional: {e}")
        return False


def save_to_samr_unconditional(record):
    """
    Save a processed listing-page record to samr_unconditional.

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
        col = get_samr_unconditional_collection()
        if col is None:
            logger.warning("samr_unconditional collection not available")
            return False

        record["processed_at"] = datetime.datetime.now().isoformat()
        col.update_one({"url": record["url"]}, {"$set": record}, upsert=True)
        logger.info(f"Saved to samr_unconditional: {record['url'][:80]}...")
        return True
    except Exception as e:
        logger.warning(f"Error saving to samr_unconditional: {e}")
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


def update_samr_case_unconditional(samr_case, unconditional_data, deal_id=None):
    """
    Add 'unconditional' node to a samr_cases record and set is_open=false.
    Optionally update deal_id if provided.
    """
    try:
        col = get_samr_cases_collection()
        if col is None:
            logger.warning("samr_cases collection not available")
            return False

        update_fields = {
            "unconditional": unconditional_data,
            "is_open": False,
        }
        if deal_id:
            update_fields["deal_id"] = deal_id

        col.update_one(
            {"_id": samr_case["_id"]},
            {"$set": update_fields},
        )
        title = samr_case.get("title_en", samr_case.get("title_cn", ""))[:60]
        logger.info(f"Updated samr_case with unconditional node: {title}...")
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
    deals = get_deals_from_mongodb()
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
    Extract listing records from listing-page HTML.
    Returns list of dicts: title_cn, title_en, url, date
    """
    records = []
    soup = BeautifulSoup(html_content, "html.parser")
    items = soup.select("div.page-content ul li")

    for item in items:
        try:
            divs = item.find_all("div")
            if not divs:
                continue

            date_text = divs[0].get_text(strip=True)
            if not date_text:
                continue

            link = item.find("a")
            if not link:
                continue

            title_cn_raw = link.get_text(strip=True)
            title_cn = re.sub(r'\s+', ' ', title_cn_raw).strip()

            href = link.get("href", "")
            if href and not href.startswith("http"):
                base_domain = "https://www.samr.gov.cn"
                href = requests.compat.urljoin(base_domain, href)

            title_en = translate_with_openai(title_cn)

            record = {
                "title_cn": title_cn,
                "title_en": title_en,
                "url": href,
                "date": date_text,
            }
            records.append(record)
            logger.info(f"Extracted: {date_text} - {title_en}")

        except Exception as e:
            logger.warning(f"Error extracting record: {e}")
            continue

    return records


def extract_page_records(page, page_num=1):
    """Extract records from the current listing page. Returns (records_list, should_stop)."""
    logger.info(f"{'='*60}")
    logger.info(f"PAGE {page_num}: Extracting records...")
    logger.info(f"{'='*60}")

    page.wait_for_selector("div.page-content ul li", timeout=100000)

    html_content = page.content()
    logger.info(f"HTML content: {html_content}")

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
# Detail-page table extraction
# ---------------------------------------------------------------------------

def extract_table_rows_from_detail(context, url, error_items: list[dict[str, Any]] | None = None):
    """
    Open detail page, parse the HTML table, return structured rows.

    Each row dict:
    {
        "serial": str,
        "case_name_cn": str,
        "case_name_en": str,
        "operators_cn": str,
        "operators_en": str,
        "approval_date": str,   # YYYY-MM-DD or raw text
    }
    """
    rows_out = []
    new_page = context.new_page()
    try:
        new_page.goto(url, wait_until="domcontentloaded")
        new_page.wait_for_timeout(5000)

        html = new_page.content()
        soup = BeautifulSoup(html, "html.parser")

        table = soup.find("table")
        if not table:
            logger.warning("  No table found on detail page")
            return rows_out

        trs = table.find_all("tr")
        for tr in trs[1:]:  # skip header row
            cols = [col.get_text(strip=True)
                    for col in tr.find_all(["td", "th"])]
            if len(cols) < 4:
                continue

            serial = cols[0]
            case_name_cn = cols[1]
            operators_cn = cols[2]
            approval_date_raw = cols[3]

            # Parse Chinese date format → YYYY-MM-DD
            approval_date = approval_date_raw
            match = re.match(
                r"(\d{4})年(\d{1,2})月(\d{1,2})日", approval_date_raw)
            if match:
                approval_date = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

            case_name_en = translate_with_openai(case_name_cn)
            time.sleep(0.2)
            operators_en = translate_with_openai(operators_cn)
            time.sleep(0.2)

            rows_out.append({
                "serial": serial,
                "case_name_cn": case_name_cn,
                "case_name_en": case_name_en,
                "operators_cn": operators_cn,
                "operators_en": operators_en,
                "approval_date": approval_date,
            })
            logger.info(f"    Row {serial}: {case_name_en[:70]}")

    except Exception as e:
        logger.exception(f"  Error extracting table from {url}: {e}")
        if error_items is not None:
            collect_error(
                error_items,
                str(e),
                step="extract_table_rows_from_detail",
                context={"url": url},
            )
    finally:
        new_page.close()

    return rows_out


# ---------------------------------------------------------------------------
# LLM: match a table row against samr_cases titles
# ---------------------------------------------------------------------------

def match_table_row_to_samr_cases(table_row, samr_cases_list):
    """
    Ask LLM whether this unconditional-approval table row matches any samr_cases
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
You are an M&A analyst. Below is ONE row from a SAMR unconditional-approval table and a list of SAMR public-notice case titles.

UNCONDITIONAL APPROVAL ROW:
- Case Name (CN): {table_row['case_name_cn']}
- Case Name (EN): {table_row['case_name_en']}
- Operators (CN): {table_row['operators_cn']}
- Operators (EN): {table_row['operators_en']}
- Approval Date: {table_row['approval_date']}

SAMR PUBLIC NOTICE CASES:
{cases_text}

TASK:
Determine if the unconditional-approval row refers to the SAME deal/transaction as any of the public-notice cases listed above.

RULES:
1. Match only if the case row and a public-notice title clearly refer to the same transaction (same acquirer AND same target).
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
            ],
        )
        result = response.choices[0].message.content.strip()
        logger.info(f"    samr_cases match LLM: {result}")

        if result.lower().startswith("match"):
            matched_id = result.replace(
                "Match:", "").replace("match:", "").strip()
            for sc in samr_cases_list:
                sc_id = sc.get("_id_str", str(sc.get("_id", "")))
                if sc_id == matched_id:
                    return sc
        return None
    except Exception as e:
        logger.warning(f"    LLM Error (samr_cases match): {e}")
        raise


# ---------------------------------------------------------------------------
# LLM: match a samr_case title against deals
# ---------------------------------------------------------------------------

def match_samr_case_to_deals(samr_case):
    """
    Ask LLM whether a samr_cases record title matches any deal.
    Returns (deal_match_dict, match_result_str) or (None, "None").
    """
    global deals
    if not deals:
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
        t_aliases = deal.get("target_aliases") or []
        p_aliases = deal.get("parent_aliases") or []
        if isinstance(t_aliases, list) and t_aliases:
            deal_info["target_aliases"] = t_aliases
        if isinstance(p_aliases, list) and p_aliases:
            deal_info["parent_aliases"] = p_aliases
        if target or acquirer:
            deals_list.append(deal_info)

    if not deals_list:
        return None, "None"

    lines = []
    for d in deals_list:
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target', 'N/A')} | Acquirer: {d.get('acquirer', 'N/A')}"
        ta = d.get("target_aliases", []) or []
        pa = d.get("parent_aliases", []) or []
        if ta:
            line += f" | Target aliases: {', '.join(str(a) for a in ta)}"
        if pa:
            line += f" | Parent aliases: {', '.join(str(a) for a in pa)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    title_en = samr_case.get("title_en", "")
    title_cn = samr_case.get("title_cn", "")

    prompt = f"""
You are an M&A deal analyst. Given the title of a SAMR China public notice, determine whether it matches any deal below.

DEALS TO MATCH:
{deals_text}

TITLE (English): {title_en}
TITLE (Chinese): {title_cn}

INSTRUCTIONS:
1. A deal matches only if BOTH the Acquirer (or alias) AND Target (or alias) are directly mentioned in the title.
2. Do not match on single-company overlap, sector similarity, or indirect association.
3. Allow normal name variations (Inc./Incorporated, Ltd/Limited, casing).

RESPONSE:
- Match: DEAL_ID
- None
"""

    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": "You are an expert in M&A deal recognition. Return the match or None."},
                {"role": "user", "content": prompt},
            ],
        )
        result = response.choices[0].message.content.strip()
        logger.info(f"    Deal match LLM: {result}")

        if result.lower() != "none" and result.lower().startswith("match"):
            deal_id = result.replace(
                "Match:", "").replace("match:", "").strip()

            for deal in deals:
                if deal.get("deal_id") == deal_id:
                    return deal, result

            logger.warning(
                f"    LLM returned deal_id '{deal_id}' but not found in loaded deals")

        return None, "None"
    except Exception as e:
        logger.warning(f"    LLM Error (deal match): {e}")
        raise


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

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
# Email – matched deal (unconditional closed)
# ---------------------------------------------------------------------------

def generate_samr_unconditional_email_html(samr_case, deal_match, unconditional_data):
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    case_title_en = samr_case.get("title_en", "N/A")
    case_title_cn = samr_case.get("title_cn", "N/A")
    case_date = samr_case.get("date", "N/A")
    case_url = samr_case.get("url", "")
    approval_date = unconditional_data.get("approval_date", "N/A")
    approval_link = unconditional_data.get("approval_link", "")

    title_text = (
        f"SAMR China Unconditional Approval – {target} / {acquirer}"
        if target != "N/A" and acquirer != "N/A"
        else f"SAMR China Unconditional Approval – {case_title_en[:50]}"
    )
    subject = build_subject("samr_unconditional", "new", deal_match)

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #27ae60;">
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
        <td style="padding:8px; font-weight:bold; color:#555;">Approval Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(approval_date))}</td>
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

    if approval_link:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Approval Page:</td>
        <td style="padding:8px;">
          <a href="{escape_html(approval_link)}" style="color:#27ae60; text-decoration:none;" target="_blank">View Approval Detail</a>
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
      <p>This is an automated email generated from SAMR China unconditional approval matches.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_samr_unconditional_email_via_webhook(samr_case, deal_match, unconditional_data):
    try:
        subject, html_email = generate_samr_unconditional_email_html(
            samr_case, deal_match, unconditional_data)
        logger.info(f"Generated email subject: {subject}")

        webhook_url = N8N_WEBHOOK_URL

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
            'date': samr_case.get("date", "N/A"),
            'approval_date': unconditional_data.get("approval_date", "N/A"),
            'url': unconditional_data.get("approval_link", ""),
        }

        response = requests.post(
            webhook_url, json=payload,
            headers={'Content-Type': 'application/json'}, timeout=60,
        )
        response.raise_for_status()
        logger.info(f"Email sent successfully! Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error sending email via webhook: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error generating/sending email: {e}")
        return False


# ---------------------------------------------------------------------------
# Email – unmatched USA-related
# ---------------------------------------------------------------------------

def generate_unmatched_unconditional_email_html(samr_case, unconditional_data, usa_companies):
    title_cn = samr_case.get("title_cn", "N/A")
    title_en = samr_case.get("title_en", "N/A")
    date_str = samr_case.get("date", "N/A")
    case_url = samr_case.get("url", "")
    approval_date = unconditional_data.get("approval_date", "N/A")
    approval_link = unconditional_data.get("approval_link", "")
    companies_str = ", ".join(usa_companies) if isinstance(
        usa_companies, list) else str(usa_companies)

    subject = build_subject("samr_unconditional", "new")

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
      SAMR China Unconditional Approval (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; font-weight:bold;">
        🇺🇸 USA-RELATED: {escape_html(companies_str)}
      </div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Approval Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(approval_date))}</td>
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

    if approval_link:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Approval Page:</td>
        <td style="padding:8px;">
          <a href="{escape_html(approval_link)}" style="color:#27ae60; text-decoration:none;" target="_blank">View Approval Detail</a>
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
      <p>This is an automated email generated from SAMR China unconditional approval monitoring.</p>
    </div>
  </div>
</body>
</html>
"""
    return subject, html_email


def send_unmatched_unconditional_email_via_webhook(samr_case, unconditional_data, usa_companies):
    try:
        subject, html_email = generate_unmatched_unconditional_email_html(
            samr_case, unconditional_data, usa_companies)
        logger.info(f"Generated email subject: {subject}")

        webhook_url = N8N_WEBHOOK_URL
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': 'N/A',
            'target': 'N/A',
            'acquirer': 'N/A',
            'title_cn': samr_case.get("title_cn", "N/A"),
            'title_en': samr_case.get("title_en", "N/A"),
            'date': samr_case.get("date", "N/A"),
            'url': unconditional_data.get("approval_link", ""),
            'is_unmatched': True,
            'usa_related': True,
        }

        response = requests.post(
            webhook_url, json=payload,
            headers={'Content-Type': 'application/json'}, timeout=60,
        )
        response.raise_for_status()
        logger.info(f"Email sent successfully! Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error sending email via webhook: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error generating/sending email: {e}")
        return False


# ---------------------------------------------------------------------------
# Save unconditional data to deals collection
# ---------------------------------------------------------------------------

def save_samr_unconditional_data_to_deal(deal_match, unconditional_data):
    """Save unconditional approval data to the deal under 'samr_unconditional' node."""
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

        data = convert_datetime_to_string(unconditional_data)

        result = collection.update_one(
            query, {"$set": {"samr_unconditional": data}})
        logger.info(
            f"Update deals: matched={result.matched_count}, modified={result.modified_count}")

        if result.modified_count > 0 or result.matched_count > 0:
            logger.info("Saved samr_unconditional to deal record")
            return True
        else:
            logger.warning("Deal not found in MongoDB")
            return False

    except Exception as e:
        logger.exception(f"Error saving unconditional to deal: {e}")
        return False


# ---------------------------------------------------------------------------
# Process a single table row against samr_cases
# ---------------------------------------------------------------------------

def process_table_row(table_row, samr_cases_list, listing_record, error_items: list[dict[str, Any]] | None = None):
    """
    For one table row:
    1. Match against samr_cases titles via LLM
    2. If matched → set is_open=false, add unconditional node
       a. If samr_case has deal_id → email "unconditionally closed"
       b. If no deal_id → LLM match against deals → email if matched
       c. If no deal match → check USA-related → email if yes
    3. If not matched → skip (no samr_cases record to link to)
    """
    row_label = f"Row {table_row['serial']}: {table_row['case_name_en'][:50]}"
    logger.info(f"  Processing {row_label}")

    unconditional_data = {
        "case_name_cn": table_row["case_name_cn"],
        "case_name_en": table_row["case_name_en"],
        "operators_cn": table_row["operators_cn"],
        "operators_en": table_row["operators_en"],
        "approval_date": table_row["approval_date"],
        "approval_link": listing_record["url"],
    }

    try:
        matched_case = match_table_row_to_samr_cases(table_row, samr_cases_list)
    except Exception as e:
        logger.exception(f"  samr_cases match failed for {row_label}: {e}")
        if error_items is not None:
            collect_error(
                error_items,
                str(e),
                step="match_table_row_to_samr_cases",
                context={
                    "case_name_en": table_row.get("case_name_en", "")[:80],
                    "url": listing_record.get("url", ""),
                },
            )
        return

    if not matched_case:
        logger.info(f"  No samr_cases match for {row_label}")
        return

    case_title = matched_case.get(
        "title_en", matched_case.get("title_cn", ""))
    logger.info(f"  Matched samr_case: {case_title}")

    # Step 2: Update samr_case: add unconditional node + is_open=false
    existing_deal_id = matched_case.get("deal_id")

    if existing_deal_id:
        # Case A: samr_case already has a deal_id
        logger.info(f"  samr_case has deal_id: {existing_deal_id}")
        update_samr_case_unconditional(matched_case, unconditional_data)

        # Find the deal to get full info for email
        deal_match = None
        for deal in deals:
            if deal.get("deal_id") == existing_deal_id:
                deal_match = deal
                break

        if deal_match:
            save_samr_unconditional_data_to_deal(
                deal_match, unconditional_data)
            send_samr_unconditional_email_via_webhook(
                matched_case, deal_match, unconditional_data)
            matched_data.append({
                "deal_id": existing_deal_id,
                "samr_case_title": case_title,
                "unconditional": unconditional_data,
            })
        else:
            logger.warning(
                f"  deal_id {existing_deal_id} not found in loaded deals (may be closed)")
    else:
        # Case B: no deal_id → try LLM deal matching
        logger.info("  No deal_id on samr_case, trying LLM deal match...")
        try:
            deal_match, match_result = match_samr_case_to_deals(matched_case)
        except Exception as e:
            logger.exception(f"  Deal match failed for {row_label}: {e}")
            if error_items is not None:
                collect_error(
                    error_items,
                    str(e),
                    step="match_samr_case_to_deals",
                    context={
                        "title": case_title[:80],
                        "url": listing_record.get("url", ""),
                    },
                )
            deal_match, match_result = None, "None"

        if deal_match:
            deal_id = deal_match.get("deal_id", "")
            logger.info(f"  Deal matched: {deal_id}")
            update_samr_case_unconditional(
                matched_case, unconditional_data, deal_id=deal_id)
            save_samr_unconditional_data_to_deal(
                deal_match, unconditional_data)
            send_samr_unconditional_email_via_webhook(
                matched_case, deal_match, unconditional_data)
            matched_data.append({
                "deal_id": deal_id,
                "samr_case_title": case_title,
                "unconditional": unconditional_data,
            })
        else:
            # Case C: no deal match → check USA-related
            logger.info("  No deal match. Checking USA relation...")
            update_samr_case_unconditional(matched_case, unconditional_data)

            try:
                company_details = f"""
Title (EN): {matched_case.get('title_en', '')}
Title (CN): {matched_case.get('title_cn', '')}
Operators (EN): {table_row['operators_en']}
Operators (CN): {table_row['operators_cn']}
""".strip()

                usa_companies = verify_usa_relation(
                    company_details=company_details,
                    case_type="CHINA-UNCONDITIONAL",
                )

                if isinstance(usa_companies, bool):
                    usa_companies = []
                elif not isinstance(usa_companies, list):
                    usa_companies = []

                if usa_companies:
                    logger.info(f"  USA-related: {usa_companies}")
                    send_unmatched_unconditional_email_via_webhook(
                        matched_case, unconditional_data, usa_companies)
                else:
                    logger.info("  Not USA-related – no email")
            except Exception as e:
                logger.exception(f"  Error verifying USA relation: {e}")
                if error_items is not None:
                    collect_error(
                        error_items,
                        str(e),
                        step="verify_usa_relation",
                        context={
                            "title": case_title[:80],
                            "url": listing_record.get("url", ""),
                        },
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
    logger.info("=" * 60)
    logger.info("Starting SAMR Unconditional Cases Register")
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

        logger.info("PHASE 1: EXTRACT UNCONDITIONAL APPROVAL LISTING RECORDS")

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
                        page.wait_for_timeout(5000)
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
            f"Total listing records extracted: {len(all_extracted_records)}")

        logger.info("PHASE 2: FILTER ALREADY-PROCESSED LISTINGS")

        skipped = 0
        for rec in all_extracted_records:
            if rec.get("url") and record_exists_in_samr_unconditional(rec["url"]):
                skipped += 1
            else:
                new_records.append(rec)

        if skipped:
            logger.info(f"Skipped {skipped} already-processed listings")
        logger.info(f"{len(new_records)} new listings to process")

        logger.info("PHASE 3: PROCESS DETAIL PAGES & TABLE ROWS")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()

            logger.info(f"Context: {context}")

            for idx, listing_record in enumerate(new_records, 1):
                try:
                    title_en = listing_record.get("title_en", "")
                    date_str = listing_record.get("date", "")
                    detail_url = listing_record.get("url", "")

                    logger.info(
                        f"[{idx}/{len(new_records)}] {date_str} - {title_en[:70]}")

                    if not detail_url:
                        logger.info("  Skipped (no URL)")
                        continue

                    table_rows = extract_table_rows_from_detail(
                        context, detail_url, error_items=error_items)
                    logger.info(f"  Extracted {len(table_rows)} table rows")
                    logger.info(f"Table rows: {table_rows}")

                    for table_row in table_rows:
                        process_table_row(
                            table_row, samr_cases_list, listing_record, error_items=error_items)

                    save_to_samr_unconditional({
                        "url": detail_url,
                        "title_cn": listing_record.get("title_cn", ""),
                        "title_en": title_en,
                        "date": date_str,
                    })
                except Exception as e:
                    logger.exception(f"Error processing listing #{idx}: {e}")
                    collect_error(
                        error_items,
                        str(e),
                        step="process_listing",
                        context={
                            "url": listing_record.get("url", ""),
                            "title": (listing_record.get("title_en") or "")[:80],
                        },
                    )

            browser.close()

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
        logger.info(
            f"Total listing records extracted: {len(all_extracted_records)}")
        logger.info(f"New listings processed: {len(new_records)}")
        logger.info(f"Total matches found: {len(matched_data)}")
        elapsed = round((datetime.datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(
            f"  Total listings extracted     : {len(all_extracted_records)}")
        logger.info(f"  New listings processed       : {len(new_records)}")
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
                "Usage: python new_samr_unconditional_approval_db.py [OPTIONS]")
            logger.info(
                "Options: --headed (visible browser), --help (this message)")
            logger.info(
                "Default: Scrape new pages from SAMR website in headless mode")
            sys.exit(0)

    logger.info("Mode: Scrape SAMR unconditional approval pages")
    main(headless=headless_mode)
