"""
Ohio PUC (PUCO DIS) Docket Scraper — production wiring
======================================================
Follows the same docket_engine flow as va_puc_scraper.py, but reuses the
already server-tested fetching from ``ohio_puc_josh.py`` (Playwright + Xvfb +
sticky residential proxy rotation) WITHOUT changing it.

Core flow (per docket / case number):
    1. Connect MongoDB "docket" collection.
    2. Open the CaseRecord page and read the FIRST listing page only (no
       pagination in production — newest filings live on page 1).
    3. Process entries OLDEST -> NEWEST (reverse of the site's newest-first order)
       so docket_entry_analyzer's incremental history stays in sequence.
    4. For each entry: skip if already in DB (docket_type + docket_number +
       document_id). Otherwise open the DocumentRecord page, download the PDF,
       extract text (OCR fallback for scanned/image PDFs), run tier1/2/3
       analysis, and send the notification email.

Sequence safety (important):
    docket_entry_analyzer numbers entries incrementally by date. If a WAF block
    prevents fetching an entry, we MUST NOT jump ahead to a later entry — that
    would corrupt the sequence. So on a WAF/fetch failure we rotate to a fresh
    residential IP and restart the docket session (already-saved entries are
    skipped via dedup, so we resume exactly where we left off). Once all proxy
    rotations are exhausted, we stop the docket entirely and send an error email.

Scanned/image PDFs are OCR'd (pytesseract) as a fallback. Only entries that
still yield no text, or have no downloadable PDF, are skipped (not inserted)
and retried on the next run — they never advance the sequence.

Run all active dockets from ohio_puc_dockets.json:
    python docket_engine/ohio_puc_scraper.py --all

Run a single docket:
    python docket_engine/ohio_puc_scraper.py --docket-number 26-0435-EL-MER

Other flags:
    --test-mode   Analyze but skip MongoDB/S3 writes
    --save-json   Save the extracted docket entry list to JSON for debugging
    --no-proxy    Disable residential proxy (direct connection)

On a Linux server the container entrypoint already runs Xvfb (headed Chromium).
"""

from __future__ import annotations
from docket_engine.intake_analyzer import generate_intake_note
from docket_engine.email_renderer import render_intake_card, render_email_html
from docket_engine.docket_email_service import send_docket_email
from error_email_service import send_error_email
from log_utils import ensure_script_logger, refresh_script_log
import ohio_puc_josh as oh
from pymongo import MongoClient
from dotenv import load_dotenv

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# Reuse the server-tested fetching functions unchanged.


load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger, _get_log_file = ensure_script_logger("ohio_puc_scraper")

DOCKET_TYPE = "oh-puc"
COLLECTION_NAME = "docket"
SCRIPT_NAME = "ohio_puc_scraper"
OHIO_PUC_DOCKETS_FILE = os.path.join(_THIS_DIR, "ohio_puc_dockets.json")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", (text or "").lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _oldest_first(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Return entries oldest -> newest.

    The PUCO site lists entries strictly newest-first (both across dates and
    within a single date), so a plain reverse gives true chronological order.
    A date-based sort would be wrong here: Python's stable sort leaves
    same-date entries in the site's newest-first order, i.e. reversed within
    the day, which would number same-day filings backwards in the analyzer's
    incremental sequence.
    """
    return list(reversed(entries))


def _is_waf_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(k in msg for k in ("WAF rejected", "WAF blocked", "F5 challenge"))


def _error_email(message: str, context: Optional[dict] = None) -> None:
    try:
        send_error_email(
            script_name=SCRIPT_NAME,
            error_message=message,
            context=context or {},
        )
    except Exception as e:  # never let error reporting break the run
        logger.warning(f"send_error_email failed: {e}")


# ---------------------------------------------------------------------------
# Config + MongoDB
# ---------------------------------------------------------------------------

def load_dockets_config(
    dockets_file: str = OHIO_PUC_DOCKETS_FILE,
) -> List[Dict[str, Any]]:
    """Load active docket entries from ohio_puc_dockets.json."""
    if not os.path.isfile(dockets_file):
        raise FileNotFoundError(
            f"Dockets config file not found: {dockets_file}")
    with open(dockets_file, "r", encoding="utf-8") as f:
        config = json.load(f)
    all_dockets = config.get("dockets", [])
    active = [d for d in all_dockets if d.get("active", True)]
    logger.info(
        f"Loaded {len(active)} active docket(s) from {dockets_file} "
        f"(total in file: {len(all_dockets)})."
    )
    return active


def _get_mongo_collection() -> Tuple[Any, Any]:
    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        raise ValueError("MONGODB_CONNECTION_STRING not set")
    client = MongoClient(mongodb_uri)
    db_name = (os.environ.get("MONGODB_DATABASE_NAME") or "").strip()
    db = client.get_database(db_name) if db_name else client.get_database()
    return db[COLLECTION_NAME], client


def _entry_exists(collection, docket_number: str, doc_id: str) -> bool:
    """True if this filing is already in MongoDB (scoped + unique by doc_id)."""
    try:
        found = collection.find_one(
            {
                "metadata.docket_type": DOCKET_TYPE,
                "metadata.docket_number": docket_number,
                "metadata.document_id": doc_id,
            },
            {"_id": 1},
        )
        return found is not None
    except Exception as e:
        # Fail open: the analyzer does its own document_id existence check.
        logger.warning(f"  Existence check failed for doc_id={doc_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# S3 + metadata + email
# ---------------------------------------------------------------------------

def _ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    """OCR fallback for scanned/image-only PDFs (returns '' on any failure)."""
    try:
        from docket_engine.ocr_extract import extract_text_from_scanned_pdf_bytes
        return extract_text_from_scanned_pdf_bytes(pdf_bytes)
    except Exception as e:
        logger.warning(f"    OCR extraction failed: {e}")
        return ""


def _upload_to_s3(pdf_bytes: bytes, doc_id: str, title: str) -> str:
    try:
        from aws_utils import build_docket_key, upload_bytes_to_s3
        slug = _safe_slug(title[:50])
        key = build_docket_key(f"oh_puc_{doc_id}_{slug}.pdf")
        result = upload_bytes_to_s3(pdf_bytes, key)
        url = result.get("url", "")
        logger.info(f"  S3 upload: {url}")
        return url
    except Exception as e:
        logger.warning(f"  S3 upload failed for doc {doc_id}: {e}")
        return ""


def _build_metadata(
    docket_number: str,
    doc_id: str,
    date_filed: str,
    summary: str,
    url: str,
) -> Dict[str, str]:
    doc_type = (summary or "Filing")[:200]
    return {
        "docket_type": DOCKET_TYPE,
        "docket_number": docket_number,
        "document_id": doc_id,
        "date": date_filed,
        "document_type": doc_type,
        "on_behalf_of": "N/A",
        "additional_info": (summary or "N/A")[:200],
        "url": url,
    }


def _send_notification_email(
    result: Dict[str, Any],
    metadata: Dict[str, str],
    docket_number: str,
    doc_id: str,
) -> bool:
    """Build + send the docket email for a new_analysis result. Never raises."""
    try:
        comprehensive_summary = result.get("comprehensive_summary") or ""
        logger.info(
            f"  Generating intake note (summary_chars={len(comprehensive_summary):,})"
        )
        intake_note = generate_intake_note(comprehensive_summary)
        if intake_note is None:
            _error_email(
                f"GPT intake note generation failed for doc_id={doc_id}",
                {"doc_id": doc_id, "docket_number": docket_number,
                 "step": "gpt_intake_note"},
            )
            return False

        document_url = metadata.get("url") or metadata.get("document_id") or ""
        base_html = render_intake_card(intake_note, document_url)
        email_html = render_email_html(
            tier2_response=(result.get("tier2_analysis")
                            or {}).get("response", ""),
            tier3_response=(result.get("tier3_risk_assessment")
                            or {}).get("response", ""),
            base_html=base_html,
            metadata=metadata,
        )
        target_company_name = (result.get("metadata") or {}).get(
            "target_company_name", ""
        )
        subject = (
            f"{target_company_name} : OH PUC - {docket_number}: "
            f"{metadata.get('additional_info', '')} - {metadata.get('document_type', '')}"
        )
        logger.info(f"  Sending docket email subject={subject!r}")
        send_docket_email(
            subject=subject,
            email_html=email_html,
            doc_id=doc_id,
            docket_number=docket_number,
            docket_type=DOCKET_TYPE,
            deal_id=result.get("deal_id"),
        )
        logger.info("  Email sent OK.")
        return True
    except Exception as e:
        # Analysis already saved — don't fail the run over an email problem.
        logger.error(f"  Post-analysis email/intake failed for doc_id={doc_id}: {e}",
                     exc_info=True)
        _error_email(
            f"Post-analysis email/intake failed for doc_id={doc_id}: {e}",
            {"doc_id": doc_id, "docket_number": docket_number,
             "step": "email_or_intake"},
        )
        return False


# ---------------------------------------------------------------------------
# Per-entry processing
# ---------------------------------------------------------------------------

def _process_entry(
    entry: Dict[str, Any],
    *,
    page,
    doc_page,
    collection,
    docket_number: str,
    test_mode: bool,
) -> Dict[str, Any]:
    """
    Process a single docket entry.

    Returns a dict with a "status" key. Raises RuntimeError on a WAF/fetch
    failure so the caller can rotate the proxy and restart the session.
    Statuses that DO NOT advance the sequence permanently: no_doc_id, no_cmids,
    no_text (these are simply skipped and retried next run). "analysis_error"
    signals the caller to hard-stop the docket.
    """
    from docket_entry_analyzer import analyze_docket_entry

    doc_id = entry.get("doc_id")
    summary = (entry.get("summary") or "").strip()
    date_filed = entry.get("date_filed") or ""
    doc_url = entry.get("document_record_url") or ""

    logger.info(f"  Entry date={date_filed} doc_id={doc_id} | {summary[:70]}")

    if not doc_id:
        logger.warning("    No doc_id on entry; skipping.")
        return {"doc_id": None, "status": "no_doc_id"}

    if collection is not None and _entry_exists(collection, docket_number, doc_id):
        logger.info("    Already in DB; skipping.")
        return {"doc_id": doc_id, "status": "skipped_exists"}

    # Detail page -> CMIDs (navigate() raises RuntimeError on WAF)
    cmids = oh.extract_cmids_from_document_page(doc_page, doc_url)
    if not cmids:
        logger.warning("    No CMIDs found; skipping (no downloadable PDF).")
        return {"doc_id": doc_id, "status": "no_cmids"}

    # Download PDF via the WAF-cleared in-browser fetch. None => WAF/blocked.
    pdf_bytes = oh.download_pdf(page, cmids[0])
    if not pdf_bytes:
        raise RuntimeError(
            f"WAF blocked PDF download for doc_id={doc_id} cmid={cmids[0]}"
        )

    text = oh.extract_text_from_pdf(pdf_bytes)
    if not text.strip():
        logger.warning("    No embedded text; attempting OCR fallback...")
        text = _ocr_pdf_bytes(pdf_bytes)
        if not text.strip():
            logger.warning("    OCR produced no text; skipping.")
            return {"doc_id": doc_id, "status": "no_text"}
        logger.info(f"    OCR extracted {len(text):,} chars.")

    s3_url = ""
    if not test_mode:
        s3_url = _upload_to_s3(pdf_bytes, doc_id, summary)
        if not s3_url:
            logger.warning("    S3 upload empty; using DocumentRecord URL.")

    metadata = _build_metadata(
        docket_number, doc_id, date_filed, summary, s3_url or doc_url
    )

    logger.info(f"    Analyzing ({len(text):,} chars)...")
    result = analyze_docket_entry(
        doc_number=doc_id,
        full_text=text,
        metadata=metadata,
        test_mode=test_mode,
    )

    if result.get("error"):
        logger.error(
            f"    Analysis error for doc_id={doc_id}: {result['error']}")
        _error_email(
            f"Docket analysis error for doc_id={doc_id}: {result['error']}",
            {"doc_id": doc_id, "docket_number": docket_number,
             "step": "docket_analysis"},
        )
        return {"doc_id": doc_id, "status": "analysis_error",
                "error": result["error"]}

    status = result.get("status", "unknown")
    logger.info(
        f"    → status={status} deal_id={result.get('deal_id') or '(none)'}")

    if status == "new_analysis":
        _send_notification_email(result, metadata, docket_number, doc_id)
        return {"doc_id": doc_id, "status": "analyzed"}

    return {"doc_id": doc_id, "status": status}


# ---------------------------------------------------------------------------
# One docket session (single browser / single proxy)
# ---------------------------------------------------------------------------

def _run_docket_session(
    pw,
    proxy: Optional[dict],
    collection,
    docket_number: str,
    test_mode: bool,
    save_json: bool,
) -> Dict[str, Any]:
    """
    Open one browser session and process the docket oldest -> newest.

    Returns an outcome dict ({"completed": True, ...} or
    {"stopped": True, "reason": ...}). Raises RuntimeError on a WAF/fetch
    failure so the caller rotates the proxy and restarts.
    """
    context, user_data_dir = oh.create_browser(pw, proxy)
    page = context.pages[0] if context.pages else context.new_page()
    processed: List[Dict[str, Any]] = []

    try:
        oh.warmup_session(page)

        case_url = f"{oh.BASE_URL}/CaseRecord.aspx?CaseNo={docket_number}"
        logger.info(f"Fetching case record: {case_url}")
        oh.navigate(page, case_url)  # raises RuntimeError on WAF

        case_meta = oh.extract_case_metadata(page)
        logger.info(f"  Case: {case_meta.get('case_title', 'N/A')} | "
                    f"Status: {case_meta.get('status', 'N/A')}")

        # Production: first listing page only (newest filings). No pagination.
        entries = oh.extract_docket_entries_from_page(page)
        logger.info(f"  Entries on first listing page: {len(entries)}")

        entries = _oldest_first(entries)
        if save_json:
            out_file = os.path.join(
                _THIS_DIR, f"oh_puc_{docket_number.replace('-', '_')}_entries.json"
            )
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
            logger.info(f"  Saved entry list to {out_file}")

        doc_page = context.new_page()

        for i, entry in enumerate(entries):
            logger.info(f"[{i + 1}/{len(entries)}]")
            outcome = _process_entry(
                entry,
                page=page,
                doc_page=doc_page,
                collection=collection,
                docket_number=docket_number,
                test_mode=test_mode,
            )
            processed.append(outcome)

            if outcome["status"] == "analysis_error":
                logger.error(
                    "Stopping docket after analysis error to preserve sequence."
                )
                return {"stopped": True, "reason": "analysis_error",
                        "processed": processed}

            time.sleep(2)  # polite pacing between entries

        return {"completed": True, "processed": processed}

    finally:
        context.close()
        shutil.rmtree(user_data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Single-docket scraper (with proxy rotation)
# ---------------------------------------------------------------------------

def scrape_oh_puc(
    docket_number: str,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
) -> Dict[str, Any]:
    """Scrape one PUCO case: list -> dedup -> per-entry download/analyze/email."""
    refresh_script_log(logger, _get_log_file)

    docket_number = (docket_number or "").strip()
    if not docket_number:
        msg = "docket_number is required"
        logger.error(msg)
        return {"success": False, "error": msg, "processed": []}

    logger.info(
        f"=== OH PUC scraper START — docket_number={docket_number} "
        f"use_proxy={use_proxy} test_mode={test_mode} "
        f"log_file={_get_log_file()} ==="
    )

    collection = None
    mongo_client = None
    if not test_mode:
        try:
            collection, mongo_client = _get_mongo_collection()
            logger.info("MongoDB connection established.")
        except Exception as e:
            msg = f"MongoDB connection failed: {e}"
            logger.error(msg)
            _error_email(msg, {"docket_number": docket_number,
                               "step": "mongodb_connect"})
            return {"success": False, "error": msg, "processed": []}

    proxies = oh.load_sticky_proxies() if use_proxy else []
    random.shuffle(proxies)
    # Up to MAX_PROXY_ROTATIONS distinct residential IPs (capped by how many
    # sticky sessions the credentials file provides).
    attempts = max(1, min(oh.MAX_PROXY_ROTATIONS, len(proxies) or 1))
    case_url = f"{oh.BASE_URL}/CaseRecord.aspx?CaseNo={docket_number}"
    tried_sessions: List[str] = []

    outcome: Optional[Dict[str, Any]] = None
    try:
        with sync_playwright() as pw:
            for attempt in range(1, attempts + 1):
                proxy = proxies[attempt - 1] if proxies else None
                session_id = oh._session_id(proxy)
                tried_sessions.append(session_id)
                logger.info(
                    f"[Attempt {attempt}/{attempts}] proxy session={session_id}"
                )
                try:
                    outcome = _run_docket_session(
                        pw, proxy, collection, docket_number, test_mode, save_json
                    )
                    break
                except Exception as e:
                    is_waf = isinstance(e, RuntimeError) and _is_waf_error(e)
                    # Rotate to a fresh residential IP only for WAF blocks that
                    # still have attempts left.
                    if is_waf and attempt < attempts:
                        logger.warning(
                            f"WAF/fetch failure on session={session_id} "
                            f"({e}); rotating to a fresh residential IP and "
                            f"restarting the docket (dedup resumes where we left off)."
                        )
                        continue
                    # Either all proxy attempts are exhausted (WAF) or an
                    # unexpected/non-WAF error occurred: force-stop this docket
                    # and ALWAYS send a detailed error email so the failure is
                    # visible and the next run can resume from where we left off.
                    if is_waf:
                        reason = "waf"
                        summary = (
                            f"Ohio PUC scraper STOPPED — WAF/fetch failure exhausted "
                            f"all {attempts} proxy attempt(s) for docket "
                            f"{docket_number}. Last error: {e}"
                        )
                    else:
                        reason = "error"
                        summary = (
                            f"Ohio PUC scraper STOPPED — unexpected "
                            f"{type(e).__name__} on attempt {attempt}/{attempts} "
                            f"for docket {docket_number}: {e}"
                        )
                    logger.error(
                        f"Stopping docket after {attempt}/{attempts} attempt(s): {e}",
                        exc_info=not is_waf,
                    )
                    _error_email(
                        summary,
                        {
                            "docket_number": docket_number,
                            "docket_type": DOCKET_TYPE,
                            "case_url": case_url,
                            "step": "fetch",
                            "reason": reason,
                            "error_type": type(e).__name__,
                            "attempts_made": attempt,
                            "max_attempts": attempts,
                            "proxy_sessions_tried": tried_sessions,
                            "last_error": str(e),
                            "log_file": _get_log_file(),
                            "timestamp": _now_iso(),
                        },
                    )
                    outcome = {"stopped": True, "reason": reason,
                               "error": str(e), "processed": []}
                    break
    finally:
        if mongo_client:
            mongo_client.close()

    processed = (outcome or {}).get("processed", [])
    analyzed = sum(1 for p in processed if p.get("status") == "analyzed")
    skipped = sum(1 for p in processed if p.get("status") == "skipped_exists")
    stopped = bool((outcome or {}).get("stopped"))

    logger.info(
        f"=== OH PUC scraper END — docket={docket_number} "
        f"analyzed={analyzed} skipped_existing={skipped} "
        f"stopped={stopped} reason={(outcome or {}).get('reason')} ==="
    )

    return {
        "success": (outcome is not None) and not stopped,
        "docket_number": docket_number,
        "stopped": stopped,
        "reason": (outcome or {}).get("reason"),
        "analyzed": analyzed,
        "skipped_existing": skipped,
        "processed": processed,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Multi-docket runner
# ---------------------------------------------------------------------------

def scrape_all_oh_puc(
    dockets_file: str = OHIO_PUC_DOCKETS_FILE,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
) -> Dict[str, Any]:
    """Run scrape_oh_puc() for every active docket in ohio_puc_dockets.json."""
    refresh_script_log(logger, _get_log_file)
    logger.info(
        f"=== OH PUC scrape_all START — file={dockets_file} "
        f"use_proxy={use_proxy} test_mode={test_mode} ==="
    )

    try:
        dockets = load_dockets_config(dockets_file)
    except Exception as e:
        logger.error(f"Failed to load dockets config: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

    if not dockets:
        logger.info("No active dockets found in config file.")
        return {"success": True, "dockets_processed": 0,
                "total_analyzed": 0, "results": []}

    all_results = []
    for entry in dockets:
        docket_number = (entry.get("docket_number") or "").strip()
        description = entry.get("description", "")
        if not docket_number:
            logger.warning(
                f"Skipping config entry missing docket_number: {entry}")
            continue

        logger.info(
            f"\n{'=' * 60}\nDocket: {docket_number}\n{description}\n{'=' * 60}")
        result = scrape_oh_puc(
            docket_number=docket_number,
            use_proxy=use_proxy,
            test_mode=test_mode,
            save_json=save_json,
        )
        all_results.append(result)

        if len(dockets) > 1:
            time.sleep(5)

    total_analyzed = sum(r.get("analyzed", 0) for r in all_results)
    any_failed = any(not r.get("success", False) for r in all_results)
    logger.info(
        f"=== OH PUC scrape_all END — dockets={len(all_results)} "
        f"analyzed={total_analyzed} any_failed={any_failed} ==="
    )
    return {
        "success": not any_failed,
        "dockets_processed": len(all_results),
        "total_analyzed": total_analyzed,
        "results": all_results,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ohio PUC Docket Scraper — download PDFs, run tier1/2/3, email."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true",
                      help="Run all active dockets from ohio_puc_dockets.json")
    mode.add_argument("--docket-number",
                      help="Single case number to process, e.g. 26-0435-EL-MER")

    parser.add_argument("--dockets-file", default=OHIO_PUC_DOCKETS_FILE,
                        help=f"Path to dockets config JSON (default: {OHIO_PUC_DOCKETS_FILE})")
    parser.add_argument("--no-proxy", action="store_true", default=False,
                        help="Disable residential proxy (direct connection)")
    parser.add_argument("--test-mode", action="store_true", default=False,
                        help="Analyze but do NOT write to MongoDB or S3")
    parser.add_argument("--save-json", action="store_true", default=False,
                        help="Save the extracted docket entry list to JSON")

    args = parser.parse_args()
    use_proxy = not args.no_proxy

    if args.all:
        result = scrape_all_oh_puc(
            dockets_file=args.dockets_file,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
        )
    else:
        result = scrape_oh_puc(
            docket_number=args.docket_number,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
