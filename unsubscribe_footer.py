"""
Deal-level unsubscribe footer for org-aware emails.

Email HTML is appended at send time (send_report_email / send_direct_email)
when a deal_id is known.
The dashboard route /unsubscribe?deal_id=... identifies the user after login
(or immediately if already logged in).
"""
import os
import urllib.parse

DEFAULT_UNSUBSCRIBE_BASE_URL = "https://dashboard.arbintel.cloud/unsubscribe"


def _is_usable_deal_id(deal_id) -> bool:
    if deal_id is None:
        return False
    value = str(deal_id).strip()
    return bool(value) and value.upper() not in {"N/A", "NONE", "NULL"}


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def build_unsubscribe_url(deal_id: str) -> str:
    """Return dashboard unsubscribe URL with deal_id query param."""
    base = os.environ.get(
        "DASHBOARD_UNSUBSCRIBE_BASE_URL",
        DEFAULT_UNSUBSCRIBE_BASE_URL,
    ).rstrip("/")
    return f"{base}?deal_id={urllib.parse.quote(str(deal_id).strip())}"


def build_unsubscribe_footer_html(deal_id: str) -> str:
    url = _escape_html(build_unsubscribe_url(deal_id))
    return f"""
    <div style="margin-top:24px; padding-top:16px; border-top:1px solid #e0e0e0; text-align:center; font-size:12px; color:#888;">
      <p style="margin:0;">
        <a href="{url}" style="color:#888; text-decoration:underline;" target="_blank" rel="noopener noreferrer">
          Unsubscribe from this deal
        </a>
      </p>
    </div>
"""


def append_unsubscribe_footer(html: str, deal_id: str) -> str:
    """Insert the unsubscribe footer before </body>, or append if missing."""
    if not html or not _is_usable_deal_id(deal_id):
        return html
    if "Unsubscribe from this deal" in html:
        return html
    footer = build_unsubscribe_footer_html(deal_id)
    close_idx = html.lower().rfind("</body>")
    if close_idx != -1:
        return html[:close_idx] + footer + html[close_idx:]
    return html + footer
