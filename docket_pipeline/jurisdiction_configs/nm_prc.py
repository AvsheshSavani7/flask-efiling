"""
New Mexico Public Regulation Commission jurisdiction config.
Case No. 25-00060-UT — TXNM Energy / Blackstone Infrastructure acquisition.
"""

from .base import JurisdictionConfig

NM_PRC_CONFIG = JurisdictionConfig(
    jurisdiction_id="nm-prc",
    name="New Mexico Public Regulation Commission",
    docket_number="25-00060-UT",
    deal_description="TXNM Energy / Blackstone Infrastructure acquisition",
    regulatory_statute="NMSA 1978 § 62-6-12",

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
        "public service company of new mexico",
        "pnm",
        "txnm energy",
        "txnm",
        "troy parentco",
        "blackstone",
        "blackstone infrastructure",
    ],

    commission_names=[
        "new mexico public regulation commission",
        "nmprc",
        "nm prc",
        "prc staff",
        "commission staff",
        "hearing examiner",
    ],

    commission_doc_types=[
        "order",
        "notice",
        "procedural order",
        "recommended decision",
        "commission order",
    ],

    filer_taxonomy="""
   - consumer_advocate (New Mexico Attorney General / DOJ, Prosperity Works,
     New Mexico Consumer Protection Alliance)
   - environmental (New Energy Economy, Center for Biological Diversity,
     San Juan Citizens Alliance, Diné CARE, Tó Nizhóní Aní)
   - renewable_energy (Renewable Energy Industries Association of NM / REIA-NM, Naeva)
   - government (Albuquerque Bernalillo County Water Utility Authority / ABCWUA,
     NM Energy Minerals & Natural Resources Dept / EMNRD / ECAM Division)
   - business_customer (Walmart, Kroger, large commercial/industrial ratepayers)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is New Mexico Public Regulation Commission Case No. 25-00060-UT: "
        "Joint application of Public Service Company of New Mexico (PNM), "
        "TXNM Energy Inc., and Troy ParentCo LLC (a subsidiary of Blackstone "
        "Infrastructure) for approval of an acquisition and merger. "
        "On August 25, 2025, PNM and TXNM Energy applied for regulatory approval "
        "to be acquired by Blackstone Infrastructure. Key issues include rate "
        "impacts, energy affordability, renewable energy commitments, and a "
        "contested $400 million stock transaction between Blackstone and TXNM. "
        "This is a highly contentious proceeding with numerous intervenors."
    ),
)
