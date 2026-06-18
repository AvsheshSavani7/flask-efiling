"""
STB (Surface Transportation Board) jurisdiction config.
For future re-runs through the generic docket_extract.py pipeline.
"""

from .base import JurisdictionConfig

STB_CONFIG = JurisdictionConfig(
    jurisdiction_id="stb",
    name="Surface Transportation Board",
    docket_number="FD-36873",
    deal_description="Union Pacific / Norfolk Southern railroad merger",
    regulatory_statute="49 U.S.C. § 11324",

    field_map={
        "document_id": "displayId",
        "filed_by": "by",
        "date": "sortDate",
        "content": "content",
        "filename": None,           # STB uses attachment URL instead
        "doc_type": "docType",
        "document_type": "documentType",
        "decision_summary": "decisionSummary",
        "attachment": "attachment",
    },

    records_path=None,  # top-level list

    date_format="%Y-%m-%d",

    party_names=[
        "union pacific",
        "norfolk southern",
    ],

    commission_names=[
        "surface transportation board",
        "stb",
    ],

    commission_doc_types=[
        "decision",
        "order",
        "notice",
    ],

    filer_taxonomy="""
   - competitor (other railroads: BNSF, CSX, CN, CPKC, short lines)
   - business_customer (shippers: chemical companies, grain elevators, manufacturers, utilities)
   - retail_customer (individual citizens)
   - special_interest (advocacy groups, trade associations, public interest orgs)
   - labor (unions: SMART-TD, BLET, BRS, TCU, etc.)
   - government (AG, municipalities, state DOTs, federal agencies)
   - other""",

    system_context=(
        "This is STB (Surface Transportation Board) Docket FD-36873: "
        "Union Pacific Corporation / Union Pacific Railroad Company seeking CONTROL "
        "of Norfolk Southern Corporation / Norfolk Southern Railway Company. "
        "This is a major railroad merger proceeding under 49 U.S.C. § 11324."
    ),
)
