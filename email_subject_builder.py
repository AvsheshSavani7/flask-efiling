"""
Email Subject Builder
=====================
Central module for building standardized email subject lines across all
foreign filing scrapers. Update AGENCY_NAMES or EVENT_LABELS here to
change subjects globally without touching individual scraper files.

Subject formats:
  Matched (FRMD): "{prefix}: {Agency} - {Event Label} - [FRMD]"
                  prefix = target_ticker, else target_name/target, else "Unknown"
  Unmatched (FRUD): "{Agency} - {Event Label} - [FRUD]"
"""

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
}

# ---------------------------------------------------------------------------
# Event type labels
# ---------------------------------------------------------------------------

EVENT_LABELS: dict[str, str] = {
    "new":              "New Regulatory Case",
    "update":           "Regulatory Update",
    "under_assessment": "Under Assessment",
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _deal_prefix_label(deal_match: dict) -> str:
    """Prefix for matched subjects: target_ticker, else target name, else Unknown."""
    ticker = (deal_match.get("target_ticker") or "").strip()
    if ticker:
        return ticker
    name = deal_match.get("target_name") or deal_match.get("target") or ""
    name = str(name).strip()
    return name if name else "Unknown"


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
        e.g. "AZEK: UK CMA - New Regulatory Case - [FRMD]"
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
