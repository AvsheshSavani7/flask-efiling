"""
Competition Bureau Canada – Merger Reviews Scraper
==================================================

Scrapes the Competition Bureau Canada's "Report of merger reviews" page
(`https://competition-bureau.canada.ca/en/mergers-and-acquisitions/report-concluded-merger-reviews#wb-auto-4`)
and extracts the main mergers table inside the `.table-responsive` container.

For each row:
- Parses Parties to the Transaction, Opened Date, Concluded Date, Industry (NAICS), Outcome
- Filters rows by a configurable CUTOFF_DATE (on Opened Date)
- Tries to match party names against deals in MongoDB (by acquirer/target names and aliases),
  **without using any deal_name field**
- Saves matched rows (and unmatched but USA-related rows) into a JSON file,
  sorted by newest Opened Date first.

Saves matched cases to MongoDB under 'canada_competition_bureau_cases' array and sends email notifications.
"""

import json
import logging
import os
import re
from datetime import datetime
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI

from llm_verification_service import verify_usa_relation
from mongodb_connection import (
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)

# Load .env for MongoDB and OpenAI
load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Source URL (provided by user)
REPORT_URL = (
    "https://competition-bureau.canada.ca/en/mergers-and-acquisitions/"
    "report-concluded-merger-reviews#wb-auto-4"
)

# N8N_WEBHOOK_URL = "https://n8n-xwx1.onrender.com/webhook/80830c6d-ff5b-45e3-9ef3-a061db1fbf0c"  # Avs
# Avs/kaushal/josh
N8N_WEBHOOK_URL = "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"

# Filter: only process records with opened_date >= CUTOFF_DATE
# You can override this to a fixed date for backfilling, e.g.:
# CUTOFF_DATE = datetime.strptime("2026-02-05", "%Y-%m-%d")
CUTOFF_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

OUTPUT_PATH = "competition_bureau_canada_matched_deals.json"
ENV_PATH = ".env"
LOG_FILE = "competition_bureau_canada_mergers.log"

# Logger: file + console
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Global state
deals: List[Dict[str, Any]] = []
normalized_deals_index: List[Dict[str, Any]] = []


def normalize_company(name: str) -> str:
    """
    Normalize company names for simple string matching.

    - Lowercase
    - Strip punctuation and common corporate suffixes
    """
    if not name:
        return ""
    n = name.lower()
    # Remove commas and periods
    for ch in [",", ".", ";"]:
        n = n.replace(ch, " ")
    # Remove common suffixes
    for suffix in [
        " inc",
        " inc.",
        " ltd",
        " ltd.",
        " plc",
        " limited",
        " corporation",
        " corp",
        " corp.",
        " company",
        " co",
        " co.",
        " llc",
        " lp",
        " s.a.",
        " s.a",
    ]:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    # Collapse whitespace
    return " ".join(n.split()).strip()


def get_deals_from_mongodb() -> List[Dict[str, Any]]:
    """
    Fetch all deals from MongoDB.

    We intentionally do NOT filter on any 'canada'‑specific field because this
    script is read‑only and only needs company names for matching.
    """
    try:
        collection = get_deals_collection()
        if collection is None:
            logger.warning(
                "MongoDB connection not available. Deals collection not accessible.")
            return []

        # Only fetch deals where deal_status is Open, Unknown, null, or not set
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        all_deals = list(collection.find(status_filter))
        for d in all_deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])
                d.pop("_id", None)

        logger.info("Fetched %d deals from MongoDB", len(all_deals))
        return all_deals
    except Exception as e:
        logger.exception("Error fetching deals from MongoDB: %s", e)
        return []


def load_deals() -> List[Dict[str, Any]]:
    """Load deals from MongoDB and build a normalized index for fast matching."""
    global deals, normalized_deals_index

    deals = get_deals_from_mongodb()
    logger.info("Loaded %d deals from MongoDB", len(deals))
    deal_ids = [d.get("deal_id") for d in deals if d.get("deal_id")]
    logger.info("Deal IDs: %s", deal_ids)

    normalized_deals_index = []
    for d in deals:
        acquirer = d.get("acquirer") or d.get("acquire_name", "")
        target = d.get("target") or d.get("target_name", "")
        target_aliases = d.get("target_aliases") or []
        parent_aliases = d.get("parent_aliases") or []

        entry: Dict[str, Any] = {
            "deal": d,
            "acquirer_norm": normalize_company(acquirer) if acquirer else "",
            "target_norm": normalize_company(target) if target else "",
            "aliases_norm": [],
        }

        # Flatten aliases into (normalized_name, role) tuples for traceability
        for alias in target_aliases:
            if isinstance(alias, str) and alias.strip():
                entry["aliases_norm"].append(
                    (normalize_company(alias), "target_alias")
                )
        for alias in parent_aliases:
            if isinstance(alias, str) and alias.strip():
                entry["aliases_norm"].append(
                    (normalize_company(alias), "parent_alias")
                )

        normalized_deals_index.append(entry)

    logger.info("Built normalized index for %d deals",
                len(normalized_deals_index))
    return deals


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
        logger.info("Fetching Competition Bureau report: %s", url)
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        logger.info("Fetched HTML (%d bytes)", len(resp.text))
        return resp.text
    except requests.RequestException as e:
        logger.error("Error fetching report page: %s", e)
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
    Extract merger rows from the main `.table-responsive` table.

    Columns (expected):
    - Parties to the Transaction
    - Opened Date
    - Concluded Date
    - Industry (NAICS *)
    - Outcome**
    """
    soup = BeautifulSoup(html_content, "html.parser")

    table = None
    # Find the first table under a .table-responsive container that has the expected header
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
        logger.warning(
            "Could not locate merger reviews table (.table-responsive)")
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
            logger.warning("Error parsing table row: %s", e)
            continue

    logger.info("Parsed %d merger rows from table", len(data_rows))
    return data_rows


def match_parties_to_deal(parties_text: str) -> Optional[Tuple[Dict[str, Any], str, str]]:
    """
    Match the 'Parties to the Transaction' text with deals from MongoDB using LLM.

    Matching is **only** based on party names (acquirer/target and aliases),
    *not* on any deal_name field.

    Returns:
        (deal_dict, matched_company_name, matched_role) or None
        where matched_role ∈ {"acquirer", "target"}.
    """
    if not deals or not parties_text:
        return None

    # Build deals text including aliases, similar to other scrapers
    lines: List[str] = []
    for d in deals:
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {target} | Acquirer: {acquirer}"

        target_aliases = d.get("target_aliases", []) or []
        parent_aliases = d.get("parent_aliases", []) or []
        if target_aliases:
            line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
        if parent_aliases:
            line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
        lines.append(line)

    deals_text = "\n".join(lines)

    prompt = f"""You are an expert M&A deal matcher. Determine if ANY company mentioned in the Competition Bureau Canada merger "Parties to the Transaction" string appears in our deals database.

DEALS DATABASE:
{deals_text}

PARTIES STRING:
{parties_text}

INSTRUCTIONS:
1. Extract ALL company names from the parties string.
2. Check if ANY of these names appears as either Target OR Acquirer in the deals database.
3. When matching, also consider target_aliases and parent_aliases – if the title matches an alias, treat it as a match for that deal.
4. Consider variations, abbreviations, and partial matches (e.g., "Example Corp." matches "Example Corporation").
5. Match on a SINGLE company name – you don't need both sides of the deal to match.

RESPONSE FORMAT:
- If you find ANY match, respond EXACTLY:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|Example Corp.|acquirer

- If NO match, respond with:
  None"""

    try:
        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert M&A deal matcher. Respond only with 'Match: DEAL_ID|COMPANY|target|acquirer' or 'None'.",
                },
                {"role": "user", "content": prompt},
            ]
        )

        # print(f"prompt: {prompt}")
        logger.info("LLM prompt: %s", prompt)
        logger.debug("LLM response: %s",
                     res.choices[0].message.content.strip())
        content = res.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("LLM match error: %s", e)
        return None

    if not content or content.strip().lower().startswith("none"):
        return None

    m = re.search(
        r"Match:\s*([^|]+)\|([^|]+)\|(target|acquirer)",
        content,
        re.IGNORECASE,
    )
    if not m:
        return None

    deal_id = m.group(1).strip()
    matched_company = m.group(2).strip()
    matched_role = m.group(3).strip().lower()

    # Find the deal by deal_id returned from LLM
    for d in deals:
        if d.get("deal_id") == deal_id:
            return d, matched_company, matched_role

    logger.warning("LLM returned unknown deal_id '%s'", deal_id)
    return None


def generate_canada_case_email_html(case_info: Dict[str, Any], deal_match: Dict[str, Any]) -> Tuple[str, str]:
    """Generate HTML email for matched Canada Competition Bureau case."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    parties = case_info.get("parties", "N/A")
    opened_date = case_info.get("opened_date", "N/A")
    concluded_date = case_info.get("concluded_date", "N/A")
    industry = case_info.get("industry", "N/A")
    outcome = case_info.get("outcome", "N/A")
    matched_company = case_info.get("matched_company", "N/A")
    matched_role = case_info.get("matched_role", "N/A")

    subject = f"FRMD: Canada Competition Bureau (New) – {target} / {acquirer}"

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Canada Competition Bureau Match</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:24px;">

<div style="background:#e0f2fe;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid #0284c7;">
<div style="font-size:14px;font-weight:700;color:#0369a1;">Matched Deal</div>
<div style="font-size:14px;color:#0c4a6e;">Acquirer: {escape_html(acquirer)} | Target: {escape_html(target)}</div>
<a href="{REPORT_URL}" target="_blank" style="display:inline-block;margin-top:8px;color:#0284c7;font-weight:700;">View Competition Bureau Report →</a>
</div>

<h2 style="font-size:18px;margin:0 0 12px 0;">Merger Review</h2>

<h3 style="font-size:16px;margin:20px 0 10px 0;">Case Details</h3>
<div style="background:#f8fafc;border-radius:6px;padding:14px;">
<table style="width:100%;border-collapse:collapse;">
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Parties to the Transaction</td><td style="padding:6px 0;">{escape_html(parties)}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Opened Date</td><td>{escape_html(opened_date)}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Concluded Date</td><td>{escape_html(concluded_date)}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Industry (NAICS)</td><td>{escape_html(industry)}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Outcome</td><td>{escape_html(outcome)}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Matched Company</td><td>{escape_html(matched_company)} ({escape_html(matched_role)})</td></tr>
</table>
</div>

</div></body></html>"""
    return subject, html


def generate_unmatched_canada_usa_email_html(case_info: Dict[str, Any]) -> Tuple[str, str]:
    """Generate HTML email for unmatched USA-related Canada Competition Bureau case."""
    parties = case_info.get("parties", "N/A")
    opened_date = case_info.get("opened_date", "N/A")
    concluded_date = case_info.get("concluded_date", "N/A")
    industry = case_info.get("industry", "N/A")
    outcome = case_info.get("outcome", "N/A")

    subject = f"FRUD: Canada Competition Bureau (USA-Related)"

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>USA-Related Canada Competition Bureau Case</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">

<div style="background:#dbeafe;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid #3b82f6;">
<div style="font-size:16px;font-weight:700;color:#1e40af;">🇺🇸 USA-Related Canada Competition Bureau Case</div>
<div style="font-size:14px;color:#1e3a8a;margin-top:6px;">This merger review appears to involve USA-related companies.</div>
</div>

<div style="font-size:18px;font-weight:700;margin-bottom:8px;">{escape_html(parties)}</div>
<div style="font-size:14px;color:#64748b;">Opened: {escape_html(opened_date)} | Concluded: {escape_html(concluded_date)} | Industry: {escape_html(industry)} | Outcome: {escape_html(outcome)}</div>

<div style="margin-top:20px;">
<a href="{REPORT_URL}" target="_blank" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:700;">View Competition Bureau Report →</a>
</div>

</div></body></html>"""
    return subject, html


def send_canada_case_email_via_webhook(case_info: Dict[str, Any], deal_match: Dict[str, Any]) -> bool:
    """Send matched case email via n8n webhook."""
    try:
        subject, html_email = generate_canada_case_email_html(
            case_info, deal_match)
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
        )

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": deal_match.get("deal_id", "N/A"),
            "target": deal_match.get("target") or deal_match.get("target_name", "N/A"),
            "acquirer": deal_match.get("acquirer") or deal_match.get("acquire_name", "N/A"),
            "parties": case_info.get("parties", "N/A"),
            "opened_date": case_info.get("opened_date", "N/A"),
            "concluded_date": case_info.get("concluded_date", "N/A"),
            "industry": case_info.get("industry", "N/A"),
            "outcome": case_info.get("outcome", "N/A"),
            "matched_company": case_info.get("matched_company", "N/A"),
            "matched_role": case_info.get("matched_role", "N/A"),
            "source": "canada_competition_bureau",
        }
        response = requests.post(
            N8N_WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=30
        )
        response.raise_for_status()
        logger.info("Email sent via webhook (%s)", response.status_code)
        return True
    except Exception as e:
        logger.warning("Error sending email via webhook: %s", e)
        return False


def send_unmatched_canada_usa_email_via_webhook(case_info: Dict[str, Any]) -> bool:
    """Send USA-related unmatched case email via webhook."""
    try:
        subject, html_email = generate_unmatched_canada_usa_email_html(
            case_info)
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
        )

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": "N/A",
            "target": "N/A",
            "acquirer": "N/A",
            "parties": case_info.get("parties", "N/A"),
            "opened_date": case_info.get("opened_date", "N/A"),
            "concluded_date": case_info.get("concluded_date", "N/A"),
            "industry": case_info.get("industry", "N/A"),
            "outcome": case_info.get("outcome", "N/A"),
            "usa_related": True,
            "is_unmatched": True,
            "source": "canada_competition_bureau",
        }
        response = requests.post(
            N8N_WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=30
        )
        response.raise_for_status()
        logger.info("USA-related email sent via webhook (%s)",
                    response.status_code)
        return True
    except Exception as e:
        logger.warning("Error sending USA email via webhook: %s", e)
        return False


def save_canada_case_to_deal(deal_match: Dict[str, Any], case_info: Dict[str, Any]) -> bool:
    """Save matched Canada Competition Bureau case to deal under 'canada_competition_bureau_cases' array and send email."""
    try:
        if not is_connected():
            logger.warning("MongoDB not available, skipping save")
            return False

        collection = get_deals_collection()
        if collection is None:
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
            or_conditions = []
            if acquirer:
                or_conditions.extend(
                    [{"acquirer": acquirer}, {"acquire_name": acquirer}])
            if target:
                or_conditions.extend(
                    [{"target": target}, {"target_name": target}])
            if or_conditions:
                query = {"$or": or_conditions}

        if not query:
            logger.warning("Cannot identify deal for MongoDB save")
            return False

        # Use parties + opened_date as unique identifier to avoid duplicates
        parties = case_info.get("parties", "")
        opened_date = case_info.get("opened_date", "")
        unique_key = f"{parties}|{opened_date}"

        existing = collection.find_one(query)
        if existing and existing.get("canada_competition_bureau_cases"):
            for c in existing["canada_competition_bureau_cases"]:
                existing_parties = c.get("parties", "")
                existing_opened = c.get("opened_date", "")
                if f"{existing_parties}|{existing_opened}" == unique_key:
                    logger.info("Case already in deal, skipping save")
                    return False

        update_result = collection.update_one(
            query, {"$push": {"canada_competition_bureau_cases": case_info}})

        if update_result.modified_count > 0:
            logger.info(
                "Saved Canada Competition Bureau case to deal (canada_competition_bureau_cases)")
            try:
                logger.info("Sending email notification...")
                send_canada_case_email_via_webhook(case_info, deal_match)
            except Exception as e:
                logger.warning("Email error: %s", e)
            return True
        if update_result.matched_count > 0:
            return True
        logger.warning("Deal not found in MongoDB")
        return False
    except Exception as e:
        logger.exception("Error saving to MongoDB: %s", e)
        return False


def main() -> None:
    logger.info("=" * 80)
    logger.info("Competition Bureau Canada – Merger Reviews Scraper")
    logger.info("=" * 80)

    # Initialize MongoDB (read-only usage)
    ok, msg = init_mongodb_connection(ENV_PATH)
    if ok:
        logger.info("%s", msg)
        load_deals()
    else:
        logger.warning("%s", msg)
        logger.warning(
            "Proceeding without MongoDB deal matching (USA-related check still works).")

    html = fetch_report_html(REPORT_URL)
    if not html:
        logger.error("No HTML fetched. Exiting.")
        return

    all_rows = parse_merger_table(html)
    if not all_rows:
        logger.warning("No merger rows parsed from table. Exiting.")
        return

    # Filter by CUTOFF_DATE (Opened Date) and build output entries
    output_entries: List[Dict[str, Any]] = []
    cutoff_date_only = CUTOFF_DATE.date() if hasattr(
        CUTOFF_DATE, "date") else CUTOFF_DATE
    logger.info("Applying cutoff on Opened Date = %s",
                cutoff_date_only.isoformat())

    for idx, row in enumerate(all_rows, start=1):
        opened_dt = row.get("opened_date_parsed")

        # Safety: if we don't have a parsed date, drop the record
        if opened_dt is None:
            continue

        try:
            from datetime import date as _date_type, datetime as _datetime_type

            if isinstance(opened_dt, _datetime_type):
                d = opened_dt.date()
            elif isinstance(opened_dt, _date_type):
                d = opened_dt
            else:
                # Unknown type – skip this record
                continue

            # Only consider rows where opened date equals cutoff date
            if d != cutoff_date_only:
                continue
        except Exception:
            # On any error interpreting the date, drop the record
            continue

        logger.info("Row #%d - Parties: %s", idx, row["parties"])
        logger.info("   Opened: %s | Concluded: %s",
                    row["opened_date"], row["concluded_date"])

        matched_deal: Optional[Dict[str, Any]] = None
        matched_company = ""
        matched_role = ""

        if deals:
            match_result = match_parties_to_deal(row["parties"])
            if match_result:
                matched_deal, matched_company, matched_role = match_result
                acq = matched_deal.get("acquirer") or matched_deal.get(
                    "acquire_name", "N/A")
                tgt = matched_deal.get("target") or matched_deal.get(
                    "target_name", "N/A")
                logger.info(
                    "Matched deal: %s / %s (company='%s', role=%s)",
                    acq, tgt, matched_company, matched_role,
                )

        if matched_deal:
            case_info: Dict[str, Any] = {
                "parties": row["parties"],
                "opened_date": row["opened_date"],
                "concluded_date": row["concluded_date"],
                "industry": row["industry"],
                "outcome": row["outcome"],
                "opened_date_parsed": row["opened_date_parsed"],
                "matched_company": matched_company,
                "matched_role": matched_role,
            }

            # Save to MongoDB and send email
            if save_canada_case_to_deal(matched_deal, case_info):
                entry: Dict[str, Any] = {
                    "parties": row["parties"],
                    "opened_date": row["opened_date"],
                    "concluded_date": row["concluded_date"],
                    "industry": row["industry"],
                    "outcome": row["outcome"],
                    "opened_date_parsed": row["opened_date_parsed"],
                    "matched": True,
                    "usa_related": False,
                    "matched_deal": {
                        "deal_id": matched_deal.get("deal_id"),
                        "acquirer": matched_deal.get("acquirer")
                        or matched_deal.get("acquire_name", ""),
                        "target": matched_deal.get("target")
                        or matched_deal.get("target_name", ""),
                        "matched_company": matched_company,
                        "matched_role": matched_role,
                    },
                }
                output_entries.append(entry)
        else:
            logger.info("No deal match found. Checking if USA-related...")
            try:
                # Reuse generic USA relation checker with a Canada‑specific case_type
                details_for_llm = (
                    f"Parties: {row['parties']}\n"
                    f"Industry (NAICS): {row['industry']}\n"
                    f"Outcome: {row['outcome']}\n"
                    f"Opened Date: {row['opened_date']}\n"
                    f"Concluded Date: {row['concluded_date']}"
                )
                is_usa = verify_usa_relation(
                    company_details=details_for_llm,
                    case_type="CANADA",
                )
            except Exception as e:
                logger.warning("USA relation check error: %s", e)
                is_usa = False

            if is_usa:
                logger.info(
                    "USA-related case detected (no deal match). Sending email and adding to JSON.")
                case_info_usa = {
                    "parties": row["parties"],
                    "opened_date": row["opened_date"],
                    "concluded_date": row["concluded_date"],
                    "industry": row["industry"],
                    "outcome": row["outcome"],
                    "opened_date_parsed": row["opened_date_parsed"],
                }
                # Send email for USA-related unmatched case
                send_unmatched_canada_usa_email_via_webhook(case_info_usa)
                entry = {
                    "parties": row["parties"],
                    "opened_date": row["opened_date"],
                    "concluded_date": row["concluded_date"],
                    "industry": row["industry"],
                    "outcome": row["outcome"],
                    "opened_date_parsed": row["opened_date_parsed"],
                    "matched": False,
                    "usa_related": True,
                }
                output_entries.append(entry)
            else:
                logger.info("Not USA-related – skipping row.")

    if not output_entries:
        logger.info("No rows to save after cutoff/match/USA filters. Done.")
        return

    # Sort by opened_date_parsed descending (newest first)
    output_entries.sort(
        key=lambda x: x.get("opened_date_parsed") or datetime.min,
        reverse=True,
    )

    # Remove non-JSON-serializable datetime objects or convert to ISO strings
    for e in output_entries:
        if isinstance(e.get("opened_date_parsed"), datetime):
            e["opened_date_parsed"] = e["opened_date_parsed"].isoformat()

    logger.info("Saving %d entries to %s", len(output_entries), OUTPUT_PATH)
    try:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output_entries, f, indent=2, ensure_ascii=False)
        logger.info("JSON saved successfully.")
    except Exception as e:
        logger.warning("Error saving JSON: %s", e)

    logger.info("Done!")
    if is_connected():
        logger.info(
            "Matched cases saved to MongoDB deals (canada_competition_bureau_cases)")
    logger.info("JSON backup → %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
