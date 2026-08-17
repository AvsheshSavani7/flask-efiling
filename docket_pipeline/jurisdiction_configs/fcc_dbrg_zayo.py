"""
Federal Communications Commission (ECFS) jurisdiction config.
WC Docket No. 26-56 — SoftBank / DigitalBridge / Zayo Group transfer of control.
"""

from .base import JurisdictionConfig

FCC_DBRG_ZAYO_CONFIG = JurisdictionConfig(
    jurisdiction_id="fcc-dbrg-zayo",
    name="Federal Communications Commission",
    docket_number="26-56",
    deal_description="SoftBank / DigitalBridge / Zayo Group indirect transfer of control",
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
        "zayo group",
        "zayo network services",
        "fiber assetco",
        "eqt",
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
   - competitor (other telecom/fiber providers, cable operators)
   - business_customer (enterprise users of Zayo fiber/bandwidth services)
   - special_interest (trade associations, public interest organizations)
   - labor (trade unions, labor organizations)
   - government (federal agencies, state entities)
   - other""",

    system_context=(
        "This is FCC WC Docket No. 26-56: Application of Duncan Holdco LLC "
        "(controlled by SoftBank Group Corp.) for indirect transfer of control "
        "of Zayo Group, LLC, Zayo Network Services, LLC, and Fiber AssetCo LLC "
        "from DigitalBridge Group, Inc. Zayo is co-owned by DigitalBridge and "
        "EQT AB. Zayo provides bandwidth infrastructure and interconnection "
        "services over regional and metropolitan fiber networks. Zayo also owns "
        "25% of the AmeriCan-1 submarine fiber optic cable (US-Canada), which "
        "triggers separate submarine cable landing license review. Part of "
        "SoftBank's $4 billion acquisition of DigitalBridge. Filed March 2026. "
        "FCC Wireline Bureau approved the transaction in July 2026."
    ),
)
