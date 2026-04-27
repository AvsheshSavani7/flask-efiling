"""
Nebraska PSC Order Search Scraper
=================================
Uses Playwright to automate the Nebraska PSC Order Search (Advanced Search),
scrape the results table, download PDFs, extract text, and generate tier1
summaries.

Incremental scraping: pass a last_id (PDF URL or date_disposition key) to only
process records newer than that watermark.

Install:
    pip install playwright python-dotenv
    playwright install chromium

Run:
    python ne_psc_scraper.py --department Natural_Gas --docket-number 128
    python ne_psc_scraper.py --department Natural_Gas --docket-number 128 --from-date 01/04/2025 --to-date 27/04/2026
    python ne_psc_scraper.py --headless --save-json
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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tier1_summary_generator import generate_tier1_summary

load_dotenv(".env")

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logger = logging.getLogger("ne_psc_scraper")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
logger.propagate = False

NE_PSC_URL = "https://www.nebraska.gov/psc/ordersearch/user/index.cgi"

DEPARTMENTS = [
    "Administration", "Grain", "Housing",
    "Natural_Gas", "State_911", "Telecommunications", "Transportation",
]

# Default proxy (residential) — same as puc_scraper
DEFAULT_PROXY_HOST = "108.59.242.138"
DEFAULT_PROXY_PORT = 46885
DEFAULT_PROXY_USER = "GSenAgrfKhuNWkd"
DEFAULT_PROXY_PASS = "8lmVa5yl0pKp9MI"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_date(value: str) -> str:
    """Normalize a scraped date string (MM-DD-YYYY) to MM/DD/YYYY."""
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""
    formats = (
        "%m-%d-%Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%m-%d-%y",
        "%m/%d/%y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%m/%d/%Y")
        except ValueError:
            continue
    return raw


def _pdf_url_to_filename(pdf_url: str) -> str:
    """Extract a safe filename from the PDF URL."""
    decoded = unquote(pdf_url)
    name = decoded.rsplit("/", 1)[-1] if "/" in decoded else decoded
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip() or "unknown.pdf"


# ---------------------------------------------------------------------------
# Browser Launch
# ---------------------------------------------------------------------------

def _build_default_proxy() -> Dict[str, str]:
    """Build proxy dict from default residential proxy credentials."""
    host = os.getenv("NE_PROXY_HOST", DEFAULT_PROXY_HOST)
    port = os.getenv("NE_PROXY_PORT", str(DEFAULT_PROXY_PORT))
    user = os.getenv("NE_PROXY_USER", DEFAULT_PROXY_USER)
    pwd = os.getenv("NE_PROXY_PASS", DEFAULT_PROXY_PASS)
    return {
        "server": f"http://{host}:{port}",
        "username": user,
        "password": pwd,
    }


def _launch_browser(p, headless: bool, proxy: Optional[str] = None, use_default_proxy: bool = True):
    """Launch Chromium with proxy. Uses residential proxy by default."""
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--ignore-certificate-errors",
    ]

    proxy_config = None
    if proxy:
        proxy_config = {"server": proxy}
        launch_args.append(f"--proxy-server={proxy}")
        logger.info(f"Using custom proxy: {proxy}")
    elif use_default_proxy:
        pd = _build_default_proxy()
        proxy_config = pd
        launch_args.append(f"--proxy-server={pd['server']}")
        logger.info(f"Using default residential proxy: {pd['server']}")

    browser = p.chromium.launch(
        headless=headless,
        args=launch_args,
    )
    context_kwargs = {
        "viewport": {"width": 1280, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "accept_downloads": True,
        "ignore_https_errors": True,
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    }
    if proxy_config:
        context_kwargs["proxy"] = proxy_config

    context = browser.new_context(**context_kwargs)
    return browser, context


# ---------------------------------------------------------------------------
# Form Interaction
# ---------------------------------------------------------------------------

def _navigate_to_advanced_search(page: Page) -> None:
    """Navigate to the Nebraska PSC Order Search and click Advanced Search tab."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(
                f"Navigating to {NE_PSC_URL} (attempt {attempt + 1}/{max_retries})...")
            page.goto(NE_PSC_URL, wait_until="commit", timeout=90000)
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            break
        except PlaywrightTimeoutError:
            if attempt < max_retries - 1:
                logger.warning(
                    f"Navigation timed out (attempt {attempt + 1}), retrying...")
                page.wait_for_timeout(5000)
            else:
                raise RuntimeError(
                    f"Failed to load {NE_PSC_URL} after {max_retries} attempts. "
                    "The site may be blocking requests — try a different proxy."
                )

    logger.info("Clicking 'Advanced Search' tab...")
    adv_tab_selectors = [
        "#advanced-search-tab",
        "a[href='#advanced-search']",
        "a:has-text('Advanced Search')",
    ]
    for sel in adv_tab_selectors:
        try:
            tab = page.locator(sel).first
            if tab.is_visible(timeout=5000):
                tab.click()
                logger.info(f"Advanced Search tab clicked (selector: {sel}).")
                page.wait_for_timeout(2000)
                return
        except PlaywrightTimeoutError:
            continue

    raise RuntimeError("Could not find 'Advanced Search' tab.")


def _fill_advanced_search_form(
    page: Page,
    department: str,
    docket_number: str,
    from_date: str,
    to_date: str,
    division: Optional[str] = None,
) -> None:
    """Fill in the Advanced Search form fields."""
    logger.info(f"Filling form: department={department}, docket={docket_number}, "
                f"from={from_date}, to={to_date}")

    # Select department from dropdown
    logger.info(f"Selecting department: {department}")
    page.select_option("#dad", value=department)
    page.wait_for_timeout(1000)

    # If division prefix specified, fill it
    if division:
        logger.info(f"Filling division prefix: {division}")
        page.fill("#prefix", division)
        page.wait_for_timeout(500)

    # Fill docket number
    if docket_number:
        logger.info(f"Filling docket number: {docket_number}")
        page.fill("#sbdn", docket_number)
        page.wait_for_timeout(500)

    # Fill date range using JS to bypass jQuery UI datepicker restrictions
    if from_date:
        logger.info(f"Setting From date: {from_date}")
        page.evaluate(
            """(date) => {
                const el = document.getElementById('datepickerStart');
                if (el) { el.value = date; }
            }""",
            from_date,
        )
        page.wait_for_timeout(500)

    if to_date:
        logger.info(f"Setting To date: {to_date}")
        page.evaluate(
            """(date) => {
                const el = document.getElementById('datepickerEnd');
                if (el) { el.value = date; }
            }""",
            to_date,
        )
        page.wait_for_timeout(500)


def _submit_search(page: Page) -> None:
    """Click the Search submit button and wait for results.
    The form POSTs to index.cgi — results may be server-rendered on page reload
    or loaded via AJAX into #advanceresults. We handle both.
    """
    logger.info("Clicking Search button...")

    submit_selectors = [
        "button[type='submit'][name='advsearchsubmit']",
        "#advsearch button[type='submit']",
        "button:has-text('Search')",
    ]

    clicked = False
    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=5000):
                btn.click()
                logger.info(f"Search button clicked (selector: {sel}).")
                clicked = True
                break
        except PlaywrightTimeoutError:
            continue

    if not clicked:
        raise RuntimeError("Could not find Search submit button.")

    logger.info("Waiting for search results...")
    page.wait_for_timeout(5000)

    for attempt in range(15):
        # Check for form validation errors
        try:
            error_div = page.locator("#adverrors")
            if error_div.is_visible(timeout=1000):
                error_text = (error_div.text_content() or "").strip()
                if error_text:
                    logger.error(f"Form error: {error_text}")
                    raise RuntimeError(f"Search form error: {error_text}")
        except PlaywrightTimeoutError:
            pass

        # Check for results table (server-rendered after POST)
        try:
            table = page.locator(
                "table.table thead th:has-text('Docket')").first
            if table.is_visible(timeout=2000):
                logger.info("Results table loaded (server-rendered).")
                return
        except PlaywrightTimeoutError:
            pass

        # Check for AJAX results in #advanceresults
        try:
            results_div = page.locator("#advanceresults")
            if results_div.is_visible(timeout=1000):
                ajax_table = results_div.locator("table").first
                if ajax_table.is_visible(timeout=1000):
                    logger.info("Results table loaded (AJAX).")
                    return
                no_res = (results_div.text_content() or "").strip().lower()
                if "no results" in no_res or "0 results" in no_res:
                    logger.info("No results found for the search criteria.")
                    return
        except PlaywrightTimeoutError:
            pass

        logger.info(f"  Waiting for results (attempt {attempt + 1}/15)...")
        page.wait_for_timeout(3000)

    logger.warning("Results may not have fully loaded, proceeding anyway...")


# ---------------------------------------------------------------------------
# Table Scraping
# ---------------------------------------------------------------------------

def _scrape_current_page(page: Page) -> List[Dict[str, Any]]:
    """Scrape all rows from the results table on the current page."""
    records = page.evaluate(r"""() => {
        const results = [];
        let table = null;
        const container = document.getElementById('advanceresults');
        if (container) table = container.querySelector('table');
        if (!table) {
            const allTables = document.querySelectorAll('table.table');
            for (const t of allTables) {
                const th = t.querySelector('th');
                if (th && th.textContent.trim() === 'Date') { table = t; break; }
            }
        }
        if (!table) return results;

        const rows = table.querySelectorAll('tbody > tr');
        for (const row of rows) {
            const cells = row.querySelectorAll('td');
            if (cells.length < 6) continue;

            const dateText = (cells[0].textContent || '').trim();
            const prefix = (cells[1].textContent || '').trim();
            const docketNum = (cells[2].textContent || '').trim();

            let caption = (cells[3].textContent || '').trim();
            caption = caption.replace(/\s+/g, ' ').trim();

            let disposition = (cells[4].textContent || '').trim();
            disposition = disposition.replace(/\s+/g, ' ').trim();

            const pdfLink = cells[5].querySelector('a');
            const pdfUrl = pdfLink ? pdfLink.getAttribute('href') : '';

            results.push({
                date: dateText,
                prefix: prefix,
                docket_number: docketNum,
                caption: caption,
                disposition: disposition,
                pdf_url: pdfUrl || '',
            });
        }
        return results;
    }""")
    return records


def _scrape_results_table(page: Page, filter_docket: Optional[str] = None) -> List[Dict[str, Any]]:
    """Scrape all pages of results, handling pagination via the Next button.
    Optionally filter rows to only those matching filter_docket."""
    all_records = []
    page_num = 1

    while True:
        logger.info(f"Scraping results page {page_num}...")
        page_records = _scrape_current_page(page)
        logger.info(f"  Page {page_num}: {len(page_records)} rows")

        if not page_records:
            break

        all_records.extend(page_records)

        # Check for "Next" pagination link
        try:
            next_link = page.locator("a.advnext").first
            if not next_link.is_visible(timeout=2000):
                logger.info("  No Next button visible — last page.")
                break

            parent_li = next_link.locator("xpath=..")
            li_class = (parent_li.get_attribute("class") or "")
            if "disabled" in li_class:
                logger.info("  Next button is disabled — last page.")
                break

            next_link.click()
            page.wait_for_timeout(5000)

            # Wait for the new page to render
            for attempt in range(10):
                try:
                    table = page.locator(
                        "table.table thead th:has-text('Docket')").first
                    if table.is_visible(timeout=2000):
                        break
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2000)

            page_num += 1

        except PlaywrightTimeoutError:
            logger.info("  No pagination found — single page of results.")
            break

    logger.info(
        f"Scraped {len(all_records)} total records across {page_num} page(s).")

    # Filter by docket number if specified
    if filter_docket:
        before = len(all_records)
        all_records = [
            r for r in all_records
            if r.get("docket_number", "").strip() == str(filter_docket).strip()
        ]
        skipped = before - len(all_records)
        if skipped:
            logger.info(
                f"Filtered to docket {filter_docket}: kept {len(all_records)}, skipped {skipped}")

    return all_records


def _apply_watermark(
    records: List[Dict[str, Any]], last_pdf_url: Optional[str]
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Filter records to only include those newer than the watermark.
    The watermark is the PDF URL of the last processed record.
    Records in the table are ordered newest-first, so we take everything
    before the watermark.
    """
    if not last_pdf_url:
        return records, False

    logger.info(f"Applying watermark: {last_pdf_url}")
    new_records = []
    reached = False
    for rec in records:
        if rec.get("pdf_url", "").strip() == last_pdf_url.strip():
            reached = True
            break
        new_records.append(rec)

    if reached:
        logger.info(f"Watermark reached. {len(new_records)} new records.")
    else:
        logger.warning(
            f"Watermark URL not found in results — returning all {len(records)} records.")
        new_records = records

    return new_records, reached


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


def _download_pdfs(
    page: Page,
    records: List[Dict[str, Any]],
    download_dir: str,
) -> None:
    """Download PDF files for each record and extract text."""
    total = len(records)
    logger.info(f"Downloading {total} PDFs to {download_dir}...")
    downloaded = 0

    for i, rec in enumerate(records):
        pdf_url = rec.get("pdf_url", "").strip()
        if not pdf_url:
            logger.warning(f"  [{i+1}/{total}] No PDF URL, skipping.")
            continue

        filename = _pdf_url_to_filename(pdf_url)
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", filename)
        save_path = os.path.join(download_dir, safe_name)

        if os.path.exists(save_path):
            logger.info(f"  [{i+1}/{total}] Already exists: {safe_name}")
            rec["filename"] = safe_name
            rec["extracted_text"] = _extract_text_from_pdf(save_path)
            downloaded += 1
            continue

        try:
            with page.expect_download(timeout=60000) as download_info:
                page.evaluate(
                    """(url) => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = '';
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    }""",
                    pdf_url,
                )
            dl = download_info.value
            dl.save_as(save_path)
            rec["filename"] = safe_name
            downloaded += 1
            logger.info(f"  [{i+1}/{total}] Downloaded: {safe_name}")

            extracted = _extract_text_from_pdf(save_path)
            rec["extracted_text"] = extracted
            if extracted:
                logger.info(
                    f"  [{i+1}/{total}] Extracted {len(extracted)} chars")
            else:
                logger.info(f"  [{i+1}/{total}] No text extracted")

        except PlaywrightTimeoutError:
            logger.warning(f"  [{i+1}/{total}] Download timed out: {pdf_url}")
            rec["filename"] = ""
            rec["extracted_text"] = ""
        except Exception as e:
            logger.warning(f"  [{i+1}/{total}] Download error: {e}")
            rec["filename"] = ""
            rec["extracted_text"] = ""

    logger.info(f"Downloaded {downloaded}/{total} PDFs.")


def _download_pdfs_via_requests(
    records: List[Dict[str, Any]],
    download_dir: str,
    proxy: Optional[str] = None,
    use_default_proxy: bool = True,
) -> None:
    """Download PDFs via requests, routed through proxy."""
    import requests

    total = len(records)
    logger.info(f"Downloading {total} PDFs via requests to {download_dir}...")
    downloaded = 0

    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        logger.info(f"PDF downloads using custom proxy: {proxy}")
    elif use_default_proxy:
        pd = _build_default_proxy()
        proxy_url = f"http://{pd['username']}:{pd['password']}@{pd['server'].replace('http://', '')}"
        session.proxies = {"http": proxy_url, "https": proxy_url}
        logger.info(f"PDF downloads using default residential proxy")

    for i, rec in enumerate(records):
        pdf_url = rec.get("pdf_url", "").strip()
        if not pdf_url:
            continue

        filename = _pdf_url_to_filename(pdf_url)
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", filename)
        save_path = os.path.join(download_dir, safe_name)

        if os.path.exists(save_path):
            logger.info(f"  [{i+1}/{total}] Already exists: {safe_name}")
            rec["filename"] = safe_name
            rec["extracted_text"] = _extract_text_from_pdf(save_path)
            downloaded += 1
            continue

        try:
            resp = session.get(pdf_url, timeout=60)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(resp.content)
            rec["filename"] = safe_name
            downloaded += 1
            logger.info(f"  [{i+1}/{total}] Downloaded: {safe_name}")

            extracted = _extract_text_from_pdf(save_path)
            rec["extracted_text"] = extracted
            if extracted:
                logger.info(
                    f"  [{i+1}/{total}] Extracted {len(extracted)} chars")
        except Exception as e:
            logger.warning(f"  [{i+1}/{total}] Download error: {e}")
            rec["filename"] = ""
            rec["extracted_text"] = ""

    logger.info(f"Downloaded {downloaded}/{total} PDFs.")


# ---------------------------------------------------------------------------
# Flatten records for output
# ---------------------------------------------------------------------------

def _flatten_records(
    records: List[Dict[str, Any]], row_number: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Normalize each record for consistent output."""
    flat = []
    for rec in records:
        flat.append({
            "row_number": row_number,
            "date": _normalize_date(rec.get("date", "")),
            "prefix": rec.get("prefix", ""),
            "docket_number": rec.get("docket_number", ""),
            "caption": rec.get("caption", ""),
            "disposition": rec.get("disposition", ""),
            "pdf_url": rec.get("pdf_url", ""),
            "filename": rec.get("filename", ""),
            "extracted_text": rec.get("extracted_text", ""),
        })
    return flat


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def scrape_ne_psc(
    department: str = "Natural_Gas",
    docket_number: str = "128",
    from_date: str = "01/04/2025",
    to_date: str = "04/27/2026",
    division: Optional[str] = None,
    last_pdf_url: Optional[str] = None,
    headless: bool = True,
    proxy: Optional[str] = None,
    no_proxy: bool = False,
    save_json: bool = False,
    row_number: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Main scraper entry point.

    Args:
        department: Department value for the dropdown (e.g. Natural_Gas)
        docket_number: Docket number to search
        from_date: Start date (MM/DD/YYYY)
        to_date: End date (MM/DD/YYYY)
        division: Optional division prefix filter
        last_pdf_url: Watermark — PDF URL of the last processed record.
                      Only records before this in the table are considered new.
        headless: Run browser in headless mode
        proxy: Optional custom proxy server URL (e.g. http://host:port)
        no_proxy: If True, skip default proxy (direct connection)
        save_json: Save results to a JSON file
        row_number: Optional row number for batch tracking

    Returns:
        List of flat record dicts (oldest first)
    """
    flat_records = []

    download_dir = os.path.join(
        os.getcwd(), "ne_psc_downloads",
        f"{department}_{docket_number}",
    )
    os.makedirs(download_dir, exist_ok=True)
    logger.info(f"Download directory: {download_dir}")

    with sync_playwright() as p:
        browser, context = _launch_browser(
            p, headless, proxy, use_default_proxy=not no_proxy
        )
        page = context.new_page()

        try:
            # Step 1: Navigate and open Advanced Search
            _navigate_to_advanced_search(page)

            # Step 2: Fill form fields
            _fill_advanced_search_form(
                page, department, docket_number, from_date, to_date, division
            )

            # Step 3: Submit search
            _submit_search(page)

            # Step 4: Scrape results table (all pages, filtered by docket number)
            raw_records = _scrape_results_table(
                page, filter_docket=docket_number)

            if not raw_records:
                logger.info("No records found.")
                return []

            # Step 5: Apply watermark filter (incremental)
            new_records, reached_watermark = _apply_watermark(
                raw_records, last_pdf_url
            )

            if not new_records:
                logger.info("No new records since last watermark.")
                return []

            # Step 6: Download PDFs and extract text
            _download_pdfs_via_requests(
                new_records, download_dir, proxy,
                use_default_proxy=not no_proxy,
            )

            # Step 7: Flatten records
            flat_records = _flatten_records(new_records, row_number)

            if save_json:
                out_file = f"ne_psc_{department}_{docket_number}.json"
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(flat_records, f, indent=2, ensure_ascii=False)
                logger.info(f"Results saved to {out_file}")

            if flat_records:
                new_watermark = flat_records[0].get("pdf_url", "")
                logger.info(f"New watermark (newest PDF URL): {new_watermark}")

            logger.info(
                f"Scraped {len(flat_records)} records for "
                f"{department} docket {docket_number}"
            )

        except Exception as e:
            logger.error(f"Scraper error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            context.close()
            browser.close()

    # Return oldest-first (table is newest-first)
    return flat_records[::-1]


def main():
    parser = argparse.ArgumentParser(
        description="Nebraska PSC Order Search Scraper"
    )
    parser.add_argument(
        "--department", default="Natural_Gas",
        choices=DEPARTMENTS,
        help="Department to search (default: Natural_Gas)",
    )
    parser.add_argument(
        "--docket-number", default="128",
        help="Docket number to search (default: 128)",
    )
    parser.add_argument(
        "--from-date", default="01/04/2025",
        help="Start date MM/DD/YYYY (default: 01/04/2025)",
    )
    parser.add_argument(
        "--to-date", default="04/27/2026",
        help="End date MM/DD/YYYY (default: 04/27/2026)",
    )
    parser.add_argument(
        "--division", default=None,
        help="Division prefix filter (optional)",
    )
    parser.add_argument(
        "--last-pdf-url", default=None,
        help="Watermark: PDF URL of last processed record. Only newer records are processed.",
    )
    parser.add_argument(
        "--headless", action="store_true", default=False,
        help="Run in headless mode",
    )
    parser.add_argument(
        "--proxy", default=None,
        help="Custom proxy server URL (e.g. http://host:port)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=False,
        help="Disable default residential proxy (direct connection)",
    )
    parser.add_argument(
        "--save-json", action="store_true", default=False,
        help="Save results to a JSON file",
    )
    parser.add_argument(
        "--row-number", type=int, default=None,
        help="Row number for batch tracking",
    )
    args = parser.parse_args()

    records = scrape_ne_psc(
        department=args.department,
        docket_number=args.docket_number,
        from_date=args.from_date,
        to_date=args.to_date,
        division=args.division,
        last_pdf_url=args.last_pdf_url,
        headless=args.headless,
        proxy=args.proxy,
        no_proxy=args.no_proxy,
        save_json=args.save_json,
        row_number=args.row_number,
    )

    with open("ne_psc_records.json", "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    if records:
        print(f"\nSuccess! Scraped {len(records)} records.")
        for rec in records:
            print(
                f"  - {rec.get('date')} | {rec.get('prefix')}-{rec.get('docket_number')} | "
                f"{rec.get('disposition')}"
            )

        print("\nGenerating tier1 summaries and saving to MongoDB...")
        for rec in records:
            text = (rec.get("extracted_text") or "").strip()
            if not text:
                print(
                    f"  - {rec.get('disposition')}: skipped (no extracted_text)")
                continue

            doc_id = (
                f"{rec.get('prefix', 'UNK')}-{rec.get('docket_number', '0')}_"
                f"{rec.get('date', '').replace('/', '-')}_{rec.get('disposition', '')[:40]}"
            )
            metadata = {
                "document_id": rec.get("pdf_url", ""),
                "date": rec.get("date", ""),
                "document_type": rec.get("disposition", "N/A"),
                "additional_info": rec.get("caption", ""),
                "on_behalf_of": "",
                "docket_number": rec.get("docket_number", ""),
                "docket_type": "ne-psc",
            }
            print(f"Metadata: {metadata}")
            # result = generate_tier1_summary(metadata=metadata, text=text)
            # status = result.get("status", "unknown")
            # if result.get("error"):
            #     print(f"  - {doc_id}: error - {result.get('error')}")
            # else:
            #     print(
            #         f"  - {doc_id}: {status} "
            #         f"(summary_length={result.get('summary_length', 0)})"
            #     )
    else:
        print("\nNo new records found (or scraper failed).")
        sys.exit(1)


if __name__ == "__main__":
    main()
