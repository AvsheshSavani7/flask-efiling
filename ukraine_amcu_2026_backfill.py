"""
ukraine_amcu_2026_backfill.py
=============================
Research backfill: scrape AMCU 2026 news (Jan 1 → today), split merger items,
group into cases, append later articles as steps.

Source:
  GET https://amcu.gov.ua/api/timeline?page=N&date_from=2026-01-01&date_to=YYYY-MM-DD
  then fetch each article HTML.

Output:
  ukraine_amcu_2026_cases.json      — cases with steps[] + npa_index
  ukraine_amcu_2026_articles.json   — raw articles (resume cache)

Usage:
  python ukraine_amcu_2026_backfill.py
  python ukraine_amcu_2026_backfill.py --max-pages 3
  python ukraine_amcu_2026_backfill.py --skip-fetch   # regroup from ukraine_amcu_2026_articles.json

Usage:
  python ukraine_amcu_2026_backfill.py
  python ukraine_amcu_2026_backfill.py --max-pages 3
  python ukraine_amcu_2026_backfill.py --skip-fetch   # regroup from cached articles
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

BASE_URL = "https://amcu.gov.ua"
ARTICLES_JSON = "ukraine_amcu_2026_articles.json"
CASES_JSON = "ukraine_amcu_2026_cases.json"
DATE_FROM = "2026-01-01"

REQUEST_TIMEOUT = 45
REQUEST_DELAY = 0.45
MAX_RETRIES = 3

HTTP_HEADERS = {
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Referer": BASE_URL + "/",
}

# Application numbers on agendas: 15-01/303-ЕКк, 8-01/256-ЕКк, 8-01/344-ПВк
APP_ID_RE = re.compile(
    r"№\s*(\d{1,3}-\d{2}/\d{1,4}-[А-ЯA-Z]{2,4}к?)",
    re.IGNORECASE,
)
APP_ID_BARE_RE = re.compile(
    r"(\d{1,3}-\d{2}/\d{1,4}-[А-ЯA-Z]{2,4}к?)",
    re.IGNORECASE,
)
# Phase II / opened cases: 130-25/5-26-ЕК, 126-25/3-26-ЕК, 130-25/7-25-ЕКк
CASE_NO_RE = re.compile(
    r"справи\s*№\s*(\d{2,4}-\d{2}(?:\.\d{2})?/\d{1,4}-\d{2}-[А-ЯA-Z]{2,4}к?)",
    re.IGNORECASE,
)
CASE_NO_BARE_RE = re.compile(
    r"(\d{2,4}-\d{2}(?:\.\d{2})?/\d{1,4}-\d{2}-[А-ЯA-Z]{2,4}к?)",
    re.IGNORECASE,
)
ORDER_NO_RE = re.compile(r"№\s*(\d{1,3}/\d{1,3}-р)", re.IGNORECASE)
QUOTED_RE = re.compile(r"[«\"]([^»\"]{2,120})[»\"]")
LATIN_CO_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\'-]*(?:\s+[A-Z][A-Za-z0-9&.\'-]*){0,6}"
    r"(?:\s+(?:Ltd|LLC|Inc|plc|GmbH|AG|SE|BV|B\.V\.|SAS|SA|LP|LLP|Oy|Oyj|SCSp))?)\b"
)
UA_CO_RE = re.compile(
    r"\b((?:ТОВ|ПП|ПрАТ|ПАТ|АТ|ДП|ТДВ|ПрАТ)\s+[«\"][^»\"]+[»\"])",
    re.IGNORECASE,
)

SUFFIX_KEEP = ("ЕК", "ЕКК")
SUFFIX_DROP = ("УД", "УДК", "ПВ", "ПВК")

TITLE_KEEP_RE = re.compile(
    r"(концентрац|набуття контрол|приєднан|злитт|"
    r"порядку денного|"
    r"інформація про рішення|"
    r"початку розгляду справ[иа].*концентрац|"
    r"зобов.?язанн.*концентрац|"
    r"розпочав\s+розгляд справи про концентрац|"
    r"надав дозвіл.*концентрац|"
    r"без дозволу)",
    re.IGNORECASE | re.DOTALL,
)

CONC_ITEM_RE = re.compile(
    r"(концентрац|набуття.{0,200}контрол|надано дозвіл.{0,400}контрол|"
    r"приєднан|злитт|"
    r"створення.{0,80}(компан|підприємств|спільного)|"
    r"розпочато розгляд справи.{0,80}концентрац|"
    r"зобов.?язанн)",
    re.IGNORECASE | re.DOTALL,
)

MERGER_ABSORPTION_RE = re.compile(r"приєднан|злитт", re.IGNORECASE)
JV_RE = re.compile(r"створення.{0,80}(компан|підприємств|спільного)", re.IGNORECASE)
ENFORCEMENT_RE = re.compile(
    r"(без отримання відповідного дозволу|оштрафовано.{0,80}концентрац|"
    r"пунктом\s*12\s*статті\s*50)",
    re.IGNORECASE | re.DOTALL,
)
UD_RE = re.compile(r"узгоджен[іих]+\s+ді[їй]|неконкуренц", re.IGNORECASE)


def utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def unwrap(val: Any) -> Any:
    if isinstance(val, list) and len(val) == 1:
        return val[0]
    return val


def normalize_id(raw: str) -> str:
    text = (raw or "").strip().upper().replace(" ", "")
    text = text.replace("ЕКК", "ЕК").replace("УДК", "УД").replace("ПВК", "ПВ")
    return text


def id_suffix(norm: str) -> str:
    if "-" not in norm:
        return ""
    return norm.rsplit("-", 1)[-1]


def is_concentration_id(norm: str) -> bool:
    suf = id_suffix(norm)
    return suf in SUFFIX_KEEP


def is_dropped_id(norm: str) -> bool:
    suf = id_suffix(norm)
    return suf in SUFFIX_DROP


def sitting_date_from_url(url: str) -> str:
    m = re.search(r"(\d{8})(?:/|$|\?)", url or "")
    if not m:
        return ""
    raw = m.group(1)
    try:
        return datetime.datetime.strptime(raw, "%d%m%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


class AmcuClient:
    def __init__(self) -> None:
        self.session = cffi_requests.Session(impersonate="chrome120")
        self.session.headers.update(HTTP_HEADERS)
        self.csrf = ""

    def _sleep(self) -> None:
        time.sleep(REQUEST_DELAY)

    def bootstrap(self) -> None:
        self.session.get(BASE_URL + "/", timeout=REQUEST_TIMEOUT)
        self._refresh_csrf()

    def _refresh_csrf(self) -> None:
        r = self.session.get(
            BASE_URL + "/csrf-token",
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        self.csrf = r.json() if isinstance(r.json(), str) else str(r.json())

    def get(self, url: str, *, json_mode: bool = False) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                headers = {}
                if json_mode:
                    headers = {
                        "X-CSRF-TOKEN": self.csrf,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json",
                    }
                r = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                if r.status_code in (403, 419, 429) and attempt < MAX_RETRIES:
                    self.bootstrap()
                    self._sleep()
                    continue
                r.raise_for_status()
                if json_mode:
                    return r.json()
                return r.text
            except Exception as exc:
                last_err = exc
                self._sleep()
                if attempt == MAX_RETRIES:
                    raise
        raise last_err or RuntimeError("fetch failed")


def classify_article(title: str, url: str, tags: List[str]) -> str:
    t = (title or "").lower()
    u = (url or "").lower()
    tagblob = " ".join(tags).lower()
    if "/npas/" in u:
        if "концентрац" in t or "набуття контрол" in t:
            return "npa"
        return "skip"
    if "порядку денного" in t or "proiekt-poriadku-dennoho" in u:
        return "agenda"
    if "інформація про рішення" in t or "informatsiia-pro-rishennia" in u:
        return "decisions"
    if "початку розгляду справ" in t and "концентрац" in t:
        return "case_opening"
    if "зобов" in t and "концентрац" in t:
        return "commitments"
    if "концентрац" in t or "набуття контрол" in t:
        return "press"
    if "концентрац" in tagblob or "надання дозволу на концентрацію" in tagblob:
        return "tagged_other"
    return "skip"


def article_is_relevant(title: str, url: str, tags: List[str]) -> bool:
    kind = classify_article(title, url, tags)
    if kind != "skip":
        return True
    return bool(TITLE_KEEP_RE.search(title or "") or TITLE_KEEP_RE.search(url or ""))


def extract_ids(text: str) -> Dict[str, List[str]]:
    apps, cases, orders = [], [], []
    for rx, bucket, use_norm in (
        (APP_ID_RE, apps, True),
        (CASE_NO_RE, cases, True),
        (ORDER_NO_RE, orders, False),
    ):
        for m in rx.finditer(text or ""):
            raw = m.group(1)
            val = normalize_id(raw) if use_norm else raw.strip()
            if val and val not in bucket:
                bucket.append(val)
    if not apps:
        for m in APP_ID_BARE_RE.finditer(text or ""):
            val = normalize_id(m.group(1))
            if val and val not in apps and not is_dropped_id(val):
                # avoid swallowing case numbers that look similar
                if re.match(r"^\d{1,3}-\d{2}/\d{1,4}-", val):
                    apps.append(val)
    if not cases:
        for m in CASE_NO_BARE_RE.finditer(text or ""):
            val = normalize_id(m.group(1))
            if "/20" in val or re.search(r"/\d{1,4}-\d{2}-", val):
                if val not in cases:
                    cases.append(val)
    return {"application_ids": apps, "case_numbers": cases, "order_numbers": orders}


PARTY_SKIP = {
    "інформація",
    "доступ до якої обмежено",
    "інформація, доступ до якої обмежено",
    "україни",
    "київ",
    "limited",
    "inc",
    "co",
    "ltd",
    "llc",
    "plc",
    "gmbh",
    "ag",
    "se",
    "bv",
    "sa",
    "lp",
    "llp",
}


def extract_parties(text: str) -> List[str]:
    parties: List[str] = []
    for rx in (UA_CO_RE, QUOTED_RE, LATIN_CO_RE):
        for m in rx.finditer(text or ""):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" «»\"'.,;")
            if len(name) < 3:
                continue
            low = name.lower().rstrip(".")
            if low in PARTY_SKIP or "доступ до якої обмежено" in low:
                continue
            if low.startswith("про "):
                continue
            if name not in parties:
                parties.append(name)
    # drop fragments already covered by a longer name ("Inc", "Organon")
    cleaned: List[str] = []
    for name in parties:
        if any(
            other != name and name.lower() in other.lower() and len(other) > len(name) + 2
            for other in parties
        ):
            continue
        cleaned.append(name)
    return cleaned[:12]


def party_tokens(parties: List[str], extra_text: str = "") -> List[str]:
    blob = " ".join(parties) + " " + extra_text
    blob = blob.lower()
    blob = re.sub(
        r"\b(тов|пп|прат|пат|ат|дп|тдв|ltd|llc|inc|plc|gmbh|ag|se|bv|sa|lp)\b",
        " ",
        blob,
    )
    blob = re.sub(r"[«»\"'.,;:()\[\]]", " ", blob)
    tokens = [t for t in blob.split() if len(t) >= 4]
    # keep distinctive tokens
    stop = {
        "компанії",
        "компанія",
        "компанією",
        "контроль",
        "контролю",
        "набуття",
        "шляхом",
        "придбання",
        "україни",
        "громадянину",
        "фізичній",
        "особі",
        "інформація",
        "доступ",
        "обмежено",
        "limited",
        "holdings",
        "holding",
        "group",
        "company",
        "надання",
        "дозволу",
        "дозвіл",
        "результати",
        "розгляду",
        "заяви",
        "спільного",
        "разом",
        "опосередковане",
        "прямого",
        "захист",
        "економічної",
        "конкуренції",
        "законодавства",
        "порушення",
        "штраф",
        "оштрафовано",
        "персональні",
        "дані",
        "антимонопольного",
        "комітету",
        "mexico",
        "netherlands",
        "home",
        "products",
    }
    out = []
    for t in tokens:
        if t in stop:
            continue
        if t not in out:
            out.append(t)
    return out[:16]


def item_kind_and_status(article_kind: str, text: str) -> Tuple[str, str]:
    if ENFORCEMENT_RE.search(text or ""):
        return "enforcement", "Fined"
    if article_kind == "agenda":
        return "agenda", "Pending"
    if article_kind == "case_opening":
        return "case_opening", "PhaseII"
    if article_kind == "commitments":
        return "commitments", "Pending"
    if article_kind == "decisions":
        low = text.lower()
        if "надано дозвіл" in low:
            return "decision", "Approved"
        if "відмовлено" in low and "концентрац" in low:
            return "decision", "Rejected"
        return "decision", "Unknown"
    return article_kind, "Unknown"


DECISION_SPLIT_RE = re.compile(
    r"(?=Надано дозвіл|Оштрафовано|Відмовлено|Залишено\s+без змін|Розпорядженням|Схвалено проєкт)",
)


def split_decision_text(full_text: str) -> List[str]:
    parts = [p.strip() for p in DECISION_SPLIT_RE.split(full_text or "") if p.strip()]
    out = []
    for p in parts:
        t = re.sub(r"\s+", " ", p)
        if len(t) > 40:
            out.append(t)
    return out


def detect_legal_form(text: str) -> str:
    low = (text or "").lower()
    if MERGER_ABSORPTION_RE.search(text or ""):
        return "merger_absorption"
    if JV_RE.search(text or ""):
        return "joint_venture"
    if "спільного контролю" in low or "спільний контроль" in low:
        return "joint_control"
    if "оренд" in low and "актив" in low:
        return "asset_lease"
    if "опосередковане" in low or "опосередкованого" in low:
        return "indirect_control"
    if "набуття" in low and "контрол" in low:
        return "acquisition_of_control"
    if "концентрац" in low:
        return "concentration"
    return "unknown"


def keep_item(text: str, application_ids: List[str], case_numbers: List[str]) -> bool:
    conc_ids = [i for i in application_ids + case_numbers if is_concentration_id(i)]
    drop_only = application_ids + case_numbers and not conc_ids and all(
        is_dropped_id(i) for i in application_ids + case_numbers
    )
    if drop_only:
        return False
    if UD_RE.search(text) and not CONC_ITEM_RE.search(text) and not conc_ids:
        return False
    if conc_ids:
        return True
    low = (text or "").lower()
    if "надано дозвіл" in low and any(
        w in low for w in ("контрол", "приєднан", "злитт", "створення", "концентрац")
    ):
        return True
    if CONC_ITEM_RE.search(text):
        return True
    if ENFORCEMENT_RE.search(text):
        return True
    return False


def parse_editor_items(html: str, article_kind: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    editor = soup.select_one(".editor-content")
    if editor is None:
        return []
    full = editor.get_text("\n", strip=True)

    if article_kind in {"decisions", "case_opening"}:
        split = split_decision_text(full)
        if split:
            return split

    lis = [
        re.sub(r"\s+", " ", li.get_text(" ", strip=True))
        for li in editor.select("li")
        if li.get_text(strip=True)
    ]
    if lis:
        return [t for t in lis if len(t) > 40]

    chunks: List[str] = []
    if article_kind == "agenda":
        in_section_i = False
        for p in editor.find_all("p"):
            t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
            if not t:
                continue
            if re.match(r"^І[\.\s]", t) and "концентрац" in t.lower():
                in_section_i = True
                continue
            if re.match(r"^ІІ[\.\s]", t):
                in_section_i = False
                continue
            if not in_section_i:
                continue
            if t.lower().startswith("(матеріали"):
                continue
            chunks.append(t)
        return chunks

    for p in editor.find_all("p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(t) > 60 and CONC_ITEM_RE.search(t):
            chunks.append(t)
    if not chunks:
        full_one = re.sub(r"\s+", " ", full)
        if full_one:
            chunks.append(full_one[:4000])
    return chunks


def item_from_text(text: str, article_kind: str, index: int) -> Optional[Dict[str, Any]]:
    ids = extract_ids(text)
    if not keep_item(text, ids["application_ids"], ids["case_numbers"]):
        return None
    step_kind, status = item_kind_and_status(article_kind, text)
    return {
        "index": index,
        "text": text,
        "application_ids": ids["application_ids"],
        "case_numbers": ids["case_numbers"],
        "order_numbers": ids["order_numbers"],
        "parties": extract_parties(text),
        "legal_form": detect_legal_form(text),
        "step_kind": step_kind,
        "status_hint": status,
    }


def rebuild_items_from_full_text(art: Dict[str, Any]) -> Dict[str, Any]:
    """Re-split cached articles without refetching HTML (fixes <p> vs <li> dumps)."""
    kind = classify_article(
        art.get("title") or "",
        art.get("url") or "",
        [t.get("name") or "" for t in (art.get("tags") or [])],
    )
    full = art.get("full_text") or ""
    raw: List[str] = []
    if kind in {"decisions", "case_opening"}:
        raw = split_decision_text(full)
    elif art.get("items"):
        raw = [it.get("text") or "" for it in art["items"] if it.get("text")]
    elif full.strip():
        raw = [re.sub(r"\s+", " ", full)[:4000]]

    items: List[Dict[str, Any]] = []
    for idx, text in enumerate(raw):
        if not (text or "").strip():
            continue
        row = item_from_text(text, kind, idx)
        if row:
            items.append(row)
    if not items and kind in {"press", "commitments", "case_opening"} and full.strip():
        row = item_from_text(re.sub(r"\s+", " ", full)[:4000], kind, 0)
        if row:
            items.append(row)
    art["kind"] = kind
    art["items"] = items
    return art


def parse_article_html(html: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = meta.get("title") or (
        soup.title.get_text(strip=True).split("|")[0].strip() if soup.title else ""
    )
    published = ""
    for sel in (".date", "time", ".news-date", ".published"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            published = el.get_text(strip=True)
            break
    tag_els = soup.select("a.tag, .tag_wrap a")
    page_tags = []
    for a in tag_els:
        name = a.get_text(strip=True)
        href = a.get("href") or ""
        if name:
            page_tags.append(
                {"name": name, "url": urljoin(BASE_URL, href) if href else ""}
            )
    if not page_tags:
        page_tags = meta.get("tags") or []

    kind = classify_article(title, meta.get("url", ""), [t["name"] for t in page_tags])
    editor = soup.select_one(".editor-content")
    full_text = editor.get_text("\n", strip=True) if editor else ""
    raw_items = parse_editor_items(html, kind)

    items: List[Dict[str, Any]] = []
    for idx, text in enumerate(raw_items):
        ids = extract_ids(text)
        if not keep_item(text, ids["application_ids"], ids["case_numbers"]):
            continue
        step_kind, status = item_kind_and_status(kind, text)
        items.append(
            {
                "index": idx,
                "text": text,
                "application_ids": ids["application_ids"],
                "case_numbers": ids["case_numbers"],
                "order_numbers": ids["order_numbers"],
                "parties": extract_parties(text),
                "legal_form": detect_legal_form(text),
                "step_kind": step_kind,
                "status_hint": status,
            }
        )

    # whole-article fallback when no split items but article is clearly M&A
    if not items and kind in {"press", "commitments", "tagged_other", "case_opening"}:
        ids = extract_ids(full_text)
        if keep_item(full_text, ids["application_ids"], ids["case_numbers"]) or kind in {
            "press",
            "commitments",
            "case_opening",
        }:
            step_kind, status = item_kind_and_status(kind, full_text)
            items.append(
                {
                    "index": 0,
                    "text": re.sub(r"\s+", " ", full_text)[:4000],
                    "application_ids": ids["application_ids"],
                    "case_numbers": ids["case_numbers"],
                    "order_numbers": ids["order_numbers"],
                    "parties": extract_parties(full_text),
                    "legal_form": detect_legal_form(full_text),
                    "step_kind": step_kind,
                    "status_hint": status,
                }
            )

    return {
        "url": meta.get("url"),
        "title": title,
        "kind": kind,
        "published_raw": published,
        "date_from": meta.get("date_from"),
        "sitting_date": sitting_date_from_url(meta.get("url") or "")
        or (meta.get("date_from") or "")[:10],
        "tags": page_tags,
        "listing_tags": meta.get("tags") or [],
        "excerpt": meta.get("excerpt") or "",
        "full_text": full_text,
        "items": items,
    }


def fetch_timeline(client: AmcuClient, date_to: str, max_pages: int) -> List[Dict[str, Any]]:
    articles: List[Dict[str, Any]] = []
    page = 1
    last_page = 1
    while page <= last_page and page <= max_pages:
        url = (
            f"{BASE_URL}/api/timeline?page={page}"
            f"&date_from={DATE_FROM}&date_to={date_to}"
        )
        print(f"  listing page {page}/{last_page if last_page > 1 else '?'} ...", flush=True)
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
                            "url": urljoin(BASE_URL, tag.get("url") or ""),
                        }
                    )
                articles.append(
                    {
                        "title": row.get("title") or "",
                        "url": row.get("url") or "",
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


def load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def group_cases(parsed_articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pending: List[Dict[str, Any]] = []
    for art in parsed_articles:
        if art.get("kind") == "npa" or "/npas/" in (art.get("url") or ""):
            continue
        for item in art.get("items") or []:
            if not (item.get("text") or "").strip():
                continue
            pending.append(
                {
                    "article_url": art["url"],
                    "article_title": art["title"],
                    "article_kind": art["kind"],
                    "date": art.get("sitting_date") or (art.get("date_from") or "")[:10],
                    "tags": art.get("tags") or [],
                    **item,
                }
            )

    cases: Dict[str, Dict[str, Any]] = {}
    unkeyed: List[Dict[str, Any]] = []

    def new_case(key: str, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": key,
            "application_ids": list(item.get("application_ids") or []),
            "case_numbers": list(item.get("case_numbers") or []),
            "parties": list(item.get("parties") or []),
            "first_seen": item.get("date") or "",
            "last_seen": item.get("date") or "",
            "statuses": [],
            "steps": [],
        }

    def add_step(case: Dict[str, Any], item: Dict[str, Any]) -> None:
        step = {
            "date": item.get("date"),
            "kind": item.get("step_kind"),
            "status_hint": item.get("status_hint"),
            "url": item.get("article_url"),
            "article_title": item.get("article_title"),
            "article_kind": item.get("article_kind"),
            "tags": item.get("tags") or [],
            "application_ids": item.get("application_ids") or [],
            "case_numbers": item.get("case_numbers") or [],
            "order_numbers": item.get("order_numbers") or [],
            "parties": item.get("parties") or [],
            "text": item.get("text"),
        }
        # skip exact duplicate steps (same url+text)
        sig = (step["url"], step["text"])
        if any((s.get("url"), s.get("text")) == sig for s in case["steps"]):
            return
        case["steps"].append(step)
        for field in ("application_ids", "case_numbers", "parties"):
            for val in item.get(field) or []:
                if val not in case[field]:
                    case[field].append(val)
        st = item.get("status_hint")
        if st and st not in case["statuses"]:
            case["statuses"].append(st)
        d = item.get("date") or ""
        if d and (not case["first_seen"] or d < case["first_seen"]):
            case["first_seen"] = d
        if d and d > case["last_seen"]:
            case["last_seen"] = d

    # Pass 1: strong IDs
    for item in pending:
        conc_cases = [i for i in item["case_numbers"] if is_concentration_id(i)]
        conc_apps = [i for i in item["application_ids"] if is_concentration_id(i)]
        key = None
        if conc_cases:
            key = "case:" + conc_cases[0]
        elif conc_apps:
            key = "app:" + conc_apps[0]
        if key:
            if key not in cases:
                cases[key] = new_case(key, item)
            add_step(cases[key], item)
        else:
            unkeyed.append(item)

    def score_match(item: Dict[str, Any], case: Dict[str, Any]) -> int:
        if item.get("step_kind") == "enforcement":
            return 0
        it = set(party_tokens(item.get("parties") or [], ""))
        ct = set(party_tokens(case.get("parties") or [], ""))
        if not it or not ct:
            return 0
        overlap = it & ct
        if not overlap:
            return 0
        step_dates = {s.get("date") for s in case.get("steps") or []}
        same_day = bool(item.get("date") and item.get("date") in step_dates)
        if len(overlap) >= 2:
            return len(overlap) * 10 + (8 if same_day else 0)
        if len(overlap) == 1 and same_day:
            return 18
        return 0

    # Pass 2: attach leftover bullets to existing cases by parties
    still: List[Dict[str, Any]] = []
    for item in unkeyed:
        best_key, best = None, 12  # need at least one solid token + date, or 2 tokens
        for key, case in cases.items():
            sc = score_match(item, case)
            if sc > best:
                best, best_key = sc, key
        if best_key and best >= 18:
            add_step(cases[best_key], item)
        else:
            still.append(item)

    # Pass 3: leftover items become their own cases (party hash or url+index)
    for item in still:
        tokens = party_tokens(item.get("parties") or [], "")
        if len(tokens) >= 2:
            key = "parties:" + hashlib.sha1(
                "|".join(sorted(tokens[:6])).encode("utf-8")
            ).hexdigest()[:12]
        else:
            key = "orphan:" + hashlib.sha1(
                f"{item.get('article_url')}|{item.get('index')}|{item.get('text','')[:80]}".encode(
                    "utf-8"
                )
            ).hexdigest()[:12]
        if key not in cases:
            cases[key] = new_case(key, item)
        add_step(cases[key], item)

    out = list(cases.values())
    for case in out:
        case["steps"].sort(key=lambda s: (s.get("date") or "", s.get("kind") or ""))
        case["step_count"] = len(case["steps"])
        case["urls"] = []
        for s in case["steps"]:
            if s.get("url") and s["url"] not in case["urls"]:
                case["urls"].append(s["url"])
        # display title from first step
        case["title"] = (case["steps"][0].get("text") or "")[:240] if case["steps"] else ""
    out.sort(key=lambda c: (c.get("first_seen") or "", c.get("key") or ""))
    return out


def run(max_pages: int, skip_fetch: bool) -> None:
    date_to = datetime.date.today().isoformat()
    print(f"AMCU 2026 backfill {DATE_FROM} → {date_to}")

    if skip_fetch:
        cached = load_json(ARTICLES_JSON)
        if not cached:
            raise SystemExit(f"No cache at {ARTICLES_JSON}; run without --skip-fetch")
        parsed = cached.get("articles") or []
        print(f"Loaded {len(parsed)} cached articles")
    else:
        client = AmcuClient()
        print("Bootstrapping session + CSRF ...")
        client.bootstrap()
        listing = fetch_timeline(client, date_to, max_pages)
        print(f"Listing rows: {len(listing)}")
        relevant = [
            a for a in listing if article_is_relevant(a["title"], a["url"], [t["name"] for t in a["tags"]])
        ]
        print(f"M&A-relevant articles: {len(relevant)}")

        cached = load_json(ARTICLES_JSON) or {}
        by_url = {
            a.get("url"): a
            for a in (cached.get("articles") or [])
            if a.get("url") and not a.get("error")
        }

        parsed = []
        for i, meta in enumerate(relevant, 1):
            cached_art = by_url.get(meta["url"])
            cached_text = (cached_art or {}).get("full_text") or ""
            truncated = len(cached_text) >= 8000
            if (
                cached_art
                and cached_art.get("items") is not None
                and "full_text" in cached_art
                and not truncated
            ):
                print(f"  [{i}/{len(relevant)}] (cache) {meta['title'][:70]}", flush=True)
                parsed.append(cached_art)
                continue
            print(f"  [{i}/{len(relevant)}] {meta['title'][:80]}", flush=True)
            try:
                html = client.get(meta["url"])
                parsed.append(parse_article_html(html, meta))
            except Exception as exc:
                print(f"    FAIL {exc}")
                parsed.append(
                    {
                        **meta,
                        "kind": classify_article(
                            meta["title"], meta["url"], [t["name"] for t in meta["tags"]]
                        ),
                        "sitting_date": sitting_date_from_url(meta["url"])
                        or (meta.get("date_from") or "")[:10],
                        "full_text": "",
                        "items": [],
                        "error": str(exc),
                    }
                )
            client._sleep()
            if i % 25 == 0:
                save_json(
                    ARTICLES_JSON,
                    {
                        "scraped_at": utc_now_iso(),
                        "date_from": DATE_FROM,
                        "date_to": date_to,
                        "listing_count": len(listing),
                        "relevant_count": len(relevant),
                        "articles": parsed,
                    },
                )

        save_json(
            ARTICLES_JSON,
            {
                "scraped_at": utc_now_iso(),
                "date_from": DATE_FROM,
                "date_to": date_to,
                "listing_count": len(listing),
                "relevant_count": len(relevant),
                "articles": parsed,
            },
        )
        print(f"Wrote {ARTICLES_JSON}")

    parsed = [rebuild_items_from_full_text(a) for a in parsed]
    npa_index = []
    for a in parsed:
        if a.get("kind") != "npa":
            continue
        npa_index.append(
            {
                "url": a.get("url"),
                "title": a.get("title"),
                "date": (a.get("sitting_date") or (a.get("date_from") or "")[:10]),
                "tags": a.get("tags") or a.get("listing_tags") or [],
                "has_body": bool((a.get("full_text") or "").strip()),
            }
        )

    cases = group_cases(parsed)
    multi = sum(1 for c in cases if c["step_count"] > 1)
    news_kinds = {"agenda", "decisions", "case_opening", "commitments", "press", "tagged_other"}
    news_articles = [a for a in parsed if a.get("kind") in news_kinds]
    items_total = sum(len(a.get("items") or []) for a in news_articles)
    summary = {
        "scraped_at": utc_now_iso(),
        "date_from": DATE_FROM,
        "date_to": date_to,
        "articles": len(news_articles),
        "npa_documents": len(npa_index),
        "merger_items": items_total,
        "cases": len(cases),
        "cases_with_updates": multi,
        "by_article_kind": {},
        "by_step_kind": {},
    }
    for a in parsed:
        k = a.get("kind") or "unknown"
        summary["by_article_kind"][k] = summary["by_article_kind"].get(k, 0) + 1
    for c in cases:
        for s in c["steps"]:
            k = s.get("kind") or "unknown"
            summary["by_step_kind"][k] = summary["by_step_kind"].get(k, 0) + 1

    save_json(
        CASES_JSON,
        {"summary": summary, "cases": cases, "npa_index": npa_index},
    )
    print(f"Wrote {CASES_JSON}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--max-pages", type=int, default=999, help="Cap timeline pages (10 articles each)")
    p.add_argument("--skip-fetch", action="store_true", help="Regroup from ukraine_amcu_2026_articles.json")
    args = p.parse_args()
    run(max_pages=args.max_pages, skip_fetch=args.skip_fetch)


if __name__ == "__main__":
    main()
