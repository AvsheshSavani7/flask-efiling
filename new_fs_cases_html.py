"""
FS Cases HTML — Playwright-based Foreign Subsidies case register
================================================================

Scrapes the EC Competition Cases portal (Foreign Subsidies instrument) with
Playwright, parses each detail page in memory, matches against existing deals
via LLM, and inserts new cases into the MongoDB 'fs_cases' collection.

Flow:
1. Open the FS search page with Playwright
2. Paginate through results, collecting case links
3. For each case:
   - Skip if case_number already exists in fs_cases
   - Open detail page, wait for SPA render, parse HTML in memory
   - LLM call #1: try to match with existing deals
     -> matched: send [FRMD] email, insert with deal_id + is_open=True
   - LLM call #2 (if no match): check if USA-related
     -> USA-related: send [FRUD] email, insert with is_open=True
   - Otherwise: insert with is_open=True (no email)

Run:
    python new_fs_cases_html.py
    python new_fs_cases_html.py --max-pages 3
    python new_fs_cases_html.py --headed
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

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_LOG_NAME = "fs_cases_register"


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_LOG_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger("new_fs_cases_html")
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

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ENV_PATH = ".env"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
    # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

START_URL = (
    "https://competition-cases.ec.europa.eu/search"
    "?caseInstrument=InstrumentFS&caseLastDecisionDate=None"
    "&pageSize=50&sortField=caseLastDecisionDate&sortOrder=DESC"
)

WAIT_SELECTORS = [
    "a[href*='/cases/']",
    "main a[href*='/cases/']",
    "[role='main'] a[href*='/cases/']",
]

NEXT_BUTTON_SELECTORS = [
    "button[aria-label*='Next']",
    "a[aria-label*='Next']",
    "button:has-text('Next')",
    "a:has-text('Next')",
]

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

def extract_case_num(url: str) -> Optional[str]:
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    values = qs.get("proc_code")
    if values:
        return values[0]
    match = re.search(r"/cases/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


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


def wait_for_results(page, timeout_ms: int = 30000) -> str:
    last_error = None
    for selector in WAIT_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            return selector
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not find result links on page. Last error: {last_error}")


def collect_case_links(page, selector: str) -> List[Dict[str, str]]:
    links = page.locator(selector)
    count = links.count()
    results: List[Dict[str, str]] = []
    seen: Set[str] = set()

    for i in range(count):
        item = links.nth(i)
        href = item.get_attribute("href")
        if not href or "/cases/" not in href:
            continue
        if not href.startswith("http"):
            href = f"https://competition-cases.ec.europa.eu{href}"
        if href in seen:
            continue
        seen.add(href)
        results.append({"url": href})

    return results


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


def click_next_page(page) -> bool:
    for selector in NEXT_BUTTON_SELECTORS:
        locator = page.locator(selector)
        count = locator.count()
        for i in range(count):
            btn = locator.nth(i)
            try:
                if not btn.is_visible():
                    continue
                aria_disabled = (btn.get_attribute(
                    "aria-disabled") or "").lower()
                disabled = btn.get_attribute("disabled")
                if aria_disabled == "true" or disabled is not None:
                    continue
                btn.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


def scrape_case_detail(context, url: str) -> Optional[Dict[str, Any]]:
    """Open detail page in a new tab, parse in memory, return structured dict."""
    case_num = extract_case_num(url)
    if not case_num:
        print(f"[SKIP] Cannot determine case number from {url}")
        return None

    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        dismiss_cookie_banner(page)

        spa_loaded = wait_for_spa_content(page, timeout_s=15)
        if not spa_loaded:
            page.wait_for_timeout(3000)

        html = page.content()
        return parse_case_html(html, case_num)
    except Exception as exc:
        print(f"[ERROR] Failed to scrape {case_num}: {exc}", level="error")
        return None
    finally:
        page.close()


# ---------------------------------------------------------------------------
# FS helpers
# ---------------------------------------------------------------------------

def get_companies_from_title(case: Dict[str, Any]) -> List[str]:
    """FS pages often have companies=null; derive from case_title instead."""
    companies = case.get("companies")
    if companies:
        return companies
    title = case.get("case_title") or ""
    if not title:
        return []
    return [c.strip() for c in title.split(" / ") if c.strip()]


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_fs_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["fs_cases"]


def case_exists(collection, case_number: str) -> bool:
    try:
        return collection.count_documents({"case_number": case_number}, limit=1) > 0
    except Exception as e:
        print(f"  Error checking existing case: {e}", level="warning")
        return False


def fetch_deals() -> List[Dict[str, Any]]:
    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            return []

        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(deals_collection.find(status_filter))

        for d in deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])

        print(f"Fetched {len(deals)} open/unknown deals from MongoDB")
        return deals
    except Exception as e:
        print(f"Error fetching deals: {e}", level="warning")
        return []


def insert_case(collection, case_doc: Dict[str, Any]) -> Optional[str]:
    try:
        result = collection.insert_one(case_doc)
        return str(result.inserted_id)
    except Exception as e:
        print(f"  Error inserting case: {e}", level="error")
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# LLM deal matching (FS-specific prompt)
# ---------------------------------------------------------------------------

def match_case_to_deal(
    case_companies: List[str], deals: List[Dict[str, Any]]
) -> Optional[Tuple[str, str, str]]:
    if not case_companies or not deals:
        return None

    companies_str = " / ".join(case_companies)

    lines = []
    for d in deals:
        deal_id = d.get("deal_id") or str(d.get("_id", ""))
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {deal_id} | Target: {target} | Acquirer: {acquirer}"
        for alias_field in ("target_aliases", "parent_aliases"):
            aliases = d.get(alias_field) or []
            if aliases:
                line += f" | {alias_field.replace('_', ' ').title()}: {', '.join(str(a) for a in aliases)}"
        lines.append(line)

    prompt = f"""You are an M&A deal analyst. Given the company names from an EC Foreign Subsidies case (case title), determine whether any match any of the deals below.

DEALS TO MATCH:
{chr(10).join(lines)}

CASE COMPANIES (from case title):
{companies_str}

INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned in the EC Foreign Subsidies case companies.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the EC Foreign Subsidies case companies.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the EC Foreign Subsidies case companies.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the EC Foreign Subsidies case companies:
   - one match for the Acquirer side
   - one match for the Target side
5. Do not return a match if only one side is present, even if that single company is an exact match.
6. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - “Inc.” vs “Incorporated”
   - “Corp.” vs “Corporation”
   - “Ltd” vs “Limited”
   - obvious spacing/casing differences
7. Do not match based only on sector, business type, article topic, indirect association, or partial deal overlap.
8. If the EC Foreign Subsidies case companies do not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
- If you find BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|General Motors|acquirer

- If no deal satisfies this rule, respond exactly: None
"""

    try:
        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert in M&A deal recognition. Your job is to find matches between EC Foreign Subsidies case companies and deal companies. If the case companies match or are contained in any Target and Acquirer name, return the match. Be thorough and check all possibilities.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = (res.choices[0].message.content or "").strip()
        print(f"  LLM match response: {content}")

        if not content.lower().startswith("match:"):
            return None

        parts = content[6:].strip().split("|")
        if len(parts) < 3:
            return None

        deal_id = parts[0].strip()
        matched_company = parts[1].strip()
        role_raw = parts[2].strip().lower().replace("(", "").replace(")", "")
        matched_role = role_raw if role_raw in (
            "target", "acquirer") else "acquirer"
        return (deal_id, matched_company, matched_role)
    except Exception as e:
        print(f"  LLM match error: {e}", level="warning")
        return None


# ---------------------------------------------------------------------------
# Email via webhook
# ---------------------------------------------------------------------------

def send_email_via_webhook(
    subject: str,
    html_content: str,
    case_number: str,
    case_title: str,
    deal_id: Optional[str] = None,
    usa_related: bool = False,
) -> bool:
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "case_number": case_number,
            "case_title": case_title,
            "deal_id": deal_id,
            "usa_related": usa_related,
            "is_new_case": True,
            "case_instrument": "FS",
            "source": "ec_foreign_subsidies_cases",
        }
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  Email sent! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"  Error sending email: {e}", level="warning")
        return False


# ---------------------------------------------------------------------------
# Email HTML generators (FS-specific: green badge style)
# ---------------------------------------------------------------------------

def _row(label: str, value: str) -> str:
    return (
        '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
        f'<span style="color:#6b7280;">{label}:</span> {value}'
        '</div>'
    )


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


def _economic_activities_html(activities: Optional[List[str]]) -> str:
    if not activities:
        return "N/A"
    return '<br>'.join(
        f'<span style="color:#2563eb;font-weight:700;">{a}</span>' for a in activities
    )


def _decisions_html(decisions: Optional[List[Dict[str, Any]]]) -> str:
    if not decisions:
        return ""
    html = '<div style="height:1px;background:#e5e7eb;"></div>'
    html += '<div style="padding:18px 28px 8px 28px;">'
    html += '<div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Decisions</div>'
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
    return html


def _other_info_html(info: Optional[List[Dict[str, Any]]]) -> str:
    if not info:
        return ""
    html = '<div style="height:1px;background:#e5e7eb;"></div>'
    html += '<div style="padding:18px 28px 26px 28px;">'
    html += '<div style="font-size:18px;font-weight:900;color:#111827;margin-bottom:14px;">Other case related information</div>'
    for item in info:
        itype = item.get("type", "")
        if itype == "note":
            html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;">{item.get("text", "")}</div>'
        elif itype in ("publication_oj", "prior_publication_oj"):
            label = "Prior publication in the OJ" if "prior" in itype else "Publication in the OJ"
            html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
            html += f'<span style="color:#6b7280;">{label}:</span> '
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
    return html


def _case_body_html(case: Dict[str, Any], companies: List[str]) -> str:
    case_num = case.get("case_number", "N/A")
    case_title = case.get("case_title", "N/A")
    instrument = case.get("instrument", "Foreign Subsidies")
    status = case.get("status", "")
    case_url = case.get(
        "case_url", f"https://competition-cases.ec.europa.eu/cases/{case_num}")

    html = '<div style="padding:28px 28px 12px 28px;">'
    html += '<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">'
    html += f'<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>'
    html += f'<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #059669;color:#059669;background:#fff;">{instrument}</span>'
    html += '<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #2563eb;color:#2563eb;background:#fff;">FS</span>'
    if status:
        html += f'<div style="margin-left:2px;font-size:14px;color:#6b7280;font-style:italic;">{status}</div>'
    html += '</div>'
    html += f'<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>'
    html += '<div style="margin-top:18px;">'

    html += _row("Companies (case title)", _companies_html(companies))
    html += _row("Case URL",
                 f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{case_url}</a>')
    html += _row("Last decision date", case.get("last_decision_date", "N/A"))
    html += _row("Case type", case.get("case_type", "N/A"))
    html += _row("Regulation", case.get("regulation", "N/A"))
    html += _row("Notification date", case.get("notification_date", "N/A"))
    html += _row("Provisional deadline",
                 case.get("provisional_deadline", "N/A"))
    html += _row("Economic activities",
                 _economic_activities_html(case.get("economic_activities")))

    html += '</div></div>'
    html += _decisions_html(case.get("decisions"))
    html += _other_info_html(case.get("other_case_related_information"))
    return html


def generate_matched_email(case: Dict[str, Any], deal: Dict[str, Any], companies: List[str]) -> Tuple[str, str]:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = deal.get("deal_id") or str(deal.get("_id", "N/A"))
    case_num = case.get("case_number", "N/A")
    case_url = case.get("case_url", "")

    subject = f"[FRMD] EC Foreign Subsidies Case (New) \u2013 {target} / {acquirer}"

    deal_banner = (
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

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC FS Case Match - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
{deal_banner}
{_case_body_html(case, companies)}
</div>
</body>
</html>'''
    return subject, html


def generate_usa_email(case: Dict[str, Any], companies: List[str]) -> Tuple[str, str]:
    case_num = case.get("case_number", "N/A")
    companies_str = " / ".join(companies) if companies else "N/A"

    subject = f"[FRUD] EC Foreign Subsidies Case (USA-Related) \u2013 {case_num}: {companies_str}"

    usa_banner = (
        '<div style="background:#fef3c7;border-radius:6px;padding:16px 22px;'
        'margin:20px 28px 0 28px;border-left:4px solid #f59e0b;">'
        '<div style="font-size:15px;font-weight:800;color:#92400e;margin-bottom:4px;">USA-Related Case</div>'
        '<div style="font-size:14px;color:#78350f;">This FS case involves companies with US connections.</div>'
        '</div>'
    )

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC FS Case (USA-Related) - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
{usa_banner}
{_case_body_html(case, companies)}
</div>
</body>
</html>'''
    return subject, html


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(start_url: str, max_pages: Optional[int], headed: bool):
    print("Starting FS Cases HTML Register\n")

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

    print("Loading deals from MongoDB...")
    deals = fetch_deals()
    if not deals:
        print("No open/unknown deals found. Will still register cases.",
              level="warning")

    deal_by_id: Dict[str, Dict[str, Any]] = {
        (d.get("deal_id") or str(d.get("_id", ""))): d
        for d in deals if d.get("deal_id") or d.get("_id")
    }

    visited_urls: Set[str] = set()
    new_count = 0
    skipped_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        search_page = context.new_page()

        search_page.goto(
            start_url, wait_until="domcontentloaded", timeout=60000)
        search_page.wait_for_timeout(3000)
        dismiss_cookie_banner(search_page)
        selector = wait_for_results(search_page)

        current_page = 1
        while True:
            print(f"\n[Page {current_page}] Collecting case links...")
            search_page.wait_for_timeout(1000)
            links = collect_case_links(search_page, selector)
            print(f"[Page {current_page}] Found {len(links)} case links")

            for item in links:
                url = item["url"]
                if url in visited_urls:
                    continue
                visited_urls.add(url)

                case_num = extract_case_num(url)
                if not case_num:
                    continue

                # DB check
                if case_exists(collection, case_num):
                    print(f"  [{case_num}] Already in DB; skipping")
                    skipped_count += 1
                    continue

                # Scrape + parse in memory
                print(f"  [{case_num}] Scraping detail page...")
                case = scrape_case_detail(context, url)
                if not case or case.get("error"):
                    print(f"  [{case_num}] Parse failed; skipping",
                          level="warning")
                    continue

                case_title = case.get("case_title") or "N/A"
                companies = get_companies_from_title(case)
                print(
                    f"  [{case_num}] {case_title} | Companies: {', '.join(companies)}")

                now_iso = utc_now_iso()

                # LLM #1: deal match
                print(f"  [{case_num}] LLM Call #1: deal match...")
                match_result = match_case_to_deal(
                    companies, deals) if deals else None

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
                        print(
                            f"  [{case_num}] Match: deal_id={matched_deal_id}")

                        subject, html_email = generate_matched_email(
                            case, deal, companies)
                        send_email_via_webhook(
                            subject, html_email, case_num, case_title, deal_id=matched_deal_id)

                        case_doc = {
                            **case,
                            "deal_id": matched_deal_id,
                            "is_open": True,
                            "created_at": now_iso,
                            "updated_at": now_iso,
                        }
                        inserted_id = insert_case(collection, case_doc)
                        if inserted_id:
                            print(
                                f"  [{case_num}] Inserted (id={inserted_id})")
                            new_count += 1
                        continue
                    else:
                        print(
                            f"  [{case_num}] LLM returned deal_id={matched_deal_id} but deal not found; checking USA")

                # LLM #2: USA check
                print(f"  [{case_num}] LLM Call #2: USA-related check...")
                try:
                    is_usa = verify_usa_relation(
                        company_details=companies, case_type="FS")
                except Exception as e:
                    print(f"  [{case_num}] USA check error: {e}",
                          level="warning")
                    is_usa = False

                if is_usa:
                    print(f"  [{case_num}] USA-related case detected")
                    subject, html_email = generate_usa_email(case, companies)
                    send_email_via_webhook(
                        subject, html_email, case_num, case_title, usa_related=True)

                    case_doc = {
                        **case,
                        "is_open": True,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                    inserted_id = insert_case(collection, case_doc)
                    if inserted_id:
                        print(f"  [{case_num}] Inserted (id={inserted_id})")
                        new_count += 1
                else:
                    # Not matched, not USA — still save
                    print(
                        f"  [{case_num}] No match, not USA-related; saving to DB")
                    case_doc = {
                        **case,
                        "is_open": True,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                    inserted_id = insert_case(collection, case_doc)
                    if inserted_id:
                        print(f"  [{case_num}] Inserted (id={inserted_id})")
                        new_count += 1

            if max_pages is not None and current_page >= max_pages:
                break

            if not click_next_page(search_page):
                break

            selector = wait_for_results(search_page)
            current_page += 1

        context.close()
        browser.close()

    print(
        f"\nFinished — {new_count} new case(s) inserted, {skipped_count} skipped (already in DB)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="FS Cases HTML: Playwright-based Foreign Subsidies case register.")
    ap.add_argument("--url", default=START_URL, help="Search URL")
    ap.add_argument("--max-pages", type=int, default=None, help="Page limit")
    ap.add_argument("--headed", action="store_true", help="Visible browser")
    args = ap.parse_args()

    run(args.url, args.max_pages, args.headed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
