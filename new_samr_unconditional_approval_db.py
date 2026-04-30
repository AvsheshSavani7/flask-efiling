from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import datetime
import time
import requests
import json
import os
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

# Configuration
CUTOFF_DATE = (datetime.datetime.now() - datetime.timedelta(days=6)).replace(
    hour=0, minute=0, second=0, microsecond=0)
BASE_URL = "https://www.samr.gov.cn/fldes/ajgs/wtjjz/"
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "samr_unconditional_html_pages"

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
        print(f"⚠️ Error checking samr_unconditional: {e}")
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
            print("⚠️ samr_unconditional collection not available")
            return False

        record["processed_at"] = datetime.datetime.now().isoformat()
        col.update_one({"url": record["url"]}, {"$set": record}, upsert=True)
        print(f"💾 Saved to samr_unconditional: {record['url'][:80]}...")
        return True
    except Exception as e:
        print(f"⚠️ Error saving to samr_unconditional: {e}")
        return False


def get_all_samr_cases():
    """Fetch samr_cases documents where is_open is true (or not set)."""
    try:
        col = get_samr_cases_collection()
        if col is None:
            print("⚠️ samr_cases collection not available")
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
        print(f"✅ Fetched {len(docs)} open samr_cases records")
        return docs
    except Exception as e:
        print(f"⚠️ Error fetching samr_cases: {e}")
        return []


def update_samr_case_unconditional(samr_case, unconditional_data, deal_id=None):
    """
    Add 'unconditional' node to a samr_cases record and set is_open=false.
    Optionally update deal_id if provided.
    """
    try:
        col = get_samr_cases_collection()
        if col is None:
            print("⚠️ samr_cases collection not available")
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
        print(f"✅ Updated samr_case with unconditional node: {title}...")
        return True
    except Exception as e:
        print(f"⚠️ Error updating samr_case: {e}")
        return False


# ---------------------------------------------------------------------------
# Deals – load from MongoDB
# ---------------------------------------------------------------------------

def get_deals_from_mongodb():
    """Fetch open/unknown deals from the deals collection."""
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
        params = {"client": "gtx", "sl": "zh-CN",
                  "tl": "en", "dt": "t", "q": text}
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

            title_en = translate_to_english(title_cn)

            record = {
                "title_cn": title_cn,
                "title_en": title_en,
                "url": href,
                "date": date_text,
            }
            records.append(record)
            print(f"📋 Extracted: {date_text} - {title_en}")

        except Exception as e:
            print(f"⚠️ Error extracting record: {e}")
            continue

    return records


def extract_page_records(page, page_num=1):
    """Extract records from the current listing page. Returns (records_list, should_stop)."""
    print(f"\n{'='*60}")
    print(f"📄 PAGE {page_num}: Extracting records...")
    print(f"{'='*60}")

    page.wait_for_selector("div.page-content ul li", timeout=100000)

    html_content = page.content()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(
        HTML_OUTPUT_DIR, f"listing_page_{page_num}_{timestamp}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"💾 Saved HTML: {os.path.basename(html_path)}")

    page_records = extract_records_from_html(html_content)
    print(f"📊 Found {len(page_records)} records on page")

    filtered_records = []
    should_stop = False

    for record in page_records:
        try:
            record_date = datetime.datetime.strptime(
                record["date"], "%Y-%m-%d")
            if record_date >= CUTOFF_DATE:
                filtered_records.append(record)
            else:
                print(
                    f"🛑 Found record older than cutoff: {record_date.date()} < {CUTOFF_DATE.date()}")
                should_stop = True
                break
        except Exception as e:
            print(f"⚠️ Error parsing date for record: {e}")
            filtered_records.append(record)

    print(f"✅ Kept {len(filtered_records)} records (filtered out {len(page_records) - len(filtered_records)} old records)")
    return filtered_records, should_stop


# ---------------------------------------------------------------------------
# Detail-page table extraction
# ---------------------------------------------------------------------------

def extract_table_rows_from_detail(context, url):
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
            print("  ⚠️ No table found on detail page")
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

            case_name_en = translate_to_english(case_name_cn)
            time.sleep(0.2)
            operators_en = translate_to_english(operators_cn)
            time.sleep(0.2)

            rows_out.append({
                "serial": serial,
                "case_name_cn": case_name_cn,
                "case_name_en": case_name_en,
                "operators_cn": operators_cn,
                "operators_en": operators_en,
                "approval_date": approval_date,
            })
            print(f"    📋 Row {serial}: {case_name_en[:70]}")

    except Exception as e:
        print(f"  ❌ Error extracting table from {url}: {e}")
        import traceback
        traceback.print_exc()
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
        print(f"    🧠 samr_cases match LLM: {result}")

        if result.lower().startswith("match"):
            matched_id = result.replace(
                "Match:", "").replace("match:", "").strip()
            for sc in samr_cases_list:
                sc_id = sc.get("_id_str", str(sc.get("_id", "")))
                if sc_id == matched_id:
                    return sc
        return None
    except Exception as e:
        print(f"    ⚠️ LLM Error (samr_cases match): {e}")
        return None


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
        print(f"    🧠 Deal match LLM: {result}")

        if result.lower() != "none" and result.lower().startswith("match"):
            deal_id = result.replace(
                "Match:", "").replace("match:", "").strip()

            for deal in deals:
                if deal.get("deal_id") == deal_id:
                    return deal, result

            print(
                f"    ⚠️ LLM returned deal_id '{deal_id}' but not found in loaded deals")

        return None, "None"
    except Exception as e:
        print(f"    ⚠️ LLM Error (deal match): {e}")
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
    subject = f"[FRMD] SAMR China Unconditional Approval (New) – {target} / {acquirer}"

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
        print(f"📝 Generated email subject: {subject}")

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
            'date': samr_case.get("date", "N/A"),
            'approval_date': unconditional_data.get("approval_date", "N/A"),
            'url': unconditional_data.get("approval_link", ""),
        }

        response = requests.post(
            webhook_url, json=payload,
            headers={'Content-Type': 'application/json'}, timeout=60,
        )
        response.raise_for_status()
        print(f"✅ Email sent successfully! Status: {response.status_code}")
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

def generate_unmatched_unconditional_email_html(samr_case, unconditional_data, usa_companies):
    title_cn = samr_case.get("title_cn", "N/A")
    title_en = samr_case.get("title_en", "N/A")
    date_str = samr_case.get("date", "N/A")
    case_url = samr_case.get("url", "")
    approval_date = unconditional_data.get("approval_date", "N/A")
    approval_link = unconditional_data.get("approval_link", "")
    companies_str = ", ".join(usa_companies) if isinstance(
        usa_companies, list) else str(usa_companies)

    subject = f"[FRUD] SAMR China Unconditional Approval (USA-Related) – {companies_str[:60]}"

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
        print(f"📝 Generated email subject: {subject}")

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
        print(f"✅ Email sent successfully! Status: {response.status_code}")
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
# Save unconditional data to deals collection
# ---------------------------------------------------------------------------

def save_samr_unconditional_data_to_deal(deal_match, unconditional_data):
    """Save unconditional approval data to the deal under 'samr_unconditional' node."""
    try:
        if not is_connected():
            print("⚠️ MongoDB connection not available")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available")
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
            print("⚠️ Cannot identify deal, skipping save")
            return False

        data = convert_datetime_to_string(unconditional_data)

        result = collection.update_one(
            query, {"$set": {"samr_unconditional": data}})
        print(
            f"📊 Update deals: matched={result.matched_count}, modified={result.modified_count}")

        if result.modified_count > 0 or result.matched_count > 0:
            print(f"✅ Saved samr_unconditional to deal record")
            return True
        else:
            print(f"⚠️ Deal not found in MongoDB")
            return False

    except Exception as e:
        print(f"❌ Error saving unconditional to deal: {e}")
        return False


# ---------------------------------------------------------------------------
# Process a single table row against samr_cases
# ---------------------------------------------------------------------------

def process_table_row(table_row, samr_cases_list, listing_record):
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
    print(f"\n  🔍 Processing {row_label}")

    unconditional_data = {
        "case_name_cn": table_row["case_name_cn"],
        "case_name_en": table_row["case_name_en"],
        "operators_cn": table_row["operators_cn"],
        "operators_en": table_row["operators_en"],
        "approval_date": table_row["approval_date"],
        "approval_link": listing_record["url"],
    }

    # Step 1: Match table row against samr_cases
    matched_case = match_table_row_to_samr_cases(table_row, samr_cases_list)

    if not matched_case:
        print(f"  ➖ No samr_cases match for {row_label}")
        return

    case_title = matched_case.get(
        "title_en", matched_case.get("title_cn", ""))
    print(f"  ✅ Matched samr_case: {case_title}")

    # Step 2: Update samr_case: add unconditional node + is_open=false
    existing_deal_id = matched_case.get("deal_id")

    if existing_deal_id:
        # Case A: samr_case already has a deal_id
        print(f"  📌 samr_case has deal_id: {existing_deal_id}")
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
            print(
                f"  ⚠️ deal_id {existing_deal_id} not found in loaded deals (may be closed)")
    else:
        # Case B: no deal_id → try LLM deal matching
        print(f"  🔎 No deal_id on samr_case, trying LLM deal match...")
        deal_match, match_result = match_samr_case_to_deals(matched_case)

        if deal_match:
            deal_id = deal_match.get("deal_id", "")
            print(f"  ✅ Deal matched: {deal_id}")
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
            print(f"  ➖ No deal match. Checking USA relation...")
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
                    print(f"  🇺🇸 USA-related: {usa_companies}")
                    send_unmatched_unconditional_email_via_webhook(
                        matched_case, unconditional_data, usa_companies)
                else:
                    print(f"  ℹ️ Not USA-related – no email")
            except Exception as e:
                print(f"  ⚠️ Error verifying USA relation: {e}")
                import traceback
                traceback.print_exc()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(headless=True):
    global all_extracted_records, matched_data, deals

    all_extracted_records = []
    matched_data = []

    # Initialize MongoDB
    ok, msg = init_mongodb_connection(ENV_PATH)
    if ok:
        print(f"✅ {msg}")
    else:
        print(f"⚠️ {msg}")

    # Load deals and samr_cases
    print("📊 Loading deals from MongoDB...")
    load_deals()

    print("📊 Loading samr_cases from MongoDB...")
    samr_cases_list = get_all_samr_cases()

    # ------------------------------------------------------------------
    # PHASE 1: Scrape listing pages
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"🚀 PHASE 1: EXTRACT UNCONDITIONAL APPROVAL LISTING RECORDS")
    print(f"{'='*60}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            print(f"📍 Calling BASE_URL: {BASE_URL}")
            page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
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
                    page.wait_for_timeout(5000)
                    page_num += 1
                except Exception as e:
                    print(f"\n⚠️ Pagination error: {e}")
                    break
        except Exception as e:
            print(f"\n❌ Scraping error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            browser.close()

    print(f"\n📊 Total listing records extracted: {len(all_extracted_records)}")

    # ------------------------------------------------------------------
    # PHASE 2: Filter already-processed listings via samr_unconditional
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"🚀 PHASE 2: FILTER ALREADY-PROCESSED LISTINGS")
    print(f"{'='*60}\n")

    new_records = []
    skipped = 0
    for rec in all_extracted_records:
        if rec.get("url") and record_exists_in_samr_unconditional(rec["url"]):
            skipped += 1
        else:
            new_records.append(rec)

    if skipped:
        print(f"⏭️ Skipped {skipped} already-processed listings")
    print(f"🔍 {len(new_records)} new listings to process\n")

    # ------------------------------------------------------------------
    # PHASE 3: For each new listing, open detail → parse table → process rows
    # ------------------------------------------------------------------
    print(f"{'='*60}")
    print(f"🚀 PHASE 3: PROCESS DETAIL PAGES & TABLE ROWS")
    print(f"{'='*60}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()

        for idx, listing_record in enumerate(new_records, 1):
            title_en = listing_record.get("title_en", "")
            date_str = listing_record.get("date", "")
            detail_url = listing_record.get("url", "")

            print(f"\n{'='*60}")
            print(f"[{idx}/{len(new_records)}] {date_str} - {title_en[:70]}")
            print(f"{'='*60}")

            if not detail_url:
                print("  ⏩ Skipped (no URL)")
                continue

            # Extract table rows from detail page
            table_rows = extract_table_rows_from_detail(context, detail_url)
            print(f"  📊 Extracted {len(table_rows)} table rows")

            # Process each row against samr_cases
            for table_row in table_rows:
                process_table_row(table_row, samr_cases_list, listing_record)

            # Mark this listing as processed
            save_to_samr_unconditional({
                "url": detail_url,
                "title_cn": listing_record.get("title_cn", ""),
                "title_en": title_en,
                "date": date_str,
            })

        browser.close()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"✅ ALL DONE!")
    print(f"{'='*60}")
    print(f"📊 Total listing records extracted: {len(all_extracted_records)}")
    print(f"🆕 New listings processed: {len(new_records)}")
    print(f"🎯 Total matches found: {len(matched_data)}")
    print(f"{'='*60}\n")

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
            print("🖥️  Mode: Running with visible browser")
        elif sys.argv[1] == "--help":
            print(
                "\nUsage: python new_samr_unconditional_approval_db.py [OPTIONS]")
            print("\nOptions:")
            print("  --headed          Run browser in headed mode (visible)")
            print("  --help            Show this help message")
            print("\nDefault: Scrape new pages from SAMR website in headless mode\n")
            sys.exit(0)

    print("🌐 Mode: Scrape SAMR unconditional approval pages")
    main(headless=headless_mode)
