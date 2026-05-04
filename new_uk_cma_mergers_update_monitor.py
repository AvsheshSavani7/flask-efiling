from dotenv import load_dotenv
import datetime
import os
import re
import sys
import logging
import requests
import time
import traceback
from logging.handlers import RotatingFileHandler
from bson import ObjectId
from bs4 import BeautifulSoup
from html import escape as escape_html
from openai import OpenAI
from pymongo import MongoClient
from typing import Optional, Tuple
from error_email_service import send_error_email
from log_utils import cleanup_old_logs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROMPT_LOG_PATH = "cma_gpt_prompts.log"
ENV_PATH = ".env"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "uk_cases_update_monitor"
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


def _log_critical_error_and_email(msg: str, context: Optional[dict] = None):
    """Immediate error email — use ONLY for critical startup / fatal failures."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context or {},
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


FIELDS_TO_COMPARE = [
    "title",
    "case_state",
    "history",
    "opened_date",
    "closed_date",
    "published_date",
    "last_updated",
]

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
        logger.exception(f"Error fetching deals from MongoDB: {e}")
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

def get_open_cases() -> list:
    """Return all uk_cma_cases documents where case_state is Open."""
    if _uk_cma_cases_collection is None:
        print("⚠️ uk_cma_cases collection not available.")
        return []
    try:
        cases = list(_uk_cma_cases_collection.find({"case_state": "Open"}))
        print(f"📋 Found {len(cases)} open cases in uk_cma_cases collection")
        return cases
    except Exception as e:
        print(f"⚠️ Error fetching open cases: {e}")
        traceback.print_exc()
        return []


def update_uk_cma_case(doc_id, update_fields: dict) -> bool:
    """Update a uk_cma_cases document by its _id."""
    if _uk_cma_cases_collection is None:
        print("⚠️ uk_cma_cases collection not available, cannot update.")
        return False
    try:
        update_fields["updated_at"] = datetime.datetime.utcnow()
        result = _uk_cma_cases_collection.update_one(
            {"_id": doc_id},
            {"$set": update_fields},
        )
        if result.modified_count > 0:
            print(f"  ✅ Updated uk_cma_cases record")
            return True
        print(f"  ℹ️ No changes written (matched={result.matched_count})")
        return True
    except Exception as e:
        print(f"  ❌ Error updating uk_cma_cases: {e}")
        traceback.print_exc()
        return False


# ===================================================================
# HTML scraping
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


def scrape_detail_page(url, max_retries: int = 2):
    """Fetch the detail page HTML and extract all fields. Retries on failure."""
    last_error = None
    last_status = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"  🌐 Attempt {attempt}/{max_retries} — scraping: {url}")
            resp = requests.get(url, timeout=60)
            last_status = resp.status_code
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
            last_error = e
            logger.warning(
                f"    Attempt {attempt}/{max_retries} failed scraping {url}: {e}")
            if attempt < max_retries:
                logger.info(f"    Retrying in 10s...")
            time.sleep(10)

    explanation = (
        f"Failed to scrape UK CMA detail page after {max_retries} attempts. "
        f"URL: {url}. Last error: {last_error}. "
        f"The page may be temporarily unavailable or its structure may have changed."
    )
    _log_critical_error_and_email(
        explanation,
        {
            "step": "scrape_detail_page",
            "page_url": url,
            "attempts": str(max_retries),
            "last_http_status": str(last_status) if last_status else "no response",
            "last_error": str(last_error),
            "possible_causes": (
                "1) UK CMA website temporarily down or slow; "
                "2) Network issue between server and gov.uk; "
                "3) Page HTML structure changed (parser broken)"
            ),
        },
    )
    return None


# ===================================================================
# Change detection
# ===================================================================

def _history_key(entry):
    return f"{entry.get('date', '')}|{entry.get('datetime', '')}|{entry.get('note', '')}"


def detect_changes(db_record, scraped):
    """Compare DB record with freshly scraped data. Returns dict of changes."""
    changes = {}
    print(f"Detect changes: db_record: {db_record}")
    print(f"Detect changes: scraped: {scraped}")

    for field in FIELDS_TO_COMPARE:
        if field == "history":
            continue
        old_val = db_record.get(field) or ""
        new_val = scraped.get(field) or ""
        if str(old_val).strip() != str(new_val).strip():
            changes[field] = {"old": old_val, "new": new_val}

    old_history = db_record.get("history", []) or []
    new_history = scraped.get("history", []) or []
    old_keys = {_history_key(e) for e in old_history}
    new_entries = [e for e in new_history if _history_key(e) not in old_keys]
    if new_entries:
        changes["new_history_entries"] = new_entries

    return changes


# ===================================================================
# LLM: match title with deals
# ===================================================================

def match_title_with_deals(title):
    """Ask LLM if this CMA case title matches any deal. Returns deal_id or None."""
    global deals
    if not deals:
        print("⚠️ Deals list is empty, reloading...")
        load_deals()

    if not deals:
        print("⚠️ No deals with company names found")
        return None

    lines = []
    for deal in deals:
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        if not target and not acquirer:
            continue
        line = f"Deal ID: {deal.get('deal_id', 'N/A')} | Target: {target} | Acquirer: {acquirer}"
        for alias_key in ("target_aliases", "parent_aliases"):
            aliases = deal.get(alias_key) or []
            if aliases:
                line += f" | {alias_key}: {', '.join(str(a) for a in aliases)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""You are a professional M&A analyst specializing in UK merger cases.

Below is a CMA merger case title. Your task is to match it with any of the deals listed below.

DEALS TO MATCH:
{deals_text}

CASE TITLE: {title}

INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned in the UK CMA case text (title).
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the UK CMA case text.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the UK CMA case text.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the UK CMA case text:
   - one match for the Acquirer side
   - one match for the Target side
5. Do not return a match if only one side is present, even if that single company is an exact match.
6. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - “Inc.” vs “Incorporated”
   - “Corp.” vs “Corporation”
   - “Ltd” vs “Limited”
   - obvious spacing/casing differences
7. Do not match based only on sector, business type, article topic, indirect association, or partial deal overlap.
8. If the UK CMA case text does not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
If you find a match, respond EXACTLY: Match: DEAL_ID
If no deal satisfies this rule, respond exactly: None"""

    with open(PROMPT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            f"\n{'='*80}\n{datetime.datetime.now()} - Prompt for: {title}\n{prompt}\n")

    try:
        res = openai_client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You identify M&A deals from UK CMA merger case titles. Respond only with Match: DEAL_ID or None.",
                },
                {"role": "user", "content": prompt},
            ]
        )
        content = (res.choices[0].message.content or "").strip()
        print(f"  🧠 LLM match response: {content}")
        if not content.lower().startswith("match"):
            return None
        try:
            _prefix, deal_id_raw = content.split(":", 1)
            return deal_id_raw.strip() or None
        except Exception:
            return None
    except Exception as e:
        print(f"  ❌ LLM error: {e}")
        return None


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
            model="gpt-5.2",
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


def _build_changes_section(changes):
    """Build 'What's Changed' HTML block from the changes dict."""
    if not changes:
        return ""

    section = """
    <div style="margin-top:25px; padding:15px; background-color:#fff3cd; border-left:4px solid #ff9800; border-radius:4px;">
      <h3 style="color:#856404; margin-top:0; margin-bottom:12px;">📋 What's Changed:</h3>
      <ul style="margin:0; padding-left:20px; color:#856404;">"""

    field_labels = {
        "title": "Title",
        "case_state": "Case State",
        "opened_date": "Opened Date",
        "closed_date": "Closed Date",
        "published_date": "Published Date",
        "last_updated": "Last Updated",
    }

    for field, label in field_labels.items():
        if field in changes:
            old = changes[field].get("old", "N/A") or "N/A"
            new = changes[field].get("new", "N/A") or "N/A"
            section += f"""
        <li style="margin-bottom:8px;">
          <strong>{escape_html(label)}:</strong> {escape_html(str(old))} → <strong style="color:#ff9800;">{escape_html(str(new))}</strong>
        </li>"""

    new_history = changes.get("new_history_entries", [])
    if new_history:
        section += f"""
        <li style="margin-bottom:8px;">
          <strong>New History Entries ({len(new_history)}):</strong>
          <ul style="margin-top:5px; padding-left:20px;">"""
        for entry in new_history:
            date = entry.get("date", "N/A")
            note = entry.get("note", "N/A")
            section += f"""
            <li style="margin-bottom:5px;">
              <strong style="color:#ff9800;">{escape_html(str(date))}</strong>: {escape_html(str(note))}
            </li>"""
        section += """
          </ul>
        </li>"""

    section += """
      </ul>
    </div>"""
    return section


def _build_history_section(case_info, new_history_keys=None):
    """Build history list with new entries highlighted."""
    history = case_info.get("history", [])
    if not history:
        return ""

    if new_history_keys is None:
        new_history_keys = set()

    section = """
    <div style="margin-top:25px;">
      <h3 style="color:#333; margin-bottom:12px; border-bottom:2px solid #e0e0e0; padding-bottom:8px;">History:</h3>
      <ul style="margin:0; padding-left:20px;">"""
    for entry in history:
        date = entry.get("date", "N/A")
        note = entry.get("note", "")
        key = _history_key(entry)
        is_new = key in new_history_keys

        if is_new:
            if note:
                section += f"""
        <li style="margin-bottom:8px; padding:8px; background-color:#fff3cd; border-left:3px solid #ff9800; border-radius:3px;">
          <strong style="color:#ff9800;">🆕 {escape_html(str(date))}</strong>: {escape_html(str(note))}</li>"""
            else:
                section += f"""
        <li style="margin-bottom:8px; padding:8px; background-color:#fff3cd; border-left:3px solid #ff9800; border-radius:3px;">
          <strong style="color:#ff9800;">🆕 {escape_html(str(date))}</strong></li>"""
        else:
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


def generate_update_email_html(case_info, changes, deal_match=None):
    """Generate update email with changes highlighted. deal_match is optional."""
    title = case_info.get("title", "N/A")

    if deal_match:
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        subject = f"[FRMD] UK CMA Merger Case (Updated) – {target} / {acquirer}"
        title_text = (
            f"📝 UK CMA Merger Case Update – {target} / {acquirer}"
            if target != "N/A" and acquirer != "N/A"
            else f"📝 UK CMA Merger Case Update – {title[:50]}"
        )
    else:
        subject = f"[FRUD] UK CMA Merger Case (Updated, USA-Related) – {title[:50]}"
        title_text = f"📝 UK CMA Merger Case Update – {title[:50]}"

    common_rows = _build_common_case_rows(case_info)
    changes_section = _build_changes_section(changes)

    new_history_keys = set()
    for entry in changes.get("new_history_entries", []):
        new_history_keys.add(_history_key(entry))
    history_section = _build_history_section(case_info, new_history_keys)

    deal_section = ""
    if deal_match:
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")
        deal_section = f"""
    <h3 style="color:#333; margin-bottom:12px; border-bottom:2px solid #ff9800; padding-bottom:8px;">Matched Deal Info:</h3>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr style="background-color:#e8f5e9;"><td style="padding:8px; font-weight:bold; color:#555;">Deal ID:</td><td style="padding:8px; color:#333;">{escape_html(str(deal_id))}</td></tr>
      <tr style="background-color:#e8f5e9;"><td style="padding:8px; font-weight:bold; color:#555;">Target:</td><td style="padding:8px; color:#333;">{escape_html(target)}</td></tr>
      <tr style="background-color:#e8f5e9;"><td style="padding:8px; font-weight:bold; color:#555;">Acquirer:</td><td style="padding:8px; color:#333;">{escape_html(acquirer)}</td></tr>
    </table>"""

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #ff9800;">
      {escape_html(title_text)}
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#ff9800; color:white; padding:8px 16px; border-radius:4px; display:inline-block; font-weight:bold;">📝 CASE UPDATED</div>
    </div>
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
{common_rows}
    </table>
{deal_section}
{changes_section}
{history_section}
    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from UK CMA merger case update monitoring.</p>
    </div>
  </div>
</body>
</html>"""
    return subject, html_email


def send_email_via_webhook(subject, html_email, extra_payload=None):
    try:
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
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
# Process a single open case
# ===================================================================

def process_case(db_record):
    doc_id = db_record.get("_id")
    detail_url = db_record.get("detail_url", "")
    title = db_record.get("title", "N/A")
    print(f"STEP 1.5.2: DB record: {db_record}")
    print(f"STEP 1.5.3: Detail URL: {detail_url}")
    print(f"STEP 1.5.4: Title: {title}")
    if not detail_url:
        print(f"  ⏩ Skipped (no detail_url): {title[:60]}")
        print(f"STEP 1.5.5: Skipped (no detail_url): {title[:60]}")
        return False

    # Scrape current state of the detail page
    scraped = scrape_detail_page(detail_url)
    print(f" Scraped: {scraped}")
    if not scraped:
        print(f"  ⚠️ Failed to scrape, skipping: {title[:60]}")
        print(f"STEP 1.5.6: Failed to scrape, skipping: {title[:60]}")
        return False

    # Detect changes
    changes = detect_changes(db_record, scraped)
    print(f"Changes: {changes}")

    if not changes:
        print(f"  ✅ No changes detected")
        print(f"STEP 1.5.7: No changes detected")
        # Still update the scraped fields to keep data fresh
        update_uk_cma_case(doc_id, {
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
        })
        print("STEP 1.5.8: Updated DB record (no-change refresh)")
        return False

    print(f"  📝 Changes detected: {list(changes.keys())}")
    print(f"STEP 1.5.9: Changes detected: {list(changes.keys())}")

    # Build case_info for email (use scraped data as current)
    case_info = {
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
    }
    print(f"STEP 1.5.10: Case info: {case_info}")
    deal_id = db_record.get("deal_id")
    deal_match = None
    print(f"STEP 1.5.11: Deal ID: {deal_id}")
    if deal_id:
        # Already has a deal_id -> find deal and send update email
        deal_match = find_deal_by_id(deal_id)
        print(f"STEP 1.5.12: Deal match: {deal_match}")
        if deal_match:
            print(f"  🎯 Existing deal_id: {deal_id}")
            print(f"STEP 1.5.13: Existing deal_id: {deal_id}")
            subj, html = generate_update_email_html(
                case_info, changes, deal_match)
            send_email_via_webhook(subj, html, {
                "deal_id": deal_id,
                "title": case_info["title"],
                "url": detail_url,
                "is_update": True,
            })
            print(f"STEP 1.5.14: Email sent: {subj}")
        else:
            print(
                f"  ⚠️ deal_id {deal_id} not found in deals list, treating as unmatched")
            deal_id = None
            print(f"STEP 1.5.15: No deal match for: {case_info['title'][:60]}")
    if not deal_id:
        # No deal_id -> try LLM matching
        matched_deal_id = match_title_with_deals(case_info["title"])
        print(f"STEP 1.5.16: Matched deal ID: {matched_deal_id}")
        deal_match = find_deal_by_id(
            matched_deal_id) if matched_deal_id else None
        print(f"STEP 1.5.17: Deal match: {deal_match}")
        if deal_match:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name", "N/A")
            target = deal_match.get("target") or deal_match.get(
                "target_name", "N/A")
            print(f"  🎯 New match: {acquirer} / {target}")
            print(f"STEP 1.5.18: New match: {acquirer} / {target}")
            deal_id = matched_deal_id

            subj, html = generate_update_email_html(
                case_info, changes, deal_match)
            send_email_via_webhook(subj, html, {
                "deal_id": deal_id,
                "title": case_info["title"],
                "url": detail_url,
                "is_update": True,
            })
            print(f"STEP 1.5.19: Email sent: {subj}")
        else:
            if matched_deal_id:
                print(f"  ⚠️ Deal ID from LLM not found: {matched_deal_id}")
            # No match -> check USA relation
            print(f"  ➖ No deal match for: {case_info['title'][:60]}")
            print(f"STEP 1.5.20: No deal match for: {case_info['title'][:60]}")
            try:
                is_usa = verify_usa_relation(case_info["title"])
                print(f"STEP 1.5.21: USA relation: {is_usa}")
                if is_usa:
                    print(f"  🇺🇸 USA-related - sending update email")
                    print(f"STEP 1.5.22: Sending update email")
                    subj, html = generate_update_email_html(case_info, changes)
                    send_email_via_webhook(subj, html, {
                        "title": case_info["title"],
                        "url": detail_url,
                        "is_update": True,
                        "usa_related": True,
                    })
                    print(f"STEP 1.5.23: Email sent: {subj}")
                else:
                    print(f"  ℹ️ Not USA-related - no email, updating DB only")
            except Exception as e:
                logger.exception(f"  Error checking USA relation: {e}")
                print(f"STEP 1.5.24: Error checking USA relation: {e}")
    # Update the DB record with all scraped fields
    update_fields = {
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
    }
    if deal_id:
        update_fields["deal_id"] = deal_id
    print(f"STEP 1.5.25: Update fields: {update_fields}")
    update_uk_cma_case(doc_id, update_fields)
    print(f"STEP 1.5.26: Updated DB record: {update_fields}")
    return True


# ===================================================================
# Main
# ===================================================================

def main():
    run_start = datetime.datetime.now()
    logger.info("=" * 60)
    logger.info("STEP 1: Starting UK CMA Cases Update Monitor")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)
    print(f"\n{'='*60}")
    print("STEP 1.1: UK CMA Open Mergers Update Monitor")
    print(f"{'='*60}\n")

    # Step 1: MongoDB connection
    print("STEP 1.2: Connecting to MongoDB...")
    success, msg = init_mongodb_connection()
    if success:
        print(f"✅ {msg}\n")
    else:
        _log_critical_error_and_email(
            f"MongoDB connection failed: {msg}",
            {"step": "mongodb_connect"},
        )
        return

    # Step 2: Load deals
    print("STEP 1.3: Loading deals from MongoDB...")
    load_deals()

    # Step 3: Fetch open cases from uk_cma_cases
    print("\nSTEP 1.4: Fetching open cases from uk_cma_cases...")
    open_cases = get_open_cases()
    if not open_cases:
        print("STEP 1.4.1: No open cases to monitor. Exiting.")
        return

    # Step 4: Process each open case
    print(f"\n{'='*60}")
    print("STEP 1.5: PROCESSING OPEN CASES")
    print(f"{'='*60}\n")

    updated_count = 0
    unchanged_count = 0

    for idx, case in enumerate(open_cases, 1):
        print(f"STEP 1.5.1: Case: {case}")
        title = case.get("title", "N/A")
        print(f"\n[{idx}/{len(open_cases)}] {title[:70]}")

        had_changes = process_case(case)
        print(f"STEP 1.5.3: Had changes: {had_changes}")
        if had_changes:
            updated_count += 1
        else:
            print(f"STEP 1.5.4: Unchanged")
            unchanged_count += 1

    # Summary
    print(f"\n{'='*60}")
    print("STEP 1.6: ALL DONE!")
    print(f"{'='*60}")
    print(f"STEP 1.6.1: Total open cases checked: {len(open_cases)}")
    print(f"STEP 1.6.2: Cases with updates: {updated_count}")
    print(f"STEP 1.6.3: Cases unchanged: {unchanged_count}")
    print(f"{'='*60}\n")
    elapsed = round((datetime.datetime.now() - run_start).total_seconds(), 1)
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(
        f"STEP 1.6.4: Total open cases checked     : {len(open_cases)}")
    logger.info(f"STEP 1.6.5: Cases with updates           : {updated_count}")
    logger.info(
        f"STEP 1.6.6: Cases unchanged              : {unchanged_count}")
    logger.info(f"STEP 1.6.7: Total time                   : {elapsed}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log_critical_error_and_email(
            f"Unhandled error in main: {e}", {"step": "main"})
        raise
