"""
New FS Cases Update Monitor (Playwright-based)
===============================================

Monitors open Foreign Subsidies cases in the 'fs_cases' MongoDB collection
for changes by scraping fresh data from each case's detail page with Playwright.

Flow:
1. Fetch all open/unknown deals from MongoDB
2. Fetch all fs_cases where is_open == True
3. For each case: open detail page via Playwright, parse HTML in memory
4. Compare ALL data fields (excluding tracking fields) with DB record
5. If changes found:
   - If last_decision_date is a real date (not "none"/empty) → set is_open: false
   - If deal_id present → generate update email with change highlights → send [FRMD]
   - If no deal_id → LLM match deal
     -> matched → email + deal banner → send [FRMD] → add deal_id
     -> not matched → LLM USA check
        -> USA → email → send [FRUD]
        -> not USA → no email
   - Always update DB record with fresh data
6. If no changes → skip

Run:
    python new_fs_cases_html_update_monitor.py
    python new_fs_cases_html_update_monitor.py --headed
    python new_fs_cases_html_update_monitor.py --max-cases 10
"""

from llm_verification_service import verify_usa_relation
from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from openai import OpenAI
from dotenv import load_dotenv
from bson import ObjectId
from playwright.sync_api import sync_playwright
import requests
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone
import argparse
import json
import logging
import builtins
import re
import sys
import os
import time

from fs_html_scraper import parse_case_html
from new_fs_cases_html import match_case_to_deal

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = "new_fs_cases_html_update_monitor.log"

logger = logging.getLogger("new_fs_cases_html_update_monitor")
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    msg = " ".join(str(a) for a in args)
    getattr(logger, level if level in ("error", "warning") else "info")(msg)
    builtins.print(*args, **kwargs)


print = _logged_print  # type: ignore

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ENV_PATH = ".env"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
    # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

# Fields to exclude from comparison:
# - _id, is_open, created_at, updated_at: DB-only metadata
# - deal_id: set by our register/monitor script, never in scraped data
_EXCLUDE_FROM_COMPARE = frozenset({
    "_id", "is_open", "created_at", "updated_at", "deal_id", "instrument", "case_title"
})

SPA_CONTENT_INDICATORS = [
    "text=Companies:",
    "text=Case type:",
    "text=Regulation:",
    "text=Notification date:",
    "text=Last decision date:",
    "text=Initiation date:",
    "text=Investigation phase:",
]

COOKIE_ACCEPT_SELECTORS = [
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept all')",
    "button[id*='cookie'] >> text=Accept",
]


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

def dismiss_cookie_banner(page) -> None:
    for selector in COOKIE_ACCEPT_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=3000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def wait_for_spa_content(page, timeout_s: int = 15) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for indicator in SPA_CONTENT_INDICATORS:
            try:
                loc = page.locator(indicator).first
                if loc.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        page.wait_for_timeout(500)
    return False


def scrape_case_page(context, case_number: str) -> Optional[Dict[str, Any]]:
    url = f"https://competition-cases.ec.europa.eu/cases/{case_number}"
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        dismiss_cookie_banner(page)

        spa_loaded = wait_for_spa_content(page, timeout_s=15)
        if not spa_loaded:
            page.wait_for_timeout(3000)

        html = page.content()
        return parse_case_html(html, case_number)
    except Exception as exc:
        print(
            f"  [ERROR] Failed to scrape {case_number}: {exc}", level="error")
        return None
    finally:
        page.close()


# ---------------------------------------------------------------------------
# FS helpers
# ---------------------------------------------------------------------------

def get_companies_from_title(case: Dict[str, Any]) -> List[str]:
    companies = case.get("companies")
    if companies:
        return companies
    title = case.get("case_title") or ""
    if not title:
        return []
    return [c.strip() for c in title.split(" / ") if c.strip()]


def has_real_decision_date(case: Dict[str, Any]) -> bool:
    """Check if last_decision_date is a real date (not 'none'/null/empty)."""
    val = case.get("last_decision_date")
    if not val:
        return False
    return str(val).strip().lower() not in ("none", "null", "n/a", "")


# ---------------------------------------------------------------------------
# Deep comparison
# ---------------------------------------------------------------------------

def normalize_for_comparison(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: normalize_for_comparison(v) for k, v in data.items()}
    elif isinstance(data, list):
        normalized = [normalize_for_comparison(item) for item in data]
        try:
            if all(isinstance(item, (str, int, float)) for item in normalized):
                normalized = sorted(normalized)
        except Exception:
            pass
        return normalized
    elif isinstance(data, str):
        return data.strip() if data else None
    return data


def deep_compare(old_data: Any, new_data: Any, path: str = "") -> List[Tuple[str, Any, Any]]:
    differences: List[Tuple[str, Any, Any]] = []
    old_n = normalize_for_comparison(old_data)
    new_n = normalize_for_comparison(new_data)

    if old_n is None and new_n is None:
        return differences
    if old_n is None:
        differences.append((path, None, new_data))
        return differences
    if new_n is None:
        differences.append((path, old_data, None))
        return differences

    if type(old_n) != type(new_n):
        differences.append((path, old_data, new_data))
        return differences

    if isinstance(old_n, dict):
        all_keys = set(old_n.keys()) | set(new_n.keys())
        for key in all_keys:
            new_path = f"{path}.{key}" if path else key
            if key not in old_n:
                differences.append((new_path, None, new_data.get(
                    key) if isinstance(new_data, dict) else None))
            elif key not in new_n:
                differences.append((new_path, old_data.get(
                    key) if isinstance(old_data, dict) else None, None))
            else:
                differences.extend(deep_compare(
                    old_n[key], new_n[key], new_path))
    elif isinstance(old_n, list):
        if len(old_n) != len(new_n):
            differences.append((path, old_data, new_data))
        else:
            try:
                old_set = set(old_n)
                new_set = set(new_n)
                if old_set != new_set:
                    for i, (o, n) in enumerate(zip(old_n, new_n)):
                        differences.extend(deep_compare(o, n, f"{path}[{i}]"))
            except (TypeError, ValueError):
                for i, (o, n) in enumerate(zip(old_n, new_n)):
                    differences.extend(deep_compare(o, n, f"{path}[{i}]"))
    else:
        if old_n != new_n:
            differences.append((path, old_data, new_data))

    return differences


def strip_tracking_fields(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in _EXCLUDE_FROM_COMPARE}


def get_field_change_status(field_path: str, differences: List[Tuple[str, Any, Any]]) -> str:
    for diff_path, old_val, new_val in differences:
        if field_path in diff_path:
            return "added" if old_val is None else "updated"
    return "unchanged"


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_fs_cases_collection():
    db = get_database()
    if db is not None:
        return db["fs_cases"]
    return None


def fetch_open_cases(collection) -> List[Dict[str, Any]]:
    try:
        cases = list(collection.find({"is_open": True}))
        print(f"Fetched {len(cases)} open cases from fs_cases")
        return cases
    except Exception as e:
        print(f"Error fetching open cases: {e}", level="error")
        return []


def fetch_deals() -> List[Dict[str, Any]]:
    try:
        deals_coll = get_deals_collection()
        if deals_coll is None:
            return []
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(deals_coll.find(status_filter))
        for d in deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])
        print(f"Fetched {len(deals)} open/unknown deals")
        return deals
    except Exception as e:
        print(f"Error fetching deals: {e}", level="warning")
        return []


def update_case_document(
    collection,
    case_doc: Dict[str, Any],
    new_data: Dict[str, Any],
    extra_fields: Optional[Dict[str, Any]] = None,
) -> bool:
    try:
        _id = case_doc.get("_id")
        if not _id:
            return False

        updated = {**new_data}

        for field in ("case_number", "deal_id", "created_at"):
            if field in case_doc:
                updated[field] = case_doc[field]

        if extra_fields:
            updated.update(extra_fields)

        updated["updated_at"] = utc_now_iso()

        if "is_open" not in updated:
            updated["is_open"] = case_doc.get("is_open", True)

        result = collection.update_one({"_id": _id}, {"$set": updated})
        if result.modified_count > 0:
            print("    Updated case document in DB")
        else:
            print("    No DB changes (already up to date)")
        return True
    except Exception as e:
        print(f"    Error updating case: {e}", level="error")
        return False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Email via webhook
# ---------------------------------------------------------------------------

def send_email_via_webhook(
    subject: str,
    html_content: str,
    case_number: str,
    case_title: str,
    deal_id: Optional[str] = None,
    changed_fields: Optional[List[str]] = None,
) -> bool:
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "case_number": case_number,
            "case_title": case_title,
            "deal_id": deal_id,
            "changed_fields": changed_fields or [],
            "case_instrument": "FS",
            "source": "ec_foreign_subsidies_cases_update",
        }
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"    Email sent! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"    Error sending email: {e}", level="warning")
        return False


# ---------------------------------------------------------------------------
# Email HTML generator with change highlighting (FS-specific: green badges)
# ---------------------------------------------------------------------------

def _highlight(status: str) -> str:
    if status == "updated":
        return 'background-color:#fef3c7;padding:3px 8px;border-radius:4px;border-left:3px solid #f59e0b;'
    elif status == "added":
        return 'background-color:#d1fae5;padding:3px 8px;border-radius:4px;border-left:3px solid #10b981;'
    return ''


def _label_suffix(status: str) -> str:
    if status == "updated":
        return ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;margin-left:4px;">(Updated)</span>'
    if status == "added":
        return ' <span style="color:#10b981;font-size:0.85em;font-weight:700;margin-left:4px;">(New)</span>'
    return ''


def _companies_html(companies: List[str]) -> str:
    if not companies:
        return "N/A"
    parts = []
    for c in companies:
        parts.append(
            f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=FS'
            f'&caseTitleOrCompanyName={c}" style="color:#2563eb;text-decoration:none;'
            f'font-weight:700;">{c}</a>'
        )
    return '<span style="color:#9ca3af;margin:0 8px;">|</span>'.join(parts)


def _row(label: str, value: str, status: str = "unchanged") -> str:
    return (
        f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{_highlight(status)}">'
        f'<span style="color:#6b7280;">{label}{_label_suffix(status)}:</span> {value}'
        '</div>'
    )


def generate_update_email_html(
    case: Dict[str, Any],
    differences: List[Tuple[str, Any, Any]],
    companies: List[str],
) -> str:
    case_num = case.get("case_number", "N/A")
    case_title = case.get("case_title", "N/A")
    instrument = case.get("instrument", "Foreign Subsidies")
    status = case.get("status", "")
    case_url = case.get(
        "case_url", f"https://competition-cases.ec.europa.eu/cases/{case_num}")

    check_fields = [
        "companies", "last_decision_date", "case_type",
        "regulation", "notification_date", "provisional_deadline",
        "economic_activities", "decisions", "other_case_related_information"
    ]
    field_status = {}
    changed_names = []
    for f in check_fields:
        s = get_field_change_status(f, differences)
        field_status[f] = s
        if s != "unchanged":
            changed_names.append(f.replace("_", " ").title())

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>FS Case Update - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
{{BANNER_PLACEHOLDER}}
<div style="padding:28px 28px 12px 28px;">
<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>
<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #059669;color:#059669;background:#fff;">{instrument}</span>
'''

    if status:
        html += f'<div style="margin-left:2px;font-size:14px;color:#6b7280;font-style:italic;">{status}</div>'

    html += f'''</div>
<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>
<div style="margin-top:18px;">'''

    html += _row("Companies (case title)", _companies_html(companies),
                 field_status.get("companies", "unchanged"))
    html += _row("Case URL",
                 f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{case_url}</a>')
    html += _row("Last decision date",
                 f'<span style="font-weight:800;">{case.get("last_decision_date", "N/A")}</span>',
                 field_status.get("last_decision_date", "unchanged"))
    html += _row("Case type", case.get("case_type", "N/A"),
                 field_status.get("case_type", "unchanged"))
    html += _row("Regulation", case.get("regulation", "N/A"),
                 field_status.get("regulation", "unchanged"))
    html += _row("Notification date", case.get("notification_date",
                 "N/A"), field_status.get("notification_date", "unchanged"))
    html += _row("Provisional deadline", case.get("provisional_deadline",
                 "N/A"), field_status.get("provisional_deadline", "unchanged"))

    activities = case.get("economic_activities") or []
    ea_status = field_status.get("economic_activities", "unchanged")
    html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;{_highlight(ea_status)}">'
    html += f'<span style="color:#6b7280;">Economic activities{_label_suffix(ea_status)}:</span> '
    if activities:
        html += '<br>'.join(
            f'<span style="color:#2563eb;font-weight:700;">{a}</span>' for a in activities)
    else:
        html += 'N/A'
    html += '</div>'

    html += '</div></div>'

    if changed_names:
        html += (
            '<div style="padding:14px 18px;margin:18px 28px;border-radius:6px;font-size:14px;'
            'font-weight:600;color:#dc2626;background-color:#fef2f2;border-left:4px solid #ef4444;">'
            f'This case was updated. Changed fields: {", ".join(changed_names)}</div>'
        )
        html += (
            '<div style="margin:0 28px 18px 28px;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            '<thead><tr style="background:#f9fafb;">'
            '<th style="text-align:left;padding:10px 14px;color:#6b7280;font-weight:700;border-bottom:1px solid #e5e7eb;">Field</th>'
            '<th style="text-align:left;padding:10px 14px;color:#6b7280;font-weight:700;border-bottom:1px solid #e5e7eb;">Previous Value</th>'
            '<th style="text-align:left;padding:10px 14px;color:#6b7280;font-weight:700;border-bottom:1px solid #e5e7eb;">New Value</th>'
            '</tr></thead><tbody>'
        )
        for diff_path, old_val, new_val in differences:
            def _fmt_val(v):
                if v is None:
                    return '<span style="color:#9ca3af;font-style:italic;">(empty)</span>'
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False, default=str)
                return str(v)
            label = diff_path.replace("_", " ").replace(".", " > ")
            html += (
                '<tr>'
                f'<td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;font-weight:600;color:#374151;">{label}</td>'
                f'<td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;color:#ef4444;text-decoration:line-through;">{_fmt_val(old_val)}</td>'
                f'<td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;color:#059669;font-weight:700;">{_fmt_val(new_val)}</td>'
                '</tr>'
            )
        html += '</tbody></table></div>'

    # Decisions section
    decisions = case.get("decisions") or []
    dec_status = field_status.get("decisions", "unchanged")
    if decisions:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += f'<div style="padding:18px 28px 8px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Decisions{_label_suffix(dec_status)}</div>'
        for d in decisions:
            dtype = d.get("decision_type", "")
            ddate = d.get("decision_date", "")
            html += '<div style="padding:14px 0;"><div style="font-size:14px;color:#111827;">'
            html += f'<span style="font-weight:900;">{dtype}</span>'
            if ddate:
                html += f'<span style="color:#6b7280;"> of {ddate}</span>'
            html += '</div>'

            if d.get("decision_texts"):
                html += '<div style="margin-top:6px;font-size:14px;color:#111827;"><span style="color:#6b7280;">Decision text(s):</span> '
                for dt in d["decision_texts"]:
                    html += f'<span style="font-weight:800;">{dt.get("lang", "")}</span>'
                    if dt.get("published_on"):
                        html += f'<span style="color:#6b7280;font-size:13px;"> published on {dt["published_on"]}</span>'
                    html += ' '
                html += '</div>'

            press = d.get("press_communication")
            if press:
                ref = press.get("ref", "")
                html += '<div style="margin-top:6px;font-size:14px;color:#111827;">'
                html += '<span style="color:#6b7280;">Press communication:</span> '
                html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={ref}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{ref}</a>'
                if press.get("date"):
                    html += f'<span style="color:#6b7280;"> of {press["date"]}</span>'
                html += '</div>'

            html += '</div>'
        html += '</div>'

    # Other case related information
    other_info = case.get("other_case_related_information") or []
    oi_status = field_status.get("other_case_related_information", "unchanged")
    if other_info:
        html += '<div style="height:1px;background:#e5e7eb;"></div>'
        html += f'<div style="padding:18px 28px 26px 28px;"><div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Other case related information{_label_suffix(oi_status)}</div>'
        for item in other_info:
            itype = item.get("type", "")
            if itype == "note":
                html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;">{item.get("text", "")}</div>'
            elif itype in ("publication_oj", "prior_publication_oj"):
                label = "Prior publication in the OJ" if "prior" in itype else "Publication in the OJ"
                html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">{label}:</span> '
                html += f'<span style="font-weight:700;">{item.get("ref", "")}</span>'
                if item.get("date"):
                    html += f' <span style="color:#6b7280;">of {item["date"]}</span>'
                html += '</div>'
            elif itype == "description_of_concentration":
                html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;"><span style="color:#6b7280;">Description of the concentration'
                if item.get("date"):
                    html += f' of {item["date"]}'
                html += ':</span> '
                for lang in item.get("languages", []):
                    html += f'<span style="font-weight:800;">{lang.get("lang", "")}</span>'
                    if lang.get("published_on"):
                        html += f'<span style="color:#6b7280;font-size:13px;"> published on {lang["published_on"]}</span>'
                    html += ' '
                html += '</div>'
        html += '</div>'

    html += '</div>\n</body>\n</html>'
    return html


def _build_deal_banner(deal: Dict[str, Any], case_number: str) -> str:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = deal.get("deal_id") or str(deal.get("_id", "N/A"))
    case_url = f"https://competition-cases.ec.europa.eu/cases/{case_number}"
    return (
        '<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;'
        'margin:20px 28px 0 28px;border-left:4px solid #2563eb;">'
        '<div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>'
        '<div style="font-size:14px;color:#1e3a8a;">'
        f'<span style="font-weight:700;">Acquirer:</span> {acquirer}'
        '<span style="color:#94a3b8;margin:0 8px;">|</span>'
        f'<span style="font-weight:700;">Target:</span> {target}'
        '<span style="color:#94a3b8;margin:0 8px;">|</span>'
        f'<span style="font-weight:700;">Deal ID:</span> {deal_id}'
        '</div>'
        '<div style="margin-top:10px;">'
        f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;'
        'font-weight:700;font-size:14px;">View FS Case \u2192</a>'
        '</div></div>'
    )


def _build_usa_banner(case_number: str) -> str:
    case_url = f"https://competition-cases.ec.europa.eu/cases/{case_number}"
    return (
        '<div style="background:#fef3c7;border-radius:6px;padding:16px 22px;'
        'margin:20px 28px 0 28px;border-left:4px solid #f59e0b;">'
        '<div style="font-size:15px;font-weight:800;color:#92400e;margin-bottom:4px;">USA-Related Case</div>'
        '<div style="font-size:14px;color:#78350f;">This FS case involves companies with US connections.</div>'
        '<div style="margin-top:10px;">'
        f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;'
        'font-weight:700;font-size:14px;">View FS Case \u2192</a>'
        '</div></div>'
    )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(headed: bool = False, max_cases: Optional[int] = None):
    print("Starting New FS Cases Update Monitor\n")

    print("Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"{message}", level="error")
        return
    print(f"{message}\n")

    if not is_connected():
        print("MongoDB not connected. Exiting.", level="error")
        return

    collection = get_fs_cases_collection()
    if collection is None:
        print("Could not access 'fs_cases' collection. Exiting.", level="error")
        return

    # Step 1: Fetch deals
    print("Loading deals from MongoDB...")
    deals = fetch_deals()
    deal_by_id: Dict[str, Dict[str, Any]] = {
        (d.get("deal_id") or str(d.get("_id", ""))): d
        for d in deals if d.get("deal_id") or d.get("_id")
    }

    # Step 2: Fetch open FS cases
    open_cases = fetch_open_cases(collection)
    if not open_cases:
        print("No open FS cases found. Exiting.")
        return

    if max_cases:
        open_cases = open_cases[:max_cases]
        print(f"Limited to first {max_cases} cases")

    total = len(open_cases)
    changed_count = 0
    closed_count = 0

    # Step 3: Iterate with Playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()

        # Dismiss cookie banner once
        init_page = context.new_page()
        init_page.goto("https://competition-cases.ec.europa.eu/cases/FS.100189",
                       wait_until="domcontentloaded", timeout=60000)
        dismiss_cookie_banner(init_page)
        init_page.close()
        print("old open cases: ", open_cases)

        for idx, case_doc in enumerate(open_cases, 1):
            case_number = case_doc.get("case_number", "")
            old_title = case_doc.get("case_title", "N/A")
            deal_id = case_doc.get("deal_id")

            print(f"\n[{idx}/{total}] {case_number}: {old_title}")

            if not case_number:
                print("  No case_number; skipping")
                continue

            # Step 4: Scrape detail page
            new_data = scrape_case_page(context, case_number)
            if not new_data or new_data.get("error"):
                print(f"  Scrape/parse failed; skipping", level="warning")
                continue

            # Step 5: Compare all fields
            old_data = strip_tracking_fields(case_doc)
            differences = deep_compare(old_data, new_data)

            differences = [
                d for d in differences
                if not any(tf in d[0] for tf in _EXCLUDE_FROM_COMPARE)
            ]

            if not differences:
                print("  No changes detected")
                continue

            changed_count += 1
            changed_names = []
            for path, _, _ in differences:
                name = path.split(".")[0].split("[")[0]
                if name not in changed_names:
                    changed_names.append(name)
            print(f"  Changes detected: {', '.join(changed_names)}")
            for diff_path, old_val, new_val in differences:
                old_display = json.dumps(old_val, ensure_ascii=False) if isinstance(
                    old_val, (dict, list)) else str(old_val) if old_val is not None else "(empty)"
                new_display = json.dumps(new_val, ensure_ascii=False) if isinstance(
                    new_val, (dict, list)) else str(new_val) if new_val is not None else "(empty)"
                print(f"    {diff_path}: {old_display} -> {new_display}")

            # Check last_decision_date for is_open
            extra_fields: Dict[str, Any] = {}
            if has_real_decision_date(new_data):
                extra_fields["is_open"] = False
                closed_count += 1
                print(f"  last_decision_date is present -> setting is_open: false")

            companies = get_companies_from_title(new_data)

            # Step 6: Email logic
            if deal_id:
                print(f"  Linked to deal_id={deal_id}")
                deal = deal_by_id.get(deal_id)
                if not deal:
                    try:
                        deals_coll = get_deals_collection()
                        if deals_coll:
                            raw = deals_coll.find_one(
                                {"_id": ObjectId(deal_id)})
                            if raw:
                                raw["deal_id"] = str(raw["_id"])
                                deal = raw
                    except Exception:
                        pass

                case_title = new_data.get("case_title", "N/A")
                email_html = generate_update_email_html(
                    new_data, differences, companies)

                if deal:
                    banner = _build_deal_banner(deal, case_number)
                    target = deal.get("target") or deal.get(
                        "target_name", "N/A")
                    acquirer = deal.get("acquirer") or deal.get(
                        "acquire_name", "N/A")
                    subject = f"[FRMD] EC FS Case (Updated) \u2013 {target} / {acquirer}"
                else:
                    banner = ""
                    subject = f"[FRMD] EC FS Case (Updated) \u2013 {case_number}: {case_title}"

                email_html = email_html.replace("{BANNER_PLACEHOLDER}", banner)
                send_email_via_webhook(subject, email_html, case_number,
                                       case_title, deal_id=deal_id, changed_fields=changed_names)

            else:
                case_title = new_data.get("case_title", "N/A")

                print(f"  No deal_id -> LLM Call #1: deal match...")
                match_result = match_case_to_deal(
                    companies, deals) if deals else None

                deal = None
                if match_result:
                    matched_deal_id, matched_company, matched_role = match_result
                    deal = deal_by_id.get(matched_deal_id)
                    if not deal:
                        try:
                            deals_coll = get_deals_collection()
                            if deals_coll:
                                raw = deals_coll.find_one(
                                    {"_id": ObjectId(matched_deal_id)})
                                if raw:
                                    raw["deal_id"] = str(raw["_id"])
                                    deal = raw
                        except Exception:
                            pass

                if deal:
                    matched_deal_id = deal.get("deal_id", matched_deal_id)
                    print(f"  Match found: deal_id={matched_deal_id}")
                    extra_fields["deal_id"] = matched_deal_id

                    email_html = generate_update_email_html(
                        new_data, differences, companies)
                    banner = _build_deal_banner(deal, case_number)
                    email_html = email_html.replace(
                        "{BANNER_PLACEHOLDER}", banner)

                    target = deal.get("target") or deal.get(
                        "target_name", "N/A")
                    acquirer = deal.get("acquirer") or deal.get(
                        "acquire_name", "N/A")
                    subject = f"[FRMD] EC FS Case (Updated) \u2013 {target} / {acquirer}"
                    send_email_via_webhook(subject, email_html, case_number, case_title,
                                           deal_id=matched_deal_id, changed_fields=changed_names)
                else:
                    print(f"  No match -> LLM Call #2: USA check...")
                    try:
                        is_usa = verify_usa_relation(
                            company_details=companies, case_type="FS")
                    except Exception as e:
                        print(f"  USA check error: {e}", level="warning")
                        is_usa = False

                    if is_usa:
                        print(f"  USA-related case detected")
                        email_html = generate_update_email_html(
                            new_data, differences, companies)
                        banner = _build_usa_banner(case_number)
                        email_html = email_html.replace(
                            "{BANNER_PLACEHOLDER}", banner)

                        companies_str = " / ".join(
                            companies) if companies else "N/A"
                        subject = f"[FRUD] EC FS Case (USA-Related Update) \u2013 {case_number}: {companies_str}"
                        send_email_via_webhook(
                            subject, email_html, case_number, case_title, changed_fields=changed_names)
                    else:
                        print(f"  Not matched, not USA-related -> no email")

            # Always update DB
            update_case_document(collection, case_doc,
                                 new_data, extra_fields or None)

        context.close()
        browser.close()

    print(f"\n{'=' * 60}")
    print(f"Summary:")
    print(f"  Total open FS cases checked   : {total}")
    print(f"  Cases with changes             : {changed_count}")
    print(f"  Cases closed (is_open=false)   : {closed_count}")
    print(f"{'=' * 60}")
    print("Done!")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="New FS Cases Update Monitor (Playwright-based)")
    ap.add_argument("--headed", action="store_true", help="Visible browser")
    ap.add_argument("--max-cases", type=int, default=None,
                    help="Limit number of cases")
    args = ap.parse_args()

    run(headed=args.headed, max_cases=args.max_cases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
