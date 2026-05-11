"""
NZ Commerce Commission (ComCom) Case Register → nz_cases collection
===================================================================
Scrapes Open cases from the ComCom case register (open_date = last 7 days),
fetches each case detail page, and inserts new cases into MongoDB 'nz_cases'
collection. Skips records whose case number already exists in nz_cases.
"""

import os
import sys
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from error_email_service import send_error_email
from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)

from log_utils import cleanup_old_logs, refresh_log_file

load_dotenv(".env")

# -----------------------------------------------------------------------------
# Logging — production setup (RotatingFileHandler, IST, env-based settings)
# -----------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "newzealand_cases_register"
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


def _log_critical_error_and_email(msg: str, context: Optional[Dict[str, Any]] = None):
    """Immediate error email — use ONLY for critical startup / fatal failures."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context,
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


# Constants
BASE_URL = "https://www.comcom.govt.nz"
ENV_PATH = ".env"
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/b3007d21-6845-47b5-aece-7b26583758bc",
)

# OpenAI client for LLM matching
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# List URL: status=Open, open_date = one week ago from today
# LIST_URL_TEMPLATE = (
#     "https://www.comcom.govt.nz/case-register/"
#     "?q=&size=200&filters%5Bstatus%5D=Open"
# )

LIST_URL_TEMPLATE = (
    "https://www.comcom.govt.nz/case-register/"
    "?q=&size=50&filters%5Bstatus%5D=Open&filters%5Bopen_date%5D={open_date}"
)


def make_absolute_url(href: str, base: str = BASE_URL) -> str:
    """Convert relative or protocol-relative href to full ComCom URL."""
    if not href or not href.strip():
        return ""
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    base = base.rstrip("/")
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def get_open_date_one_week_ago() -> str:
    """Return open_date as YYYY-MM-DD for one week ago from today."""
    d = datetime.now().replace(hour=0, minute=0, second=0,
                               microsecond=0) - timedelta(days=7)
    return d.strftime("%Y-%m-%d")


def get_nz_cases_collection():
    """Get the 'nz_cases' collection from the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["nz_cases"]


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def get_open_deals_for_matching() -> List[Dict[str, Any]]:
    """Fetch deals with deal_status in Open/Unknown/None for LLM matching."""
    try:
        collection = get_deals_collection()
        if collection is None:
            return []
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(collection.find(status_filter))
        for d in deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])
                d.pop("_id", None)
        return deals
    except Exception as e:
        _log_critical_error_and_email(
            f"Error fetching deals: {e}",
            {"step": "get_open_deals_for_matching"},
        )
        return []


def match_case_to_deal(
    title: str, parties: str, description: str, deals: List[Dict[str, Any]]
) -> Optional[str]:
    """
    Ask LLM if this NZ case matches any deal. Returns deal_id or None.
    Reference: nz_cases_update_monitor.py
    """
    if not deals:
        return None

    lines = []
    for d in deals:
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {target} | Acquirer: {acquirer}"
        for alias_key in ("target_aliases", "parent_aliases"):
            aliases = d.get(alias_key) or []
            if aliases:
                line += f" | {alias_key}: {', '.join(str(a) for a in aliases)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""You are an expert M&A deal matcher. Determine whether this NZ Commerce Commission case directly refers to a specific deal in our deals database.

DEALS DATABASE:
{deals_text}

NZ CASE:
- Title: {title}
- Parties: {parties}
- Description: {description}

INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned in the NZ case text (title, parties, description).
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the NZ case text.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the NZ case text.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the NZ case text:
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
8. If the NZ case does not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
If BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY: Match: DEAL_ID
If no deal satisfies this rule, respond exactly: None"""

    try:
        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert M&A deal matcher. Respond only with Match: DEAL_ID or None.",
                },
                {"role": "user", "content": prompt},
            ]
        )
        content = (res.choices[0].message.content or "").strip()
        tokens_used = getattr(res.usage, "total_tokens",
                              "N/A") if res.usage else "N/A"
        logger.info(
            f"   LLM match — input: title={title[:60]}, parties={parties[:60]}")
        logger.info(
            f"   LLM match raw response: {content} (tokens={tokens_used})")
        if not content.lower().startswith("match"):
            logger.info(f"   LLM match result: None")
            return None
        try:
            _prefix, deal_id_raw = content.split(":", 1)
            deal_id = deal_id_raw.strip() or None
            logger.info(f"   LLM match result: deal_id={deal_id}")
            return deal_id
        except Exception:
            logger.warning(f"   LLM match result: malformed response")
            return None
    except Exception as e:
        logger.exception(f"LLM match error: {e}")
        return None


def _post_webhook(payload: Dict[str, Any]) -> bool:
    logger.info(f"   Sending email: {payload.get('subject', 'N/A')}")
    try:
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"   Email sent successfully (status={resp.status_code})")
        return True
    except Exception as e:
        logger.exception(f"Error sending email via webhook: {e}")
        return False


def send_nz_new_case_matched_email(case_info: Dict[str, Any], deal_id: str) -> bool:
    """Send matched NZ case email via webhook (new case)."""
    details = case_info.get("case_details") or {}
    case_number = details.get("Case number", "N/A")
    title = case_info.get("title", "N/A")
    parties = details.get("Parties", "")
    detail_url = case_info.get("detail_url", "")

    prefix = "[FRMD]" if deal_id else "[FRUD]"
    subject = f"{prefix} NZ Case (New) – {case_number}: {title}"
    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NZ New Case</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:24px;">
  <div style="background:#e0f2fe;border-radius:8px;padding:16px;margin-bottom:16px;border-left:4px solid #0284c7;">
    <div style="font-size:16px;font-weight:800;color:#0369a1;">Matched deal</div>
    <div style="font-size:14px;color:#0c4a6e;margin-top:6px;"><b>Deal ID:</b> {deal_id}</div>
  </div>
  <div style="font-size:18px;font-weight:800;margin-bottom:6px;">{title}</div>
  <div style="font-size:14px;color:#64748b;margin-bottom:14px;">Case number: {case_number}</div>
  <div style="font-size:14px;line-height:1.5;margin-bottom:14px;"><b>Parties:</b> {parties or '—'}</div>
  <a href="{detail_url}" target="_blank" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:800;">View case details →</a>
</div></body></html>"""

    payload = {
        "subject": subject,
        "html": html,
        "deal_id": deal_id,
        "case_number": case_number,
        "case_title": title,
        "case_url": detail_url,
        "source": "nz_comcom_case_register_to_db",
        "is_new_case": True,
    }
    return _post_webhook(payload)


def send_unmatched_nz_usa_email_via_webhook(case_info: Dict[str, Any]) -> bool:
    """Send USA-related unmatched NZ case email via webhook."""
    details = case_info.get("case_details") or {}
    case_number = details.get("Case number", "N/A")
    category = details.get("Category", "N/A")
    status = details.get("Status", "N/A")
    date_opened = details.get("Date opened", "N/A")
    title = case_info.get("title", "N/A")
    detail_url = case_info.get("detail_url", "")

    subject = f"[FRUD] NZ Case (USA-Related) – {case_number}"
    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>USA-Related NZ Case</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
  <div style="background:#dbeafe;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid #3b82f6;">
    <div style="font-size:16px;font-weight:800;color:#1e40af;">🇺🇸 USA-Related NZ Case</div>
    <div style="font-size:14px;color:#1e3a8a;margin-top:6px;">This case appears to involve USA-related companies.</div>
  </div>
  <div style="font-size:18px;font-weight:800;margin-bottom:8px;">{title}</div>
  <div style="font-size:14px;color:#64748b;">Case number: {case_number} | Category: {category} | Status: {status} | Opened: {date_opened}</div>
  <div style="margin-top:20px;">
    <a href="{detail_url}" target="_blank" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:800;">View case details →</a>
  </div>
</div></body></html>"""

    payload = {
        "subject": subject,
        "html": html,
        "deal_id": "N/A",
        "target": "N/A",
        "acquirer": "N/A",
        "case_number": case_number,
        "case_title": title,
        "detail_url": detail_url,
        # "usa_related": True,
        "is_unmatched": True,
        "source": "nz_comcom_case_register_to_db",
        "is_new_case": True,
    }
    return _post_webhook(payload)


def extract_list_items_from_html(html_content: str) -> List[Dict[str, Any]]:
    """Extract case cards from the case register list HTML (ol.filter__results-list). Skip 'Withheld'."""
    soup = BeautifulSoup(html_content, "html.parser")
    list_el = soup.select_one("ol.filter__results-list")
    if not list_el:
        return []

    items = []
    for li in list_el.select("li"):
        try:
            link = li.select_one("a.card__link")
            if not link:
                continue

            title = (link.get_text(strip=True) or "").strip()
            # if not title or title.lower() == "withheld":
            #     continue

            href = link.get("href", "")
            if href and not href.startswith("http"):
                detail_url = BASE_URL.rstrip("/") + "/" + href.lstrip("/")
            else:
                detail_url = href or ""

            status_el = li.select_one(".card__status")
            status = status_el.get_text(strip=True) if status_el else ""

            tag_el = li.select_one(".card__tag")
            tag = tag_el.get_text(strip=True) if tag_el else ""

            outcome = ""
            info_detail = li.select_one(".card__info-detail")
            if info_detail:
                title_span = info_detail.select_one(".card__info-title")
                if title_span and "outcome" in title_span.get_text(strip=True).lower():
                    val = info_detail.select_one("span:not(.card__info-title)")
                    if val:
                        outcome = val.get_text(strip=True)

            items.append({
                "title": title,
                "detail_url": detail_url,
                "status": status,
                "tag": tag,
                "outcome": outcome,
            })
        except Exception as e:
            print(f"   ⚠️ Error parsing list item: {e}")
            continue

    return items


def parse_case_details(soup: BeautifulSoup) -> Dict[str, str]:
    """Parse .case-details__record blocks into a dict (title -> value)."""
    details = {}
    for rec in soup.select(".case-details__record"):
        title_el = rec.select_one(".case-details__record-title")
        value_el = rec.select_one(".case-details__record-value")
        if title_el and value_el:
            key = title_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            if key:
                details[key] = value
    return details


def parse_timeline(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Parse timeline blocks into list of {date, title, status, has_link}."""
    entries = []
    for block in soup.select(".timeline-block"):
        date_el = block.select_one(".timeline-block__timeline-date")
        title_el = block.select_one(".timeline-block__content-title")
        status_el = block.select_one(".timeline-block__content-status")
        link_el = block.select_one(".timeline-block__content-link")
        date_str = " ".join(date_el.get_text(
            strip=True).split()) if date_el else ""
        entries.append({
            "date": date_str,
            "title": title_el.get_text(strip=True) if title_el else "",
            "status": status_el.get_text(strip=True) if status_el else "",
            "has_link": link_el is not None,
        })
    return entries


def fetch_case_detail_page(page, url: str) -> Optional[Dict[str, Any]]:
    """Fetch case detail page and optional sections (documents, media). Returns case detail dict."""
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        try:
            view_all = page.get_by_role("button", name="View All")
            if view_all.count() > 0:
                view_all.first.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        desc_el = soup.select_one(".content-block__content p")
        description = desc_el.get_text(strip=True) if desc_el else ""

        case_details = parse_case_details(soup)
        timeline = parse_timeline(soup)

        documents_section: List[Dict[str, str]] = []
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["section"] = ["documents"]
        docs_query = urlencode(qs, doseq=True)
        docs_url = urlunparse((parsed.scheme, parsed.netloc,
                              parsed.path, parsed.params, docs_query, parsed.fragment))
        try:
            page.goto(docs_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            try:
                view_all_docs = page.get_by_role("button", name="View All")
                if view_all_docs.count() > 0:
                    view_all_docs.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            doc_soup = BeautifulSoup(page.content(), "html.parser")
            for link in doc_soup.select(".project-block__content a[href]"):
                href = link.get("href")
                if href and (href.endswith(".pdf") or "document" in href.lower() or "documents" in href):
                    documents_section.append({
                        "title": link.get_text(strip=True) or href,
                        "url": make_absolute_url(href),
                    })
            for block in doc_soup.select(".timeline-block"):
                title_el = block.select_one(".timeline-block__content-title")
                if title_el:
                    documents_section.append({
                        "title": title_el.get_text(strip=True),
                        "url": "",
                    })
        except Exception as e:
            print(f"   ⚠️ Error fetching documents section: {e}")

        media_section: List[Dict[str, str]] = []
        qs_media = parse_qs(parsed.query)
        qs_media["section"] = ["media"]
        media_query = urlencode(qs_media, doseq=True)
        media_url = urlunparse((parsed.scheme, parsed.netloc,
                               parsed.path, parsed.params, media_query, parsed.fragment))
        try:
            page.goto(media_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            try:
                view_all_media = page.get_by_role("button", name="View All")
                if view_all_media.count() > 0:
                    view_all_media.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            media_soup = BeautifulSoup(page.content(), "html.parser")
            for block in media_soup.select(".timeline-block"):
                title_el = block.select_one(".timeline-block__content-title")
                link_el = block.select_one("a[href]")
                date_el = block.select_one(".timeline-block__timeline-date")
                raw_href = (link_el.get("href") or "") if link_el else ""
                media_section.append({
                    "date": " ".join(date_el.get_text(strip=True).split()) if date_el else "",
                    "title": title_el.get_text(strip=True) if title_el else "",
                    "url": make_absolute_url(raw_href),
                })
        except Exception as e:
            print(f"   ⚠️ Error fetching media section: {e}")

        return {
            "description": description,
            "case_details": case_details,
            "timeline": timeline,
            "documents": documents_section,
            "updates_media": media_section,
        }
    except Exception as e:
        logger.exception(f"Error fetching detail page {url}: {e}")
        return None


def detail_url_exists(collection, detail_url: str) -> bool:
    """Check if a document with this detail_url already exists in nz_cases."""
    if collection is None or not detail_url or not detail_url.strip():
        return False
    return collection.find_one({"detail_url": detail_url.strip()}) is not None


def upsert_nz_case_by_detail_url(collection, detail_url: str, doc: Dict[str, Any]) -> str:
    """
    Upsert a nz_cases record keyed by detail_url.
    Preserves existing created_at if present.

    Returns: "inserted" or "updated"
    """
    if collection is None:
        raise ValueError("collection is None")
    detail_url = (detail_url or "").strip()
    if not detail_url:
        raise ValueError("detail_url is empty")

    existing = collection.find_one({"detail_url": detail_url})
    now_iso = utc_now_iso()

    out = dict(doc)
    if existing:
        if existing.get("created_at") and not out.get("created_at"):
            out["created_at"] = existing["created_at"]
        if "deal_id" in existing and "deal_id" not in out:
            out["deal_id"] = existing["deal_id"]
        out["updated_at"] = now_iso
        out["scraped_at"] = now_iso
        collection.update_one({"detail_url": detail_url}, {
                              "$set": out}, upsert=False)
        return "updated"

    out.setdefault("created_at", now_iso)
    out["updated_at"] = now_iso
    out["scraped_at"] = now_iso
    collection.update_one({"detail_url": detail_url},
                          {"$set": out}, upsert=True)
    return "inserted"


def build_case_document(list_item: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """Build the document to insert into nz_cases."""
    case_details = detail.get("case_details") or {}
    case_number = case_details.get("Case number", "").strip()

    doc = {
        "title": list_item.get("title", ""),
        "detail_url": list_item.get("detail_url", ""),
        "description": detail.get("description", ""),
        "case_details": case_details,
        "timeline": detail.get("timeline", []),
        "documents": detail.get("documents", []),
        "updates_media": detail.get("updates_media", []),
        "case_number": case_number,
        "scraped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    # List-level fields from list page
    doc["status"] = list_item.get("status", "")
    doc["tag"] = list_item.get("tag", "")
    doc["outcome"] = list_item.get("outcome", "")
    return doc


def run():
    """Main: build list URL, scrape list, for each item fetch detail, check nz_cases by detail_url, insert if new."""
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    env_flag = os.getenv("NZ_CASES_TEST_MODE", "").lower()
    test_mode = env_flag in ("1", "true", "yes", "y")
    mode_label = "TEST MODE" if test_mode else "LIVE MODE"

    logger.info("=" * 60)
    logger.info(f"Starting NZ Case Register ({mode_label})")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    logger.info("[STEP 1] Initializing MongoDB connection...")

    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        _log_critical_error_and_email(f"MongoDB connection failed: {message}", {
                                      "step": "mongodb_connect"})
        return
    logger.info(f"[STEP 1.1] MongoDB: {message}")

    collection = get_nz_cases_collection()
    if collection is None:
        _log_critical_error_and_email("[STEP 1.2] nz_cases collection not available", {
                                      "step": "get_collection"})
        return

    deals = get_open_deals_for_matching()
    logger.info(f"[STEP 1.3] Loaded {len(deals)} deals for matching")

    open_date = get_open_date_one_week_ago()
    list_url = LIST_URL_TEMPLATE.format(open_date=open_date)
    logger.info(f"[STEP 1.4] List URL (open_date={open_date}): {list_url}")

    inserted = 0
    skipped = 0
    updated = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # [STEP 2] Fetch list page and extract items
        page.goto(list_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        list_html = page.content()
        items = extract_list_items_from_html(list_html)
        logger.info(
            f"[STEP 2] Found {len(items)} list items from ol.filter__results-list")

        for i, list_item in enumerate(items, 1):
            title = list_item.get("title", "?")
            detail_url = list_item.get("detail_url", "")
            if not detail_url:
                logger.warning(
                    f"[STEP 2.1] [{i}/{len(items)}] No detail URL, skipping: {title}")
                continue

            logger.info(f"[STEP 2.2] [{i}/{len(items)}] {title}")

            if not test_mode and detail_url_exists(collection, detail_url):
                logger.info(f"[STEP 2.3] detail_url already in nz_cases, skip")
                skipped += 1
                continue

            detail = fetch_case_detail_page(page, detail_url)
            if not detail:
                logger.warning(f"[STEP 2.4] Could not fetch detail, skipping")
                error_items.append(
                    {"title": title, "error": "Detail fetch failed", "step": "fetch_case_detail_page"})
                continue

            doc = build_case_document(list_item, detail)

            # Add/refresh timestamps
            now_iso = utc_now_iso()
            doc.setdefault("created_at", now_iso)
            doc["updated_at"] = now_iso

            # 2-step LLM flow (reference: nz_cases_update_monitor.py)
            parties = (doc.get("case_details") or {}).get("Parties", "")
            description = doc.get("description", "")
            deal_id = match_case_to_deal(
                title or "", parties, description or "", deals)

            if deal_id:
                doc["deal_id"] = deal_id
                logger.info(f"[STEP 2.5] Deal match found (deal_id={deal_id})")
                if not test_mode:
                    send_nz_new_case_matched_email(doc, deal_id)
            else:
                try:
                    nz_details = {
                        "title": title,
                        "parties": parties,
                        "description": description,
                        "case_details": doc.get("case_details"),
                        "detail_url": detail_url,
                        "tag": doc.get("tag", ""),
                        "status": doc.get("status", ""),
                    }
                    is_usa = bool(
                        verify_usa_relation(
                            company_details=nz_details, case_type="NZ")
                    )
                except Exception as e:
                    logger.exception(f"[STEP 2.6] USA verification error: {e}")
                    error_items.append(
                        {"title": title, "error": str(e), "step": "verify_usa_relation"})
                    is_usa = False

                if is_usa:
                    logger.info("[STEP 2.7] USA-related (unmatched)")
                    if not test_mode:
                        send_unmatched_nz_usa_email_via_webhook(doc)

            try:
                if test_mode:
                    action = upsert_nz_case_by_detail_url(
                        collection, detail_url, doc)
                    if action == "inserted":
                        inserted += 1
                    else:
                        updated += 1
                    logger.info(
                        f"[STEP 2.8] Upserted into nz_cases ({action})")
                else:
                    collection.insert_one(doc)
                    case_number = (doc.get("case_number") or "").strip()
                    extra = f" case_number={case_number}" if case_number else ""
                    logger.info(
                        f"[STEP 2.9] Inserted into nz_cases (detail_url){extra}")
                    inserted += 1
            except Exception as e:
                logger.exception(f"[STEP 2.10] Insert failed: {e}")
                error_items.append(
                    {"title": title, "error": str(e), "step": "insert_case"})

        browser.close()
        logger.info("[STEP 2.11] Browser closed")

    if error_items:
        logger.warning(
            f"[STEP 2.12] {len(error_items)} per-case errors collected — sending summary email")
        send_error_email(
            script_name=SCRIPT_NAME,
            error_message=f"{len(error_items)} errors occurred during run",
            context={
                "error_count": len(error_items),
                "errors": error_items[:20],
            },
            traceback_str=None,
        )

    elapsed = round(time.time() - run_start, 1)
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"[STEP 2.13] Inserted                     : {inserted}")
    logger.info(f"[STEP 2.14] Updated                      : {updated}")
    logger.info(f"[STEP 2.15] Skipped (already in DB)      : {skipped}")
    logger.info(
        f"[STEP 2.16] Errors encountered           : {len(error_items)}")
    logger.info(f"[STEP 2.17] Total time                   : {elapsed}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        _log_critical_error_and_email(
            f"Unhandled error in __main__: {e}", {"step": "__main__"})
        raise
