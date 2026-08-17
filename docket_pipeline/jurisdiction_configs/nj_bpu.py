"""
New Jersey Board of Public Utilities jurisdiction config.
Docket TM26030047 — SoftBank / DigitalBridge / Zayo Group NJ telecom transfer.
"""

from .base import JurisdictionConfig

NJ_BPU_CONFIG = JurisdictionConfig(
    jurisdiction_id="nj-bpu",
    name="New Jersey Board of Public Utilities",
    docket_number="TM26030047",
    deal_description="SoftBank / DigitalBridge / Zayo Group NJ telecom transfer of control",
    regulatory_statute="N.J.S.A. 48:2-51.1 (NJ telecom transfer of control)",

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
        "softbank",
        "softbank group",
        "duncan holdco",
        "digitalbridge",
        "digitalbridge group",
        "zayo group",
        "zayo network services",
        "fiber assetco",
    ],

    commission_names=[
        "new jersey board of public utilities",
        "nj bpu",
        "bpu",
        "bpu staff",
        "board staff",
        "rate counsel",
    ],

    commission_doc_types=[
        "order",
        "notice",
        "board order",
        "initial decision",
        "procedural order",
    ],

    filer_taxonomy="""
   - consumer_advocate (NJ Division of Rate Counsel, consumer groups)
   - competitor (other telecom/fiber providers in NJ)
   - business_customer (enterprise/commercial telecom users)
   - government (NJ Attorney General, municipalities)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is New Jersey Board of Public Utilities Docket TM26030047: "
        "Petition of Duncan Holdco LLC (controlled by SoftBank Group Corp.) "
        "for approval of the indirect transfer of control of Zayo Group's "
        "New Jersey telecommunications operations from DigitalBridge Group, Inc. "
        "This is the New Jersey state-level counterpart to FCC WC Docket 26-56. "
        "Zayo provides bandwidth infrastructure and interconnection services "
        "over fiber networks in New Jersey. Part of SoftBank's $4 billion "
        "acquisition of DigitalBridge. The broader transaction has received "
        "FCC approval."
    ),
)
