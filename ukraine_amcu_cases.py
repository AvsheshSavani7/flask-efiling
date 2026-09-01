"""
ukraine_amcu_cases.py
=====================
Track Ukraine AMCU merger cases from the public news timeline.

AMCU has no case register. One news page often contains many deals.
This script splits bullets, matches them to open ukraine_cases rows,
appends timeline steps, and closes only on Approved / Rejected.

HTML (/news/...) is the live source. PDF NPAs (/npas/...) are attached
as documents and never flip is_open.

Usage:
  python ukraine_amcu_cases.py --wipe --backfill --no-deal-match
  python ukraine_amcu_cases.py --backfill --no-deal-match --dry-run
  python ukraine_amcu_cases.py --no-deal-match

Live email (Avshesh only, never org routing):
  deal match → [FRMD] / [FRRMD]; else USA-related → [FRUD].
  New case vs case update uses the matching subject.
Backfill does not send email.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import os
import re
from collections import Counter
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from PyPDF2 import PdfReader

from deal_match_llm import fetch_open_deals, llm_match_deal_id
from deal_match_regex import apply_regex_match_subject, regex_match_ukraine_deal
from email_subject_builder import build_subject
from llm_verification_service import verify_usa_relation
from log_utils import ensure_script_logger, refresh_script_log
from mongodb_connection import get_database, get_deal_by_id, init_mongodb_connection
from n8n_email_service import send_direct_email
from ukraine_amcu_2026_backfill import (
    BASE_URL,
    QUOTED_RE,
    AmcuClient,
    classify_article,
    detect_legal_form,
    extract_ids,
    extract_parties,
    is_concentration_id,
    item_from_text,
    keep_item,
    parse_article_html,
    party_tokens,
    sitting_date_from_url,
    unwrap,
    utc_now_iso,
)

load_dotenv(".env")

SCRIPT_NAME = "ukraine_amcu_cases"
CASES_COLLECTION = "ukraine_cases"
SEEN_COLLECTION = "ukraine_amcu_seen"
BACKFILL_FROM = "2026-01-01"
LIVE_LOOKBACK_DAYS = 2
MAX_PAGES_LIVE = 8
MAX_PAGES_BACKFILL = 999

KIND_ORDER = {
    "agenda": 0,
    "case_opening": 1,
    "commitments": 2,
    "press": 3,
    "tagged_other": 4,
    "decisions": 5,
    "npa": 6,
}

CLOSE_STATUSES = {"Approved", "Rejected"}
LINKER_KINDS = {"decision", "commitments", "press"}
LINK_MODEL = "gpt-4o-mini"
TEST_RECIPIENT = "avshesh.savani@teqnodux.com"
EMAIL_UPDATE_KINDS = {"case_opening", "commitments"}

logger, get_log_file = ensure_script_logger(SCRIPT_NAME)
_openai_client: Optional[OpenAI] = None

CASE_LINK_PROMPT = """You match ONE new AMCU concentration item to an existing OPEN Ukraine case.
Return a match only if it is the same transaction (same parties AND same legal form / same target).

OPEN CASES:
{cases_block}

NEW ITEM:
date: {date}
application_id: {application_id}
case_number: {case_number}
legal_form: {legal_form}
parties: {parties}
text_uk: {text_uk}

Rules:
1. If new.case_number equals an open case_number → that KEY.
2. If new.application_id equals an open application_id → that KEY.
3. Else match on TARGET name (who is acquired / absorbed), plus buyer. Ignore generic words: Holdings, Limited, Inc, ТОВ, компанія.
4. приєднання A до B is the SAME deal as an agenda that said приєднання A до B. It is NOT the same as a different application where B acquires C.
5. Do not merge two different application_ids even if the buyer is the same (Dana, Kyivstar, Bond/BASF).
6. Do not match on industry alone.
7. Redacted names (інформація, доступ до якої обмежено) → None.
8. If two open cases fit equally → None.

RESPONSE (one line only):
- Match: KEY
- None
"""


def title_sitting_date(title: str) -> str:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", title or "")
    if not m:
        return ""
    try:
        return datetime.datetime.strptime(
            f"{m.group(1)}.{m.group(2)}.{m.group(3)}", "%d.%m.%Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def resolve_sitting_date(title: str, url: str, date_from: str) -> str:
    """Prefer title / CMS date. URL slug is last — AMCU sometimes uses the wrong year."""
    titled = title_sitting_date(title)
    if titled:
        return titled
    cms = (date_from or "")[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", cms):
        return cms
    return sitting_date_from_url(url)


def absolute_url(url: str) -> str:
    """Full URL for Mongo. Never truncate."""
    raw = (url or "").strip()
    if not raw:
        return ""
    return urljoin(BASE_URL + "/", raw)


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def is_fully_redacted(text: str, parties: List[str]) -> bool:
    usable = [
        p for p in (parties or [])
        if p and "обмежено" not in p.lower() and len(p.strip()) >= 3
    ]
    if usable:
        return False
    low = (text or "").lower()
    return "обмежено" in low or not (parties or [])


def linker_candidates(store: CaseStore, item: Dict[str, Any]) -> List[Dict[str, Any]]:
    date = item.get("date") or ""
    same_day = store.open_on_date(date) if date else []
    if same_day:
        return same_day
    return [
        c for c in store.all_open()
        if c.get("status") == "PhaseII"
        or any(s.get("kind") == "case_opening" for s in (c.get("timeline") or []))
    ]


def format_cases_block(cases: List[Dict[str, Any]]) -> str:
    lines = []
    for case in cases:
        first = (case.get("timeline") or [{}])[0]
        lines.append(
            " | ".join(
                [
                    case.get("key") or "",
                    ",".join(case.get("application_ids") or []) or "-",
                    ",".join(case.get("case_numbers") or []) or "-",
                    ", ".join((case.get("parties") or [])[:6]) or "-",
                    first.get("kind") or "",
                    (first.get("text") or "")[:180],
                ]
            )
        )
    return "\n".join(lines) if lines else "(none)"


def parse_match_key(raw: str, valid: Set[str]) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    if re.match(r"^none\b", text, re.IGNORECASE):
        return None
    m = re.search(r"Match:\s*(\S+)", text, re.IGNORECASE)
    if not m:
        return None
    key = m.group(1).strip().rstrip(".")
    return key if key in valid else None


def llm_match_open_case(
    store: CaseStore, item: Dict[str, Any], candidates: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    if is_fully_redacted(item.get("text") or "", item.get("parties") or []):
        return None
    valid = {c["key"] for c in candidates if c.get("key")}
    prompt = CASE_LINK_PROMPT.format(
        cases_block=format_cases_block(candidates).replace("{", "(").replace("}", ")"),
        date=item.get("date") or "",
        application_id=",".join(item.get("application_ids") or []) or "-",
        case_number=",".join(item.get("case_numbers") or []) or "-",
        legal_form=item.get("legal_form") or detect_legal_form(item.get("text") or ""),
        parties=", ".join(item.get("parties") or []) or "-",
        text_uk=((item.get("text") or "")[:800]).replace("{", "(").replace("}", ")"),
    )
    try:
        client = _get_openai_client()
        resp = client.chat.completions.create(
            model=LINK_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (resp.choices[0].message.content or "").strip()
        logger.info("  LLM linker: %s", raw[:200])
        key = parse_match_key(raw, valid)
        if not key:
            return None
        return store.by_key.get(key)
    except Exception as exc:
        logger.warning("  LLM linker failed: %s", exc)
        return None


def fetch_timeline(
    client: AmcuClient, date_from: str, date_to: str, max_pages: int
) -> List[Dict[str, Any]]:
    articles: List[Dict[str, Any]] = []
    page = 1
    last_page = 1
    while page <= last_page and page <= max_pages:
        url = (
            f"{BASE_URL}/api/timeline?page={page}"
            f"&date_from={date_from}&date_to={date_to}"
        )
        logger.info("listing page %s/%s", page, last_page if last_page > 1 else "?")
        payload = client.get(url, json_mode=True)
        last_page = int(unwrap(payload.get("last_page") or 1))
        grouped = payload.get("data") or {}
        for _day, rows in grouped.items():
            for row in rows:
                tags = []
                for tag in row.get("tags") or []:
                    tags.append(
                        {
                            "name": tag.get("name") or "",
                            "url": absolute_url(tag.get("url") or ""),
                        }
                    )
                articles.append(
                    {
                        "title": row.get("title") or "",
                        "url": absolute_url(row.get("url") or ""),
                        "date_from": row.get("date_from") or "",
                        "excerpt": row.get("excerpt") or "",
                        "source": row.get("source") or "",
                        "tags": tags,
                    }
                )
        page += 1
        client._sleep()
    articles.reverse()
    return articles


def is_tracked(title: str, url: str, tags: List[str]) -> bool:
    kind = classify_article(title, url, tags)
    return kind in {
        "agenda",
        "decisions",
        "case_opening",
        "commitments",
        "press",
        "npa",
    }


def target_tokens(text: str, parties: List[str]) -> List[str]:
    quotes = [re.sub(r"\s+", " ", q).strip() for q in QUOTED_RE.findall(text or "")]
    quotes = [q for q in quotes if q.lower() not in {"інформація", "доступ до якої обмежено"}]
    focus = quotes[-2:] if quotes else (parties or [])[-2:]
    return party_tokens(focus, "")


def step_signature(url: str, text: str) -> Tuple[str, str]:
    return (url or "", (text or "").strip())


def case_key_for_item(item: Dict[str, Any]) -> str:
    cases = [i for i in (item.get("case_numbers") or []) if is_concentration_id(i)]
    apps = [i for i in (item.get("application_ids") or []) if is_concentration_id(i)]
    if cases:
        return "case:" + cases[0]
    if apps:
        return "app:" + apps[0]
    tokens = party_tokens(item.get("parties") or [], "")
    if len(tokens) >= 2:
        digest = hashlib.sha1("|".join(sorted(tokens[:6])).encode("utf-8")).hexdigest()[:12]
        return "parties:" + digest
    blob = f"{item.get('article_url')}|{item.get('text','')[:80]}"
    return "orphan:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def new_case_doc(key: str, item: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
    status = step.get("status_hint") or "Pending"
    is_open = status not in CLOSE_STATUSES
    now = utc_now_iso()
    return {
        "key": key,
        "application_ids": list(item.get("application_ids") or []),
        "case_numbers": list(item.get("case_numbers") or []),
        "parties": list(item.get("parties") or []),
        "legal_form": item.get("legal_form") or detect_legal_form(item.get("text") or ""),
        "title": (item.get("text") or "")[:240],
        "first_seen": item.get("date") or "",
        "last_seen": item.get("date") or "",
        "status": status,
        "is_open": is_open,
        "timeline": [step],
        "urls": [absolute_url(step["url"])] if step.get("url") else [],
        "documents": [],
        "deal_id": None,
        "match_type": None,
        "usa_related": None,
        "created_at": now,
        "updated_at": now,
    }


def make_step(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "date": item.get("date"),
        "kind": item.get("step_kind"),
        "status_hint": item.get("status_hint"),
        "url": absolute_url(item.get("article_url") or ""),
        "article_title": item.get("article_title"),
        "article_kind": item.get("article_kind"),
        "tags": item.get("tags") or [],
        "application_ids": item.get("application_ids") or [],
        "case_numbers": item.get("case_numbers") or [],
        "order_numbers": item.get("order_numbers") or [],
        "parties": item.get("parties") or [],
        "legal_form": item.get("legal_form") or detect_legal_form(item.get("text") or ""),
        "text": item.get("text"),
    }


def merge_ids(case: Dict[str, Any], item: Dict[str, Any]) -> None:
    for field in ("application_ids", "case_numbers", "parties"):
        for val in item.get(field) or []:
            if val and val not in case[field]:
                case[field].append(val)


def apply_step(case: Dict[str, Any], step: Dict[str, Any], item: Dict[str, Any]) -> str:
    """Append a step. Returns 'updated' or 'closed'."""
    sig = step_signature(step.get("url") or "", step.get("text") or "")
    if any(step_signature(s.get("url") or "", s.get("text") or "") == sig for s in case["timeline"]):
        return "duplicate"
    case["timeline"].append(step)
    case["timeline"].sort(key=lambda s: (s.get("date") or "", KIND_ORDER.get(s.get("kind") or "", 9)))
    merge_ids(case, item)
    d = item.get("date") or ""
    if d and (not case.get("first_seen") or d < case["first_seen"]):
        case["first_seen"] = d
    if d and d > (case.get("last_seen") or ""):
        case["last_seen"] = d
    step_url = absolute_url(step.get("url") or "")
    if step_url:
        step["url"] = step_url
        if step_url not in case["urls"]:
            case["urls"].append(step_url)
    status = step.get("status_hint") or ""
    if step.get("kind") == "decision" and status in CLOSE_STATUSES:
        case["status"] = status
        case["is_open"] = False
        case["updated_at"] = utc_now_iso()
        return "closed"
    if case.get("is_open") is not False:
        if status == "PhaseII":
            case["status"] = "PhaseII"
        elif case.get("status") not in CLOSE_STATUSES:
            case["status"] = case.get("status") or "Pending"
        case["is_open"] = True
    case["updated_at"] = utc_now_iso()
    return "updated"


class CaseStore:
    def __init__(self, collection, dry_run: bool) -> None:
        self.collection = collection
        self.dry_run = dry_run
        self.by_key: Dict[str, Dict[str, Any]] = {}
        self.dirty: Set[str] = set()
        self.claimed: Set[Tuple[str, str]] = set()

    def load(self) -> None:
        if self.collection is None:
            return
        for doc in self.collection.find({}):
            key = doc.get("key")
            if not key:
                continue
            self.by_key[key] = doc
            for step in doc.get("timeline") or []:
                self.claimed.add(step_signature(step.get("url") or "", step.get("text") or ""))

    def find_by_app(self, app_id: str) -> Optional[Dict[str, Any]]:
        for case in self.by_key.values():
            if app_id in (case.get("application_ids") or []):
                return case
        return None

    def find_by_case_no(self, case_no: str) -> Optional[Dict[str, Any]]:
        for case in self.by_key.values():
            if case_no in (case.get("case_numbers") or []):
                return case
        return None

    def open_on_date(self, date: str) -> List[Dict[str, Any]]:
        out = []
        for case in self.by_key.values():
            if case.get("is_open") is False:
                continue
            dates = {s.get("date") for s in (case.get("timeline") or [])}
            if date and (date in dates or case.get("last_seen") == date or case.get("first_seen") == date):
                out.append(case)
        return out

    def all_open(self) -> List[Dict[str, Any]]:
        return [c for c in self.by_key.values() if c.get("is_open") is not False]

    def upsert(self, case: Dict[str, Any]) -> None:
        self.by_key[case["key"]] = case
        self.dirty.add(case["key"])

    def flush(self) -> int:
        if self.dry_run or self.collection is None:
            return 0
        n = 0
        for key in list(self.dirty):
            doc = dict(self.by_key[key])
            doc.pop("_id", None)
            self.collection.update_one({"key": key}, {"$set": doc}, upsert=True)
            n += 1
        self.dirty.clear()
        return n


def match_existing(store: CaseStore, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for case_no in item.get("case_numbers") or []:
        if not is_concentration_id(case_no):
            continue
        found = store.find_by_case_no(case_no)
        if found:
            return found
    for app_id in item.get("application_ids") or []:
        if not is_concentration_id(app_id):
            continue
        found = store.find_by_app(app_id)
        if found:
            return found
    return None


def match_same_day_target(store: CaseStore, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attach a decision to exactly one open case using the TARGET, not the buyer."""
    candidates = linker_candidates(store, item)
    if not candidates:
        return None

    item_tgt = set(target_tokens(item.get("text") or "", item.get("parties") or []))
    if not item_tgt:
        return None

    token_freq = Counter()
    for case in candidates:
        for t in party_tokens(case.get("parties") or [], ""):
            token_freq[t] += 1

    scored: List[Tuple[int, str, Dict[str, Any]]] = []
    for case in candidates:
        case_tgt = set(target_tokens(
            (case.get("timeline") or [{}])[0].get("text") or "",
            case.get("parties") or [],
        ))
        if not case_tgt:
            case_tgt = set(party_tokens(case.get("parties") or [], ""))
        overlap = item_tgt & case_tgt
        rare = {t for t in overlap if token_freq.get(t, 0) <= 2}
        if not rare:
            continue
        scored.append((len(rare), case["key"], case))

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, best_key, best = scored[0]
    if len(scored) > 1 and scored[1][0] == best_score:
        return None
    if best_score < 1:
        return None
    return best


def find_pdf_url(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if ".pdf" in href.lower():
            return urljoin(BASE_URL, href)
    embed = soup.select_one("embed[src], iframe[src]")
    if embed:
        src = embed.get("src") or ""
        if src:
            return urljoin(BASE_URL, src)
    return page_url


def fetch_bytes(client: AmcuClient, url: str) -> bytes:
    r = client.session.get(url, timeout=45)
    r.raise_for_status()
    return r.content


def extract_pdf_text(client: AmcuClient, url: str) -> str:
    data = fetch_bytes(client, url)
    if not data.startswith(b"%PDF"):
        return ""
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages[:12]:
        parts.append(page.extract_text() or "")
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def flatten_article(art: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind = art.get("kind") or ""
    date = resolve_sitting_date(
        art.get("title") or "",
        art.get("url") or "",
        art.get("date_from") or "",
    )
    items = list(art.get("items") or [])
    if kind == "commitments" and (art.get("full_text") or "").strip():
        row = item_from_text(re.sub(r"\s+", " ", art["full_text"])[:4000], kind, 0)
        items = [row] if row else items

    out: List[Dict[str, Any]] = []
    for item in items:
        if item.get("step_kind") == "enforcement":
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if not keep_item(text, item.get("application_ids") or [], item.get("case_numbers") or []):
            continue
        out.append(
            {
                "article_url": absolute_url(art.get("url") or ""),
                "article_title": art.get("title"),
                "article_kind": kind,
                "date": date,
                "tags": art.get("tags") or [],
                **item,
            }
        )
    return out


def _party_blob(case: Dict[str, Any], item: Dict[str, Any]) -> str:
    parties = case.get("parties") or item.get("parties") or []
    text = item.get("text") or case.get("title") or ""
    return " ".join([p for p in parties if p] + [text]).strip()


def match_to_deal(
    case: Dict[str, Any],
    item: Dict[str, Any],
    open_deals: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], bool]:
    parties = ", ".join(case.get("parties") or item.get("parties") or []) or "-"
    ids_list = (case.get("application_ids") or []) + (case.get("case_numbers") or [])
    if not ids_list:
        ids_list = (item.get("application_ids") or []) + (item.get("case_numbers") or [])
    ids = ", ".join(ids_list) or "-"
    text = (item.get("text") or case.get("title") or "")[:1200]
    deal_id: Optional[str] = None
    match_type: Optional[str] = None
    matched_by_regex = False

    try:
        deal_id = llm_match_deal_id(
            regulator_name="Ukraine AMCU",
            case_sections={
                "PARTIES": parties,
                "APPLICATION / CASE NUMBERS": ids,
                "ITEM TEXT (Ukrainian)": text or "-",
            },
            source_label="the AMCU concentration item",
            deals=open_deals,
        )
        if deal_id:
            match_type = "llm"
            logger.info("  Deal match LLM hit deal_id=%s", deal_id)
        else:
            logger.info("  Deal match LLM: no match")
    except Exception as exc:
        logger.warning("  LLM deal match failed: %s", exc)

    if not deal_id:
        deal_id = regex_match_ukraine_deal(_party_blob(case, item), open_deals)
        if deal_id:
            match_type = "regex"
            matched_by_regex = True
            logger.info("  Deal match regex hit deal_id=%s", deal_id)
        else:
            logger.info("  Deal match regex: no match")

    if deal_id and not get_deal_by_id(deal_id):
        logger.warning("  deal_id=%s matched but deal doc missing — skip email", deal_id)
        return deal_id, match_type, matched_by_regex

    return deal_id, match_type, matched_by_regex


def build_case_email(
    case: Dict[str, Any],
    item: Dict[str, Any],
    event_type: str,
    deal_match: Optional[Dict[str, Any]] = None,
    *,
    matched_by_regex: bool = False,
) -> Tuple[str, str]:
    subject = build_subject("ukraine_amcu", event_type, deal_match)
    if deal_match and matched_by_regex:
        subject = apply_regex_match_subject(subject, True)
    url = item.get("article_url") or ""
    parties = ", ".join(case.get("parties") or item.get("parties") or []) or "—"
    app_ids = ", ".join(case.get("application_ids") or item.get("application_ids") or []) or "—"
    case_nos = ", ".join(case.get("case_numbers") or item.get("case_numbers") or []) or "—"
    link = (
        f'<a href="{escape_html(url)}" target="_blank" '
        f'style="color:#0ea5e9;font-weight:600;">View AMCU article &rarr;</a>'
        if url else "—"
    )
    if deal_match:
        target = deal_match.get("target") or deal_match.get("target_name") or "N/A"
        acquirer = deal_match.get("acquirer") or deal_match.get("acquire_name") or "N/A"
        deal_id = deal_match.get("deal_id") or case.get("deal_id") or "N/A"
        banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
  <strong>Matched Deal:</strong> {escape_html(str(target))} / {escape_html(str(acquirer))}<br>
  <strong>Deal ID:</strong> {escape_html(str(deal_id))}
</div>"""
    else:
        banner = """
<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
  <strong>USA-Related Case</strong> — No deal match found; case appears related to the United States.
</div>"""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;">
  <h2 style="color:#333;margin-top:0;border-bottom:3px solid #0057b8;padding-bottom:12px;">
    {escape_html(subject)}
  </h2>
  {banner}
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Event:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html("New case" if event_type == "new" else "Case update")}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Status:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;font-weight:600;">{escape_html(str(case.get("status") or "—"))}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Key:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(str(case.get("key") or "—"))}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Application ID:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(app_ids)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Case number:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(case_nos)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Parties:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(parties)}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Date:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{escape_html(str(item.get("date") or "—"))}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#64748b;font-size:14px;">Article:</td>
      <td style="padding:6px 0 6px 12px;font-size:14px;">{link}</td>
    </tr>
  </table>
  <p style="font-size:14px;color:#334155;white-space:pre-wrap;">{escape_html((item.get("text") or "")[:1200])}</p>
  <p style="color:#999;font-size:12px;margin-top:24px;">
    Automated email — Ukraine AMCU. Test recipient only.
  </p>
</div>
</body>
</html>"""
    return subject, html


def send_case_email(
    case: Dict[str, Any],
    item: Dict[str, Any],
    event_type: str,
    deal_match: Optional[Dict[str, Any]] = None,
    *,
    matched_by_regex: bool = False,
) -> bool:
    webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
    if not webhook_url:
        logger.warning("N8N_WEBHOOK_ONLY_ME not set — email skipped")
        return False
    subject, html = build_case_email(
        case, item, event_type, deal_match, matched_by_regex=matched_by_regex
    )
    payload = {
        "subject": subject,
        "html": html,
        "url": item.get("article_url") or "",
        "source": "ukraine_amcu",
        "is_new_case": event_type == "new",
        "is_unmatched": deal_match is None,
        "deal_id": (deal_match or {}).get("deal_id") or case.get("deal_id") or "",
        "case_key": case.get("key") or "",
    }
    logger.info("[TEST] Sending to %s | %s", TEST_RECIPIENT, subject)
    return bool(send_direct_email([TEST_RECIPIENT], payload, webhook_url=webhook_url))


def maybe_email_case(
    mailer: Optional[Dict[str, Any]],
    store: CaseStore,
    case: Dict[str, Any],
    item: Dict[str, Any],
    event_type: str,
    stats: Dict[str, int],
) -> None:
    if not mailer or not mailer.get("enabled"):
        return

    deal_match: Optional[Dict[str, Any]] = None
    matched_by_regex = False
    deal_id = case.get("deal_id")
    match_type = case.get("match_type")

    if deal_id:
        deal_match = get_deal_by_id(str(deal_id))
        if deal_match:
            logger.info("  Reusing stored deal_id=%s", deal_id)
        else:
            logger.warning("  stored deal_id=%s missing deal doc — no email", deal_id)
            stats["email_skipped"] += 1
            return
    elif not mailer.get("no_deal_match"):
        deal_id, match_type, matched_by_regex = match_to_deal(
            case, item, mailer.get("open_deals") or []
        )
        if deal_id:
            case["deal_id"] = deal_id
            case["match_type"] = match_type
            store.upsert(case)
            deal_match = get_deal_by_id(str(deal_id))
            if not deal_match:
                stats["email_skipped"] += 1
                return

    if deal_match:
        try:
            if send_case_email(
                case, item, event_type, deal_match, matched_by_regex=matched_by_regex
            ):
                stats["emails_sent"] += 1
                stats["deal_matched_email"] += 1
        except Exception as exc:
            logger.warning("email failed: %s", exc)
        return

    if is_fully_redacted(item.get("text") or "", item.get("parties") or []):
        stats["email_skipped"] += 1
        logger.info("  USA check skipped (redacted)")
        return

    try:
        is_usa = bool(
            verify_usa_relation(
                company_details=_party_blob(case, item)[:2000],
                case_type="UKRAINE",
            )
        )
    except Exception as exc:
        logger.warning("  USA check failed: %s", exc)
        is_usa = False
    case["usa_related"] = is_usa
    store.upsert(case)
    logger.info("  USA check: %s", is_usa)
    if not is_usa:
        stats["email_skipped"] += 1
        return
    try:
        if send_case_email(case, item, event_type, None):
            stats["emails_sent"] += 1
            stats["usa_related_email"] += 1
    except Exception as exc:
        logger.warning("email failed: %s", exc)


def process_item(
    store: CaseStore,
    item: Dict[str, Any],
    stats: Dict[str, int],
    mailer: Optional[Dict[str, Any]] = None,
) -> None:
    sig = step_signature(item.get("article_url") or "", item.get("text") or "")
    if sig in store.claimed:
        stats["skipped_claimed"] += 1
        return

    if not item.get("legal_form"):
        item["legal_form"] = detect_legal_form(item.get("text") or "")

    step = make_step(item)
    found = match_existing(store, item)
    if found is None and item.get("step_kind") in LINKER_KINDS:
        found = match_same_day_target(store, item)
        if found is None:
            found = llm_match_open_case(store, item, linker_candidates(store, item))

    if found is not None:
        result = apply_step(found, step, item)
        store.claimed.add(sig)
        store.upsert(found)
        if result == "closed":
            stats["closed"] += 1
            logger.info("CLOSE %s [%s] %s", found["key"], step.get("status_hint"), (item.get("text") or "")[:80])
            maybe_email_case(mailer, store, found, item, "update", stats)
        elif result == "updated":
            stats["updated"] += 1
            logger.info("UPDATE %s [%s] %s", found["key"], step.get("kind"), (item.get("text") or "")[:80])
            if item.get("step_kind") in EMAIL_UPDATE_KINDS:
                maybe_email_case(mailer, store, found, item, "update", stats)
        else:
            stats["skipped_claimed"] += 1
        return

    kind = item.get("step_kind") or ""
    same_day_open = linker_candidates(store, item)
    redacted = is_fully_redacted(item.get("text") or "", item.get("parties") or [])

    if kind == "decision" and item.get("status_hint") in CLOSE_STATUSES:
        if same_day_open and not redacted:
            stats["unmatched"] += 1
            logger.info(
                "UNMATCHED decision (not forking) %s",
                (item.get("text") or "")[:80],
            )
            return
        key = case_key_for_item(item)
        doc = new_case_doc(key, item, step)
        store.claimed.add(sig)
        store.upsert(doc)
        stats["new_closed"] += 1
        logger.info("NEW-CLOSED %s %s", key, (item.get("text") or "")[:80])
        maybe_email_case(mailer, store, doc, item, "new", stats)
        return

    if kind in {"agenda", "case_opening", "commitments", "press"}:
        key = case_key_for_item(item)
        if key in store.by_key:
            found = store.by_key[key]
            result = apply_step(found, step, item)
            store.claimed.add(sig)
            store.upsert(found)
            stats["updated" if result != "closed" else "closed"] += 1
            if result == "closed" or item.get("step_kind") in EMAIL_UPDATE_KINDS:
                maybe_email_case(mailer, store, found, item, "update", stats)
            return
        doc = new_case_doc(key, item, step)
        store.claimed.add(sig)
        store.upsert(doc)
        stats["new_open"] += 1
        logger.info("NEW %s is_open=%s %s", key, doc["is_open"], (item.get("text") or "")[:80])
        maybe_email_case(mailer, store, doc, item, "new", stats)
        return

    stats["unmatched"] += 1


def process_npa(
    store: CaseStore,
    client: AmcuClient,
    art: Dict[str, Any],
    stats: Dict[str, int],
) -> None:
    html = art.get("_html") or ""
    page_url = absolute_url(art.get("url") or "")
    pdf_url = absolute_url(find_pdf_url(html, page_url) if html else page_url)
    text = ""
    try:
        if pdf_url:
            text = extract_pdf_text(client, pdf_url)
    except Exception as exc:
        logger.warning("NPA pdf extract failed %s: %s", page_url, exc)
    if not text:
        text = (art.get("full_text") or "").strip()
    ids = extract_ids(text or art.get("title") or "")
    parties = extract_parties(text) if text else []
    fake_item = {
        "application_ids": ids["application_ids"],
        "case_numbers": ids["case_numbers"],
        "parties": parties,
        "text": (text or art.get("title") or "")[:500],
        "date": resolve_sitting_date(art.get("title") or "", page_url, art.get("date_from") or ""),
        "step_kind": "npa",
    }
    found = match_existing(store, fake_item)
    doc = {
        "url": page_url,
        "pdf_url": pdf_url if pdf_url != page_url else page_url,
        "title": art.get("title"),
        "date": fake_item["date"],
        "tags": art.get("tags") or [],
        "application_ids": ids["application_ids"],
        "case_numbers": ids["case_numbers"],
        "parties": parties[:8],
        "excerpt": (text or "")[:500],
    }
    if found is None:
        stats["npa_unmatched"] += 1
        logger.info("NPA unmatched %s", page_url)
        return
    existing = found.get("documents") or []
    if any(d.get("url") == page_url for d in existing):
        stats["npa_duplicate"] += 1
        return
    existing.append(doc)
    found["documents"] = existing
    merge_ids(found, fake_item)
    found["updated_at"] = utc_now_iso()
    store.upsert(found)
    stats["npa_attached"] += 1
    logger.info("NPA attached → %s %s", found["key"], page_url)


def seen_urls(collection) -> Set[str]:
    if collection is None:
        return set()
    return {d["_id"] for d in collection.find({}, {"_id": 1})}


def mark_seen(collection, url: str, kind: str, dry_run: bool) -> None:
    url = absolute_url(url)
    if dry_run or collection is None or not url:
        return
    collection.update_one(
        {"_id": url},
        {"$set": {"kind": kind, "seen_at": utc_now_iso()}},
        upsert=True,
    )


def wipe_ukraine_collections(cases_coll, seen_coll, *, dry_run: bool) -> Tuple[int, int]:
    if dry_run:
        n_cases = cases_coll.count_documents({}) if cases_coll is not None else 0
        n_seen = seen_coll.count_documents({}) if seen_coll is not None else 0
        logger.info("dry-run wipe would delete %s cases and %s seen urls", n_cases, n_seen)
        return n_cases, n_seen
    n_cases = cases_coll.delete_many({}).deleted_count if cases_coll is not None else 0
    n_seen = seen_coll.delete_many({}).deleted_count if seen_coll is not None else 0
    logger.info("wiped %s ukraine_cases and %s ukraine_amcu_seen", n_cases, n_seen)
    return n_cases, n_seen


def run_ukraine_amcu_cases(
    *,
    backfill: bool = False,
    dry_run: bool = False,
    no_deal_match: bool = True,
    max_pages: Optional[int] = None,
    force: bool = False,
    wipe: bool = False,
) -> Dict[str, int]:
    refresh_script_log(logger, get_log_file)
    date_to = datetime.date.today().isoformat()
    date_from = BACKFILL_FROM if backfill else (
        datetime.date.today() - datetime.timedelta(days=LIVE_LOOKBACK_DAYS)
    ).isoformat()
    pages = max_pages or (MAX_PAGES_BACKFILL if backfill else MAX_PAGES_LIVE)

    stats = Counter(
        {
            "listing": 0,
            "relevant": 0,
            "fetched": 0,
            "skipped_seen": 0,
            "new_open": 0,
            "new_closed": 0,
            "updated": 0,
            "closed": 0,
            "skipped_claimed": 0,
            "unmatched": 0,
            "npa_attached": 0,
            "npa_unmatched": 0,
            "npa_duplicate": 0,
            "written": 0,
            "emails_sent": 0,
            "deal_matched_email": 0,
            "usa_related_email": 0,
            "email_skipped": 0,
        }
    )

    logger.info("=" * 60)
    logger.info(
        "Ukraine AMCU cases  %s → %s  backfill=%s dry_run=%s wipe=%s no_deal_match=%s",
        date_from, date_to, backfill, dry_run, wipe, no_deal_match,
    )

    cases_coll = None
    seen_coll = None
    if not dry_run:
        ok, msg = init_mongodb_connection()
        if not ok:
            raise SystemExit(msg)
        db = get_database()
        cases_coll = db[CASES_COLLECTION]
        seen_coll = db[SEEN_COLLECTION]
        cases_coll.create_index("key", unique=True)
        cases_coll.create_index("is_open")
        cases_coll.create_index("application_ids")
        cases_coll.create_index("case_numbers")

    if wipe:
        wipe_ukraine_collections(cases_coll, seen_coll, dry_run=dry_run)

    store = CaseStore(cases_coll, dry_run)
    store.load()
    logger.info("loaded %s existing cases", len(store.by_key))
    already = set() if force else seen_urls(seen_coll)

    open_deals: List[Dict[str, Any]] = []
    mailer_on = (not dry_run) and (not backfill)
    if mailer_on and not no_deal_match:
        try:
            open_deals = fetch_open_deals()
            logger.info("loaded %s open deals for matching", len(open_deals))
        except Exception as exc:
            logger.warning("fetch_open_deals failed: %s", exc)
    mailer = {
        "enabled": mailer_on,
        "no_deal_match": no_deal_match,
        "open_deals": open_deals,
    }
    if backfill:
        logger.info("email skipped (backfill) — recipient would be %s", TEST_RECIPIENT)
    elif dry_run:
        logger.info("email skipped (dry-run)")
    else:
        logger.info(
            "email recipient (test only): %s | deal_match=%s | FRMD then USA/FRUD",
            TEST_RECIPIENT,
            not no_deal_match,
        )

    client = AmcuClient()
    logger.info("bootstrapping AMCU session")
    client.bootstrap()
    listing = fetch_timeline(client, date_from, date_to, pages)
    stats["listing"] = len(listing)

    relevant = [
        a for a in listing
        if is_tracked(a["title"], a["url"], [t["name"] for t in a["tags"]])
    ]
    stats["relevant"] = len(relevant)

    parsed: List[Dict[str, Any]] = []
    for i, meta in enumerate(relevant, 1):
        url = meta.get("url") or ""
        kind = classify_article(meta["title"], url, [t["name"] for t in meta["tags"]])
        if url in already:
            stats["skipped_seen"] += 1
            continue
        logger.info("[%s/%s] %s %s", i, len(relevant), kind, (meta.get("title") or "")[:70])
        try:
            html = client.get(url)
            art = parse_article_html(html, meta)
            art["kind"] = kind
            art["_html"] = html if kind == "npa" else ""
            parsed.append(art)
            stats["fetched"] += 1
        except Exception as exc:
            logger.error("FAIL %s: %s", url, exc)
            parsed.append({**meta, "kind": kind, "items": [], "full_text": "", "error": str(exc)})
        client._sleep()

    # Newest sitting first (last listing record first). Same day: agenda
    # before decisions (stable sort on kind, then date descending).
    parsed.sort(key=lambda a: KIND_ORDER.get(a.get("kind") or "", 9))
    parsed.sort(
        key=lambda a: resolve_sitting_date(
            a.get("title") or "", a.get("url") or "", a.get("date_from") or ""
        ),
        reverse=True,
    )
    logger.info("processing %s articles newest → oldest (same-day agenda before decisions)", len(parsed))

    for art in parsed:
        kind = art.get("kind") or ""
        if kind == "npa":
            process_npa(store, client, art, stats)
        else:
            for item in flatten_article(art):
                process_item(store, item, stats, mailer)
        mark_seen(seen_coll, art.get("url") or "", kind, dry_run)

    if no_deal_match:
        logger.info("deal_id matching skipped (--no-deal-match)")

    stats["written"] = store.flush()
    logger.info("=" * 60)
    logger.info("SUMMARY")
    for key, val in stats.items():
        logger.info("  %-20s: %s", key, val)
    logger.info("  in-memory cases    : %s", len(store.by_key))
    logger.info("=" * 60)
    return dict(stats)


def main() -> None:
    p = argparse.ArgumentParser(description="Ukraine AMCU merger case tracker")
    p.add_argument("--backfill", action="store_true", help="Scrape from 2026-01-01")
    p.add_argument("--dry-run", action="store_true", help="No MongoDB writes")
    p.add_argument("--no-deal-match", action="store_true", help="Do not run deal_id matching")
    p.add_argument("--force", action="store_true", help="Re-process URLs already in ukraine_amcu_seen")
    p.add_argument("--wipe", action="store_true", help="Delete ukraine_cases and ukraine_amcu_seen before running")
    p.add_argument("--max-pages", type=int, default=None, help="Cap timeline pages")
    args = p.parse_args()
    run_ukraine_amcu_cases(
        backfill=args.backfill,
        dry_run=args.dry_run,
        no_deal_match=args.no_deal_match,
        max_pages=args.max_pages,
        force=args.force,
        wipe=args.wipe,
    )


if __name__ == "__main__":
    main()
