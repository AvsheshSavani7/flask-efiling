"""
NZ Cases Update Monitor (nz_cases collection)
=============================================
Step 1: Fetch all records from nz_cases with status="Open".
Step 2: Fetch all deals with deal_status in ("Open", "Unknown", None).
Step 3: For each nz_cases record, call detail URL and compare with stored data.
        If any change: run LLM match → if match add deal_id, update record, send email;
        if no match check USA-related → if USA send email and update; else just update.
Reference: newzeeland_monitor.md, accc_cases_update_monitor.py, nz_comcom_case_update_monitor.py
"""

import os
import logging
import sys
import builtins
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)

load_dotenv(".env")

# Logging
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logger = logging.getLogger("nz_cases_update_monitor")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    """
    Replacement for print that also logs via the module logger.
    """
    msg = " ".join(str(a) for a in args)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)
    # Still echo to original stdout for local runs
    builtins.print(*args, **kwargs)


# Monkey-patch print in this module so any print() calls are logged.
print = _logged_print  # type: ignore

# Constants
BASE_URL = "https://www.comcom.govt.nz"
ENV_PATH = ".env"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
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


def get_nz_cases_collection():
    """Get the 'nz_cases' collection from the current MongoDB database."""
    db = get_database()
    if db is None:
        return None
    return db["nz_cases"]


def get_open_deals_for_matching() -> List[Dict[str, Any]]:
    """Fetch deals with deal_status in Open, Unknown, or None (for LLM matching)."""
    try:
        collection = get_deals_collection()
        if collection is None:
            return []
        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(collection.find(status_filter))
        for d in deals:
            if "_id" in d:
                d["deal_id"] = str(d["_id"])
                d.pop("_id", None)
        return deals
    except Exception as e:
        logger.warning("Error fetching deals: %s", e)
        return []


# ---------- Detail page fetch (copied from nz_comcom_case_register_to_db) ----------
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


def parse_timeline(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Parse timeline blocks into list of {date, title, status, has_link}."""
    entries = []
    for block in soup.select(".timeline-block"):
        date_el = block.select_one(".timeline-block__timeline-date")
        title_el = block.select_one(".timeline-block__content-title")
        status_el = block.select_one(".timeline-block__content-status")
        link_el = block.select_one(".timeline-block__content-link")
        # Use a separator so multiple spans don't concatenate (e.g. "05MAR2026").
        date_str = " ".join(date_el.get_text(
            " ", strip=True).split()) if date_el else ""
        entries.append({
            "date": date_str,
            "title": " ".join(title_el.get_text(" ", strip=True).split()) if title_el else "",
            "status": " ".join(status_el.get_text(" ", strip=True).split()) if status_el else "",
            "has_link": link_el is not None,
        })
    return entries


def fetch_case_detail_page(page, url: str) -> Optional[Dict[str, Any]]:
    """Fetch case detail page and documents/media sections. Returns case detail dict."""
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
                    documents_section.append({"title": link.get_text(
                        strip=True) or href, "url": make_absolute_url(href)})
            for block in doc_soup.select(".timeline-block"):
                title_el = block.select_one(".timeline-block__content-title")
                if title_el:
                    documents_section.append(
                        {"title": title_el.get_text(strip=True), "url": ""})
        except Exception as e:
            logger.warning("Error fetching documents section: %s", e)

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
            logger.warning("Error fetching media section: %s", e)

        return {
            "description": description,
            "case_details": case_details,
            "timeline": timeline,
            "documents": documents_section,
            "updates_media": media_section,
        }
    except Exception as e:
        logger.warning("Error fetching detail page: %s", e)
        import traceback
        traceback.print_exc()
        return None


# ---------- Comparison ----------
def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip() if value else None
    return value


def _timeline_key(entry: Dict[str, Any]) -> str:
    date = _normalize_value(entry.get("date", "")) or ""
    title = _normalize_value(entry.get("title", "")) or ""
    # Collapse internal whitespace for stable matching.
    date = " ".join(str(date).split())
    title = " ".join(str(title).split())
    return f"{date}|{title}"


def _document_key(entry: Dict[str, Any]) -> str:
    return f"{entry.get('title', '')}|{entry.get('url', '')}"


def _media_key(entry: Dict[str, Any]) -> str:
    return f"{entry.get('date', '')}|{entry.get('title', '')}|{entry.get('url', '')}"


def _doc_url(entry: Dict[str, Any]) -> str:
    u = _normalize_value(entry.get("url", "")) or ""
    return " ".join(str(u).split())


def _doc_title(entry: Dict[str, Any]) -> str:
    t = _normalize_value(entry.get("title", "")) or ""
    return " ".join(str(t).split())


def _media_url(entry: Dict[str, Any]) -> str:
    u = _normalize_value(entry.get("url", "")) or ""
    return " ".join(str(u).split())


def _media_date(entry: Dict[str, Any]) -> str:
    d = _normalize_value(entry.get("date", "")) or ""
    return " ".join(str(d).split())


def _timeline_date(entry: Dict[str, Any]) -> str:
    d = _normalize_value(entry.get("date", "")) or ""
    return " ".join(str(d).split())


def detect_changes(
    old_case: Dict[str, Any], current_info: Dict[str, Any]
) -> List[Tuple[str, Any, Any, str]]:
    """
    Compare title, description, case_details, timeline, documents, updates_media, outcome, tag, status.
    Returns list of (field_name, old_value, new_value, change_type).
    """
    changes: List[Tuple[str, Any, Any, str]] = []

    # Scalars
    for field in ("title", "description", "outcome", "tag", "status"):
        old_val = _normalize_value(old_case.get(field))
        new_val = _normalize_value(current_info.get(field))
        if old_val != new_val:
            change_type = "new" if old_val is None and new_val is not None else (
                "removed" if old_val is not None and new_val is None else "updated")
            changes.append((field.replace("_", " ").title(),
                           old_val, new_val, change_type))

    # Case details (key-value): emit per-field so email can mark only changed rows
    old_details = old_case.get("case_details") or {}
    new_details = current_info.get("case_details") or {}
    all_keys = set(old_details.keys()) | set(new_details.keys())
    for key in sorted(all_keys):
        old_val = _normalize_value(old_details.get(key))
        new_val = _normalize_value(new_details.get(key))
        if old_val is None and new_val is not None:
            changes.append((f"Case details: {key}", None, new_val, "new"))
        elif old_val is not None and new_val is None:
            changes.append((f"Case details: {key}", old_val, None, "removed"))
        elif old_val != new_val:
            changes.append(
                (f"Case details: {key}", old_val, new_val, "updated"))

    # Timeline: treat title tweaks on same date as updates, not new rows
    old_timeline = old_case.get("timeline") or []
    new_timeline = current_info.get("timeline") or []
    old_timeline_keys = {_timeline_key(e) for e in old_timeline}
    old_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for e in old_timeline:
        old_by_date.setdefault(_timeline_date(e), []).append(e)

    new_entries: List[Dict[str, Any]] = []
    updated_entries: List[Dict[str, Any]] = []
    for e in new_timeline:
        key = _timeline_key(e)
        if key in old_timeline_keys:
            continue
        d = _timeline_date(e)
        if d and d in old_by_date and old_by_date[d]:
            updated_entries.append(e)
        else:
            new_entries.append(e)

    if new_entries:
        changes.append(("Timeline (new)", None, new_entries, "new"))
    if updated_entries:
        changes.append(("Timeline (updated)", None,
                       updated_entries, "updated"))

    # Documents: treat title tweaks on same URL as updates, not new rows
    old_docs = old_case.get("documents") or []
    new_docs = current_info.get("documents") or []
    old_doc_keys = {_document_key(d) for d in old_docs}
    old_doc_urls = {_doc_url(d) for d in old_docs if _doc_url(d)}
    old_doc_titles = {_doc_title(d) for d in old_docs if _doc_title(d)}

    new_doc_entries: List[Dict[str, Any]] = []
    updated_doc_entries: List[Dict[str, Any]] = []
    for d in new_docs:
        if _document_key(d) in old_doc_keys:
            continue
        url = _doc_url(d)
        title = _doc_title(d)
        if url and url in old_doc_urls:
            updated_doc_entries.append(d)
        elif (not url) and title and title in old_doc_titles:
            updated_doc_entries.append(d)
        else:
            new_doc_entries.append(d)
    if new_doc_entries:
        changes.append(("Documents (new)", None, new_doc_entries, "new"))
    if updated_doc_entries:
        changes.append(("Documents (updated)", None,
                       updated_doc_entries, "updated"))

    # Updates/Media: treat title tweaks on same URL (or same date if no URL) as updates
    old_media = old_case.get("updates_media") or []
    new_media = current_info.get("updates_media") or []
    old_media_keys = {_media_key(m) for m in old_media}
    old_media_urls = {_media_url(m) for m in old_media if _media_url(m)}
    old_media_dates = {_media_date(m) for m in old_media if _media_date(m)}

    new_media_entries: List[Dict[str, Any]] = []
    updated_media_entries: List[Dict[str, Any]] = []
    for m in new_media:
        if _media_key(m) in old_media_keys:
            continue
        url = _media_url(m)
        date = _media_date(m)
        if url and url in old_media_urls:
            updated_media_entries.append(m)
        elif (not url) and date and date in old_media_dates:
            updated_media_entries.append(m)
        else:
            new_media_entries.append(m)
    if new_media_entries:
        changes.append(("Updates/Media (new)", None, new_media_entries, "new"))
    if updated_media_entries:
        changes.append(("Updates/Media (updated)", None,
                       updated_media_entries, "updated"))

    return changes


def _summary_changes(changes: List[Tuple[str, Any, Any, str]]) -> List[str]:
    seen = set()
    summary = []
    for field_name, _old, _new, _change_type in changes:
        if field_name == "Case details" or field_name.startswith("Case details:"):
            if "Case details" not in seen:
                summary.append("Case details")
                seen.add("Case details")
        elif field_name.startswith("Timeline"):
            if "Timeline" not in seen:
                summary.append("Timeline")
                seen.add("Timeline")
        elif field_name.startswith("Documents"):
            if "Documents" not in seen:
                summary.append("Documents")
                seen.add("Documents")
        elif field_name.startswith("Updates/Media"):
            if "Updates/Media" not in seen:
                summary.append("Updates/Media")
                seen.add("Updates/Media")
        elif field_name not in seen:
            summary.append(field_name)
            seen.add(field_name)
    return summary


# ---------- LLM match ----------
def match_case_to_deal(title: str, parties: str, description: str, deals: List[Dict[str, Any]]) -> Optional[str]:
    """
    Ask LLM if this NZ case matches any deal. Returns deal_id or None.
    """
    if not deals:
        return None
    lines = []
    for d in deals:
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {d.get('deal_id', 'N/A')} | Target: {target} | Acquirer: {acquirer}"
        for alias_key in ("target_aliases", "parent_aliases"):
            aliases = d.get(alias_key) or []
            if aliases:
                line += f" | {alias_key}: {', '.join(str(a) for a in aliases)}"
        lines.append(line)
    deals_text = "\n".join(lines)

    prompt = f"""You are an expert M&A deal matcher. Determine if ANY company mentioned in this NZ Commerce Commission case appears in our deals database.

DEALS DATABASE:
{deals_text}

NZ CASE:
- Title: {title}
- Parties: {parties}
- Description: {description}

INSTRUCTIONS:
1. Extract ALL company names from the case (title, parties, description).
2. Check if ANY of these names appears as Target OR Acquirer (or aliases) in the deals database.
3. Consider variations, abbreviations, and partial matches.
4. Match on a SINGLE company name.

RESPONSE FORMAT:
- If you find ANY match, respond EXACTLY: Match: DEAL_ID
- If NO match, respond with exactly: None"""

    try:
        res = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "You are an expert M&A deal matcher. Respond only with Match: DEAL_ID or None."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=150,
        )
        content = res.choices[0].message.content.strip()
        if not content.lower().startswith("match"):
            return None
        try:
            _prefix, deal_id_raw = content.split(":", 1)
            return deal_id_raw.strip() or None
        except Exception:
            return None
    except Exception as e:
        logger.warning("LLM match error: %s", e)
        return None


# ---------- Email HTML (from nz_comcom_case_update_monitor / nz_comcom_case_register) ----------
def generate_nz_update_email_html(
    case_info: Dict[str, Any],
    deal_match: Dict[str, Any],
    changes: List[Tuple[str, Any, Any, str]],
) -> str:
    """Generate HTML email for NZ case update (matched deal)."""
    target = deal_match.get("target") or deal_match.get("target_name", "N/A")
    acquirer = deal_match.get(
        "acquirer") or deal_match.get("acquire_name", "N/A")
    deal_id = deal_match.get("deal_id", "N/A")
    title = case_info.get("title", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("case_details") or {}
    description = case_info.get("description", "")
    case_number = details.get("Case number", "N/A")
    tag = case_info.get("tag", "") or "—"
    change_summary = _summary_changes(changes)
    changed_fields = {c[0]: (c[3], c[2]) for c in changes}
    # Per-field case details: only these keys get (updated) in the table
    changed_case_detail_keys = {
        label.replace("Case details: ", "", 1)
        for label, _o, _n, _t in changes
        if label.startswith("Case details:")
    }
    tag_changed = "Tag" in changed_fields

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NZ Case Update - {case_number}</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,-apple-system,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:28px 26px 40px 26px;">
<div style="background:#fef2f2;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #ef4444;">
<div style="font-size:16px;font-weight:800;color:#dc2626;">⚠️ NZ Case Updated</div>
<div style="font-size:14px;color:#991b1b;">Changed: {', '.join(change_summary)}</div>
</div>
<div style="background:#e0f2fe;border-radius:6px;padding:16px 22px;margin-bottom:20px;border-left:4px solid #0284c7;">
<div style="font-size:15px;font-weight:800;color:#0369a1;">Matched Deal</div>
<div style="font-size:14px;color:#0c4a6e;">Deal ID: {deal_id}</div>
<div style="font-size:14px;color:#0c4a6e;">Acquirer: {acquirer} | Target: {target}</div>
<a href="{detail_url}" target="_blank" style="color:#0284c7;font-weight:700;font-size:14px;">View NZ case →</a>
</div>
<h2 style="font-size:18px;margin:0 0 12px 0;">{title}</h2>
<p style="margin:0 0 20px 0;line-height:1.5;">{description or '—'}</p>
<h3 style="font-size:16px;margin:20px 0 10px 0;">Case Details</h3>
<div style="background:#f8fafc;border-radius:6px;padding:14px;">
<table style="width:100%;border-collapse:collapse;">"""
    for key, value in (details or {}).items():
        is_changed = key in changed_case_detail_keys
        flag = ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;">(updated)</span>' if is_changed else ""
        html += (
            f"<tr>"
            f"<td style='padding:6px 0;font-weight:600;color:#475569;'>{key}</td>"
            f"<td style='padding:6px 0;'>{value or '—'}{flag}</td>"
            f"</tr>"
        )
    tag_flag = ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;">(updated)</span>' if tag_changed else ""
    html += f"<tr><td style='padding:6px 0;font-weight:600;color:#475569;'>Tag</td><td style='padding:6px 0;'>{tag}{tag_flag}</td></tr>"
    html += "</table></div>"

    # Timeline
    timeline = case_info.get("timeline") or []
    new_timeline_list = (changed_fields.get(
        "Timeline (new)") or (None, None))[1] or []
    updated_timeline_list = (changed_fields.get(
        "Timeline (updated)") or (None, None))[1] or []
    new_timeline_keys = {_timeline_key(e) for e in new_timeline_list}
    updated_timeline_keys = {_timeline_key(e) for e in updated_timeline_list}
    if timeline:
        html += """
<h3 style="font-size:16px;margin:24px 0 10px 0;">Timeline</h3>
<div style="padding-top:8px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>"""
        for entry in timeline:
            date_str = entry.get("date", "N/A")
            tit = entry.get("title", "N/A")
            k = _timeline_key(entry)
            is_new = k in new_timeline_keys
            is_updated = (not is_new) and (k in updated_timeline_keys)
            flag = ""
            if is_new:
                flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;">(new)</span>'
            elif is_updated:
                flag = ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;">(updated)</span>'
            html += f"""
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px 12px 0;vertical-align:top;width:120px;color:#6b7280;font-size:14px;">{date_str}</td>
<td style="padding:12px 8px;vertical-align:top;">{tit}{flag}</td>
</tr>"""
        html += """
</tbody>
</table>
</div>"""

    # Documents
    documents = case_info.get("documents") or []
    new_doc_list = (changed_fields.get("Documents (new)")
                    or (None, None))[1] or []
    updated_doc_list = (changed_fields.get(
        "Documents (updated)") or (None, None))[1] or []
    new_doc_keys = {_document_key(d) for d in new_doc_list}
    updated_doc_keys = {_document_key(d) for d in updated_doc_list}
    if documents:
        html += """
<h3 style="font-size:16px;margin:24px 0 10px 0;">Documents</h3>
<div style="padding-top:8px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>"""
        for doc in documents:
            tit = doc.get("title", "N/A")
            url = doc.get("url", "")
            k = _document_key(doc)
            is_new = k in new_doc_keys
            is_updated = (not is_new) and (k in updated_doc_keys)
            flag = ""
            if is_new:
                flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;">(new)</span>'
            elif is_updated:
                flag = ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;">(updated)</span>'
            link = f'<a href="{url}" target="_blank" style="color:#2563eb;">{tit}</a>' if url else tit
            html += f"""
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px;">{link}{flag}</td>
</tr>"""
        html += """
</tbody>
</table>
</div>"""

    # Updates / Media releases
    updates_media = case_info.get("updates_media") or []
    new_media_list = (changed_fields.get(
        "Updates/Media (new)") or (None, None))[1] or []
    updated_media_list = (changed_fields.get(
        "Updates/Media (updated)") or (None, None))[1] or []
    new_media_keys = {_media_key(m) for m in new_media_list}
    updated_media_keys = {_media_key(m) for m in updated_media_list}
    if updates_media:
        html += """
<h3 style="font-size:16px;margin:24px 0 10px 0;">Updates and media releases</h3>
<div style="padding-top:8px;">
<table style="width:100%;border-collapse:collapse;">
<tbody>"""
        for m in updates_media:
            date_str = m.get("date", "N/A")
            tit = m.get("title", "N/A")
            url = m.get("url", "")
            k = _media_key(m)
            is_new = k in new_media_keys
            is_updated = (not is_new) and (k in updated_media_keys)
            flag = ""
            if is_new:
                flag = ' <span style="color:#10b981;font-size:0.85em;font-weight:700;">(new)</span>'
            elif is_updated:
                flag = ' <span style="color:#f59e0b;font-size:0.85em;font-weight:700;">(updated)</span>'
            link = f'<a href="{url}" target="_blank" style="color:#2563eb;">{tit}</a>' if url else tit
            html += f"""
<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:12px 8px 12px 0;vertical-align:top;width:120px;color:#6b7280;font-size:14px;">{date_str}</td>
<td style="padding:12px 8px;">{link}{flag}</td>
</tr>"""
        html += """
</tbody>
</table>
</div>"""

    html += """
</div>
</body>
</html>"""
    return html


def generate_unmatched_nz_usa_email_html(case_info: Dict[str, Any]) -> tuple:
    """Generate HTML for USA-related unmatched NZ case."""
    title = case_info.get("title", "N/A")
    detail_url = case_info.get("detail_url", "")
    details = case_info.get("case_details") or {}
    case_number = details.get("Case number", "N/A")
    category = details.get("Category", "N/A")
    status = details.get("Status", "N/A")
    date_opened = details.get("Date opened", "N/A")
    subject = f"🇺🇸 USA-Related NZ Case – {case_number}"
    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>USA-Related NZ Case</title></head>
<body style="margin:0;padding:0;background:#fff;color:#0f172a;font-family:system-ui,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
<div style="background:#dbeafe;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid #3b82f6;">
<div style="font-size:16px;font-weight:700;color:#1e40af;">🇺🇸 USA-Related NZ Case</div>
<div style="font-size:14px;color:#1e3a8a;">This case appears to involve USA-related companies.</div>
</div>
<div style="font-size:18px;font-weight:700;margin-bottom:8px;">{title}</div>
<div style="font-size:14px;color:#64748b;">Case number: {case_number} | Category: {category} | Status: {status} | Opened: {date_opened}</div>
<a href="{detail_url}" target="_blank" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:700;">View case details →</a>
</div></body></html>"""
    return subject, html


# ---------- Webhooks ----------
def send_nz_update_email_via_webhook(
    case_info: Dict[str, Any],
    deal_match: Dict[str, Any],
    html_content: str,
    changes: List[Tuple[str, Any, Any, str]],
) -> bool:
    """Send NZ update email (matched deal) via n8n webhook."""
    try:
        target = deal_match.get("target") or deal_match.get(
            "target_name", "N/A")
        acquirer = deal_match.get(
            "acquirer") or deal_match.get("acquire_name", "N/A")
        deal_id = deal_match.get("deal_id", "N/A")
        case_number = (case_info.get("case_details")
                       or {}).get("Case number", "N/A")
        subject = f"NZ Case Update – {case_number}: {target} / {acquirer}"
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/b3007d21-6845-47b5-aece-7b26583758bc")
        payload = {
            "subject": subject,
            "html": html_content,
            "deal_id": deal_id,
            "target": target,
            "acquirer": acquirer,
            "case_number": case_number,
            "case_title": case_info.get("title", "N/A"),
            "changed_fields": _summary_changes(changes),
            "case_url": case_info.get("detail_url", ""),
            "source": "nz_cases_update_monitor",
        }
        response = requests.post(webhook_url, json=payload, headers={
                                 "Content-Type": "application/json"}, timeout=30)
        response.raise_for_status()
        logger.info("Email sent via webhook (%s)", response.status_code)
        return True
    except Exception as e:
        logger.warning("Error sending email via webhook: %s", e)
        return False


def send_unmatched_nz_usa_email_via_webhook(case_info: Dict[str, Any]) -> bool:
    """Send USA-related unmatched NZ case email via webhook."""
    try:
        subject, html_email = generate_unmatched_nz_usa_email_html(case_info)
        webhook_url = os.getenv(
            "N8N_WEBHOOK_URL", "https://n8n-xwx1.onrender.com/webhook/b3007d21-6845-47b5-aece-7b26583758bc")
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
            "source": "nz_cases_update_monitor",
        }
        response = requests.post(webhook_url, json=payload, headers={
                                 "Content-Type": "application/json"}, timeout=30)
        response.raise_for_status()
        logger.info("USA-related email sent via webhook (%s)",
                    response.status_code)
        return True
    except Exception as e:
        logger.warning("Error sending USA email via webhook: %s", e)
        return False


# ---------- DB update ----------
def update_nz_case_document(collection, doc_id: Any, updated_doc: Dict[str, Any]) -> bool:
    """Update a single document in nz_cases by _id. Preserve _id and deal_id if already set."""
    if collection is None:
        return False
    try:
        existing = collection.find_one({"_id": doc_id})
        if not existing:
            return False
        # Preserve deal_id if new doc doesn't have it
        if existing.get("deal_id") and "deal_id" not in updated_doc:
            updated_doc["deal_id"] = existing["deal_id"]

        # Preserve created_at; always bump updated_at
        if existing.get("created_at") and "created_at" not in updated_doc:
            updated_doc["created_at"] = existing["created_at"]
        updated_doc["updated_at"] = utc_now_iso()

        updated_doc["_id"] = doc_id
        updated_doc["scraped_at"] = utc_now_iso()
        result = collection.replace_one({"_id": doc_id}, updated_doc)
        if result.modified_count > 0:
            logger.info("Updated nz_cases record")
        return True
    except Exception as e:
        logger.warning("Error updating nz_cases: %s", e)
        return False


def get_deal_by_id(deal_id: str) -> Optional[Dict[str, Any]]:
    """Fetch deal by deal_id (string). Returns deal dict with deal_id key."""
    try:
        from bson import ObjectId
        collection = get_deals_collection()
        if collection is None:
            return None
        deal = collection.find_one({"_id": ObjectId(deal_id)})
        if not deal:
            return None
        deal["deal_id"] = str(deal["_id"])
        deal.pop("_id", None)
        return deal
    except Exception:
        return None


# ---------- Main ----------
def run():
    logger.info("NZ Cases Update Monitor started")

    success, message = init_mongodb_connection(ENV_PATH)
    if not success:
        logger.error(message)
        return
    logger.info(message)

    nz_collection = get_nz_cases_collection()
    if nz_collection is None:
        logger.error("nz_cases collection not available.")
        return

    # Step 1: nz_cases with status Open
    cases = list(nz_collection.find({"status": "Open"}))
    if not cases:
        logger.warning("No nz_cases with status=Open found.")
        return
    logger.info("Found %s nz_cases with status=Open", len(cases))

    # Step 2: deals for LLM matching
    deals = get_open_deals_for_matching()
    logger.info("Loaded %s deals for matching", len(deals))

    total_checked = 0
    total_updated = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for idx, case_doc in enumerate(cases, 1):
            detail_url = case_doc.get("detail_url")
            title = case_doc.get("title", "N/A")
            case_number = (case_doc.get("case_details")
                           or {}).get("Case number", "")
            if not detail_url:
                logger.info("[%s/%s] No detail_url, skipping: %s",
                            idx, len(cases), title)
                continue

            total_checked += 1
            logger.info("[%s/%s] %s", idx, len(cases), title or case_number)
            logger.info("Detail URL: %s", detail_url)

            current_info = fetch_case_detail_page(page, detail_url)
            if not current_info:
                logger.warning("Could not fetch detail, skipping")
                continue

            # Include list-level fields in current_info for comparison
            # keep stored title unless we scrape list again
            current_info["title"] = case_doc.get("title", "")
            current_info["status"] = case_doc.get("status", "")
            current_info["tag"] = case_doc.get("tag", "")
            current_info["outcome"] = case_doc.get("outcome", "")
            # We didn't re-scrape list; compare detail-level only. Override with scraped case_details for status/outcome if we had them from list.
            # Use scraped case_details for Status, Outcome etc.
            scraped_details = current_info.get("case_details") or {}
            current_info["status"] = scraped_details.get(
                "Status", case_doc.get("status", ""))
            current_info["outcome"] = scraped_details.get(
                "Outcome", case_doc.get("outcome", ""))

            changes = detect_changes(case_doc, current_info)
            if not changes:
                logger.info("No changes detected")
                continue

            logger.info("Changes detected (%s item(s))", len(changes))
            for field_name, old_val, new_val, change_type in changes:
                if field_name.startswith("Case details:"):
                    logger.info("%s (%s)", field_name, change_type)
                elif field_name.startswith("Timeline") and isinstance(new_val, list):
                    if field_name == "Timeline (new)":
                        logger.info(
                            "Timeline: %s new entry(ies)", len(new_val))
                    elif field_name == "Timeline (updated)":
                        logger.info(
                            "Timeline: %s updated entry(ies)", len(new_val))
                    else:
                        logger.info("%s: %s entry(ies)",
                                    field_name, len(new_val))
                elif field_name.startswith("Documents") and isinstance(new_val, list):
                    if field_name == "Documents (new)":
                        logger.info(
                            "Documents: %s new document(s)", len(new_val))
                    elif field_name == "Documents (updated)":
                        logger.info(
                            "Documents: %s updated document(s)", len(new_val))
                    else:
                        logger.info("%s: %s document(s)",
                                    field_name, len(new_val))
                elif field_name.startswith("Updates/Media") and isinstance(new_val, list):
                    if field_name == "Updates/Media (new)":
                        logger.info(
                            "Updates/Media: %s new entry(ies)", len(new_val))
                    elif field_name == "Updates/Media (updated)":
                        logger.info(
                            "Updates/Media: %s updated entry(ies)", len(new_val))
                    else:
                        logger.info("%s: %s entry(ies)",
                                    field_name, len(new_val))
                else:
                    logger.info("%s: %s", field_name, change_type)

            # Build updated case document (merge current into stored)
            updated_case = dict(case_doc)
            updated_case["title"] = case_doc.get(
                "title", "")  # keep existing or from list
            updated_case["detail_url"] = detail_url
            updated_case["description"] = current_info.get(
                "description", updated_case.get("description"))
            updated_case["case_details"] = current_info.get(
                "case_details") or updated_case.get("case_details")
            updated_case["timeline"] = current_info.get("timeline", [])
            updated_case["documents"] = current_info.get("documents", [])
            updated_case["updates_media"] = current_info.get(
                "updates_media", [])
            # Status: keep list-level in sync with case_details.Status (detail page is source of truth)
            detail_status = (updated_case.get("case_details")
                             or {}).get("Status", "")
            updated_case["status"] = (detail_status.strip() or current_info.get(
                "status") or updated_case.get("status", ""))
            updated_case["tag"] = current_info.get(
                "tag", updated_case.get("tag"))
            updated_case["outcome"] = current_info.get(
                "outcome", updated_case.get("outcome"))
            updated_case["case_number"] = (updated_case.get(
                "case_details") or {}).get("Case number", "").strip()

            parties = (updated_case.get("case_details")
                       or {}).get("Parties", "")
            description = updated_case.get("description", "")

            # If already linked to a deal, skip LLM matching and email as matched.
            existing_deal_id = case_doc.get("deal_id")
            if existing_deal_id:
                deal = get_deal_by_id(str(existing_deal_id))
                if deal:
                    updated_case["deal_id"] = str(existing_deal_id)
                    html_content = generate_nz_update_email_html(
                        updated_case, deal, changes)
                    send_nz_update_email_via_webhook(
                        updated_case, deal, html_content, changes)
                else:
                    logger.warning(
                        "Stored deal_id could not be resolved; falling back to LLM matching"
                    )
                    existing_deal_id = None

            # LLM match only when not already linked to a resolvable deal
            if not existing_deal_id:
                deal_id = match_case_to_deal(
                    title or "", parties, description or "", deals)

                if deal_id:
                    deal = get_deal_by_id(deal_id)
                    if deal:
                        updated_case["deal_id"] = deal_id
                        html_content = generate_nz_update_email_html(
                            updated_case, deal, changes)
                        send_nz_update_email_via_webhook(
                            updated_case, deal, html_content, changes)
                    else:
                        # still store deal_id
                        updated_case["deal_id"] = deal_id
                else:
                    # No match: check USA-related
                    nz_details = {
                        "title": title,
                        "parties": parties,
                        "description": description,
                        "case_details": updated_case.get("case_details"),
                    }
                    is_usa = verify_usa_relation(
                        company_details=nz_details, case_type="NZ")
                    if is_usa:
                        logger.info("USA-related – sending email and updating")
                        send_unmatched_nz_usa_email_via_webhook(updated_case)
                    else:
                        logger.info("Not USA-related – updating only")

            if update_nz_case_document(nz_collection, case_doc["_id"], updated_case):
                total_updated += 1

        browser.close()

    logger.info("Done. Checked: %s, Updated: %s", total_checked, total_updated)


if __name__ == "__main__":
    run()
