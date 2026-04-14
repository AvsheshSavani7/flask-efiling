"""
Canada Cases Register
=====================

Scrapes the Competition Bureau Canada's "Report of merger reviews" page
and registers new cases in the dedicated 'canada_cases' MongoDB collection.

Flow:
1. Fetch HTML table from Competition Bureau
2. Parse rows and filter by CUTOFF_DATE (3 days ago) + concluded_date == "Ongoing"
3. For each new row:
   - Check if already exists in canada_cases (skip if yes)
   - LLM call #1: Try to match with existing deals
   - LLM call #2 (if no match): Check if USA-related
   - Insert ALL cases into DB (matched, USA-related, or neither) with is_open=True
   - Send rich HTML email notifications for matched / USA-related cases
"""


from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from llm_verification_service import verify_usa_relation
import logging
import builtins
from datetime import datetime, timedelta
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
import os
import json
import sys


load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging setup (stdout + file)
# -----------------------------------------------------------------------------
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
LOG_FILE = "canada_cases_register.log"
logger = logging.getLogger("canada_cases_register")
logger.setLevel(LOG_LEVEL)

if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    """Replacement for print that also logs via the module logger."""
    msg = " ".join(str(a) for a in args)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)
    builtins.print(*args, **kwargs)


print = _logged_print  # type: ignore

# OpenAI client for LLM matching
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants
ENV_PATH = ".env"
REPORT_URL = (
    "https://competition-bureau.canada.ca/en/mergers-and-acquisitions/"
    "report-concluded-merger-reviews#wb-auto-4"
)
BACKUP_JSON = "canada_cases_register_backup.json"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    # Kaushal/Josh/Avs
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
)
# N8N_WEBHOOK_URL = os.getenv(
#     "N8N_WEBHOOK_URL",
#     "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",  # Avs only
# )

# Cutoff: 3 days ago
CUTOFF_DATE = datetime.now() - timedelta(days=3)


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_canada_cases_collection():
    """Get or create the 'canada_cases' collection in the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["canada_cases"]


def fetch_report_html(url: str = REPORT_URL) -> Optional[str]:
    """Fetch HTML from the Competition Bureau report page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        print(f"📥 Fetching Competition Bureau report: {url}")
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        print(f"✅ Fetched HTML ({len(resp.text)} bytes)")
        return resp.text
    except requests.RequestException as e:
        print(f"❌ Error fetching report page: {e}", level="error")
        return None


def parse_opened_date(date_str: str) -> Optional[datetime]:
    """Parse Opened Date (format: YYYY-MM-DD)."""
    if not date_str:
        return None
    ds = date_str.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(ds, fmt)
        except ValueError:
            continue
    return None


def parse_merger_table(html_content: str) -> List[Dict[str, Any]]:
    """
    Extract merger rows from the main .table-responsive table.

    Columns:
    - Parties to the Transaction
    - Opened Date
    - Concluded Date
    - Industry (NAICS)
    - Outcome
    """
    soup = BeautifulSoup(html_content, "html.parser")

    table = None
    for div in soup.select("div.table-responsive"):
        candidate = div.find("table")
        if not candidate:
            continue
        header_row = candidate.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower()
                   for th in header_row.find_all("th")]
        if headers and "parties to the transaction" in headers[0]:
            table = candidate
            break

    if table is None:
        print("⚠️ Could not locate merger reviews table", level="warning")
        return []

    rows = table.find_all("tr")
    if len(rows) <= 1:
        return []

    data_rows: List[Dict[str, Any]] = []
    for tr in rows[1:]:
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        try:
            parties_text = cells[0].get_text(separator=" ", strip=True)
            opened_str = cells[1].get_text(strip=True)
            concluded_str = cells[2].get_text(strip=True)
            industry_str = cells[3].get_text(strip=True)
            outcome_str = cells[4].get_text(strip=True)

            opened_dt = parse_opened_date(opened_str)

            row_data: Dict[str, Any] = {
                "parties": parties_text,
                "opened_date": opened_str,
                "concluded_date": concluded_str,
                "industry": industry_str,
                "outcome": outcome_str,
                "opened_date_parsed": opened_dt,
            }
            data_rows.append(row_data)
        except Exception as e:
            print(f"⚠️ Error parsing table row: {e}", level="warning")
            continue

    print(f"✅ Parsed {len(data_rows)} merger rows from table")
    return data_rows


def case_exists(collection, parties: str, opened_date: str) -> bool:
    """Check if a case with this parties+opened_date already exists in canada_cases."""
    try:
        existing = collection.count_documents(
            {"parties": parties, "opened_date": opened_date}, limit=1
        )
        return existing > 0
    except Exception as e:
        print(f"⚠️ Error checking existing case: {e}", level="warning")
        return False


def match_case_to_deal(parties: str) -> Optional[str]:
    """
    Use LLM to match the case parties to an existing deal.
    Returns deal_id string or None.
    """
    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            return None

        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(deals_collection.find(status_filter))
        if not deals:
            return None

        lines = []
        for d in deals:
            deal_id = str(d.get("_id"))
            target = d.get("target") or d.get("target_name", "N/A")
            acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
            line = f"Deal ID: {deal_id} | Target: {target} | Acquirer: {acquirer}"
            target_aliases = d.get("target_aliases") or []
            parent_aliases = d.get("parent_aliases") or []
            if target_aliases:
                line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
            if parent_aliases:
                line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
            lines.append(line)

        deals_text = "\n".join(lines)

        prompt = f"""You are an expert M&A deal matcher. Your task is to determine if ANY company mentioned in the Canada Competition Bureau case parties appears in our deals database.

DEALS DATABASE:
{deals_text}

PARTIES STRING:
{parties}

MATCHING INSTRUCTIONS:
1. Extract ALL company names from the parties string (both acquirer and target).
2. Check if ANY of these company names appears as either a Target OR Acquirer in the deals database.
3. When matching, also consider target_aliases and parent_aliases - if the title matches an alias, treat it as a match for that deal.
4. Consider variations, abbreviations, and partial matches.
5. Match on a SINGLE company name - you don't need both sides to match.

RESPONSE FORMAT:
- If you find ANY match, respond EXACTLY in this format (no extra text):
  Match: DEAL_ID

- If NO match is found after thorough checking, respond with exactly:
  None
"""

        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert M&A deal identifier and matcher.",
                },
                {"role": "user", "content": prompt},
            ],
        )

        content = (res.choices[0].message.content or "").strip()
        print(f"  🤖 LLM match response: {content}")

        if not content.lower().startswith("match"):
            return None

        try:
            _prefix, deal_id_raw = content.split(":", 1)
            deal_id = deal_id_raw.strip()
            return deal_id or None
        except Exception:
            return None
    except Exception as e:
        print(f"⚠️ LLM match error: {e}", level="warning")
        return None


def generate_matched_case_email_html(
    case_info: Dict[str, Any], deal: Dict[str, Any]
) -> str:
    """Generate rich HTML email for matched Canada case (similar to ACCC style)."""
    case_number = case_info.get("case_number", "N/A")
    parties = case_info.get("parties", "N/A")
    opened_date = case_info.get("opened_date", "N/A")
    concluded_date = case_info.get("concluded_date", "N/A")
    industry = case_info.get("industry", "N/A")
    outcome = case_info.get("outcome", "N/A")

    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = str(deal.get("_id")) if deal.get("_id") else "N/A"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Canada Competition Bureau - New Case</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- Deal Match Info Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;">
    <span style="font-weight:700;">Acquirer:</span> {escape_html(acquirer)} <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Target:</span> {escape_html(target)} <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Deal ID:</span> {escape_html(deal_id)}
  </div>
  <div style="margin-top:10px;">
    <a href="{REPORT_URL}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Competition Bureau Report →</a>
  </div>
</div>

<!-- Top summary panel -->
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">Canada Competition Bureau - New Case</div>
  
  <div style="display:grid;grid-template-columns:200px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">Parties:</div>
    <div>{escape_html(parties)}</div>
    
    <div style="font-weight:700;">Opened Date:</div>
    <div>{escape_html(opened_date)}</div>
    
    <div style="font-weight:700;">Concluded Date:</div>
    <div>{escape_html(concluded_date)}</div>
    
    <div style="font-weight:700;">Industry (NAICS):</div>
    <div>{escape_html(industry)}</div>
    
    <div style="font-weight:700;">Outcome:</div>
    <div>{escape_html(outcome)}</div>
  </div>
</div>

</div>
</body>
</html>"""
    return html


def generate_usa_related_email_html(case_info: Dict[str, Any]) -> str:
    """Generate rich HTML email for USA-related (unmatched) Canada case."""
    parties = case_info.get("parties", "N/A")
    opened_date = case_info.get("opened_date", "N/A")
    concluded_date = case_info.get("concluded_date", "N/A")
    industry = case_info.get("industry", "N/A")
    outcome = case_info.get("outcome", "N/A")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>USA-Related Canada Competition Bureau Case</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- USA-Related Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">🇺🇸 USA-Related Canada Competition Bureau Case</div>
  <div style="font-size:14px;color:#1e3a8a;">
    This merger review appears to involve USA-related parties or markets.
  </div>
  <div style="margin-top:10px;">
    <a href="{REPORT_URL}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Competition Bureau Report →</a>
  </div>
</div>

<!-- Case details -->
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">Case Details</div>
  
  <div style="display:grid;grid-template-columns:200px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">Parties:</div>
    <div>{escape_html(parties)}</div>
    
    <div style="font-weight:700;">Opened Date:</div>
    <div>{escape_html(opened_date)}</div>
    
    <div style="font-weight:700;">Concluded Date:</div>
    <div>{escape_html(concluded_date)}</div>
    
    <div style="font-weight:700;">Industry (NAICS):</div>
    <div>{escape_html(industry)}</div>
    
    <div style="font-weight:700;">Outcome:</div>
    <div>{escape_html(outcome)}</div>
  </div>
</div>

</div>
</body>
</html>"""
    return html


def send_email_via_webhook(
    subject: str,
    html_content: str,
    case_info: Dict[str, Any],
    deal_id: Optional[str] = None,
    usa_related: bool = False,
) -> bool:
    """Send email via n8n webhook."""
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "parties": case_info.get("parties", "N/A"),
            "opened_date": case_info.get("opened_date", "N/A"),
            "concluded_date": case_info.get("concluded_date", "N/A"),
            "industry": case_info.get("industry", "N/A"),
            "outcome": case_info.get("outcome", "N/A"),
            "deal_id": deal_id,
            "usa_related": usa_related,
            "is_new_case": True,
            "source": "canada_competition_bureau",
        }

        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  ✅ Email sent successfully! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"  ⚠️ Error sending email: {e}", level="warning")
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    """Insert a new case document into the canada_cases collection."""
    try:
        result = collection.insert_one(case_info)
        return str(result.inserted_id)
    except Exception as e:
        print(f"⚠️ Error inserting case: {e}", level="error")
        return None


def run_canada_cases_register():
    """Main entrypoint for Canada cases registration."""
    print("🚀 Starting Canada Cases Register\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}", level="error")
        print("   MongoDB connection is required. Exiting.")
        return
    print(f"✅ {message}\n")

    if not is_connected():
        print("❌ MongoDB not connected. Exiting.", level="error")
        return

    collection = get_canada_cases_collection()
    if collection is None:
        print("❌ Could not access 'canada_cases' collection. Exiting.", level="error")
        return

    print(f"📅 CUTOFF_DATE: {CUTOFF_DATE.strftime('%Y-%m-%d')} (3 days ago)\n")

    html = fetch_report_html(REPORT_URL)
    if not html:
        print("❌ Failed to fetch report HTML. Exiting.", level="error")
        return

    all_rows = parse_merger_table(html)
    if not all_rows:
        print("⚠️ No merger rows parsed from table. Exiting.", level="warning")
        return

    new_cases: List[Dict[str, Any]] = []
    cutoff_date_only = CUTOFF_DATE.date()

    print(
        f"📊 Processing rows (filtering by opened_date >= {cutoff_date_only} AND concluded_date == 'Ongoing')...\n")

    for idx, row in enumerate(all_rows, 1):
        concluded_date = (row.get("concluded_date") or "").strip()
        if concluded_date.lower() != "ongoing":
            continue

        opened_dt = row.get("opened_date_parsed")
        if opened_dt is None:
            continue

        try:
            if isinstance(opened_dt, datetime):
                d = opened_dt.date()
            else:
                d = opened_dt

            if d < cutoff_date_only:
                continue
        except Exception:
            continue

        parties = row["parties"]
        opened_date = row["opened_date"]

        print(f"[{idx}] Parties: {parties[:80]}...")
        print(f"    Opened: {opened_date}")

        # Check if already exists
        if case_exists(collection, parties, opened_date):
            print("  ⏩ Case already exists in canada_cases; skipping\n")
            continue

        # LLM Call #1: Try to match with deals
        print("  🔍 LLM Call #1: Checking for deal match...")
        matched_deal_id = match_case_to_deal(parties)

        now_iso = utc_now_iso()
        case_info: Dict[str, Any] = {
            "parties": parties,
            "opened_date": opened_date,
            "concluded_date": row["concluded_date"],
            "industry": row["industry"],
            "outcome": row["outcome"],
            "is_open": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        if matched_deal_id:
            print(f"  🎯 Deal match found (deal_id={matched_deal_id})")
            case_info["deal_id"] = matched_deal_id

            # Fetch full deal for email
            deals_collection = get_deals_collection()
            deal = None
            if deals_collection is not None:
                try:
                    from bson import ObjectId
                    deal = deals_collection.find_one(
                        {"_id": ObjectId(matched_deal_id)})
                except Exception as e:
                    print(f"  ⚠️ Could not fetch deal: {e}", level="warning")

            if deal:
                target = deal.get("target") or deal.get("target_name", "N/A")
                acquirer = deal.get("acquirer") or deal.get(
                    "acquire_name", "N/A")
                subject = f"[FRMD] Canada Competition Bureau (New) – {target} / {acquirer}"
                html_email = generate_matched_case_email_html(case_info, deal)
                send_email_via_webhook(
                    subject, html_email, case_info, deal_id=matched_deal_id)
        else:
            # LLM Call #2: Check if USA-related
            print("  🔍 LLM Call #2: Checking if USA-related...")
            try:
                details_for_llm = (
                    f"Parties: {parties}\n"
                    f"Industry (NAICS): {row['industry']}\n"
                    f"Outcome: {row['outcome']}\n"
                    f"Opened Date: {opened_date}\n"
                    f"Concluded Date: {row['concluded_date']}"
                )
                is_usa = verify_usa_relation(
                    company_details=details_for_llm,
                    case_type="CANADA",
                )
            except Exception as e:
                print(f"  ⚠️ USA relation check error: {e}", level="warning")
                is_usa = False

            if is_usa:
                print("  🇺🇸 Case is USA-related")
                subject = f"[FRUD] Canada Competition Bureau (USA-Related)"
                html_email = generate_usa_related_email_html(case_info)
                send_email_via_webhook(subject, html_email,
                                       case_info, usa_related=True)
            else:
                print("  ℹ️ Not matched and not USA-related")

        # Always insert case into DB
        inserted_id = insert_case(collection, case_info)
        if inserted_id:
            print(
                f"  ✅ Inserted case into canada_cases (id={inserted_id})\n")
            backup_case = dict(case_info)
            backup_case.pop("_id", None)
            new_cases.append(backup_case)

    # Backup JSON
    if new_cases:
        try:
            with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                json.dump(new_cases, f, indent=2, ensure_ascii=False)
            print(
                f"\n💾 Saved {len(new_cases)} new cases to backup JSON: {BACKUP_JSON}")
        except Exception as e:
            print(f"⚠️ Error writing backup JSON: {e}", level="warning")

    print("\n🎉 Canada Cases Register finished")


if __name__ == "__main__":
    run_canada_cases_register()
