"""
Canada Competition Bureau – Case Update Monitor
================================================

Monitors deals that have 'canada_competition_bureau_cases' in MongoDB.
Fetches the current Competition Bureau report HTML, finds the matching row
for each stored case (by parties + opened_date), and compares:
  - concluded_date
  - industry
  - outcome

If any change is detected, generates an HTML email showing old vs new,
sends it via n8n webhook, and updates the case in the database.
"""

import os
from datetime import datetime
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bson import ObjectId
from dotenv import load_dotenv

from competition_bureau_canada_mergers import (
    REPORT_URL,
    fetch_report_html,
    parse_merger_table,
)
from mongodb_connection import (
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)

load_dotenv(".env")

ENV_PATH = ".env"
HTML_OUTPUT_DIR = "canada_competition_bureau_updates"


def get_deals_with_canada_cases() -> List[Dict[str, Any]]:
    """Fetch deals from MongoDB that have 'canada_competition_bureau_cases' node, limited to active/open deals."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available.")
            return []

        # Only include deals whose deal_status is Open, Unknown, null, or not set
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        query = {
            "$and": [
                status_filter,
                {
                    "canada_competition_bureau_cases": {
                        "$exists": True,
                        "$ne": [],
                        "$type": "array",
                    }
                },
            ]
        }
        all_deals = list(collection.find(query))

        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        print(
            f"✅ Fetched {len(all_deals)} deals with canada_competition_bureau_cases from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        return []


def _case_key(parties: str, opened_date: str) -> str:
    """Unique key for matching stored case to fresh row."""
    return f"{ (parties or '').strip() }|{ (opened_date or '').strip() }"


def build_fresh_lookup(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build a lookup from (parties|opened_date) to row data for comparison."""
    lookup: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        parties = row.get("parties", "")
        opened = row.get("opened_date", "")
        key = _case_key(parties, opened)
        # Keep only fields we compare (no parsed datetime in comparison)
        lookup[key] = {
            "parties": parties,
            "opened_date": opened,
            "concluded_date": row.get("concluded_date", ""),
            "industry": row.get("industry", ""),
            "outcome": row.get("outcome", ""),
        }
    return lookup


def compare_case(old_case: Dict[str, Any], new_row: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    """
    Compare stored case with fresh row. Returns list of (field_name, old_value, new_value).
    Ignores matched_company, matched_role, opened_date_parsed.
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


def generate_update_html(
    deal: Dict[str, Any],
    old_case: Dict[str, Any],
    new_row: Dict[str, Any],
    differences: List[Tuple[str, Any, Any]],
) -> str:
    """Generate HTML email showing Canada case update (old vs new)."""
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    parties = new_row.get("parties", "N/A")

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

    rows_html = ""
    for field, old_val, new_val in differences:
        label = field_labels.get(field, field)
        rows_html += f"""
<tr>
  <td style="padding:8px 12px;font-weight:600;color:#475569;vertical-align:top;">{escape_html(label)}</td>
  <td style="padding:8px 12px;color:#64748b;text-decoration:line-through;">{_val(old_val)}</td>
  <td style="padding:8px 12px;font-weight:600;color:#0f172a;">{_val(new_val)}</td>
</tr>"""

    changed_names = ", ".join(field_labels.get(f, f)
                              for f, _, _ in differences)

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Canada Competition Bureau – Case Update</title></head>
<body style="margin:0;padding:0;background:#f8fafc;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;">
<div style="max-width:720px;margin:24px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 4px 12px rgba(0,0,0,0.06);">

<div style="background:#fef3c7;border-radius:8px;padding:14px 18px;margin-bottom:20px;border-left:4px solid #f59e0b;">
<div style="font-size:15px;font-weight:700;color:#92400e;">Canada Competition Bureau – Case Update</div>
<div style="font-size:14px;color:#b45309;margin-top:4px;">Changed fields: {escape_html(changed_names)}</div>
</div>

<div style="font-size:14px;color:#475569;margin-bottom:8px;">Matched deal</div>
<div style="font-size:16px;font-weight:700;color:#0f172a;">{escape_html(acquirer)} / {escape_html(target)}</div>

<div style="font-size:14px;color:#475569;margin:16px 0 8px 0;">Parties</div>
<div style="font-size:15px;color:#0f172a;margin-bottom:20px;">{_val(parties)}</div>

<h3 style="font-size:16px;margin:20px 0 10px 0;">Changes</h3>
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

<div style="margin-top:20px;">
<a href="{REPORT_URL}" target="_blank" style="display:inline-block;padding:10px 18px;background:#0284c7;color:#fff;text-decoration:none;border-radius:6px;font-weight:700;">View Competition Bureau Report →</a>
</div>

</div></body></html>"""
    return html


def send_update_email_via_webhook(
    subject: str,
    html_content: str,
    deal_id: str,
    target: str,
    acquirer: str,
    parties: str,
    changed_fields: List[str],
) -> bool:
    """Send case update email via n8n webhook."""
    try:
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
        )
        payload = {
            "subject": subject,
            "html": html_content,
            "deal_id": deal_id,
            "target": target,
            "acquirer": acquirer,
            "parties": parties,
            "changed_fields": changed_fields,
            "source": "canada_competition_bureau_update",
        }
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        print(f"   ✅ Email sent via webhook ({response.status_code})")
        return True
    except Exception as e:
        print(f"   ⚠️ Error sending email via webhook: {e}")
        return False


def save_html_file(parties_snippet: str, html_content: str) -> str:
    """Save HTML to a file for reference."""
    os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum()
                   or c in " -_" else "_" for c in parties_snippet[:50])
    filename = f"canada_update_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(HTML_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"   💾 Saved HTML to: {filepath}")
        return filepath
    except Exception as e:
        print(f"   ❌ Error saving HTML: {e}")
        return ""


def update_case_in_db(deal_id: str, old_case: Dict[str, Any], new_case_data: Dict[str, Any]) -> bool:
    """Update the matching case in the deal's canada_competition_bureau_cases array."""
    try:
        if not is_connected():
            print("   ⚠️ MongoDB connection not available")
            return False

        collection = get_deals_collection()
        if collection is None:
            return False

        key_old = _case_key(old_case.get("parties", ""),
                            old_case.get("opened_date", ""))

        deal_id_obj = ObjectId(deal_id)
        deal = collection.find_one({"_id": deal_id_obj})
        if not deal or "canada_competition_bureau_cases" not in deal:
            print("   ⚠️ Deal not found or has no canada_competition_bureau_cases")
            return False

        # Preserve matched_company and matched_role
        merged = dict(new_case_data)
        if "matched_company" in old_case:
            merged["matched_company"] = old_case["matched_company"]
        if "matched_role" in old_case:
            merged["matched_role"] = old_case["matched_role"]
        # Keep opened_date_parsed if we have it from scraper (optional)
        if "opened_date_parsed" in old_case:
            merged["opened_date_parsed"] = old_case["opened_date_parsed"]

        updated = False
        for i, c in enumerate(deal["canada_competition_bureau_cases"]):
            k = _case_key(c.get("parties", ""), c.get("opened_date", ""))
            if k == key_old:
                deal["canada_competition_bureau_cases"][i] = merged
                updated = True
                break

        if not updated:
            print("   ⚠️ Case not found in deal's canada_competition_bureau_cases")
            return False

        result = collection.update_one(
            {"_id": deal_id_obj},
            {"$set": {
                "canada_competition_bureau_cases": deal["canada_competition_bureau_cases"]}},
        )
        if result.modified_count > 0:
            print("   ✅ Updated case in database")
        return True
    except Exception as e:
        print(f"   ❌ Error updating database: {e}")
        return False


def process_canada_case_updates() -> None:
    """Main: fetch fresh HTML, compare with stored cases, send email and update DB on changes."""
    print("🚀 Canada Competition Bureau – Case Update Monitor\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        return
    print(f"✅ {message}\n")

    print("📥 Fetching current report HTML...")
    html = fetch_report_html(REPORT_URL)
    if not html:
        print("❌ Failed to fetch report HTML. Exiting.")
        return

    fresh_rows = parse_merger_table(html)
    if not fresh_rows:
        print("⚠️ No rows parsed from report. Exiting.")
        return
    print(f"✅ Parsed {len(fresh_rows)} rows from report\n")

    fresh_lookup = build_fresh_lookup(fresh_rows)

    print("📊 Loading deals with canada_competition_bureau_cases...")
    deals = get_deals_with_canada_cases()
    if not deals:
        print("⚠️ No deals with canada_competition_bureau_cases found. Exiting.")
        return
    print(f"✅ Found {len(deals)} deals\n")

    total_checked = 0
    total_updated = 0

    for deal_idx, deal in enumerate(deals, 1):
        deal_id = deal.get("deal_id", "")
        cases = deal.get("canada_competition_bureau_cases", [])
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")

        print(
            f"[{deal_idx}/{len(deals)}] Deal {deal_id} ({acquirer} / {target}) – {len(cases)} case(s)")

        for case_idx, old_case in enumerate(cases, 1):
            total_checked += 1
            parties = old_case.get("parties", "")
            opened_date = old_case.get("opened_date", "")
            key = _case_key(parties, opened_date)

            if key not in fresh_lookup:
                print(
                    f"   [{case_idx}] Parties/opened not in current report – skipping")
                continue

            new_row = fresh_lookup[key]
            differences = compare_case(old_case, new_row)

            if not differences:
                print(f"   [{case_idx}] No changes")
                continue

            total_updated += 1
            changed_fields = [f for f, _, _ in differences]
            print(f"   [{case_idx}] 🔄 Changes: {', '.join(changed_fields)}")

            html_content = generate_update_html(
                deal, old_case, new_row, differences)
            save_html_file(parties[:50], html_content)

            subject = f"FRMD: Canada Competition Bureau (Updated) – {target} / {acquirer}"
            send_update_email_via_webhook(
                subject,
                html_content,
                deal_id,
                target,
                acquirer,
                parties,
                changed_fields,
            )

            # Merge new row into case for DB (preserve matched_company, matched_role)
            new_case_data = {
                "parties": new_row.get("parties", ""),
                "opened_date": new_row.get("opened_date", ""),
                "concluded_date": new_row.get("concluded_date", ""),
                "industry": new_row.get("industry", ""),
                "outcome": new_row.get("outcome", ""),
            }
            update_case_in_db(deal_id, old_case, new_case_data)

    print(f"\n{'='*60}")
    print("📊 Summary:")
    print(f"   Total cases checked: {total_checked}")
    print(f"   Cases with changes: {total_updated}")
    print(f"   HTML files: {HTML_OUTPUT_DIR}/")
    print(f"{'='*60}\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_canada_case_updates()
