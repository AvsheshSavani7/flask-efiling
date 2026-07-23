"""
COMESA Case Registry → comesa_cases collection
==============================================
Scrapes the COMESA M&A case registry table and inserts new cases into MongoDB.

Normal run: for each new detail_url, run deal match → regex → USA check → email.
Bootstrap run (--bootstrap): insert every row with no LLM, regex, or email.
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
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from deal_match_llm import fetch_open_deals, llm_match_deal_id
from deal_match_regex import apply_regex_match_subject, regex_match_comesa_deal
from email_subject_builder import build_subject
from llm_verification_service import verify_usa_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import (
    get_database,
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from n8n_email_service import post_email_payload
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

REGISTRY_URL = (
    "https://comesacompetition.org/mergers-and-acquisitions/case-registry/"
)
BACKUP_JSON = "comesa_cases_register_backup.json"
ENV_PATH = ".env"

PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "comesa_cases_register"
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


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def get_comesa_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["comesa_cases"]


def fetch_registry_html(url: str = REGISTRY_URL) -> Optional[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        logger.info(f"Fetching COMESA case registry: {url}")
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        logger.info(f"Fetched HTML ({len(resp.text)} bytes)")
        return resp.text
    except requests.RequestException as e:
        logger.error(f"Error fetching registry page: {e}")
        return None


def _slug_from_classes(classes: List[str], prefix: str) -> str:
    for cls in classes:
        if cls.startswith(prefix):
            return cls[len(prefix):]
    return ""


def _cell_filter_value(td) -> str:
    if td is None:
        return ""
    value = (td.get("data-filter") or "").strip()
    if value:
        return value
    return td.get_text(" ", strip=True)


def parse_notice_date(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw.strip())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return raw.strip()


def parse_comesa_table(html_content: str) -> List[Dict[str, Any]]:
    """Parse posts-data-table rows from the COMESA case registry page."""
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", class_="posts-data-table")
    if table is None:
        logger.warning("Could not locate posts-data-table on registry page")
        return []

    rows: List[Dict[str, Any]] = []
    tbody = table.find("tbody")
    if not tbody:
        return rows

    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            continue

        classes = tr.get("class") or []
        tax_case_outcome = _slug_from_classes(classes, "case-outcome-")
        hf_tax_case_status = _slug_from_classes(classes, "case-status-")

        outcome_cell = cells[3]
        outcome_slug_el = outcome_cell.select_one("[data-slug]")
        if outcome_slug_el and outcome_slug_el.get("data-slug"):
            tax_case_outcome = outcome_slug_el["data-slug"].strip()
        elif not tax_case_outcome:
            tax_case_outcome = _cell_filter_value(outcome_cell)

        if len(cells) > 6:
            status_text = cells[6].get_text(strip=True)
            if status_text:
                hf_tax_case_status = status_text

        detail_url = ""
        view_link = cells[5].find("a", href=True) if len(cells) > 5 else None
        if view_link:
            detail_url = urljoin(REGISTRY_URL, view_link["href"].strip())

        sector = _cell_filter_value(cells[0])
        if sector.lower().startswith("array"):
            sector = cells[0].get_text(" ", strip=True)

        reference_number = _cell_filter_value(cells[1])
        if not reference_number:
            reference_number = cells[1].get_text(" ", strip=True)

        case_parties = _cell_filter_value(cells[2])
        if not case_parties:
            case_parties = cells[2].get_text(" ", strip=True)

        notice_date = parse_notice_date(_cell_filter_value(cells[4]))

        if not detail_url:
            logger.warning(
                "Skipping row without detail_url (ref=%s)",
                reference_number or "?",
            )
            continue

        rows.append(
            {
                "sector": sector,
                "reference_number": reference_number,
                "case_parties": case_parties,
                "tax_case_outcome": tax_case_outcome,
                "hf_tax_case_status": hf_tax_case_status,
                "notice_date": notice_date,
                "detail_url": detail_url,
            }
        )

    logger.info(f"Parsed {len(rows)} case rows from registry table")
    return rows


def case_exists(collection, detail_url: str) -> bool:
    try:
        return (
            collection.count_documents({"detail_url": detail_url}, limit=1) > 0
        )
    except Exception as e:
        logger.exception(f"Error checking existing case: {e}")
        return False


def match_case_to_deal(
    case_parties: str,
    sector: str,
    reference_number: str,
    deals: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    return llm_match_deal_id(
        regulator_name="COMESA Competition Commission",
        case_sections={
            "REFERENCE NUMBER": reference_number,
            "PARTIES": case_parties,
            "SECTOR": sector,
        },
        source_label="the COMESA case parties and sector",
        deals=deals,
    )


def generate_matched_case_email_html(
    case_info: Dict[str, Any], deal: Dict[str, Any]
) -> str:
    target = deal.get("target") or deal.get("target_name", "N/A")
    acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
    deal_id = str(deal.get("_id")) if deal.get("_id") else "N/A"
    detail_url = case_info.get("detail_url") or REGISTRY_URL

    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>COMESA - New Case</title></head>
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
  <div style="margin-top:10px;">
    <a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Case →</a>
  </div>
</div>
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">COMESA - New Case</div>
  <div style="display:grid;grid-template-columns:200px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">Reference:</div><div>{escape_html(case_info.get("reference_number", "N/A"))}</div>
    <div style="font-weight:700;">Parties:</div><div>{escape_html(case_info.get("case_parties", "N/A"))}</div>
    <div style="font-weight:700;">Sector:</div><div>{escape_html(case_info.get("sector", "N/A"))}</div>
    <div style="font-weight:700;">Outcome:</div><div>{escape_html(case_info.get("tax_case_outcome", "N/A"))}</div>
    <div style="font-weight:700;">Status:</div><div>{escape_html(case_info.get("hf_tax_case_status", "N/A"))}</div>
    <div style="font-weight:700;">Notice Date:</div><div>{escape_html(case_info.get("notice_date", "N/A"))}</div>
  </div>
</div>
</div>
</body>
</html>"""


def generate_usa_related_email_html(case_info: Dict[str, Any]) -> str:
    detail_url = case_info.get("detail_url") or REGISTRY_URL
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>USA-Related COMESA Case</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">USA-Related COMESA Case</div>
  <div style="font-size:14px;color:#1e3a8a;">This merger review appears to involve USA-related parties or markets.</div>
  <div style="margin-top:10px;">
    <a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:700;font-size:14px;">View Case →</a>
  </div>
</div>
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;">
  <div style="font-size:18px;font-weight:800;margin-bottom:16px;">Case Details</div>
  <div style="display:grid;grid-template-columns:200px 1fr;row-gap:12px;column-gap:18px;">
    <div style="font-weight:700;">Reference:</div><div>{escape_html(case_info.get("reference_number", "N/A"))}</div>
    <div style="font-weight:700;">Parties:</div><div>{escape_html(case_info.get("case_parties", "N/A"))}</div>
    <div style="font-weight:700;">Sector:</div><div>{escape_html(case_info.get("sector", "N/A"))}</div>
    <div style="font-weight:700;">Outcome:</div><div>{escape_html(case_info.get("tax_case_outcome", "N/A"))}</div>
    <div style="font-weight:700;">Status:</div><div>{escape_html(case_info.get("hf_tax_case_status", "N/A"))}</div>
    <div style="font-weight:700;">Notice Date:</div><div>{escape_html(case_info.get("notice_date", "N/A"))}</div>
  </div>
</div>
</div>
</body>
</html>"""


def send_email_via_webhook(
    subject: str,
    html_content: str,
    case_info: Dict[str, Any],
    deal_id: Optional[str] = None,
) -> bool:
    try:
        payload = {
            "subject": subject,
            "html": html_content,
            "reference_number": case_info.get("reference_number", "N/A"),
            "case_parties": case_info.get("case_parties", "N/A"),
            "sector": case_info.get("sector", "N/A"),
            "tax_case_outcome": case_info.get("tax_case_outcome", "N/A"),
            "hf_tax_case_status": case_info.get("hf_tax_case_status", "N/A"),
            "notice_date": case_info.get("notice_date", "N/A"),
            "detail_url": case_info.get("detail_url", ""),
            "deal_id": deal_id,
            "is_new_case": True,
            "source": "comesa_competition_commission",
        }
        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.warning(f"Error sending email: {e}")
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    try:
        result = collection.insert_one(case_info)
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"Error inserting case: {e}")
        return None


def run_comesa_cases_register(bootstrap: bool = False):
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = datetime.now()
    error_items: List[Dict[str, Any]] = []
    new_cases: List[Dict[str, Any]] = []
    llm_match_count = 0
    regex_match_count = 0
    inserted_count = 0
    skipped_existing = 0
    parsed_count = 0

    mode_label = "Bootstrap (DB only)" if bootstrap else "New case monitor"
    logger.info("=" * 60)
    logger.info(f"Starting COMESA Cases Register — {mode_label}")
    logger.info(f"Log file: {LOG_FILE}")
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
        logger.info(f"MongoDB: {message}")

        if not is_connected():
            collect_error(
                error_items,
                "MongoDB not connected. Exiting.",
                step="mongodb_connect",
            )
            return

        collection = get_comesa_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "Could not access 'comesa_cases' collection. Exiting.",
                step="get_collection",
            )
            return

        html = fetch_registry_html(REGISTRY_URL)
        if not html:
            collect_error(
                error_items,
                "Failed to fetch registry HTML",
                step="fetch_registry_html",
                context={"url": REGISTRY_URL},
            )
            return

        all_rows = parse_comesa_table(html)
        parsed_count = len(all_rows)
        if not all_rows:
            logger.warning("No case rows parsed from registry table. Exiting.")
            return

        open_deals = None if bootstrap else fetch_open_deals()

        for idx, row in enumerate(all_rows, 1):
            try:
                reference_number = (row.get("reference_number") or "").strip()
                detail_url = (row.get("detail_url") or "").strip()
                logger.info(
                    f"[{idx}/{len(all_rows)}] {reference_number} | "
                    f"{(row.get('case_parties') or '')[:60]}"
                )

                if not detail_url:
                    logger.warning("Row missing detail_url; skipping")
                    continue

                if case_exists(collection, detail_url):
                    skipped_existing += 1
                    logger.info("Already in comesa_cases; skipping")
                    continue

                now_iso = utc_now_iso()
                case_info: Dict[str, Any] = {
                    **row,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }

                if bootstrap:
                    inserted_id = insert_case(collection, case_info)
                    if inserted_id:
                        inserted_count += 1
                        backup_case = dict(case_info)
                        new_cases.append(backup_case)
                        logger.info(f"Bootstrap inserted (id={inserted_id})")
                    else:
                        collect_error(
                            error_items,
                            "Failed to insert case",
                            step="insert_case",
                            context={"reference_number": reference_number},
                        )
                    continue

                case_parties = row.get("case_parties", "")
                sector = row.get("sector", "")

                try:
                    matched_deal_id = match_case_to_deal(
                        case_parties,
                        sector,
                        reference_number,
                        deals=open_deals,
                    )
                except Exception as e:
                    logger.exception(f"Deal matching error: {e}")
                    collect_error(
                        error_items,
                        str(e),
                        step="match_case_to_deal",
                        context={"reference_number": reference_number},
                    )
                    matched_deal_id = None

                matched_by_regex = False
                if matched_deal_id:
                    llm_match_count += 1
                else:
                    matched_deal_id = regex_match_comesa_deal(
                        case_parties, open_deals or []
                    )
                    if matched_deal_id:
                        matched_by_regex = True
                        regex_match_count += 1
                        logger.info(
                            f"Regex fallback matched deal_id={matched_deal_id}"
                        )

                if matched_deal_id:
                    case_info["deal_id"] = matched_deal_id
                    deal = get_deal_by_id(matched_deal_id)
                    if deal:
                        subject = build_subject("comesa", "new", deal)
                        subject = apply_regex_match_subject(
                            subject, matched_by_regex
                        )
                        html_email = generate_matched_case_email_html(
                            case_info, deal
                        )
                        if not send_email_via_webhook(
                            subject,
                            html_email,
                            case_info,
                            deal_id=matched_deal_id,
                        ):
                            collect_error(
                                error_items,
                                "Failed to send matched-case email",
                                step="send_email",
                                context={
                                    "reference_number": reference_number,
                                    "deal_id": matched_deal_id,
                                },
                            )
                else:
                    try:
                        details_for_llm = (
                            f"Reference: {reference_number}\n"
                            f"Parties: {case_parties}\n"
                            f"Sector: {sector}\n"
                            f"Outcome: {row.get('tax_case_outcome', '')}\n"
                            f"Status: {row.get('hf_tax_case_status', '')}\n"
                            f"Notice Date: {row.get('notice_date', '')}"
                        )
                        is_usa = bool(
                            verify_usa_relation(
                                company_details=details_for_llm,
                                case_type="COMESA",
                            )
                        )
                    except Exception as e:
                        logger.exception(f"USA verification error: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="verify_usa_relation",
                            context={"reference_number": reference_number},
                        )
                        is_usa = False

                    if is_usa:
                        subject = build_subject("comesa", "new")
                        html_email = generate_usa_related_email_html(case_info)
                        if not send_email_via_webhook(
                            subject, html_email, case_info
                        ):
                            collect_error(
                                error_items,
                                "Failed to send USA-related email",
                                step="send_email",
                                context={"reference_number": reference_number},
                            )
                    else:
                        logger.info("Not matched and not USA-related; silent insert")

                inserted_id = insert_case(collection, case_info)
                if inserted_id:
                    inserted_count += 1
                    backup_case = dict(case_info)
                    backup_case.pop("_id", None)
                    new_cases.append(backup_case)
                    logger.info(f"Inserted into comesa_cases (id={inserted_id})")
                else:
                    collect_error(
                        error_items,
                        "Failed to insert case",
                        step="insert_case",
                        context={"reference_number": reference_number},
                    )
            except Exception as e:
                logger.exception(f"Error processing row #{idx}: {e}")
                collect_error(
                    error_items,
                    str(e),
                    step="process_row",
                    context={
                        "reference_number": (row.get("reference_number") or ""),
                    },
                )

        if new_cases:
            try:
                with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                    json.dump(new_cases, f, indent=2, ensure_ascii=False)
                logger.info(
                    f"Saved {len(new_cases)} new cases to backup JSON: {BACKUP_JSON}"
                )
            except Exception as e:
                logger.warning(f"Error writing backup JSON: {e}")

    except Exception as e:
        logger.exception(f"Unhandled error in run_comesa_cases_register: {e}")
        collect_error(
            error_items,
            f"Unhandled error: {e}",
            step="run_main",
        )
    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round((datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Mode                         : {mode_label}")
        logger.info(f"  Rows parsed                  : {parsed_count}")
        logger.info(f"  Skipped (already in DB)      : {skipped_existing}")
        logger.info(f"  Inserted                     : {inserted_count}")
        if not bootstrap:
            logger.info(f"  LLM deal matches             : {llm_match_count}")
            logger.info(f"  Regex fallback matches       : {regex_match_count}")
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="COMESA case registry scraper")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Initial import: insert all records without deal match or USA checks",
    )
    args = parser.parse_args()
    run_comesa_cases_register(bootstrap=args.bootstrap)
