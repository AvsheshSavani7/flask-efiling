"""
California Public Utilities Commission jurisdiction config.
Application A.25-07-016 — Charter Communications / Cox Communications merger.
"""

from .base import JurisdictionConfig

CPUC_CONFIG = JurisdictionConfig(
    jurisdiction_id="cpuc",
    name="California Public Utilities Commission",
    docket_number="A2507016",
    deal_description="Charter Communications / Cox Communications telecom merger",
    regulatory_statute="California Public Utilities Code § 854",

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
        "charter communications",
        "charter",
        "cox communications",
        "cox enterprises",
        "cox california telcom",
        "liberty broadband",
    ],

    commission_names=[
        "california public utilities commission",
        "cpuc",
        "commission staff",
        "public advocates office",
        "cal advocates",
    ],

    commission_doc_types=[
        "order",
        "decision",
        "ruling",
        "proposed decision",
        "alternate proposed decision",
        "administrative law judge ruling",
        "alj ruling",
        "commissioner ruling",
    ],

    filer_taxonomy="""
   - consumer_advocate (TURN / The Utility Reform Network,
     Cal Advocates / CPUC Public Advocates Office)
   - digital_equity (CETF / California Emerging Technology Fund)
   - labor (CWA / Communications Workers of America)
   - media (Media Alliance)
   - disability (CforAT / Center for Accessible Technology)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is California Public Utilities Commission Application A.25-07-016: "
        "Application of Charter Communications, Inc. and Cox Enterprises, Inc. "
        "for approval of the indirect transfer of control of Cox California "
        "Telcom, LLC, a California public utility. Charter is acquiring Cox "
        "Communications in a $34.5 billion transaction. Liberty Broadband "
        "(LBRDA/LBRDK) stockholder approval was obtained February 26, 2025, "
        "with that closing accelerated to occur contemporaneously. Under PU "
        "Code § 854, the CPUC must find the transaction is in the public interest. "
        "Key issues include low-income broadband access, digital inclusion "
        "investments, statewide pricing, and infrastructure upgrade commitments. "
        "CPUC vote is scheduled for August 13, 2026."
    ),
)
