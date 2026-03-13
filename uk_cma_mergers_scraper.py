from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import datetime
import time
import json
import os
import requests
from openai import OpenAI
from bs4 import BeautifulSoup
import re
from bson import ObjectId
from mongodb_connection import get_deals_collection, get_mongo_client, is_connected, init_mongodb_connection
from html import escape as escape_html

# Configuration
CUTOFF_DATE = datetime.datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)
BASE_URL = "https://www.gov.uk/cma-cases?case_type%5B%5D=mergers"
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
            cma_filter = {"uk_cma_cases": {"$exists": False}}
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


def extract_records_from_html(html_content):
    """
    Extract all case records from the CMA cases page HTML.
    Returns a list of dicts with: title, case_type, case_state, market_sector, opened_date, closed_date, outcome_type, url
    """
    records = []
    soup = BeautifulSoup(html_content, "html.parser")

    # Find the results container
    results_container = soup.find("div", id="js-results")
    if not results_container:
        print("⚠️ Results container not found")
        return records

    # Find all case items
    items = results_container.select("li.gem-c-document-list__item")

    for item in items:
        try:
            record = {}

            # Extract title and URL
            title_elem = item.find(
                "div", class_="gem-c-document-list__item-title")
            if not title_elem:
                continue

            link = title_elem.find("a")
            if not link:
                continue

            record["title"] = link.get_text(strip=True)
            href = link.get("href", "")
            if href:
                if href.startswith("/"):
                    record["url"] = f"https://www.gov.uk{href}"
                else:
                    record["url"] = href
            else:
                record["url"] = ""

            # Extract metadata
            metadata_list = item.find(
                "ul", class_="gem-c-document-list__item-metadata")
            if not metadata_list:
                continue

            metadata_items = metadata_list.find_all(
                "li", class_="gem-c-document-list__attribute")

            for meta_item in metadata_items:
                text = meta_item.get_text(strip=True)

                if text.startswith("Case type:"):
                    record["case_type"] = text.replace(
                        "Case type:", "").strip()
                elif text.startswith("Case state:"):
                    record["case_state"] = text.replace(
                        "Case state:", "").strip()
                elif text.startswith("Market sector:"):
                    record["market_sector"] = text.replace(
                        "Market sector:", "").strip()
                elif text.startswith("Outcome type:"):
                    record["outcome_type"] = text.replace(
                        "Outcome type:", "").strip()
                elif text.startswith("Opened:"):
                    time_elem = meta_item.find("time")
                    if time_elem:
                        datetime_attr = time_elem.get("datetime", "")
                        if datetime_attr:
                            try:
                                record["opened_date"] = datetime.datetime.strptime(
                                    datetime_attr, "%Y-%m-%d"
                                ).strftime("%Y-%m-%d")
                            except:
                                # Fallback to text parsing
                                date_text = time_elem.get_text(strip=True)
                                record["opened_date"] = date_text
                    else:
                        record["opened_date"] = text.replace(
                            "Opened:", "").strip()
                elif text.startswith("Closed:"):
                    time_elem = meta_item.find("time")
                    if time_elem:
                        datetime_attr = time_elem.get("datetime", "")
                        if datetime_attr:
                            try:
                                record["closed_date"] = datetime.datetime.strptime(
                                    datetime_attr, "%Y-%m-%d"
                                ).strftime("%Y-%m-%d")
                            except:
                                date_text = time_elem.get_text(strip=True)
                                record["closed_date"] = date_text
                    else:
                        record["closed_date"] = text.replace(
                            "Closed:", "").strip()

            # Ensure required fields exist
            if "opened_date" not in record:
                continue

            records.append(record)
            print(
                f"📋 Extracted: {record.get('opened_date', 'N/A')} - {record['title'][:70]}...")

        except Exception as e:
            print(f"⚠️ Error extracting record: {e}")
            continue

    return records


def match_title_with_deals(title):
    """
    Match a case title with deals using LLM.
    Returns matched deal info or None.
    """
    prompt = f"""
You are a professional M&A analyst specializing in UK merger cases.

Below is a CMA merger case title. Your task is to match it with any of the known companies from our deals database.

Case Title: {title}

Known companies (acquirers and targets):
{', '.join(sorted(all_companies))}

Instructions:
- Match the case title with companies from the known set using partial or fuzzy name matching
- Consider variations, abbreviations, and common company name formats
- Return a JSON object with the match information

Return format (JSON only, no markdown):
{{
  "matched": true/false,
  "company_name": "matched company name from known set",
  "role": "acquirer" or "target",
  "confidence": "high" or "medium" or "low"
}}

If no match, return:
{{
  "matched": false,
  "company_name": null,
  "role": null,
  "confidence": null
}}
"""

    with open(PROMPT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            f"\n{'='*80}\n{datetime.datetime.now()} - Prompt for: {title}\n{prompt}\n"
        )

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system",
                    "content": "You identify M&A deals from UK CMA merger case titles."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=300
        )
        content = res.choices[0].message.content.strip()

        # Remove markdown code blocks if present
        if content.startswith("```"):
            content = re.sub(r"^```json|^```|```$", "", content).strip()

        # Extract JSON
        json_start = content.find("{")
        json_end = content.rfind("}") + 1
        if json_start != -1 and json_end > json_start:
            json_str = content[json_start:json_end]
            try:
                parsed = json.loads(json_str)
                return parsed
            except json.JSONDecodeError as e:
                print(f"❌ JSON parsing error: {e}")
                print(f"   Content: {content}")
                return {"matched": False}
        else:
            return {"matched": False}

    except Exception as e:
        print(f"❌ LLM error: {e}")
        return {"matched": False}


def extract_page_records(page, page_num=1):
    """
    Extract all records from current page.
    Returns: (records_list, should_stop, has_next_page)
    """
    print(f"\n{'='*60}")
    print(f"📄 PAGE {page_num}: Extracting records...")
    print(f"{'='*60}")

    # Wait for results to load
    try:
        page.wait_for_selector("div#js-results", timeout=10000)
    except:
        print("⚠️ Results container not found")
        return [], False, False

    # Get HTML content for parsing (not saving to disk)
    html_content = page.content()

    # Extract all records from the HTML
    page_records = extract_records_from_html(html_content)
    print(f"📊 Found {len(page_records)} records on page")

    # Filter records: only keep records >= CUTOFF_DATE
    filtered_records = []
    should_stop = False

    for record in page_records:
        try:
            opened_date_str = record.get("opened_date", "")
            if not opened_date_str:
                continue

            # Parse date (handle both YYYY-MM-DD and text formats)
            try:
                record_date = datetime.datetime.strptime(
                    opened_date_str, "%Y-%m-%d")
            except:
                # Try parsing text format like "19 January 2026"
                try:
                    record_date = datetime.datetime.strptime(
                        opened_date_str, "%d %B %Y")
                except:
                    print(f"⚠️ Could not parse date: {opened_date_str}")
                    # Include it to be safe
                    filtered_records.append(record)
                    continue

            if record_date >= CUTOFF_DATE:
                # Keep this record (date is >= cutoff)
                filtered_records.append(record)
            else:
                # This record is older than cutoff - don't include it and stop
                print(
                    f"🛑 Found record older than cutoff: {record_date.date()} < {CUTOFF_DATE.date()}"
                )
                print(f"   Stopping extraction")
                should_stop = True
                break

        except Exception as e:
            print(f"⚠️ Error parsing date for record: {e}")
            # If we can't parse the date, include the record to be safe
            filtered_records.append(record)

    print(
        f"✅ Kept {len(filtered_records)} records (filtered out {len(page_records) - len(filtered_records)} old records)"
    )

    # Check if there's a next page
    has_next_page = False
    try:
        # Try multiple selectors for next page link
        next_link = page.query_selector('a[rel="next"]')
        if not next_link:
            # Try finding "Next page" text link
            next_link = page.get_by_text("Next page", exact=False).first
        if not next_link:
            # Try finding pagination links
            pagination_links = page.query_selector_all(
                'nav a, .pagination a, .gem-c-pagination a')
            for link in pagination_links:
                link_text = link.inner_text().lower()
                if "next" in link_text or ">" in link_text:
                    next_link = link
                    break
        if next_link:
            has_next_page = True
    except:
        pass

    return filtered_records, should_stop, has_next_page


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


def generate_cma_case_email_html(case_info, deal_match):
    """
    Generate HTML email for UK CMA merger case match.

    Args:
        case_info: The CMA case data dictionary
        deal_match: The matched deal object

    Returns:
        Tuple of (subject, html_email)
    """
    # Extract deal information
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")

    # Extract CMA case data
    title = case_info.get("title", "N/A")
    opened_date = case_info.get("opened_date", "N/A")
    closed_date = case_info.get("closed_date", "")
    case_state = case_info.get("case_state", "")
    case_type = case_info.get("case_type", "")
    market_sector = case_info.get("market_sector", "")
    outcome_type = case_info.get("outcome_type", "")
    url = case_info.get("url", "")
    matched_company = case_info.get("matched_company", "")
    matched_role = case_info.get("matched_role", "")

    title_text = f"UK CMA Merger Case – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"UK CMA Merger Case – {title[:50]}"
    subject = f"UK CMA Merger Case – {target} / {acquirer}"

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #0066cc;">
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
        <td style="padding:8px; font-weight:bold; color:#555;">Case Title:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(title))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Opened Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(opened_date))}</td>
      </tr>"""

    if closed_date:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Closed Date:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(closed_date))}</td>
      </tr>"""

    if case_state:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Case State:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(case_state))}</td>
      </tr>"""

    if case_type:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Case Type:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(case_type))}</td>
      </tr>"""

    if market_sector:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Market Sector:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(market_sector))}</td>
      </tr>"""

    if outcome_type:
        html_email += f"""
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">Outcome Type:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(outcome_type))}</td>
      </tr>"""

    if matched_company:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Matched Company:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(matched_company))} ({escape_html(str(matched_role))})</td>
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

    html_email += f"""
    </table>

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from UK CMA merger case matches.</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_cma_case_email_via_webhook(case_info, deal_match):
    """
    Send email notification via n8n webhook after saving CMA case data.

    Args:
        case_info: The CMA case data dictionary
        deal_match: The matched deal object

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_cma_case_email_html(
            case_info, deal_match)
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
            'opened_date': case_info.get("opened_date", "N/A"),
            'closed_date': case_info.get("closed_date", ""),
            'case_state': case_info.get("case_state", ""),
            'case_type': case_info.get("case_type", ""),
            'market_sector': case_info.get("market_sector", ""),
            'outcome_type': case_info.get("outcome_type", ""),
            'url': case_info.get("url", ""),
            'matched_company': case_info.get("matched_company", ""),
            'matched_role': case_info.get("matched_role", ""),
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

        if update_result.modified_count > 0:
            print(f"✅ Saved CMA case data to deal record in MongoDB")

            # Send email notification via n8n webhook
            try:
                send_cma_case_email_via_webhook(
                    case_info_serializable, deal_match)
            except Exception as e:
                print(f"⚠️ Error sending email notification: {e}")
                # Don't fail the save operation if email fails

            return True
        elif update_result.matched_count > 0:
            print(f"ℹ️ Deal found but no changes made (case may already exist)")
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
        opened_date = record.get("opened_date", "")

        print(f"[{idx}/{len(records)}] {opened_date} - {title[:70]}...")

        if not title:
            print("  ⏩ Skipped (no title)")
            continue

        # Match title with deals using LLM
        match_result = match_title_with_deals(title)

        if match_result.get("matched"):
            company_name = match_result.get("company_name", "")
            role = match_result.get("role", "")
            confidence = match_result.get("confidence", "low")

            print(
                f"  🎯 Match found: {company_name} ({role}, confidence: {confidence})")

            # Find the deal and add the record
            for deal in deals:
                # Handle both old format (target/acquirer) and new format (target_name/acquire_name)
                acquirer = deal.get("acquirer") or deal.get("acquire_name", "")
                target = deal.get("target") or deal.get("target_name", "")

                normalized_acquirer = normalize_company(acquirer)
                normalized_target = normalize_company(target)

                if normalize_company(company_name) == normalized_acquirer or normalize_company(company_name) == normalized_target:
                    # Build CMA case information
                    case_info = {
                        "title": title,
                        "opened_date": opened_date,
                        "closed_date": record.get("closed_date", ""),
                        "case_state": record.get("case_state", ""),
                        "case_type": record.get("case_type", ""),
                        "market_sector": record.get("market_sector", ""),
                        "outcome_type": record.get("outcome_type", ""),
                        "url": record.get("url", ""),
                        "matched_company": company_name,
                        "matched_role": role,
                    }

                    # Save to MongoDB under 'uk_cma_cases' node
                    save_result = save_cma_case_data_to_deal(deal, case_info)
                    if save_result:
                        print(
                            f"  ✅ Added to deal: {acquirer or 'N/A'} / {target or 'N/A'}"
                        )
                        matched_count += 1
                    else:
                        print(
                            f"  ⚠️ Failed to save to MongoDB for deal: {acquirer or 'N/A'} / {target or 'N/A'}"
                        )
                    break
        else:
            print(f"  ➖ No match")

    print(f"\n{'='*60}")
    print(f"✅ Matching complete: {matched_count} records matched with deals")
    print(f"{'='*60}\n")

    return matched_count


def extract_from_existing_html_files():
    """
    Extract records from already saved HTML files.
    Note: HTML file saving has been disabled. This function is kept for compatibility
    but will not find any files since they are no longer saved.
    Returns list of extracted records.
    """
    from glob import glob

    records = []
    # HTML files are no longer saved, so this function will not find any files
    html_files = []

    if not html_files:
        print(f"⚠️ HTML file saving is disabled. No existing HTML files to process.")
        return records

    print(f"\n{'='*60}")
    print(f"📂 Found {len(html_files)} existing HTML files")
    print(f"{'='*60}\n")

    for idx, html_file in enumerate(html_files, 1):
        filename = os.path.basename(html_file)
        print(f"[{idx}/{len(html_files)}] Processing: {filename}")

        try:
            with open(html_file, "r", encoding="utf-8") as f:
                html_content = f.read()

            page_records = extract_records_from_html(html_content)
            records.extend(page_records)
            print(f"  ✅ Extracted {len(page_records)} records\n")

        except Exception as e:
            print(f"  ❌ Error: {e}\n")
            continue

    return records


# Main execution
def main(use_existing_html=False):
    """
    EXECUTION FLOW:
    ================
    Phase 1: Extract Records
        1. Call BASE_URL
        2. Extract all records from page 1 (title, dates, metadata, etc.)
        3. Navigate to next page and extract records
        4. Continue until cutoff date is reached
        5. Save all records to JSON

    Phase 2: Match with Deals
        6. Load all extracted records
        7. For each record, match title with deals using LLM
        8. If match found, save to MongoDB under 'uk_cma_cases' node

    Args:
        use_existing_html: If True, extract from existing HTML files instead of scraping
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
    load_deals(include_cma_cases=False)

    print(f"\n{'='*60}")
    print(f"🚀 PHASE 1: EXTRACT ALL CMA MERGER CASE RECORDS")
    print(f"{'='*60}\n")

    if use_existing_html:
        # Extract from existing HTML files
        print("📂 Mode: Using existing HTML files\n")
        all_extracted_records = extract_from_existing_html_files()
    else:
        # Scrape new pages
        print("🌐 Mode: Scraping CMA website\n")

        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                # Step 1: Call BASE_URL
                print(f"📍 Step 1: Calling BASE_URL")
                print(f"   URL: {BASE_URL}")
                page.goto(BASE_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                print(f"   ✅ Loaded\n")

                # Start from page 1 and extract records sequentially
                page_num = 1
                while True:
                    # Extract records from current page
                    page_records, should_stop, has_next = extract_page_records(
                        page, page_num)
                    all_extracted_records.extend(page_records)

                    # Check if we should stop
                    if should_stop:
                        print(f"\n✅ Stopped: Cutoff date reached")
                        break

                    if not has_next:
                        print(f"\n✅ Stopped: No more pages")
                        break

                    # Navigate to next page
                    try:
                        print(f"\n➡️  Navigating to page {page_num + 1}...")
                        next_link = page.query_selector('a[rel="next"]')
                        if not next_link:
                            # Try finding "Next page" text link
                            next_link = page.get_by_text(
                                "Next page", exact=False).first
                        if not next_link:
                            # Try finding pagination links
                            pagination_links = page.query_selector_all(
                                'nav a, .pagination a, .gem-c-pagination a')
                            for link in pagination_links:
                                link_text = link.inner_text().lower()
                                if "next" in link_text or ">" in link_text:
                                    next_link = link
                                    break

                        if next_link:
                            next_link.click()
                            page.wait_for_timeout(2000)
                            page_num += 1
                        else:
                            # Try URL-based pagination as fallback
                            next_url = f"{BASE_URL}&page={page_num + 1}"
                            try:
                                page.goto(
                                    next_url, wait_until="domcontentloaded")
                                page.wait_for_timeout(2000)
                                page_num += 1
                            except:
                                print(f"\n✅ Stopped: No next page found")
                                break
                    except Exception as e:
                        print(f"\n⚠️ Pagination error: {e}")
                        # Try URL-based pagination as fallback
                        try:
                            next_url = f"{BASE_URL}&page={page_num + 1}"
                            page.goto(next_url, wait_until="domcontentloaded")
                            page.wait_for_timeout(2000)
                            page_num += 1
                        except:
                            break

            except Exception as e:
                print(f"\n❌ Scraping error: {e}")
            finally:
                browser.close()

    # Step 5: Save all extracted records to JSON
    print(f"\n{'='*60}")
    print(f"📍 Step 5: SAVE EXTRACTED RECORDS TO JSON")
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
    print(f"📍 Step 6-8: Match titles with deals using LLM\n")

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
    import sys

    # Check command line arguments
    use_existing = False
    if len(sys.argv) > 1:
        if sys.argv[1] in ["--use-html", "-h", "--html"]:
            use_existing = True
            print("📂 Mode: Extract from existing HTML files")
        elif sys.argv[1] in ["--help"]:
            print("\nUsage: python cma_mergers_scraper.py [OPTIONS]")
            print("\nOptions:")
            print("  --use-html, -h    Extract from existing HTML files (no scraping)")
            print("  --help            Show this help message")
            print("\nDefault: Scrape new pages from CMA website\n")
            sys.exit(0)

    if not use_existing:
        print("🌐 Mode: Scrape new pages from CMA website")

    main(use_existing_html=use_existing)
