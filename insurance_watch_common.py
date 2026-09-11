"""
Shared helpers for state-insurance public-notice watches.

Used by Delaware DOI and Massachusetts DOI scripts. Matching items are
stored in the insurance collection and emailed via send_direct_email.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from html import escape as escape_html
from typing import Any, Dict, List, Sequence, Tuple

from mongodb_connection import get_database
from n8n_email_service import send_direct_email
from scraper_error_utils import collect_error

logger = logging.getLogger("insurance_watch_common")

COLLECTION_NAME = "insurance"

WATCH_KEYWORDS = {
    "brighthouse": "Brighthouse",
    "aquarian": "Aquarian",
}

RECIPIENTS = [
    "josh@hyperiontechnologies.ai",
    "aaron.glick@guggenheimsecurities.com",
    "chris.colpitts@guggenheimsecurities.com",
    "kaushal@hyperiontechnologies.ai",
    "avshesh.savani@teqnodux.com",
]
TEST_RECIPIENT = "avshesh.savani@teqnodux.com"

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
}

HTML_ROW_LABELS = frozenset({"Notice", "Schedule", "Source page"})


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def match_keywords(text: str) -> List[str]:
    """Match watch names, ignoring capital vs lowercase."""
    haystack = (text or "").lower()
    matched: List[str] = []
    for key, label in WATCH_KEYWORDS.items():
        if re.search(rf"\b{re.escape(key.lower())}\b", haystack):
            matched.append(label)
    return matched


def get_collection():
    db = get_database()
    if db is None:
        return None
    return db[COLLECTION_NAME]


def ensure_indexes(collection) -> None:
    try:
        collection.create_index("unique_key", unique=True, name="unique_key_unique")
        collection.create_index("source", name="source_idx")
        logger.info("Indexes ensured on %s", COLLECTION_NAME)
    except Exception as e:
        logger.warning("Index creation failed: %s", e)


def item_exists(collection, unique_key: str) -> bool:
    if collection is None or not unique_key:
        return False
    return collection.find_one({"unique_key": unique_key}) is not None


def insert_item(collection, doc: Dict[str, Any]) -> bool:
    try:
        collection.insert_one(doc)
        return True
    except Exception as e:
        logger.warning("Insert failed: %s", e)
        return False


def build_email_table_rows(rows: Sequence[Tuple[str, str]]) -> str:
    table_rows = ""
    for i, (label, value) in enumerate(rows):
        bg = ' style="background-color:#f9f9f9;"' if i % 2 == 1 else ""
        cell = value if label in HTML_ROW_LABELS else escape_html(str(value))
        table_rows += (
            f"<tr{bg}>"
            f'<td style="padding:8px;font-weight:bold;width:160px;color:#555;">'
            f"{escape_html(label)}:</td>"
            f'<td style="padding:8px;color:#333;">{cell}</td>'
            "</tr>\n"
        )
    return table_rows


def build_watch_email_html(
    *,
    subject: str,
    source_blurb: str,
    matched_phrase: str,
    matched_keywords: List[str],
    rows: Sequence[Tuple[str, str]],
    footer: str,
) -> str:
    matched = ", ".join(matched_keywords)
    table_rows = build_email_table_rows(rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{escape_html(subject)}</title></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background-color:#f4f4f4;">
<div style="max-width:900px;margin:20px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
  <h2 style="color:#333;margin-top:0;padding-bottom:16px;border-bottom:3px solid #1d4ed8;">
    {escape_html(subject)}
  </h2>
  <p style="color:#666;margin-top:0;">{escape_html(source_blurb)}</p>
  <div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #1d4ed8;">
    <div style="font-weight:800;color:#1e40af;margin-bottom:4px;">Watch match</div>
    <div style="font-size:14px;color:#1e3a8a;">
      {escape_html(matched_phrase)} <b>{escape_html(matched)}</b>.
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
    {table_rows}
  </table>
  <div style="margin-top:24px;padding-top:16px;border-top:1px solid #e0e0e0;text-align:center;color:#999;font-size:12px;">
    {escape_html(footer)}
  </div>
</div>
</body>
</html>"""


def html_link(url: str, label: str, *, bold: bool = False) -> str:
    if not url:
        return "N/A"
    weight = "font-weight:600;" if bold else ""
    return (
        f'<a href="{escape_html(url)}" target="_blank" '
        f'style="color:#0ea5e9;{weight}">{escape_html(label)}</a>'
    )


def send_watch_email(
    *,
    subject: str,
    html: str,
    extras: Dict[str, Any],
    test_mode: bool,
    error_items: List[Dict[str, Any]],
) -> bool:
    payload = {"subject": subject, "html": html, **extras}
    title = extras.get("title") or extras.get("case") or ""
    item_url = extras.get("item_url") or extras.get("schedule_url") or ""
    try:
        if test_mode:
            webhook_url = os.getenv("N8N_WEBHOOK_ONLY_ME", "")
            if not webhook_url:
                logger.warning("N8N_WEBHOOK_ONLY_ME not set — test email skipped")
                collect_error(
                    error_items,
                    "N8N_WEBHOOK_ONLY_ME not set — test email skipped",
                    step="send_email",
                    context={"title": title, "subject": subject},
                )
                return False
            logger.info("[TEST] Sending to %s via N8N_WEBHOOK_ONLY_ME", TEST_RECIPIENT)
            ok = send_direct_email([TEST_RECIPIENT], payload, webhook_url=webhook_url)
        else:
            logger.info("Sending to %s via send_direct_email", ", ".join(RECIPIENTS))
            ok = send_direct_email(RECIPIENTS, payload)
        if not ok:
            collect_error(
                error_items,
                f"Email send failed: {subject[:120]}",
                step="send_email",
                context={"title": title, "item_url": item_url},
            )
        return ok
    except Exception as e:
        collect_error(
            error_items,
            str(e),
            step="send_email",
            context={"title": title, "item_url": item_url},
        )
        return False
