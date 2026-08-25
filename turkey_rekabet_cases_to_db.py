"""
Turkey Rekabet Kurumu — New Decisions → turkey_cases collection.

Fetches all pages from https://www.rekabet.gov.tr/tr/SonKurulKararlari,
parses each M&A decision, checks decision_number against turkey_cases,
translates new records from Turkish to English, runs LLM + regex deal matching,
and sends email alerts.

Decision type filter: only "Birleşme ve Devralma" (M&A) records are processed.

Email routing (dev mode): sends to avshesh.savani@teqnodux.com via N8N_WEBHOOK_ONLY_ME.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from html import escape as escape_html
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from mongodb_connection import (
    get_database,
    get_deal_by_id,
    init_mongodb_connection,
)
from deal_match_llm import llm_match_deal_id, fetch_open_deals
from deal_match_regex import regex_match_turkey_deal
from llm_verification_service import verify_usa_relation
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload
from scraper_error_utils import collect_error, send_error_summary
from log_utils import ensure_script_logger, refresh_script_log

load_dotenv(".env")

SCRIPT_NAME = "turkey_rekabet_cases"
BASE_URL = "https://www.rekabet.gov.tr"
LISTING_URL = f"{BASE_URL}/tr/SonKurulKararlari"
MA_DECISION_TYPE = "Birleşme ve Devralma"
MAX_PAGES = 10
TRANSLATE_MODEL = "gpt-5.2"
TRANSLATE_MAX_CHARS = 12000

logger, get_log_file = ensure_script_logger(SCRIPT_NAME)
_openai_client: Optional[OpenAI] = None

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Referer": BASE_URL,
}


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

def get_turkey_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["turkey_cases"]


def detail_url_exists(collection, detail_url: str) -> bool:
    if collection is None or not detail_url:
        return False
    return collection.find_one({"detail_url": detail_url.strip()}) is not None


def insert_turkey_case(collection, doc: Dict[str, Any]) -> bool:
    try:
        now = _utc_now_iso()
        doc.setdefault("created_at", now)
        doc["updated_at"] = now
        collection.insert_one(doc)
        return True
    except Exception as e:
        logger.warning(f"Insert failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_date_iso(date_str: str) -> str:
    """Convert Turkish date 'D.M.YYYY' → 'YYYY-MM-DD'."""
    try:
        parts = date_str.strip().split(".")
        if len(parts) == 3:
            return f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}"
    except Exception:
        pass
    return date_str.strip()


def _extract_after_colon(cell_text: str) -> str:
    """Extract the value part after ':' in a cell like 'Karar Sayısı : 26-21/632-253'."""
    idx = cell_text.find(":")
    if idx >= 0:
        return cell_text[idx + 1:].strip()
    return cell_text.strip()


class TurkeyTranslateError(Exception):
    """Per-record translation failure — skip insert so the case stays new."""

    def __init__(
        self,
        message: str,
        *,
        field: str,
        decision_number: str,
        source_text: str = "",
        error_detail: str = "",
    ):
        super().__init__(message)
        self.field = field
        self.decision_number = decision_number
        self.source_text = source_text
        self.error_detail = error_detail


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def translate_tr_to_en(text: str) -> str:
    """Translate Turkish Rekabet text to English via OpenAI."""
    if not text or not isinstance(text, str) or not text.strip():
        return ""
    text = text.strip()
    to_translate = text
    if len(to_translate) > TRANSLATE_MAX_CHARS:
        logger.warning(
            "Translation input truncated from %d to %d chars",
            len(to_translate),
            TRANSLATE_MAX_CHARS,
        )
        to_translate = to_translate[:TRANSLATE_MAX_CHARS]

    response = _get_openai_client().chat.completions.create(
        model=TRANSLATE_MODEL,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional Turkish-to-English translator for Turkey "
                    "Rekabet Kurumu merger-control decisions.\n"
                    "Rules:\n"
                    "1. Return ONLY the translated English text.\n"
                    "2. Use well-known official English company names where possible.\n"
                    "3. Do NOT explain or add alternatives.\n"
                    "4. Preserve legal and regulatory meaning "
                    "(decision type, merger description).\n"
                    "5. Keep decision numbers, dates, and legal citations intact."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Translate this Turkey Rekabet Turkish decision text to English:\n"
                    f"{to_translate}"
                ),
            },
        ],
    )
    translated = (response.choices[0].message.content or "").strip()
    if translated:
        return translated
    raise RuntimeError("OpenAI translation returned empty text")


def translate_tr_to_en_required(
    text: str,
    *,
    field: str,
    decision_number: str,
) -> str:
    """Translate Turkish text via GPT. On failure, raise so the case is skipped."""
    source = (text or "").strip()
    if not source:
        logger.info(
            "  [%s] TRANSLATE %s IN: (empty) — skipping GPT",
            decision_number,
            field,
        )
        return ""
    logger.info(
        "  [%s] TRANSLATE %s IN (%d chars): %s",
        decision_number,
        field,
        len(source),
        source,
    )
    try:
        result = translate_tr_to_en(source)
    except TurkeyTranslateError:
        raise
    except RuntimeError as exc:
        if "OPENAI_API_KEY" in str(exc):
            raise
        raise TurkeyTranslateError(
            f"Translation failed for field '{field}' (decision_number={decision_number}): {exc}",
            field=field,
            decision_number=decision_number,
            source_text=source[:300],
            error_detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.warning(
            "Translation failed for field '%s' (decision_number=%s): %s",
            field,
            decision_number,
            exc,
        )
        raise TurkeyTranslateError(
            f"Translation failed for field '{field}' (decision_number={decision_number}): {exc}",
            field=field,
            decision_number=decision_number,
            source_text=source[:300],
            error_detail=str(exc),
        ) from exc
    if not result:
        raise TurkeyTranslateError(
            f"Translation failed for field '{field}' (decision_number={decision_number})",
            field=field,
            decision_number=decision_number,
            source_text=source[:300],
        )
    logger.info(
        "  [%s] TRANSLATE %s OUT (%d chars): %s",
        decision_number,
        field,
        len(result),
        result,
    )
    return result


# ---------------------------------------------------------------------------
# Fetch & parse
# ---------------------------------------------------------------------------

def _listing_page_url(page: int) -> str:
    if page <= 1:
        return LISTING_URL
    return f"{LISTING_URL}/{page}"


def fetch_page_html(url: str, max_retries: int = 3) -> Optional[str]:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.text) > 500:
                logger.info(f"  Fetched {url} ({len(resp.text):,} chars)")
                return resp.text
            logger.warning(
                f"  Attempt {attempt}: HTTP {resp.status_code}, {len(resp.text):,} chars"
            )
        except Exception as e:
            logger.warning(f"  Attempt {attempt} error: {e}")
        if attempt < max_retries:
            time.sleep(3)
    return None


def parse_records_from_html(html: str) -> List[Dict[str, str]]:
    """
    Parse listing HTML into a list of raw record dicts.

    Structure per record:
      <a href="/tr/SonKurulKarari/{uuid}">
        <table>
          <tr> Karar Sayısı | Karar Tarihi | Karar Türü </tr>
          <tr> .tablotitle (title) </tr>
          <tr> description text </tr>
        </table>
      </a>
    """
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for anchor in soup.select('a[href^="/tr/SonKurulKarari/"]'):
        try:
            href = anchor.get("href", "")
            detail_url = BASE_URL + href
            record_uuid = href.rstrip("/").split("/")[-1]

            table = anchor.find("table")
            if not table:
                continue
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue

            # Row 0: Karar Sayısı | Karar Tarihi | Karar Türü
            cells0 = rows[0].find_all("td")
            if len(cells0) < 3:
                continue
            decision_number = _extract_after_colon(cells0[0].get_text())
            decision_date = _extract_after_colon(cells0[1].get_text())
            decision_type = _extract_after_colon(cells0[2].get_text())

            if not decision_number:
                continue

            # Row 1: title (.tablotitle)
            cells1 = rows[1].find_all("td")
            title_tr = cells1[0].get_text(strip=True) if cells1 else ""

            # Row 2: description
            cells2 = rows[2].find_all("td")
            description_tr = cells2[0].get_text(strip=True) if cells2 else ""

            records.append({
                "decision_number": decision_number.strip(),
                "decision_date": decision_date.strip(),
                "decision_date_iso": _parse_date_iso(decision_date.strip()),
                "decision_type": decision_type.strip(),
                "title_tr": title_tr.strip(),
                "description_tr": description_tr.strip(),
                "detail_url": detail_url,
                "record_uuid": record_uuid,
                "source": "turkey_rekabet",
            })
        except Exception as e:
            logger.warning(f"  Error parsing record anchor: {e}")
            continue

    return records


# ---------------------------------------------------------------------------
# Email builders
# ---------------------------------------------------------------------------

def _safe(val: Any) -> str:
    if val is None or (isinstance(val, str) and not val.strip()):
        return "N/A"
    return escape_html(str(val).strip())


def _build_case_table_html(record: Dict[str, Any]) -> str:
    rows = [
        ("Decision Number", record.get("decision_number")),
        ("Decision Date", record.get("decision_date")),
        ("Decision Type (TR)", record.get("decision_type")),
        ("Decision Type (EN)", record.get("decision_type_en")),
        ("Title (Turkish)", record.get("title_tr")),
        ("Title (English)", record.get("title_en")),
        ("Description (Turkish)", record.get("description_tr")),
        ("Description (English)", record.get("description_en")),
    ]
    html = ""
    for i, (label, value) in enumerate(rows):
        bg = ' style="background-color:#f9f9f9;"' if i % 2 == 1 else ""
        html += (
            f"<tr{bg}>"
            f'<td style="padding:8px;font-weight:bold;width:220px;color:#555;">{label}:</td>'
            f'<td style="padding:8px;color:#333;">{_safe(value)}</td>'
            "</tr>\n"
        )
    return html


def build_matched_email(
    record: Dict[str, Any],
    deal_match: Dict[str, Any],
) -> Tuple[str, str]:
    subject = build_subject("turkey_rekabet", "new", deal_match)
    deal_id = _safe(deal_match.get("deal_id"))
    target = _safe(deal_match.get("target") or deal_match.get("target_name"))
    acquirer = _safe(deal_match.get("acquirer")
                     or deal_match.get("acquire_name"))
    case_table = _build_case_table_html(record)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #2563eb;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Turkey Rekabet Kurumu</p>
  <div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
    <div style="font-weight:800;color:#1e40af;margin-bottom:4px;">Matched Deal</div>
    <div style="font-size:14px;color:#1e3a8a;">
      <b>Acquirer:</b> {acquirer} &nbsp;|&nbsp;
      <b>Target:</b> {target} &nbsp;|&nbsp;
      <b>Deal ID:</b> {deal_id}
    </div>
  </div>
  <h3 style="color:#333;">Case Details</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_table}</table>
  <p>
    <a href="{escape_html(record.get('detail_url', ''))}" style="color:#2563eb;" target="_blank">
      View decision on Rekabet Kurumu →
    </a>
  </p>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    Automated alert from Turkey Rekabet Kurumu scraper.
  </div>
</div>
</body></html>"""
    return subject, html


def build_usa_email(record: Dict[str, Any]) -> Tuple[str, str]:
    subject = build_subject("turkey_rekabet", "new")
    case_table = _build_case_table_html(record)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;text-align:center;margin-top:0;padding-bottom:20px;border-bottom:3px solid #f59e0b;">{escape_html(subject)}</h2>
  <p style="color:#666;text-align:center;">Source: Turkey Rekabet Kurumu</p>
  <div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
    <div style="font-weight:800;color:#92400e;">USA-Related (Unmatched)</div>
    <div style="font-size:14px;color:#78350f;margin-top:4px;">
      This decision appears to involve USA-related companies.
    </div>
  </div>
  <h3 style="color:#333;">Case Details</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{case_table}</table>
  <p>
    <a href="{escape_html(record.get('detail_url', ''))}" style="color:#2563eb;" target="_blank">
      View decision on Rekabet Kurumu →
    </a>
  </p>
  <div style="margin-top:30px;padding-top:20px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    Automated alert from Turkey Rekabet Kurumu scraper.
  </div>
</div>
</body></html>"""
    return subject, html


def _send_email(subject: str, html: str, extras: Dict[str, Any]) -> bool:
    payload = {"subject": subject, "html": html, **extras}
    return post_email_payload(payload, subject=subject)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    LOG_FILE = refresh_script_log(logger, get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    stats: Dict[str, int] = {
        "pages_fetched": 0,
        "total_seen": 0,
        "skipped": 0,
        "inserted": 0,
        "llm_matched": 0,
        "regex_matched": 0,
        "usa_related": 0,
        "emails_sent": 0,
    }

    logger.info("=" * 60)
    logger.info("TURKEY REKABET KURUMU — NEW DECISIONS SCRAPER")
    logger.info("=" * 60)

    try:
        success, message = init_mongodb_connection(".env")
        if not success:
            collect_error(
                error_items, f"MongoDB init failed: {message}", step="init_mongodb")
            return

        collection = get_turkey_cases_collection()
        if collection is None:
            collect_error(
                error_items, "turkey_cases collection unavailable", step="get_collection")
            return

        deals = fetch_open_deals()
        deal_by_id = {str(d.get("deal_id", ""))                      : d for d in deals if d.get("deal_id")}
        logger.info(f"Loaded {len(deals)} deals for matching")

        for page in range(1, MAX_PAGES + 1):
            url = _listing_page_url(page)
            logger.info(f"[Page {page}/{MAX_PAGES}] {url}")

            html = fetch_page_html(url)
            if not html:
                collect_error(
                    error_items,
                    f"Failed to fetch page {page}",
                    step="fetch_page",
                    context={"url": url},
                )
                break

            records = parse_records_from_html(html)
            logger.info(f"[Page {page}] Parsed {len(records)} records")
            if not records:
                logger.info(
                    f"[Page {page}] No records found — stopping pagination")
                break

            stats["pages_fetched"] += 1
            all_already_in_db = True

            for record in records:
                stats["total_seen"] += 1
                dn = record["decision_number"]
                dt = record["decision_type"]

                # Only process M&A decisions
                if MA_DECISION_TYPE not in dt:
                    logger.info(f"  {dn} — skipping (type: {dt})")
                    continue

                if detail_url_exists(collection, record["detail_url"]):
                    logger.info(f"  {dn} — already in DB, skip")
                    stats["skipped"] += 1
                    continue

                all_already_in_db = False
                logger.info("=" * 50)
                logger.info("  %s — NEW RECORD", dn)
                logger.info("    decision_date     : %s (%s)",
                            record.get("decision_date"),
                            record.get("decision_date_iso"))
                logger.info("    decision_type     : %s",
                            record.get("decision_type"))
                logger.info("    detail_url        : %s",
                            record.get("detail_url"))
                logger.info("    record_uuid       : %s",
                            record.get("record_uuid"))
                logger.info("    title_tr          : %s",
                            record.get("title_tr"))
                logger.info("    description_tr    : %s",
                            record.get("description_tr"))

                # --- Translate ---
                try:
                    record["title_en"] = translate_tr_to_en_required(
                        record["title_tr"],
                        field="title_tr",
                        decision_number=dn,
                    )
                    record["description_en"] = translate_tr_to_en_required(
                        record["description_tr"],
                        field="description_tr",
                        decision_number=dn,
                    )
                    record["decision_type_en"] = translate_tr_to_en_required(
                        record["decision_type"],
                        field="decision_type",
                        decision_number=dn,
                    )
                except TurkeyTranslateError as e:
                    logger.error(
                        "  Translation failed for %s; skipping insert: %s",
                        dn,
                        e,
                    )
                    collect_error(
                        error_items,
                        str(e),
                        step="translate",
                        case_number=dn,
                        context={
                            "decision_number": dn,
                            "field": e.field,
                            "source_text_snippet": e.source_text,
                            "error_detail": e.error_detail,
                        },
                    )
                    continue

                logger.info("  [%s] TRANSLATE DONE", dn)
                logger.info("    title_en          : %s", record.get("title_en"))
                logger.info("    description_en    : %s",
                            record.get("description_en"))
                logger.info("    decision_type_en  : %s",
                            record.get("decision_type_en"))

                # --- LLM deal match ---
                deal_id: Optional[str] = None
                matched_by_regex = False
                logger.info("  [%s] DEAL MATCH IN title_en=%s",
                            dn, (record.get("title_en") or "")[:200])
                try:
                    deal_id = llm_match_deal_id(
                        regulator_name="Turkey Rekabet Kurumu",
                        case_sections={
                            "DECISION TITLE (English)": record["title_en"],
                            "DECISION DESCRIPTION (English)": record["description_en"],
                            "DECISION TITLE (Turkish)": record["title_tr"],
                        },
                        source_label="the decision title and description",
                        deals=deals,
                    )
                except Exception as e:
                    logger.exception(f"  LLM match error for {dn}: {e}")
                    collect_error(
                        error_items, str(e), step="llm_match",
                        context={"decision_number": dn},
                    )

                if deal_id:
                    stats["llm_matched"] += 1
                    logger.info("  [%s] DEAL MATCH OUT llm deal_id=%s", dn, deal_id)
                else:
                    # --- Regex fallback ---
                    combined_en = f"{record['title_en']} {record['description_en']}"
                    logger.info("  [%s] DEAL MATCH llm miss — trying regex", dn)
                    deal_id = regex_match_turkey_deal(combined_en, deals)
                    if deal_id:
                        matched_by_regex = True
                        stats["regex_matched"] += 1
                        logger.info(
                            "  [%s] DEAL MATCH OUT regex deal_id=%s", dn, deal_id)
                    else:
                        logger.info("  [%s] DEAL MATCH OUT none (LLM + regex)", dn)

                if deal_id:
                    record["deal_id"] = deal_id

                # --- Email ---
                try:
                    if deal_id:
                        deal_match = deal_by_id.get(
                            str(deal_id)) or get_deal_by_id(deal_id) or {}
                        subject, html_body = build_matched_email(
                            record, deal_match)
                        if matched_by_regex:
                            subject = subject.replace("[FRMD]", "[FRRMD]")
                        ok = _send_email(subject, html_body, {
                            "deal_id": deal_id,
                            "decision_number": dn,
                            "source": "turkey_rekabet",
                        })
                        if ok:
                            stats["emails_sent"] += 1
                            logger.info(f"  Email sent ({subject[:60]})")
                        else:
                            collect_error(
                                error_items, "Failed to send matched email",
                                step="send_email", context={"decision_number": dn},
                            )
                    else:
                        # --- USA check ---
                        usa_details = {
                            "title_en": record.get("title_en"),
                            "description_en": record.get("description_en"),
                            "title_tr": record.get("title_tr"),
                            "description_tr": record.get("description_tr"),
                            "decision_number": dn,
                        }
                        try:
                            logger.info(
                                "  [%s] USA CHECK IN title_en=%s",
                                dn,
                                (record.get("title_en") or "")[:200],
                            )
                            usa = bool(verify_usa_relation(
                                usa_details, case_type="TURKEY"))
                            logger.info(
                                "  [%s] USA CHECK OUT usa_related=%s", dn, usa)
                        except Exception as e:
                            logger.warning(f"  USA check error: {e}")
                            usa = False

                        if usa:
                            stats["usa_related"] += 1
                            subject, html_body = build_usa_email(record)
                            ok = _send_email(subject, html_body, {
                                "decision_number": dn,
                                "source": "turkey_rekabet",
                            })
                            if ok:
                                stats["emails_sent"] += 1
                                logger.info(
                                    f"  USA email sent ({subject[:60]})")
                            else:
                                collect_error(
                                    error_items, "Failed to send USA email",
                                    step="send_email", context={"decision_number": dn},
                                )
                        else:
                            logger.info(f"  Not USA-related — no email")
                except Exception as e:
                    logger.exception(f"  Email pipeline error for {dn}: {e}")
                    collect_error(
                        error_items, str(e), step="send_email",
                        context={"decision_number": dn},
                    )

                # --- Always insert to DB ---
                try:
                    ok = insert_turkey_case(collection, record)
                    if ok:
                        stats["inserted"] += 1
                        logger.info(
                            "  [%s] INSERTED into turkey_cases deal_id=%s url=%s",
                            dn,
                            record.get("deal_id") or "-",
                            record.get("detail_url"),
                        )
                    else:
                        collect_error(
                            error_items, "DB insert returned False",
                            step="insert", context={"decision_number": dn},
                        )
                except Exception as e:
                    logger.exception(f"  Insert error for {dn}: {e}")
                    collect_error(
                        error_items, str(e), step="insert",
                        context={"decision_number": dn},
                    )

                time.sleep(1)

            if all_already_in_db:
                logger.info(
                    f"[Page {page}] All records already in DB — stopping early")
                break

            time.sleep(2)

    except Exception as e:
        logger.exception(f"Unhandled error in run(): {e}")
        collect_error(error_items, f"Unhandled error: {e}", step="run_main")

    finally:
        send_error_summary(error_items, SCRIPT_NAME)
        elapsed = round(time.time() - run_start, 1)
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Pages fetched           : {stats['pages_fetched']}")
        logger.info(f"  Total records seen      : {stats['total_seen']}")
        logger.info(f"  Skipped (in DB)         : {stats['skipped']}")
        logger.info(f"  Inserted                : {stats['inserted']}")
        logger.info(f"  LLM deal matches        : {stats['llm_matched']}")
        logger.info(f"  Regex deal matches      : {stats['regex_matched']}")
        logger.info(f"  USA-related (unmatched) : {stats['usa_related']}")
        logger.info(f"  Emails sent             : {stats['emails_sent']}")
        logger.info(f"  Errors                  : {len(error_items)}")
        logger.info(f"  Total time              : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    run()
