"""
Email Subject Builder
=====================
Central module for building standardized email subject lines across all
foreign filing scrapers. Update AGENCY_NAMES or EVENT_LABELS here to
change subjects globally without touching individual scraper files.

Subject formats:
  Matched (FRMD): "{prefix}: {Agency} - {Event Label} - [FRMD]"
                  prefix = target[/acquirer]
                  target = target_ticker, else target_name/target, else "Unknown"
                  acquirer = acquirer_ticker, else acquirer/acquire_name (omit if absent)
  Unmatched (FRUD): "{Agency} - {Event Label} - [FRUD]"
  Partial (FRPMD):  "{Agency} - {Event Label} - [FRPMD-A]" or "[FRPMD-T]"
                    unmatched subject; body uses deal-details banner, not the USA block
"""

from html import escape as escape_html
from typing import Optional

# ---------------------------------------------------------------------------
# Agency name registry
# ---------------------------------------------------------------------------
# Keys are used in build_subject() calls throughout all scraper files.
# Change the display name here to update all email subjects for that agency.

AGENCY_NAMES: dict[str, str] = {
    "accc":               "ACCC",
    "accc_waiver":        "ACCC Waiver",
    "cade":               "CADE Brazil",
    "bundeskartellamt":   "German Bundeskartellamt",
    "ec_merger":          "EC Merger",
    "ec_fs":              "EC Foreign Subsidies",
    "ftc":                "FTC",
    "nz_comcom":          "NZ Commerce Commission",
    "canada":             "Canada Competition Bureau",
    "uk_cma":             "UK CMA",
    "samr_unconditional": "SAMR China Unconditional Approval",
    "samr_conditional":   "SAMR China Conditional Approval",
    "samr_public":        "SAMR China Public Notice",
    "cci":                "CCI India",
    "turkey_rekabet":     "Turkey Rekabet Kurumu",
    "mexico_cna":         "Mexico CNA",
    "comesa":             "COMESA Competition Commission",
    "bwb":                "Austrian BWB",
    "sa_compcom":         "South Africa CompCom",
    "jftc":               "JFTC Japan",
    "kftc":               "KFTC Korea",
    "chile_fne":          "Chile FNE",
    "taiwan_ftc":         "Taiwan FTC",
    "ukraine_amcu":       "Ukraine AMCU",
}

# ---------------------------------------------------------------------------
# Event type labels
# ---------------------------------------------------------------------------

EVENT_LABELS: dict[str, str] = {
    "new":              "New Regulatory Case",
    "update":           "Regulatory Update",
    "under_assessment": "Under Assessment",
    "press_release":    "Press Release",
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _target_label(deal_match: dict) -> str:
    """Target side of prefix: target_ticker, else target name, else Unknown."""
    ticker = (deal_match.get("target_ticker") or "").strip()
    if ticker:
        return ticker
    name = deal_match.get("target_name") or deal_match.get("target") or ""
    name = str(name).strip()
    return name if name else "Unknown"


def _acquirer_label(deal_match: dict) -> str:
    """Acquirer side of prefix: acquirer_ticker, else acquirer name."""
    ticker = (deal_match.get("acquirer_ticker") or "").strip()
    if ticker:
        return ticker
    name = (
        deal_match.get("acquirer")
        or deal_match.get("acquire_name")
        or deal_match.get("acquirer_name")
        or ""
    )
    return str(name).strip()


def _deal_prefix_label(deal_match: dict) -> str:
    """Prefix for matched subjects: target, or target/acquirer when acquirer is known."""
    target = _target_label(deal_match)
    acquirer = _acquirer_label(deal_match)
    if acquirer:
        return f"{target}/{acquirer}"
    return target


def build_subject(
    agency_key: str,
    event_type: str,
    deal_match: Optional[dict] = None,
) -> str:
    """
    Build a standardized email subject line.

    Args:
        agency_key:  Key from AGENCY_NAMES (e.g. "uk_cma", "accc").
        event_type:  Key from EVENT_LABELS: "new", "update", or "under_assessment".
        deal_match:  Full deal document from MongoDB (as returned by get_deal_by_id),
                     or None for unmatched / FRUD emails.

    Returns:
        e.g. "AZEK/BLDR: UK CMA - New Regulatory Case - [FRMD]"
             "AZEK: UK CMA - New Regulatory Case - [FRMD]"
             "The AZEK Company Inc.: UK CMA - New Regulatory Case - [FRMD]"
             "Unknown: UK CMA - New Regulatory Case - [FRMD]"
             "UK CMA - Regulatory Update - [FRUD]"
    """
    agency = AGENCY_NAMES.get(agency_key, agency_key)
    label = EVENT_LABELS.get(event_type, event_type)

    if deal_match:
        prefix = f"{_deal_prefix_label(deal_match)}: "
        return f"{prefix}{agency} - {label} - [FRMD]"
    else:
        return f"{agency} - {label} - [FRUD]"


def partial_match_tag(side: str) -> str:
    """[FRPMD-A] for acquirer side, [FRPMD-T] for target side."""
    if (side or "").lower().startswith("acquir"):
        return "[FRPMD-A]"
    return "[FRPMD-T]"


def apply_partial_match_subject(subject: str, side: str) -> str:
    """Replace [FRUD] (or [FRMD]) with [FRPMD-A] / [FRPMD-T]."""
    tag = partial_match_tag(side)
    if "[FRUD]" in (subject or ""):
        return subject.replace("[FRUD]", tag)
    if "[FRMD]" in (subject or ""):
        return subject.replace("[FRMD]", tag)
    return subject


def partial_match_side_label(side: str) -> str:
    """Human label for the matched side: 'acquirer' or 'target'."""
    if (side or "").lower().startswith("acquir"):
        return "acquirer"
    return "target"


def build_partial_match_banner_html(
    deal_match: Optional[dict],
    partial_side: str,
    *,
    deal_id: Optional[str] = None,
) -> str:
    """Yellow banner with one-side deal details. Not the USA-related FRUD block."""
    side = partial_match_side_label(partial_side)
    deal = deal_match or {}
    target = deal.get("target") or deal.get("target_name") or "N/A"
    acquirer = (
        deal.get("acquirer")
        or deal.get("acquire_name")
        or deal.get("acquirer_name")
        or "N/A"
    )
    shown_id = deal.get("deal_id") or deal_id or "N/A"
    return f"""
<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
  <div style="font-weight:800;color:#92400e;margin-bottom:4px;">Partial match ({escape_html(side)} side)</div>
  <div style="font-size:14px;color:#78350f;">
    Only one side of this deal is named in the filing. Deal ID is not stored on the case.<br>
    <b>Acquirer:</b> {escape_html(str(acquirer))} &nbsp;|&nbsp;
    <b>Target:</b> {escape_html(str(target))} &nbsp;|&nbsp;
    <b>Deal ID:</b> {escape_html(str(shown_id))}
  </div>
</div>"""
