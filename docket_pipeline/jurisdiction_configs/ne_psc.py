"""
Nebraska Public Service Commission jurisdiction config.
Docket NG-128 — NorthWestern Energy / Black Hills Corporation merger.
Approved via settlement agreements on May 19, 2026.
"""

from .base import JurisdictionConfig

NE_PSC_CONFIG = JurisdictionConfig(
    jurisdiction_id="ne-psc",
    name="Nebraska Public Service Commission",
    docket_number="NG-128",
    deal_description="NorthWestern Energy / Black Hills Corporation natural gas utility merger",
    regulatory_statute="Neb. Rev. Stsat. § 75-136 (State Natural Gas Regulation Act)",

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
        "northwestern energy",
        "northwestern corporation",
        "northwestern energy public service",
        "black hills",
        "black hills corporation",
        "black hills energy",
    ],

    commission_names=[
        "nebraska public service commission",
        "nebraska psc",
        "ne psc",
        "commission staff",
        "psc staff",
    ],

    commission_doc_types=[
        "order",
        "notice",
        "commission order",
        "procedural order",
    ],

    filer_taxonomy="""
   - consumer_advocate (Public Advocate, ratepayer representatives)
   - labor (Laborers International Union of North America / LIUNA,
     trade unions)
   - business_customer (industrial/commercial ratepayers)
   - government (municipalities, county governments)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is Nebraska Public Service Commission Docket NG-128: "
        "Joint application of NorthWestern Energy Public Service Corporation, "
        "Black Hills Corporation, and NorthWestern Energy Group, Inc. for "
        "approval of a natural gas utility merger. Filed October 27, 2025, "
        "with an intervention deadline of December 1, 2025. Evidentiary "
        "hearings were held April 7-8, 2026. The Commission approved "
        "settlement agreements on May 19, 2026, making Nebraska the first "
        "state to approve the merger. Key settlement terms include rate "
        "moratoriums, prohibition on recovering merger transaction costs "
        "from ratepayers, and workforce/contracting protections."
    ),
)
