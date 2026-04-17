from dotenv import load_dotenv
import datetime
import json
import os
import re
import requests
import traceback
import xml.etree.ElementTree as ET
from bson import ObjectId
from bs4 import BeautifulSoup
from html import escape as escape_html
from openai import OpenAI
from pymongo import MongoClient
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ATOM_FEED_URL = "https://www.gov.uk/cma-cases.atom?case_type%5B%5D=mergers&case_state%5B%5D=open"
PROMPT_LOG_PATH = "cma_gpt_prompts.log"
ENV_PATH = ".env"

# ---------------------------------------------------------------------------
# Environment & clients
# ---------------------------------------------------------------------------
load_dotenv(ENV_PATH)
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------------------------
# MongoDB globals
# ---------------------------------------------------------------------------
_mongo_client: Optional[MongoClient] = None
_db = None
_deals_collection = None
_uk_cma_cases_collection = None

# Deals list (loaded at runtime)
deals = []
all_companies = set()


# ===================================================================
# MongoDB helpers
# ===================================================================

def init_mongodb_connection() -> Tuple[bool, str]:
    global _mongo_client, _db, _deals_collection, _uk_cma_cases_collection
    try:
        mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
        if not mongodb_uri:
            return False, "MongoDB connection string not found in environment variables"

        _mongo_client = MongoClient(
            mongodb_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            retryWrites=True,
            retryReads=True,
        )
        _mongo_client.admin.command("ping")

        _db = _mongo_client.get_database()
        _deals_collection = _db["deals"]
        _uk_cma_cases_collection = _db["uk_cma_cases"]

        return True, "MongoDB connection established successfully"
    except Exception as e:
        error_msg = str(e)
        if "DNS" in error_msg or "timeout" in error_msg.lower() or "resolution" in error_msg.lower():
            return False, "MongoDB connection failed: DNS/Network timeout."
        return False, f"MongoDB connection failed: {error_msg[:200]}"


def is_connected() -> bool:
    if not _mongo_client:
        return False
    try:
        _mongo_client.admin.command("ping")
        return True
    except Exception:
        return False


# ===================================================================
# Deals helpers
# ===================================================================

def normalize_company(name):
    return (
        name.lower()
        .replace(",", "")
        .replace(" inc.", "")
        .replace(" ltd.", "")
        .replace(" plc", "")
        .replace(" limited", "")
        .replace(" corporation", "")
        .replace(" corp.", "")
        .strip()
    )


def get_deals_from_mongodb():
    try:
        if _deals_collection is None:
            print("⚠️ Deals collection not available.")
            return []

        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        all_deals = list(_deals_collection.find(status_filter))

        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        print(f"✅ Fetched {len(all_deals)} deals from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        traceback.print_exc()
        return []


def load_deals():
    global deals, all_companies
    deals = get_deals_from_mongodb()
    print(f"📊 Loaded {len(deals)} deals from MongoDB")

    all_companies = set()
    for d in deals:
        acquirer = d.get("acquirer") or d.get("acquire_name", "")
        target = d.get("target") or d.get("target_name", "")
        if acquirer:
            all_companies.add(normalize_company(acquirer))
        if target:
            all_companies.add(normalize_company(target))

    return deals


# ===================================================================
# uk_cma_cases collection helpers
# ===================================================================

def get_existing_open_case_urls() -> set:
    """Return a set of detail_url values from uk_cma_cases where case_state is Open."""
    if _uk_cma_cases_collection is None:
        print("⚠️ uk_cma_cases collection not available.")
        return set()
    try:
        cursor = _uk_cma_cases_collection.find(
            {"case_state": "Open"}, {"detail_url": 1, "_id": 0})
        urls = {doc["detail_url"] for doc in cursor if doc.get("detail_url")}
        print(
            f"📋 Found {len(urls)} existing open cases in uk_cma_cases collection")
        return urls
    except Exception as e:
        print(f"⚠️ Error fetching existing open cases: {e}")
        traceback.print_exc()
        return set()


def insert_uk_cma_case(record: dict) -> bool:
    """Insert a single record into the uk_cma_cases collection."""
    if _uk_cma_cases_collection is None:
        print("⚠️ uk_cma_cases collection not available, cannot insert.")
        return False
    try:
        _uk_cma_cases_collection.insert_one(record)
        print(
            f"  ✅ Inserted into uk_cma_cases: {record.get('title', 'N/A')[:60]}")
        return True
    except Exception as e:
        print(f"  ❌ Error inserting into uk_cma_cases: {e}")
        traceback.print_exc()
        return False


# ===================================================================
# Atom feed
# ===================================================================

def fetch_atom_feed():
    try:
        print(f"🌐 Fetching Atom feed from: {ATOM_FEED_URL}")
        response = requests.get(ATOM_FEED_URL, timeout=30)
        response.raise_for_status()
        print(
            f"✅ Successfully fetched Atom feed ({len(response.content)} bytes)")
        return response.text
    except Exception as e:
        print(f"❌ Error fetching Atom feed: {e}")
        traceback.print_exc()
        return None


def parse_atom_feed(xml_content):
    records = []
    try:
        root = ET.fromstring(xml_content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns)
        print(f"📋 Found {len(entries)} entries in Atom feed")

        for entry in entries:
            try:
                record = {}
                id_elem = entry.find("atom:id", ns)
                if id_elem is not None:
                    record["id"] = id_elem.text

                updated_elem = entry.find("atom:updated", ns)
                if updated_elem is not None:
                    record["updated"] = updated_elem.text

                title_elem = entry.find("atom:title", ns)
                if title_elem is not None:
                    record["title"] = title_elem.text

                link_elem = entry.find('atom:link[@rel="alternate"]', ns)
                if link_elem is not None:
                    record["url"] = link_elem.get("href", "")

                if record.get("title") and record.get("url"):
                    records.append(record)
                    print(f"  📄 {record.get('title', 'N/A')[:70]}")
            except Exception as e:
                print(f"⚠️ Error parsing entry: {e}")
                continue
    except Exception as e:
        print(f"❌ Error parsing Atom feed: {e}")
        traceback.print_exc()
    return records


# ===================================================================
# HTML scraping  (logic from uk_cma_html_parser.py + scrape_case_details)
# ===================================================================

def extract_title(soup):
    h1 = soup.find("h1", class_="gem-c-heading__text")
    if h1:
        return h1.get_text(strip=True)
    return None


def extract_description(soup):
    lead = soup.find("p", class_="gem-c-lead-paragraph")
    if lead:
        return lead.get_text(strip=True)
    return None


def extract_sidebar_metadata(soup):
    data = {}
    inverse_div = soup.find("div", class_="gem-c-metadata--inverse")
    if not inverse_div:
        return data

    dl = inverse_div.find("dl", class_="gem-c-metadata__list")
    if not dl:
        return data

    dts = dl.find_all("dt", class_="gem-c-metadata__term")
    for dt in dts:
        label = dt.get_text(strip=True).rstrip(":").lower()
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        link = dd.find("a")
        value = link.get_text(strip=True) if link else dd.get_text(strip=True)

        if "case type" in label:
            data["case_type"] = value
        elif "case state" in label:
            data["case_state"] = value
        elif "market sector" in label:
            data["market_sector"] = value
        elif "outcome" in label:
            data["outcome"] = value
        elif "opened" in label:
            data["opened_date"] = value
        elif "closed" in label:
            data["closed_date"] = value
    return data


def extract_history(soup):
    history = []
    history_ol = soup.find("ol", class_="gem-c-published-dates__list")
    if not history_ol:
        return history

    for li in history_ol.find_all("li", class_="gem-c-published-dates__change-item"):
        time_tag = li.find("time", class_="gem-c-published-dates__change-date")
        note_tag = li.find("p", class_="gem-c-published-dates__change-note")
        entry = {}
        if time_tag:
            entry["date"] = time_tag.get_text(strip=True)
            entry["datetime"] = time_tag.get("datetime", "")
        if note_tag:
            entry["note"] = note_tag.get_text(strip=True)
        if entry:
            history.append(entry)
    return history


def extract_published_dates(soup):
    result = {
        "published_date": None,
        "last_updated": None,
    }

    published_dates_div = soup.find("div", class_="gem-c-published-dates")
    if not published_dates_div:
        published_dates_div = soup.find(
            "div", id="full-publication-update-history")
    if not published_dates_div:
        return result

    for div in published_dates_div.find_all("div"):
        text = div.get_text(strip=True)
        if text.startswith("Published"):
            result["published_date"] = text.replace("Published", "").strip()
            break

    full_text = published_dates_div.get_text()
    if "Last updated" in full_text:
        m = re.search(r"Last updated\s+([^\n]+)", full_text)
        if m:
            result["last_updated"] = m.group(1).strip()

    return result


def scrape_detail_page(url):
    """Fetch the detail page HTML and extract all fields."""
    try:
        print(f"  🌐 Scraping: {url}")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        sidebar = extract_sidebar_metadata(soup)
        pub = extract_published_dates(soup)

        data = {
            "title": extract_title(soup),
            "description": extract_description(soup),
            "case_type": sidebar.get("case_type"),
            "case_state": sidebar.get("case_state"),
            "market_sector": sidebar.get("market_sector"),
            "outcome": sidebar.get("outcome"),
            "opened_date": sidebar.get("opened_date"),
            "closed_date": sidebar.get("closed_date"),
            "history": extract_history(soup),
            "published_date": pub.get("published_date"),
            "last_updated": pub.get("last_updated"),
        }
        print(
            f"    ✅ Scraped: {data.get('title', 'N/A')[:60]} | state={data.get('case_state')}")
        return data
    except Exception as e:
        print(f"    ❌ Error scraping detail page: {e}")
        traceback.print_exc()
        return None


# ===================================================================
# LLM: match title with deals
# ===================================================================

def match_title_with_deals(title):
    global deals
    if not deals:
        print("⚠️ Deals list is empty, reloading...")
        load_deals()

    deals_list = []
    for deal in deals:
        info = {"deal_id": deal.get("deal_id", "")}
        target = deal.get("target") or deal.get("target_name", "")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")
        if target:
            info["target"] = target
        if acquirer:
            info["acquirer"] = acquirer

        target_aliases = deal.get("target_aliases") or []
        parent_aliases = deal.get("parent_aliases") or []
        if isinstance(target_aliases, list) and target_aliases:
            info["target_aliases"] = target_aliases
        if isinstance(parent_aliases, list) and parent_aliases:
            info["parent_aliases"] = parent_aliases

        if target or acquirer:
            deals_list.append(info)

    if not deals_list:
        print("⚠️ No deals with company names found")
        return "None"

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

    prompt = f"""
You are a professional M&A analyst specializing in UK merger cases.

Below is a CMA merger case title. Your task is to match it with any of the deals listed below.

DEALS TO MATCH:
{deals_text}

CASE TITLE: {title}

INSTRUCTIONS:
1. Compare the case title with BOTH Target and Acquirer names in the deals list.
2. When matching, also consider target_aliases and parent_aliases - if the title matches an alias, treat it as a match for that deal.
3. Look for EXACT matches, partial matches, or variations of company names.
4. Consider that the title might be:
   - The full company name
   - A department/division name that matches the company
   - An alias (target_aliases or parent_aliases)
5. If the title appears in ANY form in a deal's Target, Acquirer, or aliases, it's a match.
6. Accept suffix variations (Inc., Ltd., PLC).

RESPONSE FORMAT:
- If you find a match, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|Warburg Pincus|acquirer

- If NO match is found after thorough checking, respond with:
  None

IMPORTANT: Check carefully - if the title matches or is contained in any Target, Acquirer, or alias name, return the match.
"""

    with open(PROMPT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            f"\n{'='*80}\n{datetime.datetime.now()} - Prompt for: {title}\n{prompt}\n")

    try:
        res = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You identify M&A deals from UK CMA merger case titles. Return Match: DEAL_ID|COMPANY|target|acquirer or None.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        result = res.choices[0].message.content.strip()
        print(f"  🧠 LLM match response: {result}")
        return result
    except Exception as e:
        print(f"  ❌ LLM error: {e}")
        return "None"


def parse_match_result(match_result):
    """Parse LLM match result. Returns (deal_id, company_name, role) or None."""
    if not match_result or str(match_result).strip().lower() == "none":
        return None
    stripped = str(match_result).strip()
    if not stripped.lower().startswith("match:"):
        return None
    parts = stripped[6:].strip().split("|")
    if len(parts) < 3:
        return None
    deal_id = parts[0].strip()
    company_name = parts[1].strip()
    role = parts[2].strip().lower().replace("(", "").replace(")", "")
    if role not in ("target", "acquirer"):
        role = "acquirer"
    return deal_id, company_name, role


def find_deal_by_id(deal_id):
    deal_by_id = {str(d.get("deal_id", ""))
                      : d for d in deals if d.get("deal_id")}
    return deal_by_id.get(deal_id)


# ===================================================================
# LLM: verify USA relation
# ===================================================================

def verify_usa_relation(company_details):
    prompt = f"""
You are a business analyst specializing in M&A and competition law cases.

Given the following companies from a UK CMA merger case, determine if this deal or these companies are related to USA.

Company Details:
{company_details}

Consider the following when determining if this is related to USA:
- Are any of these companies headquartered in USA?
- Do any of these companies have significant operations, subsidiaries, or business presence in USA?
- Is this deal likely to have material impact on USA markets?
- Are any of these companies publicly traded in USA?

Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert analyst. Respond with only 'true' or 'false' (lowercase) to indicate if companies are related to USA.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=10,
        )
        result = response.choices[0].message.content.strip().lower()
        if result == "true":
            return True
        if result == "false":
            return False
        print(
            f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
        return False
    except Exception as e:
        print(f"⚠️ LLM USA-relation error: {e}")
        return False


# ===================================================================
# Email generation & sending
# ===================================================================

def _build_common_case_rows(case_info):
    """Build the common HTML table rows shared by both matched and unmatched emails."""
    title = case_info.get("title", "N/A")
    description = case_info.get("description", "")
    case_type = case_info.get("case_type", "")
    case_state = case_info.get("case_state", "")
    opened_date = case_info.get("opened_date", "")
    published_date = case_info.get("published_date", "")
    last_updated = case_info.get("last_updated", "")
    url = case_info.get("detail_url", "")

    row_idx = 0

    def row_bg():
        nonlocal row_idx
        bg = ' style="background-color:#f9f9f9;"' if row_idx % 2 == 1 else ""
        row_idx += 1
        return bg

    rows = f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; width:170px; color:#555;">Case Title:</td><td style="padding:8px; color:#333;">{escape_html(str(title))}</td></tr>"""

    if description:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Case Description:</td><td style="padding:8px; color:#333;">{escape_html(str(description))}</td></tr>"""
    if case_type:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Case Type:</td><td style="padding:8px; color:#333;">{escape_html(str(case_type))}</td></tr>"""
    if case_state:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Case State:</td><td style="padding:8px; color:#333;">{escape_html(str(case_state))}</td></tr>"""
    if opened_date:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Opened Date:</td><td style="padding:8px; color:#333;">{escape_html(str(opened_date))}</td></tr>"""
    if published_date:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Published Date:</td><td style="padding:8px; color:#333;">{escape_html(str(published_date))}</td></tr>"""
    if last_updated:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Last Updated:</td><td style="padding:8px; color:#333;">{escape_html(str(last_updated))}</td></tr>"""
    if url:
        rows += f"""
      <tr{row_bg()}><td style="padding:8px; font-weight:bold; color:#555;">Case URL:</td><td style="padding:8px;"><a href="{escape_html(url)}" style="color:#0066cc; text-decoration:none;" target="_blank">View CMA Case Page</a></td></tr>"""

    return rows


def _build_history_section(case_info):
    """Build the HTML history list section if history is available."""
    history = case_info.get("history", [])
    if not history:
        return ""

    section = """
    <div style="margin-top:25px;">
      <h3 style="color:#333; margin-bottom:12px; border-bottom:2px solid #e0e0e0; padding-bottom:8px;">History:</h3>
      <ul style="margin:0; padding-left:20px;">"""
    for entry in history:
        date = entry.get("date", "N/A")
        note = entry.get("note", "")
        if note:
            section += f"""
        <li style="margin-bottom:8px;"><strong>{escape_html(str(date))}</strong>: {escape_html(str(note))}</li>"""
        else:
            section += f"""
        <li style="margin-bottom:8px;"><strong>{escape_html(str(date))}</strong></li>"""
    section += """
      </ul>
    </div>"""
    return section


def generate_matched_email_html(case_info, deal_match):
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")
    title = case_info.get("title", "N/A")

    subject = f"[FRMD] UK CMA Merger Case (New) – {target} / {acquirer}"
    title_text = (
        f"🆕 NEW UK CMA Merger Case – {target} / {acquirer}"
        if target != "N/A" and acquirer != "N/A"
        else f"🆕 NEW UK CMA Merger Case – {title[:50]}"
    )

    common_rows = _build_common_case_rows(case_info)
    history_section = _build_history_section(case_info)

    deal_rows = f"""
      <tr style="background-color:#e8f5e9;"><td style="padding:8px; font-weight:bold; color:#555;">Deal ID:</td><td style="padding:8px; color:#333;">{escape_html(str(deal_id))}</td></tr>
      <tr style="background-color:#e8f5e9;"><td style="padding:8px; font-weight:bold; color:#555;">Target:</td><td style="padding:8px; color:#333;">{escape_html(target)}</td></tr>
      <tr style="background-color:#e8f5e9;"><td style="padding:8px; font-weight:bold; color:#555;">Acquirer:</td><td style="padding:8px; color:#333;">{escape_html(acquirer)}</td></tr>"""

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #28a745;">
      {escape_html(title_text)}
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#28a745; color:white; padding:8px 16px; border-radius:4px; display:inline-block; font-weight:bold;">🆕 NEW CASE</div>
    </div>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
{common_rows}
    </table>
    <h3 style="color:#333; margin-bottom:12px; border-bottom:2px solid #28a745; padding-bottom:8px;">Matched Deal Info:</h3>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
{deal_rows}
    </table>
{history_section}
    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from UK CMA merger case matches.</p>
    </div>
  </div>
</body>
</html>"""
    return subject, html_email


def generate_unmatched_email_html(case_info):
    title = case_info.get("title", "N/A")

    subject = f"[FRUD] UK CMA Merger Case (USA-Related) – {title[:50]}"

    common_rows = _build_common_case_rows(case_info)
    history_section = _build_history_section(case_info)

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #f59e0b;">
      UK CMA Merger Case (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; font-weight:bold;">🇺🇸 USA-RELATED</div>
    </div>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
{common_rows}
    </table>
{history_section}
    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from UK CMA merger case matches.</p>
    </div>
  </div>
</body>
</html>"""
    return subject, html_email


def send_email_via_webhook(subject, html_email, extra_payload=None):
    try:
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
        )
        payload = {"subject": subject, "html": html_email}
        if extra_payload:
            payload.update(extra_payload)

        print(f"  📤 Sending email: {subject[:80]}")
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  ✅ Email sent! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"  ⚠️ Error sending email: {e}")
        return False


# ===================================================================
# Process a single record
# ===================================================================

def process_record(record, existing_urls):
    detail_url = record.get("url", "")
    title = record.get("title", "")

    if not detail_url:
        print(f"  ⏩ Skipped (no URL): {title[:60]}")
        return None

    if detail_url in existing_urls:
        print(f"  ⏩ Already in DB: {title[:60]}")
        return None

    # Scrape detail page
    scraped = scrape_detail_page(detail_url)
    if not scraped:
        print(f"  ⚠️ Failed to scrape, skipping: {title[:60]}")
        return None

    # Build the record to insert
    case_record = {
        "atom_id": record.get("id", ""),
        "atom_updated": record.get("updated", ""),
        "detail_url": detail_url,
        "title": scraped.get("title") or title,
        "description": scraped.get("description"),
        "case_type": scraped.get("case_type"),
        "case_state": scraped.get("case_state"),
        "market_sector": scraped.get("market_sector"),
        "outcome": scraped.get("outcome"),
        "opened_date": scraped.get("opened_date"),
        "closed_date": scraped.get("closed_date"),
        "history": scraped.get("history", []),
        "published_date": scraped.get("published_date"),
        "last_updated": scraped.get("last_updated"),
        "created_at": datetime.datetime.utcnow(),
        "updated_at": datetime.datetime.utcnow(),
    }

    # --- LLM deal matching ---
    match_result = match_title_with_deals(case_record["title"])
    parsed = parse_match_result(match_result)

    if parsed:
        deal_id, company_name, role = parsed
        deal_match = find_deal_by_id(deal_id)

        if deal_match:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name", "N/A")
            target = deal_match.get("target") or deal_match.get(
                "target_name", "N/A")
            print(
                f"  🎯 Match: {company_name} ({role}) -> {acquirer} / {target}")

            case_record["deal_id"] = deal_id

            # Generate & send email
            email_info = {**case_record, "updated": record.get("updated", "")}
            subj, html = generate_matched_email_html(email_info, deal_match)
            send_email_via_webhook(subj, html, {
                "deal_id": deal_id,
                "target": target,
                "acquirer": acquirer,
                "title": case_record["title"],
                "url": detail_url,
                "is_new_case": True,
            })
        else:
            print(f"  ⚠️ Deal ID from LLM not found in deals list: {deal_id}")
    else:
        # No deal match -> check USA relation
        print(f"  ➖ No deal match for: {case_record['title'][:60]}")
        try:
            is_usa = verify_usa_relation(case_record["title"])
            if is_usa:
                print(f"  🇺🇸 USA-related - sending email")
                email_info = {**case_record,
                              "updated": record.get("updated", "")}
                subj, html = generate_unmatched_email_html(email_info)
                send_email_via_webhook(subj, html, {
                    "title": case_record["title"],
                    "url": detail_url,
                    "is_unmatched": True,
                    "usa_related": True,
                })
            else:
                print(f"  ℹ️ Not USA-related - no email")
        except Exception as e:
            print(f"  ⚠️ Error checking USA relation: {e}")

    # Insert into uk_cma_cases collection
    insert_uk_cma_case(case_record)
    return case_record


# ===================================================================
# Main
# ===================================================================

def main():
    print(f"\n{'='*60}")
    print(f"🚀 UK CMA Open Mergers Scraper (new_uk_cma_mergers_scraper_atom)")
    print(f"{'='*60}\n")

    # Step 1: MongoDB connection
    print("🔌 Connecting to MongoDB...")
    success, msg = init_mongodb_connection()
    if success:
        print(f"✅ {msg}\n")
    else:
        print(f"❌ {msg}")
        print("Exiting - MongoDB is required.\n")
        return

    # Step 2: Load deals
    print("📊 Loading deals from MongoDB...")
    load_deals()

    # Step 3: Get existing open uk_cma_cases detail_urls for dedup
    print("\n📋 Fetching existing open uk_cma_cases for dedup...")
    existing_urls = get_existing_open_case_urls()

    # Step 4: Fetch & parse Atom feed
    print(f"\n{'='*60}")
    print("🌐 FETCHING & PARSING ATOM FEED")
    print(f"{'='*60}\n")
    xml_content = fetch_atom_feed()
    if not xml_content:
        print("❌ Failed to fetch Atom feed. Exiting.")
        return

    atom_records = parse_atom_feed(xml_content)
    print(f"\n📊 Total entries from feed: {len(atom_records)}")

    # Step 5: Process each record
    print(f"\n{'='*60}")
    print("🔄 PROCESSING RECORDS")
    print(f"{'='*60}\n")

    new_count = 0
    skipped_count = 0

    for idx, record in enumerate(atom_records, 1):
        title = record.get("title", "N/A")
        print(f"\n[{idx}/{len(atom_records)}] {title[:70]}")

        result = process_record(record, existing_urls)
        if result:
            new_count += 1
            existing_urls.add(record.get("url", ""))
        else:
            skipped_count += 1

    # Summary
    print(f"\n{'='*60}")
    print("✅ ALL DONE!")
    print(f"{'='*60}")
    print(f"📊 Total feed entries: {len(atom_records)}")
    print(f"🆕 New records processed: {new_count}")
    print(f"⏩ Skipped (already in DB): {skipped_count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
