"""
Base jurisdiction configuration dataclass.
Every jurisdiction config inherits from this and specifies its own
field mappings, party lists, filer taxonomy, and LLM context.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class JurisdictionConfig:
    """Parameterizes everything jurisdiction-specific for docket extraction."""

    # ── Identity ──────────────────────────────────────────────────────────
    jurisdiction_id: str          # e.g. "stb", "mt-psc"
    name: str                     # e.g. "Surface Transportation Board"
    docket_number: str            # e.g. "FD-36873", "2025.10.078"
    deal_description: str         # short description for prompts
    regulatory_statute: str       # e.g. "49 U.S.C. § 11324"

    # ── Field mapping ─────────────────────────────────────────────────────
    # Maps canonical field names → input JSON field names.
    # Canonical names: document_id, filed_by, date, content, filename,
    #                  doc_type, document_type, decision_summary, attachment
    field_map: Dict[str, str] = field(default_factory=dict)

    # ── Input structure ───────────────────────────────────────────────────
    # JSON path to the records list. None = top-level list, "records" = data["records"]
    records_path: Optional[str] = None

    # ── Date format ───────────────────────────────────────────────────────
    # strptime format for the date field in the input JSON
    date_format: str = "%Y-%m-%d"
    # Output date format (always ISO)
    output_date_format: str = "%Y-%m-%d"

    # ── Party / Commission classification ─────────────────────────────────
    # Lowercase names/substrings that identify merger parties
    party_names: List[str] = field(default_factory=list)
    # Lowercase names/substrings that identify the commission/regulator
    commission_names: List[str] = field(default_factory=list)
    # Document types that indicate a commission filing (lowercase)
    commission_doc_types: List[str] = field(default_factory=list)

    # ── Filer taxonomy (for LLM prompt) ───────────────────────────────────
    # Describes intervenor subtypes specific to this jurisdiction
    filer_taxonomy: str = ""

    # ── System prompt context ─────────────────────────────────────────────
    # Deal-specific paragraph appended to the system prompt
    system_context: str = ""
