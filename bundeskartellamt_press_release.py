"""
Bundeskartellamt Press Release scraper.

Workflow:
1. Fetch deals from MongoDB (Open/Unknown/null/missing status)
2. Fetch all URLs from german_press_releases collection for dedup
3. Fetch HTML from Expertensuche press releases URL
4. Extract items from search result list (raw German — no translation yet)
5. Apply 30-day cutoff date filter
6. Skip records whose URL is already in german_press_releases
7. For each new record: translate title, run LLM deal match via deal_match_llm
8. If matched → set deal_id; else → run USA-relation check
9. Upsert to german_press_releases; on first insert only: send [FRMD] or [FRUD] email
"""

import json
import logging
import os
import re
import time
from datetime import datetime, date, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from html import escape as escape_html

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from deal_match_llm import llm_match_deal_id, fetch_open_deals
from email_subject_builder import build_subject
from llm_verification_service import verify_country_relation
from mongodb_connection import get_database, is_connected, init_mongodb_connection
from n8n_email_service import post_email_payload
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SCRIPT_NAME = "germany_press_release"

PRESS_RELEASE_BASE = "https://www.bundeskartellamt.de/SiteGlobals/Forms/Suche/Expertensuche_Formular.html"
PRESS_RELEASE_PARAMS = "cl2Categories_CategorizedFormat=pressemeldungen_aktuelles&pageLocale=de&resultsPerPage=30&sortOrder=dateOfIssue_dt+desc"
PRESS_RELEASE_URL = f"{PRESS_RELEASE_BASE}?{PRESS_RELEASE_PARAMS}#resultsperpage-51534"

EXTRACTED_RECORDS_JSON = "bundeskartellamt_press_release_extracted.json"

CUTOFF_DATE = (datetime.now() - timedelta(days=30)).replace(
    hour=0, minute=0, second=0, microsecond=0
)

PERSISTENT_LOG_DIR = "/var/data/logs"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))
IST = timezone(timedelta(hours=5, minutes=30))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger("bundeskartellamt_press_release")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(_file_handler)
logger.addHandler(_console_handler)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# MongoDB collection
# ---------------------------------------------------------------------------

def get_german_press_releases_collection():
    db = get_database()
    if db is None:
        return None
    return db["german_press_releases"]


def fetch_existing_press_release_urls(collection) -> set:
    """Return all non-empty URL strings already in german_press_releases."""
    out: set = set()
    try:
        cursor = collection.find({}, {"url": 1, "_id": 0})
        for doc in cursor:
            url = doc.get("url")
            if isinstance(url, str) and url.strip():
                out.add(url.strip())
        logger.info("Loaded %d existing press release URLs for dedup", len(out))
    except Exception as e:
        logger.warning("Error fetching existing press release URLs: %s", e)
    return out


def upsert_press_release(collection, doc: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Upsert by url. Returns (mongo_id as str, inserted_new)."""
    url = (doc.get("url") or "").strip()
    if not url:
        logger.warning("upsert_press_release: missing url, skipping")
        return None, False

    now = utc_now_iso()
    payload = {k: v for k, v in doc.items() if k not in ("_id", "created_at")}
    payload["url"] = url
    payload["updated_at"] = now

    try:
        result = collection.update_one(
            {"url": url},
            {"$set": payload, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        inserted_new = result.upserted_id is not None
        if inserted_new:
            oid = result.upserted_id
        else:
            row = collection.find_one({"url": url}, {"_id": 1})
            oid = row["_id"] if row else None
        return (str(oid) if oid is not None else None), inserted_new
    except Exception as e:
        logger.warning("Error upserting press release: %s", e)
        return None, False


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate_to_english(text: str) -> str:
    """Translate German text to English using GPT-5.2."""
    if not text or not text.strip():
        return ""
    text = text.strip()
    try:
        response = openai_client.chat.completions.create(
            model="gpt-5.2",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a professional German-to-English translator for merger control "
                        "and regulatory press release titles. "
                        "Rules:\n"
                        "1. Return ONLY the translated English title.\n"
                        "2. Use well-known official English company names where possible.\n"
                        "3. Do NOT explain or add alternatives.\n"
                        "4. Preserve regulatory meaning naturally."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Translate this German regulatory press release title to English:\n{text}",
                },
            ],
        )
        result = (response.choices[0].message.content or "").strip()
        if result:
            return result
    except Exception as e:
        logger.warning("Translation failed for: %s... → %s", text[:50], e)
    return "[Translation failed]"


# ---------------------------------------------------------------------------
# HTML extraction (raw German — no translation)
# ---------------------------------------------------------------------------

def parse_press_date(date_str: str):
    """Parse date from press release topline. Returns date object or None."""
    if not date_str or not date_str.strip():
        return None
    s = date_str.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def extract_press_results(html_content: str) -> List[Dict]:
    """
    Extract press release items from search results HTML.
    Structure: section#searchResults or .l-searchresult-list, items .l-searchresult-list__item.
    Each item: h3.c-searchresult__headline > a (title, href), p.c-topline (category, date).
    Returns raw German titles — no translation here.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    records = []

    section = soup.find("section", id="searchResults") or soup.find(
        "div", class_=re.compile(r"l-searchresult-list")
    )
    if not section:
        items = soup.find_all("div", class_=re.compile(
            r"l-searchresult-list__item"))
    else:
        items = section.find_all(
            "div", class_=re.compile(r"l-searchresult-list__item"))

    for item in items:
        try:
            headline_el = item.find(
                "h3", class_=re.compile(r"c-searchresult__headline"))
            if not headline_el:
                continue
            link = headline_el.find("a", href=True)
            if not link:
                continue

            title = link.get_text(separator=" ", strip=True)
            title = re.sub(r"\s+", " ", title).strip()
            url = link.get("href", "").strip()
            if url and not url.startswith("http"):
                url = requests.compat.urljoin(
                    "https://www.bundeskartellamt.de", url)

            topline = item.find("p", class_=re.compile(
                r"c-searchresult__topline"))
            category = ""
            date_str = ""
            if topline:
                spans = topline.find_all(
                    "span", class_=re.compile(r"c-topline__item"))
                if len(spans) >= 1:
                    category = spans[0].get_text(strip=True)
                if len(spans) >= 2:
                    date_str = spans[1].get_text(strip=True)

            record = {
                "title": title,
                "url": url,
                "date_str": date_str,
                "date": parse_press_date(date_str),
                "category": category,
            }
            records.append(record)
            logger.info("Extracted: %s – %s...", date_str, title[:60])
        except Exception as e:
            logger.warning("Error extracting item: %s", e)
            continue

    return records


def filter_by_cutoff_date(records: List[Dict], cutoff_date=None) -> List[Dict]:
    """Keep only records with date >= cutoff. Records with no parseable date pass through."""
    if cutoff_date is None:
        cutoff_date = CUTOFF_DATE
    cutoff = cutoff_date.date() if isinstance(
        cutoff_date, datetime) else cutoff_date
    filtered = []
    for r in records:
        d = r.get("date")
        if d is None or d >= cutoff:
            filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _safe(val) -> str:
    if val is None or (isinstance(val, str) and not val.strip()):
        return "N/A"
    return escape_html(str(val).strip())


def _build_case_rows_html(record: Dict) -> str:
    cell = "padding:8px; color:#333; word-wrap:break-word; white-space:normal; max-width:600px;"
    rows = [
        ("Date", record.get("date_str")),
        ("Category", record.get("category")),
        ("Title (German)", record.get("title_german") or record.get("title")),
        ("Title (English)", record.get("title_english")),
        ("URL", record.get("url")),
    ]
    html = ""
    for i, (label, value) in enumerate(rows):
        bg = ' style="background-color:#f9f9f9;"' if i % 2 == 1 else ""
        if label == "URL" and value:
            cell_content = f'<a href="{escape_html(str(value))}" target="_blank" style="color:#2563eb;">{_safe(value)}</a>'
        else:
            cell_content = _safe(value)
        html += (
            f'<tr{bg}>'
            f'<td style="padding:8px; font-weight:bold; width:170px; color:#555;">{label}:</td>'
            f'<td style="{cell}">{cell_content}</td>'
            f'</tr>\n'
        )
    return html


def generate_matched_email(record: Dict, deal: Dict) -> Tuple[str, str]:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = deal.get("deal_id", "N/A")

    subject = build_subject("bundeskartellamt", "press_release", deal)

    deal_banner = (
        f'<div style="background:#dbeafe;border-radius:6px;padding:14px 20px;'
        f'margin-bottom:18px;border-left:4px solid #2563eb;">'
        f"<strong>Matched Deal:</strong> {_safe(target)} / {_safe(acquirer)}<br>"
        f"<strong>Deal ID:</strong> {_safe(deal_id)}"
        f"</div>"
    )

    case_rows = _build_case_rows_html(record)
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #2563eb;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Bundeskartellamt Press Release</p>
  {deal_banner}
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_rows}</table>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    <p>Automated email from Bundeskartellamt Press Release scraper.</p>
  </div>
</div></body></html>"""
    return subject, html


def generate_usa_email(record: Dict) -> Tuple[str, str]:
    subject = build_subject("bundeskartellamt", "press_release")

    usa_banner = (
        '<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;'
        'margin-bottom:18px;border-left:4px solid #f59e0b;">'
        "<strong>🇺🇸 USA-Related Case</strong> — No deal match found, but this press release appears related to the United States."
        "</div>"
    )

    case_rows = _build_case_rows_html(record)
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #f59e0b;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Bundeskartellamt Press Release</p>
  {usa_banner}
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_rows}</table>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    <p>Automated email from Bundeskartellamt Press Release scraper.</p>
  </div>
</div></body></html>"""
    return subject, html


def send_email_via_webhook(
    subject: str, html: str, url: str = "", deal_id: str = None
) -> bool:
    try:
        payload = {
            "subject": subject,
            "html": html,
            "view_url": url,
        }
        if deal_id:
            payload["deal_id"] = deal_id
        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.warning("Email send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    """
    Fetch press release listing, filter by 30-day cutoff, skip known URLs,
    translate + LLM-match new records, run USA check on misses,
    upsert to german_press_releases, email on first insert.
    """
    error_items: List[Dict[str, Any]] = []
    all_after_cutoff: List[Dict[str, Any]] = []
    new_records: List[Dict[str, Any]] = []
    stats = {"saved": 0, "matched": 0, "usa_related": 0, "silent": 0}

    logger.info("=" * 60)
    logger.info("BUNDESKARTELLAMT PRESS RELEASE SCRAPER")
    logger.info("=" * 60)

    try:
        # --- Step 1: Init MongoDB ---
        success, message = init_mongodb_connection(".env")
        if not success:
            logger.error("MongoDB init failed: %s", message)
            collect_error(
                error_items, f"MongoDB init failed: {message}", step="init_mongodb_connection")
            return {"success": False, "error": message}

        collection = get_german_press_releases_collection()
        if collection is None:
            msg = "german_press_releases collection not available"
            logger.error(msg)
            collect_error(error_items, msg,
                          step="get_german_press_releases_collection")
            return {"success": False, "error": msg}

        # --- Step 2: Load deals ---
        deals = fetch_open_deals()
        deal_by_id = {d["deal_id"]: d for d in deals if d.get("deal_id")}
        logger.info("Loaded %d open/unknown deals", len(deals))

        # --- Step 3: Fetch HTML ---
        logger.info("Fetching HTML from %s", PRESS_RELEASE_URL)
        html_content = None
        max_retries = 3
        wait_seconds = 5
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(PRESS_RELEASE_URL, timeout=30)
                response.raise_for_status()
                html_content = response.text
                logger.info("HTML fetched (%d bytes)", len(html_content))
                break
            except Exception as e:
                logger.warning("Attempt %d/%d failed: %s",
                               attempt, max_retries, e)
                if attempt < max_retries:
                    logger.info("Waiting %ds before retry...", wait_seconds)
                    time.sleep(wait_seconds)
                else:
                    logger.error("All %d attempts failed.", max_retries)
                    collect_error(
                        error_items,
                        f"Failed to fetch press release page: {e}",
                        step="fetch_press_release_page",
                        context={"url": PRESS_RELEASE_URL},
                    )
                    return {"success": False, "error": str(e)}

        # --- Step 4: Extract (raw German, no translation) ---
        logger.info("Extracting press release list...")
        records = extract_press_results(html_content)
        logger.info("Extracted %d items", len(records))

        # --- Step 5: Cutoff filter ---
        logger.info("Applying cutoff date (>= %s)...", CUTOFF_DATE.date())
        all_after_cutoff = filter_by_cutoff_date(records, CUTOFF_DATE)
        logger.info("After cutoff: %d records", len(all_after_cutoff))

        # --- Save debug JSON (raw, before URL dedup) ---
        records_serializable = []
        for r in all_after_cutoff:
            rec = dict(r)
            if isinstance(rec.get("date"), date):
                rec["date"] = rec["date"].isoformat()
            records_serializable.append(rec)
        try:
            with open(EXTRACTED_RECORDS_JSON, "w", encoding="utf-8") as f:
                json.dump(records_serializable, f,
                          ensure_ascii=False, indent=2)
            logger.info("Saved extracted records to %s",
                        EXTRACTED_RECORDS_JSON)
        except Exception as e:
            logger.warning("Could not save JSON: %s", e)
            collect_error(
                error_items, f"Could not save JSON: {e}", step="write_extracted_json")

        # --- Step 6: URL dedup ---
        existing_urls = fetch_existing_press_release_urls(collection)
        new_records = [
            r for r in all_after_cutoff
            if r.get("url", "").strip() not in existing_urls
        ]
        skipped_existing = len(all_after_cutoff) - len(new_records)
        logger.info(
            "URL dedup: %d skipped (already in DB), %d new to process",
            skipped_existing,
            len(new_records),
        )

        # --- Step 7: Per-record loop ---
        logger.info("=" * 60)
        logger.info("Processing %d new records...", len(new_records))
        logger.info("=" * 60)

        for idx, record in enumerate(new_records, 1):
            title = record.get("title", "")
            url = record.get("url", "").strip()
            logger.info("[%d/%d] %s...", idx, len(new_records), title[:70])

            try:
                if not title or not title.strip():
                    logger.info("  Skipped (no title)")
                    continue
                if not url:
                    logger.info("  Skipped (no url)")
                    continue

                # --- 7a: Translate title (only for new records) ---
                title_en = translate_to_english(title)
                logger.info("  title_en=%s...", title_en[:60])

                # --- 7b: Build document ---
                doc: Dict[str, Any] = {
                    "url": url,
                    "title": title,
                    "title_german": title,
                    "title_english": title_en,
                    "date_str": record.get("date_str", ""),
                    "date": record["date"].isoformat() if isinstance(record.get("date"), date) else (record.get("date") or ""),
                    "category": record.get("category", ""),
                    "deal_id": None,
                }

                # --- 7c: Deal match ---
                deal_match = None
                if title_en and title_en != "[Translation failed]":
                    try:
                        deal_id_result = llm_match_deal_id(
                            regulator_name="German Bundeskartellamt (Press Release)",
                            case_sections={
                                "PRESS RELEASE TITLE (German)": doc["title_german"],
                                "PRESS RELEASE TITLE (English)": doc["title_english"],
                            },
                            source_label="the press release title",
                            deals=deals,
                        )
                        deal_match = deal_by_id.get(
                            deal_id_result) if deal_id_result else None
                    except Exception as e:
                        logger.exception("LLM match failed: %s", e)
                        collect_error(
                            error_items,
                            str(e),
                            step="llm_match_deal_id",
                            context={"title": title[:80], "url": url},
                        )

                # --- 7d: Branch ---
                is_usa = False
                if deal_match:
                    doc["deal_id"] = deal_match.get("deal_id")
                    logger.info("  Matched: deal_id=%s",
                                deal_match.get("deal_id"))
                else:
                    logger.info("  No deal match — running USA check")
                    try:
                        is_usa = verify_country_relation(
                            company_details={
                                "today_date": datetime.now().strftime("%Y-%m-%d"),
                                "record": doc,
                            },
                            country="USA",
                            case_type="GERMANY",
                        )
                    except Exception as e:
                        logger.exception("USA check failed: %s", e)
                        collect_error(
                            error_items,
                            str(e),
                            step="verify_country_relation",
                            context={"url": url},
                        )
                        is_usa = False

                    if is_usa:
                        logger.info("  USA-related")
                    else:
                        logger.info("  Not USA-related → silent save")

                # --- 7e: Save ---
                doc_id, inserted_new = upsert_press_release(collection, doc)
                if doc_id:
                    stats["saved"] += 1
                    existing_urls.add(url)
                    logger.info("  Saved (id=%s, new=%s)",
                                doc_id, inserted_new)

                    # --- 7f: Email only on first insert - --
                    if inserted_new:
                        if deal_match:
                            subject, html_body = generate_matched_email(
                                doc, deal_match)
                            stats["matched"] += 1
                            if not send_email_via_webhook(
                                subject, html_body, url, deal_id=deal_match.get(
                                    "deal_id")
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to send matched email",
                                    step="send_matched_email",
                                    context={"url": url},
                                )
                        elif is_usa:
                            subject, html_body = generate_usa_email(doc)
                            stats["usa_related"] += 1
                            if not send_email_via_webhook(subject, html_body, url):
                                collect_error(
                                    error_items,
                                    "Failed to send USA email",
                                    step="send_usa_email",
                                    context={"url": url},
                                )
                        else:
                            stats["silent"] += 1
                else:
                    logger.error("  Failed to save to german_press_releases")
                    collect_error(
                        error_items,
                        "Failed to save press release",
                        step="upsert_press_release",
                        context={"url": url},
                    )

            except Exception as e:
                logger.exception("Error processing record: %s", e)
                collect_error(
                    error_items,
                    str(e),
                    step="process_record",
                    context={"title": title[:80], "url": url},
                )

        return {
            "success": True,
            "total_extracted": len(all_after_cutoff),
            "skipped_existing_url": skipped_existing,
            "processed_new": len(new_records),
            "saved": stats["saved"],
            "matched_frmd": stats["matched"],
            "usa_frud": stats["usa_related"],
            "silent": stats["silent"],
            "cutoff_date": CUTOFF_DATE.date().isoformat(),
        }

    except Exception as e:
        logger.exception("Unhandled error in main: %s", e)
        collect_error(
            error_items, f"Unhandled error in main: {e}", step="run_main")
        return {"success": False, "error": str(e)}

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        logger.info("=" * 60)
        logger.info("DONE")
        logger.info("  Records after cutoff  : %d", len(all_after_cutoff))
        logger.info("  New processed         : %d", len(new_records))
        logger.info("  Saved                 : %d", stats["saved"])
        logger.info("  Deal matches [FRMD]   : %d", stats["matched"])
        logger.info("  USA-related  [FRUD]   : %d", stats["usa_related"])
        logger.info("  Silent saves          : %d", stats["silent"])
        logger.info("  Errors                : %d", len(error_items))
        logger.info("=" * 60)


if __name__ == "__main__":
    main()
