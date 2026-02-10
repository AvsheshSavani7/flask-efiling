"""
NZ Commerce Commission (ComCom) Case Register Scraper
=====================================================
Scrapes cases from the ComCom case register, matches with deals in MongoDB,
saves matched cases to deals under 'nz_cases', and sends email notifications
via webhook. For unmatched cases that are USA-related, sends email with details URL.
"""

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from bs4 import BeautifulSoup
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from mongodb_connection import (
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)

load_dotenv(".env")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants
BASE_URL = "https://www.comcom.govt.nz"


def make_absolute_url(href: str, base: str = BASE_URL) -> str:
    """Convert relative or protocol-relative href to full ComCom URL so links work in emails."""
    if not href or not href.strip():
        return ""
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    base = base.rstrip("/")
    if href.startswith("/"):
        return base + href
    return base + "/" + href


LIST_URL_TEMPLATE = (
    "https://www.comcom.govt.nz/case-register/"
    "?q=&size=50&filters%5Bopen_date%5D={open_date}"
)
# CUTOFF_DATE = datetime.strptime("2026-01-12", "%Y-%m-%d")
CUTOFF_DATE = datetime.now().replace(
    hour=0, minute=0, second=0, microsecond=0)
OUTPUT_PATH = "nz_comcom_matched_deals.json"
ENV_PATH = ".env"

deals: List[Dict[str, Any]] = []
matched_data: List[Dict[str, Any]] = []
matched_count = 0


def get_deals_from_mongodb(include_nz_cases: bool = False) -> List[Dict[str, Any]]:
    """Fetch deals from MongoDB. Optionally exclude deals that already have nz_cases."""
    try:
        collection = get_deals_collection()
        if collection is None:
            print("⚠️ MongoDB connection not available. Deals collection not accessible.")
            return []

        query = {}
        if not include_nz_cases:
            query = {"nz_cases": {"$exists": False}}

        all_deals = list(collection.find(query))
        for deal in all_deals:
            if "_id" in deal:
                deal["deal_id"] = str(deal["_id"])
                deal.pop("_id", None)

        filter_msg = "without 'nz_cases' node" if not include_nz_cases else "all"
        print(f"✅ Fetched {len(all_deals)} deals from MongoDB ({filter_msg})")
        return all_deals
    except Exception as e:
        print(f"⚠️ Error fetching deals from MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_deals(include_nz_cases: bool = False) -> List[Dict[str, Any]]:
    """Load deals from MongoDB."""
    global deals
    deals = get_deals_from_mongodb(include_nz_cases=include_nz_cases)
    print(f"📊 Loaded {len(deals)} deals from MongoDB")
    return deals


def match_with_llm(title: str) -> Optional[str]:
    """Use LLM to match case title with a deal. Returns match string or None."""
    deals_text = "\n".join([
        f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {d.get('target_name', 'N/A')} | Acquirer: {d.get('acquire_name', 'N/A')}"
        for d in deals
    ])

    prompt = f"""You are an expert M&A deal matcher. Determine if ANY company mentioned in the case title appears in our deals database.

DEALS DATABASE:
{deals_text}

CASE TITLE TO MATCH:
{title}

INSTRUCTIONS:
1. Extract ALL company names from the case title.
2. Check if ANY of these names appears as either Target OR Acquirer in the deals database.
3. Consider variations, abbreviations, and partial matches.
4. Match on a SINGLE company name.

RESPONSE FORMAT:
- If you find ANY match, respond EXACTLY:
  Match: DEAL_ID|COMPANY_NAME|(target|acquirer)
  Example: Match: 69665014d0bb42af1044aecd|Astra Energy|acquirer

- If NO match, respond with:
  None"""

    try:
        res = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "You are an expert M&A deal matcher. Respond only with Match: DEAL_ID|COMPANY|target|acquirer or None."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=150,
        )
        print(f"   ✅ LLM prompt: {prompt}")
        print(
            f"   ✅ LLM match response: {res.choices[0].message.content.strip()}")
        return res.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ LLM match error: {e}")
        return None


def extract_list_items_from_html(html_content: str) -> List[Dict[str, Any]]:
    """Extract case cards from the ComCom case register list HTML. Skip 'Withheld'."""
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
            if not title or title.lower() == "withheld":
                continue

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
            print(f"⚠️ Error parsing list item: {e}")
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

        # If timeline has "View All", click it to load all timeline records before parsing
        try:
            view_all = page.get_by_role("button", name="View All")
            if view_all.count() > 0:
                view_all.first.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Description (first content block)
        desc_el = soup.select_one(".content-block__content p")
        description = desc_el.get_text(strip=True) if desc_el else ""

        # Case details table
        case_details = parse_case_details(soup)
        timeline = parse_timeline(soup)

        # Documents section (?section=documents)
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
            # Also capture any timeline-style blocks in documents tab
            for block in doc_soup.select(".timeline-block"):
                title_el = block.select_one(".timeline-block__content-title")
                if title_el:
                    documents_section.append({
                        "title": title_el.get_text(strip=True),
                        "url": "",
                    })
        except Exception as e:
            print(f"   ⚠️ Error fetching documents section: {e}")

        # Media/updates section (?section=media)
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


def generate_nz_case_email_html(case_info: Dict[str, Any], deal_match: Dict[str, Any]) -> tuple:
    """Generate HTML email for matched NZ ComCom case."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    title = case_info.get("title", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("case_details") or {}
    description = case_info.get("description", "")
    case_number = details.get("Case number", "N/A")
    parties = details.get("Parties", "N/A")
    category = details.get("Category", "N/A")
    subcategory = details.get("Sub-category", "N/A")
    act_section = details.get("Act/Section", "N/A")
    industry = details.get("Industry", "N/A")
    status = details.get("Status", "N/A")
    outcome = details.get("Outcome", "N/A")
    date_opened = details.get("Date opened", "N/A")
    date_closed = details.get("Date closed", "N/A")
    contact = details.get("Contact person", "N/A")

    subject = f"NZ ComCom Case Match – {target} / {acquirer}"

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NZ ComCom Case Match - {case_number}</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:24px;">

<div style="background:#e0f2fe;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid #0284c7;">
<div style="font-size:14px;font-weight:700;color:#0369a1;">Matched Deal</div>
<div style="font-size:14px;color:#0c4a6e;">Acquirer: {acquirer} | Target: {target}</div>
<a href="{detail_url}" target="_blank" style="display:inline-block;margin-top:8px;color:#0284c7;font-weight:700;">View NZ ComCom case →</a>
</div>

<h2 style="font-size:18px;margin:0 0 12px 0;">{title}</h2>
<p style="margin:0 0 20px 0;line-height:1.5;">{description or '—'}</p>

<h3 style="font-size:16px;margin:20px 0 10px 0;">Case Details</h3>
<div style="background:#f8fafc;border-radius:6px;padding:14px;">
<table style="width:100%;border-collapse:collapse;">
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Parties</td><td style="padding:6px 0;">{parties}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Category</td><td>{category}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Sub-category</td><td>{subcategory}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Act/Section</td><td>{act_section}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Industry</td><td>{industry}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Status</td><td>{status}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Outcome</td><td>{outcome or '—'}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Case number</td><td>{case_number}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Date opened</td><td>{date_opened}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Date closed</td><td>{date_closed or '—'}</td></tr>
<tr><td style="padding:6px 0;font-weight:600;color:#475569;">Contact</td><td>{contact}</td></tr>
</table>
</div>

</div></body></html>"""
    return subject, html


def generate_unmatched_nz_usa_email_html(case_info: Dict[str, Any]) -> tuple:
    """Generate HTML email for unmatched USA-related NZ ComCom case (link to details)."""
    title = case_info.get("title", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("case_details") or {}
    case_number = details.get("Case number", "N/A")
    category = details.get("Category", "N/A")
    status = details.get("Status", "N/A")
    date_opened = details.get("Date opened", "N/A")

    subject = f"🇺🇸 USA-Related NZ ComCom Case – {case_number}"

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>USA-Related NZ ComCom Case</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">

<div style="background:#dbeafe;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid #3b82f6;">
<div style="font-size:16px;font-weight:700;color:#1e40af;">🇺🇸 USA-Related NZ ComCom Case</div>
<div style="font-size:14px;color:#1e3a8a;margin-top:6px;">This case appears to involve USA-related companies.</div>
</div>

<div style="font-size:18px;font-weight:700;margin-bottom:8px;">{title}</div>
<div style="font-size:14px;color:#64748b;">Case number: {case_number} | Category: {category} | Status: {status} | Opened: {date_opened}</div>

<div style="margin-top:20px;">
<a href="{detail_url}" target="_blank" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:700;">View case details →</a>
</div>

</div></body></html>"""
    return subject, html


def send_nz_case_email_via_webhook(case_info: Dict[str, Any], deal_match: Dict[str, Any]) -> bool:
    """Send matched case email via n8n webhook."""
    try:
        subject, html_email = generate_nz_case_email_html(
            case_info, deal_match)
        with open("nz_case_email.html", "w") as f:
            f.write(html_email)
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": deal_match.get("deal_id", "N/A"),
            "target": deal_match.get("target") or deal_match.get("target_name", "N/A"),
            "acquirer": deal_match.get("acquirer") or deal_match.get("acquire_name", "N/A"),
            "case_number": (case_info.get("case_details") or {}).get("Case number", "N/A"),
            "case_title": case_info.get("title", "N/A"),
            "source": "nz_comcom",
        }
        response = requests.post(webhook_url, json=payload, headers={
                                 "Content-Type": "application/json"}, timeout=30)
        response.raise_for_status()
        print(f"   ✅ Email sent via webhook ({response.status_code})")
        return True
    except Exception as e:
        print(f"   ⚠️ Error sending email via webhook: {e}")
        return False


def send_unmatched_nz_usa_email_via_webhook(case_info: Dict[str, Any]) -> bool:
    """Send USA-related unmatched case email via webhook."""
    try:
        subject, html_email = generate_unmatched_nz_usa_email_html(case_info)
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6")

        payload = {
            "subject": subject,
            "html": html_email,
            "deal_id": "N/A",
            "target": "N/A",
            "acquirer": "N/A",
            "case_number": (case_info.get("case_details") or {}).get("Case number", "N/A"),
            "case_title": case_info.get("title", "N/A"),
            "detail_url": case_info.get("detail_url", ""),
            "usa_related": True,
            "is_unmatched": True,
            "source": "nz_comcom",
        }
        response = requests.post(webhook_url, json=payload, headers={
                                 "Content-Type": "application/json"}, timeout=30)
        response.raise_for_status()
        print(
            f"   ✅ USA-related email sent via webhook ({response.status_code})")
        return True
    except Exception as e:
        print(f"   ⚠️ Error sending USA email via webhook: {e}")
        return False


def save_nz_case_to_deal(deal_match: Dict[str, Any], case_info: Dict[str, Any]) -> bool:
    """Save matched NZ case to deal under 'nz_cases' array and send email."""
    try:
        if not is_connected():
            print("   ⚠️ MongoDB not available, skipping save")
            return False

        collection = get_deals_collection()
        if collection is None:
            return False

        query = {}
        if deal_match.get("deal_id"):
            try:
                query["_id"] = ObjectId(deal_match["deal_id"])
            except Exception:
                query = {}

        if not query:
            acquirer = deal_match.get(
                "acquirer") or deal_match.get("acquire_name")
            target = deal_match.get("target") or deal_match.get("target_name")
            or_conditions = []
            if acquirer:
                or_conditions.extend(
                    [{"acquirer": acquirer}, {"acquire_name": acquirer}])
            if target:
                or_conditions.extend(
                    [{"target": target}, {"target_name": target}])
            if or_conditions:
                query = {"$or": or_conditions}

        if not query:
            print("   ⚠️ Cannot identify deal for MongoDB save")
            return False

        case_number = (case_info.get("case_details") or {}).get("Case number")
        existing = collection.find_one(query)
        if existing and existing.get("nz_cases"):
            for c in existing["nz_cases"]:
                if (c.get("case_details") or {}).get("Case number") == case_number:
                    print("   ⏩ Case already in deal, skipping save")
                    return False

        update_result = collection.update_one(
            query, {"$push": {"nz_cases": case_info}})

        if update_result.modified_count > 0:
            print("   ✅ Saved NZ case to deal (nz_cases)")
            try:
                print("   📧 Sending email notification...")

                send_nz_case_email_via_webhook(case_info, deal_match)
            except Exception as e:
                print(f"   ⚠️ Email error: {e}")
            return True
        if update_result.matched_count > 0:
            return True
        print("   ⚠️ Deal not found in MongoDB")
        return False
    except Exception as e:
        print(f"   ❌ Error saving to MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return False


def main() -> None:
    global matched_count
    matched_count = 0

    list_url = LIST_URL_TEMPLATE.format(
        open_date=CUTOFF_DATE.strftime("%Y-%m-%d"))

    print("🚀 NZ ComCom Case Register Scraper\n")

    print(f"   📄 List URL: {list_url}")

    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        print(f"❌ {message}")
        return
    print(f"✅ {message}\n")

    print("📊 Loading deals (excluding deals with 'nz_cases' node)...")
    load_deals(include_nz_cases=False)
    if not deals:
        print("⚠️ No deals in MongoDB. Exiting.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"\n📄 Fetching case register: {list_url}")
        page.goto(list_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        try:
            page.wait_for_selector("ol.filter__results-list", timeout=10000)
        except Exception:
            print("⚠️ List not found; trying without selector.")
        html = page.content()
        with open("case_register_list.html", "w") as f:
            f.write(html)

        print(
            "   📄 Extracting case list items...html saved to case_register_list.html file")
        all_items = extract_list_items_from_html(html)
        print(f"✅ Found {len(all_items)} cases (excluding Withheld)\n")

        for idx, item in enumerate(all_items):
            try:
                title = item["title"]
                detail_url = item["detail_url"]
                print(f"🔍 [{idx + 1}] {title}")

                deal_match = None
                matched_company = None
                matched_role = None

                result = match_with_llm(title)
                if result and result.strip().lower().startswith("match"):
                    m = re.search(
                        r"Match:\s*([^|]+)\|([^|]+)\|(target|acquirer)", result, re.IGNORECASE)
                    if m:
                        deal_id, matched_company, matched_role = m.group(
                            1).strip(), m.group(2).strip(), m.group(3).strip().lower()
                        for d in deals:
                            if d.get("deal_id") == deal_id:
                                deal_match = d
                                print(
                                    f"   🎯 Matched: {d.get('acquire_name') or d.get('acquirer')} / {d.get('target_name') or d.get('target')} ({matched_role})")
                                break

                if deal_match and detail_url:
                    print("   📄 Fetching case details...")
                    detail_data = fetch_case_detail_page(page, detail_url)
                    if detail_data:
                        case_info = {
                            "title": title,
                            "detail_url": detail_url,
                            "status": item.get("status", ""),
                            "tag": item.get("tag", ""),
                            "outcome": item.get("outcome", ""),
                            "case_details": detail_data.get("case_details", {}),
                            "description": detail_data.get("description", ""),
                            "timeline": detail_data.get("timeline", []),
                            "documents": detail_data.get("documents", []),
                            "updates_media": detail_data.get("updates_media", []),
                            "matched_company": matched_company or "",
                            "matched_role": matched_role or "",
                        }
                        if save_nz_case_to_deal(deal_match, case_info):
                            matched_count += 1
                            matched_data.append({
                                "title": title,
                                "case_details": case_info.get("case_details", {}),
                                "matched_deal": {"acquirer": deal_match.get("acquire_name") or deal_match.get("acquirer"), "target": deal_match.get("target_name") or deal_match.get("target")},
                                "case_info": case_info,
                            })
                    else:
                        print("   ⚠️ Could not fetch case details")
                else:
                    # Unmatched: check USA relation and send email with link if USA-related
                    print("   ⏭️ No deal match")
                    try:
                        nz_details = f"""
Case Title: {title}
Detail URL: {detail_url}
Tag: {item.get('tag', '')}
Status: {item.get('status', '')}
""".strip()
                        if verify_usa_relation(company_details=nz_details, case_type="NZ"):
                            print(
                                "   🇺🇸 USA-related – fetching details and sending email")
                            detail_data = fetch_case_detail_page(
                                page, detail_url)
                            case_info = {
                                "title": title,
                                "detail_url": detail_url,
                                "status": item.get("status", ""),
                                "tag": item.get("tag", ""),
                                "case_details": (detail_data or {}).get("case_details") or {},
                            }
                            send_unmatched_nz_usa_email_via_webhook(case_info)
                        else:
                            print("   ℹ️ Not USA-related – no action")
                    except Exception as e:
                        print(f"   ⚠️ USA check error: {e}")
                        import traceback
                        traceback.print_exc()

                print()
            except Exception as e:
                print(f"❌ Error processing case: {e}")
                continue

        browser.close()

    print(f"\n💾 Saving matched data to {OUTPUT_PATH}")
    try:
        with open(OUTPUT_PATH, "w") as f:
            json.dump(matched_data, f, indent=2)
        print(f"✅ Saved {len(matched_data)} matches")
    except Exception as e:
        print(f"⚠️ JSON save error: {e}")

    print("\n🎉 Done!")
    print(f"   🎯 Total matches saved to deals (nz_cases): {matched_count}")


if __name__ == "__main__":
    main()
