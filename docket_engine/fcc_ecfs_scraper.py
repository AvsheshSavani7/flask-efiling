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
import asyncio
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
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from pymongo import MongoClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from docket_engine.intake_analyzer import generate_intake_note
from docket_engine.email_renderer import render_intake_card, render_email_html
from docket_engine.docket_email_service import send_docket_email
from n8n_email_service import send_direct_email
from log_utils import ensure_script_logger, refresh_script_log
from error_email_service import send_error_email

TEST_MODE_EMAIL_RECIPIENT = "avshesh.savani@teqnodux.com"

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

# System Chrome is required to pass the FCC ECFS API's TLS/JA3 fingerprint check.
# Playwright's bundled Chromium has a different fingerprint that gets blocked.
_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _build_proxy_dict(use_proxy: bool) -> Optional[Dict[str, str]]:
    if not use_proxy:
        return None
    host = os.getenv("FCC_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("FCC_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    user = os.getenv("FCC_PROXY_USER", DEFAULT_PROXY_USER)
    pwd = os.getenv("FCC_PROXY_PASS", DEFAULT_PROXY_PASS)
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def _build_playwright_proxy(use_proxy: bool) -> Optional[Dict[str, str]]:
    if not use_proxy:
        return None
    host = os.getenv("FCC_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("FCC_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    user = os.getenv("FCC_PROXY_USER", DEFAULT_PROXY_USER)
    pwd = os.getenv("FCC_PROXY_PASS", DEFAULT_PROXY_PASS)
    return {
        "server": f"http://{host}:{port}",
        "username": user,
        "password": pwd,
    }


async def _fetch_url_chrome_async(
    url: str,
    use_proxy: bool,
    wait_for_selector: Optional[str] = None,
    wait_until: str = "domcontentloaded",
    use_page_content: bool = False,
) -> Optional[str]:
    """
    Fetch a URL using system Chrome via Playwright.

    The FCC ECFS API uses TLS/JA3 fingerprint detection (Akamai WAF) that
    silently drops connections from Python requests and curl. System Chrome
    presents an identical TLS fingerprint to the user's browser and passes.

    Args:
        wait_for_selector: CSS selector to wait for before capturing HTML.
                           Required for React pages (filing detail).
        wait_until:        Playwright navigation event to wait for.
        use_page_content:  If True, return page.content() (fully rendered DOM for
                           React apps). If False (default), return resp.text()
                           (raw HTTP response — needed for XML/RSS feeds).
    """
    proxy = _build_playwright_proxy(use_proxy)
    chrome_exe = _CHROME_PATH if os.path.isfile(_CHROME_PATH) else None
    launch_kwargs: Dict[str, Any] = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    if chrome_exe:
        # macOS: use system Chrome for authentic TLS fingerprint
        launch_kwargs["executable_path"] = chrome_exe
        launch_kwargs["channel"] = "chrome"
    # On Linux server: no channel set — Playwright uses its bundled Chromium
    if proxy:
        launch_kwargs["proxy"] = proxy

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            resp = await page.goto(url, wait_until=wait_until, timeout=30_000)
            if resp is None or not resp.ok:
                status = resp.status if resp else "no response"
                logger.error(f"Chrome fetch failed for {url}: HTTP {status}")
                await browser.close()
                return None
            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=15_000)
                except Exception:
                    logger.warning(
                        f"  Selector '{wait_for_selector}' not found in {url} — "
                        "page may not have fully rendered."
                    )
            # use_page_content=True → fully rendered DOM (React apps)
            # use_page_content=False → raw HTTP response (XML/RSS feeds)
            body = await page.content() if use_page_content else await resp.text()
            await browser.close()
            return body
    except Exception as e:
        logger.error(f"Chrome fetch error for {url}: {e}")
        return None


def _fetch_url_chrome(
    url: str,
    use_proxy: bool = False,
    wait_for_selector: Optional[str] = None,
    wait_until: str = "domcontentloaded",
    use_page_content: bool = False,
) -> Optional[str]:
    """Sync wrapper around _fetch_url_chrome_async."""
    return asyncio.run(
        _fetch_url_chrome_async(
            url, use_proxy,
            wait_for_selector=wait_for_selector,
            wait_until=wait_until,
            use_page_content=use_page_content,
        )
    )


async def _download_bytes_chrome_async(url: str, use_proxy: bool) -> Optional[bytes]:
    """
    Download binary content (PDF etc.) via Chrome to bypass Akamai WAF.

    Strategy 1: Playwright request API (context.request.get) — uses Chrome's TLS
    fingerprint without launching a full page, ideal for direct file URLs.

    Strategy 2: page.goto + download event listener — for ECFS document URLs
    (www.fcc.gov/ecfs/document/<id>/<n>) that serve via JS-triggered downloads.

    Returns None if response is HTML (not a real document).
    """
    proxy = _build_playwright_proxy(use_proxy)
    chrome_exe = _CHROME_PATH if os.path.isfile(_CHROME_PATH) else None
    launch_kwargs: Dict[str, Any] = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    if chrome_exe:
        # macOS: use system Chrome for authentic TLS fingerprint
        launch_kwargs["executable_path"] = chrome_exe
        launch_kwargs["channel"] = "chrome"
    # On Linux server: no channel set — Playwright uses its bundled Chromium
    if proxy:
        launch_kwargs["proxy"] = proxy

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                ignore_https_errors=True,
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            # Strategy 1: request API — fastest, works for direct file URLs
            try:
                api_resp = await context.request.get(url, timeout=60_000)
                if api_resp.ok:
                    ct = api_resp.headers.get("content-type", "").lower()
                    body = await api_resp.body()
                    is_pdf = body and body.lstrip()[:5].startswith(b"%PDF")
                    is_doc = "application/pdf" in ct or "octet-stream" in ct or is_pdf
                    if is_doc and body:
                        logger.info(f"  Downloaded {len(body):,} bytes via request API from {url}.")
                        await browser.close()
                        return body
                    if "html" in ct:
                        logger.info(f"  Request API returned HTML for {url} — trying page download.")
            except Exception as e:
                logger.debug(f"  Request API failed for {url}: {e}")

            # Strategy 2: page.goto + response interception + download event
            page = await context.new_page()
            captured: List[bytes] = []

            async def on_response(response) -> None:
                if captured:
                    return
                if not response.ok:
                    return
                ct = response.headers.get("content-type", "").lower()
                if "pdf" in ct or "octet-stream" in ct or "download" in ct:
                    try:
                        body = await response.body()
                        if body and body.lstrip()[:5].startswith(b"%PDF"):
                            captured.append(body)
                            logger.info(f"  Response intercept: {len(body):,} bytes.")
                    except Exception:
                        pass

            async def on_download(download) -> None:
                if captured:
                    return
                try:
                    path = await download.path()
                    if path:
                        with open(path, "rb") as fh:
                            data = fh.read()
                        if data:
                            captured.append(data)
                            logger.info(f"  Download event: {len(data):,} bytes.")
                except Exception as dl_err:
                    if "canceled" not in str(dl_err).lower():
                        logger.warning(f"  Download event read error: {dl_err}")

            page.on("response", on_response)
            page.on("download", on_download)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                logger.debug(f"  page.goto failed for {url}: {e}")

            # Allow async responses / download events to settle
            await page.wait_for_timeout(3_000)

            if not captured:
                # Fallback: fetch the current URL from within the browser context
                # (inherits session cookies) — works when the doc loads via JS redirect
                try:
                    arr = await page.evaluate(
                        "() => fetch(window.location.href)"
                        ".then(r => r.arrayBuffer())"
                        ".then(b => Array.from(new Uint8Array(b)))"
                    )
                    if arr:
                        data = bytes(arr)
                        if data.lstrip()[:5].startswith(b"%PDF"):
                            captured.append(data)
                            logger.info(f"  JS fetch fallback: {len(data):,} bytes.")
                except Exception as e:
                    logger.debug(f"  JS fetch fallback failed: {e}")

            await browser.close()
            return captured[0] if captured else None

    except Exception as e:
        logger.warning(f"  Chrome download error for {url}: {e}")
        return None


def _download_bytes_chrome(url: str, use_proxy: bool = False) -> Optional[bytes]:
    """Sync wrapper around _download_bytes_chrome_async."""
    return asyncio.run(_download_bytes_chrome_async(url, use_proxy))


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


def _batch_filter_existing(collection, ids: List[str]) -> List[str]:
    """
    Level 1 dedup (RSS level): return IDs not yet in MongoDB.
    Checks metadata.document_id — catches brief comment filings (stored with
    filing_url as document_id) and also works for any URL-keyed record.
    """
    if not ids:
        return []
    existing = set()
    try:
        cursor = collection.find(
            {"metadata.document_id": {"$in": ids}},
            {"metadata.document_id": 1},
        )
        for doc in cursor:
            existing.add(doc.get("metadata", {}).get("document_id", ""))
    except Exception as e:
        logger.warning(f"MongoDB batch dedup (Level 1) failed: {e}")
        return ids
    new_ids = [i for i in ids if i not in existing]
    skipped = len(ids) - len(new_ids)
    if skipped:
        logger.info(f"Level 1 dedup: {skipped} filing(s) already in DB, {len(new_ids)} to process.")
    return new_ids


def _batch_filter_existing_docs(collection, doc_urls: List[str]) -> List[str]:
    """
    Level 2 dedup (document level): return doc_urls not yet in MongoDB.
    Checks metadata.document_id — called after parsing document links from the
    filing detail page to avoid re-downloading/re-analyzing existing PDFs.
    """
    if not doc_urls:
        return []
    existing = set()
    try:
        cursor = collection.find(
            {"metadata.document_id": {"$in": doc_urls}},
            {"metadata.document_id": 1},
        )
        for doc in cursor:
            existing.add(doc.get("metadata", {}).get("document_id", ""))
    except Exception as e:
        logger.warning(f"MongoDB batch dedup (Level 2) failed: {e}")
        return doc_urls
    new_urls = [u for u in doc_urls if u not in existing]
    skipped = len(doc_urls) - len(new_urls)
    if skipped:
        logger.info(f"Level 2 dedup: {skipped} document(s) already in DB, {len(new_urls)} new.")
    return new_urls


# ---------------------------------------------------------------------------
# RSS feed fetch and parse
# ---------------------------------------------------------------------------

def fetch_rss_feed(rss_url: str, use_proxy: bool = False) -> Optional[str]:
    """
    Fetch the ECFS RSS feed XML via system Chrome.

    The FCC ECFS API requires a browser TLS fingerprint (Akamai WAF); Python
    requests and curl are silently blocked. Returns raw XML string or None.
    """
    logger.info(f"Fetching RSS feed via Chrome: {rss_url}")
    xml_content = _fetch_url_chrome(rss_url, use_proxy=use_proxy)
    if xml_content:
        logger.info(f"RSS feed fetched ({len(xml_content):,} chars).")
    else:
        logger.error("Failed to fetch RSS feed via Chrome.")
    return xml_content


def _extract_xml(html_or_xml: str) -> str:
    """
    Playwright wraps plain XML/RSS responses in <html><body><pre>...</pre></body></html>
    when the server returns Content-Type: text/xml or application/rss+xml.
    Extract the raw XML from the <pre> block if present, otherwise return as-is.
    """
    pre_match = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", html_or_xml, re.IGNORECASE)
    if pre_match:
        return pre_match.group(1)
    stripped = html_or_xml.strip()
    if stripped.startswith("<?xml") or stripped.startswith("<rss"):
        return stripped
    return html_or_xml


def parse_rss_items(xml_content: str) -> List[Dict[str, Any]]:
    """
    Parse ECFS RSS XML into a list of filing dicts (newest first, as returned).

    Each dict contains:
        filing_url, title, description, comment_type, filers, lawfirms,
        date_received, date_posted, dc_date
    """
    xml_content = _extract_xml(xml_content)
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

def _fetch_filing_detail_html(filing_id: str, use_proxy: bool) -> Optional[str]:
    """
    Fetch the filing detail page via Chrome and return fully rendered HTML.
    Waits for React to render the page content before capturing.
    """
    url = f"{FCC_BASE}/ecfs/filing/{filing_id}"
    logger.info(f"  Fetching filing detail page: {url}")
    return _fetch_url_chrome(
        url,
        use_proxy=use_proxy,
        wait_for_selector="label#id_submission",
        wait_until="networkidle",
        use_page_content=True,
    )


def _parse_document_links(html: str) -> List[Dict[str, Any]]:
    """
    Parse document download links from the "Document Download" card in
    the filing detail page HTML. Works for any document host domain.
    """
    soup = BeautifulSoup(html, "html.parser")
    docs: List[Dict[str, Any]] = []
    seen_urls: set = set()

    for header in soup.find_all("div", class_="card-header"):
        if "Document Download" not in header.get_text():
            continue
        parent = header.find_parent()
        if not parent:
            continue
        list_group = parent.find("div", class_="list-group")
        if not list_group:
            continue
        for link in list_group.find_all("a", href=True):
            href = link.get("href", "").strip()
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            aria = link.get("aria-label", "")
            filename = (
                aria.replace("Download ", "").strip()
                or link.get_text(strip=True)
                or href.split("/")[-1]
                or "document.pdf"
            )
            docs.append({"url": href, "filename": filename, "index": len(docs) + 1})
        break  # Only process the first Document Download card

    return docs


def _parse_brief_comment(html: str) -> str:
    """
    Parse the Brief Comment field from the filing detail page HTML.
    Only present on text-only filings with no document attachments.
    """
    soup = BeautifulSoup(html, "html.parser")
    comment_label = soup.find("label", {"id": "comment"})
    if not comment_label:
        return ""
    parent_div = comment_label.find_parent("div", class_=lambda x: x and "form-group" in x)
    if parent_div:
        brief_label = parent_div.find("label", string=re.compile(r"Brief Comment", re.I))
        if brief_label:
            text = comment_label.get_text(strip=True)
            if text:
                logger.info(f"  Brief Comment found ({len(text)} chars).")
                return text
    return ""


# ---------------------------------------------------------------------------
# Document download and text extraction
# ---------------------------------------------------------------------------

def _download_document(url: str, use_proxy: bool = False) -> Optional[bytes]:
    """
    Download a document by URL and return raw bytes via Chrome.

    FCC document URLs (www.fcc.gov and docs.fcc.gov) are behind the same
    Akamai WAF as the API — requests/curl hang indefinitely. Chrome is used
    directly as the primary downloader.
    """
    content = _download_bytes_chrome(url, use_proxy=use_proxy)
    if content:
        logger.info(f"  Downloaded {len(content):,} bytes from {url}.")
    else:
        logger.warning(f"  Chrome download returned nothing for {url}.")
    return content


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
# Per-record analysis helper (shared by document loop and brief comment path)
# ---------------------------------------------------------------------------

def _process_single_record(
    doc_number: str,
    full_text: str,
    metadata: Dict[str, Any],
    filing_id: str,
    filers: str,
    comment_type: str,
    date_received: str,
    docket_number: str,
    s3_url: str,
    filing_ctx: Dict[str, Any],
    processed: List[Dict[str, Any]],
    test_mode: bool,
    analyze_docket_entry,
) -> None:
    """
    Run tier1/2/3 analysis for one record (a single document or a brief comment)
    and append the result to `processed`.

    MongoDB is always written (test_mode=False passed to analyze_docket_entry).
    In test_mode: email is sent to TEST_MODE_EMAIL_RECIPIENT only (not production routing).
    In production: email is sent via send_docket_email (org-aware routing).
    S3 upload is handled by the caller; not repeated here.
    """
    logger.info(f"  Analyzing — type={comment_type} filers={filers[:60]}")
    try:
        # Always save to MongoDB regardless of test_mode
        result = analyze_docket_entry(
            doc_number=doc_number,
            full_text=full_text,
            metadata=metadata,
            test_mode=False,
        )
        status = result.get("status", "unknown")

        if result.get("error"):
            msg = f"Docket analysis error for {doc_number}: {result['error']}"
            logger.warning(f"  {msg}")
            processed.append({
                "filing_id": filing_id,
                "doc_number": doc_number,
                "status": "analysis_error",
                "error": result["error"],
            })
            return

        logger.info(f"  → status={status}")

        intake_note = None
        email_html = None
        if status == "new_analysis":
            comprehensive_summary = result.get("comprehensive_summary") or ""
            intake_note = generate_intake_note(comprehensive_summary)

            if intake_note is None:
                logger.warning(f"  Intake note generation failed for {doc_number}")
            else:
                document_url = metadata.get("url") or doc_number
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

                if test_mode:
                    # Test mode: send to personal email only
                    logger.info(f"  test_mode=True — sending to {TEST_MODE_EMAIL_RECIPIENT} only.")
                    send_direct_email(
                        recipients=[TEST_MODE_EMAIL_RECIPIENT],
                        payload={"subject": f"[TEST] {subject}", "html": email_html},
                    )
                else:
                    # Production: org-aware routing
                    send_docket_email(
                        subject=subject,
                        email_html=email_html,
                        doc_id=filing_id,
                        docket_number=docket_number,
                        docket_type=DOCKET_TYPE,
                        deal_id=result.get("deal_id"),
                    )
        else:
            logger.info(f"  Skipping intake note and email — status={status}")

        processed.append({
            "filing_id": filing_id,
            "doc_number": doc_number,
            "comment_type": comment_type,
            "filers": filers,
            "date": date_received,
            "status": status,
            "s3_url": s3_url,
        })

    except Exception as e:
        msg = f"Docket analysis exception for {doc_number}: {e}"
        logger.error(f"  {msg}")
        processed.append({
            "filing_id": filing_id,
            "doc_number": doc_number,
            "status": "analysis_error",
            "error": str(e),
        })


# ---------------------------------------------------------------------------
# Single-docket scraper
# ---------------------------------------------------------------------------

def scrape_fcc_ecfs(
    rss_url: str,
    docket_number: str,
    use_proxy: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
    local_doc_dir: Optional[str] = None,
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
        local_doc_dir:  If set, save each downloaded document to
                        <local_doc_dir>/<filing_id>/<filename> on disk.

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

    # Step 1: MongoDB setup (always connect — writes happen in both test and production)
    collection = None
    mongo_client = None
    try:
        collection, mongo_client = _get_mongo_collection()
        logger.info("MongoDB connection established.")
    except Exception as e:
        msg = f"MongoDB connection failed: {e}"
        logger.error(msg)
        _error_email(msg, {"step": "mongodb_connect"})
        return {"success": False, "error": msg, "processed": []}

    # Step 2: Fetch and parse RSS feed (uses Chrome to bypass TLS fingerprinting)
    xml_content = fetch_rss_feed(rss_url, use_proxy=use_proxy)
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

    # Step 3: Level 1 dedup — filter filings by filing URL against metadata.document_id.
    # This catches brief comment filings (stored with filing_url as document_id).
    # Document filings are not caught here (their document_id = doc URL), so they
    # proceed to Level 2 dedup after the detail page is fetched.
    all_filing_urls = [f["filing_url"] for f in filings]
    not_in_db = set(_batch_filter_existing(collection, all_filing_urls))

    # A filing passes Level 1 if its filing_url is not yet a document_id in DB.
    # Document filings always pass (filing_url != their stored doc_urls).
    new_filings = [f for f in filings if f["filing_url"] in not_in_db]
    if not new_filings:
        logger.info("All filings already in the database (Level 1 dedup).")
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
    abort_run = False  # set True to stop all further filings on document failure
    for i, filing in enumerate(new_filings):
        if abort_run:
            break
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

        # 4a: Fetch filing detail page via Chrome (React app — waits for full render)
        detail_html = _fetch_filing_detail_html(filing_id, use_proxy=use_proxy)

        # 4b: Parse document download links from the detail page
        doc_list = _parse_document_links(detail_html) if detail_html else []
        logger.info(f"  Found {len(doc_list)} document link(s) on detail page.")

        # 4c: Per-document processing — each document is its own MongoDB record
        if doc_list:
            # Level 2 dedup — filter doc URLs already in MongoDB
            all_doc_urls = [d["url"] for d in doc_list]
            new_doc_urls = set(_batch_filter_existing_docs(collection, all_doc_urls))
            new_doc_list = [d for d in doc_list if d["url"] in new_doc_urls]
            if not new_doc_list:
                logger.info(f"  All {len(doc_list)} document(s) already in DB — skipping filing.")
                continue

            for doc_info in new_doc_list:
                doc_url = doc_info["url"]
                doc_filename = doc_info["filename"]
                logger.info(f"  [{doc_info['index']}/{len(doc_list)}] {doc_filename}")

                doc_bytes = _download_document(doc_url, use_proxy=use_proxy)
                if not doc_bytes:
                    msg = (
                        f"Document download failed for '{doc_filename}' "
                        f"in filing {filing_id} — aborting entire run."
                    )
                    logger.warning(f"  {msg}")
                    _error_email(msg, {**_filing_ctx, "step": "download_document", "doc_url": doc_url})
                    processed.append({
                        "filing_id": filing_id,
                        "doc_url": doc_url,
                        "filename": doc_filename,
                        "status": "run_aborted_download_failed",
                    })
                    abort_run = True
                    break  # stop remaining docs; outer loop will also break

                # Save document locally if local_doc_dir is configured
                if local_doc_dir:
                    import re as _re
                    from pathlib import Path as _Path
                    filing_dir = _Path(local_doc_dir) / filing_id
                    filing_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = _re.sub(r'[^\w\-_. ]', '_', doc_filename)[:120]
                    local_path = filing_dir / safe_name
                    local_path.write_bytes(doc_bytes)
                    logger.info(f"  Saved locally: {local_path} ({len(doc_bytes):,} bytes)")
                    doc_info["local_path"] = str(local_path)

                doc_text = _extract_text_from_bytes(doc_bytes, doc_url)
                if not doc_text.strip():
                    msg = (
                        f"Text extraction failed for '{doc_filename}' "
                        f"in filing {filing_id} — aborting entire run."
                    )
                    logger.warning(f"  {msg}")
                    _error_email(msg, {**_filing_ctx, "step": "extract_text", "doc_url": doc_url})
                    processed.append({
                        "filing_id": filing_id,
                        "doc_url": doc_url,
                        "filename": doc_filename,
                        "status": "run_aborted_no_text",
                    })
                    abort_run = True
                    break  # stop remaining docs; outer loop will also break

                logger.info(f"  Extracted {len(doc_text):,} chars.")

                # Upload to S3 (always — even in test_mode for debugging)
                s3_url = _upload_to_s3(doc_bytes, filing_id, doc_filename)

                # Build per-document metadata
                metadata = {
                    "docket_type": DOCKET_TYPE,
                    "docket_number": docket_number,
                    "date": date_received or date_posted,
                    "document_type": comment_type,
                    "on_behalf_of": filers,
                    "additional_info": (proceeding or filing["title"])[:200],
                    "url": s3_url or doc_url,
                }

                _process_single_record(
                    doc_number=doc_url,
                    full_text=doc_text,
                    metadata=metadata,
                    filing_id=filing_id,
                    filers=filers,
                    comment_type=comment_type,
                    date_received=date_received,
                    docket_number=docket_number,
                    s3_url=s3_url,
                    filing_ctx=_filing_ctx,
                    processed=processed,
                    test_mode=test_mode,
                    analyze_docket_entry=analyze_docket_entry,
                )
                time.sleep(2)

        else:
            # 4d: No documents — try brief comment (one record per filing)
            brief_comment = _parse_brief_comment(detail_html) if detail_html else ""

            if not brief_comment.strip():
                logger.info(
                    f"  No documents or brief comment for filing {filing_id} — skipping."
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

            logger.info(f"  Using brief comment ({len(brief_comment)} chars).")

            # Build metadata for brief comment record
            metadata = {
                "docket_type": DOCKET_TYPE,
                "docket_number": docket_number,
                "date": date_received or date_posted,
                "document_type": comment_type,
                "on_behalf_of": filers,
                "additional_info": (proceeding or filing["title"])[:200],
                "url": filing_url,
            }

            _process_single_record(
                doc_number=filing_url,
                full_text=brief_comment,
                metadata=metadata,
                filing_id=filing_id,
                filers=filers,
                comment_type=comment_type,
                date_received=date_received,
                docket_number=docket_number,
                s3_url="",
                filing_ctx=_filing_ctx,
                processed=processed,
                test_mode=test_mode,
                analyze_docket_entry=analyze_docket_entry,
            )
            time.sleep(2)

    if mongo_client:
        mongo_client.close()

    _failed_statuses = (
        "analysis_error", "skipped_no_text", "download_failed",
        "run_aborted_download_failed", "run_aborted_no_text",
    )
    success_count = sum(1 for p in processed if p.get("status") not in _failed_statuses)
    skipped_count = sum(1 for p in processed if p.get("status") == "skipped_no_text")
    aborted_count = sum(
        1 for p in processed
        if p.get("status") in ("run_aborted_download_failed", "run_aborted_no_text")
    )
    if abort_run:
        logger.warning(
            f"Run aborted early due to document failure. "
            f"{success_count} record(s) analyzed before abort."
        )
    else:
        logger.info(
            f"Finished. {success_count} record(s) analyzed across {len(new_filings)} filing(s), "
            f"{skipped_count} skipped (no text), {aborted_count} aborted (download/extract failure)."
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
    local_doc_dir: Optional[str] = None,
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
            local_doc_dir=local_doc_dir,
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
    parser.add_argument(
        "--local-doc-dir", default=None, metavar="DIR",
        help="Save each downloaded document to DIR/<filing_id>/<filename> on disk",
    )

    args = parser.parse_args()
    use_proxy = not args.no_proxy

    if args.all:
        result = scrape_all_fcc_ecfs(
            dockets_file=args.dockets_file,
            use_proxy=use_proxy,
            test_mode=args.test_mode,
            save_json=args.save_json,
            local_doc_dir=args.local_doc_dir,
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
            local_doc_dir=args.local_doc_dir,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
