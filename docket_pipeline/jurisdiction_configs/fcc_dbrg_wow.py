"""
Federal Communications Commission (ECFS) jurisdiction config.
WC Docket No. 26-40 — SoftBank / DigitalBridge / WideOpenWest & Knology transfer of control.
"""

from .base import JurisdictionConfig

FCC_DBRG_WOW_CONFIG = JurisdictionConfig(
    jurisdiction_id="fcc-dbrg-wow",
    name="Federal Communications Commission",
    docket_number="26-40",
    deal_description="SoftBank / DigitalBridge / WideOpenWest transfer of control",
    regulatory_statute="47 U.S.C. § 214(a) (Communications Act of 1934)",

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
        "wideopenwest",
        "wow",
        "wow parent",
        "knology",
        "knology of the valley",
        "valley telephone",
    ],

    commission_names=[
        "federal communications commission",
        "fcc",
        "fcc staff",
        "wireline competition bureau",
    ],

    commission_doc_types=[
        "order",
        "public notice",
        "decision",
        "memorandum opinion and order",
    ],

    filer_taxonomy="""
   - competitor (other ISPs, cable operators, telecom providers)
   - consumer_advocate (consumer protection organizations)
   - business_customer (enterprise/commercial users)
   - special_interest (trade associations, public interest organizations)
   - labor (trade unions, labor organizations)
   - government (federal agencies, state entities, state PUCs)
   - other""",

    system_context=(
        "This is FCC WC Docket No. 26-40: Application of Duncan Holdco LLC "
        "(controlled by SoftBank Group Corp.) for transfer of control of "
        "WideOpenWest, Inc. (WOW), Knology of the Valley, Inc., Valley "
        "Telephone Company, LLC, Knology Total Communications, Inc., and "
        "Knology of Florida, LLC from DigitalBridge Group, Inc. WOW is a "
        "regional ISP/cable operator. Knology Valley is designated as an "
        "Eligible Telecommunications Carrier (ETC) in Alabama and Georgia, "
        "receives Connect America Fund BLS and CAF ICC support, and "
        "participates in the Lifeline program. Part of SoftBank's $4 billion "
        "acquisition of DigitalBridge. Filed February 2026. FCC Wireline "
        "Bureau approved the transaction in July 2026."
    ),
)
