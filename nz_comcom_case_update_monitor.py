"""
NZ ComCom Case Update Monitor
=============================
Monitors NZ Commerce Commission cases stored in deals (nz_cases).
Fetches each case's details_url, compares case details / timeline / documents / updates_media
with DB, detects new or changed records, sends notification and updates DB.
Reference: accc_case_update_monitor.py
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from bson import ObjectId
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from mongodb_connection import (
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from nz_comcom_case_register import fetch_case_detail_page

load_dotenv(".env")

# Constants
ENV_PATH = ".env"
HTML_OUTPUT_DIR = "nz_comcom_case_updates"


def get_deals_with_nz_cases() -> List[Dict[str, Any]]:
    """Fetch deals from MongoDB that have 'nz_cases' node (non-empty array), limited to active/open deals."""
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
                {"nz_cases": {"$exists": True, "$ne": [], "$type": "array"}},
            ]
        }
        all_deals = list(collection.find(query))

        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        print(f"✅ Fetched {len(all_deals)} deals with nz_cases from MongoDB")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        return []


def _normalize_value(value: Any) -> Any:
    """Normalize a value for comparison."""
    if isinstance(value, str):
        return value.strip() if value else None
    return value


def _timeline_key(entry: Dict[str, Any]) -> str:
    """Unique key for a timeline entry (date + title)."""
    return f"{entry.get('date', '')}|{entry.get('title', '')}"


def _document_key(entry: Dict[str, Any]) -> str:
    """Unique key for a document (title + url)."""
    return f"{entry.get('title', '')}|{entry.get('url', '')}"


def _media_key(entry: Dict[str, Any]) -> str:
    """Unique key for updates_media entry (date + title + url)."""
    return f"{entry.get('date', '')}|{entry.get('title', '')}|{entry.get('url', '')}"


def detect_changes(
    old_case: Dict[str, Any], current_info: Dict[str, Any]
) -> List[Tuple[str, Any, Any, str]]:
    """
    Detect changes in case details, timeline, documents, updates_media.

    Returns:
        List of (field_name, old_value, new_value, change_type)
        change_type: 'new' | 'updated' | 'removed'
    """
    changes: List[Tuple[str, Any, Any, str]] = []

    # --- Case details (key-value comparison) ---
    old_details = old_case.get("case_details") or {}
    new_details = current_info.get("case_details") or {}
    all_keys = set(old_details.keys()) | set(new_details.keys())
    detail_changes = []
    for key in sorted(all_keys):
        old_val = _normalize_value(old_details.get(key))
        new_val = _normalize_value(new_details.get(key))
        if old_val is None and new_val is not None:
            detail_changes.append((key, None, new_val, "new"))
        elif old_val is not None and new_val is None:
            detail_changes.append((key, old_val, None, "removed"))
        elif old_val != new_val:
            detail_changes.append((key, old_val, new_val, "updated"))
    if detail_changes:
        changes.append(("Case details", old_details, new_details, "updated"))

    # --- Timeline: new records not in DB ---
    old_timeline = old_case.get("timeline") or []
    new_timeline = current_info.get("timeline") or []
    old_timeline_keys = {_timeline_key(e) for e in old_timeline}
    new_timeline_entries = [
        e for e in new_timeline if _timeline_key(e) not in old_timeline_keys
    ]
    if new_timeline_entries:
        changes.append(
            ("Timeline", None, new_timeline_entries, "new")
        )

    # --- Documents: new records not in DB ---
    old_docs = old_case.get("documents") or []
    new_docs = current_info.get("documents") or []
    old_doc_keys = {_document_key(d) for d in old_docs}
    new_doc_entries = [
        d for d in new_docs if _document_key(d) not in old_doc_keys]
    if new_doc_entries:
        changes.append(
            ("Documents", None, new_doc_entries, "new")
        )

    # --- Updates/Media: new records not in DB ---
    old_media = old_case.get("updates_media") or []
    new_media = current_info.get("updates_media") or []
    old_media_keys = {_media_key(m) for m in old_media}
    new_media_entries = [
        m for m in new_media if _media_key(m) not in old_media_keys
    ]
    if new_media_entries:
        changes.append(
            ("Updates/Media", None, new_media_entries, "new")
        )

    return changes


def _summary_changes(changes: List[Tuple[str, Any, Any, str]]) -> List[str]:
    """Reduce to high-level change summary (avoid duplicate 'Case details: X' in banner)."""
    seen_top = set()
    summary = []
    for field_name, _old, _new, change_type in changes:
        if field_name == "Case details":
            if "Case details" not in seen_top:
                summary.append("Case details")
                seen_top.add("Case details")
            continue
        if field_name.startswith("Case details:"):
            if "Case details" not in seen_top:
                summary.append("Case details")
                seen_top.add("Case details")
            continue
        if field_name not in seen_top:
            summary.append(field_name)
            seen_top.add(field_name)
    return summary


def generate_nz_update_email_html(
    case_info: Dict[str, Any],
    deal_match: Dict[str, Any],
    changes: List[Tuple[str, Any, Any, str]],
) -> str:
    """Generate HTML email for NZ ComCom case update notification."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    title = case_info.get("title", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("case_details") or {}
    description = case_info.get("description", "")
    case_number = details.get("Case number", "N/A")

    change_summary = _summary_changes(changes)
    changed_fields = {c[0]: (c[3], c[2]) for c in changes}

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>NZ ComCom Case Update - {case_number}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:28px 26px 40px 26px;">

<!-- Update Banner -->
<div style="background:#fef2f2;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #ef4444;">
<div style="font-size:16px;font-weight:800;color:#dc2626;margin-bottom:8px;">⚠️ NZ ComCom Case Updated</div>
<div style="font-size:14px;color:#991b1b;">
Changed: {', '.join(change_summary)}
</div>
</div>

<!-- Deal Match -->
<div style="background:#e0f2fe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #0284c7;">
<div style="font-size:15px;font-weight:800;color:#0369a1;margin-bottom:6px;">Matched Deal</div>
<div style="font-size:14px;color:#0c4a6e;">
<span style="font-weight:700;">Acquirer:</span> {acquirer} <span style="color:#94a3b8;margin:0 8px;">|</span>
<span style="font-weight:700;">Target:</span> {target}
</div>"""
    if detail_url:
        html += f'''
<div style="margin-top:10px;">
<a href="{detail_url}" target="_blank" style="color:#0284c7;text-decoration:none;font-weight:700;font-size:14px;">View NZ ComCom case →</a>
</div>'''
    html += '''
</div>

<!-- Case title & description -->
<h2 style="font-size:18px;margin:0 0 12px 0;">''' + title + '''</h2>
<p style="margin:0 0 20px 0;line-height:1.5;">''' + (description or "—") + '''</p>

<!-- Case Details -->
<h3 style="font-size:16px;margin:20px 0 10px 0;">Case Details</h3>
<div style="background:#f8fafc;border-radius:6px;padding:14px;">
<table style="width:100%;border-collapse:collapse;">'''

    # If "Case details" changed, we don't have per-field flags; show full table
    case_details_changed = "Case details" in changed_fields
    for key, value in (details or {}).items():
        flag = ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;">(updated)</span>' if case_details_changed else ""
        html += f"""
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">{key}</td><td style="padding:6px 0;">{value or '—'}{flag}</td></tr>"""
    html += """
</table>
</div>"""

    # Timeline (if we have it and possibly new entries)
    timeline = case_info.get("timeline") or []
    if timeline:
        new_timeline_list = []
        if "Timeline" in changed_fields:
            _, new_timeline_list = changed_fields["Timeline"]
        new_timeline_keys = {_timeline_key(e)
                             for e in (new_timeline_list or [])}
        html += """
<h3 style="font-size:16px;margin:24px 0 10px 0;">Timeline</h3>
<div style="padding-top:8px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>"""
        for entry in timeline:
            date_str = entry.get("date", "N/A")
            tit = entry.get("title", "N/A")
            is_new = _timeline_key(entry) in new_timeline_keys
            new_flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;">(new)</span>' if is_new else ""
            html += f"""
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px 12px 0;vertical-align:top;width:120px;color:#6b7280;font-size:14px;">{date_str}</td>
<td style="padding:12px 8px;vertical-align:top;">{tit}{new_flag}</td>
</tr>"""
        html += """
</tbody>
</table>
</div>"""

    # Documents
    documents = case_info.get("documents") or []
    if documents:
        new_doc_list = []
        if "Documents" in changed_fields:
            _, new_doc_list = changed_fields["Documents"]
        new_doc_keys = {_document_key(d) for d in (new_doc_list or [])}
        html += """
<h3 style="font-size:16px;margin:24px 0 10px 0;">Documents</h3>
<div style="padding-top:8px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>"""
        for doc in documents:
            tit = doc.get("title", "N/A")
            url = doc.get("url", "")
            is_new = _document_key(doc) in new_doc_keys
            new_flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;">(new)</span>' if is_new else ""
            link = f'<a href="{url}" target="_blank" style="color:#2563eb;">{tit}</a>' if url else tit
            html += f"""
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px;">{link}{new_flag}</td>
</tr>"""
        html += """
</tbody>
</table>
</div>"""

    # Updates/Media
    updates_media = case_info.get("updates_media") or []
    if updates_media:
        new_media_list = []
        if "Updates/Media" in changed_fields:
            _, new_media_list = changed_fields["Updates/Media"]
        new_media_keys = {_media_key(m) for m in (new_media_list or [])}
        html += """
<h3 style="font-size:16px;margin:24px 0 10px 0;">Updates / Media</h3>
<div style="padding-top:8px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>"""
        for m in updates_media:
            date_str = m.get("date", "N/A")
            tit = m.get("title", "N/A")
            url = m.get("url", "")
            is_new = _media_key(m) in new_media_keys
            new_flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;">(new)</span>' if is_new else ""
            link = f'<a href="{url}" target="_blank" style="color:#2563eb;">{tit}</a>' if url else tit
            html += f"""
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px 12px 0;vertical-align:top;width:120px;color:#6b7280;font-size:14px;">{date_str}</td>
<td style="padding:12px 8px;">{link}{new_flag}</td>
</tr>"""
        html += """
</tbody>
</table>
</div>"""

    html += """
</div>
</body>
</html>"""
    return html


def save_html_file(case_number: str, html_content: str) -> str:
    """Save HTML content to a file."""
    os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)
    safe_case_number = (case_number or "unknown").replace(
        ".", "_").replace("/", "_")
    filename = f"nz_{safe_case_number}_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(HTML_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"      💾 Saved HTML to: {filepath}")
        return filepath
    except Exception as e:
        print(f"      ❌ Error saving HTML file: {e}")
        return ""


def send_nz_update_email_via_webhook(
    case_info: Dict[str, Any],
    deal_match: Dict[str, Any],
    html_content: str,
    changes: List[Tuple[str, Any, Any, str]],
) -> bool:
    """Send NZ ComCom update email via n8n webhook."""
    try:
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")
        case_number = (case_info.get("case_details")
                       or {}).get("Case number", "N/A")

        subject = f"NZ ComCom Case Update – {case_number}: {target} / {acquirer}"
        print(f"      📝 Subject: {subject}")

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
            "case_number": case_number,
            "case_title": case_info.get("title", "N/A"),
            "changed_fields": _summary_changes(changes),
            "case_url": case_info.get("detail_url", ""),
            "source": "nz_comcom_update",
        }
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        print(f"      ✅ Email sent via webhook ({response.status_code})")
        return True
    except Exception as e:
        print(f"      ⚠️ Error sending email via webhook: {e}")
        return False


def update_case_in_db(
    deal_id: str,
    case_identifier: str,
    updated_case_data: Dict[str, Any],
) -> bool:
    """Update the NZ case in the deal's nz_cases array. Identify case by Case number or detail_url."""
    try:
        if not is_connected():
            print("      ⚠️ MongoDB connection not available")
            return False

        collection = get_deals_collection()
        if collection is None:
            return False

        deal_id_obj = ObjectId(deal_id)
        deal = collection.find_one({"_id": deal_id_obj})
        if not deal or "nz_cases" not in deal:
            print("      ⚠️ Deal not found or has no nz_cases")
            return False

        updated_details = updated_case_data.get("case_details") or {}
        case_number = updated_details.get("Case number")
        detail_url = updated_case_data.get("detail_url", "")

        updated = False
        for i, existing in enumerate(deal["nz_cases"]):
            existing_number = (existing.get("case_details")
                               or {}).get("Case number")
            existing_url = existing.get("detail_url", "")
            if case_number and existing_number == case_number:
                deal["nz_cases"][i] = updated_case_data
                updated = True
                break
            if detail_url and existing_url == detail_url:
                deal["nz_cases"][i] = updated_case_data
                updated = True
                break

        if not updated:
            print(
                f"      ⚠️ Case not found in deal nz_cases (case_number={case_identifier})")
            return False

        result = collection.update_one(
            {"_id": deal_id_obj},
            {"$set": {"nz_cases": deal["nz_cases"]}},
        )
        if result.modified_count > 0:
            print("      ✅ Updated case in database")
        return True
    except Exception as e:
        print(f"      ❌ Error updating database: {e}")
        import traceback
        traceback.print_exc()
        return False


def process_nz_case_updates() -> None:
    """Main: fetch deals with nz_cases, call details_url, detect changes, notify and update DB."""
    print("🚀 NZ ComCom Case Update Monitor\n")

    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        return
    print(f"✅ {message}\n")

    print("📊 Loading deals with nz_cases from MongoDB...")
    deals = get_deals_with_nz_cases()
    if not deals:
        print("⚠️ No deals with nz_cases found. Exiting.")
        return
    print(f"✅ Found {len(deals)} deals with nz_cases\n")

    total_cases_checked = 0
    total_cases_updated = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for deal_idx, deal in enumerate(deals, 1):
            deal_id = deal.get("deal_id", "")
            nz_cases = deal.get("nz_cases", [])
            acquirer = deal.get("acquire_name") or deal.get("acquirer", "N/A")
            target = deal.get("target_name") or deal.get("target", "N/A")

            print(f"\n[{deal_idx}/{len(deals)}] Deal: {acquirer} / {target}")
            print(f"   Deal ID: {deal_id} | NZ cases: {len(nz_cases)}")

            for case_idx, existing_case in enumerate(nz_cases, 1):
                total_cases_checked += 1
                detail_url = existing_case.get("detail_url")
                title = existing_case.get("title", "N/A")
                case_number = (existing_case.get("case_details")
                               or {}).get("Case number", "")

                if not detail_url:
                    print(
                        f"   [{case_idx}/{len(nz_cases)}] No detail_url, skipping")
                    continue

                print(
                    f"\n   [{case_idx}/{len(nz_cases)}] {title or case_number}")
                print(f"      📄 Fetching: {detail_url}")

                try:
                    current_info = fetch_case_detail_page(page, detail_url)
                    if not current_info:
                        print("      ⚠️ Could not fetch current info, skipping")
                        continue

                    changes = detect_changes(existing_case, current_info)

                    if not changes:
                        print("      ✅ No changes detected")
                        continue

                    print(f"      🔄 Changes detected ({len(changes)} item(s))")
                    for field_name, old_val, new_val, change_type in changes:
                        if field_name == "Case details":
                            print(f"         • Case details: updated")
                            continue
                        if field_name == "Timeline" and isinstance(new_val, list):
                            print(
                                f"         • Timeline: {len(new_val)} new entry(ies)")
                            continue
                        if field_name == "Documents" and isinstance(new_val, list):
                            print(
                                f"         • Documents: {len(new_val)} new document(s)")
                            continue
                        if field_name == "Updates/Media" and isinstance(new_val, list):
                            print(
                                f"         • Updates/Media: {len(new_val)} new entry(ies)")
                            continue
                        print(f"         • {field_name}: {change_type}")

                    # Build updated case (merge current into existing, replacing lists with full current)
                    updated_case = dict(existing_case)
                    updated_case["description"] = current_info.get(
                        "description", updated_case.get("description"))
                    updated_case["case_details"] = current_info.get(
                        "case_details") or updated_case.get("case_details")
                    updated_case["timeline"] = current_info.get("timeline", [])
                    updated_case["documents"] = current_info.get(
                        "documents", [])
                    updated_case["updates_media"] = current_info.get(
                        "updates_media", [])

                    html_content = generate_nz_update_email_html(
                        updated_case, deal, changes)
                    save_html_file(case_number or "nz_case", html_content)
                    send_nz_update_email_via_webhook(
                        updated_case, deal, html_content, changes)

                    if update_case_in_db(deal_id, case_number or detail_url, updated_case):
                        total_cases_updated += 1

                except Exception as e:
                    print(f"      ❌ Error: {e}")
                    import traceback
                    traceback.print_exc()

        browser.close()

    print(f"\n{'='*60}")
    print("📊 Summary:")
    print(f"   Total cases checked: {total_cases_checked}")
    print(f"   Cases updated: {total_cases_updated}")
    print(f"   HTML saved to: {HTML_OUTPUT_DIR}/")
    print(f"{'='*60}\n")
    print("🎉 Done!")


if __name__ == "__main__":
    process_nz_case_updates()
