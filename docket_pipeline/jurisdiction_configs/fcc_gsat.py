"""
Federal Communications Commission (ECFS) jurisdiction config.
GN Docket No. 26-134 — Amazon / Globalstar satellite license transfer.
"""

from .base import JurisdictionConfig

FCC_GSAT_CONFIG = JurisdictionConfig(
    jurisdiction_id="fcc-gsat",
    name="Federal Communications Commission",
    docket_number="26-134",
    deal_description="Amazon / Globalstar satellite license assignment and transfer of control",
    regulatory_statute="47 U.S.C. §§ 214(a), 310(d) (Communications Act of 1934)",

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
        "amazon",
        "amazon.com",
        "globalstar",
        "globalstar inc",
        "kuiper systems",
        "amazon leo",
    ],

    commission_names=[
        "federal communications commission",
        "fcc",
        "fcc staff",
        "wireline competition bureau",
        "international bureau",
        "wireless telecommunications bureau",
    ],

    commission_doc_types=[
        "order",
        "public notice",
        "decision",
        "memorandum opinion and order",
        "notice of proposed rulemaking",
    ],

    filer_taxonomy="""
   - competitor (satellite operators, MSS providers, spectrum holders)
   - business_customer (enterprise/commercial users of satellite services)
   - special_interest (ITIF, Public Knowledge, Open Technology Institute / New America,
     trade associations, public interest organizations)
   - retail_customer (individual citizens)
   - government (federal agencies, state entities)
   - other""",

    system_context=(
        "This is FCC GN Docket No. 26-134: Application of Amazon.com, Inc. "
        "and Globalstar, Inc. for assignment and transfer of control of "
        "Globalstar's space station licenses, earth station licenses, domestic "
        "and international Section 214 authorizations, and experimental license. "
        "Globalstar is a Mobile Satellite Service (MSS) provider using Big LEO "
        "frequency bands (1.6/2.4 GHz service links, 5/7 GHz feeder links). "
        "Upon completion, Globalstar will become a wholly-owned subsidiary of "
        "Amazon and operate as an affiliate to Kuiper Systems LLC. Amazon also "
        "plans to acquire Apple's 20% stake in Globalstar. Application filed "
        "May 26, 2026; Public Notice DA 26-550 issued June 4, 2026."
    ),
)
