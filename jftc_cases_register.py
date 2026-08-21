"""
JFTC Japan — Merger press releases → japan_cases collection.

Fetches https://www.jftc.go.jp/en/pressreleases/categories/mergers/index.html,
parses each press-release list item, skips records already in japan_cases,
runs LLM + regex deal matching (both acquirer and target must hit), USA check,
and inserts every new record.

Non-deal items (annual status reports, guidelines) are skipped without insert.

Email routing:
  production  → org-aware post_email_payload
  --test-email → send_direct_email to avshesh.savani@teqnodux.com
"""

import argparse
import os
import re
import time
from datetime import datetime, timezone
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from mongodb_connection import (
    get_database,
    get_deal_by_id,
    init_mongodb_connection,
)
from deal_match_llm import llm_match_deal_id, fetch_open_deals
from deal_match_regex import apply_regex_match_subject, regex_match_jftc_deal
from llm_verification_service import verify_usa_relation
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload, send_direct_email
from scraper_error_utils import collect_error, send_error_summary
from log_utils import ensure_script_logger, refresh_script_log

load_dotenv(".env")

SCRIPT_NAME = "jftc_cases_register"
BASE_URL = "https://www.jftc.go.jp"
LISTING_URL = f"{BASE_URL}/en/pressreleases/categories/mergers/index.html"
TEST_RECIPIENT = "avshesh.savani@teqnodux.com"

logger, get_log_file = ensure_script_logger(SCRIPT_NAME)

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Referer": BASE_URL,
}

# Annual reports / guideline notices — not individual deal reviews
NON_DEAL_TITLE_RE = re.compile(
    r"(?i)("
    r"status of notifications regarding business combinations"
    r"|major business combination cases in fiscal year"
    r"|guidelines to application of the antimonopoly act"
    r"|policies concerning procedures of review of business combination"
    r")"
)


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

def get_japan_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["japan_cases"]


def detail_url_exists(collection, detail_url: str) -> bool:
    if collection is None or not detail_url:
        return False
    return collection.find_one({"detail_url": detail_url.strip()}) is not None


def insert_japan_case(collection, doc: Dict[str, Any]) -> bool:
    try:
        now = _utc_now_iso()
        doc.setdefault("created_at", now)
        doc["updated_at"] = now
        collection.insert_one(doc)
        return True
    except Exception as e:
        logger.warning(f"Insert failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_jftc_date(raw: str) -> str:
    """Convert '2026/4/24' or '2025/06/18' → 'YYYY-MM-DD'."""
    raw = (raw or "").strip()
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})$", raw)
    if m:
        y, month, day = m.groups()
        return f"{y}-{int(month):02d}-{int(day):02d}"
    return raw


def is_non_deal_title(title: str) -> bool:
    return bool(title and NON_DEAL_TITLE_RE.search(title))


EMAIL_BODY_STYLE = (
    "font-size:14px;line-height:1.65;color:#333;"
    "font-family:Arial,Helvetica,sans-serif;"
)
CLASS_STYLES = {
    "txt-right": "text-align:right;color:#555;font-size:13px;margin:0 0 12px 0;",
    "dottet-line": (
        "margin:20px 0 10px 0;padding-bottom:10px;"
        "border-bottom:1px dotted #666;"
    ),
    "filelink": "margin:8px 0;",
}
TAG_STYLES = {
    "h2": (
        "font-size:16px;margin:18px 0 8px;color:#1e3a8a;"
        "border-bottom:1px solid #325F85;padding-bottom:6px;"
    ),
    "h3": (
        "font-size:16px;margin:18px 0 8px;color:#1e3a8a;"
        "border-bottom:1px solid #325F85;padding-bottom:6px;"
    ),
    "p": "margin:0 0 10px 0;",
    "a": "color:#2563eb;",
}


def _merge_style(tag, extra: str) -> None:
    existing = (tag.get("style") or "").strip()
    if existing and not existing.endswith(";"):
        existing += ";"
    tag["style"] = f"{existing}{extra}"


def restyle_detail_html(wrap) -> str:
    """Inline email-safe styles on a JFTC .en_otWrap element."""
    wrap["style"] = EMAIL_BODY_STYLE
    if "class" in wrap.attrs:
        del wrap.attrs["class"]

    for tag in wrap.find_all(True):
        for cls in list(tag.get("class") or []):
            if cls in CLASS_STYLES:
                _merge_style(tag, CLASS_STYLES[cls])
        if tag.name in TAG_STYLES:
            _merge_style(tag, TAG_STYLES[tag.name])
        if tag.name == "a" and tag.get("href"):
            tag["href"] = urljoin(BASE_URL, tag["href"])
        if tag.name == "img" and tag.get("src"):
            tag["src"] = urljoin(BASE_URL, tag["src"])
        tag.attrs.pop("class", None)

    return str(wrap)


# ---------------------------------------------------------------------------
# Fetch & parse
# ---------------------------------------------------------------------------

def fetch_listing_html(url: str = LISTING_URL, max_retries: int = 3) -> Optional[str]:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.text) > 500:
                logger.info(f"  Fetched {url} ({len(resp.text):,} chars)")
                return resp.text
            logger.warning(
                f"  Attempt {attempt}: HTTP {resp.status_code}, {len(resp.text):,} chars"
            )
        except Exception as e:
            logger.warning(f"  Attempt {attempt} error: {e}")
        if attempt < max_retries:
            time.sleep(3)
    return None


def parse_records_from_html(html: str) -> List[Dict[str, str]]:
    """Parse ul.norcor listing items into record dicts."""
    soup = BeautifulSoup(html, "html.parser")
    records: List[Dict[str, str]] = []

    for li in soup.select("ul.norcor > li"):
        try:
            a = li.find("a", href=True)
            if not a:
                continue
            href = (a.get("href") or "").strip()
            if not href:
                continue

            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not title:
                continue

            li_text = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
            date_raw = li_text[len(title):].strip() if li_text.startswith(title) else ""
            if not date_raw:
                br = a.find_next_sibling("br")
                if br and br.next_sibling:
                    date_raw = str(br.next_sibling).strip()

            records.append({
                "title": title,
                "date_raw": date_raw,
                "date": parse_jftc_date(date_raw),
                "href": href,
                "detail_url": urljoin(BASE_URL, href),
                "source": "jftc_press_releases",
            })
        except Exception as e:
            logger.warning(f"  Error parsing list item: {e}")
            continue

    return records


def fetch_detail_body_html(detail_url: str) -> str:
    """Fetch the press-release page and return restyled .en_otWrap HTML."""
    html = fetch_listing_html(detail_url)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    wrap = soup.select_one(".en_otWrap")
    if not wrap:
        logger.warning(f"  No .en_otWrap on {detail_url}")
        return ""
    return restyle_detail_html(wrap)


# ---------------------------------------------------------------------------
# Email builders
# ---------------------------------------------------------------------------

def _safe(val: Any) -> str:
    if val is None or (isinstance(val, str) and not val.strip()):
        return "N/A"
    return escape_html(str(val).strip())


def _build_case_table_html(record: Dict[str, Any]) -> str:
    rows = [
        ("Title", record.get("title")),
        ("Date", record.get("date") or record.get("date_raw")),
        ("URL", record.get("detail_url")),
    ]
    html = ""
    for i, (label, value) in enumerate(rows):
        bg = ' style="background-color:#f9f9f9;"' if i % 2 == 1 else ""
        html += (
            f"<tr{bg}>"
            f'<td style="padding:8px;font-weight:bold;width:220px;color:#555;">{label}:</td>'
            f'<td style="padding:8px;color:#333;">{_safe(value)}</td>'
            "</tr>\n"
        )
    return html


def _press_release_section(record: Dict[str, Any]) -> str:
    title = _safe(record.get("title"))
    case_table = _build_case_table_html(record)
    detail_html = record.get("detail_html") or ""
    details_block = ""
    if detail_html:
        details_block = f"""
  <h3 style="color:#333;">Press Release Details</h3>
  <div style="border:1px solid #e5e7eb;border-radius:6px;padding:16px 20px;background:#fafafa;">
    {detail_html}
  </div>
"""
    return f"""
  <h3 style="color:#333;margin-bottom:8px;">Press Release</h3>
  <div style="font-size:16px;font-weight:700;color:#111827;line-height:1.4;margin:0 0 12px 0;">
    {title}
  </div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_table}</table>
  {details_block}
"""


def build_matched_email(
    record: Dict[str, Any],
    deal_match: Dict[str, Any],
) -> Tuple[str, str]:
    subject = build_subject("jftc", "press_release", deal_match)
    deal_id = _safe(deal_match.get("deal_id"))
    target = _safe(deal_match.get("target") or deal_match.get("target_name"))
    acquirer = _safe(deal_match.get("acquirer") or deal_match.get("acquire_name"))
    body = _press_release_section(record)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #2563eb;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: JFTC Japan</p>
  <div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
    <div style="font-weight:800;color:#1e40af;margin-bottom:4px;">Matched Deal</div>
    <div style="font-size:14px;color:#1e3a8a;">
      <b>Acquirer:</b> {acquirer} &nbsp;|&nbsp;
      <b>Target:</b> {target} &nbsp;|&nbsp;
      <b>Deal ID:</b> {deal_id}
    </div>
  </div>
  {body}
  <p>
    <a href="{escape_html(record.get('detail_url', ''))}" style="color:#2563eb;" target="_blank">
      View press release on JFTC →
    </a>
  </p>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    Automated alert from JFTC Japan scraper.
  </div>
</div>
</body></html>"""
    return subject, html


def build_usa_email(record: Dict[str, Any]) -> Tuple[str, str]:
    subject = build_subject("jftc", "press_release")
    body = _press_release_section(record)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #f59e0b;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: JFTC Japan</p>
  <div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
    <div style="font-weight:800;color:#92400e;">USA-Related (Unmatched)</div>
    <div style="font-size:14px;color:#78350f;margin-top:4px;">
      This press release appears to involve USA-related companies.
    </div>
  </div>
  {body}
  <p>
    <a href="{escape_html(record.get('detail_url', ''))}" style="color:#2563eb;" target="_blank">
      View press release on JFTC →
    </a>
  </p>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    Automated alert from JFTC Japan scraper.
  </div>
</div>
</body></html>"""
    return subject, html


def _send_email(
    subject: str,
    html: str,
    extras: Dict[str, Any],
    test_mode: bool = False,
) -> bool:
    """
    production  → org-aware post_email_payload
    test_mode   → send_direct_email to TEST_RECIPIENT via N8N_WEBHOOK_ONLY_ME
    """
    payload = {"subject": subject, "html": html, **extras}
    if test_mode:
        webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
        if not webhook_url:
            logger.warning(
                "N8N_WEBHOOK_ONLY_ME not set in .env — test email skipped")
            return False
        logger.info(
            "[TEST] Sending to %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
        return send_direct_email(
            [TEST_RECIPIENT], payload, webhook_url=webhook_url)
    return post_email_payload(payload, subject=subject)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(test_mode: bool = False):
    refresh_script_log(logger, get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats: Dict[str, int] = {
        "total_seen": 0,
        "skipped_existing": 0,
        "skipped_non_deal": 0,
        "inserted": 0,
        "llm_matched": 0,
        "regex_matched": 0,
        "usa_related": 0,
        "emails_sent": 0,
    }

    logger.info("=" * 60)
    logger.info("JFTC JAPAN — MERGER PRESS RELEASES SCRAPER")
    if test_mode:
        logger.info(
            "TEST-EMAIL: emails → %s via send_direct_email", TEST_RECIPIENT)
    logger.info("=" * 60)

    try:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(
                error_items, f"MongoDB init failed: {message}", step="init_mongodb")
            return

        collection = get_japan_cases_collection()
        if collection is None:
            collect_error(
                error_items, "japan_cases collection unavailable", step="get_collection")
            return

        deals = fetch_open_deals()
        deal_by_id = {str(d.get("deal_id", "")): d for d in deals if d.get("deal_id")}
        logger.info(f"Loaded {len(deals)} deals for matching")

        html = fetch_listing_html()
        if not html:
            collect_error(
                error_items,
                "Failed to fetch JFTC listing page",
                step="fetch_page",
                context={"url": LISTING_URL},
            )
            return

        records = parse_records_from_html(html)
        logger.info(f"Parsed {len(records)} press releases")
        if not records:
            collect_error(
                error_items,
                "No press releases parsed from listing HTML",
                step="parse_records",
            )
            return

        for record in records:
            stats["total_seen"] += 1
            title = record["title"]
            detail_url = record["detail_url"]

            if is_non_deal_title(title):
                logger.info(f"  Non-deal title — skip: {title[:80]}")
                stats["skipped_non_deal"] += 1
                continue

            if detail_url_exists(collection, detail_url):
                logger.info(f"  Already in DB, skip: {title[:80]}")
                stats["skipped_existing"] += 1
                continue

            logger.info(f"  New — processing: {title[:80]}")

            try:
                record["detail_html"] = fetch_detail_body_html(detail_url)
                if record["detail_html"]:
                    logger.info("  Fetched .en_otWrap detail HTML")
            except Exception as e:
                logger.warning(f"  Detail fetch failed: {e}")
                record["detail_html"] = ""
                collect_error(
                    error_items, str(e), step="fetch_detail",
                    context={"title": title, "detail_url": detail_url},
                )

            deal_id: Optional[str] = None
            matched_by_regex = False
            try:
                deal_id = llm_match_deal_id(
                    regulator_name="JFTC Japan",
                    case_sections={"PRESS RELEASE TITLE": title},
                    source_label="the JFTC press release title",
                    deals=deals,
                )
            except Exception as e:
                logger.exception(f"  LLM match error: {e}")
                collect_error(
                    error_items, str(e), step="llm_match",
                    context={"title": title, "detail_url": detail_url},
                )

            if deal_id:
                stats["llm_matched"] += 1
                logger.info(f"  LLM matched deal_id={deal_id}")
            else:
                deal_id = regex_match_jftc_deal(title, deals)
                if deal_id:
                    matched_by_regex = True
                    stats["regex_matched"] += 1
                    logger.info(f"  Regex matched deal_id={deal_id}")
                else:
                    logger.info("  No deal match (LLM + regex)")

            if deal_id:
                record["deal_id"] = deal_id

            try:
                if deal_id:
                    deal_match = deal_by_id.get(str(deal_id)) or get_deal_by_id(deal_id) or {}
                    subject, html_body = build_matched_email(record, deal_match)
                    subject = apply_regex_match_subject(subject, matched_by_regex)
                    ok = _send_email(subject, html_body, {
                        "deal_id": deal_id,
                        "case_title": title,
                        "case_url": detail_url,
                        "source": "jftc_cases_register",
                        "is_new_case": True,
                    }, test_mode=test_mode)
                    if ok:
                        stats["emails_sent"] += 1
                        logger.info(f"  Email sent ({subject[:60]})")
                    else:
                        collect_error(
                            error_items, "Failed to send matched email",
                            step="send_email",
                            context={"detail_url": detail_url},
                        )
                else:
                    usa_details = {
                        "title": title,
                        "date": record.get("date"),
                        "detail_url": detail_url,
                    }
                    try:
                        usa = bool(verify_usa_relation(usa_details, case_type="JFTC"))
                    except Exception as e:
                        logger.warning(f"  USA check error: {e}")
                        usa = False

                    if usa:
                        stats["usa_related"] += 1
                        subject, html_body = build_usa_email(record)
                        ok = _send_email(subject, html_body, {
                            "deal_id": "N/A",
                            "case_title": title,
                            "case_url": detail_url,
                            "source": "jftc_cases_register",
                            "is_unmatched": True,
                            "is_new_case": True,
                        }, test_mode=test_mode)
                        if ok:
                            stats["emails_sent"] += 1
                            logger.info(f"  USA email sent ({subject[:60]})")
                        else:
                            collect_error(
                                error_items, "Failed to send USA email",
                                step="send_email",
                                context={"detail_url": detail_url},
                            )
                    else:
                        logger.info("  Not USA-related — save only")
            except Exception as e:
                logger.exception(f"  Email pipeline error: {e}")
                collect_error(
                    error_items, str(e), step="send_email",
                    context={"detail_url": detail_url},
                )

            try:
                ok = insert_japan_case(collection, record)
                if ok:
                    stats["inserted"] += 1
                    logger.info(f"  Inserted into japan_cases: {detail_url}")
                else:
                    collect_error(
                        error_items, "DB insert returned False",
                        step="insert", context={"detail_url": detail_url},
                    )
            except Exception as e:
                logger.exception(f"  Insert error: {e}")
                collect_error(
                    error_items, str(e), step="insert",
                    context={"detail_url": detail_url},
                )

            time.sleep(1)

    except Exception as e:
        logger.exception(f"Unhandled error in run(): {e}")
        collect_error(error_items, f"Unhandled error: {e}", step="run_main")

    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Total records seen      : {stats['total_seen']}")
        logger.info(f"  Skipped (in DB)         : {stats['skipped_existing']}")
        logger.info(f"  Skipped (non-deal)      : {stats['skipped_non_deal']}")
        logger.info(f"  Inserted                : {stats['inserted']}")
        logger.info(f"  LLM deal matches        : {stats['llm_matched']}")
        logger.info(f"  Regex deal matches      : {stats['regex_matched']}")
        logger.info(f"  USA-related (unmatched) : {stats['usa_related']}")
        logger.info(f"  Emails sent             : {stats['emails_sent']}")
        logger.info(f"  Errors                  : {len(error_items)}")
        logger.info(f"  Total time              : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="JFTC Japan merger press releases → japan_cases"
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help=(
            f"Backfill/run with emails to {TEST_RECIPIENT} via "
            "send_direct_email (N8N_WEBHOOK_ONLY_ME)"
        ),
    )
    args = parser.parse_args()
    run(test_mode=args.test_email)
