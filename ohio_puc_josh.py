"""
Ohio PUC (Public Utilities Commission of Ohio) Docket Scraper.

Scrapes docket entries from the PUCO DIS (Docketing Information System),
downloads the PDF for each filing, and extracts the full text.

Supports incremental mode: on subsequent runs it only downloads new entries
that weren't present in the previous run.

The site is protected by an F5 BIG-IP WAF that requires a real browser
with JavaScript execution. Uses Playwright in headed mode with stealth
patches and a persistent browser profile to reliably bypass bot detection.

The WAF also blocks datacenter IPs, so on a server we exit through a US/Ohio
residential proxy. Sticky sessions are read from proxy-2174291-credentials.txt;
one session (a fixed IP for ~30 min) is used per attempt, rotating to a fresh
session if the WAF blocks. If the file is absent, it runs without a proxy.

Requirements:
    pip install playwright pdfplumber
    python -m playwright install chromium

Usage:
    python ohio_puc.py                          # incremental (default)
    python ohio_puc.py --full                   # force full re-download
    python ohio_puc.py --no-pdf                 # skip PDF download/extraction
    python ohio_puc.py --delay 3                # seconds between requests
    python ohio_puc.py 26-0435-EL-MER           # specific case
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pdfplumber
from playwright.sync_api import sync_playwright, Page

from log_utils import ensure_script_logger, refresh_script_log


# ---------------------------------------------------------------------------
# Logging — date-wise files under /var/data/logs/ohio_puc_josh/ (IST),
# also streamed to stdout so `docker logs` / terminal show live output.
# ---------------------------------------------------------------------------
logger, _get_log_file = ensure_script_logger("ohio_puc_josh")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://dis.puc.state.oh.us"
DEFAULT_CASE_NO = "26-0435-EL-MER"

STEALTH_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    delete navigator.__proto__.webdriver;
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    const _origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) => (
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : _origQuery(p)
    );
"""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)

# JS executed inside the browser to fetch a URL as binary and return base64.
# This is the only reliable way to download PDFs through the F5 WAF because
# the in-browser fetch() shares the exact session/cookies that passed the
# JS challenge — API-level requests (context.request) do not.
FETCH_BINARY_JS = """
async (url) => {
    const resp = await fetch(url);
    const buffer = await resp.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return {
        ok: resp.ok,
        status: resp.status,
        contentType: resp.headers.get('content-type') || '',
        size: bytes.length,
        base64: btoa(binary),
    };
}
"""


# ---------------------------------------------------------------------------
# Residential proxy (sticky Ohio sessions)
#
# The F5 WAF blocks datacenter IPs, so from the server we must exit through a
# US/Ohio residential IP. Each line in the credentials file is a sticky session
# that pins one residential IP for ttl-30 minutes — plenty for a single run.
# We pick one session per attempt and rotate to a fresh one if the WAF blocks.
# ---------------------------------------------------------------------------

# Resolve relative to this file (robust to CWD); override via OH_PUC_PROXY_FILE
# so the credentials can live on a mounted path without editing code.
PROXY_FILE = Path(
    os.environ.get("OH_PUC_PROXY_FILE")
    or Path(__file__).resolve().parent / "proxy-2174291-credentials.txt"
)

# reCAPTCHA / stealth need Google domains direct (not proxied).
PROXY_BYPASS = "*.google.com;*.gstatic.com;*.googleapis.com;*.recaptcha.net"

# Max distinct sticky sessions to try before giving up on the WAF.
MAX_PROXY_ROTATIONS = 4


def _parse_proxy_line(line: str) -> dict | None:
    """Parse ``user:pass@host:port`` into a Playwright proxy dict."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    creds, _, hostport = line.rpartition("@")
    if not creds or not hostport:
        return None
    user, _, password = creds.partition(":")
    return {"server": f"http://{hostport}", "username": user, "password": password}


def load_sticky_proxies(path: Path = PROXY_FILE) -> list[dict]:
    """Load sticky Ohio session proxies from the credentials file."""
    if not path.exists():
        logger.warning(f"Proxy file not found ({path}); running without proxy.")
        return []
    proxies = [
        p for p in (_parse_proxy_line(ln) for ln in path.read_text().splitlines()) if p
    ]
    logger.info(f"Loaded {len(proxies)} sticky Ohio proxy session(s) from {path.name}")
    return proxies


def _session_id(proxy: dict | None) -> str:
    if not proxy:
        return "no-proxy"
    m = re.search(r"session-(\d+)", proxy.get("password", "") or "")
    return m.group(1) if m else "unknown"


def _is_waf_error(exc: Exception) -> bool:
    msg = str(exc)
    return "WAF rejected" in msg or "F5 challenge" in msg


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def create_browser(playwright_instance, proxy: dict | None = None):
    """Launch Chromium with a persistent profile, stealth patches, optional proxy."""
    user_data_dir = tempfile.mkdtemp(prefix="puco_browser_")
    launch_kwargs = dict(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--proxy-bypass-list={PROXY_BYPASS}",
        ],
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    if proxy:
        launch_kwargs["proxy"] = {
            "server": proxy["server"],
            "username": proxy.get("username"),
            "password": proxy.get("password"),
        }
    context = playwright_instance.chromium.launch_persistent_context(
        user_data_dir, **launch_kwargs
    )
    context.add_init_script(STEALTH_INIT_SCRIPT)
    return context, user_data_dir


def _wait_for_challenge(page: Page, timeout: float = 15):
    """If the page shows an F5 JS challenge, wait for it to resolve."""
    content = page.content()
    if "bobcmn" not in content:
        return content

    logger.info("  JS challenge detected, waiting for resolution...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        content = page.content()
        if "bobcmn" not in content:
            return content

    page.reload(wait_until="networkidle", timeout=30_000)
    time.sleep(3)
    return page.content()


def navigate(page: Page, url: str, wait_seconds: float = 3, max_retries: int = 5) -> str:
    """Navigate to *url*, handle F5 JS challenge, return page HTML."""
    for attempt in range(1, max_retries + 1):
        page.goto(url, wait_until="networkidle", timeout=30_000)
        time.sleep(wait_seconds)

        content = _wait_for_challenge(page)

        if "The requested URL was rejected" in content:
            if attempt < max_retries:
                wait = 10 * attempt
                logger.warning(f"  WAF rejected (attempt {attempt}/{max_retries}), retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"WAF rejected request to {url} after {max_retries} attempts")

        if "bobcmn" not in content:
            return content

    raise RuntimeError(f"Could not pass F5 challenge for {url} after {max_retries} attempts")


def warmup_session(page: Page):
    """Visit the DIS CaseSearch page to establish WAF cookies."""
    logger.info("Warming up browser session...")
    try:
        page.goto(f"{BASE_URL}/CaseSearch.aspx", wait_until="networkidle", timeout=30_000)
        time.sleep(3)
        _wait_for_challenge(page)
        logger.info("  Session ready.")
    except Exception as e:
        logger.warning(f"  Warmup warning: {e} (continuing anyway)")


# ---------------------------------------------------------------------------
# PDF download & text extraction
# ---------------------------------------------------------------------------

def download_pdf(page: Page, cmid: str) -> bytes | None:
    """Download a PDF by CMID using an in-browser fetch().

    Returns the raw PDF bytes, or None on failure.
    """
    url = f"{BASE_URL}/ViewImage.aspx?CMID={cmid}"
    try:
        result = page.evaluate(FETCH_BINARY_JS, url)
    except Exception as e:
        logger.error(f"    fetch() error for CMID {cmid}: {e}")
        return None

    if not result.get("ok"):
        logger.warning(f"    HTTP {result.get('status')} for CMID {cmid}")
        return None

    data = base64.b64decode(result["base64"])

    if data[:5] != b"%PDF-":
        if b"rejected" in data[:500].lower() or b"bobcmn" in data[:500]:
            logger.warning(f"    WAF blocked PDF download for CMID {cmid}")
        else:
            logger.warning(f"    Unexpected content for CMID {cmid} (not a PDF, {len(data)} bytes)")
        return None

    return data


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract all text from a PDF byte string using pdfplumber."""
    pages_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in pdf.pages:
            text = pg.extract_text()
            if text:
                pages_text.append(text)
    return "\n\n".join(pages_text)


# ---------------------------------------------------------------------------
# Docket extraction helpers
# ---------------------------------------------------------------------------

def extract_case_metadata(page: Page) -> dict:
    """Pull case-level metadata from the case record page."""
    def _text(sel: str) -> str:
        el = page.query_selector(sel)
        return el.inner_text().strip() if el else ""

    return {
        "case_number": _text("#ContentPlaceHolderMaster_lblsearchcaseno, #ContentPlaceHolderMaster_mlblCase"),
        "case_title": _text("#ContentPlaceHolderMaster_mlblCaseDesc"),
        "status": _text("#ContentPlaceHolderMaster_mlblStatus"),
        "industry": _text("#ContentPlaceHolderMaster_mlblIndustry"),
        "purpose": _text("#ContentPlaceHolderMaster_mlblPurpose"),
        "date_opened": _text("#ContentPlaceHolderMaster_mlblOpenedDate"),
        "date_closed": _text("#ContentPlaceHolderMaster_mlblClosedDate"),
    }


def extract_docket_entries_from_page(page: Page) -> list:
    """Extract docket entries from the currently visible table page."""
    table = page.query_selector("#ContentPlaceHolderMaster_gvDocketInformation")
    if not table:
        return []

    entries = []
    rows = table.query_selector_all("tr")
    for row in rows[1:]:  # skip header
        cells = row.query_selector_all("td")
        if len(cells) < 3:
            continue

        date_cell = cells[0]
        date_filed = date_cell.inner_text().strip()
        summary = cells[1].inner_text().strip()
        link_el = date_cell.query_selector("a[href*='DocumentRecord']")
        doc_link = link_el.get_attribute("href") if link_el else None
        if doc_link and not doc_link.startswith("http"):
            doc_link = f"{BASE_URL}/{doc_link}"
        page_count = cells[2].inner_text().strip()

        doc_id = None
        if doc_link:
            m = re.search(r"DocID=([a-f0-9-]+)", doc_link, re.I)
            if m:
                doc_id = m.group(1)

        entries.append({
            "date_filed": date_filed,
            "summary": summary,
            "pages": page_count,
            "doc_id": doc_id,
            "document_record_url": doc_link,
        })
    return entries


def extract_all_docket_entries(page: Page) -> list:
    """Paginate through the docket table and collect all entries."""
    all_entries = []

    total_el = page.query_selector("#ContentPlaceHolderMaster_TotalPages")
    total_pages = int(total_el.inner_text().strip()) if total_el else 1
    logger.info(f"  Docket has {total_pages} page(s) of entries")

    entries = extract_docket_entries_from_page(page)
    all_entries.extend(entries)
    logger.info(f"  Page 1: {len(entries)} entries")

    for pg in range(2, total_pages + 1):
        next_btn = page.query_selector("#ContentPlaceHolderMaster_NextPage")
        if not next_btn:
            logger.warning(f"  No 'Next' button found, stopping at page {pg - 1}")
            break
        next_btn.click()
        page.wait_for_load_state("networkidle", timeout=15_000)
        time.sleep(2)

        entries = extract_docket_entries_from_page(page)
        all_entries.extend(entries)
        logger.info(f"  Page {pg}: {len(entries)} entries")

    return all_entries


def extract_cmids_from_document_page(page: Page, doc_url: str) -> list:
    """Visit a DocumentRecord page and return the list of unique CMIDs."""
    navigate(page, doc_url)
    content = page.content()
    return list(dict.fromkeys(re.findall(r"ViewImage\.aspx\?CMID=([A-Z0-9]+)", content)))


# ---------------------------------------------------------------------------
# State management (incremental mode)
# ---------------------------------------------------------------------------

def _state_path(case_no: str) -> Path:
    return Path(__file__).parent / f"puco_docket_{case_no.replace('-', '_')}.json"


def _text_path(case_no: str) -> Path:
    return Path(__file__).parent / f"puco_docket_{case_no.replace('-', '_')}_text.json"


def load_previous_state(case_no: str) -> dict | None:
    """Load the JSON from a previous run, or None if no prior run exists."""
    path = _state_path(case_no)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def get_known_doc_ids(state: dict) -> set:
    """Return the set of doc_ids already processed in a previous run."""
    return {e["doc_id"] for e in state.get("entries", []) if e.get("doc_id")}


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape_case(
    case_no: str = DEFAULT_CASE_NO,
    download_pdfs: bool = True,
    request_delay: float = 2.0,
    pdf_output_dir: Path | None = None,
    force_full: bool = False,
) -> dict:
    """
    Scrape docket entries for a PUCO case.  Incremental by default.

    On the first run (or with ``force_full=True``) every entry is downloaded.
    On subsequent runs only *new* doc_ids are fetched; existing entries are
    preserved from the previous JSON.

    Args:
        case_no: The PUCO case number (e.g. "26-0435-EL-MER").
        download_pdfs: If True, download each document PDF and extract text.
        request_delay: Seconds to wait between requests.
        pdf_output_dir: Directory to save PDFs.  Defaults to ./pdfs_{case_no}/.
        force_full: If True, ignore previous state and re-download everything.
    """
    case_url = f"{BASE_URL}/CaseRecord.aspx?CaseNo={case_no}"
    if pdf_output_dir is None:
        pdf_output_dir = Path(__file__).parent / f"pdfs_{case_no.replace('-', '_')}"

    # --- Load previous state for incremental mode ---
    previous = None if force_full else load_previous_state(case_no)
    known_ids = get_known_doc_ids(previous) if previous else set()
    if known_ids:
        logger.info(f"Incremental mode: {len(known_ids)} entries already on file.")

    # --- Also load previously extracted text so we can merge ---
    prev_text_by_id = {}
    if previous and not force_full:
        tp = _text_path(case_no)
        if tp.exists():
            with open(tp) as f:
                for te in json.load(f):
                    if te.get("doc_id"):
                        prev_text_by_id[te["doc_id"]] = te

    def _run_session(pw, proxy: dict | None):
        """One full scrape attempt through *proxy*. Raises on WAF block."""
        context, user_data_dir = create_browser(pw, proxy)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # --- Warmup ---
            warmup_session(page)

            # --- Case record page ---
            logger.info(f"Fetching case record: {case_url}")
            navigate(page, case_url)

            metadata = extract_case_metadata(page)
            logger.info(f"  Case: {metadata.get('case_title', 'N/A')}")
            logger.info(f"  Status: {metadata.get('status', 'N/A')}")

            # --- Docket entries (with pagination) ---
            logger.info("Extracting docket entries...")
            entries = extract_all_docket_entries(page)
            logger.info(f"  Total entries on docket: {len(entries)}")

            # --- Determine which entries are new ---
            new_entries = [e for e in entries if e.get("doc_id") not in known_ids]
            old_entries = [e for e in entries if e.get("doc_id") in known_ids]

            if not new_entries:
                logger.info("No new entries found.")
            else:
                logger.info(f"  New entries to process: {len(new_entries)}")

            # --- Carry forward old entry metadata from previous state ---
            prev_entries_by_id = {}
            if previous:
                for pe in previous.get("entries", []):
                    if pe.get("doc_id"):
                        prev_entries_by_id[pe["doc_id"]] = pe

            for e in old_entries:
                prev = prev_entries_by_id.get(e["doc_id"], {})
                e["cmids"] = prev.get("cmids")
                e["pdf_file"] = prev.get("pdf_file")
                e["text_length"] = prev.get("text_length", 0)

            # --- Fetch document details + PDFs for NEW entries only ---
            if download_pdfs and new_entries:
                pdf_output_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"Downloading {len(new_entries)} new PDF(s) to {pdf_output_dir}/ ...")

                doc_page = context.new_page()
                pdf_stats = {"downloaded": 0, "failed": 0, "total_bytes": 0}

                for i, entry in enumerate(new_entries):
                    if not entry.get("document_record_url"):
                        continue

                    logger.info(f"  [NEW {i+1}/{len(new_entries)}] {entry['summary'][:65]}...")

                    # Step 1: Get CMIDs
                    try:
                        cmids = extract_cmids_from_document_page(
                            doc_page, entry["document_record_url"]
                        )
                    except Exception as e:
                        logger.error(f"    Error loading document page: {e}")
                        entry["extracted_text"] = ""
                        entry["pdf_file"] = None
                        continue

                    entry["cmids"] = cmids
                    if not cmids:
                        logger.warning("    No CMIDs found")
                        entry["extracted_text"] = ""
                        entry["pdf_file"] = None
                        continue

                    # Step 2: Download PDF
                    cmid = cmids[0]
                    pdf_bytes = download_pdf(page, cmid)

                    if pdf_bytes:
                        pdf_filename = f"{entry['doc_id'] or cmid}.pdf"
                        pdf_path = pdf_output_dir / pdf_filename
                        pdf_path.write_bytes(pdf_bytes)
                        entry["pdf_file"] = str(pdf_path)
                        pdf_stats["downloaded"] += 1
                        pdf_stats["total_bytes"] += len(pdf_bytes)

                        # Step 3: Extract text
                        try:
                            text = extract_text_from_pdf(pdf_bytes)
                            entry["extracted_text"] = text
                            logger.info(f"    OK: {len(pdf_bytes):,} bytes, "
                                        f"{len(text):,} chars extracted")
                        except Exception as e:
                            logger.warning(f"    PDF saved but text extraction failed: {e}")
                            entry["extracted_text"] = ""
                    else:
                        pdf_stats["failed"] += 1
                        entry["extracted_text"] = ""
                        entry["pdf_file"] = None

                    time.sleep(request_delay)

                doc_page.close()
                logger.info(f"PDF download summary: "
                            f"{pdf_stats['downloaded']} new downloaded, "
                            f"{pdf_stats['failed']} failed, "
                            f"{pdf_stats['total_bytes']:,} bytes")

            return metadata, entries, new_entries

        finally:
            context.close()
            shutil.rmtree(user_data_dir, ignore_errors=True)

    # --- Sticky-session proxies: shuffle so each run/attempt uses a fresh IP ---
    proxies = load_sticky_proxies()
    random.shuffle(proxies)
    attempts = max(1, min(MAX_PROXY_ROTATIONS, len(proxies) or 1))

    with sync_playwright() as pw:
        last_exc: Exception | None = None
        metadata = entries = new_entries = None
        for attempt in range(1, attempts + 1):
            proxy = proxies[attempt - 1] if proxies else None
            logger.info(
                f"[Attempt {attempt}/{attempts}] proxy session={_session_id(proxy)}"
            )
            try:
                metadata, entries, new_entries = _run_session(pw, proxy)
                break
            except RuntimeError as e:
                last_exc = e
                if _is_waf_error(e) and attempt < attempts:
                    logger.warning(
                        f"WAF blocked session={_session_id(proxy)}; "
                        f"rotating to a fresh residential IP..."
                    )
                    continue
                raise

    return {
        "case_number": case_no,
        "case_url": case_url,
        "metadata": metadata,
        "entries": entries,          # full list (old + new), in docket order
        "new_entries": new_entries,
        "total_entries": len(entries),
        "prev_text_by_id": prev_text_by_id,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_results(result: dict, do_pdfs: bool):
    """Write the main JSON and text JSON, merging old + new text."""
    case_no = result["case_number"]
    output_path = _state_path(case_no)
    prev_text_by_id = result.get("prev_text_by_id", {})

    # --- Main JSON (no inline text — too large) ---
    entries_for_json = []
    for e in result["entries"]:
        entry_copy = dict(e)
        text = entry_copy.pop("extracted_text", "")
        entry_copy.pop("prev_text_by_id", None)
        entry_copy["text_length"] = len(text) if text else entry_copy.get("text_length", 0)
        entries_for_json.append(entry_copy)

    json_output = {
        "case_number": result["case_number"],
        "case_url": result["case_url"],
        "metadata": result["metadata"],
        "total_entries": result["total_entries"],
        "last_checked": datetime.now().isoformat(),
        "entries": entries_for_json,
    }

    with open(output_path, "w") as f:
        json.dump(json_output, f, indent=2)
    logger.info(f"Saved {result['total_entries']} entries to {output_path}")

    # --- Text JSON: merge previous text with any new text ---
    if do_pdfs:
        text_path = _text_path(case_no)

        # Build merged text list in docket order
        text_entries = []
        for e in result["entries"]:
            doc_id = e.get("doc_id")
            new_text = e.get("extracted_text", "")
            if new_text:
                # New or re-downloaded entry
                text_entries.append({
                    "doc_id": doc_id,
                    "date_filed": e["date_filed"],
                    "summary": e["summary"],
                    "extracted_text": new_text,
                })
            elif doc_id in prev_text_by_id:
                # Carry forward from previous run
                text_entries.append(prev_text_by_id[doc_id])

        with open(text_path, "w") as f:
            json.dump(text_entries, f, indent=2)
        logger.info(f"Saved extracted text for {len(text_entries)} documents to {text_path}")

    # --- Report new entries ---
    new = result.get("new_entries", [])
    if new:
        logger.info(f"--- {len(new)} NEW ENTRY/ENTRIES ---")
        for e in new:
            logger.info(f"  {e['date_filed']}  {e['summary'][:80]}")


def main():
    refresh_script_log(logger, _get_log_file)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    case_no = args[0] if args else DEFAULT_CASE_NO
    do_pdfs = "--no-pdf" not in flags
    force_full = "--full" in flags

    # Parse --delay N
    request_delay = 2.0
    for i, f in enumerate(flags):
        if f == "--delay" and i + 1 < len(sys.argv[1:]):
            try:
                request_delay = float(sys.argv[sys.argv.index(f) + 2])
            except (ValueError, IndexError):
                pass

    logger.info(f"Starting Ohio PUC scrape: case={case_no}, "
                f"pdfs={do_pdfs}, full={force_full}, delay={request_delay}s")
    try:
        result = scrape_case(
            case_no,
            download_pdfs=do_pdfs,
            request_delay=request_delay,
            force_full=force_full,
        )
        save_results(result, do_pdfs)
        logger.info("Ohio PUC scrape finished.")
    except Exception:
        logger.exception("Ohio PUC scrape failed with an unhandled error")
        raise


if __name__ == "__main__":
    main()
