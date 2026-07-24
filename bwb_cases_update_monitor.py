"""
BWB Cases Update Monitor (bwb_cases collection)
===============================================
Monitors is_open=True records. Re-scrapes listing page(s) once per run (current
year; plus prior year in Jan–Apr), then re-scrapes detail pages for open cases.
Compares German fields; translates only changed values to English before saving.
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

from bwb_cases_common import (
    BwbWorkflowError,
    COLLECTION_NAME,
    attach_shared_module_loggers,
    build_listing_lookup,
    determine_is_open,
    ensure_bwb_cases_indexes,
    fetch_listing_page_html,
    fetch_page_html,
    get_bwb_cases_collection,
    listing_url,
    monitor_listing_years,
    normalize_de_text,
    parse_detail_page,
    parse_listing_table,
    playwright_page,
    translate_to_english_required,
    utc_now_iso,
)
from bwb_cases_register import (
    BwbDealMatchResult,
    log_bwb_notification_decision,
    run_bwb_deal_match_pipeline,
    run_bwb_usa_relation_check,
)
from deal_match_llm import fetch_open_deals
from deal_match_regex import apply_regex_match_subject
from email_subject_builder import build_subject
from log_utils import cleanup_old_logs, refresh_log_file
from mongodb_connection import (
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from n8n_email_service import post_email_payload
from scraper_error_utils import collect_error, send_error_summary

load_dotenv(".env")

ENV_PATH = ".env"
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "bwb_cases_update_monitor"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))
DETAIL_FETCH_DELAY_MS = int(os.getenv("BWB_DETAIL_DELAY_MS", "500"))

LISTING_COMPARE_FIELDS = ["status", "parties"]

FIELD_LABELS = {
    "status": "Status (German)",
    "status_en": "Status (English)",
    "parties": "Parties (German)",
    "parties_en": "Parties (English)",
    "detail_content": "Detail Content (German)",
    "detail_content_en": "Detail Content (English)",
    "is_open": "Is Open",
}

TRANSLATABLE_FIELDS = {
    "status": "status_en",
    "parties": "parties_en",
    "detail_content": "detail_content_en",
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
attach_shared_module_loggers(logger, "deal_match_llm", "llm_verification_service")


def detect_changes(
    stored: Dict[str, Any],
    live_listing: Dict[str, Any],
    live_detail_content: str,
) -> List[Tuple[str, Any, Any]]:
    differences: List[Tuple[str, Any, Any]] = []

    for field in LISTING_COMPARE_FIELDS:
        old_val = normalize_de_text(stored.get(field) or "")
        new_val = normalize_de_text(live_listing.get(field) or "")
        if old_val != new_val:
            differences.append((field, stored.get(field), live_listing.get(field)))

    old_detail = normalize_de_text(stored.get("detail_content") or "")
    new_detail = normalize_de_text(live_detail_content or "")
    if old_detail != new_detail:
        differences.append(
            ("detail_content", stored.get("detail_content"), live_detail_content)
        )

    return differences


def generate_update_email_html(
    old_case: Dict[str, Any],
    merged_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any]],
) -> str:
    file_number = merged_case.get(
        "file_number", old_case.get("file_number", "N/A")
    )
    detail_url = merged_case.get("detail_url") or old_case.get("detail_url") or listing_url()

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
        en_key = TRANSLATABLE_FIELDS.get(field)
        if en_key and merged_case.get(en_key):
            rows_html += f"""
<tr>
  <td style="padding:8px 12px;font-weight:600;color:#475569;">{escape_html(FIELD_LABELS.get(en_key, en_key))}</td>
  <td style="padding:8px 12px;color:#64748b;">{_val(old_case.get(en_key))}</td>
  <td style="padding:8px 12px;font-weight:600;color:#0f172a;">{_val(merged_case.get(en_key))}</td>
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
        deal_banner = """
<div style="background:#dbeafe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
  <div style="font-size:15px;font-weight:800;color:#1e40af;margin-bottom:6px;">USA-Related BWB Case</div>
  <div style="font-size:14px;color:#1e3a8a;">This case appears to involve USA-related parties or markets.</div>
</div>"""

    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>BWB Case Update</title></head>
<body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:1100px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#fef3c7;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #f59e0b;">
  <div style="font-size:16px;font-weight:800;color:#92400e;margin-bottom:8px;">BWB Austria Case Updated</div>
  <div style="font-size:14px;color:#b45309;">Changed fields: {escape_html(changed_names)}</div>
</div>
{deal_banner}
<div style="background:#f3f4f6;border-radius:6px;padding:22px 26px;margin-bottom:20px;">
  <div style="font-size:16px;font-weight:800;margin-bottom:12px;">Case Information</div>
  <div style="display:grid;grid-template-columns:180px 1fr;row-gap:10px;column-gap:18px;">
    <div style="font-weight:700;">File Number:</div><div>{_val(file_number)}</div>
    <div style="font-weight:700;">Parties:</div><div>{_val(merged_case.get("parties_en") or merged_case.get("parties"))}</div>
    <div style="font-weight:700;">Merger Date:</div><div>{_val(merged_case.get("merger_date"))}</div>
    <div style="font-weight:700;">Status:</div><div>{_val(merged_case.get("status_en") or merged_case.get("status"))}</div>
    <div style="font-weight:700;">Is Open:</div><div>{_val(merged_case.get("is_open"))}</div>
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
    merged_case: Dict[str, Any],
    deal: Optional[Dict[str, Any]],
    changes: List[Tuple[str, Any, Any]],
    matched_by_regex: bool = False,
    usa_related: bool = False,
) -> bool:
    try:
        html = generate_update_email_html(old_case, merged_case, deal, changes)
        file_number = old_case.get("file_number", "N/A")

        if deal:
            subject = build_subject("bwb", "update", deal)
            subject = apply_regex_match_subject(subject, matched_by_regex)
            deal_id = str(deal.get("_id")) if deal.get("_id") else None
        else:
            subject = build_subject("bwb", "update")
            deal_id = None

        payload = {
            "subject": subject,
            "html": html,
            "file_number": file_number,
            "parties": merged_case.get("parties_en") or merged_case.get("parties", "N/A"),
            "changed_fields": [f for f, _, _ in changes],
            "deal_id": deal_id,
            "source": "bwb_austria_update",
        }
        if usa_related and not deal:
            payload["usa_related"] = True

        logger.info(
            "[%s] [update] Sending email — kind=%s | subject=%s | deal_id=%s | "
            "changed_fields=%s | matched_by_regex=%s | usa_related=%s",
            file_number,
            "matched_deal" if deal else ("usa_related" if usa_related else "update"),
            subject,
            deal_id or "None",
            [f for f, _, _ in changes],
            matched_by_regex,
            usa_related,
        )
        sent = post_email_payload(payload, subject=subject)
        if sent:
            logger.info("[%s] [update] Email sent successfully", file_number)
        else:
            logger.warning("[%s] [update] Email webhook returned failure", file_number)
        return sent
    except Exception as e:
        logger.warning("Error sending update email: %s", e)
        return False


def update_case_document(
    collection,
    case_doc: Dict[str, Any],
    update_fields: Dict[str, Any],
) -> bool:
    try:
        _id = case_doc.get("_id")
        if not _id:
            logger.warning("Case document has no _id; cannot update")
            return False

        update_fields = dict(update_fields)
        update_fields["updated_at"] = utc_now_iso()

        result = collection.update_one({"_id": _id}, {"$set": update_fields})
        if result.modified_count > 0:
            logger.info("Updated case document in %s", COLLECTION_NAME)
        else:
            logger.info("No DB changes made (document already up to date)")
        return True
    except Exception as e:
        logger.exception("Error updating case document: %s", e)
        return False


def build_update_fields(
    changes: List[Tuple[str, Any, Any]],
    live_listing: Dict[str, Any],
    live_detail_content: str,
    file_number: str,
) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {}

    for field, _, new_val in changes:
        if field == "detail_content":
            update_fields["detail_content"] = new_val
        else:
            update_fields[field] = new_val

    if "status" in update_fields:
        update_fields["is_open"] = determine_is_open(update_fields["status"])

    for field, en_field in TRANSLATABLE_FIELDS.items():
        if field in update_fields and update_fields[field]:
            logger.info(
                "[%s] [update] Translating changed field %s → %s",
                file_number,
                field,
                en_field,
            )
            update_fields[en_field] = translate_to_english_required(
                str(update_fields[field]),
                field=en_field,
                file_number=file_number,
            )

    if live_listing.get("merger_date"):
        update_fields["merger_date"] = live_listing["merger_date"]

    return update_fields


def process_bwb_cases_updates(headless: Optional[bool] = None) -> None:
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = datetime.now()
    error_items: List[Dict[str, Any]] = []
    total_checked = 0
    total_changed = 0
    llm_match_count = 0
    regex_match_count = 0
    listing_years = monitor_listing_years()

    logger.info("=" * 60)
    logger.info(
        "Starting BWB Cases Update Monitor (listing_years=%s)",
        listing_years,
    )
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
                error_items,
                "MongoDB not connected. Exiting.",
                step="mongodb_connect",
            )
            return

        collection = get_bwb_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                f"Could not access '{COLLECTION_NAME}' collection. Exiting.",
                step="get_collection",
            )
            return
        ensure_bwb_cases_indexes(collection)

        deals_collection = get_deals_collection()
        open_deals = fetch_open_deals()
        logger.info(
            "Loaded %d open deals for LLM/regex matching",
            len(open_deals or []),
        )

        open_cases = list(collection.find({"is_open": True}))
        if not open_cases:
            logger.info("No open cases to monitor")
            return

        logger.info("Found %d open cases to monitor", len(open_cases))

        with playwright_page(headless=headless) as page:
            live_lookup: Dict[str, Dict[str, Any]] = {}
            for listing_year in listing_years:
                url = listing_url(listing_year)
                listing_html = fetch_listing_page_html(page, url)
                fresh_rows = parse_listing_table(listing_html, year=listing_year)
                if not fresh_rows:
                    collect_error(
                        error_items,
                        "No rows parsed from listing page",
                        step="parse_listing_table",
                        context={"url": url, "year": listing_year},
                    )
                    continue
                live_lookup.update(build_listing_lookup(fresh_rows))
                logger.info(
                    "Loaded %d rows from year %s listing (lookup size=%d)",
                    len(fresh_rows),
                    listing_year,
                    len(live_lookup),
                )

            if not live_lookup:
                collect_error(
                    error_items,
                    "No listing rows available from any year",
                    step="parse_listing_table",
                    context={"listing_years": listing_years},
                )
                return

            for idx, case_doc in enumerate(open_cases, 1):
                try:
                    total_checked += 1
                    file_number = (case_doc.get("file_number") or "").strip()
                    detail_url = (case_doc.get("detail_url") or "").strip()
                    logger.info("[%d/%d] Checking %s", idx, len(open_cases), file_number)

                    if not detail_url:
                        logger.warning("Case %s has no detail_url; skipping", file_number)
                        continue

                    live_listing = live_lookup.get(file_number)
                    if live_listing is None:
                        logger.warning(
                            "Case %s not found on listing page; skipping",
                            file_number,
                        )
                        continue

                    detail_html = fetch_page_html(
                        page,
                        detail_url,
                        wait_ms=DETAIL_FETCH_DELAY_MS,
                    )
                    live_detail = parse_detail_page(detail_html)
                    live_detail_content = live_detail.get("detail_content", "")

                    differences = detect_changes(
                        case_doc, live_listing, live_detail_content
                    )
                    if not differences:
                        logger.info("No changes detected")
                        continue

                    total_changed += 1
                    changed_fields = [f for f, _, _ in differences]
                    logger.info("Changes detected: %s", ", ".join(changed_fields))

                    update_fields = build_update_fields(
                        differences,
                        live_listing,
                        live_detail_content,
                        file_number,
                    )
                    merged = {**case_doc, **update_fields}

                    deal_id = case_doc.get("deal_id")
                    deal = None

                    if deal_id:
                        logger.info(
                            "[%s] [update] Case already linked to deal_id=%s — "
                            "sending update email without re-matching",
                            file_number,
                            deal_id,
                        )
                        if deals_collection is not None:
                            try:
                                deal = deals_collection.find_one(
                                    {"_id": ObjectId(deal_id)}
                                )
                            except Exception as e:
                                logger.exception("Could not fetch deal: %s", e)
                                collect_error(
                                    error_items,
                                    str(e),
                                    step="fetch_linked_deal",
                                    context={
                                        "file_number": file_number,
                                        "deal_id": deal_id,
                                    },
                                )
                        if deal is None:
                            collect_error(
                                error_items,
                                "Linked deal_id present but deal document not found",
                                step="fetch_linked_deal",
                                context={
                                    "file_number": file_number,
                                    "deal_id": deal_id,
                                },
                            )
                        if not send_update_email(
                            case_doc, merged, deal, differences
                        ):
                            collect_error(
                                error_items,
                                "Failed to send update email",
                                step="send_email",
                                context={
                                    "file_number": file_number,
                                    "deal_id": deal_id,
                                },
                            )
                        log_bwb_notification_decision(
                            file_number=file_number,
                            flow="update",
                            match_method="linked_deal",
                            deal_id=str(deal_id),
                            usa_related=False,
                            email_action="linked_deal_update",
                        )
                    else:
                        parties_en = merged.get("parties_en") or merged.get("parties", "")
                        status_en = merged.get("status_en") or merged.get("status", "")
                        logger.info(
                            "[%s] [update] No linked deal_id — running match pipeline",
                            file_number,
                        )

                        match_result = BwbDealMatchResult()
                        try:
                            match_result = run_bwb_deal_match_pipeline(
                                file_number=file_number,
                                parties_en=parties_en,
                                status_en=status_en,
                                open_deals=open_deals,
                                flow="update",
                            )
                        except Exception as e:
                            logger.exception(
                                "[%s] [update] Deal matching error: %s",
                                file_number,
                                e,
                            )
                            collect_error(
                                error_items,
                                str(e),
                                step="match_case_to_deal",
                                context={"file_number": file_number},
                            )

                        matched_deal_id = match_result.deal_id
                        matched_by_regex = match_result.matched_by_regex
                        if match_result.match_method == "llm":
                            llm_match_count += 1
                        elif match_result.match_method == "regex":
                            regex_match_count += 1

                        is_usa = False
                        email_action = "db_only"

                        if matched_deal_id:
                            update_fields["deal_id"] = matched_deal_id
                            merged["deal_id"] = matched_deal_id
                            if deals_collection is not None:
                                try:
                                    deal = deals_collection.find_one(
                                        {"_id": ObjectId(matched_deal_id)}
                                    )
                                except Exception as e:
                                    logger.exception(
                                        "[%s] [update] Could not fetch matched deal: %s",
                                        file_number,
                                        e,
                                    )
                            email_action = "matched_deal_update"
                            if not send_update_email(
                                case_doc,
                                merged,
                                deal,
                                differences,
                                matched_by_regex=matched_by_regex,
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to send update email",
                                    step="send_email",
                                    context={
                                        "file_number": file_number,
                                        "deal_id": matched_deal_id,
                                    },
                                )
                        else:
                            try:
                                details_for_llm = (
                                    f"File Number: {file_number}\n"
                                    f"Parties: {parties_en}\n"
                                    f"Status: {status_en}\n"
                                    f"Detail: {(merged.get('detail_content_en') or '')[:2000]}"
                                )
                                is_usa = run_bwb_usa_relation_check(
                                    file_number=file_number,
                                    details_for_llm=details_for_llm,
                                    flow="update",
                                )
                            except Exception as e:
                                logger.exception(
                                    "[%s] [update] USA relation check error: %s",
                                    file_number,
                                    e,
                                )
                                is_usa = False

                            if is_usa:
                                email_action = "usa_related_update"
                                if not send_update_email(
                                    case_doc,
                                    merged,
                                    None,
                                    differences,
                                    usa_related=True,
                                ):
                                    collect_error(
                                        error_items,
                                        "Failed to send USA-related update email",
                                        step="send_email",
                                        context={"file_number": file_number},
                                    )
                            else:
                                logger.info(
                                    "[%s] [update] Not USA-related; updating DB only",
                                    file_number,
                                )

                        log_bwb_notification_decision(
                            file_number=file_number,
                            flow="update",
                            match_method=match_result.match_method,
                            deal_id=matched_deal_id,
                            usa_related=is_usa,
                            email_action=email_action,
                        )

                    if not update_case_document(collection, case_doc, update_fields):
                        collect_error(
                            error_items,
                            "Failed to update case document",
                            step="update_case",
                            context={"file_number": file_number},
                        )
                except BwbWorkflowError as e:
                    logger.error("Workflow stopped: %s", e)
                    collect_error(
                        error_items,
                        str(e),
                        step=e.step,
                        context=e.context,
                    )
                    raise
                except Exception as e:
                    logger.exception("Error processing case #%d: %s", idx, e)
                    collect_error(
                        error_items,
                        str(e),
                        step="process_case",
                        context={"file_number": case_doc.get("file_number", "")},
                    )

    except BwbWorkflowError:
        logger.error("BWB update monitor workflow aborted due to fatal error")
    except Exception as e:
        logger.exception("Unhandled error in process_bwb_cases_updates: %s", e)
        collect_error(error_items, f"Unhandled error: {e}", step="run_main")
    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round((datetime.now() - run_start).total_seconds(), 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("  Listing years                : %s", listing_years)
        logger.info("  Cases checked                : %s", total_checked)
        logger.info("  Cases with changes           : %s", total_changed)
        logger.info("  LLM deal matches             : %s", llm_match_count)
        logger.info("  Regex fallback matches       : %s", regex_match_count)
        logger.info("  Errors encountered           : %s", len(error_items))
        logger.info("  Total time                   : %ss", elapsed)
        logger.info("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BWB merger cases update monitor")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()
    process_bwb_cases_updates(headless=False if args.no_headless else None)
