"""
Docket Email Service
====================
Shared service for sending docket filing notification emails.

Uses the same org-aware routing as foreign regulatory scrapers:
  - report_type "docket" must be in org's enabled_report_types
  - recipients must have "docket" in their report_types
  - when deal_id is provided, only recipients with that deal_id
    in their allowed_deal_ids receive the email

Reusable across all jurisdiction scrapers in docket_engine/.

Usage:
    from docket_engine.docket_email_service import send_docket_email

    send_docket_email(
        subject="DigitalBridge Group, Inc. : NJ - TM26030047: ...",
        email_html="<!doctype html>...",
        doc_id="1425557",
        docket_number="TM26030047",
        docket_type="nj-bpu",
        deal_id="697731195fa114e42889363a",
    )
"""

from __future__ import annotations
from n8n_email_service import send_report_email

import logging
import os
import sys
from typing import Optional

# Ensure project root is on sys.path
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


logger = logging.getLogger(__name__)  # → docket_engine.docket_email_service

DOCKET_REPORT_TYPE = "dockets"


def send_docket_email(
    subject: str,
    email_html: str,
    doc_id: str,
    docket_number: str,
    docket_type: str,
    deal_id: Optional[str] = None,
    *,
    webhook_url: Optional[str] = None,
) -> dict:
    """
    Send a docket filing notification email via org-aware routing.

    Filters recipients by:
      1. Org has "docket" in organization_notification_settings.enabled_report_types
      2. Recipient has "docket" in organization_email_recipients.report_types
      3. If deal_id provided: recipient has deal_id in allowed_deal_ids

    Args:
        subject:       Email subject line.
        email_html:    Full <!doctype html> email body from email_renderer.
        doc_id:        Document ID (for payload context and logging).
        docket_number: Docket number (e.g. "TM26030047").
        docket_type:   Docket type slug (e.g. "nj-bpu").
        deal_id:       Deal ID for recipient filtering (from analyze_docket_entry result).
        webhook_url:   Override NEW_N8N_EMAIL_WEBHOOK_URL (useful for testing).

    Returns:
        Summary dict from send_report_email with orgs_processed, orgs_sent, results.
    """
    payload = {
        "subject": subject,
        "html": email_html,
        "doc_id": doc_id,
        "docket_number": docket_number,
        "docket_type": docket_type,
    }

    logger.info(
        f"  Sending docket email — doc_id={doc_id} "
        f"docket_number={docket_number} deal_id={deal_id or '(none)'}"
    )

    try:
        summary = send_report_email(
            report_type=DOCKET_REPORT_TYPE,
            payload=payload,
            deal_id=deal_id,
            webhook_url=webhook_url,
        )
        logger.info(
            f"  Docket email done — orgs_sent={summary.get('orgs_sent', 0)} / "
            f"orgs_processed={summary.get('orgs_processed', 0)}"
        )
        return summary
    except Exception as e:
        logger.warning(f"  Docket email failed for doc_id={doc_id}: {e}")
        return {"success": False, "error": str(e)}
