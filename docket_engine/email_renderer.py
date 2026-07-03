"""
Email Renderer — Docket Filing HTML Email Generator
====================================================
Python port of the two n8n JavaScript nodes that build the HTML email for a
docket filing notification.

Node 1 equivalent → render_intake_card()
    Takes the GPT intake note (Filing / Type / Summary / Relevance) and renders
    the header card fragment (partial HTML, not a full document).

Node 2 equivalent → render_email_html()
    Takes the tier2 analysis text, the intake card HTML, and the document
    metadata, and assembles the complete <!doctype html> email.

Usage (shared across all docket scrapers in docket_engine/):
    from docket_engine.email_renderer import render_intake_card, render_email_html

    base_html = render_intake_card(intake_note, document_url)
    email_html = render_email_html(
        tier2_response=result["tier2_analysis"]["response"],
        tier3_response=result["tier3_risk_assessment"]["response"],
        base_html=base_html,
        metadata=metadata,
    )
"""

from __future__ import annotations

import html as _html_lib
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)  # → docket_engine.email_renderer


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _esc(s: Any = "") -> str:
    """HTML-escape a value (mirrors JS esc() function)."""
    return _html_lib.escape(str(s or ""))


def _format_date(raw: Any) -> str:
    """
    Normalise a date value to MM/DD/YYYY (mirrors JS formatDateToMMDDYYYY).

    Accepts:
        - ISO string:  "2026-06-09T00:00:00.000Z"
        - Plain date:  "2026-06-09" or "06/09/2026"
        - Mongo dict:  {"$date": "..."}
    """
    if not raw:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("$date", "") or ""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return raw


def _parse_sections(text: str = "") -> Dict[str, str]:
    """
    Parse tier2 LLM text into three named sections
    (mirrors JS parseSections — supports both old 'N.' and new '## N.' headers).
    """
    t = str(text or "").replace("\r\n", "\n")

    entry_m = re.search(
        r"(?:^|\n)\s*(?:#+\s*)?1\.\s*ENTRY\s+SUMMARY\s*:?\s*([\s\S]*?)"
        r"(?=(?:\n\s*(?:#+\s*)?2\.\s*LEGAL\s*/\s*REGULATORY\s+SIGNIFICANCE\s*:?)|\s*$)",
        t, re.IGNORECASE,
    )
    legal_m = re.search(
        r"(?:^|\n)\s*(?:#+\s*)?2\.\s*LEGAL\s*/\s*REGULATORY\s+SIGNIFICANCE\s*:?\s*([\s\S]*?)"
        r"(?=(?:\n\s*(?:#+\s*)?3\.\s*CUMULATIVE\s+IMPACT\s*:?)|\s*$)",
        t, re.IGNORECASE,
    )
    cumulative_m = re.search(
        r"(?:^|\n)\s*(?:#+\s*)?3\.\s*CUMULATIVE\s+IMPACT\s*:?\s*([\s\S]*?)\s*$",
        t, re.IGNORECASE,
    )

    return {
        "entry_summary":        entry_m.group(1).strip() if entry_m else "",
        "legal_reg_significance": legal_m.group(1).strip() if legal_m else "",
        "cumulative_impact":    cumulative_m.group(1).strip() if cumulative_m else "",
    }


def _md_to_html(md: str = "") -> str:
    """
    Lightweight markdown → HTML converter (mirrors JS mdToHtml).
    Handles ## headings, **bold**, bullet lists, and line breaks.
    """
    h = str(md or "")
    h = re.sub(
        r"^##\s+(.*)$",
        r'<h3 style="margin:0 0 8px 0;font-size:16px;">\1</h3>',
        h, flags=re.MULTILINE,
    )
    h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
    h = re.sub(r"^[•\-]\s+(.*)$", r"<li>\1</li>", h, flags=re.MULTILINE)
    if "<li>" in h:
        h = re.sub(
            r"(<li>[\s\S]*?</li>)",
            r'<ul style="margin:4px 0 8px 12px;padding:0;">\1</ul>',
            h,
        )
    h = h.replace("\n", "<br>")
    return h


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_intake_card(
    intake_note: Dict[str, Any],
    document_url: str = "",
) -> str:
    """
    Render the intake note as a partial HTML card fragment (header rows).

    Equivalent to the n8n 'Code in JavaScript4' node (Untitled-2).

    Args:
        intake_note:   Dict with keys Filing, Type, Summary, Relevance.
        document_url:  URL to link as the document (metadata.url or document_id).

    Returns:
        Partial HTML string (<tr> blocks) to embed inside the email card.
        Summary is extracted but intentionally not rendered (mirrors n8n).
    """
    TYPE = _esc(intake_note.get("Type",      ""))
    FILING = _esc(intake_note.get("Filing",    ""))
    RELEVANCE = _esc(intake_note.get("Relevance", ""))
    # Summary intentionally not rendered — matches n8n behaviour

    BORDER = "#e5e7eb"
    TEXT, MUTED = "#1f2937", "#6b7280"

    doc_link = _esc(document_url or "")
    link_row = (
        f"""<tr>
          <td style="padding:12px 0;font-weight:700;color:{TEXT};vertical-align:top">Document URL</td>
          <td style="padding:12px 0;color:{TEXT};line-height:1.6"><a href="{doc_link}">Link</a></td>
        </tr>"""
        if doc_link else ""
    )

    return f"""
          <tr>
            <td style="padding:20px 24px;border-bottom:1px solid {BORDER}">
              <div style="font-size:14px;color:{MUTED};margin-bottom:4px">{TYPE}</div>
              <div style="font-size:20px;font-weight:700;color:{TEXT};line-height:1.3">{FILING}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px 4px 24px">
              <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse">
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};width:180px;vertical-align:top">Filing</td>
                  <td style="padding:8px 0;color:{TEXT}">{FILING}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">Type</td>
                  <td style="padding:8px 0;color:{TEXT}">{TYPE}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">Relevance</td>
                  <td style="padding:8px 0;color:{TEXT}">{RELEVANCE}</td>
                </tr>
             
              </table>
            </td>
          </tr>
    """


def render_email_html(
    tier2_response: str,
    tier3_response: str,
    base_html: str,
    metadata: Dict[str, Any],
) -> str:
    """
    Assemble the complete HTML email for a docket filing notification.

    Equivalent to the n8n 'Html Formate For Email' node (Untitled-1).

    Args:
        tier2_response: Raw tier2 analysis text from analyze_docket_entry().
        tier3_response: Raw tier3 risk assessment text (converted internally
                        but not inserted — matches n8n behaviour).
        base_html:      Partial HTML fragment from render_intake_card().
        metadata:       Metadata dict with keys: date, url, document_id.

    Returns:
        Complete <!doctype html> email string.
    """
    # Parse tier2 into named sections
    tier2 = _parse_sections(tier2_response)

    # tier3 converted but intentionally not inserted — matches n8n
    _tier3_html = _md_to_html(tier3_response)  # noqa: F841

    ENTRY_SUMMARY = _esc(tier2["entry_summary"])
    LEGAL_REG = _esc(tier2["legal_reg_significance"])
    CUMULATIVE = _esc(tier2["cumulative_impact"])

    # Document URL: prefer metadata.url, fall back to document_id
    doc_url = metadata.get("url") or metadata.get("document_id") or ""
    DATE = _format_date(metadata.get("date", ""))

    BG, CARD, BORDER = "#f8fafc", "#ffffff", "#e5e7eb"
    TEXT = "#1f2937"

    doc_link_row = (
        f"""<tr>
          <td style="padding:12px 0;font-weight:700;color:{TEXT};vertical-align:top">Document URL</td>
          <td style="padding:12px 0;color:{TEXT};line-height:1.6">
            <a href="{_esc(doc_url)}">Link</a>
          </td>
        </tr>"""
        if doc_url else ""
    )

    # Relevance is already shown in base_html (render_intake_card).
    # _parse_sections never produces a "Relevance" key, so this row stays empty —
    # matches n8n behaviour where tier2['Relevance'] was also always empty.
    relevance_row = ""

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Docket Update</title>
</head>
<body style="margin:0;padding:0;background:{BG};-webkit-text-size-adjust:100%">
  <table role="presentation" cellpadding="0" cellspacing="0"
         style="width:100%;border-collapse:collapse;background:{BG}">
    <tr>
      <td align="center" style="padding:24px 12px">
        <table role="presentation" cellpadding="0" cellspacing="0"
               style="width:100%;max-width:640px;background:{CARD};border:1px solid {BORDER};
                      border-radius:10px;overflow:hidden;border-collapse:separate">
          {base_html}
          <tr>
            <td style="padding:4px 24px 16px 24px">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;border-collapse:collapse">
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};width:180px;vertical-align:top">Date</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{DATE}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};width:180px;vertical-align:top">ENTRY SUMMARY</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{ENTRY_SUMMARY}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">LEGAL / REGULATORY SIGNIFICANCE</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{LEGAL_REG}</td>
                </tr>
                <tr>
                  <td style="padding:8px 0;font-weight:600;color:{TEXT};vertical-align:top">CUMULATIVE IMPACT</td>
                  <td style="padding:8px 0;color:{TEXT};line-height:1.6">{CUMULATIVE}</td>
                </tr>
                {relevance_row}
                {doc_link_row}
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
