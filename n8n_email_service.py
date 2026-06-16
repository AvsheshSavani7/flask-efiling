"""
Shared n8n email webhook routing.

Matched deal emails ([FRMD] in subject) go to N8N_WEBHOOK_SEND_TO_ALL.
All other emails use the caller's default webhook (typically
N8N_WEBHOOK_INTERNAL_WITH_JOSH).

Organisation-aware dispatch
---------------------------
send_report_email(report_type, payload, org_id=None)
  1. Find active organization(s).
  2. Check organization_notification_settings — keep only those where
     enabled_report_types contains the given report_type.
  3. Find active organization_email_recipients for each qualifying org
     — keep only those whose report_types list contains the report_type.
  4. POST one webhook request per org (with its recipients list)
     to NEW_N8N_EMAIL_WEBHOOK_URL.

Supported report_types (foreign regulatory):
  "foreign_regulatory_matched_deal"  — case matched to a deal [FRMD]
  "foreign_regulatory_us_deal"       — US-related case, no deal match [FRUD]

MongoDB collections (Deal_DB)
------------------------------
- organizations                    : _id (ObjectId), status ("active"|...), name
- organization_notification_settings: organization_id (str), enabled_report_types (list)
- organization_email_recipients    : organization_id (str), email, name,
                                     is_active (bool), report_types (list)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests
from bson import ObjectId
from dotenv import load_dotenv

from mongodb_connection import get_database

load_dotenv(".env")

logger = logging.getLogger("n8n_email_service")

BASE_URL = os.getenv("BASE_URL", "")
N8N_WEBHOOK_DEFAULT = os.getenv(
    "N8N_WEBHOOK_INTERNAL_WITH_JOSH",
    "N8N_WEBHOOK_ONLY_ME",
)
N8N_WEBHOOK_SEND_TO_ALL = os.getenv("N8N_WEBHOOK_SEND_TO_ALL", "")
NEW_N8N_EMAIL_WEBHOOK_URL = os.getenv("NEW_N8N_EMAIL_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# Existing broadcast helpers (unchanged)
# ---------------------------------------------------------------------------

def resolve_webhook_url(subject: str, *, default_url: Optional[str] = None) -> str:
    """Return send-to-all webhook for [FRMD] subjects, else the default."""
    if "[FRMD]" in (subject or "") and N8N_WEBHOOK_SEND_TO_ALL:
        return N8N_WEBHOOK_SEND_TO_ALL
    return default_url or N8N_WEBHOOK_DEFAULT


_TESTING_ORG_ID = "6a031d87e4f1d72367bd2f92"


def post_email_payload(
    payload: Dict[str, Any],
    *,
    subject: Optional[str] = None,
    default_url: Optional[str] = None,
    webhook_url: Optional[str] = None,
    timeout: int = 30,
) -> bool:
    """POST an email payload to the appropriate n8n webhook."""
    subj = subject or payload.get("subject", "")
    url = webhook_url or resolve_webhook_url(subj, default_url=default_url)

    # Existing broadcast send (comment out once org-aware send is fully live)
    result = False
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        logger.info("Email sent via %s: %s", url, subj)
        result = True
    except Exception as e:
        logger.warning("Webhook failed (%s): %s", url, e)

    # Org-aware send — triggered by subject tag
    if "[FRMD]" in subj:
        report_type = "foreign_regulatory_matched_deal"
    elif "[FRUD]" in subj:
        report_type = "foreign_regulatory_us_deal"
    else:
        report_type = None

    if report_type:
        send_report_email(report_type, payload, org_id=_TESTING_ORG_ID)

    return result


# ---------------------------------------------------------------------------
# Organisation-aware helpers (private)
# ---------------------------------------------------------------------------

def _get_active_orgs(org_id: Optional[str] = None) -> list:
    """Return active organization documents, optionally scoped to one org."""
    db = get_database()
    if db is None:
        logger.error("MongoDB not connected — cannot fetch organizations.")
        return []
    query: Dict[str, Any] = {"status": "active"}
    if org_id:
        try:
            query["_id"] = ObjectId(org_id)
        except Exception:
            logger.error("Invalid org_id format: %s", org_id)
            return []
    return list(db["organizations"].find(query))


def _is_report_type_enabled(organization_id: str, report_type: str) -> bool:
    """Return True when the org's notification settings include report_type."""
    db = get_database()
    if db is None:
        return False
    settings = db["organization_notification_settings"].find_one(
        {"organization_id": organization_id}
    )
    if not settings:
        return False
    return report_type in settings.get("enabled_report_types", [])


def _get_recipients(organization_id: str, report_type: str) -> list:
    """Return active recipients for this org subscribed to report_type."""
    db = get_database()
    if db is None:
        return []
    return list(
        db["organization_email_recipients"].find(
            {
                "organization_id": organization_id,
                "is_active": True,
                "report_types": report_type,
            }
        )
    )


def _post_to_webhook(url: str, payload: dict, timeout: int = 30) -> bool:
    """POST payload to a webhook URL. Returns True on success."""
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        logger.info(
            "Webhook POST succeeded [%s] — status %s", url, resp.status_code)
        return True
    except requests.exceptions.RequestException as exc:
        logger.error("Webhook POST failed [%s]: %s", url, exc)
        if hasattr(exc, "response") and exc.response is not None:
            logger.error("Response body: %s", exc.response.text[:300])
        return False


# ---------------------------------------------------------------------------
# Organisation-aware public API
# ---------------------------------------------------------------------------

def send_report_email(
    report_type: str,
    payload: dict,
    org_id: Optional[str] = None,
    webhook_url: Optional[str] = None,
) -> dict:
    """
    Send a report email to all eligible org recipients for the given report_type.

    Parameters
    ----------
    report_type : str
        One of the supported types, e.g. "foreign_regulatory_matched_deal"
        or "foreign_regulatory_us_deal".
    payload : dict
        Must include at minimum:
            "subject" (str) — email subject line
            "html"    (str) — HTML body
        Any additional keys are forwarded to the webhook as-is.
    org_id : str, optional
        Target a single organisation. If omitted, all active orgs are processed.
    webhook_url : str, optional
        Override NEW_N8N_EMAIL_WEBHOOK_URL (useful for testing).

    Returns
    -------
    dict
        {
          "report_type": str,
          "orgs_processed": int,
          "orgs_sent": int,
          "orgs_skipped_no_setting": int,
          "orgs_skipped_no_recipients": int,
          "results": [
              {"org_id": str, "org_name": str, "recipients": [...], "sent": bool},
              ...
          ]
        }
    """
    hook = webhook_url or NEW_N8N_EMAIL_WEBHOOK_URL
    if not hook:
        raise EnvironmentError(
            "NEW_N8N_EMAIL_WEBHOOK_URL is not set. Add it to your .env file."
        )

    summary: Dict[str, Any] = {
        "report_type": report_type,
        "orgs_processed": 0,
        "orgs_sent": 0,
        "orgs_skipped_no_setting": 0,
        "orgs_skipped_no_recipients": 0,
        "results": [],
    }

    active_orgs = _get_active_orgs(org_id)
    logger.info(
        "send_report_email | report_type=%s | active orgs=%d",
        report_type,
        len(active_orgs),
    )

    for org in active_orgs:
        org_id_str = str(org["_id"])
        org_name = org.get("name", org_id_str)
        summary["orgs_processed"] += 1

        if not _is_report_type_enabled(org_id_str, report_type):
            logger.info(
                "Org '%s' — '%s' not in enabled_report_types, skipping.",
                org_name, report_type,
            )
            summary["orgs_skipped_no_setting"] += 1
            summary["results"].append({
                "org_id": org_id_str,
                "org_name": org_name,
                "skipped_reason": "report_type not in enabled_report_types",
                "sent": False,
            })
            continue

        recipients = _get_recipients(org_id_str, report_type)
        if not recipients:
            logger.info(
                "Org '%s' — no active recipients for '%s', skipping.",
                org_name, report_type,
            )
            summary["orgs_skipped_no_recipients"] += 1
            summary["results"].append({
                "org_id": org_id_str,
                "org_name": org_name,
                "skipped_reason": "no active recipients for this report_type",
                "sent": False,
            })
            continue

        recipient_list: List[str] = [r["email"] for r in recipients]
        webhook_payload = {
            **payload,
            "report_type": report_type,
            "org_id": org_id_str,
            "org_name": org_name,
            "recipients": recipient_list,
        }

        logger.info(
            "Sending '%s' to org '%s' (%d recipients).",
            report_type, org_name, len(recipient_list),
        )
        sent = _post_to_webhook(hook, webhook_payload)

        summary["results"].append({
            "org_id": org_id_str,
            "org_name": org_name,
            "recipients": recipient_list,
            "sent": sent,
        })
        if sent:
            summary["orgs_sent"] += 1

    logger.info(
        "send_report_email done | report_type=%s | orgs_sent=%d / orgs_processed=%d",
        report_type, summary["orgs_sent"], summary["orgs_processed"],
    )
    return summary


def send_direct_email(
    recipients: List[str],
    payload: dict,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    Send an email directly to a fixed recipient list — no org lookup,
    no report_type check, no MongoDB queries.

    Use for internal alerts, pipeline error notifications, admin-only sends, etc.

    Parameters
    ----------
    recipients : list[str]
        Flat list of email addresses, e.g. ["admin@example.com"].
    payload : dict
        Must include at minimum:
            "subject" (str) — email subject line
            "html"    (str) — HTML body
        Any additional keys are forwarded to the webhook as-is.
    webhook_url : str, optional
        Override NEW_N8N_EMAIL_WEBHOOK_URL.

    Returns
    -------
    bool
        True if the webhook call succeeded, False otherwise.
    """
    hook = webhook_url or NEW_N8N_EMAIL_WEBHOOK_URL
    if not hook:
        raise EnvironmentError(
            "NEW_N8N_EMAIL_WEBHOOK_URL is not set. Add it to your .env file."
        )

    if not recipients:
        logger.warning(
            "send_direct_email called with empty recipients list — skipping.")
        return False

    webhook_payload = {**payload, "recipients": recipients}
    logger.info(
        "send_direct_email | recipients=%d | subject=%s",
        len(recipients), payload.get("subject", ""),
    )
    return _post_to_webhook(hook, webhook_payload)
