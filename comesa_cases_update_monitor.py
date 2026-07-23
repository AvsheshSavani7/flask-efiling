"""
COMESA Cases Update Monitor (comesa_cases collection)
=====================================================
Monitors cases with hf_tax_case_status == "current-cases" for outcome/status changes.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from html import escape as escape_html
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from dotenv import load_dotenv

from comesa_cases_register import (
    REGISTRY_URL,
    fetch_registry_html,
    match_case_to_deal,
    parse_comesa_table,
    utc_now_iso,
)
from deal_match_llm import fetch_open_deals
from deal_match_regex import apply_regex_match_subject, regex_match_comesa_deal
from email_subject_builder import build_subject
from llm_verification_service import verify_usa_relation
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from n8n_email_service import post_email_payload
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

ENV_PATH = ".env"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "comesa_cases_update_monitor"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

CHANGE_FIELDS = ["hf_tax_case_status", "tax_case_outcome"]

FIELD_LABELS = {
    "hf_tax_case_status": "Case Status",
    "tax_case_outcome": "Case Outcome",
}


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


def get_comesa_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["comesa_cases"]


def build_fresh_lookup(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        detail_url = (row.get("detail_url") or "").strip()
        if detail_url:
            lookup[detail_url] = row
    return lookup


def detect_changes(
    old_case: Dict[str, Any], new_row: Dict[str, Any]
) -> List[Tuple[str, Any, Any]]:
    differences: List[Tuple[str, Any, Any]] = []
    for field in CHANGE_FIELDS:
        old_val = (old_case.get(field) or "").strip()
        new_val = (new_row.get(field) or "").strip()
        if old_val != new_val:
            differences.append((field, old_case.get(field), new_row.get(field)))
    return differences


def generate_update_email_html(
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any]],
) -> str:
    reference_number = new_case.get(
        "reference_number", old_case.get("reference_number", "N/A")
    )
    case_parties = new_case.get(
        "case_parties", old_case.get("case_parties", "N/A")
    )
    detail_url = new_case.get("detail_url") or old_case.get("detail_url") or REGISTRY_URL

    def _val(v: Any) -> str:
        if v is None:
            return "—"
        return escape_html(str(v).strip())

    rows_html = ""
    for field, old_val, new_val in changes:
        label = FIELD_LABELS.get(field, field)
        rows_html += f"""
<tr>
  <td style="padding:8px 12px;font-weight:600;color:#475569;">{escape_html(label)}</td>
  <td style="padding:8px 12px;color:#64748b;text-decoration:line-through;">{_val(old_val)}</td>
  <td style="padding:8px 12px;font-weight:600;color:#0f172a;">{_val(new_val)}</td>
</tr>"""

    changed_names = ", ".join(FIELD_LABELS.get(f, f) for f, _, _ in changes)

    if deal:
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        deal_id = str(deal.get("_id")) if deal.get("_id") else "N/A"
        deal_banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #2563eb;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;">
    <span style="font-weight:700;">Acquirer:</span> {escape_html(str(acquirer))}
    <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Target:</span> {escape_html(str(target))}
    <span style="color:#94a3b8;margin:0 8px;">|</span>
    <span style="font-weight:700;">Deal ID:</span> {escape_html(deal_id)}
  </div>
</div>"""
    else:
        deal_banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">USA-Related COMESA Case</div>
  <div style="font-size:14px;color:#1e3a8a;">This case appears to involve USA-related parties or markets.</div>
</div>"""

    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>COMESA Case Update</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#fef3c7;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #f59e0b;">
  <div style="font-size:16px;font-weight:800;color:#92400e;margin-bottom:8px;">COMESA Case Updated</div>
  <div style="font-size:14px;color:#b45309;">Changed fields: {escape_html(changed_names)}</div>
</div>
{deal_banner}
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;margin-bottom:20px;">
  <div style="font-size:16px;font-weight:800;margin-bottom:12px;">Case Information</div>
  <div style="display:grid;grid-template-columns:180px 1fr;row-gap:10px;column-gap:18px;">
    <div style="font-weight:700;">Reference:</div><div>{_val(reference_number)}</div>
    <div style="font-weight:700;">Parties:</div><div>{_val(case_parties)}</div>
    <div style="font-weight:700;">Sector:</div><div>{_val(new_case.get("sector", old_case.get("sector")))}</div>
    <div style="font-weight:700;">Notice Date:</div><div>{_val(new_case.get("notice_date", old_case.get("notice_date")))}</div>
  </div>
  <div style="margin-top:12px;">
    <a href="{escape_html(detail_url)}" target="_blank" style="color:#2563eb;font-weight:700;">View Case →</a>
  </div>
</div>
<table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;">
<thead><tr style="background:#f1f5f9;">
  <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Field</th>
  <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Previous</th>
  <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Current</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>
</body>
</html>"""


def send_update_email(
    old_case: Dict[str, Any],
    new_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any]],
    matched_by_regex: bool = False,
    usa_related: bool = False,
) -> bool:
    try:
        html = generate_update_email_html(old_case, new_case, deal, changes)
        reference_number = old_case.get("reference_number", "N/A")

        if deal:
            subject = build_subject("comesa", "update", deal)
            subject = apply_regex_match_subject(subject, matched_by_regex)
            deal_id = str(deal.get("_id")) if deal.get("_id") else None
        else:
            subject = build_subject("comesa", "update")
            deal_id = None

        payload = {
            "subject": subject,
            "html": html,
            "reference_number": reference_number,
            "case_parties": old_case.get("case_parties", "N/A"),
            "changed_fields": [f for f, _, _ in changes],
            "deal_id": deal_id,
            "source": "comesa_competition_commission_update",
        }
        if usa_related and not deal:
            payload["usa_related"] = True

        return post_email_payload(payload, subject=subject)
    except Exception as e:
        logger.warning(f"Error sending email: {e}")
        return False


def update_case_document(
    collection, case_doc: Dict[str, Any], new_case_data: Dict[str, Any]
) -> bool:
    try:
        _id = case_doc.get("_id")
        if not _id:
            logger.warning("Case document has no _id; cannot update")
            return False

        updated = dict(new_case_data)
        if "created_at" in case_doc:
            updated["created_at"] = case_doc["created_at"]
        updated["updated_at"] = utc_now_iso()

        result = collection.update_one({"_id": _id}, {"$set": updated})
        if result.modified_count > 0:
            logger.info("Updated case document in comesa_cases")
        else:
            logger.info("No DB changes made (document already up to date)")
        return True
    except Exception as e:
        logger.exception(f"Error updating case document: {e}")
        return False


def process_comesa_cases_updates():
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = datetime.now()
    error_items: List[Dict[str, Any]] = []
    total_checked = 0
    total_changed = 0
    llm_match_count = 0
    regex_match_count = 0

    logger.info("=" * 60)
    logger.info("Starting COMESA Cases Update Monitor")
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

        cases_collection = get_comesa_cases_collection()
        if cases_collection is None:
            collect_error(
                error_items,
                "Could not access 'comesa_cases' collection. Exiting.",
                step="get_collection",
            )
            return

        deals_collection = get_deals_collection()
        open_deals = fetch_open_deals()

        cases = list(
            cases_collection.find({"hf_tax_case_status": "current-cases"})
        )
        if not cases:
            logger.warning(
                'No cases with hf_tax_case_status="current-cases" found.'
            )
            return

        logger.info(f"Found {len(cases)} current cases to monitor")

        html = fetch_registry_html(REGISTRY_URL)
        if not html:
            collect_error(
                error_items,
                "Failed to fetch registry HTML",
                step="fetch_registry_html",
                context={"url": REGISTRY_URL},
            )
            return

        fresh_rows = parse_comesa_table(html)
        if not fresh_rows:
            logger.warning("No rows parsed from registry. Exiting.")
            return

        fresh_lookup = build_fresh_lookup(fresh_rows)

        for idx, case_doc in enumerate(cases, 1):
            try:
                total_checked += 1
                reference_number = (case_doc.get("reference_number") or "").strip()
                detail_url = (case_doc.get("detail_url") or "").strip()
                logger.info(
                    f"[{idx}/{len(cases)}] Checking {reference_number}"
                )

                if not detail_url:
                    logger.warning(
                        f"Case {reference_number} has no detail_url in DB; skipping"
                    )
                    continue

                if detail_url not in fresh_lookup:
                    logger.warning(
                        f"Case {reference_number} not found in registry table; skipping"
                    )
                    continue

                new_row = fresh_lookup[detail_url]
                differences = detect_changes(case_doc, new_row)
                if not differences:
                    logger.info("No changes detected")
                    continue

                total_changed += 1
                changed_fields = [f for f, _, _ in differences]
                logger.info(f"Changes detected: {', '.join(changed_fields)}")

                deal_id = case_doc.get("deal_id")
                deal = None
                # Build $set payload from fresh row only (never include _id).
                new_case_data = dict(new_row)

                if deal_id:
                    logger.info(f"Case linked to deal_id={deal_id}")
                    if deals_collection is not None:
                        try:
                            deal = deals_collection.find_one(
                                {"_id": ObjectId(deal_id)}
                            )
                        except Exception as e:
                            logger.exception(f"Could not fetch deal: {e}")
                            collect_error(
                                error_items,
                                str(e),
                                step="fetch_linked_deal",
                                context={
                                    "reference_number": reference_number,
                                    "deal_id": deal_id,
                                },
                            )

                    # deal_id is set but the deal document could not be found.
                    # Keep the existing email behaviour, but raise an error email
                    # so the missing/broken linkage is visible.
                    if deal is None:
                        collect_error(
                            error_items,
                            "Linked deal_id present but deal document not found",
                            step="fetch_linked_deal",
                            context={
                                "reference_number": reference_number,
                                "deal_id": deal_id,
                                "detail_url": detail_url,
                                "changed_fields": changed_fields,
                            },
                        )

                    if not send_update_email(
                        case_doc, new_row, deal, differences
                    ):
                        collect_error(
                            error_items,
                            "Failed to send update email",
                            step="send_email",
                            context={
                                "reference_number": reference_number,
                                "deal_id": deal_id,
                            },
                        )
                    new_case_data["deal_id"] = deal_id
                else:
                    logger.info("No deal_id; attempting deal match")
                    case_parties = case_doc.get("case_parties", "")
                    sector = case_doc.get("sector", "")

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
                            case_parties, open_deals
                        )
                        if matched_deal_id:
                            matched_by_regex = True
                            regex_match_count += 1
                            logger.info(
                                f"Regex fallback matched deal_id={matched_deal_id}"
                            )

                    if matched_deal_id:
                        if deals_collection is not None:
                            try:
                                deal = deals_collection.find_one(
                                    {"_id": ObjectId(matched_deal_id)}
                                )
                            except Exception as e:
                                logger.exception(f"Could not fetch matched deal: {e}")
                                collect_error(
                                    error_items,
                                    str(e),
                                    step="fetch_matched_deal",
                                    context={
                                        "reference_number": reference_number,
                                        "deal_id": matched_deal_id,
                                    },
                                )

                        if not send_update_email(
                            case_doc,
                            new_row,
                            deal,
                            differences,
                            matched_by_regex=matched_by_regex,
                        ):
                            collect_error(
                                error_items,
                                "Failed to send update email",
                                step="send_email",
                                context={
                                    "reference_number": reference_number,
                                    "deal_id": matched_deal_id,
                                },
                            )
                        new_case_data["deal_id"] = matched_deal_id
                    else:
                        logger.info("No deal match; checking USA-related")
                        try:
                            details_for_llm = (
                                f"Reference: {reference_number}\n"
                                f"Parties: {case_parties}\n"
                                f"Sector: {sector}\n"
                                f"Outcome: {new_row.get('tax_case_outcome', '')}\n"
                                f"Status: {new_row.get('hf_tax_case_status', '')}\n"
                                f"Notice Date: {new_row.get('notice_date', '')}"
                            )
                            is_usa = bool(
                                verify_usa_relation(
                                    company_details=details_for_llm,
                                    case_type="COMESA",
                                )
                            )
                        except Exception as e:
                            logger.exception(f"USA relation check error: {e}")
                            collect_error(
                                error_items,
                                str(e),
                                step="verify_usa_relation",
                                context={"reference_number": reference_number},
                            )
                            is_usa = False

                        if is_usa:
                            logger.info("USA-related; sending update email")
                            if not send_update_email(
                                case_doc,
                                new_row,
                                None,
                                differences,
                                usa_related=True,
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to send USA-related update email",
                                    step="send_email",
                                    context={"reference_number": reference_number},
                                )
                        else:
                            logger.info("Not USA-related; updating DB only")

                if not update_case_document(
                    cases_collection, case_doc, new_case_data
                ):
                    collect_error(
                        error_items,
                        "Failed to update case document",
                        step="update_case",
                        context={"reference_number": reference_number},
                    )
            except Exception as e:
                logger.exception(f"Error processing case #{idx}: {e}")
                collect_error(
                    error_items,
                    str(e),
                    step="process_case",
                    context={
                        "reference_number": case_doc.get("reference_number", ""),
                    },
                )

    except Exception as e:
        logger.exception(f"Unhandled error in process_comesa_cases_updates: {e}")
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
        logger.info(f"  Cases checked                : {total_checked}")
        logger.info(f"  Cases with changes           : {total_changed}")
        logger.info(f"  LLM deal matches             : {llm_match_count}")
        logger.info(f"  Regex fallback matches       : {regex_match_count}")
        logger.info(f"  Errors encountered           : {len(error_items)}")
        logger.info(f"  Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    process_comesa_cases_updates()
