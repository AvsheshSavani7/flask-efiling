"""
Montana Public Service Commission jurisdiction config.
Docket 2025.10.078 — NorthWestern Energy / Black Hills Corporation merger.
"""

from .base import JurisdictionConfig

MT_PSC_CONFIG = JurisdictionConfig(
    jurisdiction_id="mt-psc",
    name="Montana Public Service Commission",
    docket_number="2025.10.078",
    deal_description="NorthWestern Energy / Black Hills Corporation utility merger",
    regulatory_statute="Mont. Code Ann. 69-3-103",

    field_map={
        "document_id": "document_id",
        "filed_by": "filed_by",
        "date": "created_date",
        "content": "extracted_text",
        "filename": "document_filename",
        "doc_type": "document_type",
        "document_type": "document_type",
        "decision_summary": None,       # MT PSC doesn't have this field
        "attachment": "document_filename",  # local PDF filename
    },

    records_path="records",  # data is nested under {"records": [...]}

    date_format="%d/%m/%Y",

    party_names=[
        "northwestern energy",
        "northwestern corporation",
        "black hills",
        "black hills corporation",
        "black hills energy",
        "nwe group",
    ],

    commission_names=[
        "montana public service commission",
        "montana psc",
        "mpsc",
        "commission staff",
        "regulatory division",
    ],

    commission_doc_types=[
        "order",
        "notice",
        "commission order",
        "procedural order",
        "commission notice",
    ],

    filer_taxonomy="""
   - environmental (350 Montana, Northwest Energy Coalition/NWEC, Montana Environmental Information Center)
   - consumer_advocate (Montana Consumer Counsel, MCC)
   - business_customer (Montana Large Customer Group, industrial/commercial ratepayers)
   - agricultural (Montana Farmers Union, agricultural cooperatives)
   - labor (Laborers Local 1686, IBEW, trade unions)
   - government (City of Missoula, Missoula County, municipal governments)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is Montana Public Service Commission Docket No. 2025.10.078: "
        "Application of NWE Group Inc. (a subsidiary of NorthWestern Corporation, "
        "d/b/a NorthWestern Energy) to acquire Black Hills Corporation's Montana "
        "utility operations (Black Hills Montana Gas and Electric). "
        "This is a utility merger proceeding under Mont. Code Ann. 69-3-103, "
        "which requires the Commission to find the transaction serves the public interest. "
        "Key issues include rate impacts, service reliability, environmental commitments, "
        "and workforce protections for Montana utility customers."
    ),
)
