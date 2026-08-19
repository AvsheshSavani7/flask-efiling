"""
South Africa CompCom Press Release Monitor
==========================================
Loads https://www.compcom.co.za/2026-media-releases/, filters "Statement on
the latest decisions by the Competition Commission" PDFs, extracts merger
decisions via OpenAI, and matches them to sa_compcom_cases with status
"Pending" or "removed from pending list".

Collections:
  sa_compcom_cases          — update matched cases (title/description/
                              decision_status, status=completed)
  sa_compcom_press_releases — PDF URL ledger (skip already-processed PDFs)

Does NOT import sa_compcom_cases_register (keeps logging fully separate).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from html import escape as escape_html
from io import BytesIO
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bson import ObjectId
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from deal_match_llm import fetch_open_deals, llm_match_deal_id
from deal_match_regex import apply_regex_match_subject, regex_match_sa_compcom_deal
from email_subject_builder import build_subject
from llm_verification_service import verify_usa_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import (
    get_database,
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from n8n_email_service import post_email_payload, send_direct_email
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

MEDIA_RELEASES_URL = "https://www.compcom.co.za/2026-media-releases/"
ENV_PATH = ".env"
CASES_COLLECTION = "sa_compcom_cases"
PRESS_COLLECTION = "sa_compcom_press_releases"
REMOVED_STATUS = "removed from pending list"
COMPLETED_STATUS = "completed"
STATEMENT_TITLE_PREFIX = "statement on the latest decisions by the competition commission"
TEST_RECIPIENT = "avshesh.savani@teqnodux.com"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")

PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "sa_compcom_press_release_monitor"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


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

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def get_cases_collection():
    db = get_database()
    return None if db is None else db[CASES_COLLECTION]


def get_press_collection():
    db = get_database()
    return None if db is None else db[PRESS_COLLECTION]


def ensure_indexes(press_col) -> None:
    try:
        press_col.create_index("pdf_url", unique=True)
    except Exception as e:
        logger.warning("Could not create unique index on pdf_url: %s", e)


def fetch_media_page_html(url: str = MEDIA_RELEASES_URL, headless: bool = True) -> Optional[str]:
    try:
        logger.info("Fetching media releases page via Playwright: %s", url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                html = page.content()
                logger.info(
                    "Fetched HTML via Playwright (%s bytes)", len(html))
                return html
            finally:
                browser.close()
    except Exception as e:
        logger.warning(
            "Playwright fetch failed (%s); falling back to requests", e)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=60,
        )
        resp.raise_for_status()
        logger.info("Fetched HTML via requests (%s bytes)", len(resp.text))
        return resp.text
    except requests.RequestException as e:
        logger.error("Error fetching media releases page: %s", e)
        return None


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _is_statement_title(title: str) -> bool:
    return _normalize_title(title).lower().startswith(STATEMENT_TITLE_PREFIX)


def _parse_release_date(raw: str) -> str:
    cleaned = _normalize_title(raw)
    cleaned = re.sub(r"^Date:\s*", "", cleaned, flags=re.IGNORECASE)
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return cleaned


def parse_media_release_items(html_content: str) -> List[Dict[str, Any]]:
    """Parse title / date / pdf_url triples from the media releases page."""
    soup = BeautifulSoup(html_content, "html.parser")
    content = soup.select_one(".financity-content-area") or soup
    items: List[Dict[str, Any]] = []

    paragraphs = content.find_all("p")
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        strong = p.find("strong")
        title = _normalize_title(strong.get_text(
            " ", strip=True) if strong else "")
        if not title:
            i += 1
            continue

        date_raw = ""
        pdf_url = ""
        if i + 1 < len(paragraphs):
            date_raw = _normalize_title(
                paragraphs[i + 1].get_text(" ", strip=True))
        if i + 2 < len(paragraphs):
            link = paragraphs[i + 2].find("a", href=True)
            if link:
                href = (link.get("href") or "").strip()
                if href.lower().endswith(".pdf"):
                    pdf_url = urljoin(MEDIA_RELEASES_URL, href)

        # Nested date/link blocks (e.g. Takata entry)
        if not pdf_url:
            for nxt in paragraphs[i + 1: i + 4]:
                if not date_raw and nxt.get_text(" ", strip=True).lower().startswith("date:"):
                    date_raw = _normalize_title(nxt.get_text(" ", strip=True))
                link = nxt.find("a", href=True)
                if link and (link.get("href") or "").lower().endswith(".pdf"):
                    pdf_url = urljoin(MEDIA_RELEASES_URL,
                                      (link.get("href") or "").strip())
                    break

        if title and pdf_url:
            items.append(
                {
                    "title": title,
                    "date": _parse_release_date(date_raw),
                    "pdf_url": pdf_url,
                }
            )
            i += 3
            continue
        i += 1

    # Dedup by pdf_url preserving order
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in items:
        url = item["pdf_url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(item)

    logger.info("Parsed %s media release items", len(unique))
    return unique


def filter_statement_releases(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = [it for it in items if _is_statement_title(it.get("title", ""))]
    logger.info(
        "Filtered to %s 'Statement on the latest decisions...' releases",
        len(filtered),
    )
    return filtered


def pdf_url_already_processed(press_col, pdf_url: str) -> bool:
    try:
        return press_col.count_documents({"pdf_url": pdf_url}, limit=1) > 0
    except Exception as e:
        logger.exception("Error checking press release dedup: %s", e)
        return False


def download_pdf_bytes(url: str) -> Optional[bytes]:
    try:
        logger.info("Downloading PDF: %s", url)
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=120)
        resp.raise_for_status()
        logger.info("Downloaded PDF (%s bytes)", len(resp.content))
        return resp.content
    except requests.RequestException as e:
        logger.error("Failed to download PDF %s: %s", url, e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader  # type: ignore

    reader = PdfReader(BytesIO(pdf_bytes))
    parts: List[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    logger.info("Extracted %s chars of PDF text", len(text))
    return text


def extract_cases_from_pdf_text(pdf_text: str) -> Optional[List[Dict[str, str]]]:
    """
    Ask OpenAI for merger/acquisition decisions as JSON:
      [{title, description, decision_status}, ...]

    Returns:
      list  — extraction succeeded (may be empty if no M&A items)
      None  — extraction failed (caller must NOT mark PDF as processed)
    """
    if not pdf_text.strip():
        return []

    prompt = f"""You are extracting merger and acquisition decisions from a South Africa Competition Commission media statement.

From the PDF text below, extract EVERY numbered merger/acquisition item under "MERGERS AND ACQUISITIONS" (e.g. 1.1, 1.2, 1.3…). Ignore cartels, complaints, advisories, and the closing/contact section.

For each item return:
- title: Copy the case heading text EXACTLY as written in the document (the line under 1.1 / 1.2 / etc.). Include both parties and any "in respect of …" wording. Do NOT shorten, rewrite, or invent text. You may omit only the leading number like "1.1".
- description: Copy the FULL body text under that heading EXACTLY as written, until the next numbered item (or [ENDS]). Do NOT summarize, paraphrase, or omit paragraphs.
- decision_status: The ONLY field you analyze. Infer from the description one of:
    "approved"
    "approved_with_conditions"
    "rejected"
    "recommended_for_approval"
    "recommended_for_approval_with_conditions"
    "prohibited"
    "other"

Return ONLY valid JSON in this exact shape:
{{"cases":[{{"title":"...","description":"...","decision_status":"..."}}]}}

PDF TEXT:
{pdf_text}
"""
    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract merger decisions from competition authority "
                        "press releases. Copy title and description verbatim from "
                        "the source text. Only decision_status may be inferred. "
                        "Respond with JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        cases = data.get("cases") if isinstance(data, dict) else data
        if not isinstance(cases, list):
            logger.warning(
                "Unexpected JSON shape from PDF extraction: %s", type(cases)
            )
            return None

        cleaned: List[Dict[str, str]] = []
        for item in cases:
            if not isinstance(item, dict):
                continue
            title = _normalize_title(str(item.get("title") or ""))
            description = str(item.get("description") or "").strip()
            decision_status = _normalize_title(
                str(item.get("decision_status") or "other")
            ).lower().replace(" ", "_")
            if not title:
                continue
            cleaned.append(
                {
                    "title": title,
                    "description": description,
                    "decision_status": decision_status or "other",
                }
            )
        logger.info(
            "OpenAI extracted %s merger decisions from PDF", len(cleaned)
        )
        for i, case in enumerate(cleaned, 1):
            logger.info(
                "  Extracted case %s: %s | decision_status=%s",
                i,
                (case.get("title") or "")[:120],
                case.get("decision_status"),
            )
        return cleaned
    except Exception as e:
        logger.exception("PDF case extraction failed: %s", e)
        return None


def fetch_removed_pending_cases(cases_col) -> List[Dict[str, Any]]:
    cursor = cases_col.find(
        {
            "status": {
                "$in": ["removed from pending list", "Pending"]
            }
        }
    )
    rows = list(cursor)
    logger.info(
        "Loaded %s sa_compcom_cases with status in "
        "['Pending', 'removed from pending list']",
        len(rows),
    )
    return rows


def _candidate_summary(cases: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for doc in cases:
        out.append(
            {
                "id": str(doc.get("_id")),
                "case_number": str(doc.get("case_number") or ""),
                "primary_acquiring_firm": str(
                    doc.get("primary_acquiring_firm") or ""
                ),
                "primary_target_firm": str(doc.get("primary_target_firm") or ""),
                "status": str(doc.get("status") or ""),
            }
        )
    return out


def match_case_title_to_record(
    press_title: str,
    candidates: List[Dict[str, Any]],
) -> Optional[str]:
    """
    Ask LLM whether the press-release case title matches any candidate
    sa_compcom_cases row (Pending or removed from pending list).

    Returns Mongo _id only if BOTH primary_acquiring_firm and
    primary_target_firm of that row match the case title; else None.
    """
    if not press_title or not candidates:
        return None

    summary = _candidate_summary(candidates)
    prompt = f"""You match a Competition Commission press-release case title to an existing weekly case-list record.

Press-release case title:
{press_title}

Candidate DB records (JSON) — status is "Pending" or "removed from pending list":
{json.dumps(summary, ensure_ascii=False, indent=2)}

Rules (strict):
- Return a matched_id ONLY if BOTH the primary_acquiring_firm AND the
  primary_target_firm from that DB record clearly correspond to the parties
  in the press-release case title (allow trivial differences like
  Pty/Proprietary/Limited, punctuation, or short aliases in quotes).
- Matching one side only (acquirer OR target) is NOT enough → return null.
- If no record is a clear both-parties match, return null.
- If more than one candidate fits equally, or you are unsure, return null.
- Do NOT invent an id. Only use an id from the candidate list.

Return ONLY JSON:
{{"matched_id": "<mongo id string or null>"}}
"""
    try:
        logger.info(
            "Asking LLM to match title against %s candidate cases",
            len(summary),
        )
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict M&A case matcher. A match requires BOTH "
                        "acquirer AND target from the DB record to match the case "
                        "title. JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        matched = data.get("matched_id")
        if matched in (None, "", "null", "None"):
            logger.info("LLM: no case match for title: %s", press_title[:80])
            return None
        matched_str = str(matched).strip()
        valid_ids = {str(c.get("_id")) for c in candidates}
        if matched_str not in valid_ids:
            logger.warning(
                "LLM returned id not in candidate set: %s", matched_str
            )
            return None
        logger.info("LLM matched title → case id=%s", matched_str)
        return matched_str
    except Exception as e:
        logger.exception("Title→case match failed: %s", e)
        return None


def match_deal_for_case(
    case_doc: Dict[str, Any],
    press_title: str,
    description: str,
    deals: List[Dict[str, Any]],
) -> tuple[Optional[str], bool]:
    """LLM → regex. Returns (deal_id, matched_by_regex)."""
    acquiring = (case_doc.get("primary_acquiring_firm") or "").strip()
    target = (case_doc.get("primary_target_firm") or "").strip()
    case_number = (case_doc.get("case_number") or "").strip()

    matched = llm_match_deal_id(
        regulator_name="South Africa Competition Commission",
        case_sections={
            "CASE NUMBER": case_number,
            "PRESS TITLE": press_title,
            "PRIMARY ACQUIRING FIRM": acquiring,
            "PRIMARY TARGET FIRM": target,
            "DESCRIPTION": description,
        },
        source_label="the South Africa CompCom press-release case text",
        deals=deals,
    )
    if matched:
        return matched, False

    matched = regex_match_sa_compcom_deal(acquiring, target, deals)
    if matched:
        return matched, True
    return None, False


def generate_matched_email_html(
    case_info: Dict[str, Any],
    deal: Dict[str, Any],
    press_url: str,
) -> str:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = str(deal.get("deal_id") or "N/A")
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>South Africa CompCom - Press Release</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;">
    <span style="font-weight:700;">Acquirer:</span> {escape_html(str(acquirer))}
    <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Target:</span> {escape_html(str(target))}
    <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Deal ID:</span> {escape_html(deal_id)}
  </div>
</div>
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">South Africa CompCom – Press Release Decision</div>
  <div style="display:grid;grid-template-columns:220px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">Case Number:</div><div>{escape_html(case_info.get("case_number", "N/A"))}</div>
    <div style="font-weight:700;">Title:</div><div>{escape_html(case_info.get("title", "N/A"))}</div>
    <div style="font-weight:700;">Decision Status:</div><div>{escape_html(case_info.get("decision_status", "N/A"))}</div>
    <div style="font-weight:700;">Status:</div><div>{escape_html(case_info.get("status", "N/A"))}</div>
    <div style="font-weight:700;">Acquiring Firm:</div><div>{escape_html(case_info.get("primary_acquiring_firm", "N/A"))}</div>
    <div style="font-weight:700;">Target Firm:</div><div>{escape_html(case_info.get("primary_target_firm", "N/A"))}</div>
    <div style="font-weight:700;">Description:</div><div>{escape_html(case_info.get("description", "N/A"))}</div>
  </div>
  <div style="margin-top:16px;">
    <a href="{escape_html(press_url)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">View Press Release PDF →</a>
  </div>
</div>
</div>
</body>
</html>"""


def generate_usa_email_html(case_info: Dict[str, Any], press_url: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>USA-Related South Africa CompCom Press Release</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">USA-Related South Africa CompCom Decision</div>
  <div style="font-size:14px;color:#1e3a8a;">This press-release decision appears to involve USA-related parties or markets.</div>
</div>
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">Case Details</div>
  <div style="display:grid;grid-template-columns:220px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">Case Number:</div><div>{escape_html(case_info.get("case_number", "N/A"))}</div>
    <div style="font-weight:700;">Title:</div><div>{escape_html(case_info.get("title", "N/A"))}</div>
    <div style="font-weight:700;">Decision Status:</div><div>{escape_html(case_info.get("decision_status", "N/A"))}</div>
    <div style="font-weight:700;">Description:</div><div>{escape_html(case_info.get("description", "N/A"))}</div>
  </div>
  <div style="margin-top:16px;">
    <a href="{escape_html(press_url)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;">View Press Release PDF →</a>
  </div>
</div>
</div>
</body>
</html>"""


def send_email(
    subject: str,
    html_content: str,
    case_info: Dict[str, Any],
    press_url: str,
    deal_id: Optional[str] = None,
    test_mode: bool = False,
) -> bool:
    payload = {
        "subject": subject,
        "html": html_content,
        "case_number": case_info.get("case_number", "N/A"),
        "title": case_info.get("title", "N/A"),
        "description": case_info.get("description", ""),
        "decision_status": case_info.get("decision_status", ""),
        "status": case_info.get("status", ""),
        "primary_acquiring_firm": case_info.get("primary_acquiring_firm", "N/A"),
        "primary_target_firm": case_info.get("primary_target_firm", "N/A"),
        "press_release_url": press_url,
        "deal_id": deal_id,
        "is_new_case": False,
        "source": SCRIPT_NAME,
    }
    try:
        if test_mode:
            webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
            if not webhook_url:
                logger.warning(
                    "N8N_WEBHOOK_ONLY_ME not set — test email skipped"
                )
                return False
            logger.info(
                "[TEST] Sending to %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT
            )
            return send_direct_email(
                [TEST_RECIPIENT], payload, webhook_url=webhook_url
            )
        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.warning("Error sending email: %s", e)
        return False


def update_case_as_completed(
    cases_col,
    case_id: str,
    *,
    title: str,
    description: str,
    decision_status: str,
    press_url: str,
    press_date: str,
    deal_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    now_iso = utc_now_iso()
    update_fields: Dict[str, Any] = {
        "title": title,
        "description": description,
        "decision_status": decision_status,
        "status": COMPLETED_STATUS,
        "press_release_url": press_url,
        "press_release_date": press_date,
        "updated_at": now_iso,
        "completed_at": now_iso,
    }
    if deal_id:
        update_fields["deal_id"] = deal_id

    try:
        cases_col.update_one(
            {"_id": ObjectId(case_id)},
            {"$set": update_fields},
        )
        doc = cases_col.find_one({"_id": ObjectId(case_id)})
        return doc
    except Exception as e:
        logger.exception("Failed to update case %s: %s", case_id, e)
        return None


def insert_press_release_record(
    press_col,
    item: Dict[str, Any],
    *,
    cases_extracted: int,
    cases_matched: int,
) -> None:
    now_iso = utc_now_iso()
    doc = {
        "pdf_url": item["pdf_url"],
        "title": item.get("title", ""),
        "date": item.get("date", ""),
        "list_page_url": MEDIA_RELEASES_URL,
        "cases_extracted": cases_extracted,
        "cases_matched": cases_matched,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        press_col.insert_one(doc)
        logger.info("Recorded processed press release: %s", item["pdf_url"])
    except Exception as e:
        logger.warning("Failed to insert press release ledger row: %s", e)


def process_extracted_case(
    extracted: Dict[str, str],
    *,
    candidates: List[Dict[str, Any]],
    cases_col,
    open_deals: List[Dict[str, Any]],
    press_url: str,
    press_date: str,
    test_mode: bool,
    error_items: List[Dict[str, Any]],
) -> bool:
    """
    Match one extracted press-release case to a removed-from-pending DB row.
    Returns True if a DB case was matched and updated.
    """
    press_title = extracted["title"]
    description = extracted.get("description", "")
    decision_status = extracted.get("decision_status", "other")

    matched_id = match_case_title_to_record(press_title, candidates)
    if not matched_id:
        logger.info("No 100%% case match for title: %s", press_title[:80])
        return False

    case_doc = next(
        (c for c in candidates if str(c.get("_id")) == matched_id), None
    )
    if case_doc is None:
        return False

    logger.info(
        "Matched press title → case_number=%s id=%s",
        case_doc.get("case_number"),
        matched_id,
    )

    existing_deal_id = (case_doc.get("deal_id") or "").strip() or None
    matched_by_regex = False
    deal_id = existing_deal_id

    if not deal_id:
        try:
            deal_id, matched_by_regex = match_deal_for_case(
                case_doc, press_title, description, open_deals
            )
        except Exception as e:
            logger.exception("Deal match error: %s", e)
            collect_error(
                error_items,
                str(e),
                step="match_deal_for_case",
                context={"case_number": case_doc.get("case_number")},
            )
            deal_id = None

    updated = update_case_as_completed(
        cases_col,
        matched_id,
        title=press_title,
        description=description,
        decision_status=decision_status,
        press_url=press_url,
        press_date=press_date,
        deal_id=deal_id,
    )
    if not updated:
        collect_error(
            error_items,
            "Failed to update matched case",
            step="update_case_as_completed",
            context={"case_id": matched_id},
        )
        return False

    # Drop from in-memory candidate pool so it cannot rematch in this run
    candidates[:] = [c for c in candidates if str(c.get("_id")) != matched_id]

    if deal_id:
        deal = get_deal_by_id(deal_id)
        if deal:
            subject = build_subject("sa_compcom", "press_release", deal)
            subject = apply_regex_match_subject(subject, matched_by_regex)
            html = generate_matched_email_html(updated, deal, press_url)
            if not send_email(
                subject,
                html,
                updated,
                press_url,
                deal_id=deal_id,
                test_mode=test_mode,
            ):
                collect_error(
                    error_items,
                    "Failed to send matched press-release email",
                    step="send_email",
                    context={
                        "case_number": updated.get("case_number"),
                        "deal_id": deal_id,
                    },
                )
        else:
            collect_error(
                error_items,
                "deal_id set but deal document not found",
                step="fetch_matched_deal",
                context={"deal_id": deal_id, "case_id": matched_id},
            )
        return True

    # No deal_id — USA check
    try:
        details = (
            f"Case Number: {updated.get('case_number', '')}\n"
            f"Title: {press_title}\n"
            f"Acquiring: {updated.get('primary_acquiring_firm', '')}\n"
            f"Target: {updated.get('primary_target_firm', '')}\n"
            f"Decision: {decision_status}\n"
            f"Description: {description}"
        )
        is_usa = bool(
            verify_usa_relation(
                company_details=details,
                case_type="South Africa CompCom",
            )
        )
    except Exception as e:
        logger.exception("USA verification error: %s", e)
        collect_error(
            error_items,
            str(e),
            step="verify_usa_relation",
            context={"case_number": updated.get("case_number")},
        )
        is_usa = False

    if is_usa:
        subject = build_subject("sa_compcom", "press_release")
        html = generate_usa_email_html(updated, press_url)
        if not send_email(
            subject, html, updated, press_url, test_mode=test_mode
        ):
            collect_error(
                error_items,
                "Failed to send USA-related press-release email",
                step="send_email",
                context={"case_number": updated.get("case_number")},
            )
    else:
        logger.info(
            "Case %s completed with no deal / USA match; silent save",
            updated.get("case_number"),
        )
    return True


def run_sa_compcom_press_release_monitor(
    headless: bool = True,
    test_mode: bool = False,
    limit: Optional[int] = None,
) -> None:
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = datetime.now()
    error_items: List[Dict[str, Any]] = []

    pdfs_seen = 0
    pdfs_skipped = 0
    pdfs_processed = 0
    cases_extracted_total = 0
    cases_matched_total = 0

    mode_label = (
        f"TEST-EMAIL → {TEST_RECIPIENT}" if test_mode else "LIVE"
    )
    logger.info("=" * 60)
    logger.info("Starting SA CompCom Press Release Monitor — %s", mode_label)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("=" * 60)

    try:
        success, message = init_mongodb_connection(ENV_PATH)
        if not success:
            collect_error(
                error_items,
                f"MongoDB connection failed: {message}",
                step="mongodb_connect",
            )
            return
        logger.info("MongoDB: %s", message)
        if not is_connected():
            collect_error(
                error_items, "MongoDB not connected", step="mongodb_connect"
            )
            return

        cases_col = get_cases_collection()
        press_col = get_press_collection()
        if cases_col is None or press_col is None:
            collect_error(
                error_items,
                "Could not access Mongo collections",
                step="get_collection",
            )
            return
        ensure_indexes(press_col)

        html = fetch_media_page_html(MEDIA_RELEASES_URL, headless=headless)
        if not html:
            collect_error(
                error_items,
                "Failed to fetch media releases HTML",
                step="fetch_media_page_html",
                context={"url": MEDIA_RELEASES_URL},
            )
            return

        statements = filter_statement_releases(parse_media_release_items(html))
        if not statements:
            logger.warning("No statement press releases found. Exiting.")
            return

        if limit is not None and limit > 0:
            statements = statements[:limit]
            logger.info("Limiting run to first %s statement PDF(s)", limit)

        candidates = fetch_removed_pending_cases(cases_col)
        open_deals = fetch_open_deals()

        for idx, item in enumerate(statements, 1):
            pdf_url = item["pdf_url"]
            pdfs_seen += 1
            logger.info(
                "[%s/%s] %s | %s",
                idx,
                len(statements),
                item.get("date"),
                pdf_url,
            )

            if pdf_url_already_processed(press_col, pdf_url):
                pdfs_skipped += 1
                logger.info("PDF already in %s; skipping", PRESS_COLLECTION)
                continue

            pdf_bytes = download_pdf_bytes(pdf_url)
            if not pdf_bytes:
                collect_error(
                    error_items,
                    "PDF download failed",
                    step="download_pdf",
                    context={"pdf_url": pdf_url},
                )
                continue

            try:
                pdf_text = extract_pdf_text(pdf_bytes)
            except Exception as e:
                logger.exception("PDF text extract failed: %s", e)
                collect_error(
                    error_items,
                    str(e),
                    step="extract_pdf_text",
                    context={"pdf_url": pdf_url},
                )
                continue

            extracted_cases = extract_cases_from_pdf_text(pdf_text)
            if extracted_cases is None:
                collect_error(
                    error_items,
                    "PDF case extraction failed — PDF not marked processed",
                    step="extract_cases_from_pdf_text",
                    context={"pdf_url": pdf_url},
                )
                continue

            cases_extracted_total += len(extracted_cases)
            matched_in_pdf = 0

            for extracted in extracted_cases:
                try:
                    ok = process_extracted_case(
                        extracted,
                        candidates=candidates,
                        cases_col=cases_col,
                        open_deals=open_deals,
                        press_url=pdf_url,
                        press_date=item.get("date", ""),
                        test_mode=test_mode,
                        error_items=error_items,
                    )
                    if ok:
                        matched_in_pdf += 1
                        cases_matched_total += 1
                except Exception as e:
                    logger.exception("Error processing extracted case: %s", e)
                    collect_error(
                        error_items,
                        str(e),
                        step="process_extracted_case",
                        context={
                            "pdf_url": pdf_url,
                            "title": extracted.get("title", ""),
                        },
                    )

            # Only mark processed after a successful extract (list may be empty).
            insert_press_release_record(
                press_col,
                item,
                cases_extracted=len(extracted_cases),
                cases_matched=matched_in_pdf,
            )
            pdfs_processed += 1

    except Exception as e:
        logger.exception("Unhandled error in press release monitor: %s", e)
        collect_error(
            error_items, f"Unhandled error: {e}", step="run_main"
        )
    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round((datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  Mode                         : %s", mode_label)
        logger.info("  Statement PDFs found         : %s", pdfs_seen)
        logger.info("  PDFs skipped (already seen)  : %s", pdfs_skipped)
        logger.info("  PDFs processed               : %s", pdfs_processed)
        logger.info("  Cases extracted              : %s",
                    cases_extracted_total)
        logger.info("  Cases matched & completed    : %s", cases_matched_total)
        logger.info("  Errors                       : %s", len(error_items))
        logger.info("  Total time                   : %ss", elapsed)
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="South Africa CompCom press release monitor"
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help=f"Send emails to {TEST_RECIPIENT} via N8N_WEBHOOK_ONLY_ME",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N statement PDFs (newest first)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Playwright with a visible browser",
    )
    args = parser.parse_args()
    run_sa_compcom_press_release_monitor(
        headless=not args.headed,
        test_mode=args.test_email,
        limit=args.limit,
    )
