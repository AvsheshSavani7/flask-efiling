"""
Ohio PUC (PUCO DIS) Tier1 Historical Backfill (2-phase)
=======================================================
Phase 1 — EXTRACT:
  Opens the PUCO CaseRecord page with the WAF-safe headed browser + residential
  proxy rotation (reused from ohio_puc_josh, unchanged), reads ALL docket entries
  (full pagination — not just page 1), downloads each PDF, extracts text
  (OCR fallback for scanned PDFs), and writes ONE JSON file with `content`
  filled. Records are stored oldest → newest.

Phase 2 — ANALYZE (from JSON, batched):
  Reads that JSON, processes records oldest → newest in batches via
  tier1_summary_generator (tier1 ONLY — no tier2/tier3, no email). Inserts each
  new record into MongoDB and stamps a top-level deal_id. Stops immediately on
  an LLM error so hash_id stays chronological; re-run --analyze to resume
  (DB dedup skips already-saved records).

Notes:
  - No S3, no email, no tier2/tier3.
  - deal_id is written on every JSON record and stamped on every saved Mongo doc.
  - Existence is checked by docket_type + docket_number + document_id.

Examples:
    # Phase 1 — extract the FULL docket history to JSON
    python docket_engine/ohio_puc_tier1_backfill.py --docket-number 26-0435-EL-MER --extract

    # Phase 1 smoke — only the 3 newest entries
    python docket_engine/ohio_puc_tier1_backfill.py --docket-number 26-0435-EL-MER --extract --max-docs 3

    # Phase 2 — tier1 from JSON, batches of 10, oldest first, insert to Mongo
    python docket_engine/ohio_puc_tier1_backfill.py --docket-number 26-0435-EL-MER --analyze --batch-size 10

    # Both phases in one go (full history)
    python docket_engine/ohio_puc_tier1_backfill.py --docket-number 26-0435-EL-MER --extract --analyze

    # Retry only extract_status=failed rows in an existing JSON
    python docket_engine/ohio_puc_tier1_backfill.py --docket-number 26-0435-EL-MER --retry-failed

    # Analyze a specific JSON path
    python docket_engine/ohio_puc_tier1_backfill.py --analyze --json-file docket_engine/oh_puc_26-0435-EL-MER_tier1_extract.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

import ohio_puc_josh as oh
from docket_engine.ohio_puc_scraper import (
    DOCKET_TYPE,
    OHIO_PUC_DOCKETS_FILE,
    _get_mongo_collection,
    _is_waf_error,
    _now_iso,
    _ocr_pdf_bytes,
    _oldest_first,
    load_dockets_config,
)
from log_utils import ensure_script_logger, refresh_script_log
from tier1_summary_generator import generate_tier1_summary

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger, _get_log_file = ensure_script_logger("ohio_puc_tier1_backfill")

DEFAULT_MAX_DOCS = 0            # 0 = ALL entries (full history)
DEFAULT_BATCH_SIZE = 10
DEFAULT_DEAL_ID = "69a57e0554958e923ceb7a46"


def _default_json_path(docket_number: str) -> str:
    return os.path.join(_THIS_DIR, f"oh_puc_{docket_number}_tier1_extract.json")


def _resolve_docket_entry(
    docket_number: str,
    dockets_file: str = OHIO_PUC_DOCKETS_FILE,
) -> Dict[str, Any]:
    dockets = load_dockets_config(dockets_file)
    entry = next(
        (d for d in dockets
         if (d.get("docket_number") or "").strip() == docket_number),
        None,
    )
    if not entry:
        raise ValueError(f"Docket '{docket_number}' not found in {dockets_file}")
    return entry


# ---------------------------------------------------------------------------
# Record shape helpers
# ---------------------------------------------------------------------------

def _base_record(entry: Dict[str, Any], docket_number: str, deal_id: Optional[str]) -> Dict[str, Any]:
    summary = (entry.get("summary") or "").strip()
    doc_url = entry.get("document_record_url") or ""
    return {
        "document_id": entry.get("doc_id") or "",
        "docket_number": docket_number,
        "docket_type": DOCKET_TYPE,
        "date": entry.get("date_filed") or "",
        "title": summary,
        "document_type": (summary or "Filing")[:200],
        "on_behalf_of": "N/A",
        "additional_info": (summary or "N/A")[:200],
        "pages": entry.get("pages", ""),
        "url": doc_url,
        "document_record_url": doc_url,
        "content": "",
        "content_length": 0,
        "extract_status": "pending",
        "extract_error": None,
        "deal_id": deal_id,
    }


def _record_to_metadata(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "docket_type": rec.get("docket_type") or DOCKET_TYPE,
        "docket_number": rec.get("docket_number", ""),
        "document_id": rec.get("document_id", ""),
        "date": rec.get("date", ""),
        "document_type": rec.get("document_type") or "N/A",
        "on_behalf_of": rec.get("on_behalf_of") or "N/A",
        "additional_info": (rec.get("additional_info") or rec.get("title") or "")[:200] or "N/A",
        "url": rec.get("url") or "",
    }


def _entry_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the minimal 'entry' shape used by the fetching functions."""
    return {
        "doc_id": rec.get("document_id") or "",
        "summary": rec.get("title") or rec.get("additional_info") or "",
        "date_filed": rec.get("date") or "",
        "pages": rec.get("pages", ""),
        "document_record_url": rec.get("document_record_url") or rec.get("url") or "",
    }


# ---------------------------------------------------------------------------
# Phase 1 — extract one entry (raises RuntimeError on WAF during navigation)
# ---------------------------------------------------------------------------

def _extract_one(
    entry: Dict[str, Any],
    docket_number: str,
    deal_id: Optional[str],
    *,
    page,
    doc_page,
) -> Dict[str, Any]:
    rec = _base_record(entry, docket_number, deal_id)
    doc_id = rec["document_id"]
    doc_url = rec["document_record_url"]

    if not doc_id:
        rec["extract_status"] = "failed"
        rec["extract_error"] = "missing_doc_id"
        return rec
    if not doc_url:
        rec["extract_status"] = "failed"
        rec["extract_error"] = "missing_doc_url"
        return rec

    # DocumentRecord page → CMIDs (navigate() raises RuntimeError on WAF)
    cmids = oh.extract_cmids_from_document_page(doc_page, doc_url)
    if not cmids:
        rec["extract_status"] = "failed"
        rec["extract_error"] = "no_cmids"
        return rec

    pdf_bytes = oh.download_pdf(page, cmids[0])
    if not pdf_bytes:
        rec["extract_status"] = "failed"
        rec["extract_error"] = "download_failed"
        return rec

    text = oh.extract_text_from_pdf(pdf_bytes)
    if not text.strip():
        text = _ocr_pdf_bytes(pdf_bytes)
    if not text.strip():
        rec["extract_status"] = "failed"
        rec["extract_error"] = "no_text"
        return rec

    rec["content"] = text
    rec["content_length"] = len(text)
    rec["cmids"] = cmids
    rec["extract_status"] = "ok"
    logger.info(f"    Extracted {len(text):,} chars.")
    return rec


def _run_extract_session(
    pw,
    proxy: Optional[dict],
    docket_number: str,
    deal_id: Optional[str],
    state: Dict[str, Any],
    max_docs: int,
    only_doc_ids: Optional[set] = None,
) -> None:
    """
    One browser session. Fills state['extracted'] (doc_id -> record) and sets
    state['ordered'] (list of entries, oldest-first) so progress + order survive
    a WAF-triggered restart. Raises RuntimeError on WAF so the caller rotates.
    """
    context, user_data_dir = oh.create_browser(pw, proxy)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        oh.warmup_session(page)

        # If retrying specific doc_ids, use the pre-built ordered list; otherwise
        # scrape the full docket (ALL pages) once and cache the oldest-first order.
        if only_doc_ids is not None and state.get("ordered"):
            ordered = state["ordered"]
        else:
            case_url = f"{oh.BASE_URL}/CaseRecord.aspx?CaseNo={docket_number}"
            logger.info(f"Fetching case record (all pages): {case_url}")
            oh.navigate(page, case_url)  # raises on WAF
            entries = oh.extract_all_docket_entries(page)  # ALL pages
            logger.info(f"  Total entries on docket: {len(entries)}")
            if max_docs and max_docs > 0:
                entries = entries[:max_docs]  # newest N
                logger.info(f"  Limited to {len(entries)} newest entry(ies).")
            ordered = _oldest_first(entries)
            state["ordered"] = ordered

        extracted: Dict[str, Any] = state["extracted"]
        doc_page = context.new_page()

        for i, entry in enumerate(ordered):
            doc_id = entry.get("doc_id")
            if only_doc_ids is not None and doc_id not in only_doc_ids:
                continue
            if doc_id and extracted.get(doc_id, {}).get("extract_status") == "ok":
                continue  # already extracted in a prior attempt

            logger.info(
                f"  [{i + 1}/{len(ordered)}] date={entry.get('date_filed')} "
                f"doc_id={doc_id} | {(entry.get('summary') or '')[:60]}"
            )
            rec = _extract_one(
                entry, docket_number, deal_id, page=page, doc_page=doc_page
            )
            if doc_id:
                extracted[doc_id] = rec
            if rec["extract_status"] != "ok":
                logger.warning(f"    extract_error={rec.get('extract_error')}")
            time.sleep(1)
    finally:
        context.close()
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _extract_with_rotation(
    docket_number: str,
    deal_id: Optional[str],
    use_proxy: bool,
    max_docs: int,
    ordered_seed: Optional[List[Dict[str, Any]]] = None,
    only_doc_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Run extraction under proxy rotation; restart-and-resume on WAF."""
    proxies = oh.load_sticky_proxies() if use_proxy else []
    random.shuffle(proxies)
    attempts = max(1, min(oh.MAX_PROXY_ROTATIONS, len(proxies) or 1))

    state: Dict[str, Any] = {"ordered": ordered_seed, "extracted": {}}
    stopped_error: Optional[str] = None

    with sync_playwright() as pw:
        for attempt in range(1, attempts + 1):
            proxy = proxies[attempt - 1] if proxies else None
            logger.info(
                f"[Attempt {attempt}/{attempts}] proxy session={oh._session_id(proxy)}"
            )
            try:
                _run_extract_session(
                    pw, proxy, docket_number, deal_id, state, max_docs, only_doc_ids
                )
                stopped_error = None
                break
            except RuntimeError as e:
                if _is_waf_error(e) and attempt < attempts:
                    logger.warning(
                        f"WAF/fetch failure ({e}); rotating IP and resuming "
                        f"(already-extracted entries are skipped)."
                    )
                    continue
                stopped_error = str(e)
                logger.error(f"Extraction stopped after WAF/fetch failure: {e}")
                break

    return {"state": state, "error": stopped_error}


# ---------------------------------------------------------------------------
# Phase 1 — EXTRACT → JSON
# ---------------------------------------------------------------------------

def extract_oh_puc_to_json(
    docket_number: str,
    use_proxy: bool = True,
    max_docs: int = DEFAULT_MAX_DOCS,
    deal_id: Optional[str] = DEFAULT_DEAL_ID,
    json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Scrape all docket entries, extract PDF text, write oldest→newest JSON."""
    refresh_script_log(logger, _get_log_file)
    docket_number = docket_number.strip()
    out_path = json_path or _default_json_path(docket_number)

    logger.info(
        f"=== OH PUC EXTRACT START — docket={docket_number} "
        f"max_docs={'ALL' if max_docs == 0 else max_docs} "
        f"use_proxy={use_proxy} deal_id={deal_id or '(none)'} "
        f"out={out_path} log={_get_log_file()} ==="
    )

    run = _extract_with_rotation(docket_number, deal_id, use_proxy, max_docs)
    state = run["state"]
    ordered = state.get("ordered")
    extracted = state.get("extracted", {})

    if not ordered:
        msg = run.get("error") or "Could not load docket entries (WAF or empty)."
        logger.error(msg)
        return {"success": False, "error": msg, "phase": "extract",
                "docket_number": docket_number}

    # Build records in oldest→newest order (failed placeholders for un-extracted)
    records: List[Dict[str, Any]] = []
    for entry in ordered:
        doc_id = entry.get("doc_id")
        rec = extracted.get(doc_id) if doc_id else None
        if rec is None:
            rec = _base_record(entry, docket_number, deal_id)
            rec["extract_status"] = "failed"
            rec["extract_error"] = "not_extracted"
        records.append(rec)

    ok = sum(1 for r in records if r.get("extract_status") == "ok")
    failed = len(records) - ok

    payload = {
        "docket_type": DOCKET_TYPE,
        "docket_number": docket_number,
        "deal_id": deal_id,
        "created_at": _now_iso(),
        "total": len(records),
        "extract_ok": ok,
        "extract_failed": failed,
        "records": records,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    logger.info(f"EXTRACT done. ok={ok} failed={failed} → wrote {out_path}")
    return {
        "success": run.get("error") is None,
        "phase": "extract",
        "docket_number": docket_number,
        "json_path": out_path,
        "total_scraped": len(records),
        "extract_ok": ok,
        "extract_failed": failed,
        "error": run.get("error"),
        "timestamp": _now_iso(),
    }


def retry_failed_extracts_in_json(
    json_path: str,
    use_proxy: bool = True,
) -> Dict[str, Any]:
    """Re-extract only extract_status=failed rows in an existing JSON, in place."""
    refresh_script_log(logger, _get_log_file)

    if not os.path.isfile(json_path):
        msg = f"JSON file not found: {json_path}"
        logger.error(msg)
        return {"success": False, "error": msg, "phase": "retry_failed"}

    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    docket_number = payload.get("docket_number", "")
    deal_id = payload.get("deal_id", DEFAULT_DEAL_ID)
    records: List[Dict[str, Any]] = list(payload.get("records") or [])
    by_id = {r.get("document_id"): r for r in records if r.get("document_id")}
    failed_ids = {
        r["document_id"] for r in records
        if (r.get("extract_status") or "").lower() == "failed" and r.get("document_id")
    }

    logger.info(
        f"=== OH PUC RETRY-FAILED — docket={docket_number} "
        f"failed={len(failed_ids)} / total={len(records)} file={json_path} ==="
    )
    if not failed_ids:
        return {"success": True, "phase": "retry_failed", "docket_number": docket_number,
                "json_path": json_path, "message": "No failed records to retry.",
                "retried": 0, "recovered": 0, "timestamp": _now_iso()}

    # Seed the ordered list from the JSON (so we don't rescrape the case page).
    ordered_seed = [_entry_from_record(r) for r in records]
    run = _extract_with_rotation(
        docket_number, deal_id, use_proxy, max_docs=0,
        ordered_seed=ordered_seed, only_doc_ids=failed_ids,
    )
    extracted = run["state"].get("extracted", {})

    recovered = 0
    for doc_id, new_rec in extracted.items():
        if doc_id in by_id and new_rec.get("extract_status") == "ok":
            # preserve deal_id + carry over any missing metadata
            new_rec.setdefault("deal_id", by_id[doc_id].get("deal_id", deal_id))
            by_id[doc_id].update(new_rec)
            recovered += 1

    ok = sum(1 for r in records if r.get("extract_status") == "ok")
    failed = len(records) - ok
    payload["records"] = records
    payload["extract_ok"] = ok
    payload["extract_failed"] = failed
    payload["updated_at"] = _now_iso()
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    logger.info(f"RETRY-FAILED done. recovered={recovered} (ok={ok} failed={failed})")
    return {
        "success": run.get("error") is None,
        "phase": "retry_failed",
        "docket_number": docket_number,
        "json_path": json_path,
        "retried": len(failed_ids),
        "recovered": recovered,
        "extract_ok": ok,
        "extract_failed": failed,
        "error": run.get("error"),
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Phase 2 — ANALYZE from JSON (tier1 only, batched, oldest first)
# ---------------------------------------------------------------------------

def _filter_new_ids(collection, docket_number: str, doc_ids: List[str]) -> List[str]:
    """Return doc_ids not yet in Mongo (docket_type + docket_number + document_id)."""
    if not doc_ids:
        return []
    existing = set()
    try:
        cursor = collection.find(
            {
                "metadata.docket_type": DOCKET_TYPE,
                "metadata.docket_number": docket_number,
                "metadata.document_id": {"$in": doc_ids},
            },
            {"metadata.document_id": 1},
        )
        for d in cursor:
            existing.add(str(d.get("metadata", {}).get("document_id", "")))
    except Exception as e:
        logger.warning(f"Dedup query failed: {e}")
        return doc_ids
    return [i for i in doc_ids if i not in existing]


def analyze_oh_puc_from_json(
    json_path: str,
    test_mode: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    deal_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Tier1 from JSON (oldest→newest, batched). Insert to Mongo + stamp deal_id."""
    refresh_script_log(logger, _get_log_file)

    if not os.path.isfile(json_path):
        msg = f"JSON file not found: {json_path}"
        logger.error(msg)
        return {"success": False, "error": msg, "phase": "analyze"}

    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    docket_number = payload.get("docket_number", "")
    # JSON is already stored oldest→newest — preserve that exact order.
    records: List[Dict[str, Any]] = list(payload.get("records") or [])

    resolved_deal_id = (deal_id or "").strip() or str(
        payload.get("deal_id") or DEFAULT_DEAL_ID
    ).strip()

    logger.info(
        f"=== OH PUC ANALYZE START — docket={docket_number} records={len(records)} "
        f"batch_size={batch_size} deal_id={resolved_deal_id or '(none)'} "
        f"test_mode={test_mode} json={json_path} log={_get_log_file()} ==="
    )

    with_content = [
        r for r in records
        if (r.get("content") or "").strip() and r.get("extract_status") == "ok"
    ]
    if len(records) - len(with_content):
        logger.warning(
            f"{len(records) - len(with_content)} record(s) have no content — "
            f"skipped (not inserted)."
        )
    if not with_content:
        return {"success": True, "phase": "analyze", "docket_number": docket_number,
                "json_path": json_path, "message": "No records with content.",
                "timestamp": _now_iso()}

    collection = None
    mongo_client = None
    if not test_mode:
        try:
            collection, mongo_client = _get_mongo_collection()
            logger.info("MongoDB connection established.")
        except Exception as e:
            msg = f"MongoDB connection failed: {e}"
            logger.error(msg)
            return {"success": False, "error": msg, "phase": "analyze"}

    all_ids = [r["document_id"] for r in with_content if r.get("document_id")]
    if collection is not None:
        new_ids = set(_filter_new_ids(collection, docket_number, all_ids))
    else:
        new_ids = set(all_ids)

    to_process = [r for r in with_content if r.get("document_id") in new_ids]
    already = len(with_content) - len(to_process)
    logger.info(
        f"With content: {len(with_content)} | already in DB: {already} | "
        f"to analyze: {len(to_process)} (oldest→newest)"
    )
    if not to_process:
        if mongo_client:
            mongo_client.close()
        return {"success": True, "phase": "analyze", "docket_number": docket_number,
                "json_path": json_path, "message": "All records already in DB.",
                "already_in_db": already, "timestamp": _now_iso()}

    processed: List[Dict[str, Any]] = []
    saved = skipped = errors = 0
    stopped_early = False
    stop_reason = None
    batch_size = max(1, batch_size)
    total_batches = (len(to_process) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(to_process))
        batch = to_process[start:end]
        logger.info(
            f"\n--- Batch {batch_idx + 1}/{total_batches} "
            f"(items {start + 1}-{end} of {len(to_process)}) ---"
        )

        for j, rec in enumerate(batch):
            global_i = start + j
            doc_id = rec.get("document_id", "")
            content = (rec.get("content") or "").strip()
            logger.info(
                f"[{global_i + 1}/{len(to_process)}] doc_id={doc_id} | "
                f"date={rec.get('date')} | {(rec.get('document_type') or '')[:50]}"
            )
            metadata = _record_to_metadata(rec)
            rec_deal_id = str(rec.get("deal_id") or "").strip() or resolved_deal_id

            try:
                result = generate_tier1_summary(
                    metadata=metadata, text=content, test_mode=test_mode
                )
                status = result.get("status", "unknown")

                if result.get("error"):
                    msg = (
                        f"tier1 error for doc_id={doc_id}: {result['error']}. "
                        f"Stopping to preserve hash_id sequence (saved={saved}). "
                        f"Re-run --analyze to resume."
                    )
                    logger.error(f"  {msg}")
                    errors += 1
                    stopped_early = True
                    stop_reason = msg
                    processed.append({"doc_id": doc_id, "status": "error",
                                      "error": result["error"], "stopped_run": True})
                    break

                if status == "skipped":
                    logger.info("  → skipped (already exists)")
                    skipped += 1
                    processed.append({"doc_id": doc_id, "status": "skipped"})
                    continue

                # Stamp top-level deal_id on the newly saved Mongo doc.
                if (not test_mode and collection is not None
                        and rec_deal_id and status == "saved"):
                    try:
                        upd = collection.update_one(
                            {
                                "metadata.docket_type": DOCKET_TYPE,
                                "metadata.docket_number": docket_number,
                                "metadata.document_id": doc_id,
                            },
                            {"$set": {"deal_id": rec_deal_id}},
                        )
                        logger.info(
                            f"  → deal_id set={rec_deal_id} "
                            f"matched={upd.matched_count} modified={upd.modified_count}"
                        )
                        if upd.matched_count == 0:
                            logger.warning(
                                f"  deal_id update matched 0 docs for doc_id={doc_id}"
                            )
                    except Exception as e:
                        logger.warning(f"  deal_id update failed for doc_id={doc_id}: {e}")

                logger.info(f"  → {status} (summary_length={result.get('summary_length', 0)})")
                saved += 1
                processed.append({
                    "doc_id": doc_id, "status": status,
                    "summary_length": result.get("summary_length", 0),
                    "deal_id": rec_deal_id or None,
                })
            except Exception as e:
                msg = (
                    f"tier1 exception for doc_id={doc_id}: {e}. Stopping to "
                    f"preserve hash_id sequence (saved={saved})."
                )
                logger.error(f"  {msg}")
                errors += 1
                stopped_early = True
                stop_reason = msg
                processed.append({"doc_id": doc_id, "status": "error",
                                  "error": str(e), "stopped_run": True})
                break

            time.sleep(1)

        if stopped_early:
            break
        logger.info(f"Batch {batch_idx + 1}/{total_batches} complete.")

    if mongo_client:
        mongo_client.close()

    logger.info(
        f"ANALYZE done. saved={saved} skipped={skipped} errors={errors} "
        f"stopped_early={stopped_early}"
    )
    if stopped_early:
        logger.error(
            "Halted for hash_id sequence. Fix/retry, then re-run: "
            f"python docket_engine/ohio_puc_tier1_backfill.py --analyze "
            f"--json-file {json_path}"
        )

    return {
        "success": not stopped_early,
        "phase": "analyze",
        "docket_number": docket_number,
        "json_path": json_path,
        "deal_id": resolved_deal_id or None,
        "batch_size": batch_size,
        "total_in_json": len(records),
        "with_content": len(with_content),
        "already_in_db": already,
        "queued": len(to_process),
        "saved": saved,
        "skipped": skipped,
        "errors": errors,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "processed": processed,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Multi-docket runner
# ---------------------------------------------------------------------------

def run_for_entry(
    entry: Dict[str, Any],
    do_extract: bool,
    do_analyze: bool,
    use_proxy: bool,
    test_mode: bool,
    max_docs: int,
    batch_size: int,
    json_path: Optional[str],
    deal_id: Optional[str],
) -> Dict[str, Any]:
    docket_number = (entry.get("docket_number") or "").strip()
    path = json_path or _default_json_path(docket_number)
    out: Dict[str, Any] = {"docket_number": docket_number, "json_path": path}

    if do_extract:
        out["extract"] = extract_oh_puc_to_json(
            docket_number=docket_number, use_proxy=use_proxy,
            max_docs=max_docs, deal_id=deal_id, json_path=path,
        )
        if not out["extract"].get("success"):
            out["success"] = False
            return out

    if do_analyze:
        out["analyze"] = analyze_oh_puc_from_json(
            json_path=path, test_mode=test_mode,
            batch_size=batch_size, deal_id=deal_id,
        )
        out["success"] = bool(out["analyze"].get("success"))
        return out

    out["success"] = True
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ohio PUC tier1 backfill — Phase1 extract ALL entries → JSON, "
            "Phase2 tier1 + Mongo insert (oldest first, with deal_id)"
        )
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true",
                        help="Run for all active dockets in ohio_puc_dockets.json")
    target.add_argument("--docket-number",
                        help="Single case number (e.g. 26-0435-EL-MER)")
    target.add_argument("--json-file",
                        help="Path to existing extract JSON (use with --analyze/--retry-failed)")

    parser.add_argument("--extract", action="store_true",
                        help="Phase 1: scrape all entries + extract PDFs → JSON")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Re-extract only failed rows in the existing JSON")
    parser.add_argument("--analyze", action="store_true",
                        help="Phase 2: tier1 from JSON → Mongo (batched, oldest first)")
    parser.add_argument("--dockets-file", default=OHIO_PUC_DOCKETS_FILE,
                        help=f"Dockets config (default: {OHIO_PUC_DOCKETS_FILE})")
    parser.add_argument("--max-docs", type=int, default=DEFAULT_MAX_DOCS,
                        help=f"Extract: max newest entries (default {DEFAULT_MAX_DOCS}; 0 = ALL)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Analyze: docs per batch (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--out-json", default=None,
                        help="Override extract JSON output/input path")
    parser.add_argument("--no-proxy", action="store_true", default=False,
                        help="Disable residential proxy")
    parser.add_argument("--test-mode", action="store_true", default=False,
                        help="Analyze without MongoDB writes")
    parser.add_argument("--deal-id", default=DEFAULT_DEAL_ID,
                        help=f"deal_id stamped on each record (default: {DEFAULT_DEAL_ID})")
    args = parser.parse_args()

    if args.extract and args.retry_failed:
        print("ERROR: use either --extract or --retry-failed, not both", file=sys.stderr)
        sys.exit(1)
    if args.json_file and args.extract:
        print("ERROR: --json-file cannot be used with --extract", file=sys.stderr)
        sys.exit(1)
    if not (args.extract or args.analyze or args.retry_failed):
        print("ERROR: pass --extract, --retry-failed, and/or --analyze", file=sys.stderr)
        sys.exit(1)
    if args.max_docs < 0 or args.batch_size < 1:
        print("ERROR: --max-docs >= 0 and --batch-size >= 1", file=sys.stderr)
        sys.exit(1)

    use_proxy = not args.no_proxy
    deal_id = (args.deal_id or "").strip() or None

    # JSON-driven path: --json-file with --retry-failed and/or --analyze
    if args.json_file and not args.extract:
        path = args.json_file
        out: Dict[str, Any] = {"json_path": path, "success": True}
        if args.retry_failed:
            out["retry_failed"] = retry_failed_extracts_in_json(path, use_proxy=use_proxy)
            if not out["retry_failed"].get("success"):
                out["success"] = False
                print(json.dumps(out, indent=2, default=str))
                sys.exit(1)
        if args.analyze:
            out["analyze"] = analyze_oh_puc_from_json(
                json_path=path, test_mode=args.test_mode,
                batch_size=args.batch_size, deal_id=deal_id,
            )
            out["success"] = bool(out["analyze"].get("success"))
        print(json.dumps(out, indent=2, default=str))
        sys.exit(0 if out.get("success") else 1)

    # Docket-driven paths
    if args.all:
        dockets = load_dockets_config(args.dockets_file)
        results = []
        overall_ok = True
        for entry in dockets:
            if not (entry.get("docket_number") or "").strip():
                continue
            logger.info(f"\n{'=' * 60}\nDocket {entry.get('docket_number')}\n{'=' * 60}")
            if args.retry_failed:
                path = args.out_json or _default_json_path(
                    (entry.get("docket_number") or "").strip())
                r = {"docket_number": entry.get("docket_number"), "json_path": path}
                r["retry_failed"] = retry_failed_extracts_in_json(path, use_proxy=use_proxy)
                r["success"] = r["retry_failed"].get("success", False)
                if args.analyze and r["success"]:
                    r["analyze"] = analyze_oh_puc_from_json(
                        json_path=path, test_mode=args.test_mode,
                        batch_size=args.batch_size, deal_id=deal_id)
                    r["success"] = bool(r["analyze"].get("success"))
            else:
                r = run_for_entry(
                    entry=entry, do_extract=args.extract, do_analyze=args.analyze,
                    use_proxy=use_proxy, test_mode=args.test_mode,
                    max_docs=args.max_docs, batch_size=args.batch_size,
                    json_path=args.out_json, deal_id=deal_id,
                )
            results.append(r)
            if not r.get("success"):
                overall_ok = False
                break
            time.sleep(3)
        result = {"success": overall_ok, "dockets_processed": len(results),
                  "results": results, "timestamp": _now_iso()}
    else:
        path = args.out_json or _default_json_path(args.docket_number.strip())
        if args.retry_failed:
            result = {"docket_number": args.docket_number, "json_path": path,
                      "success": True}
            result["retry_failed"] = retry_failed_extracts_in_json(path, use_proxy=use_proxy)
            result["success"] = result["retry_failed"].get("success", False)
            if args.analyze and result["success"]:
                result["analyze"] = analyze_oh_puc_from_json(
                    json_path=path, test_mode=args.test_mode,
                    batch_size=args.batch_size, deal_id=deal_id)
                result["success"] = bool(result["analyze"].get("success"))
        else:
            entry = _resolve_docket_entry(args.docket_number, args.dockets_file)
            result = run_for_entry(
                entry=entry, do_extract=args.extract, do_analyze=args.analyze,
                use_proxy=use_proxy, test_mode=args.test_mode,
                max_docs=args.max_docs, batch_size=args.batch_size,
                json_path=args.out_json, deal_id=deal_id,
            )

    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
