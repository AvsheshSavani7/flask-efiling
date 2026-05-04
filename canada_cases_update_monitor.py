"""
Canada Cases Update Monitor
============================

Monitors cases in the 'canada_cases' MongoDB collection for changes.

Flow:
1. Fetch all cases where is_open == True from canada_cases collection
2. Fetch fresh data from Competition Bureau table
3. Compare concluded_date and outcome for each case
4. If changes detected:
   - If deal_id exists: Send update email
   - If deal_id empty: Check USA-related → send email if true, else just update DB
5. Update case in database; set is_open=False if concluded_date and outcome both != "Ongoing"
"""

from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from llm_verification_service import verify_usa_relation
from canada_cases_register import match_case_to_deal
from error_email_service import send_error_email
import os
import sys
import logging
import traceback
from datetime import datetime, timezone, timedelta
from html import escape as escape_html
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

import requests
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI


from log_utils import cleanup_old_logs

load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging — production setup (RotatingFileHandler, IST, env-based settings)
# -----------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "canada_cases_update_monitor"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


logger = logging.getLogger(SCRIPT_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

if not logger.handlers:
    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


def _log_critical_error_and_email(msg: str, context: Optional[Dict[str, Any]] = None):
    """Immediate error email — use ONLY for critical startup / fatal failures."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context,
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


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
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    fields_to_compare = ["concluded_date", "outcome"]

    for field in fields_to_compare:
        old_val = (old_case.get(field) or "").strip(
        ) if old_case.get(field) is not None else ""
        new_val = (new_row.get(field) or "").strip(
        ) if new_row.get(field) is not None else ""
        if old_val != new_val:
            differences.append(
                (field, old_case.get(field), new_row.get(field)))

    return differences


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

        updated = dict(new_case_data)
        if "created_at" in case_doc:
            updated["created_at"] = case_doc["created_at"]

        updated["updated_at"] = utc_now_iso()

        result = collection.update_one({"_id": _id}, {"$set": updated})
        if result.modified_count > 0:
            print("    ✅ Updated case document in canada_cases")
        else:
            print("    ℹ️ No DB changes made (document already up to date)")
        return True
    except Exception as e:
        logger.exception(f"Error updating case document: {e}")
        return False


def process_canada_cases_updates():
    """Main entrypoint for Canada cases update monitoring."""
    run_start = datetime.now()
    error_items: List[Dict[str, Any]] = []
    logger.info("=" * 60)
    logger.info(f"[STEP 1] Starting Canada Cases Update Monitor")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    logger.info(f"[STEP 1.1] Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        _log_critical_error_and_email(
            f"MongoDB connection failed: {message}",
            {"step": "mongodb_connect"},
        )
        return
    logger.info(f"[STEP 1.2] MongoDB: {message}")

    if not is_connected():
        _log_critical_error_and_email(f"[STEP 1.3] MongoDB not connected. Exiting.", {
                                      "step": "mongodb_connect"})
        return

    cases_collection = get_canada_cases_collection()
    if cases_collection is None:
        _log_critical_error_and_email(
            f"[STEP 1.4] Could not access 'canada_cases' collection. Exiting.",
            {"step": "get_collection"},
        )
        return

    deals_collection = get_deals_collection()

    cursor = cases_collection.find({"is_open": True})
    cases = list(cursor)
    if not cases:
        logger.warning(
            f"[STEP 1.5] No cases with is_open=True found in canada_cases collection.")
        return

    logger.info(
        f"[STEP 1.6] Found {len(cases)} open cases (is_open=True) in canada_cases collection")

    logger.info(f"[STEP 1.7] Existing cases: {cases}")
    html = fetch_report_html(REPORT_URL)
    logger.info(f"[STEP 1.7] HTML: {html}")
    if not html:
        logger.error("Failed to fetch report HTML. Exiting.")
        return

    fresh_rows = parse_merger_table(html)
    logger.info(f"[STEP 1.8] Fresh rows: {fresh_rows}")
    if not fresh_rows:
        logger.warning(f"[STEP 1.9] No rows parsed from report. Exiting.")
        return

    fresh_lookup = build_fresh_lookup(fresh_rows)
    logger.info(f"[STEP 1.10] Fresh lookup: {fresh_lookup}")
    total_checked = 0
    total_changed = 0

    for idx, case_doc in enumerate(cases, 1):
        total_checked += 1
        parties = case_doc.get("parties", "")
        opened_date = case_doc.get("opened_date", "")
        key = f"{parties.strip()}|{opened_date.strip()}"

        logger.info(
            f"[STEP 1.11] [{idx}/{len(cases)}] Checking case: {parties[:60]}...")

        if key not in fresh_lookup:
            logger.warning(
                f"[STEP 1.12] Case not found in current report; skipping")
            continue

        new_row = fresh_lookup[key]
        differences = detect_changes(case_doc, new_row)

        if not differences:
            logger.info(f"[STEP 1.13] No changes detected")
            continue

        total_changed += 1
        changed_fields = [f for f, _, _ in differences]
        logger.info(
            f"[STEP 1.14] Changes detected: {', '.join(changed_fields)}")

        deal_id = case_doc.get("deal_id")
        deal = None
        new_case_data = dict(new_row)

        if deal_id:
            logger.info(
                f"[STEP 1.15] Case already linked to deal_id={deal_id}")
            if deals_collection is not None:
                try:
                    deal = deals_collection.find_one(
                        {"_id": ObjectId(deal_id)})
                except Exception as e:
                    logger.exception(f"[STEP 1.16] Could not fetch deal: {e}")
                    error_items.append(
                        {"parties": parties[:80], "error": str(e), "step": "fetch_linked_deal"})

            send_update_email(case_doc, new_row, deal, differences)
            new_case_data["deal_id"] = deal_id
        else:
            logger.info(
                f"[STEP 1.17] No deal_id found; attempting LLM deal match...")
            matched_deal_id = match_case_to_deal(parties)

            if matched_deal_id:
                logger.info(
                    f"[STEP 1.18] LLM matched case to deal_id={matched_deal_id}")
                if deals_collection is not None:
                    try:
                        deal = deals_collection.find_one(
                            {"_id": ObjectId(matched_deal_id)})
                    except Exception as e:
                        logger.exception(
                            f"[STEP 1.19] Could not fetch matched deal: {e}")
                        error_items.append(
                            {"parties": parties[:80], "error": str(e), "step": "fetch_matched_deal"})

                send_update_email(case_doc, new_row, deal, differences)
                new_case_data["deal_id"] = matched_deal_id
            else:
                logger.info("  No deal match; checking if USA-related...")
                try:
                    details_for_llm = (
                        f"Parties: {parties}\n"
                        f"Industry (NAICS): {case_doc.get('industry', '')}\n"
                        f"Outcome: {new_row.get('outcome', '')}\n"
                        f"Opened Date: {opened_date}\n"
                        f"Concluded Date: {new_row.get('concluded_date', '')}"
                    )
                    is_usa = verify_usa_relation(
                        company_details=details_for_llm,
                        case_type="CANADA",
                    )
                    logger.info(
                        f"[STEP 1.20] details_for_llm: {details_for_llm}")
                except Exception as e:
                    logger.exception(
                        f"[STEP 1.21] USA relation check error: {e}")
                    error_items.append(
                        {"parties": parties[:80], "error": str(e), "step": "verify_usa_relation"})
                    is_usa = False

                if is_usa:
                    logger.info(
                        f"[STEP 1.22] Case is USA-related; sending update email")
                    send_update_email(case_doc, new_row, None, differences)
                else:
                    logger.info(
                        f"[STEP 1.23] Not USA-related; updating DB only (no email)")

        # Set is_open=False if both concluded_date and outcome are not "Ongoing"
        new_concluded = (new_row.get("concluded_date") or "").strip().lower()
        new_outcome = (new_row.get("outcome") or "").strip().lower()
        if new_concluded != "ongoing" and new_outcome != "ongoing":
            new_case_data["is_open"] = False
            logger.info(
                f"[STEP 1.24] Case no longer ongoing; setting is_open=False")

        update_case_document(cases_collection, case_doc, new_case_data)

    if error_items:
        logger.warning(
            f"[STEP 1.25] {len(error_items)} per-case errors collected — sending summary email")
        send_error_email(
            script_name=SCRIPT_NAME,
            error_message=f"[STEP 1.26] {len(error_items)} errors occurred during run",
            context={"error_count": len(
                error_items), "errors": error_items[:20]},
            traceback_str=None,
        )

    elapsed = round((datetime.now() - run_start).total_seconds(), 1)
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"[STEP 1.27] Total cases checked          : {total_checked}")
    logger.info(f"[STEP 1.28] Cases with changes           : {total_changed}")
    logger.info(
        f"[STEP 1.29] Errors encountered           : {len(error_items)}")
    logger.info(f"[STEP 1.30] Total time                   : {elapsed}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        process_canada_cases_updates()
    except Exception as e:
        _log_critical_error_and_email(
            f"Unhandled error in __main__: {e}", {"step": "__main__"})
        raise
