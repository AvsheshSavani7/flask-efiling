import json
import os
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
from bson import ObjectId
from mongodb_connection import get_deals_collection, is_connected, init_mongodb_connection

# Load environment variables
load_dotenv(".env")

# Constants
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "accc_case_updates"

# Residential proxy configuration
PROXY_HOST = "108.59.242.138"
PROXY_PORT = 46885
PROXY_USERNAME = "GSenAgrfKhuNWkd"
PROXY_PASSWORD = "8lmVa5yl0pKp9MI"


def get_deals_with_accc_cases() -> List[Dict[str, Any]]:
    """Fetch deals from MongoDB that have 'accc_cases' node, limited to active/open deals."""
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
                {"accc_cases": {"$exists": True, "$ne": [], "$type": "array"}},
            ]
        }
        all_deals = list(collection.find(query))

        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        print(f"✅ Fetched {len(all_deals)} deals with accc_cases from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        return []


def extract_current_detail_page_info(page, url: str) -> Dict[str, Any]:
    """Extract current detailed info from the ACCC acquisition detail page."""
    try:
        print(f"      📄 Fetching detail page: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        current_info = {}

        # Extract acquisition status from top summary panel
        try:
            status_elem = page.query_selector(
                ".field--name-field-acccgov-merger-status .field__item")
            if status_elem:
                current_info["acquisition_status"] = status_elem.inner_text(
                ).strip()
        except Exception as e:
            print(f"      ⚠️ Error extracting acquisition status: {e}")

        # Extract stage
        try:
            stage_elem = page.query_selector(
                ".field--name-field-acquisition-stage .field__item")
            if stage_elem:
                current_info["stage"] = stage_elem.inner_text().strip()
        except Exception as e:
            print(f"      ⚠️ Error extracting stage: {e}")

        # Extract ACCC Determination (Approved, Not opposed, etc.) from Status section
        try:
            determination_elem = page.query_selector(
                ".field--name-field-acccgov-acquisition-deter .field__item")
            if determination_elem:
                current_info["accc_determination"] = determination_elem.inner_text(
                ).strip()
        except Exception as e:
            print(f"      ⚠️ Error extracting ACCC determination: {e}")

        # Extract Determination publication date from Status section
        try:
            pub_date_elem = page.query_selector(
                ".field--name-field-acccgov-pub-reg-end-date .field__item time")
            if pub_date_elem:
                current_info["determination_publication_date"] = pub_date_elem.inner_text(
                ).strip()
        except Exception as e:
            print(
                f"      ⚠️ Error extracting determination publication date: {e}")

        # Extract Decisions and key events
        try:
            events = []
            event_rows = page.query_selector_all(
                ".field--name-field-acccgov-merger-events table tbody tr")

            for row in event_rows:
                event = {}

                # Extract date
                date_elem = row.query_selector(
                    "td.acccgov-timeline__date time")
                if date_elem:
                    event["date"] = date_elem.inner_text().strip()

                # Extract event description (2nd td)
                desc_elem = row.query_selector("td:nth-child(2)")
                if desc_elem:
                    event["description"] = desc_elem.inner_text().strip()

                # Extract attachment link and size if present
                link_elem = row.query_selector(
                    "td.acccgov-timeline__file-link a")
                if link_elem:
                    href = link_elem.get_attribute("href")
                    if href:
                        # Make absolute URL if relative
                        if href.startswith("/"):
                            event["attachment_url"] = f"https://www.accc.gov.au{href}"
                        else:
                            event["attachment_url"] = href

                        # Extract file size from badge
                        size_elem = link_elem.query_selector("span.badge")
                        if size_elem:
                            event["attachment_size"] = size_elem.inner_text().strip()

                if event.get("description"):  # Only add if has description
                    events.append(event)

            if events:
                current_info["decisions_and_events"] = events
                print(f"      📋 Extracted {len(events)} event(s)")
        except Exception as e:
            print(f"      ⚠️ Error extracting decisions and events: {e}")

        return current_info

    except Exception as e:
        print(f"      ❌ Error extracting detail page: {e}")
        return {}


def normalize_value(value: Any) -> Any:
    """Normalize a value for comparison."""
    if isinstance(value, str):
        return value.strip() if value else None
    return value


def detect_changes(old_case: Dict[str, Any], current_info: Dict[str, Any]) -> List[Tuple[str, Any, Any, str]]:
    """
    Detect changes in the monitored fields.

    Returns:
        List of tuples: (field_name, old_value, new_value, change_type)
        change_type: 'new' | 'updated' | 'removed'
    """
    changes = []

    # Fields to monitor
    monitored_fields = [
        ("acquisition_status", "Acquisition status"),
        ("stage", "Stage"),
        ("determination_publication_date", "Determination publication date"),
        ("accc_determination", "ACCC Determination")
    ]

    for field_key, field_label in monitored_fields:
        # Get old value
        old_value = None
        if field_key in old_case:
            old_value = old_case.get(field_key)
        elif field_key in old_case.get("details", {}):
            old_value = old_case.get("details", {}).get(field_key)

        # Get new value from current_info
        new_value = current_info.get(field_key)

        # Normalize both values
        old_normalized = normalize_value(old_value)
        new_normalized = normalize_value(new_value)

        # Determine change type
        if old_normalized is None and new_normalized is not None:
            # New field added
            changes.append(
                (field_label, old_normalized, new_normalized, 'new'))
        elif old_normalized is not None and new_normalized is None:
            # Field removed (rare case)
            changes.append((field_label, old_normalized,
                           new_normalized, 'removed'))
        elif old_normalized != new_normalized and new_normalized is not None:
            # Field updated
            changes.append((field_label, old_normalized,
                           new_normalized, 'updated'))

    # Check for new events in decisions_and_events
    old_events = old_case.get("decisions_and_events", [])
    if not old_events:
        old_events = old_case.get("details", {}).get(
            "decisions_and_events", [])

    new_events = current_info.get("decisions_and_events", [])

    if new_events:
        # Find new events by comparing descriptions and dates
        old_event_keys = {
            f"{e.get('date', '')}|{e.get('description', '')}" for e in old_events}
        new_event_list = []

        for event in new_events:
            event_key = f"{event.get('date', '')}|{event.get('description', '')}"
            if event_key not in old_event_keys:
                new_event_list.append(event)

        if new_event_list:
            # Add as a special change entry
            changes.append(("Decisions and key events",
                           None, new_event_list, 'new'))

    return changes


def format_date(date_str: str) -> str:
    """Format date from various formats to DD.MM.YYYY"""
    if not date_str:
        return "N/A"

    try:
        # Try DD Mon YYYY format (e.g., "30 Jan 2026")
        dt = datetime.strptime(date_str, "%d %b %Y")
        return dt.strftime("%d.%m.%Y")
    except:
        pass

    try:
        # Try YYYY-MM-DD format
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except:
        pass

    return date_str


def generate_accc_update_email_html(case_info: Dict[str, Any], deal_match: Dict[str, Any], changes: List[Tuple[str, Any, Any, str]]) -> str:
    """
    Generate HTML email for ACCC case update notification.

    Args:
        case_info: The ACCC case data dictionary
        deal_match: The matched deal object
        changes: List of (field_name, old_value, new_value, change_type) tuples

    Returns:
        HTML email content
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")

    # Extract case information
    case_number = case_info.get("case_number", "N/A")
    title = case_info.get("title", "N/A")
    acquisition_status = case_info.get("acquisition_status", "N/A")
    case_type = case_info.get("type", "N/A")
    stage = case_info.get("stage", "N/A")
    notification_date = case_info.get("effective_notification_date", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("details", {})

    # Get current values (from the changes or existing data)
    determination_pub_date = case_info.get("determination_publication_date",
                                           details.get("determination_publication_date", ""))
    accc_determination = case_info.get("accc_determination",
                                       details.get("accc_determination", ""))

    # Determine status badge color
    status_color = "#1e1b4b"  # default dark blue
    if "assessment completed" in acquisition_status.lower():
        status_color = "#14b8a6"  # teal
    elif "under assessment" in acquisition_status.lower():
        status_color = "#1e1b4b"
    elif "not opposed" in acquisition_status.lower():
        status_color = "#059669"
    elif "withdrawn" in acquisition_status.lower():
        status_color = "#6b7280"

    # Build change map: field_name -> (change_type, new_value)
    changed_fields = {}
    change_summary = []
    for field_name, old_val, new_val, change_type in changes:
        changed_fields[field_name] = (change_type, new_val)
        change_summary.append(field_name)

    # Build HTML
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ACCC Acquisition Update - {case_number}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- Update Banner -->
<div style="background:#fef2f2;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #ef4444;">
<div style="font-size:16px;font-weight:800;color:#dc2626;margin-bottom:8px;">⚠️ ACCC Case Updated</div>
<div style="font-size:14px;color:#991b1b;">
This case has been updated. Changed fields: {', '.join(change_summary)}
</div>
</div>

<!-- Deal Match Info Banner -->
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
<div style="font-size:14px;color:#1e3a8a;">
<span style="font-weight:700;">Acquirer:</span> {acquirer} <span style="color:#94a3b8;margin:0 8px;">|</span>
<span style="font-weight:700;">Target:</span> {target}
</div>'''

    if detail_url:
        html += f'''
<div style="margin-top:10px;">
<a href="{detail_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View ACCC Case →</a>
</div>'''

    html += f'''
</div>

<!-- Top summary panel -->
<div style="background:#f3f4f6;border-radius:2px;padding:22px 26px;">
<div style="display:grid;grid-template-columns:260px 1fr;row-gap:16px;column-gap:18px;align-items:center;">

<div style="font-weight:700;">Acquisition status:</div>
<div>
<span style="display:inline-block;padding:8px 14px;border-radius:6px;background:{status_color};color:#ffffff;font-weight:800;font-size:14px;">
{acquisition_status}
</span>
</div>

<div style="font-weight:700;">Acquisition case number:</div>
<div>{case_number}</div>

<div style="font-weight:700;">Type:</div>
<div>{case_type}</div>'''

    # Add waiver/notification date based on type
    if "waiver" in case_type.lower():
        html += f'''
<div style="font-weight:700;">Waiver application date:</div>
<div>{notification_date}</div>'''
    else:
        html += f'''
<div style="font-weight:700;">Effective notification date:</div>
<div>{notification_date}</div>'''

    html += '''
</div>
</div>

<!-- Status -->
<div style="margin-top:36px;">
<div style="font-size:22px;font-weight:800;margin-bottom:14px;">Status</div>
<div style="height:1px;background:#e5e7eb;"></div>

<div style="display:grid;grid-template-columns:240px 1fr;row-gap:14px;column-gap:18px;padding-top:18px;">'''

    # Helper function to generate change flag
    def get_change_flag(field_label):
        if field_label in changed_fields:
            change_type, _ = changed_fields[field_label]
            if change_type == 'new':
                return ' <span style="color:#10b981;font-size:0.85em;font-weight:700;margin-left:6px;">(new)</span>'
            elif change_type == 'updated':
                return ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;margin-left:6px;">(updated)</span>'
        return ''

    # Show Stage if has value
    if stage and stage != "N/A":
        stage_flag = get_change_flag("Stage")
        html += f'''
<div>Stage:</div>
<div>{stage}{stage_flag}</div>'''

    # Show ACCC Determination if has value
    if accc_determination and accc_determination != "N/A":
        determination_flag = get_change_flag("ACCC Determination")
        html += f'''
<div>ACCC Determination:</div>
<div>{accc_determination}{determination_flag}</div>'''

    # Show Determination publication date if has value
    if determination_pub_date and determination_pub_date != "N/A":
        pub_date_flag = get_change_flag("Determination publication date")
        html += f'''
<div>Determination publication date:</div>
<div>{determination_pub_date}{pub_date_flag}</div>'''

    html += '''
</div>
</div>'''

    # About the acquisition section (if details exist)
    acquirers = details.get("acquirers", [])
    targets = details.get("targets", [])
    other_parties = details.get("other_parties", [])
    anzsic = details.get("anzsic_codes", "")
    description = details.get("description", "")

    if acquirers or targets or other_parties or anzsic or description:
        html += '''
<!-- About the acquisition -->
<div style="margin-top:34px;">
<div style="font-size:22px;font-weight:800;margin-bottom:14px;">About the acquisition</div>
<div style="height:1px;background:#e5e7eb;"></div>

<div style="display:grid;grid-template-columns:240px 1fr;row-gap:18px;column-gap:18px;padding-top:18px;">'''

        # Acquirers
        if acquirers:
            html += '''
<div>Acquirer(s):</div>
<div>'''
            for i, acq in enumerate(acquirers):
                margin_bottom = "8px" if i < len(acquirers) - 1 else "0"
                html += f'''
<div style="margin-bottom:{margin_bottom};">
<span style="font-weight:800;">{acq.get("name", "N/A")}</span>'''
                if acq.get("registration"):
                    html += f'''
<span style="float:right;">{acq["registration"]}</span>'''
                html += '''
<div style="clear:both;"></div>
</div>'''
            html += '''
</div>'''

        # Targets
        if targets:
            html += '''
<div>Target(s) or Vendor(s):</div>
<div>'''
            for i, tgt in enumerate(targets):
                margin_bottom = "8px" if i < len(targets) - 1 else "0"
                html += f'''
<div style="margin-bottom:{margin_bottom};">
<span style="font-weight:800;">{tgt.get("name", "N/A")}</span>'''
                if tgt.get("registration"):
                    html += f'''
<span style="float:right;">BRN - {tgt["registration"]}</span>'''
                html += '''
<div style="clear:both;"></div>
</div>'''
            html += '''
</div>'''

        # Other parties
        if other_parties:
            html += '''
<div>Other party(ies):</div>
<div>'''
            for i, party in enumerate(other_parties):
                margin_bottom = "8px" if i < len(other_parties) - 1 else "0"
                html += f'''
<div style="margin-bottom:{margin_bottom};">
<span style="font-weight:800;">{party.get("name", "N/A")}</span>'''
                if party.get("registration"):
                    html += f'''
<span style="float:right;">{party["registration"]}</span>'''
                html += '''
<div style="clear:both;"></div>
</div>'''
            html += '''
</div>'''

        # ANZSIC codes
        if anzsic:
            html += f'''
<div>ANZSIC code(s):</div>
<div>{anzsic}</div>'''

        # Description
        if description:
            html += f'''
<div>Description:</div>
<div style="line-height:1.55;">{description}</div>'''

        html += '''
</div>
</div>'''

    # Decisions and key events section (after About the acquisition)
    decisions_events = case_info.get("decisions_and_events", [])
    if not decisions_events:
        decisions_events = details.get("decisions_and_events", [])

    if decisions_events:
        html += '''
<!-- Decisions and key events -->
<div style="margin-top:36px;">
<div style="font-size:22px;font-weight:800;margin-bottom:14px;">Decisions and key events</div>
<div style="height:1px;background:#e5e7eb;"></div>

<div style="padding-top:18px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>'''

        # Check if any events are new
        new_event_list = []
        if "Decisions and key events" in changed_fields:
            _, new_event_list = changed_fields["Decisions and key events"]

        for event in decisions_events:
            event_date = event.get("date", "N/A")
            event_desc = event.get("description", "N/A")
            event_url = event.get("attachment_url", "")
            event_size = event.get("attachment_size", "")

            # Check if this event is new
            is_new_event = any(
                e.get("date") == event_date and e.get(
                    "description") == event_desc
                for e in new_event_list
            )
            new_flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;margin-left:6px;">(new)</span>' if is_new_event else ''

            html += f'''
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px 12px 0;vertical-align:top;width:120px;color:#6b7280;font-size:14px;">{event_date}</td>
<td style="padding:12px 8px;vertical-align:top;font-weight:600;">{event_desc}{new_flag}</td>'''

            if event_url:
                html += f'''
<td style="padding:12px 0 12px 8px;vertical-align:top;text-align:right;width:180px;">
<a href="{event_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-size:14px;">
📄 Attachment'''
                if event_size:
                    html += f''' <span style="color:#6b7280;font-size:12px;">({event_size})</span>'''
                html += '''
</a>
</td>'''
            else:
                html += '<td></td>'

            html += '''
</tr>'''

        html += '''
</tbody>
</table>
</div>
</div>'''

    html += '''

</div>
</body>
</html>'''

    return html


def save_html_file(case_number: str, html_content: str) -> str:
    """Save HTML content to a file."""
    os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)
    safe_case_number = case_number.replace(".", "_").replace("/", "_")
    filename = f"{safe_case_number}_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(HTML_OUTPUT_DIR, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"      💾 Saved HTML to: {filepath}")
        return filepath
    except Exception as e:
        print(f"      ❌ Error saving HTML file: {e}")
        return ""


def send_accc_update_email_via_webhook(case_info: Dict[str, Any], deal_match: Dict[str, Any], html_content: str, changes: List[Tuple[str, Any, Any, str]]) -> bool:
    """
    Send email notification via n8n webhook for ACCC case updates.

    Args:
        case_info: The ACCC case data dictionary
        deal_match: The matched deal object
        html_content: The HTML content to send in email
        changes: List of (field_name, old_value, new_value, change_type) tuples

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Extract deal information
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")
        case_number = case_info.get("case_number", "N/A")

        # Generate email subject
        subject = f"ACCC Case Update – {case_number}: {target} / {acquirer}"
        print(f"      📝 Generated email subject: {subject}")

        with open("accc_update_email.html", "w") as f:
            f.write(html_content)

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL",
            "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6"
        )
        print(f"      📤 Sending email via n8n webhook: {webhook_url}")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_content,
            'deal_id': deal_id,
            'target': target,
            'acquirer': acquirer,
            'case_number': case_number,
            'changed_fields': [c[0] for c in changes],
            'case_url': case_info.get("detail_url", "")
        }

        # Send POST request to n8n webhook
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        response.raise_for_status()

        print(
            f"      ✅ Email sent successfully! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"      ⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"      ⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def update_case_in_db(deal_id: str, case_number: str, updated_case_data: Dict[str, Any]) -> bool:
    """Update the ACCC case in the database."""
    try:
        if not is_connected():
            print("      ⚠️ MongoDB connection not available")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("      ⚠️ Deals collection not available")
            return False

        deal_id_obj = ObjectId(deal_id)
        deal = collection.find_one({"_id": deal_id_obj})
        if not deal or "accc_cases" not in deal:
            print(f"      ⚠️ Deal not found or has no accc_cases")
            return False

        # Find and update the case
        updated = False
        for i, existing_case in enumerate(deal["accc_cases"]):
            existing_case_number = existing_case.get("case_number")
            if existing_case_number == case_number:
                # Update the case with new data while preserving structure
                deal["accc_cases"][i] = updated_case_data
                updated = True
                break

        if not updated:
            print(
                f"      ⚠️ Case {case_number} not found in deal's accc_cases")
            return False

        # Update the database
        update_result = collection.update_one(
            {"_id": deal_id_obj},
            {"$set": {"accc_cases": deal["accc_cases"]}}
        )

        if update_result.modified_count > 0:
            print(f"      ✅ Updated case in database")
            return True
        else:
            print(f"      ℹ️ No DB changes made")
            return True
    except Exception as e:
        print(f"      ❌ Error updating database: {e}")
        import traceback
        traceback.print_exc()
        return False


def process_accc_case_updates():
    """Main function to process ACCC case updates."""
    print("🚀 Starting ACCC Case Update Monitor\n")

    # Initialize MongoDB
    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        return
    print(f"✅ {message}\n")

    # Get deals with accc_cases
    print("📊 Loading deals with accc_cases from MongoDB...")
    deals = get_deals_with_accc_cases()
    if not deals:
        print("⚠️ No deals with accc_cases found. Exiting.")
        return
    print(f"✅ Found {len(deals)} deals with accc_cases\n")

    # Process each deal
    total_cases_checked = 0
    total_cases_updated = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                f"--proxy-server=http://{PROXY_HOST}:{PROXY_PORT}",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            proxy={
                "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
                "username": PROXY_USERNAME,
                "password": PROXY_PASSWORD,
            },
        )
        page = context.new_page()

        for deal_idx, deal in enumerate(deals, 1):
            deal_id = deal.get("deal_id", "")
            accc_cases = deal.get("accc_cases", [])
            acquirer = deal.get("acquire_name", "N/A")
            target = deal.get("target_name", "N/A")

            print(
                f"\n[{deal_idx}/{len(deals)}] Processing deal: {acquirer} / {target}")
            print(f"   Deal ID: {deal_id}")
            print(f"   ACCC cases: {len(accc_cases)}")

            for case_idx, existing_case in enumerate(accc_cases, 1):
                total_cases_checked += 1

                case_number = existing_case.get("case_number")
                case_title = existing_case.get("title", "N/A")
                detail_url = existing_case.get("detail_url")

                if not case_number:
                    print(
                        f"   [{case_idx}/{len(accc_cases)}] ⚠️ No case_number, skipping")
                    continue

                if not detail_url:
                    print(
                        f"   [{case_idx}/{len(accc_cases)}] {case_number}: ⚠️ No detail_url, skipping")
                    continue

                print(
                    f"\n   [{case_idx}/{len(accc_cases)}] Checking case: {case_number}")
                print(f"      Title: {case_title}")

                # Fetch current information from detail page
                try:
                    current_info = extract_current_detail_page_info(
                        page, detail_url)

                    print("Accc daily monitor: current_info: ", current_info)

                    if not current_info:
                        print(f"      ⚠️ Could not fetch current info, skipping")
                        continue

                    # Detect changes
                    changes = detect_changes(existing_case, current_info)

                    if not changes:
                        print(f"      ✅ No changes detected")
                        continue

                    # Changes found!
                    print(f"      🔄 Changes detected ({len(changes)} fields)")
                    for field_name, old_val, new_val, change_type in changes:
                        if field_name == "Decisions and key events":
                            # Special handling for events
                            event_count = len(new_val) if isinstance(
                                new_val, list) else 0
                            print(
                                f"         • {field_name}: {event_count} new event(s) (NEW)")
                        else:
                            old_display = str(
                                old_val) if old_val is not None else "N/A"
                            new_display = str(
                                new_val) if new_val is not None else "N/A"
                            change_indicator = "NEW" if change_type == 'new' else "UPDATED"
                            print(
                                f"         • {field_name}: {old_display} → {new_display} ({change_indicator})")

                    # Update the existing case data with new values
                    updated_case = existing_case.copy()
                    for field_name, old_val, new_val, change_type in changes:
                        # Map field label back to key
                        field_key_map = {
                            "Acquisition status": "acquisition_status",
                            "Stage": "stage",
                            "Determination publication date": "determination_publication_date",
                            "ACCC Determination": "accc_determination",
                            "Decisions and key events": "decisions_and_events"
                        }
                        field_key = field_key_map.get(field_name)
                        if field_key and new_val is not None:
                            if field_key == "decisions_and_events":
                                # Merge new events with existing events
                                existing_events = updated_case.get(
                                    "decisions_and_events", [])
                                if not existing_events:
                                    existing_events = updated_case.get(
                                        "details", {}).get("decisions_and_events", [])

                                # Add new events to the list
                                for new_event in new_val:
                                    existing_events.append(new_event)

                                updated_case[field_key] = existing_events
                            else:
                                updated_case[field_key] = new_val

                    # Generate HTML email
                    html_content = generate_accc_update_email_html(
                        updated_case, deal, changes)
                    save_html_file(case_number, html_content)

                    # Send email notification
                    send_accc_update_email_via_webhook(
                        updated_case, deal, html_content, changes)

                    # Update database
                    update_success = update_case_in_db(
                        deal_id, case_number, updated_case)
                    if update_success:
                        total_cases_updated += 1

                except Exception as e:
                    print(f"      ❌ Error processing case: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

        browser.close()

    print(f"\n{'='*60}")
    print(f"📊 Summary:")
    print(f"   Total cases checked: {total_cases_checked}")
    print(f"   Cases with changes: {total_cases_updated}")
    print(f"   HTML files saved to: {HTML_OUTPUT_DIR}/")
    print(f"{'='*60}\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_accc_case_updates()
