"""
South Dakota PUC Docket Filed Documents Scraper
================================================
Fetches a docket page from the SD PUC website, parses the "Filed Documents"
section into nested JSON, flattens it (skipping confidential docs), downloads
PDFs, extracts text, and generates tier1 summaries.

Uses requests + BeautifulSoup (static HTML, no JS rendering needed).

Install:
    pip install requests beautifulsoup4 PyPDF2 python-dotenv anthropic

Run:
    python sd_puc_scraper.py --url https://puc.sd.gov/Dockets/GasElectric/2025/GE25-001.aspx
    python sd_puc_scraper.py --url https://puc.sd.gov/Dockets/GasElectric/2025/GE25-001.aspx --save-json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from pymongo import MongoClient
from tier1_summary_generator import generate_tier1_summary

load_dotenv(".env")

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logger = logging.getLogger("sd_puc_scraper")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
logger.propagate = False

BASE_URL = "https://puc.sd.gov"
DATE_PATTERN = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*(.+)")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_date(value: str) -> str:
    if not value:
        return ""
    raw = value.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return raw


def _extract_docket_number(url: str) -> str:
    match = re.search(
        r"/([A-Z]{1,4}\d{2}-\d{3,4})(?:\.aspx|/|$)", url, re.IGNORECASE)
    return match.group(1) if match else ""


def _pdf_url_to_filename(pdf_url: str) -> str:
    name = pdf_url.rsplit("/", 1)[-1] if "/" in pdf_url else pdf_url
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip() or "unknown.pdf"


def _is_pdf_url(url: str) -> bool:
    if not url or not url.strip():
        return False
    path = url.split("?", 1)[0].lower().rstrip("/")
    return path.endswith(".pdf")


# ---------------------------------------------------------------------------
# Fetch HTML
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    logger.info(f"Fetching {url}...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    logger.info(f"Fetched {len(resp.text):,} chars")
    return resp.text


# ---------------------------------------------------------------------------
# Parse "Filed Documents" into nested JSON
# ---------------------------------------------------------------------------

def _is_confidential(li_tag: Tag) -> bool:
    """Check the *direct* text of an <li> (not descendants in nested <ul>)
    for confidential markers like '(not available to the public)'."""
    parts = []
    for child in li_tag.children:
        if isinstance(child, str):
            parts.append(child)
        elif isinstance(child, Tag) and child.name != "ul":
            parts.append(child.get_text())
    text = " ".join(parts).lower()
    return "confidential" in text and "not available to the public" in text


def _parse_li(li: Tag, page_url: str, depth: int = 0) -> Optional[Dict[str, Any]]:
    """Recursively parse an <li> into a document dict with children."""
    link = li.find("a", recursive=False)
    if not link:
        inner_tags = li.find_all("a")
        link = inner_tags[0] if inner_tags else None
    if not link:
        return None

    href = link.get("href", "")
    title = re.sub(r"\s+", " ", link.get_text(strip=True))
    if not title:
        return None

    full_url = urljoin(page_url, href) if href else ""
    confidential = _is_confidential(li)

    date_match = DATE_PATTERN.match(title)
    date = ""
    if date_match:
        date = date_match.group(1)
        title = date_match.group(2).strip()

    doc: Dict[str, Any] = {
        "title": title,
        "url": full_url,
        "date": date,
        "confidential": confidential,
        "children": [],
    }

    for child_ul in li.find_all("ul", recursive=False):
        for child_li in child_ul.find_all("li", recursive=False):
            child = _parse_li(child_li, page_url, depth + 1)
            if child:
                doc["children"].append(child)

    return doc


def parse_filed_documents(html: str, page_url: str) -> List[Dict[str, Any]]:
    """Parse the 'Filed Documents' section into a nested list of dicts."""
    soup = BeautifulSoup(html, "html.parser")
    filed_docs_header = None
    for tag in soup.find_all(["b", "strong"]):
        if "Filed Documents" in tag.get_text():
            filed_docs_header = tag
            break

    if not filed_docs_header:
        logger.error("Could not find 'Filed Documents' section in HTML")
        return []

    main_ul = filed_docs_header.find_next_sibling("ul")
    if not main_ul:
        main_ul = filed_docs_header.find_next("ul")
    if not main_ul:
        logger.error("No <ul> found after 'Filed Documents' header")
        return []

    documents: List[Dict[str, Any]] = []
    for li in main_ul.find_all("li", recursive=False):
        doc = _parse_li(li, page_url)
        if doc:
            documents.append(doc)

    logger.info(f"Parsed {len(documents)} top-level filing groups")
    return documents


# ---------------------------------------------------------------------------
# Flatten nested documents (skip confidential)
# ---------------------------------------------------------------------------

def flatten_documents(
    nested_docs: List[Dict[str, Any]],
    skip_confidential: bool = True,
) -> List[Dict[str, Any]]:
    """
    Walk the nested tree and emit one flat record per document.
    Inherits the date from the nearest ancestor that has one.
    Skips confidential documents when skip_confidential=True.
    """
    flat: List[Dict[str, Any]] = []

    def _walk(
        node: Dict[str, Any],
        inherited_date: str,
        parent_chain: List[str],
    ) -> None:
        date = node.get("date") or inherited_date
        confidential = node.get("confidential", False)

        if skip_confidential and confidential:
            logger.debug(f"Skipping confidential: {node['title']}")
            return

        flat.append({
            "date": _normalize_date(date),
            "title": node["title"],
            "url": node["url"],
            "parent_title": " > ".join(parent_chain) if parent_chain else "",
            "confidential": confidential,
            "filename": "",
            "extracted_text": "",
        })

        for child in node.get("children", []):
            _walk(child, date, parent_chain + [node["title"]])

    for doc in nested_docs:
        _walk(doc, "", [])

    logger.info(
        f"Flattened to {len(flat)} documents"
        f" (skipped confidential={skip_confidential})"
    )
    return flat


# ---------------------------------------------------------------------------
# PDF Download & Text Extraction
# ---------------------------------------------------------------------------

def _extract_text_from_pdf(file_path: str) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(file_path)
        parts = []
        for pg in reader.pages:
            try:
                text = pg.extract_text()
                if text:
                    parts.append(text)
            except Exception:
                continue
        return "\n".join(parts)
    except Exception as e:
        logger.warning(f"PDF extraction failed for {file_path}: {e}")
        return ""


def download_pdfs_and_extract(
    records: List[Dict[str, Any]],
    download_dir: str,
) -> None:
    total = len(records)
    logger.info(f"Downloading {total} PDFs to {download_dir}...")
    downloaded = 0
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    })

    for i, rec in enumerate(records):
        pdf_url = rec.get("url", "").strip()
        if not _is_pdf_url(pdf_url):
            logger.info(
                f"  [{i+1}/{total}] Not a PDF URL, skipping: {pdf_url}")
            continue

        filename = _pdf_url_to_filename(pdf_url)
        save_path = os.path.join(download_dir, filename)
        rec["filename"] = filename

        if os.path.exists(save_path):
            logger.info(f"  [{i+1}/{total}] Already exists: {filename}")
            rec["extracted_text"] = _extract_text_from_pdf(save_path)
            downloaded += 1
            continue

        try:
            resp = session.get(pdf_url, timeout=60)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(resp.content)
            downloaded += 1
            logger.info(f"  [{i+1}/{total}] Downloaded: {filename}")

            extracted = _extract_text_from_pdf(save_path)
            rec["extracted_text"] = extracted
            if extracted:
                logger.info(
                    f"  [{i+1}/{total}] Extracted {len(extracted):,} chars")
            else:
                logger.info(f"  [{i+1}/{total}] No text extracted")
        except Exception as e:
            logger.warning(f"  [{i+1}/{total}] Download error: {e}")
            rec["filename"] = ""
            rec["extracted_text"] = ""

    logger.info(f"Downloaded {downloaded}/{total} PDFs.")


# ---------------------------------------------------------------------------
# MongoDB: filter out already-processed documents
# ---------------------------------------------------------------------------

COLLECTION_NAME = "docket"


def _get_mongo_collection():
    """Connect to MongoDB and return the docket collection, or None."""
    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        logger.warning("MONGODB_CONNECTION_STRING not set — skipping DB dedup check")
        return None
    try:
        client = MongoClient(mongodb_uri)
        db_name = (os.environ.get("MONGODB_DATABASE_NAME") or "").strip()
        db = client.get_database(db_name) if db_name else client.get_database()
        return db[COLLECTION_NAME]
    except Exception as e:
        logger.warning(f"MongoDB connection failed — skipping DB dedup: {e}")
        return None


def filter_already_processed(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Check MongoDB for documents that have already been processed
    (matched by metadata.document_id + metadata.docket_type == 'sd-puc').
    Returns only new records.
    """
    coll = _get_mongo_collection()
    if coll is None:
        return records

    all_urls = [r["url"] for r in records if r.get("url")]
    if not all_urls:
        return records

    try:
        existing_cursor = coll.find(
            {
                "metadata.document_id": {"$in": all_urls},
                "metadata.docket_type": "sd-puc",
            },
            {"metadata.document_id": 1},
        )
        existing_ids = {
            doc["metadata"]["document_id"]
            for doc in existing_cursor
            if doc.get("metadata", {}).get("document_id")
        }
    except Exception as e:
        logger.warning(f"MongoDB query failed — processing all records: {e}")
        return records

    if not existing_ids:
        logger.info("DB check: no previously processed documents found")
        return records

    new_records = [r for r in records if r.get("url") not in existing_ids]
    skipped = len(records) - len(new_records)
    logger.info(
        f"DB check: {skipped} already processed, {len(new_records)} new documents to process"
    )
    return new_records


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scrape_sd_puc(
    url: str,
    last_url: Optional[str] = None,
    save_json: bool = False,
    row_number: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Main scraper entry point.

    Args:
        url: Full URL to the SD PUC docket page.
        last_url: Watermark — PDF URL of the last processed document.
                  Only documents appearing before this are considered new.
        save_json: Save results to a JSON file.
        row_number: Optional row number for batch tracking.

    Returns:
        List of flat record dicts (oldest first).
    """
    docket_number = _extract_docket_number(url)
    logger.info(f"Docket number: {docket_number}")

    download_dir = os.path.join(
        os.getcwd(), "sd_puc_downloads", docket_number or "unknown")
    os.makedirs(download_dir, exist_ok=True)

    # Step 1: Fetch HTML
    html = fetch_html(url)

    # Step 2: Parse nested structure
    nested_docs = parse_filed_documents(html, url)
    if not nested_docs:
        logger.info("No filed documents found.")
        return []

    if save_json:
        nested_file = f"sd_puc_{docket_number}_nested.json"
        with open(nested_file, "w", encoding="utf-8") as f:
            json.dump(nested_docs, f, indent=2, ensure_ascii=False)
        logger.info(f"Nested JSON saved to {nested_file}")

    # Step 3: Flatten (skip confidential)
    flat_records = flatten_documents(nested_docs, skip_confidential=True)
    if not flat_records:
        logger.info("No non-confidential documents found.")
        return []

    before = len(flat_records)
    flat_records = [r for r in flat_records if _is_pdf_url(r.get("url", ""))]
    skipped = before - len(flat_records)
    if skipped:
        logger.info(f"Skipped {skipped} non-PDF document links")
    if not flat_records:
        logger.info("No PDF documents found.")
        return []

    # Step 4: Filter out already-processed documents (MongoDB dedup)
    flat_records = filter_already_processed(flat_records)
    if not flat_records:
        logger.info("All documents already processed in DB.")
        return []

    # Step 5: Apply watermark (incremental)
    if last_url:
        logger.info(f"Applying watermark: {last_url}")
        new_records = []
        for rec in flat_records:
            if rec.get("url", "").strip() == last_url.strip():
                break
            new_records.append(rec)
        if new_records:
            logger.info(f"Watermark applied: {len(new_records)} new records")
            flat_records = new_records
        else:
            logger.info(
                "No new records since watermark (or watermark not found).")

    # Step 6: Download PDFs and extract text
    download_pdfs_and_extract(flat_records, download_dir)

    # Add docket metadata to each record
    for rec in flat_records:
        rec["docket_number"] = docket_number
        rec["row_number"] = row_number

    if save_json:
        flat_file = f"sd_puc_{docket_number}_flat.json"
        with open(flat_file, "w", encoding="utf-8") as f:
            json.dump(flat_records, f, indent=2, ensure_ascii=False)
        logger.info(f"Flat JSON saved to {flat_file}")

    logger.info(
        f"Scraped {len(flat_records)} documents for docket {docket_number}")
    return flat_records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="South Dakota PUC Docket Filed Documents Scraper"
    )
    parser.add_argument(
        "--url", required=True,
        help="Full URL to the SD PUC docket page",
    )
    parser.add_argument(
        "--last-url", default=None,
        help="Watermark: PDF URL of the last processed doc (incremental scraping)",
    )
    parser.add_argument(
        "--save-json", action="store_true", default=False,
        help="Save nested and flat JSON files",
    )
    parser.add_argument(
        "--row-number", type=int, default=None,
        help="Row number for batch tracking",
    )
    args = parser.parse_args()

    records = scrape_sd_puc(
        url=args.url,
        last_url=args.last_url,
        save_json=args.save_json,
        row_number=args.row_number,
    )

    out_file = "sd_puc_records.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    if records:
        docket_number = records[0].get("docket_number", "")
        print(
            f"\nSuccess! Scraped {len(records)} documents for {docket_number}.")
        for rec in records:
            print(f"  - {rec.get('date')} | {rec.get('title')[:80]}")

        print("\nGenerating tier1 summaries and saving to MongoDB...")
        for rec in records:
            text = (rec.get("extracted_text") or "").strip()
            if not text:
                print(
                    f"  - {rec.get('title')[:60]}: skipped (no extracted_text)")
                continue

            metadata = {
                "document_id": rec.get("url", ""),
                "date": rec.get("date", ""),
                "document_type": rec.get("title", "N/A"),
                "additional_info": rec.get("parent_title", ""),
                "on_behalf_of": "",
                "docket_number": rec.get("docket_number", ""),
                "docket_type": "sd-puc",
            }
            print(f"Metadata: {metadata}")
            result = generate_tier1_summary(metadata=metadata, text=text)
            status = result.get("status", "unknown")
            if result.get("error"):
                print(f"  - {rec.get('title')[:60]}: error - {result.get('error')}")
            else:
                print(
                    f"  - {rec.get('title')[:60]}: {status} "
                    f"(summary_length={result.get('summary_length', 0)})"
                )
    else:
        print("\nNo documents found (or scraper failed).")
        sys.exit(1)


if __name__ == "__main__":
    main()
