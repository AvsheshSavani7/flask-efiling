"""
Virginia SCC / VA PUC Docket Scraper
====================================
Fetches the Breeze documents API for a matter, filters by cutoff (last 15 days),
downloads PDFs via residential proxy, extracts text, and runs tier1/2/3 analysis
via docket_entry_analyzer.

Dockets to follow are configured in va_puc_dockets.json (same folder).
matter_no and docket_number are the same value (e.g. 147078d).

Documents API:
    https://www.scc.virginia.gov/docketsearchapi/breeze/casedetails/getdocuments
        ?$filter=MATTER_NO eq {docket_number}
        &$select=Document_Name,Date_Filed,DocID,FileName

PDF URL pattern:
    https://www.scc.virginia.gov/docketsearch/DOCS/{FileName}

Run all active dockets from va_puc_dockets.json:
    python docket_engine/va_puc_scraper.py --all

Run a single docket:
    python docket_engine/va_puc_scraper.py --docket-number 147078d

Other flags:
    --test-mode     Analyze but skip MongoDB/S3 writes
    --save-json     Save document list to JSON for debugging
    --no-proxy      Disable residential proxy
    --cutoff-days N Only filings on/after today - N days (default: 15)
"""

from __future__ import annotations
from error_email_service import send_error_email
from log_utils import ensure_script_logger, refresh_script_log
from docket_engine.docket_email_service import send_docket_email
from docket_engine.email_renderer import render_intake_card, render_email_html
from docket_engine.intake_analyzer import generate_intake_note
from pymongo import MongoClient
from dotenv import load_dotenv
import requests

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger, _get_log_file = ensure_script_logger("va_puc_scraper")
LOG_FILE = _get_log_file()

VA_SCC_DOCS_API = (
    "https://www.scc.virginia.gov/docketsearchapi/breeze/casedetails/getdocuments"
)
VA_SCC_PDF_BASE = "https://www.scc.virginia.gov/docketsearch/DOCS"

DOCKET_TYPE = "va-puc"
COLLECTION_NAME = "docket"
CUTOFF_DAYS = 15

VA_PUC_DOCKETS_FILE = os.path.join(_THIS_DIR, "va_puc_dockets.json")

DEFAULT_PROXY_HOST = "108.59.242.138"
DEFAULT_PROXY_PORT = 46885
DEFAULT_PROXY_USER = "GSenAgrfKhuNWkd"
DEFAULT_PROXY_PASS = "8lmVa5yl0pKp9MI"

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _cutoff_date(days: int = CUTOFF_DAYS) -> datetime:
    """Return midnight for today - days (naive local date)."""
    today = datetime.now().date()
    return datetime.combine(today - timedelta(days=days), datetime.min.time())


def _normalize_date(value: str) -> str:
    """Normalize API Date_Filed to MM/DD/YYYY."""
    if not value:
        return ""
    raw = value.strip()
    # ISO with optional time: 2026-07-28T00:00:00.000
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return value.strip()


def _parse_filing_date(value: str) -> Optional[datetime]:
    """Parse Date_Filed into a datetime for cutoff comparison."""
    if not value:
        return None
    raw = value.strip()
    if "T" in raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            raw = raw.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _sort_documents_oldest_first(
    documents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Sort by filing date ascending, then document_id for stable same-day order.
    Undated items go last.
    """

    def _key(doc: Dict[str, Any]):
        raw = doc.get("date_raw") or doc.get("date") or ""
        parsed = _parse_filing_date(str(raw))
        doc_id = str(doc.get("document_id") or "")
        if parsed is None:
            return (1, datetime.max, doc_id)
        return (0, parsed, doc_id)

    return sorted(documents, key=_key)


def _truncate(text: str, max_len: int = 180) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _split_document_name(name: str) -> Tuple[str, str]:
    """
    Split 'Party - Document title' into (on_behalf_of, document_type).
    Falls back to empty party and full name as type.
    """
    if not name:
        return "N/A", "Filing"
    if " - " in name:
        party, rest = name.split(" - ", 1)
        party = party.strip() or "N/A"
        rest = rest.strip() or "Filing"
        return party, rest
    return "N/A", name.strip()


def _build_pdf_url(filename: str) -> str:
    """Build PDF URL; encode path segment so # $ @ ! survive."""
    encoded = quote(filename, safe="")
    return f"{VA_SCC_PDF_BASE}/{encoded}"


def _build_documents_api_url(matter_no: str) -> str:
    # OData filter: MATTER_NO eq 147078d (unquoted literal as used by SCC API)
    return (
        f"{VA_SCC_DOCS_API}"
        f"?$filter=MATTER_NO%20eq%20{quote(str(matter_no), safe='')}"
        f"&$select=Document_Name%2CDate_Filed%2CDocID%2CFileName"
    )


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_dockets_config(dockets_file: str = VA_PUC_DOCKETS_FILE) -> List[Dict[str, Any]]:
    """
    Load active docket entries from va_puc_dockets.json.

    Each entry must have:
        docket_number — also used as MATTER_NO in the API (e.g. 147078d)
    Optional:
        description — human-readable label
        active      — set false to skip (default: true)
    """
    if not os.path.isfile(dockets_file):
        raise FileNotFoundError(
            f"Dockets config file not found: {dockets_file}\n"
            f"Expected at: {VA_PUC_DOCKETS_FILE}"
        )
    with open(dockets_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    all_dockets = config.get("dockets", [])
    active = [d for d in all_dockets if d.get("active", True)]
    logger.info(
        f"Loaded {len(active)} active docket(s) from {dockets_file} "
        f"(total in file: {len(all_dockets)})."
    )
    return active


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def _get_mongo_collection() -> Tuple[Any, Any]:
    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        raise ValueError("MONGODB_CONNECTION_STRING not set")
    client = MongoClient(mongodb_uri)
    db_name = (os.environ.get("MONGODB_DATABASE_NAME") or "").strip()
    db = client.get_database(db_name) if db_name else client.get_database()
    return db[COLLECTION_NAME], client


def _batch_filter_existing(collection, doc_ids: List[str]) -> List[str]:
    """Return subset of doc_ids that are NOT yet in MongoDB for va-puc."""
    if not doc_ids:
        return []
    existing = set()
    try:
        cursor = collection.find(
            {
                "metadata.docket_type": DOCKET_TYPE,
                "metadata.document_id": {"$in": doc_ids},
            },
            {"metadata.document_id": 1},
        )
        for doc in cursor:
            existing.add(str(doc.get("metadata", {}).get("document_id", "")))
    except Exception as e:
        logger.warning(f"MongoDB batch dedup failed: {e}")
        return doc_ids
    new_ids = [d for d in doc_ids if d not in existing]
    skipped = len(doc_ids) - len(new_ids)
    logger.info(
        f"Dedup ({DOCKET_TYPE}): checked={len(doc_ids)} "
        f"already_in_db={skipped} new={len(new_ids)}"
    )
    return new_ids


# ---------------------------------------------------------------------------
# Proxy + HTTP
# ---------------------------------------------------------------------------

def _build_proxy_dict(use_proxy: bool) -> Optional[Dict[str, str]]:
    if not use_proxy:
        return None
    host = os.getenv("VA_PUC_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("VA_PUC_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    user = os.getenv("VA_PUC_PROXY_USER", DEFAULT_PROXY_USER)
    pwd = os.getenv("VA_PUC_PROXY_PASS", DEFAULT_PROXY_PASS)
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def _proxy_host_port(use_proxy: bool) -> str:
    if not use_proxy:
        return "direct"
    host = os.getenv("VA_PUC_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("VA_PUC_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    return f"{host}:{port}"


def fetch_documents_json(
    matter_no: str,
    use_proxy: bool = True,
    session: Optional[requests.Session] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch document list JSON from SCC Breeze API."""
    url = _build_documents_api_url(matter_no)
    proxies = _build_proxy_dict(use_proxy)

    if session is None:
        session = requests.Session()
        session.headers.update(_REQUEST_HEADERS)

    try:
        logger.info(
            f"Fetching documents API for matter_no={matter_no} "
            f"proxy={_proxy_host_port(use_proxy)} url={url}"
        )
        t0 = time.time()
        resp = session.get(url, proxies=proxies, timeout=90)
        elapsed = time.time() - t0
        logger.info(
            f"Documents API response: status={resp.status_code} "
            f"bytes={len(resp.content):,} elapsed={elapsed:.1f}s"
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning(
                f"Unexpected API response type: {type(data).__name__} "
                f"preview={str(data)[:300]!r}"
            )
            return None
        logger.info(f"API returned {len(data)} raw document row(s).")
        return data
    except requests.Timeout as e:
        logger.error(
            f"Documents API TIMEOUT for matter_no={matter_no}: {e}"
        )
        return None
    except requests.RequestException as e:
        logger.error(
            f"Documents API request failed for matter_no={matter_no}: {e}"
        )
        return None
    except ValueError as e:
        logger.error(
            f"Documents API JSON parse failed for matter_no={matter_no}: {e}"
        )
        return None
    except Exception as e:
        logger.error(
            f"Documents API unexpected error for matter_no={matter_no}: {e}",
            exc_info=True,
        )
        return None


def normalize_api_documents(raw_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map SCC API rows into the internal document shape."""
    documents: List[Dict[str, Any]] = []
    skipped_no_id = skipped_no_file = 0
    for row in raw_docs:
        doc_id = row.get("DocID")
        if doc_id is None:
            skipped_no_id += 1
            continue
        filename = (row.get("FileName") or "").strip()
        if not filename:
            skipped_no_file += 1
            logger.warning(
                f"Skipping DocID={doc_id}: missing FileName"
            )
            continue
        title = (row.get("Document_Name") or "").strip()
        date_filed = row.get("Date_Filed") or ""
        on_behalf_of, document_type = _split_document_name(title)
        documents.append({
            "document_id": str(doc_id),
            "title": title,
            "date": _normalize_date(str(date_filed)),
            "date_raw": str(date_filed),
            "filename": filename,
            "url": _build_pdf_url(filename),
            "on_behalf_of": on_behalf_of,
            "document_type": document_type,
            "additional_info": title[:200] if title else "N/A",
        })
    logger.info(
        f"Normalized {len(documents)} document(s) "
        f"(skipped_no_DocID={skipped_no_id} skipped_no_FileName={skipped_no_file})."
    )
    return documents


def filter_by_cutoff(
    documents: List[Dict[str, Any]],
    cutoff: datetime,
) -> List[Dict[str, Any]]:
    """Keep documents with Date_Filed on/after cutoff."""
    kept: List[Dict[str, Any]] = []
    dropped = unparseable = 0
    for doc in documents:
        logger.info(
            f"document_id={doc.get('document_id')} date_raw={doc.get('date_raw')}")
        parsed = _parse_filing_date(
            doc.get("date_raw") or doc.get("date") or "")
        if parsed is None:
            unparseable += 1
            logger.warning(
                f"Skipping doc_id={doc.get('document_id')}: "
                f"unparseable date {doc.get('date_raw')!r}"
            )
            continue
        if parsed >= cutoff:
            kept.append(doc)
        else:
            dropped += 1
    logger.info(
        f"Cutoff filter (>= {cutoff.date()}): "
        f"kept={len(kept)} dropped_older={dropped} unparseable={unparseable}"
    )
    if kept:
        logger.info(
            f"Kept {len(kept)} doc(s) on/after cutoff "
            f"(API order preserved until process sort)."
        )
    return kept


# ---------------------------------------------------------------------------
# PDF download and text extraction
# ---------------------------------------------------------------------------

def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes — PyPDF2 first, pymupdf fallback."""
    if not pdf_bytes:
        return ""
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(BytesIO(pdf_bytes))
        parts = []
        for pg in reader.pages:
            try:
                text = pg.extract_text()
                if text:
                    parts.append(text)
            except Exception:
                continue
        result = "\n".join(parts).strip()
        if result:
            logger.info(
                f"  Text extracted via PyPDF2 "
                f"({len(result):,} chars, pages={len(reader.pages)})"
            )
            return result
        logger.info("  PyPDF2 returned empty text; trying pymupdf…")
    except Exception as e:
        logger.warning(f"  PyPDF2 extraction failed: {e}")

    try:
        import fitz  # type: ignore
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = [page.get_text() for page in doc]
        page_count = len(doc)
        doc.close()
        result = "\n".join(parts).strip()
        if result:
            logger.info(
                f"  Text extracted via pymupdf "
                f"({len(result):,} chars, pages={page_count})"
            )
            return result
        logger.warning("  pymupdf also returned empty text.")
    except Exception as e:
        logger.warning(f"  pymupdf extraction failed: {e}")

    return ""


def _download_pdf(
    pdf_url: str,
    doc_id: str,
    use_proxy: bool = True,
    session: Optional[requests.Session] = None,
) -> Optional[bytes]:
    """Download a PDF by URL and return raw bytes. Rejects non-PDF payloads."""
    proxies = _build_proxy_dict(use_proxy)

    if session is None:
        session = requests.Session()
        session.headers.update(_REQUEST_HEADERS)

    try:
        logger.info(f"  Downloading PDF doc_id={doc_id} url={pdf_url}")
        t0 = time.time()
        resp = session.get(pdf_url, proxies=proxies, timeout=120)
        elapsed = time.time() - t0
        content = resp.content or b""
        content_type = resp.headers.get("Content-Type", "")
        logger.info(
            f"  PDF response doc_id={doc_id}: status={resp.status_code} "
            f"Content-Type={content_type!r} bytes={len(content):,} "
            f"elapsed={elapsed:.1f}s"
        )
        resp.raise_for_status()

        if not content:
            logger.warning(f"  doc {doc_id}: empty response body. Skipping.")
            return None

        # Reject HTML / soft-block pages
        head = content[:200].lower()
        if (
            "html" in content_type.lower()
            or b"<html" in head
            or b"<!doctype" in head
        ):
            logger.warning(
                f"  doc {doc_id}: got HTML instead of PDF "
                f"(Content-Type={content_type!r}). Skipping."
            )
            return None

        if not content.startswith(b"%PDF-"):
            logger.warning(
                f"  doc {doc_id}: missing %PDF- magic "
                f"(starts_with={content[:20]!r}). Skipping."
            )
            return None

        logger.info(
            f"  Downloaded valid PDF {len(content):,} bytes for doc {doc_id}.")
        return content
    except requests.Timeout as e:
        logger.warning(f"  Download TIMEOUT for doc {doc_id}: {e}")
        return None
    except requests.RequestException as e:
        logger.warning(f"  Download failed for doc {doc_id}: {e}")
        return None
    except Exception as e:
        logger.warning(
            f"  Download unexpected error for doc {doc_id}: {e}",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def _upload_to_s3(pdf_bytes: bytes, doc_id: str, title: str) -> str:
    """Upload PDF bytes to S3 and return the public URL. Returns '' on failure."""
    try:
        from aws_utils import build_docket_key, upload_bytes_to_s3
        slug = _safe_slug(title[:50])
        key = build_docket_key(f"va_puc_{doc_id}_{slug}.pdf")
        result = upload_bytes_to_s3(pdf_bytes, key)
        url = result.get("url", "")
        logger.info(f"  S3 upload: {url}")
        return url
    except Exception as e:
        logger.warning(f"  S3 upload failed for doc {doc_id}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Single-docket scraper
# ---------------------------------------------------------------------------

def scrape_va_puc(
    docket_number: str,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
    cutoff_days: int = CUTOFF_DAYS,
) -> Dict[str, Any]:
    """
    Scrape one VA PUC / SCC matter: fetch API → cutoff → dedup →
    download PDFs → extract text → analyze → email.

    Args:
        docket_number: Matter number / docket number (same), e.g. "147078d"
        use_proxy:     Route requests through residential proxy
        test_mode:     Analyze but do NOT write to MongoDB or S3
        save_json:     Save document list to JSON for debugging
        cutoff_days:   Only filings on/after today - N days

    Returns:
        Dict with success, counts, and per-doc results.
    """
    from docket_entry_analyzer import analyze_docket_entry

    refresh_script_log(logger, _get_log_file)

    matter_no = (docket_number or "").strip()
    if not matter_no:
        msg = "docket_number / matter_no is required"
        logger.error(msg)
        return {"success": False, "error": msg, "processed": []}

    if cutoff_days < 0:
        logger.warning(
            f"cutoff_days={cutoff_days} invalid; using {CUTOFF_DAYS}")
        cutoff_days = CUTOFF_DAYS

    cutoff = _cutoff_date(cutoff_days)
    log_path = _get_log_file()

    logger.info(
        f"=== VA PUC scraper START — docket_number={matter_no} "
        f"cutoff={cutoff.date()} (last {cutoff_days} days) "
        f"use_proxy={use_proxy} test_mode={test_mode} "
        f"save_json={save_json} log_file={log_path} ==="
    )

    _run_ctx = {
        "docket_number": matter_no,
        "matter_no": matter_no,
        "cutoff_days": cutoff_days,
        "use_proxy": use_proxy,
        "test_mode": test_mode,
    }

    def _error_email(error_message: str, extra_ctx: Optional[dict] = None) -> None:
        try:
            send_error_email(
                script_name="va_puc_scraper",
                error_message=error_message,
                context={**_run_ctx, **(extra_ctx or {})},
            )
        except Exception as e:
            logger.warning(f"send_error_email failed: {e}")

    collection = None
    mongo_client = None
    if not test_mode:
        try:
            collection, mongo_client = _get_mongo_collection()
            logger.info("MongoDB connection established.")
        except Exception as e:
            msg = f"MongoDB connection failed: {e}"
            logger.error(msg)
            _error_email(msg, {"step": "mongodb_connect"})
            return {"success": False, "error": msg, "processed": []}

    download_session = requests.Session()
    download_session.headers.update(_REQUEST_HEADERS)

    # Step 1: Fetch documents API
    raw_docs = fetch_documents_json(
        matter_no, use_proxy=use_proxy, session=download_session
    )
    if raw_docs is None:
        msg = f"Could not fetch documents API for matter_no={matter_no}"
        logger.error(msg)
        _error_email(
            msg,
            {"step": "fetch_api", "url": _build_documents_api_url(matter_no)},
        )
        if mongo_client:
            mongo_client.close()
        return {"success": False, "error": msg, "processed": []}

    documents = normalize_api_documents(raw_docs)
    if not documents:
        logger.info("No documents returned after normalization.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "No documents found.",
            "docket_number": matter_no,
            "cutoff_date": cutoff.date().isoformat(),
            "timestamp": _now_iso(),
        }

    logger.info(
        f"After normalize: {len(documents)} docs. "
        f"Sample newest date={documents[0].get('date')} "
        f"oldest_in_list={documents[-1].get('date')}"
    )

    # Step 2: Cutoff filter
    documents = filter_by_cutoff(documents, cutoff)
    if not documents:
        logger.info("No documents within cutoff window.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "No documents within cutoff window.",
            "docket_number": matter_no,
            "cutoff_date": cutoff.date().isoformat(),
            "timestamp": _now_iso(),
        }

    if save_json:
        out_file = os.path.join(
            _THIS_DIR, f"va_puc_{matter_no}_documents.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(documents, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved document list to {out_file}")

    # Step 3: Dedup
    all_ids = [d["document_id"] for d in documents]
    if collection is not None:
        new_ids_set = set(_batch_filter_existing(collection, all_ids))
    else:
        new_ids_set = set(all_ids)
        logger.info(
            f"test_mode/no-mongo: treating all {len(all_ids)} cutoff docs as new."
        )

    new_documents = [d for d in documents if d["document_id"] in new_ids_set]
    if not new_documents:
        logger.info("All documents already in the database.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "All documents already in DB.",
            "docket_number": matter_no,
            "total_found": len(documents),
            "new": 0,
            "cutoff_date": cutoff.date().isoformat(),
            "timestamp": _now_iso(),
        }

    # Stable chronological order (date ASC, then document_id)
    new_documents = _sort_documents_oldest_first(new_documents)
    logger.info(
        f"{len(new_documents)} new document(s) to process "
        f"(out of {len(documents)} within cutoff), oldest first. "
        f"Range: {new_documents[0].get('date')} → {new_documents[-1].get('date')}"
    )

    # Step 4: Download → extract → analyze → email
    processed: List[Dict[str, Any]] = []

    for i, doc in enumerate(new_documents):
        doc_id = doc["document_id"]
        title = doc["title"]
        filed_date = doc["date"]
        pdf_url = doc["url"]
        on_behalf_of = doc.get("on_behalf_of") or "N/A"
        document_type = doc.get("document_type") or "Filing"
        additional_info = doc.get("additional_info") or title[:200]

        logger.info(
            f"[{i+1}/{len(new_documents)}] doc_id={doc_id} | "
            f"date={filed_date} | {title[:70]}"
        )
        _doc_ctx = {
            "doc_id": doc_id,
            "title": title[:120],
            "filed_date": filed_date,
        }

        pdf_bytes = _download_pdf(
            pdf_url, doc_id, use_proxy=use_proxy, session=download_session
        )
        if not pdf_bytes:
            msg = f"PDF download failed for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(
                msg, {**_doc_ctx, "step": "download_pdf", "pdf_url": pdf_url})
            processed.append({
                "doc_id": doc_id,
                "title": title,
                "status": "download_failed",
            })
            continue

        extracted_text = _extract_text_from_pdf_bytes(pdf_bytes)
        if not extracted_text.strip():
            msg = f"No text extracted from PDF for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "extract_text"})
            processed.append({
                "doc_id": doc_id,
                "title": title,
                "status": "no_text_extracted",
            })
            continue

        s3_url = ""
        if not test_mode:
            s3_url = _upload_to_s3(pdf_bytes, doc_id, title)
            if not s3_url:
                logger.warning(
                    f"  S3 upload empty for doc_id={doc_id}; "
                    f"falling back to source PDF URL"
                )

        metadata = {
            "docket_type": DOCKET_TYPE,
            "docket_number": matter_no,
            "document_id": doc_id,
            "date": filed_date,
            "document_type": document_type[:200],
            "on_behalf_of": on_behalf_of[:200],
            "additional_info": additional_info[:200],
            "url": s3_url or pdf_url,
        }

        logger.info(
            f"  Analyzing — type={_truncate(metadata['document_type'], 60)} "
            f"on_behalf_of={_truncate(on_behalf_of, 50)}"
        )
        try:
            result = analyze_docket_entry(
                doc_number=doc_id,
                full_text=extracted_text,
                metadata=metadata,
                test_mode=test_mode,
            )
            status = result.get("status", "unknown")

            if result.get("error"):
                msg = f"Docket analysis error for doc_id={doc_id}: {result['error']}"
                logger.warning(f"  {msg}")
                _error_email(
                    msg,
                    {
                        **_doc_ctx,
                        "step": "docket_analysis",
                        "analysis_error": result["error"],
                    },
                )
                processed.append({
                    "doc_id": doc_id,
                    "title": title,
                    "status": "analysis_error",
                    "error": result["error"],
                })
            else:
                logger.info(
                    f"  → status={status} "
                    f"deal_id={result.get('deal_id') or '(none)'} "
                    f"hash_id={result.get('hash_id') or result.get('metadata', {}).get('hash_id') or '(n/a)'}"
                )
                email_sent = False
                if status == "new_analysis":
                    try:
                        comprehensive_summary = (
                            result.get("comprehensive_summary") or ""
                        )
                        logger.info(
                            f"  Generating intake note "
                            f"(summary_chars={len(comprehensive_summary):,})"
                        )
                        intake_note = generate_intake_note(
                            comprehensive_summary)
                        if intake_note is None:
                            msg = (
                                f"GPT intake note generation failed "
                                f"for doc_id={doc_id}"
                            )
                            logger.warning(f"  {msg}")
                            _error_email(
                                msg, {**_doc_ctx, "step": "gpt_intake_note"}
                            )
                        else:
                            document_url = (
                                metadata.get("url")
                                or metadata.get("document_id")
                                or ""
                            )
                            base_html = render_intake_card(
                                intake_note, document_url
                            )
                            email_html = render_email_html(
                                tier2_response=(
                                    (result.get("tier2_analysis") or {}).get(
                                        "response"
                                    )
                                    or ""
                                ),
                                tier3_response=(
                                    (result.get("tier3_risk_assessment") or {}).get(
                                        "response"
                                    )
                                    or ""
                                ),
                                base_html=base_html,
                                metadata=metadata,
                            )
                            target_company_name = (
                                (result.get("metadata") or {}).get(
                                    "target_company_name", ""
                                )
                            )
                            subject = (
                                f"{target_company_name} : VAPUC - {matter_no}"
                                f": {additional_info} - {document_type}"
                            )
                            logger.info(
                                f"  Sending docket email subject={subject!r} "
                                f"html_chars={len(email_html):,}"
                            )
                            send_docket_email(
                                subject=subject,
                                email_html=email_html,
                                doc_id=doc_id,
                                docket_number=matter_no,
                                docket_type=DOCKET_TYPE,
                                deal_id=result.get("deal_id"),
                            )
                            email_sent = True
                            logger.info("  Email sent OK.")
                    except Exception as e:
                        # Analysis already succeeded — do not mark as analysis_error
                        msg = (
                            f"Post-analysis email/intake failed for "
                            f"doc_id={doc_id}: {e}"
                        )
                        logger.error(f"  {msg}", exc_info=True)
                        _error_email(
                            msg, {**_doc_ctx, "step": "email_or_intake"}
                        )
                else:
                    logger.info(
                        f"  Skipping intake note and email — status={status}"
                    )

                processed.append({
                    "doc_id": doc_id,
                    "title": title,
                    "filed_date": filed_date,
                    "status": status,
                    "s3_url": s3_url,
                    "email_sent": email_sent,
                })
        except Exception as e:
            msg = f"Docket analysis exception for doc_id={doc_id}: {e}"
            logger.error(f"  {msg}", exc_info=True)
            _error_email(msg, {**_doc_ctx, "step": "docket_analysis"})
            processed.append({
                "doc_id": doc_id,
                "title": title,
                "status": "analysis_error",
                "error": str(e),
            })

        time.sleep(2)

    if mongo_client:
        mongo_client.close()

    success_count = sum(
        1
        for p in processed
        if p["status"]
        not in ("download_failed", "no_text_extracted", "analysis_error")
    )
    fail_count = len(new_documents) - success_count
    logger.info(
        f"=== VA PUC scraper END — docket={matter_no} "
        f"analyzed_ok={success_count}/{len(new_documents)} "
        f"failed={fail_count} cutoff={cutoff.date()} ==="
    )

    return {
        "success": True,
        "docket_number": matter_no,
        "total_found": len(documents),
        "new": len(new_documents),
        "analyzed": success_count,
        "failed": fail_count,
        "processed": processed,
        "cutoff_date": cutoff.date().isoformat(),
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Multi-docket runner
# ---------------------------------------------------------------------------

def scrape_all_va_puc(
    dockets_file: str = VA_PUC_DOCKETS_FILE,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
    cutoff_days: int = CUTOFF_DAYS,
) -> Dict[str, Any]:
    """
    Read va_puc_dockets.json and run scrape_va_puc() for every active entry.

    To add a new docket: add it to va_puc_dockets.json with active=true.
    """
    refresh_script_log(logger, _get_log_file)
    logger.info(
        f"=== VA PUC scrape_all START — file={dockets_file} "
        f"use_proxy={use_proxy} test_mode={test_mode} "
        f"cutoff_days={cutoff_days} log={_get_log_file()} ==="
    )

    try:
        dockets = load_dockets_config(dockets_file)
    except FileNotFoundError as e:
        logger.error(str(e))
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Failed to load dockets config: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

    if not dockets:
        logger.info("No active dockets found in config file.")
        return {
            "success": True,
            "dockets_processed": 0,
            "total_analyzed": 0,
            "results": [],
        }

    all_results = []
    for entry in dockets:
        docket_number = (entry.get("docket_number") or "").strip()
        description = entry.get("description", "")

        if not docket_number:
            logger.warning(
                f"Skipping invalid config entry (missing docket_number): {entry}"
            )
            continue

        logger.info(
            f"\n{'='*60}\n"
            f"Docket / matter: {docket_number}\n"
            f"{description}\n"
            f"{'='*60}"
        )

        result = scrape_va_puc(
            docket_number=docket_number,
            use_proxy=use_proxy,
            test_mode=test_mode,
            save_json=save_json,
            cutoff_days=cutoff_days,
        )
        all_results.append(result)

        if len(dockets) > 1:
            time.sleep(5)

    total_analyzed = sum(r.get("analyzed", 0) for r in all_results)
    any_failed = any(not r.get("success", False) for r in all_results)
    logger.info(
        f"=== VA PUC scrape_all END — dockets={len(all_results)} "
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
        description=(
            "VA PUC / SCC Docket Scraper — downloads PDFs and runs "
            "tier1/2/3 analysis"
        )
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all",
        action="store_true",
        help="Run all active dockets from va_puc_dockets.json",
    )
    mode.add_argument(
        "--docket-number",
        help="Single matter/docket number to process, e.g. 147078d",
    )

    parser.add_argument(
        "--dockets-file",
        default=VA_PUC_DOCKETS_FILE,
        help=f"Path to dockets config JSON (default: {VA_PUC_DOCKETS_FILE})",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=False,
        help="Disable residential proxy (use direct connection)",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        default=False,
        help="Analyze but do NOT write to MongoDB or S3",
    )
    parser.add_argument(
        "--save-json",
        action="store_true",
        default=False,
        help="Save document list to JSON for debugging",
    )
    parser.add_argument(
        "--cutoff-days",
        type=int,
        default=CUTOFF_DAYS,
        help=f"Only filings on/after today - N days (default: {CUTOFF_DAYS})",
    )

    args = parser.parse_args()
    use_proxy = not args.no_proxy

    if args.all:
        result = scrape_all_va_puc(
            dockets_file=args.dockets_file,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
            cutoff_days=args.cutoff_days,
        )
    else:
        result = scrape_va_puc(
            docket_number=args.docket_number,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
            cutoff_days=args.cutoff_days,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
