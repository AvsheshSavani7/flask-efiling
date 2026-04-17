#!/usr/bin/env python3
"""
Playwright scraper for EC Competition Foreign Subsidies (FS) case detail pages.

Saves full HTML of each case detail page into fs_html/ as {case_num}.html,
then parses all saved HTML files into structured JSON and computes the
superset of all fields encountered across every case.

Run:
    python fs_html_scraper.py
    python fs_html_scraper.py --max-pages 3
    python fs_html_scraper.py --headed
    python fs_html_scraper.py --parse-only
    python fs_html_scraper.py --json-output fs_cases.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

START_URL = (
    "https://competition-cases.ec.europa.eu/search"
    "?caseInstrument=InstrumentFS&caseLastDecisionDate=None"
    "&pageSize=50&sortField=caseLastDecisionDate&sortOrder=DESC"
)

WAIT_SELECTORS = [
    "a[href*='/cases/']",
    "main a[href*='/cases/']",
    "[role='main'] a[href*='/cases/']",
]

NEXT_BUTTON_SELECTORS = [
    "button[aria-label*='Next']",
    "a[aria-label*='Next']",
    "button:has-text('Next')",
    "a:has-text('Next')",
]

SPA_CONTENT_INDICATORS = [
    "text=Companies:",
    "text=Case type:",
    "text=Regulation:",
    "text=Notification date:",
    "text=Last decision date:",
    "text=Initiation date:",
    "text=Investigation phase:",
]

COOKIE_ACCEPT_SELECTORS = [
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept all')",
    "button[id*='cookie'] >> text=Accept",
]


# ---------------------------------------------------------------------------
# HTML text extractor
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _SKIP_TAGS = frozenset(["style", "script", "noscript"])

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.texts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.texts.append(text)


def _extract_visible_texts(html: str) -> List[str]:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.texts


def _slice_case_section(texts: List[str]) -> List[str]:
    start = end = None
    for i, t in enumerate(texts):
        if "Competition case search" in t and start is None:
            start = i + 1
        if start is not None and t.strip() == "Competition Policy":
            end = i
            break
    if start is None:
        return []
    return texts[start:end] if end else texts[start:]


# All known labels that the EC SPA renders for summary fields (FS + Merger)
_LABEL_MAP = {
    "companies:": "companies",
    "last decision date:": "last_decision_date",
    "case type:": "case_type",
    "investigation phase:": "investigation_phase",
    "regulation:": "regulation",
    "notification date:": "notification_date",
    "initiation date:": "initiation_date",
    "provisional deadline:": "provisional_deadline",
    "economic activities:": "economic_activities",
    "simplified procedure:": "simplified_procedure",
    "case notified under:": "case_notified_under",
}

_SUMMARY_LABELS = set(_LABEL_MAP.keys())

_NACE_RE = re.compile(r"^\(NACE Rev\.")
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_ART_RE = re.compile(r"^Art\.\s")
_EU_LANGUAGES = frozenset([
    "EN", "FR", "DE", "ES", "IT", "NL", "PT", "DA", "FI", "SV", "EL",
    "CS", "HU", "PL", "RO", "BG", "HR", "SK", "SL", "LT", "LV", "ET",
    "GA", "MT",
])


def parse_case_html(html: str, case_num: str) -> Dict[str, Any]:
    """Parse a single FS case detail page HTML into a structured dict."""
    texts = _extract_visible_texts(html)
    section = _slice_case_section(texts)
    if not section:
        return {"case_number": case_num, "error": "Could not locate case section"}

    record: Dict[str, Any] = {
        "case_number": case_num,
        "case_url": f"https://competition-cases.ec.europa.eu/cases/{case_num}",
    }

    idx = 0

    while idx < len(section) and section[idx] == case_num:
        idx += 1

    if idx < len(section):
        record["instrument"] = section[idx]
        idx += 1

    status = None
    if idx < len(section):
        token = section[idx]
        if token.lower() not in _SUMMARY_LABELS and "subscribe" not in token.lower():
            status = token
            idx += 1
    record["status"] = status

    if idx < len(section) and "subscribe" in section[idx].lower():
        idx += 1

    title_parts = []
    while idx < len(section) and section[idx].lower() not in _SUMMARY_LABELS:
        title_parts.append(section[idx])
        idx += 1
    record["case_title"] = " ".join(title_parts) if title_parts else None

    # --- Summary label-value pairs ---
    companies: List[str] = []
    economic_activities: List[str] = []
    current_label = None

    while idx < len(section):
        token = section[idx]
        low = token.lower()

        if low in _SUMMARY_LABELS:
            current_label = _LABEL_MAP[low]
            idx += 1
            continue

        if low in ("decisions", "other case related information"):
            break

        if current_label == "companies":
            if token not in ("|", "|"):
                companies.append(token)
            idx += 1
            continue

        if current_label == "economic_activities":
            if _NACE_RE.match(token):
                if economic_activities:
                    economic_activities[-1] += f" {token}"
            else:
                economic_activities.append(token)
            idx += 1
            continue

        if current_label:
            record[current_label] = token
            current_label = None
            idx += 1
            continue

        idx += 1

    record["companies"] = companies if companies else None
    record["economic_activities"] = economic_activities if economic_activities else None

    # --- Decisions section ---
    decisions: List[Dict[str, Any]] = []
    if idx < len(section) and section[idx].lower() == "decisions":
        idx += 1
        current_decision: Optional[Dict[str, Any]] = None

        while idx < len(section):
            token = section[idx]
            low = token.lower()

            if low == "other case related information":
                break

            if _ART_RE.match(token) or low == "withdrawn":
                if current_decision:
                    decisions.append(current_decision)
                current_decision = {"decision_type": token}
                idx += 1
                continue

            if current_decision is not None:
                if token == "of" and (idx + 1) < len(section) and _DATE_RE.match(section[idx + 1]):
                    current_decision["decision_date"] = section[idx + 1]
                    idx += 2
                    continue

                if low.startswith("decision text"):
                    texts_list: List[Dict[str, str]] = []
                    idx += 1
                    while idx < len(section):
                        t = section[idx]
                        if (_ART_RE.match(t) or t.lower() == "withdrawn"
                                or t.lower().startswith("press")
                                or t.lower() == "other case related information"):
                            break
                        if t in _EU_LANGUAGES:
                            entry: Dict[str, str] = {"lang": t}
                            if ((idx + 1) < len(section)
                                    and section[idx + 1] == "published on"
                                    and (idx + 2) < len(section)):
                                entry["published_on"] = section[idx + 2]
                                idx += 3
                            else:
                                idx += 1
                            texts_list.append(entry)
                            continue
                        idx += 1
                    current_decision["decision_texts"] = texts_list
                    continue

                if low.startswith("press communication"):
                    idx += 1
                    if idx < len(section):
                        press: Dict[str, str] = {"ref": section[idx]}
                        idx += 1
                        if (idx < len(section) and section[idx] == "of"
                                and (idx + 1) < len(section)):
                            press["date"] = section[idx + 1]
                            idx += 2
                        current_decision["press_communication"] = press
                    continue

                if (low.startswith("publication in the oj")
                        or low.startswith("prior publication in the oj")):
                    label_key = "prior_publication_oj" if "prior" in low else "publication_oj"
                    idx += 1
                    if idx < len(section):
                        pub: Dict[str, str] = {"ref": section[idx]}
                        idx += 1
                        if (idx < len(section) and section[idx] == "of"
                                and (idx + 1) < len(section)):
                            pub["date"] = section[idx + 1]
                            idx += 2
                        current_decision.setdefault(label_key, []).append(pub)
                    continue

            idx += 1

        if current_decision:
            decisions.append(current_decision)

    record["decisions"] = decisions if decisions else None

    # --- Other case related information ---
    other_info: List[Dict[str, Any]] = []
    if idx < len(section) and section[idx].lower() == "other case related information":
        idx += 1

        while idx < len(section):
            token = section[idx]
            low = token.lower()

            if (low.startswith("publication in the oj")
                    or low.startswith("prior publication in the oj")):
                label_key = "prior_publication_oj" if "prior" in low else "publication_oj"
                idx += 1
                if idx < len(section):
                    pub = {"type": label_key, "ref": section[idx]}
                    idx += 1
                    if (idx < len(section) and section[idx] == "of"
                            and (idx + 1) < len(section)):
                        pub["date"] = section[idx + 1]
                        idx += 2
                    other_info.append(pub)
                continue

            if low.startswith("description of the concentration"):
                entry_desc: Dict[str, Any] = {
                    "type": "description_of_concentration"}
                idx += 1
                if (idx < len(section) and section[idx] == "of"
                        and (idx + 1) < len(section)):
                    entry_desc["date"] = section[idx + 1]
                    idx += 2
                if idx < len(section) and section[idx] == ":":
                    idx += 1
                langs: List[Dict[str, str]] = []
                while idx < len(section):
                    t = section[idx]
                    if t in _EU_LANGUAGES:
                        lang_entry: Dict[str, str] = {"lang": t}
                        if ((idx + 1) < len(section)
                                and section[idx + 1] == "published on"
                                and (idx + 2) < len(section)):
                            lang_entry["published_on"] = section[idx + 2]
                            idx += 3
                        else:
                            idx += 1
                        langs.append(lang_entry)
                        continue
                    break
                if langs:
                    entry_desc["languages"] = langs
                other_info.append(entry_desc)
                continue

            if not _DATE_RE.match(token) and token != "of":
                other_info.append({"type": "note", "text": token})

            idx += 1

    record["other_case_related_information"] = other_info if other_info else None

    return record


def parse_all_html_files(html_dir: Path) -> List[Dict[str, Any]]:
    """Parse all .html files in a directory into structured records."""
    records: List[Dict[str, Any]] = []
    html_files = sorted(html_dir.glob("*.html"))

    if not html_files:
        print(f"[WARN] No HTML files found in {html_dir}/", flush=True)
        return records

    for fpath in html_files:
        case_num = fpath.stem
        html = fpath.read_text(encoding="utf-8")
        try:
            record = parse_case_html(html, case_num)
            records.append(record)
            print(
                f"[PARSED] {case_num}: {record.get('case_title', '?')}", flush=True)
        except Exception as exc:
            print(f"[ERROR] Failed to parse {fpath.name}: {exc}", flush=True)
            records.append({"case_number": case_num, "error": str(exc)})

    return records


def compute_field_superset(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build a superset of every field name seen across all records,
    with sample values and occurrence counts.
    """
    field_stats: Dict[str, Dict[str, Any]] = {}

    for record in records:
        _collect_fields(record, "", field_stats)

    summary: Dict[str, Any] = {}
    for field_path, stats in sorted(field_stats.items()):
        summary[field_path] = {
            "occurrences": stats["count"],
            "sample_values": stats["samples"][:5],
            "types_seen": list(stats["types"]),
        }

    return summary


def _collect_fields(
    obj: Any,
    prefix: str,
    stats: Dict[str, Dict[str, Any]],
) -> None:
    """Recursively collect field paths from a dict/list structure."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            entry = stats.setdefault(
                path, {"count": 0, "samples": [], "types": set()})
            entry["count"] += 1
            entry["types"].add(type(val).__name__)
            if val is not None and len(entry["samples"]) < 5:
                sample = val if not isinstance(
                    val, (dict, list)) else f"<{type(val).__name__}>"
                if sample not in entry["samples"]:
                    entry["samples"].append(sample)
            _collect_fields(val, path, stats)
    elif isinstance(obj, list):
        for item in obj:
            _collect_fields(item, prefix + "[]", stats)


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

def extract_case_num(url: str) -> Optional[str]:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    values = qs.get("proc_code")
    if values:
        return values[0]
    match = re.search(r"/cases/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def dismiss_cookie_banner(page) -> None:
    for selector in COOKIE_ACCEPT_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=3000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def wait_for_results(page, timeout_ms: int = 30000) -> str:
    last_error = None
    for selector in WAIT_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            return selector
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not find result links on page. Last error: {last_error}")


def collect_case_links(page, selector: str) -> List[Dict[str, str]]:
    links = page.locator(selector)
    count = links.count()
    results: List[Dict[str, str]] = []
    seen: Set[str] = set()

    for i in range(count):
        item = links.nth(i)
        href = item.get_attribute("href")
        if not href or "/cases/" not in href:
            continue
        if not href.startswith("http"):
            href = f"https://competition-cases.ec.europa.eu{href}"
        if href in seen:
            continue
        seen.add(href)
        results.append({"url": href})

    return results


def wait_for_spa_content(page, timeout_s: int = 15) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for indicator in SPA_CONTENT_INDICATORS:
            try:
                loc = page.locator(indicator).first
                if loc.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        page.wait_for_timeout(500)
    return False


def save_case_html(context, url: str, output_dir: Path) -> Optional[str]:
    """Open a new tab, wait for SPA render, save full HTML, close tab."""
    case_num = extract_case_num(url)
    if not case_num:
        print(f"[SKIP] Cannot determine case number from {url}", flush=True)
        return None

    filename = f"{case_num}.html"
    filepath = output_dir / filename

    if filepath.exists():
        print(f"[SKIP] Already saved: {filename}", flush=True)
        return filename

    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        dismiss_cookie_banner(page)

        spa_loaded = wait_for_spa_content(page, timeout_s=15)
        if not spa_loaded:
            page.wait_for_timeout(3000)

        html = page.content()
        filepath.write_text(html, encoding="utf-8")
        print(f"[OK] Saved {filename} ({len(html):,} bytes)", flush=True)
        return filename
    except Exception as exc:
        print(f"[ERROR] {case_num}: {exc}", flush=True)
        return None
    finally:
        page.close()


def click_next_page(page) -> bool:
    for selector in NEXT_BUTTON_SELECTORS:
        locator = page.locator(selector)
        count = locator.count()
        for i in range(count):
            btn = locator.nth(i)
            try:
                if not btn.is_visible():
                    continue
                aria_disabled = (btn.get_attribute(
                    "aria-disabled") or "").lower()
                disabled = btn.get_attribute("disabled")
                if aria_disabled == "true" or disabled is not None:
                    continue
                btn.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


def scrape(start_url: str, max_pages: Optional[int], headed: bool, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_count = 0
    visited_urls: Set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        search_page = context.new_page()

        search_page.goto(
            start_url, wait_until="domcontentloaded", timeout=60000)
        search_page.wait_for_timeout(3000)
        dismiss_cookie_banner(search_page)
        selector = wait_for_results(search_page)

        current_page = 1
        while True:
            print(
                f"\n[INFO] Search page {current_page}: collecting case links...",
                flush=True,
            )
            search_page.wait_for_timeout(1000)
            links = collect_case_links(search_page, selector)
            print(f"[INFO] Found {len(links)} case links", flush=True)

            for item in links:
                url = item["url"]
                if url in visited_urls:
                    continue
                visited_urls.add(url)

                result = save_case_html(context, url, output_dir)
                if result:
                    saved_count += 1

            if max_pages is not None and current_page >= max_pages:
                break

            if not click_next_page(search_page):
                break

            selector = wait_for_results(search_page)
            current_page += 1

        context.close()
        browser.close()

    return saved_count


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scrape EC Foreign Subsidies case detail pages, "
                    "save HTML, parse to JSON, and compute field superset.")
    ap.add_argument("--url", default=START_URL, help="Search URL to scrape")
    ap.add_argument("--output-dir", default="fs_html",
                    help="Folder for saved HTML files (default: fs_html)")
    ap.add_argument("--json-output", default="fs_cases.json",
                    help="JSON output file (default: fs_cases.json)")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Optional page limit for scraping")
    ap.add_argument("--headed", action="store_true",
                    help="Run browser in visible mode")
    ap.add_argument("--parse-only", action="store_true",
                    help="Skip scraping, just parse existing HTML files to JSON")
    args = ap.parse_args()

    output_dir = Path(args.output_dir)

    if not args.parse_only:
        saved = scrape(args.url, args.max_pages, args.headed, output_dir)
        print(f"\n[DONE] Saved {saved} HTML files to {output_dir}/")

    print(f"\n[INFO] Parsing HTML files in {output_dir}/ ...")
    records = parse_all_html_files(output_dir)

    if records:
        json_path = Path(args.json_output)
        json_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[DONE] Wrote {len(records)} records to {json_path}")

        print(
            f"\n[INFO] Computing field superset across {len(records)} cases...")
        superset = compute_field_superset(records)
        superset_path = Path(args.json_output).with_suffix(".fields.json")
        superset_path.write_text(
            json.dumps(superset, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[DONE] Field superset ({len(superset)} unique field paths) "
              f"saved to {superset_path}")

        print("\n--- FIELD SUPERSET SUMMARY ---")
        for field_path, info in superset.items():
            samples = ", ".join(str(s) for s in info["sample_values"][:3])
            print(f"  {field_path:45s}  "
                  f"({info['occurrences']:>3d} occurrences)  "
                  f"samples: {samples}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
