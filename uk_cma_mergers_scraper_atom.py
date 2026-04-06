from dotenv import load_dotenv
import datetime
import json
import os
import requests
from openai import OpenAI
from bs4 import BeautifulSoup
import re
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection
from html import escape as escape_html
import xml.etree.ElementTree as ET
from llm_verification_service import verify_usa_relation

# Configuration
# CUTOFF_DATE = datetime.datetime.strptime("2026-01-20", "%Y-%m-%d")
CUTOFF_DATE = datetime.datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)

ATOM_FEED_URL = "https://www.gov.uk/cma-cases.atom?case_type%5B%5D=mergers"
OUTPUT_JSON = "deals_with_cma.json"
EXTRACTED_RECORDS_JSON = "cma_extracted_records.json"
DEALS_PATH = "deals.json"
PROMPT_LOG_PATH = "cma_gpt_prompts.log"
ENV_PATH = ".env"

# Initialize extracted records list
all_extracted_records = []

# Load OpenAI API Key
load_dotenv(ENV_PATH)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Global deals list - will be loaded from MongoDB
deals = []

# Normalize company names
all_companies = set()


def normalize_company(name):
    return name.lower().replace(",", "").replace(" inc.", "").replace(" ltd.", "").replace(" plc", "").replace(" limited", "").replace(" corporation", "").replace(" corp.", "").strip()


def get_deals_from_mongodb(include_cma_cases=False):
    """
    Fetch deals from MongoDB collection 'deals' using global connection.

    Args:
        include_cma_cases: If False, only return deals that don't have a 'uk_cma_cases' node

    Returns:
        List of deal dictionaries
    """
    try:
        # Use global MongoDB connection
        collection = get_deals_collection()

        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Base status filter - only include Open/Unknown/null/missing deals
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        # Optionally also exclude deals with existing 'uk_cma_cases' node
        if not include_cma_cases:
            cma_filter = {
                "$or": [
                    {"uk_cma_cases": {"$exists": False}},
                    {"uk_cma_cases": None},
                    {"uk_cma_cases": []},
                    {"uk_cma_cases": {}},
                ]
            }
            query = {"$and": [status_filter, cma_filter]}
        else:
            query = status_filter

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization and keep it as deal_id
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'uk_cma_cases' node" if not include_cma_cases else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_cma_cases=False):
    """
    Load deals from MongoDB. Can be called multiple times to refresh.

    Args:
        include_cma_cases: If False, only load deals that don't have a 'uk_cma_cases' node
    """
    global deals, all_companies
    deals = get_deals_from_mongodb(include_cma_cases=include_cma_cases)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")

    # Rebuild all_companies set
    all_companies = set()
    for d in deals:
        # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
        acquirer = d.get("acquirer") or d.get("acquire_name", "")
        target = d.get("target") or d.get("target_name", "")
        if acquirer:
            all_companies.add(normalize_company(acquirer))
        if target:
            all_companies.add(normalize_company(target))

    return deals


def fetch_atom_feed():
    """
    Fetch the Atom XML feed from the CMA cases URL.
    Returns the XML content as a string.
    """
    try:
        print(f"🌐 Fetching Atom feed from: {ATOM_FEED_URL}")
        response = requests.get(ATOM_FEED_URL, timeout=30)
        response.raise_for_status()
        print(
            f"✅ Successfully fetched Atom feed ({len(response.content)} bytes)")
        return response.text
    except Exception as e:
        print(f"❌ Error fetching Atom feed: {e}")
        import traceback
        traceback.print_exc()
        return None


def parse_atom_feed(xml_content):
    """
    Parse the Atom XML feed and extract entry information.
    Returns a list of dictionaries with: id, updated, title, url
    """
    records = []

    try:
        # Parse XML using ElementTree
        root = ET.fromstring(xml_content)

        # Define namespace for Atom feed
        ns = {'atom': 'http://www.w3.org/2005/Atom'}

        # Find all entry elements
        entries = root.findall('.//atom:entry', ns)
        print(f"📋 Found {len(entries)} entries in Atom feed")

        for entry in entries:
            try:
                record = {}

                # Extract id
                id_elem = entry.find('atom:id', ns)
                if id_elem is not None:
                    record["id"] = id_elem.text

                # Extract updated date
                updated_elem = entry.find('atom:updated', ns)
                if updated_elem is not None:
                    record["updated"] = updated_elem.text

                # Extract title
                title_elem = entry.find('atom:title', ns)
                if title_elem is not None:
                    record["title"] = title_elem.text

                # Extract link URL
                link_elem = entry.find('atom:link[@rel="alternate"]', ns)
                if link_elem is not None:
                    href = link_elem.get('href', '')
                    record["url"] = href

                # Only add record if it has required fields
                if record.get("title") and record.get("updated"):
                    records.append(record)
                    print(
                        f"  📄 Extracted: {record.get('updated', 'N/A')} - {record.get('title', 'N/A')[:70]}...")

            except Exception as e:
                print(f"⚠️ Error parsing entry: {e}")
                continue

    except Exception as e:
        print(f"❌ Error parsing Atom feed: {e}")
        import traceback
        traceback.print_exc()

    return records


def filter_records_by_date(records):
    """
    Filter records by checking updated date against CUTOFF_DATE.
    Returns filtered list of records.
    """
    filtered_records = []

    print(f"\n{'='*60}")
    print(
        f"🔍 Filtering records by date (CUTOFF_DATE: {CUTOFF_DATE.isoformat()})")
    print(f"{'='*60}")

    for record in records:
        updated_str = record.get("updated", "")
        if not updated_str:
            print(
                f"⚠️ Skipping record with no updated date: {record.get('title', 'N/A')[:50]}...")
            continue

        try:
            # Parse ISO 8601 format: 2026-01-20T12:30:43+00:00
            # Remove timezone info for comparison (or handle it properly)
            updated_date = datetime.datetime.fromisoformat(
                updated_str.replace('Z', '+00:00'))

            # Normalize timezone to UTC for comparison
            if updated_date.tzinfo:
                updated_date = updated_date.replace(tzinfo=None)

            # Compare with CUTOFF_DATE (which is timezone-naive)
            if updated_date >= CUTOFF_DATE:
                filtered_records.append(record)
                print(
                    f"✅ Kept: {updated_date.date()} - {record.get('title', 'N/A')[:70]}...")
            else:
                print(
                    f"⏭️  Skipped (old): {updated_date.date()} - {record.get('title', 'N/A')[:70]}...")

        except Exception as e:
            print(f"⚠️ Error parsing date '{updated_str}': {e}")
            # Include record to be safe if we can't parse the date
            filtered_records.append(record)
            print(f"  ✅ Included anyway (unparseable date)")

    print(
        f"\n📊 Filtered: {len(filtered_records)} records kept out of {len(records)} total")
    return filtered_records


def match_title_with_deals(title):
    """
    Match a case title with deals using LLM.
    Returns "Match: DEAL_ID|COMPANY_NAME|(target|acquirer)" or "None".
    """
    global deals

    # Reload deals if list is empty
    if not deals:
        print("⚠️ Deals list is empty, reloading from MongoDB...")
        load_deals(include_cma_cases=True)

    # Build deals list with all relevant information (including aliases)
    deals_list = []
    for deal in deals:
        deal_info = {
            "deal_id": deal.get("deal_id", ""),
        }
        target = deal.get("target") or deal.get("target_name", "")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "")

        if target:
            deal_info["target"] = target
        if acquirer:
            deal_info["acquirer"] = acquirer

        target_aliases = deal.get("target_aliases") or []
        parent_aliases = deal.get("parent_aliases") or []
        if isinstance(target_aliases, list) and target_aliases:
            deal_info["target_aliases"] = target_aliases
        if isinstance(parent_aliases, list) and parent_aliases:
            deal_info["parent_aliases"] = parent_aliases

        if target or acquirer:
            deals_list.append(deal_info)

    if not deals_list:
        print("⚠️ No deals with company names found")
        return "None"

    # Build structured prompt with deal information (including aliases)
    lines = []
    for d in deals_list:
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target', 'N/A')} | Acquirer: {d.get('acquirer', 'N/A')}"
        target_aliases = d.get("target_aliases", []) or []
        parent_aliases = d.get("parent_aliases", []) or []
        if target_aliases:
            line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
        if parent_aliases:
            line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
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
            f"\n{'='*80}\n{datetime.datetime.now()} - Prompt for: {title}\n{prompt}\n"
        )

    try:
        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system",
                    "content": "You identify M&A deals from UK CMA merger case titles. Return Match: DEAL_ID|COMPANY|target|acquirer or None."},
                {"role": "user", "content": prompt}
            ]
        )
        result = res.choices[0].message.content.strip()
        print(f"🧠 LLM Response: {result}")
        return result

    except Exception as e:
        print(f"❌ LLM error: {e}")
        return "None"


def scrape_case_details(url):
    """
    Scrape case detail page to extract Published date, Last updated date, and all updates.

    Args:
        url: The URL of the case detail page

    Returns:
        Dictionary with:
        - published_date: Published date string
        - last_updated: Last updated date string (None if not available)
        - updates: List of update dictionaries with date and note (empty if no updates)
    """
    try:
        print(f"  🌐 Scraping case details from: {url}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Find the published dates container
        published_dates_div = soup.find('div', class_='gem-c-published-dates')
        if not published_dates_div:
            # Try alternative selector
            published_dates_div = soup.find(
                'div', id='full-publication-update-history')

        result = {
            "published_date": None,
            "last_updated": None,
            "updates": []
        }

        if published_dates_div:
            # Extract Published date
            # Look for "Published" text in divs - it can be in direct div children
            published_divs = published_dates_div.find_all(
                'div', recursive=False)
            for div in published_divs:
                text = div.get_text(strip=True)
                if text.startswith('Published'):
                    # Extract date after "Published"
                    date_text = text.replace('Published', '').strip()
                    result["published_date"] = date_text
                    break

            # If not found in direct children, search all divs
            if not result["published_date"]:
                for div in published_dates_div.find_all('div'):
                    text = div.get_text(strip=True)
                    if text.startswith('Published'):
                        date_text = text.replace('Published', '').strip()
                        result["published_date"] = date_text
                        break

            # Check if Last updated exists - look for text containing "Last updated"
            full_text = published_dates_div.get_text()
            if 'Last updated' in full_text:
                # Extract Last updated date
                # The "Last updated" text might be directly in the div or in a text node
                # Try to find it by searching the text content
                last_updated_match = re.search(
                    r'Last updated\s+([^\n]+)', full_text)
                if last_updated_match:
                    result["last_updated"] = last_updated_match.group(
                        1).strip()
                else:
                    # Fallback: try to find in divs
                    for div in published_dates_div.find_all('div'):
                        text = div.get_text(strip=True)
                        if 'Last updated' in text:
                            # Extract date after "Last updated"
                            date_text = re.sub(
                                r'Last updated\s*', '', text, flags=re.IGNORECASE).strip()
                            if date_text and date_text != text:
                                result["last_updated"] = date_text
                                break

                # Extract all updates from the history list
                # The list might be hidden initially, but we can still extract it
                history_list = published_dates_div.find(
                    'ol', class_='gem-c-published-dates__list')
                if not history_list:
                    # Try alternative selector
                    history_list = published_dates_div.find('ol')

                if history_list:
                    update_items = history_list.find_all(
                        'li', class_='gem-c-published-dates__change-item')
                    if not update_items:
                        # Try without class filter
                        update_items = history_list.find_all('li')

                    for item in update_items:
                        update_data = {}

                        # Extract date from time element
                        time_elem = item.find(
                            'time', class_='gem-c-published-dates__change-date')
                        if not time_elem:
                            # Try without class filter
                            time_elem = item.find('time')

                        if time_elem:
                            # Get datetime attribute first, fallback to text
                            datetime_attr = time_elem.get('datetime', '')
                            if datetime_attr:
                                update_data["date"] = datetime_attr
                            else:
                                update_data["date"] = time_elem.get_text(
                                    strip=True)

                        # Extract note
                        note_elem = item.find(
                            'p', class_='gem-c-published-dates__change-note')
                        if not note_elem:
                            # Try without class filter
                            note_elem = item.find('p')

                        if note_elem:
                            update_data["note"] = note_elem.get_text(
                                strip=True)

                        if update_data:
                            result["updates"].append(update_data)

            print(
                f"    ✅ Extracted: Published={result['published_date']}, Last updated={result['last_updated']}, Updates={len(result['updates'])}")
        else:
            print(f"    ⚠️ Published dates section not found")

        return result

    except Exception as e:
        print(f"    ❌ Error scraping case details: {e}")
        import traceback
        traceback.print_exc()
        return {
            "published_date": None,
            "last_updated": None,
            "updates": []
        }


def convert_datetime_to_string(obj):
    """
    Recursively convert datetime objects to strings for JSON serialization.
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    elif isinstance(obj, datetime.date):
        return obj.isoformat()
    elif hasattr(obj, 'isoformat') and callable(getattr(obj, 'isoformat')):
        try:
            return obj.isoformat()
        except:
            return str(obj)
    elif isinstance(obj, dict):
        return {key: convert_datetime_to_string(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetime_to_string(item) for item in obj]
    else:
        return obj


def generate_cma_case_email_html(case_info, deal_match, is_new_case=False, changes_detected=None):
    """
    Generate HTML email for UK CMA merger case match.

    Args:
        case_info: The CMA case data dictionary
        deal_match: The matched deal object
        is_new_case: True if this is a new case, False if it's an update
        changes_detected: Dictionary with changes detected (new_updates, last_updated_changed, etc.)

    Returns:
        Tuple of (subject, html_email)
    """
    if changes_detected is None:
        changes_detected = {}

    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    # Extract CMA case data
    title = case_info.get("title", "N/A")
    updated_date = case_info.get("updated", "N/A")
    url = case_info.get("url", "")
    matched_company = case_info.get("matched_company", "")
    matched_role = case_info.get("matched_role", "")
    published_date = case_info.get("published_date", "")
    last_updated = case_info.get("last_updated", "")
    updates = case_info.get("updates", [])

    # Determine email type and subject
    if is_new_case:
        title_text = f"🆕 NEW UK CMA Merger Case – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"🆕 NEW UK CMA Merger Case – {title[:50]}"
        subject = f"[FRMD] UK CMA Merger Case (New) – {target} / {acquirer}"
        header_color = "#28a745"  # Green for new
        status_badge = '<div style="background-color:#28a745; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">🆕 NEW CASE</div>'
    else:
        title_text = f"📝 UK CMA Merger Case Update – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"📝 UK CMA Merger Case Update – {title[:50]}"
        subject = f"[FRMD] UK CMA Merger Case (Updated) – {target} / {acquirer}"
        header_color = "#ff9800"  # Orange for update
        status_badge = '<div style="background-color:#ff9800; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">📝 CASE UPDATED</div>'

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid {header_color};">
      {escape_html(title_text)}
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      {status_badge}
    </div>

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
        <td style="padding:8px; font-weight:bold; color:#555;">Case Title:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Updated Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(updated_date))}</td>
      </tr>"""

    if published_date:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Published Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(published_date))}</td>
      </tr>"""

    if last_updated:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Last Updated:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(last_updated))}</td>
      </tr>"""

    if matched_company:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Matched Company:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(matched_company))} ({escape_html(str(matched_role))})</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Case URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#0066cc; text-decoration:none;" target="_blank">
            View CMA Case Page
          </a>
        </td>
      </tr>"""

    html_email += """
    </table>"""

    # Add "What's New" section for updates
    if not is_new_case and changes_detected:
        html_email += """
    <div style="margin-top:25px; padding:15px; background-color:#fff3cd; border-left:4px solid #ff9800; border-radius:4px;">
      <h3 style="color:#856404; margin-top:0; margin-bottom:12px;">📋 What's New / Changed:</h3>
      <ul style="margin:0; padding-left:20px; color:#856404;">"""

        if changes_detected.get("last_updated_changed"):
            old_date = changes_detected["last_updated_changed"].get(
                "old", "N/A")
            new_date = changes_detected["last_updated_changed"].get(
                "new", "N/A")
            html_email += f"""
        <li style="margin-bottom:8px;">
          <strong>Last Updated Changed:</strong> {escape_html(str(old_date))} → <strong style="color:#ff9800;">{escape_html(str(new_date))}</strong>
        </li>"""

        if changes_detected.get("new_updates"):
            new_updates_list = changes_detected["new_updates"]
            html_email += f"""
        <li style="margin-bottom:8px;">
          <strong>New Updates ({len(new_updates_list)}):</strong>
          <ul style="margin-top:5px; padding-left:20px;">"""
            for update in new_updates_list:
                update_date = update.get("date", "N/A")
                update_note = update.get("note", "N/A")
                html_email += f"""
            <li style="margin-bottom:5px;">
              <strong style="color:#ff9800;">{escape_html(str(update_date))}</strong>: {escape_html(str(update_note))}
            </li>"""
            html_email += """
          </ul>
        </li>"""

        html_email += """
      </ul>
    </div>"""

    # Add all updates section
    if updates:
        html_email += f"""
    <div style="margin-top:25px;">
      <h3 style="color:#333; margin-bottom:12px; border-bottom:2px solid #e0e0e0; padding-bottom:8px;">All Updates:</h3>
      <ul style="margin:0; padding-left:20px;">"""

        # Create a set of new update keys for highlighting
        new_update_keys = set()
        if changes_detected.get("new_updates"):
            for update in changes_detected["new_updates"]:
                update_key = f"{update.get('date', '')}|{update.get('note', '')}"
                new_update_keys.add(update_key)

        for update in updates:
            update_date = update.get("date", "N/A")
            update_note = update.get("note", "N/A")
            update_key = f"{update_date}|{update_note}"
            is_new = update_key in new_update_keys

            if is_new:
                html_email += f"""
        <li style="margin-bottom:8px; padding:8px; background-color:#fff3cd; border-left:3px solid #ff9800; border-radius:3px;">
          <strong style="color:#ff9800;">🆕 {escape_html(str(update_date))}</strong>: {escape_html(str(update_note))}
        </li>"""
            else:
                html_email += f"""
        <li style="margin-bottom:8px;">
          <strong>{escape_html(str(update_date))}</strong>: {escape_html(str(update_note))}
        </li>"""

        html_email += """
      </ul>
    </div>"""

    html_email += f"""

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from UK CMA merger case matches.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_cma_case_email_via_webhook(case_info, deal_match, is_new_case=False, changes_detected=None):
    """
    Send email notification via n8n webhook after saving CMA case data.

    Args:
        case_info: The CMA case data dictionary
        deal_match: The matched deal object
        is_new_case: True if this is a new case, False if it's an update
        changes_detected: Dictionary with changes detected (new_updates, last_updated_changed, etc.)

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_cma_case_email_html(
            case_info, deal_match, is_new_case, changes_detected)
        print(f"📝 Generated email subject: {subject}")

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        # Extract deal information for payload
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': deal_id,
            'target': target,
            'acquirer': acquirer,
            'title': case_info.get("title", "N/A"),
            'updated_date': case_info.get("updated", "N/A"),
            'published_date': case_info.get("published_date", ""),
            'last_updated': case_info.get("last_updated", ""),
            'updates': case_info.get("updates", []),
            'url': case_info.get("url", ""),
            'matched_company': case_info.get("matched_company", ""),
            'matched_role': case_info.get("matched_role", ""),
            'is_new_case': is_new_case,
            'changes_detected': changes_detected or {},
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
            f"✅ Email sent successfully via n8n webhook! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def generate_unmatched_cma_case_email_html(record: dict) -> tuple:
    """
    Generate HTML email for unmatched UK CMA case that is USA-related.

    Args:
        record: The UK CMA record dictionary

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract record data
    title = record.get("title", "N/A")
    updated_date = record.get("updated", "N/A")
    url = record.get("url", "")
    record_id = record.get("id", "")

    # Build subject
    subject = f"[FRUD] UK CMA Merger Case (USA-Related) – {title[:50]}"

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
      UK CMA Merger Case (USA-Related)
    </h2>
    <div style="text-align:center; margin-bottom:20px;">
      <div style="background-color:#f59e0b; color:white; padding:8px 16px; border-radius:4px; display:inline-block; margin-bottom:15px; font-weight:bold;">🇺🇸 USA-RELATED</div>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <tr>
        <td style="padding:8px; font-weight:bold; width:170px; color:#555;">Case Title:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Updated Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(updated_date))}</td>
      </tr>"""

    if record_id:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Case ID:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(record_id))}</td>
      </tr>"""

    if url:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Case URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(url)}" style="color:#0066cc; text-decoration:none;" target="_blank">
            View CMA Case Page
          </a>
        </td>
      </tr>"""

    html_email += """
    </table>

    

    
  </div>
</body>
</html>
"""

    return subject, html_email


def send_unmatched_cma_case_email_via_webhook(record: dict) -> bool:
    """
    Send email notification via n8n webhook for unmatched UK CMA case that is USA-related.

    Args:
        record: The UK CMA record dictionary

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_unmatched_cma_case_email_html(record)
        print(f"📝 Generated email subject: {subject}")

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending email via n8n webhook: {webhook_url}")

        # Extract record information
        title = record.get("title", "N/A")
        updated_date = record.get("updated", "N/A")
        url = record.get("url", "")
        record_id = record.get("id", "")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': 'N/A',  # No deal match
            'target': 'N/A',  # No deal match
            'acquirer': 'N/A',  # No deal match
            'title': title,
            'updated_date': updated_date,
            'url': url,
            'id': record_id,
            'is_unmatched': True,
            'usa_related': True,
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
            f"✅ Email sent successfully via n8n webhook! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending email: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_cma_case_data_to_deal(deal_match, case_info):
    """
    Save matched CMA case data to MongoDB deal record under 'uk_cma_cases' node.

    Args:
        deal_match: The matched deal object (must have deal_id to identify)
        case_info: The CMA case information to save
    """
    try:
        print(f"💾 Saving CMA case data to deal...")

        # Use global MongoDB connection
        if not is_connected():
            print("⚠️ MongoDB connection not available, skipping save to MongoDB")
            return False

        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available, skipping save to MongoDB")
            return False

        # Convert datetime objects in case_info to strings for MongoDB
        case_info_serializable = convert_datetime_to_string(case_info)

        print(
            f"📝 Preparing CMA case data with keys: {list(case_info_serializable.keys())}")

        # Find the deal by deal_id (preferred) or by acquirer and target
        query = {}
        if deal_match.get("deal_id"):
            # Try to find by deal_id first (convert back to ObjectId)
            try:
                query["_id"] = ObjectId(deal_match["deal_id"])
            except Exception as e:
                print(
                    f"⚠️ Invalid deal_id format: {e}, falling back to acquirer/target")
                query = {}

        # Fallback to acquirer/target if no deal_id or if deal_id lookup failed
        if not query:
            # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name")
            target = deal_match.get("target") or deal_match.get("target_name")

            # Build query with $or to handle both field name formats
            or_conditions = []
            if acquirer:
                or_conditions.append({"acquirer": acquirer})
                or_conditions.append({"acquire_name": acquirer})
            if target:
                or_conditions.append({"target": target})
                or_conditions.append({"target_name": target})

            if or_conditions:
                query = {"$or": or_conditions}

        if not query:
            print(
                "⚠️ Cannot identify deal (no deal_id, acquirer, or target), skipping MongoDB save")
            return False

        print(f"🔍 Searching for deal with query: {query}")

        # Debug: Show what we're looking for
        if deal_match.get("deal_id"):
            print(f"   Using deal_id: {deal_match.get('deal_id')}")
        else:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name", "")
            target = deal_match.get(
                "target") or deal_match.get("target_name", "")
            print(f"   Looking for: Acquirer='{acquirer}', Target='{target}'")

        # First, check if the case already exists to determine if it's new or an update
        existing_deal = collection.find_one(query)
        is_new_case = False
        changes_detected = {}

        if existing_deal and existing_deal.get("uk_cma_cases"):
            # Case already exists - compare to find what's new/changed
            existing_case = existing_deal.get("uk_cma_cases", {})
            existing_updates = existing_case.get("updates", [])
            new_updates = case_info_serializable.get("updates", [])

            # Find new updates by comparing dates and notes
            new_update_items = []
            existing_update_keys = set()
            for update in existing_updates:
                # Create a key from date and note to identify unique updates
                update_key = f"{update.get('date', '')}|{update.get('note', '')}"
                existing_update_keys.add(update_key)

            for update in new_updates:
                update_key = f"{update.get('date', '')}|{update.get('note', '')}"
                if update_key not in existing_update_keys:
                    new_update_items.append(update)

            # Check if last_updated changed
            existing_last_updated = existing_case.get("last_updated")
            new_last_updated = case_info_serializable.get("last_updated")

            if new_update_items:
                changes_detected["new_updates"] = new_update_items
                print(f"📝 Found {len(new_update_items)} new update(s)")

            if new_last_updated and new_last_updated != existing_last_updated:
                changes_detected["last_updated_changed"] = {
                    "old": existing_last_updated,
                    "new": new_last_updated
                }
                print(
                    f"📝 Last updated changed: {existing_last_updated} → {new_last_updated}")

            is_new_case = False
        else:
            # This is a new case
            is_new_case = True
            print(f"🆕 This is a new case (no existing uk_cma_cases found)")

        # Update the deal document with uk_cma_cases data as a single object
        update_result = collection.update_one(
            query,
            {
                "$set": {
                    "uk_cma_cases": case_info_serializable
                }
            }
        )

        print(
            f"📊 Update result: matched={update_result.matched_count}, modified={update_result.modified_count}")

        if update_result.modified_count > 0 or update_result.matched_count > 0:
            print(f"✅ Saved CMA case data to deal record in MongoDB")

            # Determine if we should send an email
            should_send_email = False
            email_reason = ""

            if is_new_case:
                should_send_email = True
                email_reason = "new case"
            elif changes_detected:
                # Check if there are actual changes
                has_new_updates = bool(changes_detected.get("new_updates"))
                has_last_updated_change = bool(
                    changes_detected.get("last_updated_changed"))

                if has_new_updates or has_last_updated_change:
                    should_send_email = True
                    reasons = []
                    if has_new_updates:
                        reasons.append(
                            f"{len(changes_detected['new_updates'])} new update(s)")
                    if has_last_updated_change:
                        reasons.append("last updated changed")
                    email_reason = ", ".join(reasons)
                else:
                    email_reason = "no changes detected"
            else:
                email_reason = "no changes detected"

            # Send email notification via n8n webhook only if new or updated
            if should_send_email:
                print(f"📧 Sending email notification ({email_reason})...")
                try:
                    send_cma_case_email_via_webhook(
                        case_info_serializable, deal_match, is_new_case, changes_detected)
                except Exception as e:
                    print(f"⚠️ Error sending email notification: {e}")
                    # Don't fail the save operation if email fails
            else:
                print(f"⏭️  Skipping email notification ({email_reason})")

            return True
        else:
            print(f"⚠️ Deal not found in MongoDB: {query}")
            return False

    except Exception as e:
        error_msg = str(e)
        # Check if it's a DNS/network timeout issue
        if "DNS" in error_msg or "timeout" in error_msg.lower() or "resolution" in error_msg.lower():
            print(
                f"⚠️ MongoDB connection timeout/network issue. Data saved to JSON file only.")
        else:
            print(f"❌ Error saving to MongoDB: {error_msg[:300]}")
        # Don't print full traceback for network issues to reduce noise
        if "DNS" not in error_msg and "timeout" not in error_msg.lower():
            import traceback
            traceback.print_exc()
        return False


def match_records_with_deals(records):
    """
    Match extracted records with deals using LLM.
    Adds matched records to the corresponding deal objects.
    """
    print(f"\n{'='*60}")
    print(f"🔍 Matching {len(records)} records with deals...")
    print(f"{'='*60}\n")

    matched_count = 0

    for idx, record in enumerate(records, 1):
        title = record.get("title", "")
        updated_date = record.get("updated", "")

        print(f"[{idx}/{len(records)}] {updated_date} - {title[:70]}...")

        if not title:
            print("  ⏩ Skipped (no title)")
            continue

        # Match title with deals using LLM
        match_result = match_title_with_deals(title)

        # Parse "Match: DEAL_ID|COMPANY_NAME|(target|acquirer)" or "None"
        deal_match = None
        company_name = ""
        role = ""
        if match_result and str(match_result).strip().lower() != "none":
            stripped = str(match_result).strip()
            if stripped.lower().startswith("match:"):
                # Parse: Match: DEAL_ID|COMPANY_NAME|role
                parts = stripped[6:].strip().split("|")  # Remove "Match:"
                if len(parts) >= 3:
                    llm_deal_id = parts[0].strip()
                    company_name = parts[1].strip()
                    role = parts[2].strip().lower().replace(
                        "(", "").replace(")", "")
                    if role not in ("target", "acquirer"):
                        role = "acquirer"  # default
                    # Direct lookup by deal_id
                    deal_by_id = {str(d.get("deal_id", ""))
                                      : d for d in deals if d.get("deal_id")}
                    if llm_deal_id in deal_by_id:
                        deal_match = deal_by_id[llm_deal_id]

        if deal_match and company_name and role:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name", "")
            target = deal_match.get(
                "target") or deal_match.get("target_name", "")

            print(
                f"  🎯 Match found: {company_name} ({role})")
            print(
                f"  🔍 Deal found: {acquirer or 'N/A'} / {target or 'N/A'}")

            # Scrape case details from the URL
            case_url = record.get("url", "")
            case_details = {}
            if case_url:
                case_details = scrape_case_details(case_url)

            # Build CMA case information
            case_info = {
                "title": title,
                "updated": updated_date,
                "url": case_url,
                "id": record.get("id", ""),
                "matched_company": company_name,
                "matched_role": role,
                "published_date": case_details.get("published_date"),
                "last_updated": case_details.get("last_updated"),
                "updates": case_details.get("updates", []),
            }

            # Save to MongoDB under 'uk_cma_cases' node
            print(f"  💾 Attempting to save to MongoDB...")
            save_result = save_cma_case_data_to_deal(deal_match, case_info)
            if save_result:
                print(
                    f"  ✅ Added to deal: {acquirer or 'N/A'} / {target or 'N/A'}"
                )
                matched_count += 1
            else:
                print(
                    f"  ⚠️ Failed to save to MongoDB for deal: {acquirer or 'N/A'} / {target or 'N/A'}"
                )
        elif deal_match is None and match_result and str(match_result).strip().lower() != "none":
            # LLM returned a match format but we couldn't find the deal
            print(
                f"  ⚠️ Deal not found for match: {match_result}")
        else:
            print(f"  ➖ No match")
            # Verify if case title is USA-related
            try:
                is_usa_related = verify_usa_relation(
                    company_details=title,
                    case_type="UK"
                )
                if is_usa_related:
                    print(
                        f"   🇺🇸 USA-related case detected - sending email notification")
                    send_unmatched_cma_case_email_via_webhook(record)
                else:
                    print(f"   ℹ️ Not USA-related - no action taken")
            except Exception as e:
                print(f"   ⚠️ Error verifying USA relation: {e}")
                import traceback
                traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"✅ Matching complete: {matched_count} records matched with deals")
    print(f"{'='*60}\n")

    return matched_count


# Main execution
def main():
    """
    EXECUTION FLOW:
    ================
    Phase 1: Extract Records from Atom Feed
        1. Fetch Atom XML feed from ATOM_FEED_URL
        2. Parse XML to extract all entries
        3. Filter entries by updated date (>= CUTOFF_DATE)
        4. Save all filtered records to JSON

    Phase 2: Match with Deals
        5. Load all extracted records
        6. For each record, match title with deals using LLM
        7. If match found, save to MongoDB under 'uk_cma_cases' node
    """
    global all_extracted_records
    all_extracted_records = []

    # Initialize MongoDB connection
    print(f"\n{'='*60}")
    print(f"🔌 INITIALIZING MONGODB CONNECTION")
    print(f"{'='*60}")
    success, message = init_mongodb_connection(ENV_PATH)
    if success:
        print(f"✅ {message}\n")
    else:
        print(f"⚠️ {message}")
        print("⚠️ Continuing with local operations only...\n")

    # Load deals from MongoDB when main() is called (connection should be ready by then)
    # Only load deals without 'uk_cma_cases' node to avoid re-processing
    print("📊 Loading deals from MongoDB (excluding deals with 'uk_cma_cases' node)...")
    load_deals(include_cma_cases=True)

    print(f"\n{'='*60}")
    print(f"🚀 PHASE 1: EXTRACT CMA MERGER CASE RECORDS FROM ATOM FEED")
    print(f"{'='*60}\n")

    # Step 1: Fetch Atom feed
    print(f"📍 Step 1: Fetching Atom feed")
    xml_content = fetch_atom_feed()
    if not xml_content:
        print("❌ Failed to fetch Atom feed. Exiting.")
        return

    # Step 2: Parse XML to extract entries
    print(f"\n📍 Step 2: Parsing Atom feed XML")
    all_records = parse_atom_feed(xml_content)
    print(f"   ✅ Extracted {len(all_records)} entries from feed\n")

    # Step 3: Filter by updated date
    print(f"📍 Step 3: Filtering entries by updated date")
    all_extracted_records = filter_records_by_date(all_records)

    # Step 4: Save all extracted records to JSON
    print(f"\n{'='*60}")
    print(f"📍 Step 4: SAVE EXTRACTED RECORDS TO JSON")
    print(f"{'='*60}")
    print(f"   Total records extracted: {len(all_extracted_records)}")
    print(f"   Saving to: {EXTRACTED_RECORDS_JSON}")

    with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
        json.dump(all_extracted_records, f, ensure_ascii=False, indent=2)
    print(f"   ✅ Saved!\n")

    # Phase 2: Match records with deals
    print(f"\n{'='*60}")
    print(f"🚀 PHASE 2: MATCH RECORDS WITH DEALS")
    print(f"{'='*60}\n")
    print(f"📍 Step 5-7: Match titles with deals using LLM\n")

    matched_count = match_records_with_deals(all_extracted_records)

    # Final summary
    print(f"\n{'='*60}")
    print(f"✅ ALL DONE!")
    print(f"{'='*60}")
    print(f"📊 Total records extracted: {len(all_extracted_records)}")
    print(f"🎯 Total matches found: {matched_count}")
    print(f"📁 All extracted records → {EXTRACTED_RECORDS_JSON}")
    print(f"💾 Matched deals saved to MongoDB under 'uk_cma_cases' node")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
