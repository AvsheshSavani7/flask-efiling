"""
NZ Commerce Commission (ComCom) Case Register → nz_cases collection
===================================================================
Scrapes Open cases from the ComCom case register (open_date = last 7 days),
fetches each case detail page, and inserts new cases into MongoDB 'nz_cases'
collection. Skips records whose case number already exists in nz_cases.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from mongodb_connection import get_database, init_mongodb_connection, is_connected

load_dotenv(".env")

# Constants
BASE_URL = "https://www.comcom.govt.nz"
ENV_PATH = ".env"

# List URL: status=Open, open_date = one week ago from today
LIST_URL_TEMPLATE = (
    "https://www.comcom.govt.nz/case-register/"
    "?q=&size=50&filters%5Bstatus%5D=Open&filters%5Bopen_date%5D={open_date}"
)


def make_absolute_url(href: str, base: str = BASE_URL) -> str:
    """Convert relative or protocol-relative href to full ComCom URL."""
    if not href or not href.strip():
        return ""
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    base = base.rstrip("/")
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def get_open_date_one_week_ago() -> str:
    """Return open_date as YYYY-MM-DD for one week ago from today."""
    d = datetime.now().replace(hour=0, minute=0, second=0,
                               microsecond=0) - timedelta(days=7)
    return d.strftime("%Y-%m-%d")


def get_nz_cases_collection():
    """Get the 'nz_cases' collection from the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["nz_cases"]


def extract_list_items_from_html(html_content: str) -> List[Dict[str, Any]]:
    """Extract case cards from the ComCom case register list HTML (ol.filter__results-list). Skip 'Withheld'."""
    soup = BeautifulSoup(html_content, "html.parser")
    list_el = soup.select_one("ol.filter__results-list")
    if not list_el:
        return []

    items = []
    for li in list_el.select("li"):
        try:
            link = li.select_one("a.card__link")
            if not link:
                continue

            title = (link.get_text(strip=True) or "").strip()
            # if not title or title.lower() == "withheld":
            #     continue

            href = link.get("href", "")
            if href and not href.startswith("http"):
                detail_url = BASE_URL.rstrip("/") + "/" + href.lstrip("/")
            else:
                detail_url = href or ""

            status_el = li.select_one(".card__status")
            status = status_el.get_text(strip=True) if status_el else ""

            tag_el = li.select_one(".card__tag")
            tag = tag_el.get_text(strip=True) if tag_el else ""

            outcome = ""
            info_detail = li.select_one(".card__info-detail")
            if info_detail:
                title_span = info_detail.select_one(".card__info-title")
                if title_span and "outcome" in title_span.get_text(strip=True).lower():
                    val = info_detail.select_one("span:not(.card__info-title)")
                    if val:
                        outcome = val.get_text(strip=True)

            items.append({
                "title": title,
                "detail_url": detail_url,
                "status": status,
                "tag": tag,
                "outcome": outcome,
            })
        except Exception as e:
            print(f"   ⚠️ Error parsing list item: {e}")
            continue

    return items


def parse_case_details(soup: BeautifulSoup) -> Dict[str, str]:
    """Parse .case-details__record blocks into a dict (title -> value)."""
    details = {}
    for rec in soup.select(".case-details__record"):
        title_el = rec.select_one(".case-details__record-title")
        value_el = rec.select_one(".case-details__record-value")
        if title_el and value_el:
            key = title_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            if key:
                details[key] = value
    return details


def parse_timeline(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Parse timeline blocks into list of {date, title, status, has_link}."""
    entries = []
    for block in soup.select(".timeline-block"):
        date_el = block.select_one(".timeline-block__timeline-date")
        title_el = block.select_one(".timeline-block__content-title")
        status_el = block.select_one(".timeline-block__content-status")
        link_el = block.select_one(".timeline-block__content-link")
        date_str = " ".join(date_el.get_text(
            strip=True).split()) if date_el else ""
        entries.append({
            "date": date_str,
            "title": title_el.get_text(strip=True) if title_el else "",
            "status": status_el.get_text(strip=True) if status_el else "",
            "has_link": link_el is not None,
        })
    return entries


def fetch_case_detail_page(page, url: str) -> Optional[Dict[str, Any]]:
    """Fetch case detail page and optional sections (documents, media). Returns case detail dict."""
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        try:
            view_all = page.get_by_role("button", name="View All")
            if view_all.count() > 0:
                view_all.first.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        desc_el = soup.select_one(".content-block__content p")
        description = desc_el.get_text(strip=True) if desc_el else ""

        case_details = parse_case_details(soup)
        timeline = parse_timeline(soup)

        documents_section: List[Dict[str, str]] = []
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["section"] = ["documents"]
        docs_query = urlencode(qs, doseq=True)
        docs_url = urlunparse((parsed.scheme, parsed.netloc,
                              parsed.path, parsed.params, docs_query, parsed.fragment))
        try:
            page.goto(docs_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            try:
                view_all_docs = page.get_by_role("button", name="View All")
                if view_all_docs.count() > 0:
                    view_all_docs.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            doc_soup = BeautifulSoup(page.content(), "html.parser")
            for link in doc_soup.select(".project-block__content a[href]"):
                href = link.get("href")
                if href and (href.endswith(".pdf") or "document" in href.lower() or "documents" in href):
                    documents_section.append({
                        "title": link.get_text(strip=True) or href,
                        "url": make_absolute_url(href),
                    })
            for block in doc_soup.select(".timeline-block"):
                title_el = block.select_one(".timeline-block__content-title")
                if title_el:
                    documents_section.append({
                        "title": title_el.get_text(strip=True),
                        "url": "",
                    })
        except Exception as e:
            print(f"   ⚠️ Error fetching documents section: {e}")

        media_section: List[Dict[str, str]] = []
        qs_media = parse_qs(parsed.query)
        qs_media["section"] = ["media"]
        media_query = urlencode(qs_media, doseq=True)
        media_url = urlunparse((parsed.scheme, parsed.netloc,
                               parsed.path, parsed.params, media_query, parsed.fragment))
        try:
            page.goto(media_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            try:
                view_all_media = page.get_by_role("button", name="View All")
                if view_all_media.count() > 0:
                    view_all_media.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            media_soup = BeautifulSoup(page.content(), "html.parser")
            for block in media_soup.select(".timeline-block"):
                title_el = block.select_one(".timeline-block__content-title")
                link_el = block.select_one("a[href]")
                date_el = block.select_one(".timeline-block__timeline-date")
                raw_href = (link_el.get("href") or "") if link_el else ""
                media_section.append({
                    "date": " ".join(date_el.get_text(strip=True).split()) if date_el else "",
                    "title": title_el.get_text(strip=True) if title_el else "",
                    "url": make_absolute_url(raw_href),
                })
        except Exception as e:
            print(f"   ⚠️ Error fetching media section: {e}")

        return {
            "description": description,
            "case_details": case_details,
            "timeline": timeline,
            "documents": documents_section,
            "updates_media": media_section,
        }
    except Exception as e:
        print(f"   ⚠️ Error fetching detail page: {e}")
        import traceback
        traceback.print_exc()
        return None


def detail_url_exists(collection, detail_url: str) -> bool:
    """Check if a document with this detail_url already exists in nz_cases."""
    if collection is None or not detail_url or not detail_url.strip():
        return False
    return collection.find_one({"detail_url": detail_url.strip()}) is not None


def build_case_document(list_item: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """Build the document to insert into nz_cases."""
    case_details = detail.get("case_details") or {}
    case_number = case_details.get("Case number", "").strip()

    doc = {
        "title": list_item.get("title", ""),
        "detail_url": list_item.get("detail_url", ""),
        "description": detail.get("description", ""),
        "case_details": case_details,
        "timeline": detail.get("timeline", []),
        "documents": detail.get("documents", []),
        "updates_media": detail.get("updates_media", []),
        "case_number": case_number,
        "scraped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    # List-level fields from list page
    doc["status"] = list_item.get("status", "")
    doc["tag"] = list_item.get("tag", "")
    doc["outcome"] = list_item.get("outcome", "")
    return doc


def run():
    """Main: build list URL, scrape list, for each item fetch detail, check nz_cases by detail_url, insert if new."""
    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"⚠️ {message}")
        return
    print(f"✅ {message}")

    collection = get_nz_cases_collection()
    if collection is None:
        print("⚠️ nz_cases collection not available.")
        return

    open_date = get_open_date_one_week_ago()
    list_url = LIST_URL_TEMPLATE.format(open_date=open_date)
    print(f"📋 List URL (open_date={open_date}): {list_url}\n")

    inserted = 0
    skipped = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Step 1: fetch list page and extract items
        page.goto(list_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        list_html = page.content()
        items = extract_list_items_from_html(list_html)
        print(
            f"✅ Found {len(items)} list items from ol.filter__results-list\n")

        for i, list_item in enumerate(items, 1):
            title = list_item.get("title", "?")
            detail_url = list_item.get("detail_url", "")
            if not detail_url:
                print(f"   [{i}/{len(items)}] No detail URL, skipping: {title}")
                continue

            print(f"   [{i}/{len(items)}] {title}")

            # Check nz_cases by detail_url (dedupe key)
            if detail_url_exists(collection, detail_url):
                print(f"      ⏭️ detail_url already in nz_cases, skip")
                skipped += 1
                continue

            # Step 2: fetch detail page
            detail = fetch_case_detail_page(page, detail_url)
            if not detail:
                print(f"      ⚠️ Could not fetch detail, skipping")
                continue

            doc = build_case_document(list_item, detail)
            try:
                collection.insert_one(doc)
                case_number = (doc.get("case_number") or "").strip()
                extra = f" case_number={case_number}" if case_number else ""
                print(f"      ✅ Inserted into nz_cases (detail_url){extra}")
                inserted += 1
            except Exception as e:
                print(f"      ⚠️ Insert failed: {e}")

        browser.close()

    print(
        f"\n📊 Done. Inserted: {inserted}, Skipped (already in DB): {skipped}")


if __name__ == "__main__":
    run()
