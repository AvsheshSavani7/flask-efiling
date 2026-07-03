"""
New Jersey BPU (Board of Public Utilities) Docket Scraper
==========================================================
Fetches BPU case summary pages, parses the documents table, downloads PDFs,
extracts text, and runs tier1/2/3 analysis via docket_entry_analyzer.

Dockets to follow are configured in nj_bpu_dockets.json (same folder).
To add a new docket, just add an entry to that file — no code changes needed.

Case URL pattern:
    https://publicaccess.bpu.state.nj.us/CaseSummary.aspx?case_id=<CASE_ID>

Document download URL pattern:
    https://publicaccess.bpu.state.nj.us/DocumentHandler.ashx?document_id=<DOC_ID>

Install:
    pip install requests beautifulsoup4 PyPDF2 playwright playwright-stealth
    playwright install chromium

Run all active dockets from nj_bpu_dockets.json:
    python docket_engine/nj_bpu_scraper.py --all

Run a single docket:
    python docket_engine/nj_bpu_scraper.py --case-id 2114202 --docket-number TM26030047

Other flags (apply to both modes):
    --test-mode    Analyze but skip MongoDB/S3 writes
    --save-json    Save parsed document list to JSON for debugging
    --no-proxy     Disable residential proxy
    --no-headless  Show browser window (Playwright fallback only)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is on sys.path so imports like docket_entry_analyzer work
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient

from docket_engine.intake_analyzer import generate_intake_note
from docket_engine.email_renderer import render_intake_card, render_email_html
from docket_engine.docket_email_service import send_docket_email
from log_utils import ensure_script_logger, refresh_script_log
from error_email_service import send_error_email

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# Logger writes to /var/data/logs/nj_bpu_scraper/<YYYY-MM-DD>.log (IST)
# and stdout. Child loggers (intake_analyzer, email_renderer) propagate here.
logger, _get_log_file = ensure_script_logger("nj_bpu_scraper")
LOG_FILE = _get_log_file()

NJ_BPU_BASE = "https://publicaccess.bpu.state.nj.us"
NJ_BPU_CASE_URL = f"{NJ_BPU_BASE}/CaseSummary.aspx"
NJ_BPU_DOC_URL = f"{NJ_BPU_BASE}/DocumentHandler.ashx"

DOCKET_TYPE = "nj-bpu"
COLLECTION_NAME = "docket"

# Config file lives next to this script
NJ_BPU_DOCKETS_FILE = os.path.join(_THIS_DIR, "nj_bpu_dockets.json")

# Static residential proxy (same as other scrapers in this repo)
DEFAULT_PROXY_HOST = "108.59.242.138"
DEFAULT_PROXY_PORT = 46885
DEFAULT_PROXY_USER = "GSenAgrfKhuNWkd"
DEFAULT_PROXY_PASS = "8lmVa5yl0pKp9MI"

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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


def _normalize_date(value: str) -> str:
    """Normalize a scraped date string to MM/DD/YYYY."""
    if not value:
        return ""
    raw = value.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return raw


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _normalize_docket_number(raw: str) -> str:
    """Strip trailing dash from docket numbers like 'TM26030047-'."""
    return raw.strip().rstrip("-").strip()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_dockets_config(dockets_file: str = NJ_BPU_DOCKETS_FILE) -> List[Dict[str, Any]]:
    """
    Load active docket entries from nj_bpu_dockets.json.

    Each entry must have:
        case_id       — BPU case_id from the URL
        docket_number — docket number for MongoDB metadata
    Optional:
        description   — human-readable label (for logging)
        active        — set false to skip without removing (default: true)
    """
    if not os.path.isfile(dockets_file):
        raise FileNotFoundError(
            f"Dockets config file not found: {dockets_file}\n"
            f"Expected at: {NJ_BPU_DOCKETS_FILE}"
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


def _batch_filter_existing(
    collection, doc_ids: List[str]
) -> List[str]:
    """Return subset of doc_ids that are NOT yet in MongoDB."""
    if not doc_ids:
        return []
    existing = set()
    try:
        cursor = collection.find(
            {"metadata.document_id": {"$in": doc_ids}},
            {"metadata.document_id": 1},
        )
        for doc in cursor:
            existing.add(doc.get("metadata", {}).get("document_id", ""))
    except Exception as e:
        logger.warning(f"MongoDB batch dedup failed: {e}")
        return doc_ids  # process all on error
    new_ids = [d for d in doc_ids if d not in existing]
    skipped = len(doc_ids) - len(new_ids)
    if skipped:
        logger.info(f"Dedup: {skipped} already in DB, {len(new_ids)} new.")
    return new_ids


# ---------------------------------------------------------------------------
# HTML fetch (requests first, Playwright fallback)
# ---------------------------------------------------------------------------

def _build_proxy_dict(use_proxy: bool) -> Optional[Dict[str, str]]:
    if not use_proxy:
        return None
    host = os.getenv("NJ_BPU_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("NJ_BPU_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    user = os.getenv("NJ_BPU_PROXY_USER", DEFAULT_PROXY_USER)
    pwd = os.getenv("NJ_BPU_PROXY_PASS", DEFAULT_PROXY_PASS)
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def _fetch_case_html_requests(
    case_id: str, use_proxy: bool = True
) -> Optional[str]:
    """Try to fetch the case page with requests (fast path)."""
    url = f"{NJ_BPU_CASE_URL}?case_id={case_id}"
    proxies = _build_proxy_dict(use_proxy)
    try:
        resp = requests.get(
            url, headers=_REQUEST_HEADERS, proxies=proxies, timeout=30
        )
        resp.raise_for_status()
        html = resp.text
        if "gvDocuments" in html:
            logger.info(f"Case page fetched via requests ({len(html):,} chars).")
            return html
        logger.info("requests fetch succeeded but #gvDocuments missing — trying Playwright.")
        return None
    except Exception as e:
        logger.info(f"requests fetch failed: {e} — falling back to Playwright.")
        return None


def _fetch_case_html_playwright(
    case_id: str, headless: bool = True, use_proxy: bool = True
) -> Optional[str]:
    """Fetch the case page with Playwright (handles JS-rendered tabs)."""
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    url = f"{NJ_BPU_CASE_URL}?case_id={case_id}"
    launch_args = ["--no-sandbox", "--ignore-certificate-errors"]
    context_kwargs: Dict[str, Any] = {
        "ignore_https_errors": True,
        "user_agent": _REQUEST_HEADERS["User-Agent"],
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }

    if use_proxy:
        host = os.getenv("NJ_BPU_PROXY_HOST", DEFAULT_PROXY_HOST)
        port = os.getenv("NJ_BPU_PROXY_PORT", str(DEFAULT_PROXY_PORT))
        user = os.getenv("NJ_BPU_PROXY_USER", DEFAULT_PROXY_USER)
        pwd = os.getenv("NJ_BPU_PROXY_PASS", DEFAULT_PROXY_PASS)
        launch_args.append(f"--proxy-server=http://{host}:{port}")
        context_kwargs["proxy"] = {
            "server": f"http://{host}:{port}",
            "username": user,
            "password": pwd,
        }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=launch_args)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            try:
                page.goto(url, wait_until="load", timeout=120_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except PlaywrightTimeoutError:
                    pass

                # Click Documents tab if not already active
                try:
                    tab = page.query_selector("#ui-id-1, a[href='#tabs-1']")
                    if tab:
                        tab.click()
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

                try:
                    page.wait_for_selector("#gvDocuments", timeout=15_000)
                except PlaywrightTimeoutError:
                    logger.warning("Playwright: #gvDocuments not found after wait.")

                html = page.content()
                logger.info(f"Case page fetched via Playwright ({len(html):,} chars).")
                return html
            except PlaywrightTimeoutError as e:
                logger.error(f"Playwright timeout fetching case page: {e}")
                return None
            finally:
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Playwright launch failed: {e}")
        return None


def fetch_case_html(
    case_id: str, headless: bool = True, use_proxy: bool = True
) -> Optional[str]:
    """Fetch case page HTML — requests first, Playwright fallback."""
    html = _fetch_case_html_requests(case_id, use_proxy=use_proxy)
    if html:
        return html
    return _fetch_case_html_playwright(case_id, headless=headless, use_proxy=use_proxy)


# ---------------------------------------------------------------------------
# Parse documents table
# ---------------------------------------------------------------------------

def parse_documents(html: str) -> List[Dict[str, Any]]:
    """
    Parse the #gvDocuments table into a list of document dicts.
    Returns list ordered as they appear on the page (newest first).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="gvDocuments")
    if not table:
        logger.warning("Could not find #gvDocuments table in HTML.")
        return []

    documents: List[Dict[str, Any]] = []
    for row in table.find_all("tr"):
        checkbox = row.find("input", {"name": "document_id", "type": "checkbox"})
        if not checkbox:
            continue
        doc_id = checkbox.get("value", "").strip()
        if not doc_id:
            continue

        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        # Columns: checkbox | Docket # | Title (link) | Folder | Uploaded By | Description | Posted Date
        docket_number = _normalize_docket_number(cells[1].get_text(strip=True))

        title_cell = cells[2]
        link_tag = title_cell.find("a")
        title = link_tag.get_text(strip=True) if link_tag else title_cell.get_text(strip=True)
        doc_href = link_tag.get("href", "") if link_tag else ""
        if doc_href and not doc_href.startswith("http"):
            doc_href = f"{NJ_BPU_BASE}/{doc_href.lstrip('/')}"

        documents.append({
            "document_id": doc_id,
            "docket_number": docket_number,
            "title": title,
            "url": doc_href or f"{NJ_BPU_DOC_URL}?document_id={doc_id}",
            "folder": cells[3].get_text(strip=True),
            "uploaded_by": cells[4].get_text(strip=True),
            "description": cells[5].get_text(strip=True),
            "posted_date": _normalize_date(cells[6].get_text(strip=True)),
        })

    logger.info(f"Parsed {len(documents)} document(s) from #gvDocuments.")
    return documents


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
            return result
    except Exception as e:
        logger.debug(f"PyPDF2 extraction failed: {e}")

    try:
        import fitz  # type: ignore
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = [page.get_text() for page in doc]
        doc.close()
        result = "\n".join(parts).strip()
        if result:
            logger.info(f"  Text extracted via pymupdf ({len(result):,} chars).")
            return result
    except Exception as e:
        logger.debug(f"pymupdf extraction failed: {e}")

    return ""


def _download_document(
    doc_id: str,
    use_proxy: bool = True,
    session: Optional[requests.Session] = None,
) -> Optional[bytes]:
    """Download a BPU document by its document_id and return raw bytes."""
    url = f"{NJ_BPU_DOC_URL}?document_id={doc_id}"
    proxies = _build_proxy_dict(use_proxy)

    if session is None:
        session = requests.Session()
        session.headers.update(_REQUEST_HEADERS)

    try:
        resp = session.get(url, proxies=proxies, timeout=60)
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type.lower() or (
            content and content[:5] != b"%PDF-" and b"<html" in content[:200].lower()
        ):
            logger.warning(
                f"  doc {doc_id}: got HTML instead of PDF "
                f"(Content-Type={content_type!r}). Skipping."
            )
            return None
        logger.info(f"  Downloaded {len(content):,} bytes for doc {doc_id}.")
        return content
    except Exception as e:
        logger.warning(f"  Download failed for doc {doc_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# S3 upload (optional)
# ---------------------------------------------------------------------------

def _upload_to_s3(pdf_bytes: bytes, doc_id: str, title: str) -> str:
    """Upload PDF bytes to S3 and return the public URL. Returns '' on failure."""
    try:
        from aws_utils import build_docket_key, upload_bytes_to_s3
        slug = _safe_slug(title[:50])
        key = build_docket_key(f"nj_bpu_{doc_id}_{slug}.pdf")
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

def scrape_nj_bpu(
    case_id: str,
    docket_number: str,
    headless: bool = True,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
) -> Dict[str, Any]:
    """
    Scrape one NJ BPU docket: fetch page → dedup → download PDFs →
    extract text → analyze (tier1/2/3) → save to MongoDB.

    Args:
        case_id:        NJ BPU case_id, e.g. "2114202"
        docket_number:  Docket number for metadata, e.g. "TM26030047"
        headless:       Run Playwright in headless mode (fallback only)
        use_proxy:      Use residential proxy for requests
        test_mode:      Analyze but do NOT write to MongoDB or S3
        save_json:      Save parsed document list to JSON for debugging

    Returns:
        Dict with success, counts, and per-doc results.
    """
    from docket_entry_analyzer import analyze_docket_entry

    # Roll log file over to today's IST date (handles midnight crossover in long-lived workers)
    refresh_script_log(logger, _get_log_file)

    logger.info(
        f"=== NJ BPU scraper — case_id={case_id} "
        f"docket_number={docket_number} test_mode={test_mode} ==="
    )

    # Shared base context for all error emails in this run
    _run_ctx = {"case_id": case_id, "docket_number": docket_number}

    def _error_email(error_message: str, extra_ctx: Optional[dict] = None) -> None:
        send_error_email(
            script_name="nj_bpu_scraper",
            error_message=error_message,
            context={**_run_ctx, **(extra_ctx or {})},
        )

    # Step 1: MongoDB setup
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

    # Step 2: Fetch case page
    html = fetch_case_html(case_id, headless=headless, use_proxy=use_proxy)
    if not html:
        msg = f"Could not fetch case page for case_id={case_id}"
        logger.error(msg)
        _error_email(msg, {"step": "fetch_html", "url": f"{NJ_BPU_CASE_URL}?case_id={case_id}"})
        if mongo_client:
            mongo_client.close()
        return {"success": False, "error": msg, "processed": []}

    # Step 3: Parse documents
    documents = parse_documents(html)
    if not documents:
        logger.info("No documents found on case page.")
        if mongo_client:
            mongo_client.close()
        return {"success": True, "processed": [], "message": "No documents found."}

    for doc in documents:
        if not doc.get("docket_number"):
            doc["docket_number"] = docket_number

    if save_json:
        out_file = os.path.join(_THIS_DIR, f"nj_bpu_{case_id}_documents.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(documents, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved document list to {out_file}")

    # Step 4: Dedup check
    all_ids = [d["document_id"] for d in documents]
    if collection is not None:
        new_ids_set = set(_batch_filter_existing(collection, all_ids))
    else:
        new_ids_set = set(all_ids)

    new_documents = [d for d in documents if d["document_id"] in new_ids_set]
    if not new_documents:
        logger.info("All documents already in the database.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "All documents already in DB.",
            "total_found": len(documents),
            "new": 0,
        }

    # Process oldest first (site shows newest at top)
    new_documents = list(reversed(new_documents))
    logger.info(
        f"{len(new_documents)} new document(s) to process "
        f"(out of {len(documents)} total)."
    )

    # Step 5: Download → extract → analyze
    processed = []
    download_session = requests.Session()
    download_session.headers.update(_REQUEST_HEADERS)

    for i, doc in enumerate(new_documents):
        doc_id = doc["document_id"]
        title = doc["title"]
        posted_date = doc["posted_date"]
        folder = doc["folder"]
        uploaded_by = doc["uploaded_by"]
        description = doc["description"]
        doc_url = doc["url"]
        doc_docket_number = doc.get("docket_number") or docket_number

        logger.info(
            f"[{i+1}/{len(new_documents)}] doc_id={doc_id} | "
            f"date={posted_date} | {title[:70]}"
        )

        # Shared per-document context for error emails
        _doc_ctx = {"doc_id": doc_id, "title": title[:120], "posted_date": posted_date}

        # 5a: Download PDF
        pdf_bytes = _download_document(doc_id, use_proxy=use_proxy, session=download_session)
        if not pdf_bytes:
            msg = f"PDF download failed for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "download_pdf", "doc_url": doc_url})
            processed.append({"doc_id": doc_id, "title": title, "status": "download_failed"})
            continue

        # 5b: Extract text
        extracted_text = _extract_text_from_pdf_bytes(pdf_bytes)
        if not extracted_text.strip():
            msg = f"No text extracted from PDF for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "extract_text"})
            processed.append({"doc_id": doc_id, "title": title, "status": "no_text_extracted"})
            continue
        logger.info(f"  Extracted {len(extracted_text):,} chars.")

        # 5c: Upload to S3 (skip in test_mode)
        s3_url = ""
        if not test_mode:
            s3_url = _upload_to_s3(pdf_bytes, doc_id, title)

        # 5d: Build metadata
        metadata = {
            "docket_type": DOCKET_TYPE,
            "docket_number": doc_docket_number,
            "document_id": doc_id,
            "date": posted_date,
            "document_type": folder,
            "on_behalf_of": uploaded_by,
            "additional_info": description[:200] if description else title[:200],
            "url": s3_url or doc_url,
        }

        # 5e: Analyze (tier 1/2/3 + MongoDB insert)
        logger.info(f"  Analyzing — folder={folder} uploaded_by={uploaded_by[:50]}")
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
                _error_email(msg, {**_doc_ctx, "step": "docket_analysis", "analysis_error": result["error"]})
                processed.append({"doc_id": doc_id, "title": title, "status": "analysis_error", "error": result["error"]})
                # Use a flag instead of continue so time.sleep(2) still runs
                analysis_failed = True
            else:
                logger.info(f"  → status={status}")
                analysis_failed = False

            # 5f & 5g: Only proceed if analysis succeeded
            intake_note = None
            email_html = None
            if not analysis_failed:
                if status == "new_analysis":
                    comprehensive_summary = result.get("comprehensive_summary") or ""
                    intake_note = generate_intake_note(comprehensive_summary)

                    if intake_note is None:
                        msg = f"GPT intake note generation failed for doc_id={doc_id}"
                        logger.warning(f"  {msg}")
                        _error_email(msg, {**_doc_ctx, "step": "gpt_intake_note"})
                    else:
                        # 5g: Build HTML email from intake note + tier2/tier3 analysis
                        document_url = metadata.get("url") or metadata.get("document_id") or ""
                        base_html = render_intake_card(intake_note, document_url)
                        email_html = render_email_html(
                            tier2_response=((result.get("tier2_analysis") or {}).get("response") or ""),
                            tier3_response=((result.get("tier3_risk_assessment") or {}).get("response") or ""),
                            base_html=base_html,
                            metadata=metadata,
                        )
                        logger.info("  Email HTML rendered.")

                        # 5h: Send email via docket_email_service (org-aware routing)
                        target_company_name = (result.get("metadata") or {}).get("target_company_name", "")
                        additional_info = metadata.get("additional_info", "")
                        document_type = metadata.get("document_type", "")
                        subject = (
                            f"{target_company_name} : NJ - {doc_docket_number}"
                            f": {additional_info} - {document_type}"
                        )
                        send_docket_email(
                            subject=subject,
                            email_html=email_html,
                            doc_id=doc_id,
                            docket_number=doc_docket_number,
                            docket_type=DOCKET_TYPE,
                            deal_id=result.get("deal_id"),
                        )
                else:
                    logger.info(f"  Skipping intake note and email HTML — status={status}")

                processed.append({
                    "doc_id": doc_id,
                    "title": title,
                    "posted_date": posted_date,
                    "status": status,
                    "s3_url": s3_url,
                    "intake_note": intake_note,
                    "email_html": email_html,
                })
        except Exception as e:
            msg = f"Docket analysis exception for doc_id={doc_id}: {e}"
            logger.error(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "docket_analysis"})
            processed.append({"doc_id": doc_id, "title": title, "status": "analysis_error", "error": str(e)})

        time.sleep(2)

    if mongo_client:
        mongo_client.close()

    success_count = sum(
        1 for p in processed
        if p["status"] not in ("download_failed", "no_text_extracted", "analysis_error")
    )
    logger.info(f"Finished. {success_count}/{len(new_documents)} new document(s) analyzed.")

    return {
        "success": True,
        "case_id": case_id,
        "docket_number": docket_number,
        "total_found": len(documents),
        "new": len(new_documents),
        "analyzed": success_count,
        "processed": processed,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Multi-docket runner (reads nj_bpu_dockets.json)
# ---------------------------------------------------------------------------

def scrape_all_nj_bpu(
    dockets_file: str = NJ_BPU_DOCKETS_FILE,
    headless: bool = True,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
) -> Dict[str, Any]:
    """
    Read nj_bpu_dockets.json and run scrape_nj_bpu() for every active entry.

    To add a new docket: just add it to nj_bpu_dockets.json with active=true.
    No code changes required.
    """
    try:
        dockets = load_dockets_config(dockets_file)
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}

    if not dockets:
        logger.info("No active dockets found in config file.")
        return {"success": True, "dockets_processed": 0, "total_analyzed": 0, "results": []}

    all_results = []
    for entry in dockets:
        case_id = entry.get("case_id", "").strip()
        docket_number = entry.get("docket_number", "").strip()
        description = entry.get("description", "")

        if not case_id or not docket_number:
            logger.warning(f"Skipping invalid config entry (missing case_id or docket_number): {entry}")
            continue

        logger.info(
            f"\n{'='*60}\n"
            f"Docket: {docket_number} | case_id: {case_id}\n"
            f"{description}\n"
            f"{'='*60}"
        )

        result = scrape_nj_bpu(
            case_id=case_id,
            docket_number=docket_number,
            headless=headless,
            use_proxy=use_proxy,
            test_mode=test_mode,
            save_json=save_json,
        )
        all_results.append(result)

        if len(dockets) > 1:
            time.sleep(5)  # polite delay between dockets

    total_analyzed = sum(r.get("analyzed", 0) for r in all_results)
    logger.info(
        f"\nAll done. {len(all_results)} docket(s) processed, "
        f"{total_analyzed} document(s) analyzed in total."
    )

    return {
        "success": True,
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
        description="NJ BPU Docket Scraper — downloads PDFs and runs tier1/2/3 analysis"
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all", action="store_true",
        help="Run all active dockets from nj_bpu_dockets.json",
    )
    mode.add_argument(
        "--case-id",
        help="Single case_id to process, e.g. 2114202",
    )

    parser.add_argument(
        "--docket-number",
        help="Docket number (required when --case-id is used), e.g. TM26030047",
    )
    parser.add_argument(
        "--dockets-file", default=NJ_BPU_DOCKETS_FILE,
        help=f"Path to dockets config JSON (default: {NJ_BPU_DOCKETS_FILE})",
    )
    parser.add_argument(
        "--no-headless", action="store_true", default=False,
        help="Show browser window (Playwright fallback only)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=False,
        help="Disable residential proxy (use direct connection)",
    )
    parser.add_argument(
        "--test-mode", action="store_true", default=False,
        help="Analyze but do NOT write to MongoDB or S3",
    )
    parser.add_argument(
        "--save-json", action="store_true", default=False,
        help="Save parsed document list to JSON for debugging",
    )

    args = parser.parse_args()

    headless = not args.no_headless
    use_proxy = not args.no_proxy

    if args.all:
        result = scrape_all_nj_bpu(
            dockets_file=args.dockets_file,
            headless=headless,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
        )
    else:
        if not args.docket_number:
            parser.error("--docket-number is required when using --case-id")
        result = scrape_nj_bpu(
            case_id=args.case_id,
            docket_number=args.docket_number,
            headless=headless,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
