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
from error_email_service import send_error_email
from scraper_error_utils import (
    collect_error,
    scrape_error,
    scrape_error_context,
    send_error_summary,
)
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
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone, timedelta
import argparse
import json
import logging
import re
import sys
import os
import time
import traceback

from fs_html_scraper import parse_case_html
from log_utils import cleanup_old_logs, refresh_log_file
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Logging — production setup (RotatingFileHandler, IST, env-based settings)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "fs_cases_register"
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
    """Format log timestamps in IST."""

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
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ENV_PATH = ".env"

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

# Plain-text versions of the above, used to validate fetched HTML content
_SPA_HTML_INDICATORS = [
    "Notification date:",
    "Case type:",
    "Regulation:",
    "Last decision date:",
    "Initiation date:",
    "Investigation phase:",
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
                logger.info("Cookie banner dismissed")
                return
        except Exception:
            continue


def wait_for_results(page, timeout_ms: int = 45000) -> str:
    last_error = None
    for selector in WAIT_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            logger.info(f"Results loaded with selector: {selector}")
            return selector
        except Exception as exc:
            last_error = exc

    page_url = "unknown"
    page_title = "unknown"
    screenshot_path = None
    try:
        page_url = page.url
        page_title = page.title()
        log_dir = os.path.dirname(LOG_FILE)
        screenshot_path = os.path.join(
            log_dir,
            f"debug_screenshot_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.png",
        )
        page.screenshot(path=screenshot_path)
        logger.error(f"Debug screenshot saved to {screenshot_path}")
    except Exception:
        pass

    tried_selectors = ", ".join(WAIT_SELECTORS)
    explanation = (
        f"The EC Foreign Subsidies search page loaded (URL: {page_url}, "
        f"title: '{page_title}') but no case-result links appeared within "
        f"{timeout_ms / 1000:.0f}s. This usually means the site's SPA "
        f"JavaScript did not render results in time — the site may be "
        f"temporarily slow, under maintenance, or its HTML structure changed "
        f"so the selectors no longer match. "
        f"Selectors tried: {tried_selectors}"
    )
    raise RuntimeError(explanation)


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


def is_spa_content_in_html(html: str) -> bool:
    """Return True if the fetched HTML contains at least one SPA-rendered case label."""
    return any(indicator in html for indicator in _SPA_HTML_INDICATORS)


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
            logger.error(
                f"  [{case_num}] Detail page returned HTTP {resp.status}")
            return scrape_error(
                case_num,
                f"HTTP {resp.status} loading {url}",
                url=url,
                http_status=resp.status,
            )

        dismiss_cookie_banner(page)

        spa_loaded = wait_for_spa_content(page, timeout_s=15)
        logger.info(f"  [{case_num}] SPA content loaded: {spa_loaded}")
        if not spa_loaded:
            logger.warning(
                f"  [{case_num}] SPA not ready, waiting 3s fallback")
            page.wait_for_timeout(3000)

        html = page.content()
        logger.info(f"  [{case_num}] HTML fetched ({len(html)} chars)")
        logger.info(f"  [{case_num}] HTML: {html[:1500]}...")

        if not is_spa_content_in_html(html):
            logger.warning(
                f"  [{case_num}] SPA labels not found in HTML — page did not fully load, skipping")
            return scrape_error(
                case_num,
                f"SPA content not rendered after loading {url} (html_length={len(html)})",
                url=url,
            )

        parsed = parse_case_html(html, case_num)
        logger.info(f"  [{case_num}] Parsed: {parsed}")
        if parsed and not parsed.get("error"):
            logger.info(f"  [{case_num}] Parsed fields: {list(parsed.keys())}")
        else:
            error_msg = parsed.get(
                "error") if parsed else "parse returned None"
            logger.warning(
                f"  [{case_num}] Parse returned error: {error_msg}")
            if parsed:
                parsed["url"] = url
        return parsed
    except Exception as exc:
        logger.exception(f"  [{case_num}] Failed to scrape: {exc}")
        return scrape_error(
            case_num,
            f"Failed to load {url}: {exc}",
            url=url,
            traceback=traceback.format_exc(),
        )
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
        logger.error("get_database() returned None")
        return None
    logger.info(f"Connected to database: {db.name}")
    return db["fs_cases"]


def case_exists(collection, case_number: str) -> bool:
    try:
        exists = collection.count_documents(
            {"case_number": case_number}, limit=1) > 0
        return exists
    except Exception as e:
        logger.exception(f"Error checking existing case {case_number}: {e}")
        return False


def fetch_deals(error_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
                logger.info(
                    f"  Sample deal: id={d.get('deal_id')} | target={d.get('target') or d.get('target_name','N/A')} | acquirer={d.get('acquirer') or d.get('acquire_name','N/A')}")
        return deals
    except Exception as e:
        logger.exception(f"Error fetching deals: {e}")
        collect_error(
            error_items,
            f"Error fetching deals: {e}",
            step="fetch_deals",
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
        logger.exception(f"Error inserting case {case_num}: {e}")
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

    logger.info(f"  LLM deal match — input companies: {companies_str}")
    logger.info(f"  LLM deal match — checking against {len(deals)} deals")

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
        tokens_used = getattr(res.usage, "total_tokens",
                              "N/A") if res.usage else "N/A"
        logger.info(
            f"  LLM match raw response: {content} (tokens={tokens_used})")

        if not content.lower().startswith("match:"):
            logger.info(f"  LLM match result: None (no match prefix)")
            return None

        parts = content[6:].strip().split("|")
        if len(parts) < 3:
            logger.warning(
                f"  LLM match result: malformed response, parts={parts}")
            return None

        deal_id = parts[0].strip()
        matched_company = parts[1].strip()
        role_raw = parts[2].strip().lower().replace("(", "").replace(")", "")
        matched_role = role_raw if role_raw in (
            "target", "acquirer") else "acquirer"
        logger.info(
            f"  LLM match result: deal_id={deal_id} | company={matched_company} | role={matched_role}")
        return (deal_id, matched_company, matched_role)
    except Exception as e:
        logger.exception(f"LLM deal match error: {e}")
        raise


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
            "case_instrument": "FS",
            "source": "ec_foreign_subsidies_cases",
        }
        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.exception(
            f"Error sending notification email for {case_number}: {e}")
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

    subject = build_subject("ec_fs", "new", deal)

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

    subject = build_subject("ec_fs", "new")

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
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    new_count = 0
    skipped_count = 0
    visited_urls: Set[str] = set()

    logger.info("=" * 60)
    logger.info(f"Starting FS Cases HTML Register")
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
            collect_error(
                error_items,
                f"MongoDB connection failed: {message}",
                step="mongodb_connect",
            )
            return
        logger.info(f"[STEP 1] {message}")

        if not is_connected():
            collect_error(
                error_items,
                "MongoDB not connected after init",
                step="mongodb_connect",
            )
            return

        collection = get_fs_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "Could not access 'fs_cases' collection",
                step="get_collection",
            )
            return
        logger.info("[STEP 1] fs_cases collection ready")

        # --- Step 2: Fetch deals ---
        logger.info("[STEP 2] Loading deals from MongoDB...")
        deals = fetch_deals(error_items)
        if not deals:
            logger.warning(
                "[STEP 2] No open/unknown deals found. Will still register cases.")

        deal_by_id: Dict[str, Dict[str, Any]] = {
            (d.get("deal_id") or str(d.get("_id", ""))): d
            for d in deals if d.get("deal_id") or d.get("_id")
        }
        logger.info(
            f"[STEP 2] Deal lookup map built ({len(deal_by_id)} entries)")

        logger.info("[STEP 2.1] Existing cases in DB:")
        for case in collection.find():
            logger.info(
                f"[STEP 2.1.1] [{case.get('case_number')}] Case: {case}")

        # --- Step 3: Playwright scraping ---
        logger.info("[STEP 3] Launching Playwright browser...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context()
            search_page = context.new_page()

            max_nav_retries = 2
            selector = None
            for nav_attempt in range(1, max_nav_retries + 1):
                logger.info(
                    f"[STEP 3] Attempt {nav_attempt}/{max_nav_retries} "
                    f"— navigating to search page: {start_url}")
                search_page.goto(
                    start_url, wait_until="networkidle", timeout=90000)
                search_page.wait_for_timeout(5000)
                dismiss_cookie_banner(search_page)
                try:
                    selector = wait_for_results(search_page)
                    logger.info(f"[STEP 3.1] Selector found: {selector}")
                    break
                except RuntimeError:
                    if nav_attempt == max_nav_retries:
                        raise
                    logger.warning(
                        f"[STEP 3.2] Attempt {nav_attempt} failed to find results, "
                        f"retrying in 10s...")
                    search_page.wait_for_timeout(10000)

            current_page = 1
            while True:
                logger.info(
                    f"\n[STEP 3.3] [Page {current_page}] Collecting case links...")
                search_page.wait_for_timeout(1000)
                links = collect_case_links(search_page, selector)
                logger.info(
                    f"[STEP 3.4] [Page {current_page}] Found {len(links)} case links")
                logger.info(f"[STEP 3.4.1] [{current_page}] links: {links}")

                for item in links:
                    url = item["url"]
                    if url in visited_urls:
                        continue
                    visited_urls.add(url)

                    case_num = extract_case_num(url)
                    if not case_num:
                        logger.warning(
                            f"[STEP 3.5] Could not extract case number from {url}")
                        continue

                    # DB check
                    if case_exists(collection, case_num):
                        logger.info(f"  [{case_num}] Already in DB; skipping")
                        skipped_count += 1
                        continue

                    # Scrape + parse in memory
                    logger.info(
                        f"[STEP 3.6] links=>  [{case_num}] Scraping detail page...")
                    case = scrape_case_detail(context, url)

                    logger.info(f"[STEP 3.7] [{case_num}] Case: {case}")
                    if not case or case.get("error"):
                        parse_error = (
                            case.get(
                                "error") if case else "Scrape/parse failed"
                        )
                        logger.warning(
                            f"[STEP 3.8] [{case_num}] Scrape failed; skipping")
                        collect_error(
                            error_items,
                            str(parse_error),
                            context=scrape_error_context(case, url),
                            case_number=case_num,
                            step="scrape_case_detail",
                        )
                        continue

                    case_title = case.get("case_title") or "N/A"
                    companies = get_companies_from_title(case)
                    logger.info(f"[STEP 3.9] [{case_num}] Title: {case_title}")
                    logger.info(
                        f"[STEP 3.10] [{case_num}] Companies: {companies}")
                    logger.info(f"[STEP 3.11] [{case_num}] Parsed data: case_type={case.get('case_type')} | regulation={case.get('regulation')} | notification_date={case.get('notification_date')} | last_decision_date={case.get('last_decision_date')} | status={case.get('status')}")

                    now_iso = utc_now_iso()

                    # --- LLM #1: deal match ---
                    logger.info(
                        f"[STEP 3.12] [{case_num}] LLM Call #1: deal match (companies={companies})...")
                    match_result = None
                    if deals:
                        try:
                            match_result = match_case_to_deal(companies, deals)
                        except Exception as e:
                            logger.exception(
                                f"[STEP 3.12] [{case_num}] LLM deal match error: {e}")
                            collect_error(
                                error_items,
                                str(e),
                                case_number=case_num,
                                step="match_case_to_deal",
                            )

                    if match_result:
                        matched_deal_id, matched_company, matched_role = match_result
                        logger.info(
                            f"[STEP 3.13] [{case_num}] LLM returned match: deal_id={matched_deal_id}, company={matched_company}, role={matched_role}")
                        deal = deal_by_id.get(matched_deal_id)

                        if not deal:
                            logger.info(
                                f"[STEP 3.14] [{case_num}] deal_id={matched_deal_id} not in cache, querying DB...")
                            try:
                                deals_coll = get_deals_collection()
                                if deals_coll is not None:

                                    raw = deals_coll.find_one(
                                        {"_id": ObjectId(matched_deal_id)})
                                    if raw:
                                        raw["deal_id"] = str(raw["_id"])
                                        deal = raw
                                        logger.info(
                                            f"[STEP 3.15] [{case_num}] Found deal in DB: target={raw.get('target')}, acquirer={raw.get('acquirer')}")
                                    else:
                                        logger.warning(
                                            f"[STEP 3.16] [{case_num}] deal_id={matched_deal_id} not found in DB either")
                            except Exception as e:
                                logger.exception(
                                    f"[STEP 3.17] [{case_num}] Error looking up deal {matched_deal_id}: {e}")
                                collect_error(
                                    error_items,
                                    str(e),
                                    case_number=case_num,
                                    step="deal_lookup",
                                )

                        if deal:
                            target = deal.get("target") or deal.get(
                                "target_name", "N/A")
                            acquirer = deal.get("acquirer") or deal.get(
                                "acquire_name", "N/A")
                            logger.info(
                                f"[STEP 3.18] [{case_num}] Matched deal: target={target} | acquirer={acquirer} | deal_id={matched_deal_id}")

                            subject, html_email = generate_matched_email(
                                case, deal, companies)
                            if not send_email_via_webhook(
                                    subject, html_email, case_num, case_title, deal_id=matched_deal_id):
                                collect_error(
                                    error_items,
                                    "Failed to send matched-case notification email",
                                    case_number=case_num,
                                    step="send_email_via_webhook",
                                )

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
                            else:
                                collect_error(
                                    error_items,
                                    "Failed to insert matched case into DB",
                                    case_number=case_num,
                                    step="insert_case",
                                )
                            continue
                        else:
                            logger.warning(
                                f"[STEP 3.19] [{case_num}] LLM returned deal_id={matched_deal_id} but deal not found anywhere; falling through to USA check")

                    # --- LLM #2: USA check ---
                    logger.info(
                        f"  [{case_num}] LLM Call #2: USA-related check (companies={companies})...")
                    try:
                        is_usa = verify_usa_relation(
                            company_details=companies, case_type="FS")
                        logger.info(
                            f"[STEP 3.20] [{case_num}] USA check result: {is_usa}")
                    except Exception as e:
                        logger.exception(
                            f"[STEP 3.21] [{case_num}] USA check error: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            case_number=case_num,
                            step="verify_usa_relation",
                        )
                        is_usa = False

                    if is_usa:
                        logger.info(
                            f"[STEP 3.22] [{case_num}] USA-related case detected — sending email")
                        subject, html_email = generate_usa_email(
                            case, companies)
                        if not send_email_via_webhook(
                                subject, html_email, case_num, case_title, usa_related=True):
                            collect_error(
                                error_items,
                                "Failed to send USA-related notification email",
                                case_number=case_num,
                                step="send_email_via_webhook",
                            )

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
                            collect_error(
                                error_items,
                                "Failed to insert USA-related case into DB",
                                case_number=case_num,
                                step="insert_case",
                            )
                    else:
                        logger.info(
                            f"[STEP 3.23] [{case_num}] No match, not USA-related — saving to DB (no email)")
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
                            collect_error(
                                error_items,
                                "Failed to insert case into DB",
                                case_number=case_num,
                                step="insert_case",
                            )

                if max_pages is not None and current_page >= max_pages:
                    logger.info(
                        f"[STEP 3.24] Reached max pages limit ({max_pages})")
                    break

                if not click_next_page(search_page):
                    logger.info(
                        f"[STEP 3.25] No more pages (next button not found or disabled)")
                    break

                selector = wait_for_results(search_page)
                current_page += 1

            context.close()
            browser.close()
            logger.info(f"[STEP 3.26] Browser closed")

    except Exception as e:
        logger.exception(f"Unhandled error in run(): {e}")
        collect_error(
            error_items,
            f"Unhandled error in run(): {e}",
            context={"start_url": start_url},
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(
            f"[STEP 3.28] Total URLs visited           : {len(visited_urls)}")
        logger.info(f"  New cases inserted           : {new_count}")
        logger.info(
            f"[STEP 3.29] Skipped (already in DB)      : {skipped_count}")
        logger.info(
            f"[STEP 3.30] Errors encountered           : {len(error_items)}")
        logger.info(f"[STEP 3.31] Total time                   : {elapsed}s")
        logger.info("=" * 60)


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
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        logger.exception(f"Unhandled error in __main__: {e}")
        send_error_email(
            script_name=SCRIPT_NAME,
            error_message=f"Unhandled error in __main__: {e}",
            context={"step": "__main__"},
            traceback_str=traceback.format_exc(),
        )
        raise
