import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from mongodb_connection import init_mongodb_connection, get_database, is_connected


# Load environment variables
load_dotenv(".env")

# Constants
ENV_PATH = ".env"
LIST_URL = (
    "https://www.accc.gov.au/public-registers/mergers-and-acquisitions-registers/"
    "acquisitions-register"
    "?f[0]=acccgov_merger_matter_status:under_assessment&items_per_page=50"
)
BACKUP_JSON = "accc_cases_register_backup.json"


def get_accc_cases_collection():
    """
    Get or create the 'accc_cases' collection in the current MongoDB database.
    """
    db = get_database()
    if db is None:
        return None
    return db["accc_cases"]


def parse_list_items(html_content: str) -> List[Dict[str, Any]]:
    """
    Parse the acquisitions register list HTML into a list of item dicts.
    Only "Under assessment" items are expected on this URL, but we still
    record the acquisition_status from the page.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select(".views-row")

    items: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        try:
            item: Dict[str, Any] = {}

            # Title
            title_elem = row.select_one("h3")
            if title_elem:
                item["title"] = title_elem.get_text(strip=True)

            # Detail URL
            link_elem = row.select_one(
                "a[href*='/public-registers/mergers-and-acquisitions-registers/acquisitions-register/']"
            )
            if link_elem and link_elem.get("href"):
                href = link_elem["href"]
                if href and not href.startswith("http"):
                    item["url"] = "https://www.accc.gov.au" + href
                else:
                    item["url"] = href

            # Acquisition status
            status_elem = row.select_one(
                ".field--name-field-acccgov-merger-status .field__item"
            )
            if status_elem:
                item["acquisition_status"] = status_elem.get_text(strip=True)

            # Type
            type_elem = row.select_one(".field--acccgov-type .field__item")
            if type_elem:
                item["type"] = type_elem.get_text(strip=True)

            # Case number
            case_number_elem = row.select_one(
                ".field--name-field-acccgov-mcmsmergermatterno .field__item"
            )
            if case_number_elem:
                item["case_number"] = case_number_elem.get_text(strip=True)

            # Effective notification date (or waiver application date)
            date_elem = row.select_one(
                ".field--name-field-acccgov-pub-reg-date .field__item time"
            )
            if date_elem:
                item["effective_notification_date"] = date_elem.get_text(
                    strip=True
                )

            if "case_number" in item and "url" in item:
                items.append(item)
        except Exception as e:
            print(f"⚠️ Error parsing list item #{idx + 1}: {e}")
            continue

    print(f"✅ Parsed {len(items)} items from list page")
    return items


def extract_text(element) -> str:
    """Safely get inner text from a Playwright element handle."""
    if not element:
        return ""
    try:
        return element.inner_text().strip()
    except Exception:
        return ""


def extract_detail_page_case(
    page, url: str
) -> Optional[Dict[str, Any]]:
    """
    Open the detail URL and parse it into a structured case_info dict.

    Supports both:
    - Under assessment cases (with Stage + End of determination period)
    - Assessment completed / Waiver cases (with ACCC Determination, Determination publication date)
    """
    try:
        print(f"  📄 Fetching detail page: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        case: Dict[str, Any] = {
            "url": url,
        }

        # Title (page heading)
        try:
            title_elem = page.query_selector(
                "h1.page-title span.field--name-title")
            if title_elem:
                case["title"] = extract_text(title_elem)
        except Exception:
            pass

        # Acquisition status, case number, type, notification/waiver date
        try:
            status_elem = page.query_selector(
                ".field--name-field-acccgov-merger-status .field__item"
            )
            if status_elem:
                case["acquisition_status"] = extract_text(status_elem)

            case_number_elem = page.query_selector(
                ".field--name-dynamic-token-fieldnode-acccgov-merger-id .field__item"
            )
            if case_number_elem:
                case["case_number"] = extract_text(case_number_elem)

            type_elem = page.query_selector(
                ".field--acccgov-type .field__item")
            if type_elem:
                case["type"] = extract_text(type_elem)

            # Effective notification date or Waiver application date
            date_elem = page.query_selector(
                ".field--name-field-acccgov-pub-reg-date .field__item time"
            )
            if date_elem:
                case["effective_notification_date"] = extract_text(date_elem)
        except Exception as e:
            print(f"  ⚠️ Error extracting summary fields: {e}")

        # Status section
        status_info: Dict[str, Any] = {}
        try:
            stage_elem = page.query_selector(
                ".field--name-field-acquisition-stage .field__item"
            )
            if stage_elem:
                status_info["stage"] = extract_text(stage_elem)

            # End of determination period (under assessment)
            end_period_elem = page.query_selector(
                ".field--name-field-acccgov-end-determination .field__item time"
            )
            if end_period_elem:
                status_info["end_of_determination_period"] = extract_text(
                    end_period_elem
                )

            # ACCC Determination + publication date (assessment completed / waiver)
            determination_elem = page.query_selector(
                ".field--name-field-acccgov-acquisition-deter .field__item"
            )
            if determination_elem:
                status_info["accc_determination"] = extract_text(
                    determination_elem)

            pub_date_elem = page.query_selector(
                ".field--name-field-acccgov-pub-reg-end-date .field__item time"
            )
            if pub_date_elem:
                status_info["determination_publication_date"] = extract_text(
                    pub_date_elem
                )
        except Exception as e:
            print(f"  ⚠️ Error extracting status section: {e}")

        if status_info:
            case["status"] = status_info

        # About the acquisition
        about: Dict[str, Any] = {}
        try:
            # Acquirer(s)
            acquirers: List[Dict[str, Any]] = []
            acq_section = page.query_selector(
                ".field--name-field-acccgov-applicants"
            )
            if acq_section:
                company_elements = acq_section.query_selector_all(
                    ".paragraph--type--acccgov-trader"
                )
                for elem in company_elements:
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    c: Dict[str, Any] = {}
                    if name_elem:
                        c["name"] = extract_text(name_elem)
                    if reg_elem:
                        c["registration"] = extract_text(reg_elem)
                    if c:
                        acquirers.append(c)
            if acquirers:
                about["acquirers"] = acquirers

            # Target(s) or Vendor(s)
            targets: List[Dict[str, Any]] = []
            tgt_section = page.query_selector(
                ".field--name-field-acccgov-pub-reg-targets"
            )
            if tgt_section:
                company_elements = tgt_section.query_selector_all(
                    ".paragraph--type--acccgov-trader"
                )
                for elem in company_elements:
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    c: Dict[str, Any] = {}
                    if name_elem:
                        c["name"] = extract_text(name_elem)
                    if reg_elem:
                        c["registration"] = extract_text(reg_elem)
                    if c:
                        targets.append(c)
            if targets:
                about["targets"] = targets

            # Other party(ies)
            others: List[Dict[str, Any]] = []
            other_section = page.query_selector(
                ".field--name-field-acccgov-other-parties"
            )
            if other_section:
                company_elements = other_section.query_selector_all(
                    ".paragraph--type--acccgov-trader"
                )
                for elem in company_elements:
                    name_elem = elem.query_selector(".field_acccgov_name")
                    reg_elem = elem.query_selector(
                        "span:has-text('ACN'), span:has-text('ABN'), span:has-text('BN')"
                    )
                    c: Dict[str, Any] = {}
                    if name_elem:
                        c["name"] = extract_text(name_elem)
                    if reg_elem:
                        c["registration"] = extract_text(reg_elem)
                    if c:
                        others.append(c)
            if others:
                about["other_parties"] = others

            # ANZSIC code(s)
            anzsic_elem = page.query_selector(
                ".field--name-field-acquisition-anzsic-code .field__item"
            )
            if anzsic_elem:
                about["anzsic_codes"] = extract_text(anzsic_elem)

            # Description (full text)
            desc_elem = page.query_selector(
                ".field--name-field-accc-body .full-text, "
                ".field--name-field-accc-body .summary-text"
            )
            if desc_elem:
                # Try expand "read more"
                try:
                    read_more = page.query_selector(
                        ".field--name-field-accc-body .read-toggle"
                    )
                    if read_more:
                        read_more.click()
                        page.wait_for_timeout(500)
                        desc_elem = page.query_selector(
                            ".field--name-field-accc-body .full-text"
                        ) or desc_elem
                except Exception:
                    pass
                about["description"] = extract_text(desc_elem)
        except Exception as e:
            print(f"  ⚠️ Error extracting 'About the acquisition': {e}")

        if about:
            case["about_the_acquisition"] = about

        # Decisions and key events / Consultation
        events: List[Dict[str, Any]] = []
        try:
            # Consultation table (treated as decisions_and_key_events)
            consult_rows = page.query_selector_all(
                ".field--name-field-acccgov-consultations table tbody tr"
            )
            for row in consult_rows:
                try:
                    date_elem = row.query_selector("time")
                    desc_elem = row.query_selector("td:nth-child(2)")
                    link_elem = row.query_selector(
                        "a[href$='.docx'], a[href$='.pdf'], a[href$='.doc']"
                    )
                    ev: Dict[str, Any] = {}
                    if date_elem:
                        ev["date"] = extract_text(date_elem)
                    if desc_elem:
                        ev["description"] = extract_text(desc_elem)
                    if link_elem:
                        href = link_elem.get_attribute("href")
                        if href and not href.startswith("http"):
                            href = "https://www.accc.gov.au" + href
                        ev["attachment_url"] = href
                        size_elem = link_elem.query_selector("span.badge")
                        if size_elem:
                            ev["attachment_size"] = extract_text(size_elem)
                    if ev.get("description"):
                        events.append(ev)
                except Exception:
                    continue

            # Decisions and key events section
            event_rows = page.query_selector_all(
                ".field--name-field-acccgov-merger-events table tbody tr"
            )
            for row in event_rows:
                try:
                    date_elem = row.query_selector(
                        "td.acccgov-timeline__date time"
                    )
                    desc_elem = row.query_selector("td:nth-child(2)")
                    link_elem = row.query_selector(
                        "td.acccgov-timeline__file-link a"
                    )
                    ev = {}
                    if date_elem:
                        ev["date"] = extract_text(date_elem)
                    if desc_elem:
                        ev["description"] = extract_text(desc_elem)
                    if link_elem:
                        href = link_elem.get_attribute("href")
                        if href and not href.startswith("http"):
                            href = "https://www.accc.gov.au" + href
                        ev["attachment_url"] = href
                        size_elem = link_elem.query_selector("span.badge")
                        if size_elem:
                            ev["attachment_size"] = extract_text(size_elem)
                    if ev.get("description"):
                        events.append(ev)
                except Exception:
                    continue
        except Exception as e:
            print(f"  ⚠️ Error extracting decisions/consultation: {e}")

        if events:
            case["decisions_and_key_events"] = events

        # Require a case_number to be useful
        if not case.get("case_number"):
            print("  ⚠️ No case_number found on detail page; skipping")
            return None

        return case
    except Exception as e:
        print(f"  ❌ Error extracting detail page {url}: {e}")
        return None


def case_exists(collection, case_number: str) -> bool:
    """Check if a case with this case_number already exists in accc_cases."""
    try:
        existing = collection.count_documents(
            {"case_number": case_number}, limit=1)
        return existing > 0
    except Exception as e:
        print(f"⚠️ Error checking existing case {case_number}: {e}")
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    """Insert a new case document into the accc_cases collection."""
    try:
        result = collection.insert_one(case_info)
        return str(result.inserted_id)
    except Exception as e:
        print(f"⚠️ Error inserting case {case_info.get('case_number')}: {e}")
        return None


def run_accc_cases_register(test_mode: bool = False):
    """
    Main entrypoint for scraping the ACCC acquisitions register (under assessment)
    and inserting new cases into the 'accc_cases' collection.
    """
    mode_label = "TEST MODE" if test_mode else "LIVE MODE"
    print(f"🚀 Starting ACCC Cases Register scraper ({mode_label})\n")

    # Initialize MongoDB (still connect in test mode so we exercise full path,
    # but skip writes later)
    print("🔌 Initializing MongoDB connection...")
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        print("   MongoDB connection is required. Exiting.")
        return
    print(f"✅ {message}\n")

    if not is_connected():
        print("❌ MongoDB not connected. Exiting.")
        return

    collection = get_accc_cases_collection()
    if collection is None:
        print("❌ Could not access 'accc_cases' collection. Exiting.")
        return

    new_cases: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(
            f"📄 Loading ACCC acquisitions register list page:\n   {LIST_URL}")
        page.goto(LIST_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        try:
            page.wait_for_selector(".views-row", timeout=10000)
            html_content = page.content()
            items = parse_list_items(html_content)
        except Exception as e:
            print(f"⚠️ Error loading list page items: {e}")
            browser.close()
            return

        # Process each list item
        for idx, item in enumerate(items, 1):
            try:
                case_number = item.get("case_number")
                url = item.get("url")
                title = item.get("title", "")

                print(f"\n[{idx}/{len(items)}] Case {case_number}: {title}")

                if not case_number or not url:
                    print("  ⚠️ Missing case_number or url; skipping")
                    continue

                # Step 2: skip if already in accc_cases (for live mode)
                if not test_mode and case_exists(collection, case_number):
                    print("  ⏩ Case already exists in accc_cases; skipping")
                    continue

                # Step 3: fetch and parse detail page into case_info
                case_info = extract_detail_page_case(page, url)
                if not case_info:
                    print("  ⚠️ Could not extract case info; skipping")
                    continue

                # Ensure summary fields from list are preserved if missing from detail
                for key in [
                    "acquisition_status",
                    "type",
                    "effective_notification_date",
                    "title",
                ]:
                    if key not in case_info and key in item:
                        case_info[key] = item[key]

                if test_mode:
                    print(
                        "  🧪 [TEST MODE] Would insert new case into accc_cases"
                    )
                    # Store a copy without any MongoDB _id for JSON backup
                    backup_case = dict(case_info)
                    backup_case.pop("_id", None)
                    new_cases.append(backup_case)
                else:
                    inserted_id = insert_case(collection, case_info)
                    if inserted_id:
                        print(
                            f"  ✅ Inserted new case into accc_cases (id={inserted_id})"
                        )
                        # PyMongo may inject an _id field into case_info; strip it
                        # before adding to the JSON backup list.
                        backup_case = dict(case_info)
                        backup_case.pop("_id", None)
                        new_cases.append(backup_case)
                    else:
                        print("  ⚠️ Insert failed")
            except Exception as e:
                print(f"❌ Error processing list item #{idx}: {e}")
                continue

        browser.close()

    # Backup JSON for new cases in this run
    if new_cases:
        try:
            # Ensure there are no non-JSON-serializable values (e.g. ObjectId)
            serializable_cases: List[Dict[str, Any]] = []
            for c in new_cases:
                d = dict(c)
                if "_id" in d:
                    d["_id"] = str(d["_id"])
                serializable_cases.append(d)

            with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                json.dump(serializable_cases, f, indent=2, ensure_ascii=False)
            print(
                f"\n💾 Saved {len(serializable_cases)} new cases to backup JSON: {BACKUP_JSON}"
            )
        except Exception as e:
            print(f"⚠️ Error writing backup JSON: {e}")

    print("\n🎉 ACCC Cases Register scraper finished")


if __name__ == "__main__":
    # Allow enabling test mode via environment variable for easy CLI testing.
    # Example: ACCC_CASES_TEST_MODE=1 python accc_cases_register.py
    env_flag = os.getenv("ACCC_CASES_TEST_MODE", "").lower()
    test_mode_env = env_flag in ("1", "true", "yes", "y")
    run_accc_cases_register(test_mode=test_mode_env)
