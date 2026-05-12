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
from error_email_service import send_error_email
from log_utils import cleanup_old_logs, refresh_log_file

# Configuration
CUTOFF_DATE = (datetime.datetime.now() - datetime.timedelta(days=15)).replace(
    hour=0, minute=0, second=0, microsecond=0)
# CUTOFF_DATE = datetime.datetime.now().replace(
#     hour=0, minute=0, second=0, microsecond=0)
BASE_URL = "https://www.samr.gov.cn/fldes/tzgg/ftj/"
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


def _log_critical_error_and_email(msg: str, context: dict | None = None):
    """Immediate error email — use ONLY for critical startup / fatal failures."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context or {},
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


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
    Extract listing records from HTML.
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

            title_en = translate_with_openai(title_cn)

            record = {
                "title_cn": title_cn,
                "title_en": title_en,
                "url": href,
                "date": date_str,
            }
            records.append(record)
            logger.info(f"Extracted: {date_str} - {title_en}")

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
        return None


# ---------------------------------------------------------------------------
# LLM: match a samr_case title against deals
# ---------------------------------------------------------------------------

def normalize_company(name):
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").strip()


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
    logger.info(f"samr_case title: {title_en} {title_cn}")

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
- Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
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
        logger.info(f"  Deal match LLM: {result}")

        if result.lower() != "none" and result.lower().startswith("match"):
            parts_str = result.replace(
                "Match:", "").replace("match:", "").strip()
            parts = parts_str.split("|")
            if len(parts) >= 3:
                deal_id = parts[0].strip()
                company_name = parts[1].strip()
                match_type = parts[2].strip().lower().replace(
                    "(", "").replace(")", "")

                for deal in deals:
                    if deal.get("deal_id") == deal_id:
                        return deal, result

                for deal in deals:
                    target = deal.get("target") or deal.get("target_name", "")
                    acquirer = deal.get("acquirer") or deal.get(
                        "acquire_name", "")
                    if match_type == "target" and target and normalize_company(target) == normalize_company(company_name):
                        return deal, result
                    elif match_type == "acquirer" and acquirer and normalize_company(acquirer) == normalize_company(company_name):
                        return deal, result

        return None, "None"
    except Exception as e:
        logger.warning(f"  LLM Error (deal match): {e}")
        return None, "None"


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
    subject = f"[FRMD] SAMR China Conditional Approval (New) – {target} / {acquirer}"

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

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
        )

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

def generate_unmatched_conditional_email_html(samr_case, conditional_data):
    title_cn = samr_case.get(
        "title_cn", conditional_data.get("title_cn", "N/A"))
    title_en = samr_case.get(
        "title_en", conditional_data.get("title_en", "N/A"))
    date_str = samr_case.get("date", "N/A")
    case_url = samr_case.get("url", "")
    cond_url = conditional_data.get("url", "")
    cond_date = conditional_data.get("date", "N/A")

    subject = f"[FRUD] SAMR China Conditional Approval (USA-Related) – {title_en[:60]}"

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

        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
        )

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

def process_record(record, samr_cases_list):
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

    matched_case = match_record_to_samr_cases(record, samr_cases_list)

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
        deal_match, match_result = match_samr_case_to_deals(matched_case)

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(headless=True):
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    global all_extracted_records, matched_data, deals
    run_start = datetime.datetime.now()

    all_extracted_records = []
    matched_data = []
    logger.info("=" * 60)
    logger.info(" Starting SAMR Conditional Cases Register")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    # Initialize MongoDB
    ok, msg = init_mongodb_connection(ENV_PATH)
    if ok:
        logger.info(msg)
    else:
        _log_critical_error_and_email(f"MongoDB initialization failed: {msg}", {
                                      "step": "init_mongodb_connection"})
        return {"success": False, "error": msg}

    # Load deals and samr_cases
    logger.info("Loading deals from MongoDB...")
    load_deals()

    logger.info("Loading samr_cases from MongoDB...")
    samr_cases_list = get_all_samr_cases()

    # ------------------------------------------------------------------
    # PHASE 1: Scrape listing pages
    # ------------------------------------------------------------------
    logger.info("PHASE 1: EXTRACT CONDITIONAL APPROVAL RECORDS")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        logger.info(f"Page: {page}")

        try:
            logger.info(f"Calling BASE_URL: {BASE_URL}")
            _goto_with_retry(page, BASE_URL)
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
                    break
        except Exception as e:
            _log_critical_error_and_email(
                f"Scraping error: {e}",
                {"step": "scrape_listing", "base_url": BASE_URL},
            )
        finally:
            browser.close()

    logger.info(f"Total records extracted: {len(all_extracted_records)}")

    # ------------------------------------------------------------------
    # PHASE 2: Filter already-processed records via samr_conditional
    # ------------------------------------------------------------------
    logger.info("PHASE 2: FILTER ALREADY-PROCESSED RECORDS")

    new_records = []
    skipped = 0
    for rec in all_extracted_records:
        if rec.get("url") and record_exists_in_samr_conditional(rec["url"]):
            skipped += 1
        else:
            new_records.append(rec)

    if skipped:
        logger.info(f"Skipped {skipped} already-processed records")
    logger.info(f"{len(new_records)} new records to process")

    # ------------------------------------------------------------------
    # PHASE 3: Process each new record
    # ------------------------------------------------------------------
    logger.info("PHASE 3: MATCH RECORDS AGAINST samr_cases & DEALS")

    for idx, record in enumerate(new_records, 1):
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

        process_record(record, samr_cases_list)

        # Always save to samr_conditional to track processed records
        save_to_samr_conditional({
            "url": record.get("url", ""),
            "title_cn": record.get("title_cn", ""),
            "title_en": title_en,
            "date": date_str,
        })

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    logger.info("ALL DONE!")
    logger.info(f"Total records extracted: {len(all_extracted_records)}")
    logger.info(f"New records processed: {len(new_records)}")
    logger.info(f"Total matches found: {len(matched_data)}")
    elapsed = round((datetime.datetime.now() - run_start).total_seconds(), 1)
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(
        f"  Total records extracted      : {len(all_extracted_records)}")
    logger.info(f"  New records processed        : {len(new_records)}")
    logger.info(f"  Total matches found          : {len(matched_data)}")
    logger.info(f"  Total time                   : {elapsed}s")
    logger.info("=" * 60)

    return {
        "success": True,
        "extraction_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_extracted": len(all_extracted_records),
        "total_new": len(new_records),
        "total_matched": len(matched_data),
        "matched_results": convert_datetime_to_string(matched_data),
    }


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
    try:
        main(headless=headless_mode)
    except Exception as e:
        _log_critical_error_and_email(
            f"Unhandled error in main: {e}", {"step": "main"})
        raise
