"""
Virginia State Corporation Commission jurisdiction config.
Case No. PUR-2026-00112 — NextEra Energy / Dominion Energy merger.
"""

from .base import JurisdictionConfig

VA_PUC_CONFIG = JurisdictionConfig(
    jurisdiction_id="va-puc",
    name="Virginia State Corporation Commission",
    docket_number="147078d",
    deal_description="NextEra Energy / Dominion Energy merger",
    regulatory_statute="Virginia Code § 56-90 (Utility Transfers Act)",

    field_map={
        "document_id": "document_id",
        "filed_by": "filed_by",
        "date": "created_date",
        "content": "extracted_text",
        "filename": "document_filename",
        "doc_type": "document_type",
        "document_type": "document_type",
        "decision_summary": None,
        "attachment": "document_filename",
    },

    records_path="records",

    date_format="%d/%m/%Y",

    party_names=[
        "nextera energy",
        "nextera",
        "dominion energy",
        "dominion",
        "virginia electric and power",
        "vepco",
        "dominion energy virginia",
        "osw project",
    ],

    commission_names=[
        "virginia state corporation commission",
        "virginia scc",
        "scc",
        "scc staff",
        "commission staff",
    ],

    commission_doc_types=[
        "order",
        "ruling",
        "hearing examiner ruling",
        "final order",
        "procedural order",
        "commission order",
    ],

    filer_taxonomy="""
   - consumer_advocate (Virginia Office of the Attorney General,
     consumer protection organizations)
   - environmental (Clean Virginia, Southern Environmental Law Center / SELC,
     conservation/environmental advocacy groups)
   - government (Governor of Virginia, municipal governments,
     Virginia General Assembly members)
   - business_customer (large commercial/industrial ratepayers)
   - labor (trade unions, labor organizations)
   - renewable_energy (clean energy developers, solar/wind industry groups)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is Virginia State Corporation Commission Case No. PUR-2026-00112: "
        "Joint Petition of NextEra Energy, Inc. and Dominion Energy, Inc. for "
        "approval of a merger under the Virginia Utility Transfers Act. NextEra "
        "is acquiring Dominion Energy in a $67 billion transaction. Virginia "
        "Electric and Power Company (d/b/a Dominion Energy Virginia) would remain "
        "a separately incorporated Virginia public utility. Under VA Code § 56-90, "
        "the SCC must find that adequate service at just and reasonable rates will "
        "not be impaired or jeopardized. The Transfers Act provides for a 60-day "
        "initial review deadline (Sept 13, 2026) extendable by up to 120 days "
        "(final deadline Jan 11, 2027). Key issues include customer rate impacts, "
        "Coastal Virginia Offshore Wind project continuity, Virginia Clean Economy "
        "Act compliance, and NextEra's track record. Governor Spanberger has "
        "formally intervened in the proceeding. Joint Petition filed July 15, 2026."
    ),
)
