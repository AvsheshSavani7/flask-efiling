"""
EC Cases HTML — Playwright-based EC merger case register
========================================================

Scrapes the EC Competition Cases portal with Playwright, parses each detail
page in memory, matches against existing deals via LLM, and inserts new
cases into the MongoDB 'ec_cases' collection.

Flow:
1. Open the ongoing mergers search page with Playwright
2. Paginate through results, collecting case links
3. For each case:
   - Skip if case_number already exists in ec_cases
   - Open detail page, wait for SPA render, parse HTML in memory
   - LLM call #1: try to match with existing deals
     -> matched: send [FRMD] email, insert with deal_id + is_open=True
   - LLM call #2 (if no match): check if USA-related
     -> USA-related: send [FRUD] email, insert with is_open=True
   - Otherwise: insert with is_open=True (no email)

Run:
    python ec_cases_html.py
    python ec_cases_html.py --max-pages 3
    python ec_cases_html.py --headed
"""

from llm_verification_service import verify_usa_relation
from error_email_service import send_error_email
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
from datetime import datetime, timezone, timedelta
import argparse
import json
import logging
import builtins
import re
import sys
import os
import time
import traceback

from ec_html_scraper import parse_case_html

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# Timestamps in IST (UTC+5:30)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "ec_cases_register"
IST = timezone(timedelta(hours=5, minutes=30))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger("ec_cases_html")
logger.setLevel(logging.INFO)


class _ISTFormatter(logging.Formatter):
    """Format log timestamps in IST."""
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


if not logger.handlers:
    formatter = _ISTFormatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
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


def _log_error_and_email(msg: str, context: Optional[Dict[str, Any]] = None):
    """Log at ERROR level and fire an error email."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context,
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )

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
    "?caseInstrument=M&caseOngoing=ongoing&pageSize=50"
    "&sortField=caseLastDecisionDate&sortOrder=DESC"
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
            logger.info(f"Results loaded with selector: {selector}")
            return selector
        except Exception as exc:
            last_error = exc
    _log_error_and_email(
        f"Could not find result links on page. Last error: {last_error}",
        {"step": "wait_for_results"},
    )
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
        logger.warning(f"[SKIP] Cannot determine case number from {url}")
        return None

    logger.info(f"  [{case_num}] Opening detail page: {url}")
    page = context.new_page()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)

        http_status = resp.status if resp else "N/A"
        logger.info(f"  [{case_num}] Page response: status={http_status}")

        if resp and resp.status >= 400:
            _log_error_and_email(
                f"Detail page returned HTTP {resp.status} for {case_num}",
                {"case_number": case_num, "url": url, "http_status": resp.status, "step": "scrape_case_detail"},
            )
            return None

        dismiss_cookie_banner(page)

        spa_loaded = wait_for_spa_content(page, timeout_s=15)
        logger.info(f"  [{case_num}] SPA content loaded: {spa_loaded}")
        if not spa_loaded:
            logger.warning(f"  [{case_num}] SPA not ready, waiting 3s fallback")
            page.wait_for_timeout(3000)

        html = page.content()
        logger.info(f"  [{case_num}] HTML fetched ({len(html)} chars)")

        record = parse_case_html(html, case_num)
        if record and not record.get("error"):
            logger.info(f"  [{case_num}] Parsed fields: {list(record.keys())}")
        else:
            error_msg = record.get("error") if record else "parse returned None"
            _log_error_and_email(
                f"HTML parse failed for {case_num}: {error_msg}",
                {"case_number": case_num, "url": url, "html_length": len(html), "step": "parse_case_html"},
            )
        return record
    except Exception as exc:
        _log_error_and_email(
            f"Failed to scrape {case_num}: {exc}",
            {"case_number": case_num, "url": url, "step": "scrape_case_detail"},
        )
        return None
    finally:
        page.close()


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_ec_cases_collection():
    db = get_database()
    if db is None:
        logger.error("get_database() returned None")
        return None
    logger.info(f"Connected to database: {db.name}")
    return db["ec_cases"]


def case_exists(collection, case_number: str) -> bool:
    try:
        return collection.count_documents({"case_number": case_number}, limit=1) > 0
    except Exception as e:
        _log_error_and_email(
            f"Error checking existing case {case_number}: {e}",
            {"case_number": case_number, "step": "case_exists"},
        )
        return False


def fetch_deals() -> List[Dict[str, Any]]:
    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            logger.warning("get_deals_collection() returned None")
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

        logger.info(f"Fetched {len(deals)} open/unknown deals from MongoDB")
        if deals:
            sample = deals[:3]
            for d in sample:
                logger.info(f"  Sample deal: id={d.get('deal_id')} | target={d.get('target') or d.get('target_name','N/A')} | acquirer={d.get('acquirer') or d.get('acquire_name','N/A')}")
        return deals
    except Exception as e:
        _log_error_and_email(
            f"Error fetching deals: {e}",
            {"step": "fetch_deals"},
        )
        return []


def insert_case(collection, case_doc: Dict[str, Any]) -> Optional[str]:
    case_num = case_doc.get("case_number", "?")
    try:
        result = collection.insert_one(case_doc)
        inserted_id = str(result.inserted_id)
        logger.info(f"  [{case_num}] Inserted into DB (id={inserted_id})")
        return inserted_id
    except Exception as e:
        _log_error_and_email(
            f"Error inserting case {case_num}: {e}",
            {"case_number": case_num, "step": "insert_case"},
        )
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# LLM deal matching
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

    prompt = f"""You are an M&A deal analyst. Given the company names from an EC merger case, determine whether any of these companies match any of the deals listed below.

DEALS TO MATCH:
{chr(10).join(lines)}

CASE COMPANIES:
{companies_str}

INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned in the EC merger case companies.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the EC merger case companies.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the EC merger case companies.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the EC merger case companies:
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
8. If the EC merger case companies do not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
- If you find BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY in this format:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|General Motors|acquirer

- If no deal satisfies this rule, respond exactly: None"""

    logger.info(f"  LLM deal match — input companies: {companies_str}")
    logger.info(f"  LLM deal match — checking against {len(deals)} deals")

    try:
        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert in M&A deal recognition. Your job is to find matches between EC merger case companies and deal companies. If the case companies match or are contained in any Target and Acquirer name, return the match. Be thorough and check all possibilities.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = (res.choices[0].message.content or "").strip()
        tokens_used = getattr(res.usage, "total_tokens", "N/A") if res.usage else "N/A"
        logger.info(f"  LLM match raw response: {content} (tokens={tokens_used})")

        if not content.lower().startswith("match:"):
            logger.info(f"  LLM match result: None (no match prefix)")
            return None

        parts = content[6:].strip().split("|")
        if len(parts) < 3:
            logger.warning(f"  LLM match result: malformed response, parts={parts}")
            return None

        deal_id = parts[0].strip()
        matched_company = parts[1].strip()
        role_raw = parts[2].strip().lower().replace("(", "").replace(")", "")
        matched_role = role_raw if role_raw in (
            "target", "acquirer") else "acquirer"
        logger.info(f"  LLM match result: deal_id={deal_id} | company={matched_company} | role={matched_role}")
        return (deal_id, matched_company, matched_role)
    except Exception as e:
        _log_error_and_email(
            f"LLM deal match error: {e}",
            {"companies": companies_str, "step": "match_case_to_deal"},
        )
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
    logger.info(f"  [{case_number}] Sending email: {subject}")
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "case_number": case_number,
            "case_title": case_title,
            "deal_id": deal_id,
            "usa_related": usa_related,
            "is_new_case": True,
            "source": "ec_competition_cases",
        }
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"  [{case_number}] Email sent successfully (status={resp.status_code})")
        return True
    except Exception as e:
        _log_error_and_email(
            f"Error sending notification email for {case_number}: {e}",
            {"case_number": case_number, "subject": subject, "step": "send_email_via_webhook"},
        )
        return False


# ---------------------------------------------------------------------------
# Email HTML generators (adapted for parsed HTML data shape)
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
            f'<a href="https://competition-cases.ec.europa.eu/search?caseInstrument=M'
            f'&caseTitleOrCompanyName={c}" style="color:#2563eb;text-decoration:none;'
            f'font-weight:700;">{c}</a>'
        )
    return '<span style="color:#9ca3af;margin:0 8px;">|</span>'.join(parts)


def _economic_activities_html(activities: Optional[List[str]]) -> str:
    if not activities:
        return "N/A"
    parts = []
    for a in activities:
        parts.append(
            f'<span style="color:#2563eb;font-weight:700;">{a}</span>'
        )
    return '<br>'.join(parts)


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

        sub_parts = []
        if d.get("decision_texts"):
            for dt in d["decision_texts"]:
                lang = dt.get("lang", "")
                pub = dt.get("published_on", "")
                line = f'<span style="font-weight:800;">{lang}</span>'
                if pub:
                    line += f'<span style="color:#6b7280;font-size:13px;"> published on {pub}</span>'
                sub_parts.append(line)
            html += '<div style="margin-top:6px;font-size:14px;color:#111827;">'
            html += '<span style="color:#6b7280;">Decision text(s):</span> '
            html += ' '.join(sub_parts) + '</div>'

        press = d.get("press_communication")
        if press:
            ref = press.get("ref", "")
            pdate = press.get("date", "")
            html += '<div style="margin-top:6px;font-size:14px;color:#111827;">'
            html += '<span style="color:#6b7280;">Press communication:</span> '
            html += f'<a href="http://europa.eu/rapid/pressReleasesAction.do?reference={ref}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:800;">{ref}</a>'
            if pdate:
                html += f'<span style="color:#6b7280;"> of {pdate}</span>'
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
            ref = item.get("ref", "")
            date = item.get("date", "")
            html += f'<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
            html += f'<span style="color:#6b7280;">{label}:</span> '
            html += f'<span style="font-weight:700;">{ref}</span>'
            if date:
                html += f' <span style="color:#6b7280;">of {date}</span>'
            html += '</div>'
        elif itype == "description_of_concentration":
            date = item.get("date", "")
            langs = item.get("languages", [])
            html += '<div style="font-size:14px;color:#111827;margin-bottom:8px;">'
            html += f'<span style="color:#6b7280;">Description of the concentration'
            if date:
                html += f' of {date}'
            html += ':</span> '
            for lang in langs:
                html += f'<span style="font-weight:800;">{lang.get("lang", "")}</span>'
                pub = lang.get("published_on", "")
                if pub:
                    html += f'<span style="color:#6b7280;font-size:13px;"> published on {pub}</span>'
                html += ' '
            html += '</div>'
    html += '</div>'
    return html


def _case_body_html(case: Dict[str, Any]) -> str:
    """Shared case detail body used by both matched and unmatched emails."""
    case_num = case.get("case_number", "N/A")
    case_title = case.get("case_title", "N/A")
    instrument = case.get("instrument", "Merger")
    status = case.get("status", "")
    case_url = case.get(
        "case_url", f"https://competition-cases.ec.europa.eu/cases/{case_num}")

    html = '<div style="padding:28px 28px 12px 28px;">'
    html += '<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">'
    html += f'<div style="font-size:28px;font-weight:800;letter-spacing:0.2px;color:#111827;">{case_num}</div>'
    html += f'<span style="display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 14px;border-radius:999px;font-size:13px;font-weight:700;border:1px solid #ef4444;color:#ef4444;background:#fff;">{instrument}</span>'
    if status:
        html += f'<div style="margin-left:2px;font-size:14px;color:#6b7280;font-style:italic;">{status}</div>'
    html += '</div>'
    html += f'<div style="margin-top:18px;font-size:26px;font-weight:900;color:#111827;">{case_title}</div>'
    html += '<div style="margin-top:18px;">'

    html += _row("Companies", _companies_html(case.get("companies") or []))
    html += _row("Case URL",
                 f'<a href="{case_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">{case_url}</a>')
    html += _row("Last decision date",
                 f'<span style="font-weight:800;">{case.get("last_decision_date", "N/A")}</span>')
    html += _row("Case type", case.get("case_type", "N/A"))
    if case.get("investigation_phase"):
        html += _row("Investigation phase", case["investigation_phase"])
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


def generate_matched_email(case: Dict[str, Any], deal: Dict[str, Any]) -> Tuple[str, str]:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = deal.get("deal_id") or str(deal.get("_id", "N/A"))
    case_num = case.get("case_number", "N/A")
    case_url = case.get("case_url", "")

    subject = f"[FRMD] EC Merger Case (New) \u2013 {target} / {acquirer}"

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
        'font-weight:700;font-size:14px;">View EC Case \u2192</a>'
        '</div></div>'
    )

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC Case Match - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
{deal_banner}
{_case_body_html(case)}
</div>
</body>
</html>'''
    return subject, html


def generate_usa_email(case: Dict[str, Any]) -> Tuple[str, str]:
    case_num = case.get("case_number", "N/A")
    companies = case.get("companies") or []
    companies_str = " / ".join(companies) if companies else "N/A"

    subject = f"[FRUD] EC Merger Case (USA-Related) \u2013 {case_num}: {companies_str}"

    usa_banner = (
        '<div style="background:#fef3c7;border-radius:6px;padding:16px 22px;'
        'margin:20px 28px 0 28px;border-left:4px solid #f59e0b;">'
        '<div style="font-size:15px;font-weight:800;color:#92400e;margin-bottom:4px;">USA-Related Case</div>'
        '<div style="font-size:14px;color:#78350f;">This case involves companies with US connections.</div>'
        '</div>'
    )

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EC Case (USA-Related) - {case_num}</title></head>
<body style="margin:0;background:#f5f7fb;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937;">
<div style="max-width:980px;margin:28px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 6px 18px rgba(17,24,39,0.06);overflow:hidden;">
{usa_banner}
{_case_body_html(case)}
</div>
</body>
</html>'''
    return subject, html


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(start_url: str, max_pages: Optional[int], headed: bool):
    run_start = time.time()
    error_count = 0
    new_count = 0
    skipped_count = 0
    visited_urls: Set[str] = set()

    logger.info("=" * 60)
    logger.info(f"Starting EC Cases HTML Register")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"Start URL: {start_url}")
    logger.info(f"Max pages: {max_pages or 'unlimited'}")
    logger.info(f"Headed: {headed}")
    logger.info("=" * 60)

    try:
        # --- Step 1: MongoDB ---
        logger.info("[STEP 1] Initializing MongoDB connection...")
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            _log_error_and_email(
                f"MongoDB connection failed: {message}",
                {"step": "mongodb_connect"},
            )
            return
        logger.info(f"[STEP 1] {message}")

        if not is_connected():
            _log_error_and_email(
                "MongoDB not connected after init",
                {"step": "mongodb_connect"},
            )
            return

        collection = get_ec_cases_collection()
        if collection is None:
            _log_error_and_email(
                "Could not access 'ec_cases' collection",
                {"step": "get_collection"},
            )
            return
        logger.info("[STEP 1] ec_cases collection ready")

        # --- Step 2: Fetch deals ---
        logger.info("[STEP 2] Loading deals from MongoDB...")
        deals = fetch_deals()
        if not deals:
            logger.warning("[STEP 2] No open/unknown deals found. Will still register cases.")

        deal_by_id: Dict[str, Dict[str, Any]] = {
            (d.get("deal_id") or str(d.get("_id", ""))): d
            for d in deals if d.get("deal_id") or d.get("_id")
        }
        logger.info(f"[STEP 2] Deal lookup map built ({len(deal_by_id)} entries)")

        # --- Step 3: Playwright scraping ---
        logger.info("[STEP 3] Launching Playwright browser...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context()
            search_page = context.new_page()

            logger.info(f"[STEP 3] Navigating to search page: {start_url}")
            search_page.goto(
                start_url, wait_until="domcontentloaded", timeout=60000)
            search_page.wait_for_timeout(3000)
            dismiss_cookie_banner(search_page)
            selector = wait_for_results(search_page)

            current_page = 1
            while True:
                logger.info(f"\n[Page {current_page}] Collecting case links...")
                search_page.wait_for_timeout(1000)
                links = collect_case_links(search_page, selector)
                logger.info(f"[Page {current_page}] Found {len(links)} case links")

                for item in links:
                    url = item["url"]
                    if url in visited_urls:
                        continue
                    visited_urls.add(url)

                    case_num = extract_case_num(url)
                    if not case_num:
                        logger.warning(f"  Could not extract case number from {url}")
                        continue

                    if case_exists(collection, case_num):
                        logger.info(f"  [{case_num}] Already in DB; skipping")
                        skipped_count += 1
                        continue

                    logger.info(f"  [{case_num}] Scraping detail page...")
                    case = scrape_case_detail(context, url)
                    if not case or case.get("error"):
                        logger.warning(f"  [{case_num}] Parse failed — skipping (error email already sent)")
                        error_count += 1
                        continue

                    case_title = case.get("case_title") or "N/A"
                    companies = case.get("companies") or []
                    logger.info(f"  [{case_num}] Title: {case_title}")
                    logger.info(f"  [{case_num}] Companies: {companies}")
                    logger.info(f"  [{case_num}] Parsed data: case_type={case.get('case_type')} | regulation={case.get('regulation')} | notification_date={case.get('notification_date')} | investigation_phase={case.get('investigation_phase')} | status={case.get('status')}")

                    now_iso = utc_now_iso()

                    # --- LLM #1: deal match ---
                    logger.info(f"  [{case_num}] LLM Call #1: deal match (companies={companies})...")
                    match_result = match_case_to_deal(
                        companies, deals) if deals else None

                    if match_result:
                        matched_deal_id, matched_company, matched_role = match_result
                        logger.info(f"  [{case_num}] LLM returned match: deal_id={matched_deal_id}, company={matched_company}, role={matched_role}")
                        deal = deal_by_id.get(matched_deal_id)

                        if not deal:
                            logger.info(f"  [{case_num}] deal_id={matched_deal_id} not in cache, querying DB...")
                            try:
                                deals_coll = get_deals_collection()
                                if deals_coll:
                                    raw = deals_coll.find_one(
                                        {"_id": ObjectId(matched_deal_id)})
                                    if raw:
                                        raw["deal_id"] = str(raw["_id"])
                                        deal = raw
                                        logger.info(f"  [{case_num}] Found deal in DB: target={raw.get('target')}, acquirer={raw.get('acquirer')}")
                                    else:
                                        logger.warning(f"  [{case_num}] deal_id={matched_deal_id} not found in DB either")
                            except Exception as e:
                                _log_error_and_email(
                                    f"Error looking up deal {matched_deal_id}: {e}",
                                    {"case_number": case_num, "deal_id": matched_deal_id, "step": "deal_lookup"},
                                )
                                error_count += 1

                        if deal:
                            target = deal.get("target") or deal.get("target_name", "N/A")
                            acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
                            logger.info(f"  [{case_num}] Matched deal: target={target} | acquirer={acquirer} | deal_id={matched_deal_id}")

                            subject, html_email = generate_matched_email(
                                case, deal)
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
                                new_count += 1
                            continue
                        else:
                            logger.warning(
                                f"  [{case_num}] LLM returned deal_id={matched_deal_id} but deal not found anywhere; falling through to USA check")

                    # --- LLM #2: USA check ---
                    logger.info(f"  [{case_num}] LLM Call #2: USA-related check (companies={companies})...")
                    try:
                        is_usa = verify_usa_relation(
                            company_details=companies, case_type="EC")
                        logger.info(f"  [{case_num}] USA check result: {is_usa}")
                    except Exception as e:
                        _log_error_and_email(
                            f"USA check error for {case_num}: {e}",
                            {"case_number": case_num, "companies": str(companies), "step": "verify_usa_relation"},
                        )
                        is_usa = False
                        error_count += 1

                    if is_usa:
                        logger.info(f"  [{case_num}] USA-related case detected — sending email")
                        subject, html_email = generate_usa_email(case)
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
                            new_count += 1
                    else:
                        logger.info(
                            f"  [{case_num}] No match, not USA-related — saving to DB (no email)")
                        case_doc = {
                            **case,
                            "is_open": True,
                            "created_at": now_iso,
                            "updated_at": now_iso,
                        }
                        inserted_id = insert_case(collection, case_doc)
                        if inserted_id:
                            new_count += 1

                if max_pages is not None and current_page >= max_pages:
                    logger.info(f"Reached max pages limit ({max_pages})")
                    break

                if not click_next_page(search_page):
                    logger.info("No more pages (next button not found or disabled)")
                    break

                selector = wait_for_results(search_page)
                current_page += 1

            context.close()
            browser.close()
            logger.info("Browser closed")

    except Exception as e:
        error_count += 1
        _log_error_and_email(
            f"Unhandled error in run(): {e}",
            {"step": "run_main", "start_url": start_url},
        )

    finally:
        elapsed = round(time.time() - run_start, 1)
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Total URLs visited           : {len(visited_urls)}")
        logger.info(f"  New cases inserted           : {new_count}")
        logger.info(f"  Skipped (already in DB)      : {skipped_count}")
        logger.info(f"  Errors encountered           : {error_count}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="EC Cases HTML: Playwright-based merger case register.")
    ap.add_argument("--url", default=START_URL, help="Search URL")
    ap.add_argument("--max-pages", type=int, default=None, help="Page limit")
    ap.add_argument("--headed", action="store_true", help="Visible browser")
    args = ap.parse_args()

    run(args.url, args.max_pages, args.headed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
