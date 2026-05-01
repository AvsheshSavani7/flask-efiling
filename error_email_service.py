"""
Reusable error email notification service.

Sends error alerts via the ERROR_EMAIL_WEBHOOK_URL webhook whenever a
scraper or monitor encounters an error.  Import and call from any script:

    from error_email_service import send_error_email

    send_error_email(
        script_name="fs_cases_register",
        error_message="MongoDB connection failed",
        context={"case_number": "FS.100189", "step": "db_connect"},
        traceback_str=traceback.format_exc(),
    )
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
import logging
import os
import traceback as tb_module

import requests
from dotenv import load_dotenv

load_dotenv(".env")

IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger("error_email_service")

ERROR_EMAIL_WEBHOOK_URL = os.getenv("ERROR_EMAIL_WEBHOOK_URL", "")


def _ist_now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")


def send_error_email(
    script_name: str,
    error_message: str,
    context: Optional[Dict[str, Any]] = None,
    traceback_str: Optional[str] = None,
) -> bool:
    """Send an error notification email via the webhook.

    Returns True if the email was sent successfully, False otherwise.
    Never raises — errors are logged and swallowed so the caller keeps running.
    """
    if not ERROR_EMAIL_WEBHOOK_URL:
        logger.warning("ERROR_EMAIL_WEBHOOK_URL not set; skipping error email")
        return False

    timestamp = _ist_now()
    ctx = context or {}

    context_rows = ""
    for k, v in ctx.items():
        label = k.replace("_", " ").title()
        context_rows += (
            f'<tr>'
            f'<td style="padding:6px 12px;font-weight:600;color:#374151;border-bottom:1px solid #f3f4f6;">{label}</td>'
            f'<td style="padding:6px 12px;color:#111827;border-bottom:1px solid #f3f4f6;">{v}</td>'
            f'</tr>'
        )

    traceback_block = ""
    if traceback_str:
        traceback_block = (
            '<div style="margin-top:16px;">'
            '<div style="font-size:13px;font-weight:700;color:#6b7280;margin-bottom:6px;">Traceback</div>'
            f'<pre style="background:#1f2937;color:#f9fafb;padding:14px;border-radius:6px;'
            f'font-size:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;">{traceback_str}</pre>'
            '</div>'
        )

    html = f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/></head>
<body style="margin:0;background:#fef2f2;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
<div style="max-width:720px;margin:24px auto;background:#fff;border:1px solid #fca5a5;border-radius:10px;overflow:hidden;box-shadow:0 4px 12px rgba(239,68,68,0.10);">
<div style="background:#dc2626;padding:18px 24px;">
  <div style="font-size:20px;font-weight:800;color:#fff;">Scraper Error Alert</div>
  <div style="font-size:13px;color:#fecaca;margin-top:4px;">{timestamp}</div>
</div>
<div style="padding:20px 24px;">
  <div style="font-size:15px;color:#111827;margin-bottom:12px;">
    <span style="font-weight:700;color:#6b7280;">Script:</span>
    <span style="font-weight:800;color:#dc2626;margin-left:6px;">{script_name}</span>
  </div>
  <div style="background:#fef2f2;border-left:4px solid #ef4444;padding:12px 16px;border-radius:4px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:700;color:#991b1b;">{error_message}</div>
  </div>'''

    if context_rows:
        html += (
            '<div style="margin-bottom:16px;">'
            '<div style="font-size:13px;font-weight:700;color:#6b7280;margin-bottom:6px;">Context</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'{context_rows}'
            '</table></div>'
        )

    html += traceback_block
    html += '</div></div></body></html>'

    subject = f"[ERROR] {script_name} — {error_message[:120]}"

    payload = {
        "subject": subject,
        "html": html,
        "script_name": script_name,
        "error_message": error_message,
        "context": ctx,
        "source": "error_email_service",
    }

    try:
        resp = requests.post(
            ERROR_EMAIL_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"Error email sent for {script_name} (status={resp.status_code})")
        return True
    except Exception as e:
        logger.warning(f"Failed to send error email: {e}")
        return False
