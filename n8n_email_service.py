"""
Shared n8n email webhook routing.

Matched deal emails ([FRMD] in subject) go to N8N_WEBHOOK_SEND_TO_ALL.
All other emails use the caller's default webhook (typically
N8N_WEBHOOK_INTERNAL_WITH_JOSH).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv(".env")

logger = logging.getLogger("n8n_email_service")

BASE_URL = os.getenv("BASE_URL", "")
N8N_WEBHOOK_DEFAULT = os.getenv(
    "N8N_WEBHOOK_INTERNAL_WITH_JOSH",
    "N8N_WEBHOOK_ONLY_ME",
)
N8N_WEBHOOK_SEND_TO_ALL = os.getenv("N8N_WEBHOOK_SEND_TO_ALL", "")


def resolve_webhook_url(subject: str, *, default_url: Optional[str] = None) -> str:
    """Return send-to-all webhook for [FRMD] subjects, else the default."""
    if "[FRMD]" in (subject or "") and N8N_WEBHOOK_SEND_TO_ALL:
        return N8N_WEBHOOK_SEND_TO_ALL
    return default_url or N8N_WEBHOOK_DEFAULT


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
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        logger.info("Email sent via %s: %s", url, subj)
        return True
    except Exception as e:
        logger.warning("Webhook failed (%s): %s", url, e)
        return False
