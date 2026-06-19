"""
South Dakota Public Utilities Commission jurisdiction config.
Docket GE25-001 — NorthWestern Energy / Black Hills Corporation merger.
"""

from .base import JurisdictionConfig

SD_PUC_CONFIG = JurisdictionConfig(
    jurisdiction_id="sd-puc",
    name="South Dakota Public Utilities Commission",
    docket_number="GE25-001",
    deal_description="NorthWestern Energy / Black Hills Corporation utility merger",
    regulatory_statute="SDCL 49-34A-35",

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
        "black hills service company",
    ],

    commission_names=[
        "south dakota public utilities commission",
        "south dakota puc",
        "sd puc",
        "commission staff",
        "puc staff",
    ],

    commission_doc_types=[
        "order",
        "notice",
        "commission order",
        "procedural order",
        "commission notice",
    ],

    filer_taxonomy="""
   - consumer_advocate (SD PUC Staff, consumer groups)
   - labor (Laborers Local 620, LIUNA, trade unions)
   - business_customer (industrial/commercial ratepayers)
   - agricultural (farm organizations, agricultural cooperatives)
   - government (municipalities, county governments)
   - environmental (conservation/environmental advocacy groups)
   - special_interest (advocacy groups, community organizations)
   - other""",

    system_context=(
        "This is South Dakota Public Utilities Commission Docket GE25-001: "
        "Joint application of NorthWestern Energy Public Service Corporation, "
        "Black Hills Corporation, and NorthWestern Energy Group, Inc. for "
        "approval of merger. This is a utility merger proceeding under "
        "SDCL 49-34A-35, which prohibits the sale, merger, or consolidation "
        "of utility property unless authorized by the commission. "
        "Key issues include rate impacts, service reliability, and "
        "workforce protections for South Dakota utility customers."
    ),
)
