"""
Canada Cases Update Monitor
============================

Monitors cases in the 'canada_cases' MongoDB collection for changes.

Flow:
1. Fetch all cases from canada_cases collection
2. Fetch fresh data from Competition Bureau table
3. Compare each case with fresh data
4. If changes detected:
   - If deal_id exists: Send update email (no LLM call)
   - If deal_id empty: Try LLM match, then send appropriate email
5. Update case in database with new values
"""

from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
import os
import sys
import logging
import builtins
from datetime import datetime
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------------
LOGGER_NAME = "canada_cases_update_monitor"
LOG_FILE = "canada_cases_update_monitor.log"

logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    """Replacement for print that also logs to a file via the module logger."""
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
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    # Kaushal/Josh/Avs only
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
)


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_canada_cases_collection():
    """Get the 'canada_cases' collection from MongoDB."""
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


def parse_merger_table(html_content: str) -> List[Dict[str, Any]]:
    """
    Extract merger rows from the main .table-responsive table.
    Reuses the same logic as canada_cases_register.py
    """
    from bs4 import BeautifulSoup

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

            row_data: Dict[str, Any] = {
                "parties": parties_text,
                "opened_date": opened_str,
                "concluded_date": concluded_str,
                "industry": industry_str,
                "outcome": outcome_str,
            }
            data_rows.append(row_data)
        except Exception as e:
            print(f"⚠️ Error parsing table row: {e}", level="warning")
            continue

    print(f"✅ Parsed {len(data_rows)} merger rows from table")
    return data_rows


def build_fresh_lookup(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build a lookup from (parties|opened_date) to row data for comparison."""
    lookup: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        parties = (row.get("parties") or "").strip()
        opened = (row.get("opened_date") or "").strip()
        key = f"{parties}|{opened}"
        lookup[key] = {
            "parties": row.get("parties", ""),
            "opened_date": row.get("opened_date", ""),
            "concluded_date": row.get("concluded_date", ""),
            "industry": row.get("industry", ""),
            "outcome": row.get("outcome", ""),
        }
    return lookup


def detect_changes(
    old_case: Dict[str, Any], new_row: Dict[str, Any]
) -> List[Tuple[str, Any, Any]]:
    """
    Compare stored case with fresh row.
    Returns list of (field_name, old_value, new_value).
    """
    differences: List[Tuple[str, Any, Any]] = []
    fields_to_compare = ["parties", "opened_date",
                         "concluded_date", "industry", "outcome"]

    for field in fields_to_compare:
        old_val = (old_case.get(field) or "").strip(
        ) if old_case.get(field) is not None else ""
        new_val = (new_row.get(field) or "").strip(
        ) if new_row.get(field) is not None else ""
        if old_val != new_val:
            differences.append(
                (field, old_case.get(field), new_row.get(field)))

    return differences


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
        print(f"    🤖 LLM match response: {content}")

        if not content.lower().startswith("match"):
            return None

        try:
            _prefix, deal_id_raw = content.split(":", 1)
            deal_id = deal_id_raw.strip()
            return deal_id or None
        except Exception:
            return None
    except Exception as e:
        print(f"    ⚠️ LLM match error: {e}", level="warning")
        return None


def generate_update_email_html(
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any]],
) -> str:
    """Generate rich HTML email for case update (similar to ACCC style)."""
    parties = new_case.get("parties", old_case.get("parties", "N/A"))
    opened_date = new_case.get(
        "opened_date", old_case.get("opened_date", "N/A"))

    field_labels = {
        "parties": "Parties to the Transaction",
        "opened_date": "Opened Date",
        "concluded_date": "Concluded Date",
        "industry": "Industry (NAICS)",
        "outcome": "Outcome",
    }

    def _val(v: Any) -> str:
        if v is None:
            return "—"
        return escape_html(str(v).strip())

    # Build changes table
    rows_html = ""
    for field, old_val, new_val in changes:
        label = field_labels.get(field, field)
        rows_html += f"""
<tr>
  <td style="padding:8px 12px;font-weight:600;color:#475569;vertical-align:top;">{escape_html(label)}</td>
  <td style="padding:8px 12px;color:#64748b;text-decoration:line-through;">{_val(old_val)}</td>
  <td style="padding:8px 12px;font-weight:600;color:#0f172a;">{_val(new_val)}</td>
</tr>"""

    changed_names = ", ".join(field_labels.get(f, f) for f, _, _ in changes)

    # Deal info banner
    deal_banner = ""
    if deal:
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        deal_id = str(deal.get("_id")) if deal.get("_id") else "N/A"
        deal_banner = f"""
<!-- Deal Match Info Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid:#2563eb;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;">
    <span style="font-weight:700;">Acquirer:</span> {escape_html(acquirer)} <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Target:</span> {escape_html(target)} <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Deal ID:</span> {escape_html(deal_id)}
  </div>
  <div style="margin-top:10px;">
    <a href="{REPORT_URL}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Competition Bureau Report →</a>
  </div>
</div>"""
    else:
        # USA-related banner
        deal_banner = f"""
<!-- USA-Related Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid:#3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">🇺🇸 USA-Related Case</div>
  <div style="font-size:14px;color:#1e3a8a;">
    This merger review appears to involve USA-related parties or markets.
  </div>
  <div style="margin-top:10px;">
    <a href="{REPORT_URL}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Competition Bureau Report →</a>
  </div>
</div>"""

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Canada Competition Bureau - Case Update</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- Update Banner -->
<div style="background:#fef3c7;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #f59e0b;">
  <div style="font-size:16px;font-weight:800;color:#92400e;margin-bottom:8px;">⚠️ Canada Competition Bureau Case Updated</div>
  <div style="font-size:14px;color:#b45309;">
    This case has been updated. Changed fields: {escape_html(changed_names)}
  </div>
</div>

{deal_banner}

<!-- Case Info -->
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;margin-bottom:20px;">
  <div style="font-size:16px;font-weight:800;margin-bottom:12px;">Case Information</div>
  <div style="display:grid;grid-template-columns:180px 1fr;row-gap:10px;column-gap:18px;">
    <div style="font-weight:700;">Parties:</div>
    <div>{_val(parties)}</div>
    <div style="font-weight:700;">Opened Date:</div>
    <div>{_val(opened_date)}</div>
  </div>
</div>

<!-- Changes -->
<div style="font-size:18px;font-weight:800;margin-bottom:12px;">Changes</div>
<table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;border-radius:8px;">
<thead>
<tr style="background:#f1f5f9;">
  <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Field</th>
  <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Previous</th>
  <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Current</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>

</div>
</body>
</html>"""
    return html


def send_update_email(
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any]],
) -> bool:
    """Send update email via n8n webhook."""
    try:
        html = generate_update_email_html(old_case, new_case, deal, changes)
        parties = old_case.get("parties", "N/A")

        if deal:
            target = deal.get("target") or deal.get("target_name", "N/A")
            acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
            subject = f"[FRMD] Canada Competition Bureau (Updated) – {target} / {acquirer}"
            deal_id = str(deal.get("_id")) if deal.get("_id") else None
        else:
            subject = f"[FRUD] Canada Competition Bureau (USA-Related Update)"
            deal_id = None

        payload = {
            "subject": subject,
            "html": html,
            "parties": parties,
            "changed_fields": [f for f, _, _ in changes],
            "deal_id": deal_id,
            "source": "canada_competition_bureau_update",
        }

        print(f"    📤 Sending email via n8n webhook")
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"    ✅ Email sent successfully! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"    ⚠️ Error sending email: {e}", level="warning")
        return False


def update_case_document(
    collection, case_doc: Dict[str, Any], new_case_data: Dict[str, Any]
) -> bool:
    """Update the case document in canada_cases collection."""
    try:
        _id = case_doc.get("_id")
        if not _id:
            print("    ⚠️ Case document has no _id; cannot update", level="warning")
            return False

        # Merge new data, preserving created_at and usa_related
        updated = dict(new_case_data)
        if "created_at" in case_doc:
            updated["created_at"] = case_doc["created_at"]
        if "usa_related" in case_doc and "usa_related" not in updated:
            updated["usa_related"] = case_doc["usa_related"]

        updated["updated_at"] = utc_now_iso()

        result = collection.update_one({"_id": _id}, {"$set": updated})
        if result.modified_count > 0:
            print("    ✅ Updated case document in canada_cases")
        else:
            print("    ℹ️ No DB changes made (document already up to date)")
        return True
    except Exception as e:
        print(f"    ❌ Error updating case document: {e}", level="error")
        return False


def process_canada_cases_updates():
    """Main entrypoint for Canada cases update monitoring."""
    print("🚀 Starting Canada Cases Update Monitor\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}", level="error")
        return
    print(f"✅ {message}\n")

    if not is_connected():
        print("❌ MongoDB not connected. Exiting.", level="error")
        return

    cases_collection = get_canada_cases_collection()
    if cases_collection is None:
        print("❌ Could not access 'canada_cases' collection. Exiting.", level="error")
        return

    deals_collection = get_deals_collection()

    # Fetch only cases with concluded_date = "Ongoing"
    cursor = cases_collection.find({"concluded_date": "Ongoing"})
    cases = list(cursor)
    if not cases:
        print("⚠️ No cases with concluded_date='Ongoing' found in canada_cases collection.", level="warning")
        return

    print(
        f"📊 Found {len(cases)} cases with concluded_date='Ongoing' in canada_cases collection\n")

    # Fetch fresh data from Competition Bureau
    html = fetch_report_html(REPORT_URL)
    if not html:
        print("❌ Failed to fetch report HTML. Exiting.", level="error")
        return

    fresh_rows = parse_merger_table(html)
    if not fresh_rows:
        print("⚠️ No rows parsed from report. Exiting.", level="warning")
        return

    fresh_lookup = build_fresh_lookup(fresh_rows)

    total_checked = 0
    total_changed = 0

    for idx, case_doc in enumerate(cases, 1):
        total_checked += 1
        parties = case_doc.get("parties", "")
        opened_date = case_doc.get("opened_date", "")
        key = f"{parties.strip()}|{opened_date.strip()}"

        print(f"[{idx}/{len(cases)}] Checking case: {parties[:60]}...")

        if key not in fresh_lookup:
            print("  ⚠️ Case not found in current report; skipping")
            continue

        new_row = fresh_lookup[key]
        differences = detect_changes(case_doc, new_row)

        if not differences:
            print("  ✅ No changes detected")
            continue

        total_changed += 1
        changed_fields = [f for f, _, _ in differences]
        print(f"  🔄 Changes detected: {', '.join(changed_fields)}")

        # Check if deal_id exists
        deal_id = case_doc.get("deal_id")
        deal = None

        if deal_id:
            # Case already linked to deal - no LLM call needed
            print(f"  🔗 Case already linked to deal_id={deal_id}")
            if deals_collection is not None:
                try:
                    deal = deals_collection.find_one(
                        {"_id": ObjectId(deal_id)})
                except Exception as e:
                    print(f"  ⚠️ Could not fetch deal: {e}", level="warning")

            send_update_email(case_doc, new_row, deal, differences)

            # Update case with new data (preserve deal_id)
            new_case_data = dict(new_row)
            new_case_data["deal_id"] = deal_id
            update_case_document(cases_collection, case_doc, new_case_data)
        else:
            # No deal_id - try LLM matching
            print("  🔍 No deal_id found; attempting LLM match...")
            matched_deal_id = match_case_to_deal(parties)

            if matched_deal_id:
                print(f"  🎯 LLM matched case to deal_id={matched_deal_id}")

                if deals_collection is not None:
                    try:
                        deal = deals_collection.find_one(
                            {"_id": ObjectId(matched_deal_id)})
                    except Exception as e:
                        print(
                            f"  ⚠️ Could not fetch deal: {e}", level="warning")

                # Update case with new data + deal_id
                new_case_data = dict(new_row)
                new_case_data["deal_id"] = matched_deal_id

                send_update_email(case_doc, new_row, deal, differences)
                update_case_document(cases_collection, case_doc, new_case_data)
            else:
                # No match - send USA-related update email
                print("  🇺🇸 No deal match; sending USA-related update email")

                send_update_email(case_doc, new_row, None, differences)
                update_case_document(cases_collection, case_doc, new_row)

    print("\n" + "=" * 60)
    print("📊 Summary:")
    print(f"   Total cases checked: {total_checked}")
    print(f"   Cases with changes: {total_changed}")
    print("=" * 60 + "\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_canada_cases_updates()
