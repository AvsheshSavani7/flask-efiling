"""
CPUC (California Public Utilities Commission) Docket Scraper
============================================================
Uses Playwright to open a proceeding Documents page, paginates through
the filings table until the cutoff date (last 15 days), downloads PDFs
from each document detail page, extracts text, and runs tier1/2/3
analysis via docket_entry_analyzer.

Dockets to follow are configured in cpuc_dockets.json (same folder).
CPUC APEX URLs do not embed the docket number in a simple path, so each
entry must include the full Documents (or Proceeding) URL plus
docket_number for MongoDB metadata.

Uniqueness: metadata.docket_type + metadata.docket_number + metadata.document_id
Cutoff: filings with Filing Date on/after (today - 15 days)

Run all active dockets from cpuc_dockets.json:
    python docket_engine/cpuc_scraper.py --all

Run a single docket:
    python docket_engine/cpuc_scraper.py --docket-number A2507016

Other flags:
    --test-mode    Analyze but skip MongoDB/S3 writes
    --save-json    Save scraped document list to JSON for debugging
    --no-headless  Show browser window
"""

from __future__ import annotations
from error_email_service import send_error_email
from log_utils import ensure_script_logger, refresh_script_log
from docket_engine.docket_email_service import send_docket_email
from docket_engine.email_renderer import render_intake_card, render_email_html
from docket_engine.intake_analyzer import generate_intake_note
from pymongo import MongoClient
from dotenv import load_dotenv
from bs4 import BeautifulSoup
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
from urllib.parse import urljoin, urlparse, parse_qs

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger, _get_log_file = ensure_script_logger("cpuc_scraper")
LOG_FILE = _get_log_file()

DOCKET_TYPE = "CPUC"
COLLECTION_NAME = "docket"
CPUC_DOCKETS_FILE = os.path.join(_THIS_DIR, "cpuc_dockets.json")
CUTOFF_DAYS = 15
CPUC_DOCS_BASE = "https://docs.cpuc.ca.gov"

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


def _cutoff_date(days: int = CUTOFF_DAYS) -> datetime:
    """Return midnight (local/naive date) for today - days."""
    today = datetime.now().date()
    return datetime.combine(today - timedelta(days=days), datetime.min.time())


def _parse_filing_date(value: str) -> Optional[datetime]:
    """Parse CPUC Filing Date strings like 'July 27, 2026' or '07/27/2026'."""
    if not value:
        return None
    raw = re.sub(r"\s+", " ", value.strip())
    for fmt in (
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _normalize_date(value: str) -> str:
    """Normalize a scraped date string to MM/DD/YYYY."""
    parsed = _parse_filing_date(value)
    if parsed:
        return parsed.strftime("%m/%d/%Y")
    return (value or "").strip()


def _sort_documents_oldest_first(
    documents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Sort filings oldest → newest by Filing Date / normalized date.
    Rows with unparseable dates sort last (stable among themselves).
    """
    def _sort_key(doc: Dict[str, Any]):
        parsed = _parse_filing_date(
            doc.get("filing_date_raw") or doc.get("date") or ""
        )
        if parsed is None:
            return (1, datetime.max)
        return (0, parsed)

    return sorted(documents, key=_sort_key)


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _extract_doc_id(url: str) -> str:
    """Extract stable DocID from a docs.cpuc.ca.gov SearchRes URL."""
    if not url:
        return ""
    try:
        qs = parse_qs(urlparse(url).query)
        doc_id = (qs.get("DocID") or qs.get("docid") or [""])[0]
        if doc_id:
            return str(doc_id).strip()
    except Exception:
        pass
    m = re.search(r"DocID=(\d+)", url, re.IGNORECASE)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_dockets_config(dockets_file: str = CPUC_DOCKETS_FILE) -> List[Dict[str, Any]]:
    """
    Load active docket entries from cpuc_dockets.json.

    Each entry must have:
        url            — full CPUC APEX Documents (or Proceeding) URL
        docket_number  — docket number for MongoDB metadata (e.g. A2507016)
    Optional:
        description    — human-readable label
        active         — set false to skip (default: true)
    """
    if not os.path.isfile(dockets_file):
        raise FileNotFoundError(
            f"Dockets config file not found: {dockets_file}\n"
            f"Expected at: {CPUC_DOCKETS_FILE}"
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
    collection,
    docket_number: str,
    doc_ids: List[str],
) -> List[str]:
    """
    Return subset of doc_ids that are NOT yet in MongoDB for this
    docket_type + docket_number.
    """
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
        for doc in cursor:
            existing.add(doc.get("metadata", {}).get("document_id", ""))
    except Exception as e:
        logger.warning(f"MongoDB batch dedup failed: {e}")
        return doc_ids
    new_ids = [d for d in doc_ids if d not in existing]
    skipped = len(doc_ids) - len(new_ids)
    if skipped:
        logger.info(
            f"Dedup ({DOCKET_TYPE}/{docket_number}): "
            f"{skipped} already in DB, {len(new_ids)} new."
        )
    return new_ids


# ---------------------------------------------------------------------------
# Detail-page PDF extraction (copied/adapted from old cpuc_table_extractor)
# ---------------------------------------------------------------------------

def extract_tables_from_url(url: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch a docs.cpuc.ca.gov SearchRes page and extract ResultTable rows.
    Skips Certificate of Service rows. Returns list of dicts with PDF_URL etc.
    """
    try:
        response = requests.get(url, headers=_REQUEST_HEADERS, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        result_table = soup.find("table", {"id": "ResultTable"})
        extracted_data: List[Dict[str, Any]] = []

        if not result_table:
            return extracted_data

        rows = result_table.find_all("tr")
        headers_row = rows[0] if rows else None
        if headers_row:
            header_cells = headers_row.find_all(["th", "td"])
            headers = [cell.get_text(strip=True) for cell in header_cells]
        else:
            headers = []

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            row_text = " ".join(
                [cell.get_text(strip=True) for cell in cells]
            ).upper()
            if "CERTIFICATE OF SERVICE" in row_text:
                continue

            row_dict: Dict[str, Any] = {}
            for idx, cell in enumerate(cells):
                key = headers[idx] if idx < len(headers) else f"Column_{idx}"
                value = cell.get_text(strip=True)

                if key == "Title" and " on " in value:
                    value = value.split(" on ")[0]

                if key == "Doc Links" or "PDF" in value.upper():
                    pdf_link = cell.find("a")
                    if pdf_link and pdf_link.get("href"):
                        href = pdf_link.get("href")
                        if href.startswith("http"):
                            value = href
                        else:
                            value = urljoin(url, href)
                    key = "PDF_URL"

                row_dict[key] = value

            if row_dict and any(v for v in row_dict.values() if v):
                extracted_data.append(row_dict)

        return extracted_data

    except requests.exceptions.RequestException as e:
        logger.warning(f"Error fetching detail URL {url}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error processing detail URL {url}: {e}")
        return None


def extract_text_from_pdf_url(pdf_url: str) -> Optional[str]:
    """Download a PDF from URL and extract text (PyPDF2, then pymupdf)."""
    try:
        response = requests.get(pdf_url, headers=_REQUEST_HEADERS, timeout=60)
        response.raise_for_status()
        content = response.content
        if content[:5] != b"%PDF-":
            content_type = response.headers.get("Content-Type", "")
            if "html" in content_type.lower() or b"<html" in content[:200].lower():
                logger.warning(f"  Got HTML instead of PDF: {pdf_url}")
                return None
        return _extract_text_from_pdf_bytes(content)
    except Exception as e:
        logger.warning(f"Error extracting PDF {pdf_url}: {e}")
        return None


def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
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
            logger.info(
                f"  Text extracted via pymupdf ({len(result):,} chars).")
            return result
    except Exception as e:
        logger.debug(f"pymupdf extraction failed: {e}")

    return ""


def _download_pdf_bytes(pdf_url: str) -> Optional[bytes]:
    try:
        response = requests.get(pdf_url, headers=_REQUEST_HEADERS, timeout=60)
        response.raise_for_status()
        content = response.content
        if content[:5] != b"%PDF-":
            content_type = response.headers.get("Content-Type", "")
            if "html" in content_type.lower() or b"<html" in content[:200].lower():
                logger.warning(f"  Got HTML instead of PDF: {pdf_url}")
                return None
        return content
    except Exception as e:
        logger.warning(f"  PDF download failed ({pdf_url}): {e}")
        return None


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def _upload_to_s3(pdf_bytes: bytes, doc_id: str, title: str) -> str:
    try:
        from aws_utils import build_docket_key, upload_bytes_to_s3
        slug = _safe_slug(title[:50])
        key = build_docket_key(f"cpuc_{doc_id}_{slug}.pdf")
        result = upload_bytes_to_s3(pdf_bytes, key)
        url = result.get("url", "")
        logger.info(f"  S3 upload: {url}")
        return url
    except Exception as e:
        logger.warning(f"  S3 upload failed for doc {doc_id}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Playwright: Documents table scrape + pagination
# ---------------------------------------------------------------------------

def _parse_documents_from_html(html: str) -> List[Dict[str, Any]]:
    """Parse Filing Date / Document Type / Filed By / Description rows."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.a-IRR-table") or soup.find(
        "table", class_="tbl-body"
    )
    if not table:
        # fallback: any table whose headers look right
        for candidate in soup.find_all("table"):
            header_text = " ".join(
                th.get_text(strip=True) for th in candidate.find_all("th")
            )
            if "Filing Date" in header_text and "Document Type" in header_text:
                table = candidate
                break
    if not table:
        logger.warning("Could not find Documents report table in HTML.")
        return []

    documents: List[Dict[str, Any]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        filing_date_raw = cells[0].get_text(strip=True)
        doc_type_cell = cells[1]
        link_tag = doc_type_cell.find("a")
        document_type = (
            link_tag.get_text(strip=True)
            if link_tag
            else doc_type_cell.get_text(strip=True)
        )
        detail_href = link_tag.get("href", "") if link_tag else ""
        if detail_href and not detail_href.startswith("http"):
            detail_href = urljoin(CPUC_DOCS_BASE, detail_href)

        filed_by = cells[2].get_text(strip=True)
        description = cells[3].get_text(strip=True)

        doc_id = _extract_doc_id(detail_href)
        if not document_type and not detail_href:
            continue

        documents.append({
            "document_id": doc_id,
            "filing_date_raw": filing_date_raw,
            "date": _normalize_date(filing_date_raw),
            "document_type": document_type,
            "detail_url": detail_href,
            "filed_by": filed_by,
            "description": description,
        })

    return documents


def scrape_documents_table_with_playwright(
    url: str,
    cutoff: Optional[datetime] = None,
    headless: bool = True,
    max_docs: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Open CPUC proceeding URL with Playwright, ensure Documents tab,
    paginate until rows fall before cutoff (if set), return matching rows.

    Args:
        url: CPUC APEX Documents/Proceeding URL
        cutoff: If set, stop when Filing Date < cutoff. If None, scrape all pages.
        headless: Run Chromium headless
        max_docs: If set, stop after collecting this many rows (newest-first order)
    """
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    collected: List[Dict[str, Any]] = []
    seen_ids: set = set()
    stop = False

    launch_args = ["--no-sandbox", "--ignore-certificate-errors"]
    context_kwargs: Dict[str, Any] = {
        "ignore_https_errors": True,
        "user_agent": _REQUEST_HEADERS["User-Agent"],
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=launch_args)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        try:
            logger.info(f"Opening CPUC URL: {url}")
            page.goto(url, wait_until="load", timeout=120_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                pass

            # Click Documents tab if present / not active
            try:
                docs_tab = page.locator(
                    "div.sHorizontalTabsInner a", has_text="Documents"
                )
                if docs_tab.count() > 0:
                    parent_li = docs_tab.first.locator("xpath=ancestor::li[1]")
                    classes = parent_li.get_attribute("class") or ""
                    if "active" not in classes:
                        docs_tab.first.click()
                        page.wait_for_timeout(2500)
                        try:
                            page.wait_for_load_state(
                                "networkidle", timeout=15_000)
                        except PlaywrightTimeoutError:
                            pass
            except Exception as e:
                logger.info(f"Documents tab click skipped/failed: {e}")

            # Prefer more rows per page to reduce pagination
            try:
                row_select = page.locator("select.a-IRR-selectList").first
                if row_select.count() > 0:
                    row_select.select_option("200")
                    page.wait_for_timeout(2500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except PlaywrightTimeoutError:
                        pass
            except Exception as e:
                logger.info(f"Row selector change skipped/failed: {e}")

            try:
                page.wait_for_selector(
                    "table.a-IRR-table, table.tbl-body", timeout=20_000
                )
            except PlaywrightTimeoutError:
                logger.warning(
                    "Documents table selector not found after wait.")

            page_num = 1
            while not stop:
                html = page.content()
                page_docs = _parse_documents_from_html(html)
                logger.info(
                    f"Page {page_num}: parsed {len(page_docs)} row(s)."
                )
                if not page_docs:
                    break

                for doc in page_docs:
                    parsed = _parse_filing_date(doc.get("filing_date_raw", ""))
                    if cutoff is not None:
                        if parsed is None:
                            logger.warning(
                                f"Could not parse date "
                                f"{doc.get('filing_date_raw')!r}; keeping row."
                            )
                        elif parsed < cutoff:
                            stop = True
                            break

                    doc_id = doc.get("document_id") or ""
                    # Prefer DocID; fall back to detail_url for uniqueness
                    if not doc_id:
                        doc_id = doc.get("detail_url") or ""
                        doc["document_id"] = doc_id
                    if not doc_id:
                        continue
                    if doc_id in seen_ids:
                        continue
                    seen_ids.add(doc_id)
                    collected.append(doc)

                    if max_docs is not None and len(collected) >= max_docs:
                        stop = True
                        logger.info(
                            f"Reached max_docs={max_docs} — stopping collection."
                        )
                        break

                if stop:
                    if cutoff is not None and (
                        max_docs is None or len(collected) < max_docs
                    ):
                        logger.info(
                            f"Reached cutoff {cutoff.date()} — stopping pagination."
                        )
                    break

                # Click next pagination button if available
                next_clicked = False
                try:
                    next_btn = page.locator(
                        "ul.a-IRR-pagination li.a-IRR-pagination-item "
                        "button.a-IRR-button--pagination"
                    ).last
                    if next_btn.count() == 0:
                        break
                    # Disabled previous/next sit under li.is-disabled
                    parent_disabled = page.locator(
                        "ul.a-IRR-pagination li.a-IRR-pagination-item"
                    ).last
                    parent_class = parent_disabled.get_attribute("class") or ""
                    if "is-disabled" in parent_class:
                        break
                    next_btn.click()
                    page.wait_for_timeout(2500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except PlaywrightTimeoutError:
                        pass
                    next_clicked = True
                    page_num += 1
                except Exception as e:
                    logger.info(f"No further pagination / click failed: {e}")
                    break

                if not next_clicked:
                    break

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    if cutoff is not None:
        logger.info(
            f"Collected {len(collected)} document(s) on/after cutoff {cutoff.date()}."
        )
    else:
        logger.info(f"Collected {len(collected)} document(s) (no cutoff).")
    return collected


# ---------------------------------------------------------------------------
# Single-docket scraper
# ---------------------------------------------------------------------------

def scrape_cpuc(
    url: str,
    docket_number: str,
    headless: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
    cutoff_days: int = CUTOFF_DAYS,
) -> Dict[str, Any]:
    """
    Scrape one CPUC docket Documents table, download PDFs for new rows,
    extract text, and run analyze_docket_entry.
    """
    from docket_entry_analyzer import analyze_docket_entry

    refresh_script_log(logger, _get_log_file)

    cutoff = _cutoff_date(cutoff_days)
    logger.info(
        f"=== CPUC scraper — docket_number={docket_number} "
        f"cutoff={cutoff.date()} (last {cutoff_days} days) "
        f"test_mode={test_mode} ==="
    )

    _run_ctx = {"docket_number": docket_number, "url": url}

    def _error_email(error_message: str, extra_ctx: Optional[dict] = None) -> None:
        send_error_email(
            script_name="cpuc_scraper",
            error_message=error_message,
            context={**_run_ctx, **(extra_ctx or {})},
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
            _error_email(msg, {"step": "mongodb_connect"})
            return {"success": False, "error": msg, "processed": []}

    # Step 1: Playwright scrape Documents table with cutoff
    try:
        documents = scrape_documents_table_with_playwright(
            url=url,
            cutoff=cutoff,
            headless=headless,
        )
    except Exception as e:
        msg = f"Playwright scrape failed: {e}"
        logger.error(msg)
        _error_email(msg, {"step": "playwright_scrape"})
        if mongo_client:
            mongo_client.close()
        return {"success": False, "error": msg, "processed": []}

    if not documents:
        logger.info("No documents within cutoff window.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "No documents within cutoff window.",
            "docket_number": docket_number,
            "cutoff_date": cutoff.date().isoformat(),
            "timestamp": _now_iso(),
        }

    if save_json:
        out_file = os.path.join(
            _THIS_DIR, f"cpuc_{docket_number}_documents.json"
        )
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(documents, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved document list to {out_file}")

    # Step 2: Dedup by docket_type + docket_number + document_id
    all_ids = [d["document_id"] for d in documents if d.get("document_id")]
    if collection is not None:
        new_ids_set = set(
            _batch_filter_existing(collection, docket_number, all_ids)
        )
    else:
        new_ids_set = set(all_ids)

    new_documents = [d for d in documents if d.get(
        "document_id") in new_ids_set]
    if not new_documents:
        logger.info("All documents already in the database.")
        if mongo_client:
            mongo_client.close()
        return {
            "success": True,
            "processed": [],
            "message": "All documents already in DB.",
            "docket_number": docket_number,
            "total_found": len(documents),
            "new": 0,
            "cutoff_date": cutoff.date().isoformat(),
            "timestamp": _now_iso(),
        }

    # Process oldest first for chronological hash_id / history
    new_documents = _sort_documents_oldest_first(new_documents)
    logger.info(
        f"{len(new_documents)} new document(s) to process "
        f"(out of {len(documents)} within cutoff), oldest first."
    )

    processed: List[Dict[str, Any]] = []
    for i, doc in enumerate(new_documents):
        doc_id = doc["document_id"]
        detail_url = doc.get("detail_url", "")
        document_type = doc.get("document_type", "")
        filed_by = doc.get("filed_by", "")
        description = doc.get("description", "")
        filing_date = doc.get("date", "")

        logger.info(
            f"[{i+1}/{len(new_documents)}] doc_id={doc_id} | "
            f"date={filing_date} | {document_type}"
        )
        _doc_ctx = {
            "doc_id": doc_id,
            "document_type": document_type,
            "filing_date": filing_date,
        }

        if not detail_url:
            msg = f"No detail URL for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "missing_detail_url"})
            processed.append({
                "doc_id": doc_id,
                "document_type": document_type,
                "status": "missing_detail_url",
            })
            continue

        # Step 3: Detail page → PDF URLs (copied ResultTable logic)
        tables = extract_tables_from_url(detail_url)
        if tables is None:
            msg = f"Failed to fetch detail page for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(
                msg, {**_doc_ctx, "step": "fetch_detail", "detail_url": detail_url})
            processed.append({
                "doc_id": doc_id,
                "document_type": document_type,
                "status": "detail_fetch_failed",
            })
            continue

        pdf_urls = [
            r.get("PDF_URL", "").strip()
            for r in tables
            if r.get("PDF_URL", "").strip()
        ]
        if not pdf_urls:
            msg = f"No PDF links on detail page for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            processed.append({
                "doc_id": doc_id,
                "document_type": document_type,
                "status": "no_pdf",
            })
            continue

        # Prefer Published Date from detail table when present
        published = ""
        for r in tables:
            if r.get("Published Date"):
                published = _normalize_date(r["Published Date"])
                break
        if published:
            filing_date = published

        title_from_table = ""
        for r in tables:
            if r.get("Title"):
                title_from_table = r["Title"]
                break
        doc_type_from_table = ""
        for r in tables:
            if r.get("Doc Type"):
                doc_type_from_table = r["Doc Type"]
                break

        # Step 4: Download + extract all PDFs, combine text
        all_texts: List[str] = []
        first_pdf_bytes: Optional[bytes] = None
        for idx, pdf_url in enumerate(pdf_urls):
            logger.info(f"  PDF {idx+1}/{len(pdf_urls)}: {pdf_url}")
            pdf_bytes = _download_pdf_bytes(pdf_url)
            if not pdf_bytes:
                continue
            if first_pdf_bytes is None:
                first_pdf_bytes = pdf_bytes
            text = _extract_text_from_pdf_bytes(pdf_bytes)
            if text:
                all_texts.append(f"--- Document {idx + 1} ---\n{text}")

        if not all_texts:
            msg = f"No text extracted for doc_id={doc_id}"
            logger.warning(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "extract_text"})
            processed.append({
                "doc_id": doc_id,
                "document_type": document_type,
                "status": "no_text_extracted",
            })
            continue

        extracted_text = "\n\n".join(all_texts)
        logger.info(
            f"  Combined text from {len(all_texts)} PDF(s): "
            f"{len(extracted_text):,} chars."
        )

        # Step 5: S3 upload (first PDF as primary attachment)
        s3_url = ""
        if not test_mode and first_pdf_bytes:
            s3_url = _upload_to_s3(
                first_pdf_bytes,
                doc_id,
                title_from_table or document_type or doc_id,
            )

        metadata = {
            "docket_type": DOCKET_TYPE,
            "docket_number": docket_number,
            "document_id": doc_id,
            "date": filing_date,
            "document_type": doc_type_from_table or document_type or "N/A",
            "on_behalf_of": filed_by or "N/A",
            "additional_info": (description or title_from_table or "")[:200] or "N/A",
            "url": s3_url or pdf_urls[0],
        }

        # Step 6: analyze_docket_entry
        logger.info(
            f"  Analyzing — type={metadata['document_type']} "
            f"on_behalf_of={str(metadata['on_behalf_of'])[:50]}"
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
                    "document_type": document_type,
                    "status": "analysis_error",
                    "error": result["error"],
                })
            else:
                logger.info(f"  → status={status}")
                intake_note = None
                email_html = None
                if status == "new_analysis":
                    comprehensive_summary = (
                        result.get("comprehensive_summary") or ""
                    )
                    intake_note = generate_intake_note(comprehensive_summary)
                    if intake_note is None:
                        msg = (
                            f"GPT intake note generation failed "
                            f"for doc_id={doc_id}"
                        )
                        logger.warning(f"  {msg}")
                        _error_email(
                            msg, {**_doc_ctx, "step": "gpt_intake_note"})
                    else:
                        document_url = (
                            metadata.get("url")
                            or metadata.get("document_id")
                            or ""
                        )
                        base_html = render_intake_card(
                            intake_note, document_url)
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
                        additional_info = metadata.get("additional_info", "")
                        doc_type_label = metadata.get("document_type", "")
                        subject = (
                            f"{target_company_name} : CAPUC - {docket_number}"
                            f": {additional_info} - {doc_type_label}"
                        )
                        send_docket_email(
                            subject=subject,
                            email_html=email_html,
                            doc_id=doc_id,
                            docket_number=docket_number,
                            docket_type=DOCKET_TYPE,
                            deal_id=result.get("deal_id"),
                        )
                else:
                    logger.info(
                        f"  Skipping intake note / email — status={status}"
                    )

                processed.append({
                    "doc_id": doc_id,
                    "document_type": document_type,
                    "filing_date": filing_date,
                    "status": status,
                    "s3_url": s3_url,
                    "intake_note": intake_note,
                    "email_html": email_html,
                })
        except Exception as e:
            msg = f"Docket analysis exception for doc_id={doc_id}: {e}"
            logger.error(f"  {msg}")
            _error_email(msg, {**_doc_ctx, "step": "docket_analysis"})
            processed.append({
                "doc_id": doc_id,
                "document_type": document_type,
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
        not in (
            "missing_detail_url",
            "detail_fetch_failed",
            "no_pdf",
            "no_text_extracted",
            "analysis_error",
        )
    )
    logger.info(
        f"Finished. {success_count}/{len(new_documents)} new document(s) analyzed."
    )

    return {
        "success": True,
        "docket_number": docket_number,
        "url": url,
        "cutoff_date": cutoff.date().isoformat(),
        "total_found": len(documents),
        "new": len(new_documents),
        "analyzed": success_count,
        "processed": processed,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Multi-docket runner
# ---------------------------------------------------------------------------

def scrape_all_cpuc(
    dockets_file: str = CPUC_DOCKETS_FILE,
    headless: bool = True,
    test_mode: bool = False,
    save_json: bool = False,
    cutoff_days: int = CUTOFF_DAYS,
) -> Dict[str, Any]:
    try:
        dockets = load_dockets_config(dockets_file)
    except FileNotFoundError as e:
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
        docket_url = (entry.get("url") or "").strip()
        docket_number = (entry.get("docket_number") or "").strip()
        description = entry.get("description", "")

        if not docket_url or not docket_number:
            logger.warning(
                f"Skipping invalid config entry "
                f"(missing url or docket_number): {entry}"
            )
            continue

        logger.info(
            f"\n{'='*60}\n"
            f"Docket: {docket_number}\n"
            f"URL: {docket_url}\n"
            f"{description}\n"
            f"{'='*60}"
        )

        result = scrape_cpuc(
            url=docket_url,
            docket_number=docket_number,
            headless=headless,
            test_mode=test_mode,
            save_json=save_json,
            cutoff_days=cutoff_days,
        )
        all_results.append(result)

        if len(dockets) > 1:
            time.sleep(5)

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
        description=(
            "CPUC Docket Scraper — Playwright Documents table, "
            "PDF extract, tier1/2/3 analysis"
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all",
        action="store_true",
        help="Run all active dockets from cpuc_dockets.json",
    )
    mode.add_argument(
        "--docket-number",
        help="Single docket_number from cpuc_dockets.json, e.g. A2507016",
    )
    parser.add_argument(
        "--dockets-file",
        default=CPUC_DOCKETS_FILE,
        help=f"Path to dockets config JSON (default: {CPUC_DOCKETS_FILE})",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        default=False,
        help="Show browser window",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        default=False,
        help="Analyze but skip MongoDB/S3 writes",
    )
    parser.add_argument(
        "--save-json",
        action="store_true",
        default=False,
        help="Save scraped document list to JSON",
    )
    parser.add_argument(
        "--cutoff-days",
        type=int,
        default=CUTOFF_DAYS,
        help=f"Only filings on/after today - N days (default: {CUTOFF_DAYS})",
    )
    args = parser.parse_args()

    if args.all:
        result = scrape_all_cpuc(
            dockets_file=args.dockets_file,
            headless=not args.no_headless,
            test_mode=args.test_mode,
            save_json=args.save_json,
            cutoff_days=args.cutoff_days,
        )
    else:
        dockets = load_dockets_config(args.dockets_file)
        entry = next(
            (
                d
                for d in dockets
                if (d.get("docket_number") or "").strip() == args.docket_number
            ),
            None,
        )
        if not entry:
            print(
                f"ERROR: Docket '{args.docket_number}' not found in "
                f"{args.dockets_file}",
                file=sys.stderr,
            )
            sys.exit(1)
        result = scrape_cpuc(
            url=entry["url"].strip(),
            docket_number=entry["docket_number"].strip(),
            headless=not args.no_headless,
            test_mode=args.test_mode,
            save_json=args.save_json,
            cutoff_days=args.cutoff_days,
        )

    print(json.dumps(result, indent=2, default=str))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
