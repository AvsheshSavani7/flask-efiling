"""
Shared error collection for scraper/monitor scripts.

Each run() keeps a local error_items list. send_error_summary() sends one
email per script_name — safe for parallel workflows (no global state).
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from error_email_service import send_error_email

logger = logging.getLogger("scraper_error_utils")

# First match wins when building record_ref for summary emails.
RECORD_ID_PRIORITY = (
    "case_number",
    "case_id",
    "file_number",
    "process",
    "parties",
    "title",
    "detail_url",
    "url",
    "record_url",
)

SCRAPE_CONTEXT_KEYS = (
    "url",
    "http_status",
    "traceback",
    "attempts",
    "screenshot",
)


def resolve_record_ref(fields: Dict[str, Any]) -> Tuple[str, str]:
    """Return (field_name, value) for the best available record identifier."""
    for key in RECORD_ID_PRIORITY:
        value = fields.get(key)
        if value is not None and str(value).strip():
            return key, str(value)[:200]
    step = fields.get("step")
    if step:
        return "step", str(step)
    return "step", "(run-level)"


def collect_error(
    error_items: List[Dict[str, Any]],
    msg: str,
    *,
    step: str,
    context: Optional[Dict[str, Any]] = None,
    case_number: Optional[str] = None,
) -> None:
    """Append an error to this run's list; summary email is sent in run() finally."""
    logger.error(msg)
    item: Dict[str, Any] = {"error": msg, "step": step}
    if case_number:
        item["case_number"] = case_number
    if context:
        for key, value in context.items():
            if key not in item:
                item[key] = value
    ref_type, ref = resolve_record_ref(item)
    item["record_ref"] = ref
    item["record_ref_type"] = ref_type
    error_items.append(item)


def scrape_error(case_number: str, message: str, **extra: Any) -> Dict[str, Any]:
    """Return a failure dict consumed by run() for error summary collection."""
    result: Dict[str, Any] = {"case_number": case_number, "error": message}
    result.update(extra)
    return result


def scrape_error_context(
    case: Optional[Dict[str, Any]],
    fallback_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build extra context for scrape failures (url, traceback, etc.)."""
    context: Dict[str, Any] = {}
    if case:
        for key in SCRAPE_CONTEXT_KEYS:
            if case.get(key) is not None:
                context[key] = case[key]
    if fallback_url and "url" not in context:
        context["url"] = fallback_url
    return context or None


def send_error_summary(
    error_items: List[Dict[str, Any]],
    script_name: str,
    *,
    max_errors: int = 20,
) -> None:
    """Send one summary email for this script run, only if errors were collected."""
    if not error_items:
        return
    logger.warning(
        f"[{script_name}] {len(error_items)} errors collected — sending summary email"
    )
    send_error_email(
        script_name=script_name,
        error_message=f"{len(error_items)} errors occurred during run",
        context={
            "error_count": len(error_items),
            "errors": error_items[:max_errors],
        },
        traceback_str=None,
    )
