#!/usr/bin/env python3
"""
Docket Entry Analyzer
=====================
Analyzes docket entries with tier 1, tier 2, and tier 3 summaries.
If entry already exists in database, returns existing data.
Otherwise generates new analysis using all historical summaries and adds to database.
"""

import json
import os
from typing import Dict, Any, Optional
from datetime import datetime
import anthropic
from pymongo import MongoClient

ENV_FILE = ".env"
TIER1_MODEL = "claude-3-haiku-20240307"
TIER2_MODEL = "claude-3-5-haiku-20241022"
TIER3_MODEL = "claude-sonnet-4-20250514"


def _load_env_file(env_path: str) -> None:
    """Load environment variables from .env file"""
    if not os.path.exists(env_path):
        return

    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value


def analyze_docket_entry(
    doc_number: str,
    full_text: str,
    metadata: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Analyze a docket entry by document number and full text.

    Args:
        doc_number: The document ID/number (e.g., "202510-224401-01")
        full_text: The full text content of the document
        metadata: Optional metadata dict with keys: date, document_type, 
                 additional_info, on_behalf_of, docket_number

    Returns:
        Dictionary containing analysis results with tier1, tier2 and tier3 responses
    """

    _load_env_file(ENV_FILE)

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        return {
            "error": "MongoDB connection string not found in .env",
            "doc_number": doc_number
        }

    if metadata is None:
        metadata = {}

    # Get docket_type from metadata for filtering
    docket_type = metadata.get("docket_type", "N/A")

    try:
        mongo_client = MongoClient(mongodb_uri)
        db = mongo_client.get_database()
        collection = db["docket"]

        existing_entry = collection.find_one(
            {"metadata.document_id": doc_number})

        if existing_entry:
            existing_entry.pop("_id", None)
            return {
                "doc_number": doc_number,
                "status": "existing",
                "entry": existing_entry
            }

        # Filter entries by docket_type if provided
        if docket_type and docket_type != "N/A":
            query_filter = {"metadata.docket_type": docket_type}
        else:
            query_filter = {}

        # Sort by created_at timestamp for chronological order within docket_type
        all_entries = list(collection.find(
            query_filter).sort("created_at", 1))
        for entry in all_entries:
            entry.pop("_id", None)

    except Exception as e:
        return {
            "error": f"MongoDB connection error: {str(e)}",
            "doc_number": doc_number
        }

    api_key = os.environ.get(
        "CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "error": "API key not found",
            "doc_number": doc_number
        }

    client = anthropic.Anthropic(api_key=api_key)

    # Build historical context from filtered entries with sequential numbering
    historical_context = _build_historical_context(all_entries)

    # Next entry number is simply the count of filtered entries + 1
    next_entry_number = len(all_entries) + 1

    entry_metadata = {
        "date": metadata.get("date", "N/A"),
        "document_type": metadata.get("document_type", "N/A"),
        "additional_info": metadata.get("additional_info", "N/A"),
        "on_behalf_of": metadata.get("on_behalf_of", "N/A"),
        "docket_number": metadata.get("docket_number", "N/A"),
        "document_id": doc_number,
        "docket_type": docket_type
    }

    tier2_prompt = f"""You are a legal analyst specializing in M&A regulatory proceedings.

COMPLETE DOCKET HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEW ENTRY #{next_entry_number} TO ANALYZE:
Document ID: {doc_number}

CONTENT:
{full_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Based on the COMPLETE docket history above and this new entry, provide:

1. ENTRY SUMMARY (2-3 sentences): What is this entry and what does it contain?

2. LEGAL/REGULATORY SIGNIFICANCE (3-4 sentences): 
   - What legal or procedural issues does this raise?
   - How does it relate to previous entries? (cite specific entry numbers)
   - What stakeholder positions are emerging or evolving?

3. CUMULATIVE IMPACT (3-4 sentences):
   Given EVERYTHING that has happened from Entry #1 through #{next_entry_number}, how does 
   this entry change the overall picture? Does it strengthen/weaken the deal's position? 
   Does it introduce new themes or continue existing patterns?

Be specific and cite entry numbers when referencing prior events."""

    tier2_message = client.messages.create(
        model=TIER2_MODEL,
        max_tokens=1000,
        temperature=0.3,
        messages=[{"role": "user", "content": tier2_prompt}]
    )

    print(f"Tier 2 prompt: {tier2_prompt}")

    tier2_response = tier2_message.content[0].text
    tier2_input_tokens = tier2_message.usage.input_tokens
    tier2_output_tokens = tier2_message.usage.output_tokens
    tier2_cost = _estimate_cost(
        tier2_input_tokens, tier2_output_tokens, TIER2_MODEL)

    tier3_prompt = f"""You are a senior legal analyst providing risk assessment for an M&A transaction regulatory review.

CASE: ALLETE acquisition by Canada Pension Plan Investment Board and Global Infrastructure Partners

COMPLETE DOCKET HISTORY (Entries 1-{next_entry_number}):
{historical_context}

MOST RECENT ENTRY (#{next_entry_number}):
{tier2_response}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Based on ALL evidence from Entry #1 through #{next_entry_number}, provide comprehensive risk assessment:

1. DEAL CHALLENGE RISK - Rate as LOW, MODERATE, or HIGH:

Rating criteria:
• LOW: Limited opposition, mostly procedural concerns, deal structure sound
• MODERATE: Multiple substantive intervenors, significant concerns but deal viable with conditions
• HIGH: Widespread strong opposition, fundamental public interest concerns, approval unlikely

Your rating: [LOW/MODERATE/HIGH]
Reasoning (4-5 sentences): [Cite specific entries by number to support your assessment]

2. TIMING RISK - Rate as LOW, MODERATE, or HIGH:

Rating criteria:
• LOW: Standard review timeline, few intervenors, proceeding smoothly
• MODERATE: Contested case, multiple intervenors, 6-12 month timeline
• HIGH: Highly contested, procedural disputes, likely 12+ month delay

Your rating: [LOW/MODERATE/HIGH]
Reasoning (4-5 sentences): [Cite specific entries by number to support your assessment]

3. KEY RISK FACTORS (list 3-5 most significant concerns that have emerged)

4. TRAJECTORY ASSESSMENT (3-4 sentences): 
   Looking at the arc from Entry #1 to #{next_entry_number}, is the deal strengthening or 
   weakening? What have been the key inflection points? (cite entry numbers)

Be decisive. Ground all assessments in specific entries from the docket history."""

    tier3_message = client.messages.create(
        model=TIER3_MODEL,
        max_tokens=2000,
        temperature=0.2,
        messages=[{"role": "user", "content": tier3_prompt}]
    )
    print(f"Tier 3 prompt: {tier3_prompt}")

    tier3_response = tier3_message.content[0].text
    tier3_input_tokens = tier3_message.usage.input_tokens
    tier3_output_tokens = tier3_message.usage.output_tokens
    tier3_cost = _estimate_cost(
        tier3_input_tokens, tier3_output_tokens, TIER3_MODEL)

    content = full_text
    max_content_length = 100000
    if len(content) > max_content_length:
        content = (
            content[:80000] +
            f"\n\n[TRUNCATED - {len(content):,} total chars]\n\n" +
            content[-20000:]
        )

    tier1_prompt = f"""You are extracting key facts from a legal docket entry. Be concise and factual.

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

CONTENT:
{content}

Extract the key facts in 3-5 bullet points (max 500 words total):
- What type of filing is this?
- Who filed it and what do they want?
- What are the main arguments/concerns raised?
- Any commitments, recommendations, or conclusions?

Be factual and concise. Focus on substantive content, not procedural details."""

    tier1_message = client.messages.create(
        model=TIER1_MODEL,
        max_tokens=1000,
        temperature=0.1,
        messages=[{"role": "user", "content": tier1_prompt}]
    )

    tier1_summary = tier1_message.content[0].text.strip()
    tier1_input_tokens = tier1_message.usage.input_tokens
    tier1_output_tokens = tier1_message.usage.output_tokens
    tier1_cost = _estimate_cost(
        tier1_input_tokens, tier1_output_tokens, TIER1_MODEL)

    total_cost = tier1_cost + tier2_cost + tier3_cost

    new_entry = {
        "metadata": entry_metadata,
        "summary": tier1_summary,
        "original_content_length": len(full_text),
        "summary_length": len(tier1_summary),
        "tokens": {
            "input": tier1_input_tokens,
            "output": tier1_output_tokens,
            "summary_estimated": len(tier1_summary) // 4
        },
        "cost": tier1_cost,
        "tier2_analysis": {
            "response": tier2_response,
            "tokens": {
                "input": tier2_input_tokens,
                "output": tier2_output_tokens
            },
            "cost": tier2_cost
        },
        "tier3_risk_assessment": {
            "response": tier3_response,
            "tokens": {
                "input": tier3_input_tokens,
                "output": tier3_output_tokens
            },
            "cost": tier3_cost
        },
        "total_analysis_cost": total_cost,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }

    collection.insert_one(new_entry)

    result = {
        "doc_number": doc_number,
        "status": "new_analysis",
        "metadata": entry_metadata,
        "tier1_summary": {
            "summary": tier1_summary,
            "tokens": {
                "input": tier1_input_tokens,
                "output": tier1_output_tokens
            },
            "cost": tier1_cost
        },
        "tier2_analysis": {
            "response": tier2_response,
            "tokens": {
                "input": tier2_input_tokens,
                "output": tier2_output_tokens
            },
            "cost": tier2_cost
        },
        "tier3_risk_assessment": {
            "response": tier3_response,
            "tokens": {
                "input": tier3_input_tokens,
                "output": tier3_output_tokens
            },
            "cost": tier3_cost
        },
        "total_cost": total_cost,
        "timestamp": datetime.now().isoformat(),
        "database_updated": True
    }

    return result


def _build_historical_context(entries: list) -> str:
    """Build historical context string from filtered entries using sequential numbering"""
    if not entries:
        return "No prior entries."

    context_parts = []
    # Use enumerate to assign sequential numbers starting from 1
    for idx, entry in enumerate(entries, start=1):
        metadata = entry.get("metadata", {})
        date = metadata.get("date", "N/A")
        doc_type = metadata.get("document_type", "N/A")
        summary = entry.get("summary", "")

        context_parts.append(
            f"Entry #{idx} ({date}) - {doc_type}:\n{summary}"
        )

    return "\n\n".join(context_parts)


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate API cost based on token usage"""
    pricing = {
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3-5-haiku-20241022": {"input": 1.0, "output": 5.0},
        "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    }

    if model not in pricing:
        return 0.0

    input_cost = (input_tokens / 1_000_000) * pricing[model]["input"]
    output_cost = (output_tokens / 1_000_000) * pricing[model]["output"]

    return input_cost + output_cost


if __name__ == "__main__":
    import sys

    doc_num = "202510-224026-01"
    text = "From: Thom, Anne (PUC)\nTo: Staff, CAO (PUC)\nSubject: FW: Oppose the Proposed Takeover of Minnesota Utility by BlackRock (Global Infrastructure Partners)\nDate: Wednesday, October 15, 2025 10:04:02 AM\nAttachments: image001.png\n \n \nAnne Thom\nSupervisor | Consumer Affairs Office\nMinnesota Public Utilities Commission\n121 7th Place E, Suite 350 Saint Paul, MN 55101-2147O: 651-355-0000F: 651-297-7073mn.gov/puc\nDISCLAIMER: The Consumer Affairs Office works to resolve consumer complaints informally. This\nemail does not constitute legal advice or formal determination by the Minnesota Public UtilitiesCommission. CONFIDENTIALITY NOTICE:  This message is only for the use of the individual(s) named above.\nInformation in this email or any attachment may be confidential or may be protected by state orfederal law. Any unauthorized disclosure, use, dissemination, or copying of this message isprohibited. If you are not the intended recipient, do not read this email or any attachments and notifythe sender immediately. Please delete all copies of this communication.\n \nFrom: Sieben, Katie (PUC) <katie.sieben@state.mn.us> \nSent:  Wednesday, October 15, 2025 10:03 AM\nTo: Thom, Anne (PUC) <anne.thom@state.mn.us>\nSubject: FW: Oppose the Proposed Takeover of Minnesota Utility by BlackRock (Global\nInfrastructure Partners)\n \nI just saw this email.\n \nFrom: costume_funky9u@icloud.com  <costume_funky9u@icloud.com > \nSent:  Friday, September 26, 2025 7:34 PM\nTo: Sieben, Katie (PUC) < katie.sieben@state.mn.us >\nSubject: Oppose the Proposed Takeover of Minnesota Utility by BlackRock (Global Infrastructure\nPartners)\nThis message may be from an external email source.\nDo not select links or open attachments unless verified. Report all suspicious emails to Minnesota IT Services Security\nOperations Center.You don't often get email from costume_funky9u@icloud.com . Learn why this is important \n \nDear Chair Sieben and Commissioners,\nI am writing as a concerned resident to express my strong opposition to the proposed\nacquisition of the parent company of a Duluth-based electric utility by Global InfrastructurePartners, a division of BlackRock. This utility currently provides electricity to over 150,000Minnesotans, and the decision regarding its future should prioritize the public good overprivate profit.\nAllowing a Wall Street private equity firm to take over a vital public utility raises serious red\nflags. Private equity firms like BlackRock are notorious for putting profits for executives andshareholders ahead of the needs of communities. Handing control of a critical energy providerto a firm with no direct accountability to Minnesota residents risks increased rates, reducedtransparency, and service decisions that prioritize investor returns over reliable and affordableaccess to power.\nAs Jenna Yeakle of the Sierra Club rightly stated, “If the deal goes through, it will force\nratepayers to be beholden to the private equity agenda.” This is unacceptable. Our utilitiesshould be accountable to the public, not to corporate investors with no stake in ourcommunities.\nI urge the Public Utilities Commission to reject this proposal and ensure that Minnesota’s\nenergy future is controlled by those who have the public interest, environmental responsibility,and long-term affordability at heart—not by distant private equity firms.\nThank you for your time and attention to this urgent matter.Sincerely,Kristy M."

    result = analyze_docket_entry(doc_num, text)
    print(json.dumps(result, indent=2))
