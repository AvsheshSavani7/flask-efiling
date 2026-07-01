"""
New Jersey BPU (Board of Public Utilities) Docket Scraper
==========================================================
Fetches a BPU case summary page, parses the documents table, downloads PDFs,
extracts text, and runs tier1/2/3 analysis via docket_entry_analyzer.

Case URL pattern:
    https://publicaccess.bpu.state.nj.us/CaseSummary.aspx?case_id=<CASE_ID>

Document download URL pattern:
    https://publicaccess.bpu.state.nj.us/DocumentHandler.ashx?document_id=<DOC_ID>

Install:
    pip install requests beautifulsoup4 PyPDF2 playwright playwright-stealth
    playwright install chromium

Run:
    python nj_bpu_scraper.py --case-id 2114202 --docket-number TM26030047
    python nj_bpu_scraper.py --case-id 2114202 --docket-number TM26030047 --test-mode
    python nj_bpu_scraper.py --case-id 2114202 --docket-number TM26030047 --save-json
    python nj_bpu_scraper.py --case-id 2114202 --docket-number TM26030047 --no-proxy
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

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logger = logging.getLogger("nj_bpu_scraper")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
logger.propagate = False

NJ_BPU_BASE = "https://publicaccess.bpu.state.nj.us"
NJ_BPU_CASE_URL = f"{NJ_BPU_BASE}/CaseSummary.aspx"
NJ_BPU_DOC_URL = f"{NJ_BPU_BASE}/DocumentHandler.ashx"

DOCKET_TYPE = "nj-bpu"
COLLECTION_NAME = "docket"

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


def _already_in_db(collection, doc_id: str) -> bool:
    return collection.find_one({"metadata.document_id": doc_id}) is not None


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
            logger.info(
                f"Case page fetched via requests ({len(html):,} chars)."
            )
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
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
        },
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

                # If the Documents tab is not active, click it
                try:
                    tab = page.query_selector("#ui-id-1, a[href='#tabs-1']")
                    if tab:
                        tab.click()
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Wait for the documents table to appear
                try:
                    page.wait_for_selector("#gvDocuments", timeout=15_000)
                except PlaywrightTimeoutError:
                    logger.warning("Playwright: #gvDocuments not found after wait.")

                html = page.content()
                logger.info(
                    f"Case page fetched via Playwright ({len(html):,} chars)."
                )
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

    Returns list ordered as they appear on the page (newest first on the site).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="gvDocuments")
    if not table:
        logger.warning("Could not find #gvDocuments table in HTML.")
        return []

    documents: List[Dict[str, Any]] = []
    rows = table.find_all("tr")

    for row in rows:
        checkbox = row.find("input", {"name": "document_id", "type": "checkbox"})
        if not checkbox:
            continue

        doc_id = checkbox.get("value", "").strip()
        if not doc_id:
            continue

        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        # Columns: checkbox | Docket # | Document Title (link) | Folder | Uploaded By | Description | Posted Date
        docket_num_raw = cells[1].get_text(strip=True)
        docket_number = _normalize_docket_number(docket_num_raw)

        title_cell = cells[2]
        link_tag = title_cell.find("a")
        title = link_tag.get_text(strip=True) if link_tag else title_cell.get_text(strip=True)
        doc_href = link_tag.get("href", "") if link_tag else ""
        if doc_href and not doc_href.startswith("http"):
            doc_href = f"{NJ_BPU_BASE}/{doc_href.lstrip('/')}"

        folder = cells[3].get_text(strip=True)
        uploaded_by = cells[4].get_text(strip=True)
        description = cells[5].get_text(strip=True)
        posted_date_raw = cells[6].get_text(strip=True)
        posted_date = _normalize_date(posted_date_raw)

        documents.append({
            "document_id": doc_id,
            "docket_number": docket_number,
            "title": title,
            "url": doc_href or f"{NJ_BPU_DOC_URL}?document_id={doc_id}",
            "folder": folder,
            "uploaded_by": uploaded_by,
            "description": description,
            "posted_date": posted_date,
        })

    logger.info(f"Parsed {len(documents)} document(s) from #gvDocuments.")
    return documents


# ---------------------------------------------------------------------------
# PDF download and text extraction
# ---------------------------------------------------------------------------

def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using PyPDF2, with pymupdf fallback."""
    if not pdf_bytes:
        return ""

    # PyPDF2 first
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

    # pymupdf fallback
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

        # Detect content type — guard against HTML error pages
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
# Main scraper
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
    Scrape NJ BPU docket, download PDFs, run tier1/2/3 analysis, save to MongoDB.

    Args:
        case_id:        NJ BPU case_id (e.g. "2114202")
        docket_number:  Docket number for metadata (e.g. "TM26030047")
        headless:       Run Playwright in headless mode (fallback only)
        use_proxy:      Use residential proxy for requests
        test_mode:      Analyze but do NOT write to MongoDB or S3
        save_json:      Save parsed document list to JSON for debugging

    Returns:
        Dict with success, processed count, and per-doc results.
    """
    from docket_entry_analyzer import analyze_docket_entry

    logger.info(
        f"Starting NJ BPU scraper — case_id={case_id}, "
        f"docket_number={docket_number}, test_mode={test_mode}"
    )

    # --- Step 1: MongoDB setup ---
    collection = None
    mongo_client = None
    if not test_mode:
        try:
            collection, mongo_client = _get_mongo_collection()
            logger.info("MongoDB connection established.")
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")
            return {"success": False, "error": str(e), "processed": []}

    # --- Step 2: Fetch case page ---
    html = fetch_case_html(case_id, headless=headless, use_proxy=use_proxy)
    if not html:
        msg = f"Could not fetch case page for case_id={case_id}"
        logger.error(msg)
        if mongo_client:
            mongo_client.close()
        return {"success": False, "error": msg, "processed": []}

    # --- Step 3: Parse documents ---
    documents = parse_documents(html)
    if not documents:
        logger.info("No documents found on case page.")
        if mongo_client:
            mongo_client.close()
        return {"success": True, "processed": [], "message": "No documents found."}

    # Normalize docket_number from metadata if parsed value is more specific
    for doc in documents:
        if not doc.get("docket_number"):
            doc["docket_number"] = docket_number

    if save_json:
        out_file = f"nj_bpu_{case_id}_documents.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(documents, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved document list to {out_file}")

    # --- Step 4: Dedup check ---
    all_ids = [d["document_id"] for d in documents]
    if collection is not None:
        new_ids_set = set(_batch_filter_existing(collection, all_ids))
    else:
        new_ids_set = set(all_ids)

    new_documents = [d for d in documents if d["document_id"] in new_ids_set]
    if not new_documents:
        logger.info("All documents are already in the database. Nothing to do.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "All documents already in DB.",
            "total_found": len(documents),
            "new": 0,
        }

    # Process oldest first (reverse order — table shows newest at top)
    new_documents = list(reversed(new_documents))
    logger.info(
        f"{len(new_documents)} new document(s) to process "
        f"(out of {len(documents)} total)."
    )

    # --- Step 5: Process each new document ---
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

        # 5a: Download PDF
        pdf_bytes = _download_document(
            doc_id, use_proxy=use_proxy, session=download_session
        )
        if not pdf_bytes:
            logger.warning(f"  Skipping doc {doc_id} — no content downloaded.")
            processed.append({
                "doc_id": doc_id,
                "title": title,
                "status": "download_failed",
            })
            continue

        # 5b: Extract text
        extracted_text = _extract_text_from_pdf_bytes(pdf_bytes)
        if not extracted_text.strip():
            logger.warning(
                f"  doc {doc_id}: no text extracted from PDF."
            )
            processed.append({
                "doc_id": doc_id,
                "title": title,
                "status": "no_text_extracted",
            })
            continue
        logger.info(f"  Extracted {len(extracted_text):,} chars.")

        # 5c: Upload to S3 (skip in test_mode)
        s3_url = ""
        if not test_mode:
            s3_url = _upload_to_s3(pdf_bytes, doc_id, title)

        final_url = s3_url or doc_url

        # 5d: Build metadata
        metadata = {
            "docket_type": DOCKET_TYPE,
            "docket_number": doc_docket_number,
            "document_id": doc_id,
            "date": posted_date,
            "document_type": folder,          # BPU uses folder as doc type
            "on_behalf_of": uploaded_by,
            "additional_info": description[:200] if description else title[:200],
            "url": final_url,
        }

        # 5e: Analyze (tier 1/2/3 + MongoDB insert)
        logger.info(
            f"  Analyzing doc {doc_id} — "
            f"folder={folder} uploaded_by={uploaded_by[:50]}"
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
                logger.warning(f"  Analysis error: {result['error']}")
            else:
                logger.info(f"  → status={status}")

            processed.append({
                "doc_id": doc_id,
                "title": title,
                "posted_date": posted_date,
                "status": status,
                "s3_url": s3_url,
            })
        except Exception as e:
            logger.error(f"  analyze_docket_entry exception for doc {doc_id}: {e}")
            processed.append({
                "doc_id": doc_id,
                "title": title,
                "status": "analysis_error",
                "error": str(e),
            })

        time.sleep(2)  # polite delay between documents

    if mongo_client:
        mongo_client.close()

    success_count = sum(1 for p in processed if p["status"] not in ("download_failed", "no_text_extracted", "analysis_error"))
    logger.info(
        f"Finished. {success_count}/{len(new_documents)} new document(s) analyzed."
    )

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
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NJ BPU Docket Scraper — downloads PDFs and runs tier1/2/3 analysis"
    )
    parser.add_argument(
        "--case-id", required=True,
        help="NJ BPU case_id, e.g. 2114202",
    )
    parser.add_argument(
        "--docket-number", required=True,
        help="Docket number for metadata, e.g. TM26030047",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Run Playwright in headless mode (default: true)",
    )
    parser.add_argument(
        "--no-headless", action="store_false", dest="headless",
        help="Run Playwright with browser window visible",
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

    result = scrape_nj_bpu(
        case_id=args.case_id,
        docket_number=args.docket_number,
        headless=args.headless,
        use_proxy=not args.no_proxy,
        test_mode=args.test_mode,
        save_json=args.save_json,
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
