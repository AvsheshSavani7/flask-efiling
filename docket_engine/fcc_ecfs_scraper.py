"""
FCC ECFS Docket Scraper
=======================
Fetches FCC Electronic Comment Filing System (ECFS) RSS feeds,
parses new filing items, downloads PDF documents, extracts text,
and runs tier1/2/3 analysis via docket_entry_analyzer.

Dockets to follow are configured in fcc_ecfs_dockets.json (same folder).
To add a new docket, just add an entry to that file — no code changes needed.

Uses a residential proxy and SSL verification disabled (same pattern as fcc_rss_to_json.py)
to reliably reach the FCC ECFS API. Pass --no-proxy to disable the proxy.

RSS URL pattern:
    https://ecfsapi.fcc.gov/filings?q=...&limit=25&sort=date_disseminated,DESC&type=rss

FCC JSON API (filing detail):
    https://ecfsapi.fcc.gov/filing/<filing_id>

Document download URL pattern:
    https://www.fcc.gov/ecfs/document/<filing_id>/<index>

Run all active dockets from fcc_ecfs_dockets.json:
    python docket_engine/fcc_ecfs_scraper.py --all

Run a single docket:
    python docket_engine/fcc_ecfs_scraper.py --docket-number 26-134

Other flags (apply to both modes):
    --test-mode    Analyze but skip MongoDB/S3 writes
    --save-json    Save parsed filing list to JSON for debugging
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests
import urllib3
from dotenv import load_dotenv
from pymongo import MongoClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from docket_engine.intake_analyzer import generate_intake_note
from docket_engine.email_renderer import render_intake_card, render_email_html
from docket_engine.docket_email_service import send_docket_email
from log_utils import ensure_script_logger, refresh_script_log
from error_email_service import send_error_email

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger, _get_log_file = ensure_script_logger("fcc_ecfs_scraper")
LOG_FILE = _get_log_file()

FCC_BASE = "https://www.fcc.gov"
FCC_API_BASE = "https://ecfsapi.fcc.gov"

DOCKET_TYPE = "fcc-ecfs"
COLLECTION_NAME = "docket"

FCC_ECFS_DOCKETS_FILE = os.path.join(_THIS_DIR, "fcc_ecfs_dockets.json")

# DC namespace used in the RSS feed
_DC_NS = "http://purl.org/dc/elements/1.1/"

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_PROXY_HOST = "108.59.242.138"
DEFAULT_PROXY_PORT = 46885
DEFAULT_PROXY_USER = "GSenAgrfKhuNWkd"
DEFAULT_PROXY_PASS = "8lmVa5yl0pKp9MI"


def _build_proxy_dict(use_proxy: bool) -> Optional[Dict[str, str]]:
    if not use_proxy:
        return None
    host = os.getenv("FCC_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("FCC_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    user = os.getenv("FCC_PROXY_USER", DEFAULT_PROXY_USER)
    pwd = os.getenv("FCC_PROXY_PASS", DEFAULT_PROXY_PASS)
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


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
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    # ISO 8601 with time component
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass
    return raw


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _extract_filing_id(filing_url: str) -> str:
    """Extract the numeric filing ID from a FCC ECFS filing URL."""
    match = re.search(r"/filing/(\w+)$", filing_url)
    return match.group(1) if match else filing_url


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_dockets_config(dockets_file: str = FCC_ECFS_DOCKETS_FILE) -> List[Dict[str, Any]]:
    """
    Load active docket entries from fcc_ecfs_dockets.json.

    Each entry must have:
        rss_url       — ECFS RSS feed URL for the proceeding
        docket_number — docket number for MongoDB metadata (e.g. "26-134")
    Optional:
        description   — human-readable label (for logging)
        active        — set false to skip without removing (default: true)
    """
    if not os.path.isfile(dockets_file):
        raise FileNotFoundError(
            f"Dockets config file not found: {dockets_file}\n"
            f"Expected at: {FCC_ECFS_DOCKETS_FILE}"
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


def _batch_filter_existing(collection, filing_urls: List[str]) -> List[str]:
    """Return subset of filing_urls that are NOT yet in MongoDB."""
    if not filing_urls:
        return []
    existing = set()
    try:
        cursor = collection.find(
            {"metadata.document_id": {"$in": filing_urls}},
            {"metadata.document_id": 1},
        )
        for doc in cursor:
            existing.add(doc.get("metadata", {}).get("document_id", ""))
    except Exception as e:
        logger.warning(f"MongoDB batch dedup failed: {e}")
        return filing_urls
    new_urls = [u for u in filing_urls if u not in existing]
    skipped = len(filing_urls) - len(new_urls)
    if skipped:
        logger.info(f"Dedup: {skipped} already in DB, {len(new_urls)} new.")
    return new_urls


# ---------------------------------------------------------------------------
# RSS feed fetch and parse
# ---------------------------------------------------------------------------

def fetch_rss_feed(rss_url: str, session: requests.Session) -> Optional[str]:
    """Fetch the ECFS RSS feed XML. Returns raw XML string or None on failure."""
    try:
        resp = session.get(rss_url, headers=_REQUEST_HEADERS, timeout=(5, 120), verify=False)
        resp.raise_for_status()
        logger.info(f"RSS feed fetched ({len(resp.text):,} chars).")
        return resp.text
    except Exception as e:
        logger.error(f"Failed to fetch RSS feed: {e}")
        return None


def parse_rss_items(xml_content: str) -> List[Dict[str, Any]]:
    """
    Parse ECFS RSS XML into a list of filing dicts (newest first, as returned).

    Each dict contains:
        filing_url, title, description, comment_type, filers, lawfirms,
        date_received, date_posted, dc_date
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        logger.error(f"RSS XML parse error: {e}")
        return []

    channel = root.find("channel")
    if channel is None:
        logger.error("No <channel> element found in RSS feed.")
        return []

    items = []
    for item in channel.findall("item"):
        def _text(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        def _dc(field: str) -> str:
            el = item.find(f"{{{_DC_NS}}}{field}")
            return (el.text or "").strip() if el is not None else ""

        filing_url = _text("link") or _text("guid")
        title = _text("title")
        description = _text("description")
        dc_date = _dc("date")

        # Parse structured fields from <description> HTML fragment
        desc_clean = description.replace("&#xD;", "").replace("<br/>", "\n")

        def _desc_field(label: str) -> str:
            m = re.search(rf"{re.escape(label)}:\s*([^\n<]+)", desc_clean, re.IGNORECASE)
            return m.group(1).strip() if m else ""

        comment_type = _desc_field("Comment Type")
        filers = _desc_field("Filers(s)")
        lawfirms = _desc_field("Lawfirm(s)")
        proceeding = _desc_field("Proceeding(s)")
        date_received = _desc_field("Date Received")
        date_posted = _desc_field("Date Posted")

        if not filing_url:
            logger.warning("RSS item has no link, skipping.")
            continue

        items.append({
            "filing_url": filing_url,
            "filing_id": _extract_filing_id(filing_url),
            "title": title,
            "description": description,
            "comment_type": comment_type,
            "filers": filers,
            "lawfirms": lawfirms,
            "proceeding": proceeding,
            "date_received": _normalize_date(date_received),
            "date_posted": _normalize_date(date_posted),
            "dc_date": dc_date,
        })

    logger.info(f"Parsed {len(items)} filing(s) from RSS feed.")
    return items


# ---------------------------------------------------------------------------
# FCC JSON API — filing detail and document list
# ---------------------------------------------------------------------------

def _get_filing_documents(filing_id: str, session: requests.Session) -> List[Dict[str, Any]]:
    """
    Fetch the document list for a filing via the FCC JSON API.
    Falls back to sequential URL probing if the API returns no documents.

    Returns list of dicts: [{"url": "...", "filename": "..."}]
    """
    api_url = f"{FCC_API_BASE}/filing/{filing_id}"
    try:
        resp = session.get(api_url, headers=_REQUEST_HEADERS, timeout=(5, 60), verify=False)
        if resp.status_code == 200:
            data = resp.json()
            # The filing may be nested under "filing" key or at root level
            filing_data = data.get("filing", data)
            raw_docs = filing_data.get("documents", [])
            if raw_docs:
                docs = []
                for i, doc in enumerate(raw_docs, 1):
                    url = (
                        doc.get("src")
                        or doc.get("url")
                        or f"{FCC_BASE}/ecfs/document/{filing_id}/{i}"
                    )
                    filename = doc.get("filename", f"document_{i}.pdf")
                    docs.append({"url": url, "filename": filename, "index": i})
                logger.info(f"  JSON API returned {len(docs)} document(s) for {filing_id}.")
                return docs
            # API succeeded but no documents listed — may be a text-only filing
            logger.info(f"  JSON API returned 0 documents for {filing_id} (may be text-only).")
            return []
    except Exception as e:
        logger.debug(f"  JSON API request failed for {filing_id}: {e}")

    # Fallback: probe sequential document URLs until 404
    logger.info(f"  Probing sequential document URLs for {filing_id}...")
    docs = []
    for i in range(1, 21):
        url = f"{FCC_BASE}/ecfs/document/{filing_id}/{i}"
        try:
            resp = session.head(url, headers=_REQUEST_HEADERS, timeout=(5, 30), verify=False, allow_redirects=True)
            if resp.status_code == 404:
                break
            if resp.status_code < 400:
                docs.append({"url": url, "filename": f"document_{i}.pdf", "index": i})
        except Exception:
            break
    logger.info(f"  Found {len(docs)} document(s) via URL probing for {filing_id}.")
    return docs


def _get_brief_comment_from_api(filing_id: str, session: requests.Session) -> str:
    """
    Attempt to retrieve the brief comment text for a text-only filing
    from the FCC JSON API.
    """
    api_url = f"{FCC_API_BASE}/filing/{filing_id}"
    try:
        resp = session.get(api_url, headers=_REQUEST_HEADERS, timeout=(5, 60), verify=False)
        if resp.status_code == 200:
            data = resp.json()
            filing_data = data.get("filing", data)
            # Try common field names for brief comment / inline text
            for field in ("text_data", "brief_comment", "comment", "description_of_filing"):
                val = filing_data.get(field, "")
                if val and isinstance(val, str) and len(val.strip()) > 5:
                    logger.info(f"  Brief comment found in API field '{field}' ({len(val)} chars).")
                    return val.strip()
    except Exception as e:
        logger.debug(f"  Could not retrieve brief comment from API for {filing_id}: {e}")
    return ""


# ---------------------------------------------------------------------------
# Document download and text extraction
# ---------------------------------------------------------------------------

def _download_document(url: str, session: requests.Session) -> Optional[bytes]:
    """Download a document by URL and return raw bytes."""
    try:
        resp = session.get(url, headers=_REQUEST_HEADERS, timeout=(5, 120), verify=False)
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type.lower() and b"%PDF" not in content[:10]:
            logger.warning(f"  Got HTML instead of document for {url}. Skipping.")
            return None
        logger.info(f"  Downloaded {len(content):,} bytes from {url}.")
        return content
    except Exception as e:
        logger.warning(f"  Download failed for {url}: {e}")
        return None


def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
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


def _extract_text_from_bytes(content: bytes, url: str) -> str:
    """Route to the correct text extractor based on content signature."""
    if not content:
        return ""
    if content.lstrip()[:5].startswith(b"%PDF"):
        return _extract_text_from_pdf(content)
    # DOCX (ZIP archive)
    if content[:4] == b"PK\x03\x04":
        try:
            import docx
            doc = docx.Document(BytesIO(content))
            text = "\n".join(p.text for p in doc.paragraphs if p.text)
            return text.strip()
        except Exception as e:
            logger.debug(f"DOCX extraction failed: {e}")
    # Plain text fallback
    try:
        decoded = content.decode("utf-8", errors="ignore")
        if decoded.strip():
            return decoded.strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def _upload_to_s3(pdf_bytes: bytes, filing_id: str, filename: str) -> str:
    """Upload document bytes to S3 and return the public URL. Returns '' on failure."""
    try:
        from aws_utils import build_docket_key, upload_bytes_to_s3
        slug = _safe_slug(filename[:50])
        key = build_docket_key(f"fcc_ecfs_{filing_id}_{slug}.pdf")
        result = upload_bytes_to_s3(pdf_bytes, key)
        url = result.get("url", "")
        logger.info(f"  S3 upload: {url}")
        return url
    except Exception as e:
        logger.warning(f"  S3 upload failed for filing {filing_id}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Single-docket scraper
# ---------------------------------------------------------------------------

def scrape_fcc_ecfs(
    rss_url: str,
    docket_number: str,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
) -> Dict[str, Any]:
    """
    Scrape one FCC ECFS docket: fetch RSS → dedup → download docs →
    extract text → analyze (tier1/2/3) → save to MongoDB.

    Args:
        rss_url:        ECFS RSS feed URL for the proceeding
        docket_number:  Docket number for metadata (e.g. "26-134")
        use_proxy:      Route requests through residential proxy
        test_mode:      Analyze but do NOT write to MongoDB or S3 or send emails
        save_json:      Save parsed filing list to JSON for debugging

    Returns:
        Dict with success, counts, and per-filing results.
    """
    from docket_entry_analyzer import analyze_docket_entry

    refresh_script_log(logger, _get_log_file)

    logger.info(
        f"=== FCC ECFS scraper — docket_number={docket_number} "
        f"use_proxy={use_proxy} test_mode={test_mode} ==="
    )

    _run_ctx = {"rss_url": rss_url, "docket_number": docket_number}

    def _error_email(error_message: str, extra_ctx: Optional[dict] = None) -> None:
        if test_mode:
            logger.info(f"  [test_mode] Suppressed error email: {error_message}")
            return
        send_error_email(
            script_name="fcc_ecfs_scraper",
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

    # Shared requests session
    session = requests.Session()
    session.headers.update(_REQUEST_HEADERS)
    session.verify = False
    proxies = _build_proxy_dict(use_proxy)
    if proxies:
        session.proxies.update(proxies)
        logger.info(f"Using proxy: {DEFAULT_PROXY_HOST}:{DEFAULT_PROXY_PORT}")

    # Step 2: Fetch and parse RSS feed
    xml_content = fetch_rss_feed(rss_url, session)
    if not xml_content:
        msg = f"Could not fetch RSS feed for docket {docket_number}"
        logger.error(msg)
        _error_email(msg, {"step": "fetch_rss"})
        if mongo_client:
            mongo_client.close()
        return {"success": False, "error": msg, "processed": []}

    filings = parse_rss_items(xml_content)
    if not filings:
        logger.info("No filings found in RSS feed.")
        if mongo_client:
            mongo_client.close()
        return {"success": True, "processed": [], "message": "No filings in RSS feed."}

    if save_json:
        out_file = os.path.join(_THIS_DIR, f"fcc_ecfs_{docket_number.replace('-', '_')}_filings.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(filings, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved filing list to {out_file}")

    # Step 3: Dedup against MongoDB
    all_urls = [f["filing_url"] for f in filings]
    if collection is not None:
        new_urls_set = set(_batch_filter_existing(collection, all_urls))
    else:
        new_urls_set = set(all_urls)

    new_filings = [f for f in filings if f["filing_url"] in new_urls_set]
    if not new_filings:
        logger.info("All filings already in the database.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "All filings already in DB.",
            "total_found": len(filings),
            "new": 0,
        }

    # Process oldest first (RSS is newest-first)
    new_filings = list(reversed(new_filings))
    logger.info(
        f"{len(new_filings)} new filing(s) to process "
        f"(out of {len(filings)} total)."
    )

    # Step 4: Process each new filing
    processed = []
    for i, filing in enumerate(new_filings):
        filing_url = filing["filing_url"]
        filing_id = filing["filing_id"]
        comment_type = filing["comment_type"] or "FILING"
        filers = filing["filers"]
        lawfirms = filing["lawfirms"]
        proceeding = filing["proceeding"]
        date_received = filing["date_received"]
        date_posted = filing["date_posted"]

        logger.info(
            f"[{i+1}/{len(new_filings)}] filing_id={filing_id} | "
            f"type={comment_type} | filers={filers[:60]} | date={date_received}"
        )

        _filing_ctx = {
            "filing_id": filing_id,
            "filing_url": filing_url,
            "comment_type": comment_type,
            "filers": filers[:120],
        }

        # 4a: Get document list from FCC JSON API
        doc_list = _get_filing_documents(filing_id, session)

        # 4b: Download and extract text from each document
        full_text = ""
        primary_bytes: Optional[bytes] = None
        primary_filename = ""

        for doc_info in doc_list:
            doc_url = doc_info["url"]
            doc_bytes = _download_document(doc_url, session)
            if not doc_bytes:
                continue
            doc_text = _extract_text_from_bytes(doc_bytes, doc_url)
            if doc_text:
                if full_text:
                    full_text += f"\n\n--- {doc_info['filename']} ---\n{doc_text}"
                else:
                    full_text = doc_text
                    primary_bytes = doc_bytes
                    primary_filename = doc_info["filename"]
                logger.info(
                    f"  Extracted {len(doc_text):,} chars from {doc_info['filename']}."
                )

        # 4c: If no documents, try brief comment from JSON API
        if not full_text.strip():
            logger.info(f"  No documents found — checking JSON API for brief comment.")
            full_text = _get_brief_comment_from_api(filing_id, session)

        # 4d: Skip if still no usable text (avoid wasting API tokens on metadata-only records)
        if not full_text.strip():
            logger.info(
                f"  No PDF text or brief comment available for filing {filing_id} — skipping."
            )
            processed.append({
                "filing_id": filing_id,
                "filing_url": filing_url,
                "comment_type": comment_type,
                "filers": filers,
                "date": date_received,
                "status": "skipped_no_text",
            })
            time.sleep(1)
            continue

        # 4e: Upload primary document to S3
        s3_url = ""
        if primary_bytes and not test_mode:
            s3_url = _upload_to_s3(primary_bytes, filing_id, primary_filename)

        # 4f: Build metadata dict
        metadata = {
            "docket_type": DOCKET_TYPE,
            "docket_number": docket_number,
            "document_id": filing_url,
            "date": date_received or date_posted,
            "document_type": comment_type,
            "on_behalf_of": filers,
            "additional_info": (proceeding or filing["title"])[:200],
            "url": s3_url or filing_url,
        }

        # 4g: Analyze (tier1/2/3 + MongoDB insert)
        logger.info(
            f"  Analyzing — type={comment_type} filers={filers[:60]}"
        )
        try:
            result = analyze_docket_entry(
                doc_number=filing_url,
                full_text=full_text,
                metadata=metadata,
                test_mode=test_mode,
            )
            status = result.get("status", "unknown")

            if result.get("error"):
                msg = f"Docket analysis error for filing {filing_id}: {result['error']}"
                logger.warning(f"  {msg}")
                _error_email(msg, {**_filing_ctx, "step": "docket_analysis", "analysis_error": result["error"]})
                processed.append({
                    "filing_id": filing_id,
                    "status": "analysis_error",
                    "error": result["error"],
                })
                time.sleep(2)
                continue

            logger.info(f"  → status={status}")

            # 4h: Intake note + email (new analyses only, skipped in test_mode)
            intake_note = None
            email_html = None
            if status == "new_analysis" and not test_mode:
                comprehensive_summary = result.get("comprehensive_summary") or ""
                intake_note = generate_intake_note(comprehensive_summary)

                if intake_note is None:
                    msg = f"Intake note generation failed for filing {filing_id}"
                    logger.warning(f"  {msg}")
                    _error_email(msg, {**_filing_ctx, "step": "gpt_intake_note"})
                else:
                    document_url = metadata.get("url") or filing_url
                    base_html = render_intake_card(intake_note, document_url)
                    email_html = render_email_html(
                        tier2_response=((result.get("tier2_analysis") or {}).get("response") or ""),
                        tier3_response=((result.get("tier3_risk_assessment") or {}).get("response") or ""),
                        base_html=base_html,
                        metadata=metadata,
                    )
                    logger.info("  Email HTML rendered.")

                    target_company_name = (result.get("metadata") or {}).get("target_company_name", "")
                    subject = (
                        f"{target_company_name} : FCC {docket_number}"
                        f": {filers[:60]} - {comment_type}"
                    )
                    send_docket_email(
                        subject=subject,
                        email_html=email_html,
                        doc_id=filing_id,
                        docket_number=docket_number,
                        docket_type=DOCKET_TYPE,
                        deal_id=result.get("deal_id"),
                    )
            elif status == "new_analysis" and test_mode:
                logger.info("  test_mode=True — skipping intake note and email.")
            else:
                logger.info(f"  Skipping intake note and email — status={status}")

            processed.append({
                "filing_id": filing_id,
                "filing_url": filing_url,
                "comment_type": comment_type,
                "filers": filers,
                "date": date_received,
                "status": status,
                "s3_url": s3_url,
                "intake_note": intake_note,
                "email_html": email_html,
            })

        except Exception as e:
            msg = f"Docket analysis exception for filing {filing_id}: {e}"
            logger.error(f"  {msg}")
            _error_email(msg, {**_filing_ctx, "step": "docket_analysis"})
            processed.append({
                "filing_id": filing_id,
                "status": "analysis_error",
                "error": str(e),
            })

        time.sleep(2)

    if mongo_client:
        mongo_client.close()

    success_count = sum(
        1 for p in processed
        if p.get("status") not in ("analysis_error", "skipped_no_text")
    )
    skipped_count = sum(1 for p in processed if p.get("status") == "skipped_no_text")
    logger.info(
        f"Finished. {success_count}/{len(new_filings)} filing(s) analyzed, "
        f"{skipped_count} skipped (no text)."
    )

    return {
        "success": True,
        "docket_number": docket_number,
        "total_found": len(filings),
        "new": len(new_filings),
        "analyzed": success_count,
        "processed": processed,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Multi-docket runner (reads fcc_ecfs_dockets.json)
# ---------------------------------------------------------------------------

def scrape_all_fcc_ecfs(
    dockets_file: str = FCC_ECFS_DOCKETS_FILE,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
) -> Dict[str, Any]:
    """
    Read fcc_ecfs_dockets.json and run scrape_fcc_ecfs() for every active entry.

    To add a new docket: just add it to fcc_ecfs_dockets.json with active=true.
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
        rss_url = entry.get("rss_url", "").strip()
        docket_number = entry.get("docket_number", "").strip()
        description = entry.get("description", "")

        if not rss_url or not docket_number:
            logger.warning(
                f"Skipping invalid config entry (missing rss_url or docket_number): {entry}"
            )
            continue

        logger.info(
            f"\n{'='*60}\n"
            f"Docket: {docket_number}\n"
            f"{description}\n"
            f"{'='*60}"
        )

        result = scrape_fcc_ecfs(
            rss_url=rss_url,
            docket_number=docket_number,
            use_proxy=use_proxy,
            test_mode=test_mode,
            save_json=save_json,
        )
        all_results.append(result)

        if len(dockets) > 1:
            time.sleep(5)

    total_analyzed = sum(r.get("analyzed", 0) for r in all_results)
    logger.info(
        f"\nAll done. {len(all_results)} docket(s) processed, "
        f"{total_analyzed} filing(s) analyzed in total."
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
        description="FCC ECFS Docket Scraper — downloads filings and runs tier1/2/3 analysis"
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all", action="store_true",
        help="Run all active dockets from fcc_ecfs_dockets.json",
    )
    mode.add_argument(
        "--docket-number",
        help="Single docket number to process, e.g. 26-134",
    )

    parser.add_argument(
        "--dockets-file", default=FCC_ECFS_DOCKETS_FILE,
        help=f"Path to dockets config JSON (default: {FCC_ECFS_DOCKETS_FILE})",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=False,
        help="Disable residential proxy (use direct connection)",
    )
    parser.add_argument(
        "--test-mode", action="store_true", default=False,
        help="Analyze but do NOT write to MongoDB or S3, and suppress emails",
    )
    parser.add_argument(
        "--save-json", action="store_true", default=False,
        help="Save parsed filing list to JSON for debugging",
    )

    args = parser.parse_args()
    use_proxy = not args.no_proxy

    if args.all:
        result = scrape_all_fcc_ecfs(
            dockets_file=args.dockets_file,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
        )
    else:
        # Find the rss_url for the given docket_number from the config
        try:
            dockets = load_dockets_config(args.dockets_file)
        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(1)

        match = next(
            (d for d in dockets if d.get("docket_number") == args.docket_number),
            None,
        )
        if not match:
            print(
                f"Error: docket_number '{args.docket_number}' not found in "
                f"{args.dockets_file}. Add it first."
            )
            sys.exit(1)

        result = scrape_fcc_ecfs(
            rss_url=match["rss_url"],
            docket_number=args.docket_number,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
