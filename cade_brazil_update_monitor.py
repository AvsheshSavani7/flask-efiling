from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv
import datetime
import time
import requests
import json
import os
from bs4 import BeautifulSoup
import re
from mongodb_connection import get_deals_collection, is_connected
from cade_public_notice_brazil import (
    extract_table_data_from_detail_page,
    send_brazil_email_via_webhook,
    generate_brazil_email_html,
    convert_datetime_to_string,
    translate_specific_fields_only
)
from bson import ObjectId

# Load environment variables
load_dotenv(".env")


def get_deals_with_brazil_node():
    """
    Fetch all deals from MongoDB that have a 'brazil' node.

    Returns:
        List of deal dictionaries with brazil data
    """
    try:
        collection = get_deals_collection()

        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        # Query for deals that have 'brazil' node
        query = {"brazil": {"$exists": True}}

        # Fetch documents from the deals collection
        all_deals = list(collection.find(query))

        # Convert _id to string for JSON serialization
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal["_id_object"] = deal["_id"]  # Keep ObjectId for updates

        print(
            f"✅ Fetched {len(all_deals)} deals with Brazil node from MongoDB")
        return all_deals

    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def compare_table_records(existing_records, new_records):
    """
    Compare existing table records with new records to find new entries.

    Args:
        existing_records: List of existing table records from MongoDB
        new_records: List of newly scraped table records

    Returns:
        List of new records that don't exist in existing_records
    """
    if not existing_records:
        return new_records

    if not new_records:
        return []

    # Create a set of unique identifiers for existing records
    # Use documento_processo and data_documento as unique identifier
    existing_ids = set()
    for record in existing_records:
        doc_process = record.get("documento_processo") or record.get(
            "document_process", "")
        doc_date = record.get("data_documento") or record.get(
            "document_date", "")
        if doc_process:
            existing_ids.add(f"{doc_process}|{doc_date}")

    # Find new records
    new_found = []
    for record in new_records:
        doc_process = record.get("documento_processo") or record.get(
            "document_process", "")
        doc_date = record.get("data_documento") or record.get(
            "document_date", "")
        if doc_process:
            record_id = f"{doc_process}|{doc_date}"
            if record_id not in existing_ids:
                new_found.append(record)

    return new_found


def update_deal_with_new_records(deal_id_obj, new_records, all_records):
    """
    Update MongoDB deal with new table records.

    Args:
        deal_id_obj: ObjectId of the deal
        new_records: List of new records to add
        all_records: Complete list of all table records (existing + new)

    Returns:
        bool: True if update successful, False otherwise
    """
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ Deals collection not available")
            return False

        # Translate new records before saving
        translated_records = []
        for record in all_records:
            if isinstance(record, dict):
                translated_record = record.copy()
                # Translate 'tipo_documento' to 'document_type'
                if "tipo_documento" in translated_record:
                    doc_type = translated_record["tipo_documento"]
                    if isinstance(doc_type, str) and doc_type.strip():
                        from cade_public_notice_brazil import translate_to_english
                        translated_record["document_type"] = translate_to_english(
                            doc_type)
                        translated_record.pop("tipo_documento", None)
                translated_records.append(translated_record)
            else:
                translated_records.append(record)

        # Update the deal's brazil.table_records with the complete list
        update_result = collection.update_one(
            {"_id": deal_id_obj},
            {
                "$set": {
                    "brazil.table_records": translated_records,
                    "brazil.last_updated": datetime.datetime.now().isoformat()
                }
            }
        )

        if update_result.modified_count > 0:
            print(f"✅ Updated deal with {len(new_records)} new table records")
            return True
        else:
            print(f"⚠️ No changes made to deal (update may have failed)")
            return False

    except Exception as e:
        print(f"❌ Error updating deal: {e}")
        import traceback
        traceback.print_exc()
        return False


def generate_update_email_html(brazil_data, deal_info, new_records):
    """
    Generate HTML email for Brazil deal update notification.

    Args:
        brazil_data: The Brazil data dictionary
        deal_info: The deal information
        new_records: List of new table records found

    Returns:
        Tuple of (subject, html_email)
    """
    from html import escape as escape_html

    # Extract deal information
    target = deal_info.get("target") or deal_info.get("target_name", "N/A")
    acquirer = deal_info.get("acquirer") or deal_info.get(
        "acquire_name", "N/A")
    deal_id = deal_info.get("deal_id", "N/A")

    # Extract Brazil data
    process = brazil_data.get("process", "N/A")
    type_text = brazil_data.get("type", "N/A")
    detail_url = brazil_data.get("detail_url", "")

    # Count new records
    new_records_count = len(new_records) if new_records else 0

    # Generate table records HTML for NEW records only
    table_records_html = ""
    if new_records and len(new_records) > 0:
        table_records_html = """
    <table style="width:100%; border-collapse:collapse; margin-top:10px;">
      <thead>
        <tr style="background-color:#f5f5f5;">
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Document Process</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Document Type</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Document Date</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Registration Date</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Unit</th>
        </tr>
      </thead>
      <tbody>
"""
        for idx, record in enumerate(new_records):
            # Highlighted yellow background for new records
            bg = "#fffacd" if idx % 2 == 0 else "#fff9b3"
            doc_process = escape_html(
                str(record.get("document_process", record.get("documento_processo", ""))))
            doc_type = escape_html(
                str(record.get("document_type", record.get("tipo_documento", ""))))
            doc_date = escape_html(
                str(record.get("document_date", record.get("data_documento", ""))))
            reg_date = escape_html(
                str(record.get("registration_date", record.get("data_registro", ""))))
            unit = escape_html(
                str(record.get("unit", record.get("unidade", ""))))

            # Build document URL if available
            doc_url = record.get("document_url", "")
            if doc_url:
                doc_process_html = f'<a href="{escape_html(doc_url)}" style="color:#4a90e2; text-decoration:none;" target="_blank">{doc_process}</a>'
            else:
                doc_process_html = doc_process

            table_records_html += f"""
      <tr style="background-color:{bg};">
        <td style="padding:8px; border:1px solid #ddd;">{doc_process_html}</td>
        <td style="padding:8px; border:1px solid #ddd;">{doc_type}</td>
        <td style="padding:8px; border:1px solid #ddd;">{doc_date}</td>
        <td style="padding:8px; border:1px solid #ddd;">{reg_date}</td>
        <td style="padding:8px; border:1px solid #ddd;">{unit}</td>
      </tr>
"""
        table_records_html += """
      </tbody>
    </table>
"""
    else:
        table_records_html = "<p><em>No new records found.</em></p>"

    title_text = f"CADE Update – {target} / {acquirer}" if target != "N/A" and acquirer != "N/A" else f"CADE Brazil Update – Process {process}"
    subject = f"Brazil Update – {new_records_count} New Record(s) – {target} / {acquirer}"

    html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape_html(subject)}</title>
</head>
<body style="margin:0; padding:0; font-family:Arial,sans-serif; background-color:#f4f4f4;">
  <div style="max-width:900px; margin:20px auto; background-color:#ffffff; padding:30px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
    <div style="background-color:#ff9800; color:#ffffff; padding:15px; border-radius:5px; margin-bottom:20px; text-align:center;">
      <h2 style="margin:0; font-size:20px;"> NEW RECORDS DETECTED</h2>
    </div>
    
    <h2 style="color:#333; text-align:center; margin-top:0; padding-bottom:20px; border-bottom:3px solid #ff9800;">
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
        <td style="padding:8px; font-weight:bold; color:#555;">Process:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(process))}</td>
      </tr>
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Type:</td>
        <td style="padding:8px; color:#333;">{escape_html(str(type_text))}</td>
      </tr>
      <tr style="background-color:#f9f9f9;">
        <td style="padding:8px; font-weight:bold; color:#555;">New Records Found:</td>
        <td style="padding:8px; color:#333; font-weight:bold; font-size:18px; color:#ff9800;">{escape_html(str(new_records_count))}</td>
      </tr>
"""

    if detail_url:
        html_email += f"""
      <tr>
        <td style="padding:8px; font-weight:bold; color:#555;">Detail URL:</td>
        <td style="padding:8px;">
          <a href="{escape_html(detail_url)}" style="color:#4a90e2; text-decoration:none;" target="_blank">
            View CADE Detail Page
          </a>
        </td>
      </tr>
"""

    html_email += f"""
    </table>

    <h3 style="color:#333; margin-top:20px; margin-bottom:10px;">🆕 New Table Records</h3>
    <p style="color:#666; font-style:italic; margin-bottom:15px;">The following records were not present in the previous check:</p>
    {table_records_html}

    <div style="margin-top:30px; padding-top:20px; border-top:1px solid #e0e0e0; text-align:center; color:#999; font-size:12px;">
      <p>This is an automated email generated from CADE Brazil deal update monitoring.</p>
      <p>Update detected at: {escape_html(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"))}</p>
    </div>
  </div>
</body>
</html>
"""

    return subject, html_email


def send_update_email_via_webhook(brazil_data, deal_info, new_records):
    """
    Send email notification for Brazil deal updates.

    Args:
        brazil_data: The Brazil data dictionary
        deal_info: The deal information
        new_records: List of new table records found

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate email HTML
        subject, html_email = generate_update_email_html(
            brazil_data, deal_info, new_records)
        print(f"📝 Generated update email subject: {subject}")

        # Get n8n webhook URL from environment variable
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")
        print(f"📤 Sending update email via n8n webhook: {webhook_url}")

        # Extract deal information for payload
        target = deal_info.get("target") or deal_info.get("target_name", "N/A")
        acquirer = deal_info.get("acquirer") or deal_info.get(
            "acquire_name", "N/A")
        deal_id = deal_info.get("deal_id", "N/A")

        # Prepare payload for n8n webhook
        payload = {
            'subject': subject,
            'html': html_email,
            'deal_id': deal_id,
            'target': target,
            'acquirer': acquirer,
            'process': brazil_data.get("process", "N/A"),
            'type': brazil_data.get("type", "N/A"),
            'detail_url': brazil_data.get("detail_url", ""),
            'new_records_count': len(new_records),
            'update_type': 'brazil_deal_update'
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
            f"✅ Update email sent successfully! Status: {response.status_code}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error sending update email via webhook: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error generating/sending update email: {e}")
        import traceback
        traceback.print_exc()
        return False


def monitor_brazil_deals(headless=True):
    """
    Monitor all deals with Brazil node for updates in their table records.

    Args:
        headless: bool, whether to run browser in headless mode (default: True)

    Returns:
        dict: {
            "success": bool,
            "total_deals_checked": int,
            "deals_with_updates": int,
            "total_new_records": int,
            "updated_deals": list,
            "error": str (if failed)
        }
    """
    print("=" * 80)
    print("🔍 CADE Brazil Deal Update Monitor")
    print(f"🖥️  Environment: {'HEADLESS' if headless else 'VISIBLE'}")
    print(
        f"🔑 2Captcha API Key: {'SET' if os.getenv('2CAPTCHA_API_KEY') or os.getenv('CAPTCHA_API_KEY') else 'NOT SET'}")
    print("=" * 80)

    # Get all deals with Brazil node
    deals = get_deals_with_brazil_node()

    if not deals:
        return {
            "success": False,
            "error": "No deals with Brazil node found",
            "total_deals_checked": 0,
            "deals_with_updates": 0,
            "total_new_records": 0,
            "updated_deals": []
        }

    print(f"\n📊 Found {len(deals)} deals with Brazil node to monitor")

    updated_deals = []
    total_new_records_count = 0

    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Set up dialog handler
        context.on("dialog", lambda dialog: dialog.accept())

        page = context.new_page()

        try:
            # Process each deal
            for idx, deal in enumerate(deals):
                deal_num = idx + 1
                print(f"\n{'=' * 80}")
                print(f"📋 Processing deal {deal_num}/{len(deals)}")
                print(f"{'=' * 80}")

                brazil_data = deal.get("brazil", {})
                detail_url = brazil_data.get("detail_url")

                if not detail_url:
                    print(f"⚠️ No detail_url found for deal, skipping...")
                    continue

                # Get deal info
                target = deal.get("target") or deal.get("target_name", "N/A")
                acquirer = deal.get("acquirer") or deal.get(
                    "acquire_name", "N/A")
                deal_id = deal.get("deal_id", "N/A")
                process = brazil_data.get("process", "N/A")

                print(f"🎯 Target: {target}")
                print(f"🎯 Acquirer: {acquirer}")
                print(f"📄 Process: {process}")
                print(f"🔗 Detail URL: {detail_url[:80]}...")

                # Get existing table records
                existing_records = brazil_data.get("table_records", [])
                print(
                    f"📊 Existing table records in MongoDB: {len(existing_records)}")

                # DETAILED LOGGING - Show what's in MongoDB
                if existing_records:
                    print(f"📝 Existing records in MongoDB:")
                    for rec in existing_records:
                        doc_id = rec.get("documento_processo") or rec.get(
                            "document_process", "N/A")
                        doc_date = rec.get("data_documento") or rec.get(
                            "document_date", "N/A")
                        print(f"   - {doc_id} ({doc_date})")

                # Scrape current table records from detail page
                print(f"🌐 Scraping current table records from detail page...")
                try:
                    current_records = extract_table_data_from_detail_page(
                        page, context, detail_url)
                    print(
                        f"📊 Current table records found: {len(current_records)}")

                    # DETAILED LOGGING - Show what was actually scraped
                    if current_records:
                        print(f"📝 Scraped records details:")
                        for rec in current_records:
                            doc_id = rec.get("documento_processo", "N/A")
                            doc_date = rec.get("data_documento", "N/A")
                            print(f"   - {doc_id} ({doc_date})")
                    else:
                        print(f"⚠️ WARNING: No records scraped from detail page!")

                except Exception as e:
                    print(f"❌ Error scraping detail page: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

                # Compare records
                new_records = compare_table_records(
                    existing_records, current_records)

                if new_records and len(new_records) > 0:
                    print(f"\n🆕 FOUND {len(new_records)} NEW RECORD(S)!")
                    print("-" * 80)

                    for record in new_records:
                        doc_process = record.get("documento_processo", "N/A")
                        doc_type = record.get("tipo_documento", "N/A")
                        doc_date = record.get("data_documento", "N/A")
                        print(f"  • {doc_process} - {doc_type} ({doc_date})")

                    print("-" * 80)

                    # Update MongoDB with new records (merge with existing)
                    all_records = existing_records + new_records
                    update_success = update_deal_with_new_records(
                        deal.get("_id_object"),
                        new_records,
                        all_records
                    )

                    if update_success:
                        # Send email notification
                        email_success = send_update_email_via_webhook(
                            brazil_data,
                            deal,
                            new_records
                        )

                        updated_deals.append({
                            "deal_id": deal_id,
                            "target": target,
                            "acquirer": acquirer,
                            "process": process,
                            "new_records_count": len(new_records),
                            "email_sent": email_success
                        })
                        total_new_records_count += len(new_records)
                    else:
                        print(f"⚠️ Failed to update MongoDB")
                else:
                    print(f"✅ No new records found (up to date)")

                # Small delay between deals
                time.sleep(2)

            browser.close()

            # Summary
            print(f"\n{'=' * 80}")
            print(f"✅ MONITORING COMPLETED")
            print(f"{'=' * 80}")
            print(f"📊 Total deals checked: {len(deals)}")
            print(f"🆕 Deals with updates: {len(updated_deals)}")
            print(f"📝 Total new records found: {total_new_records_count}")

            return {
                "success": True,
                "total_deals_checked": len(deals),
                "deals_with_updates": len(updated_deals),
                "total_new_records": total_new_records_count,
                "updated_deals": updated_deals,
                "timestamp": datetime.datetime.now().isoformat()
            }

        except Exception as e:
            error_msg = str(e)
            print(f"❌ Error in monitoring: {error_msg}")
            import traceback
            traceback.print_exc()
            browser.close()
            return {
                "success": False,
                "error": error_msg,
                "total_deals_checked": 0,
                "deals_with_updates": 0,
                "total_new_records": 0,
                "updated_deals": []
            }


if __name__ == "__main__":
    # Run monitor with headless=False for testing
    result = monitor_brazil_deals(headless=False)
    print("\n" + "=" * 80)
    print("FINAL RESULT:")
    print("=" * 80)
    print(json.dumps(result, indent=2))
