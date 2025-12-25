#!/usr/bin/env python3
"""
Docket Entry Analyzer
=====================
Analyzes docket entries with tier 1, tier 2, and tier 3 summaries.
If entry already exists in database, skips it and returns skip status.
Otherwise generates new analysis using all historical summaries and adds to database.
Only returns new entries that are analyzed and added to the database.
"""

import json
import os
import tempfile
import time
from typing import Dict, Any, Optional
from datetime import datetime
import anthropic
from openai import OpenAI
from pymongo import MongoClient

ENV_FILE = ".env"
COMPREHENSIVE_SUMMARY_MODEL = "gpt-5-mini-2025-08-07"
# Model for Assistants API (must support file_search)
ASSISTANTS_API_MODEL = "gpt-4o-mini"
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


def convert_date_to_datetime(date_str: str) -> Optional[datetime]:
    """
    Convert date string to datetime object.

    Args:
        date_str: Date string in MM/DD/YYYY format

    Returns:
        datetime object, or None if conversion fails
    """
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y")
    except Exception:
        return None


def _generate_comprehensive_summary_with_file_upload(
    openai_client: OpenAI,
    full_text: str,
    entry_metadata: Dict[str, str],
    estimated_tokens: int,
    next_entry_number: int
) -> Dict[str, Any]:
    """
    Generate comprehensive summary by uploading file to OpenAI.
    Use this when content is too large for direct API calls.

    Args:
        openai_client: OpenAI client instance
        full_text: Full document text
        entry_metadata: Document metadata
        estimated_tokens: Estimated token count
        next_entry_number: The entry number for this document

    Returns:
        Dictionary with summary, tokens, and cost information
    """
    print("Using file upload approach for comprehensive summary...")

    # Create a temporary text file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp_file:
        tmp_file.write(full_text)
        tmp_file_path = tmp_file.name

    try:
        # Upload file to OpenAI
        print(f"Uploading file to OpenAI ({len(full_text):,} characters)...")
        with open(tmp_file_path, 'rb') as file:
            uploaded_file = openai_client.files.create(
                file=file,
                purpose='assistants'
            )

        file_id = uploaded_file.id
        print(f"✓ File uploaded with ID: {file_id}")

        # Create an assistant
        print("Creating assistant...")
        assistant = openai_client.beta.assistants.create(
            name="Document Summarizer",
            instructions="""You are a legal document summarizer. Create comprehensive summaries that preserve all important details for further analysis.""",
            model=ASSISTANTS_API_MODEL,
            tools=[{"type": "file_search"}]
        )

        print(f"✓ Assistant created with ID: {assistant.id}")

        # Create a thread with the file
        print("Creating thread with file...")
        thread = openai_client.beta.threads.create(
            messages=[
                {
                    "role": "user",
                    "content": f"""You are summarizing a legal docket entry for further analysis. Create a comprehensive summary that preserves all important details.

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

The full document content is in the attached file. Please read it and create a COMPREHENSIVE SUMMARY that includes:

1. Document type and purpose
2. All parties involved and their positions
3. All key arguments, claims, and concerns raised
4. Any evidence, data, or exhibits referenced
5. Procedural requests or recommendations
6. Any commitments, conditions, or proposed remedies
7. Legal citations or regulatory references
8. Timeline information or deadlines mentioned

Be thorough and detailed. Preserve specific facts, numbers, names, and legal arguments. 
This summary must contain enough detail for downstream analysis of legal significance and risk assessment.

Target length: 1000-2000 words depending on complexity.""",
                    "attachments": [
                        {
                            "file_id": file_id,
                            "tools": [{"type": "file_search"}]
                        }
                    ]
                }
            ]
        )

        print(f"✓ Thread created with ID: {thread.id}")

        # Run the assistant
        print("Running assistant...")
        run = openai_client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=assistant.id
        )

        # Wait for completion
        max_wait_time = 300  # 5 minutes max
        start_time = time.time()

        while run.status in ['queued', 'in_progress']:
            if time.time() - start_time > max_wait_time:
                raise TimeoutError("Assistant run exceeded maximum wait time")

            time.sleep(2)
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )
            print(f"  Status: {run.status}")

        if run.status != 'completed':
            raise Exception(f"Assistant run failed with status: {run.status}")

        print(f"✓ Assistant run completed")

        # Get the messages
        messages = openai_client.beta.threads.messages.list(
            thread_id=thread.id
        )

        # Extract the assistant's response
        assistant_message = None
        for message in messages.data:
            if message.role == 'assistant':
                assistant_message = message
                break

        if not assistant_message or not assistant_message.content:
            raise Exception("No response from assistant")

        comprehensive_summary_text = assistant_message.content[0].text.value

        # Estimate token usage (since Assistants API doesn't provide exact counts)
        comprehensive_summary_input_tokens = estimated_tokens + \
            500  # Add buffer for instructions
        comprehensive_summary_output_tokens = len(
            comprehensive_summary_text) // 4
        comprehensive_summary_cost = _estimate_cost(
            comprehensive_summary_input_tokens,
            comprehensive_summary_output_tokens,
            ASSISTANTS_API_MODEL
        )

        print(
            f"✓ Generated comprehensive summary: {len(comprehensive_summary_text):,} characters")

        # Clean up
        try:
            openai_client.files.delete(file_id)
            print(f"✓ Cleaned up uploaded file")
        except Exception as e:
            print(f"Warning: Could not delete uploaded file: {str(e)}")

        try:
            openai_client.beta.assistants.delete(assistant.id)
            print(f"✓ Cleaned up assistant")
        except Exception as e:
            print(f"Warning: Could not delete assistant: {str(e)}")

        return {
            "summary": comprehensive_summary_text,
            "tokens": {
                "input": comprehensive_summary_input_tokens,
                "output": comprehensive_summary_output_tokens,
                "estimated_original": estimated_tokens
            },
            "cost": comprehensive_summary_cost,
            "generated": True,
            "method": "file_upload",
            "reason": f"Content too large ({estimated_tokens:,} tokens)"
        }

    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_file_path)
        except Exception as e:
            print(f"Warning: Could not delete temp file: {str(e)}")


def analyze_docket_entry(
    doc_number: str,
    full_text: str,
    metadata: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Analyze a docket entry by document number and full text.

    Args:
        doc_number: The document ID/number (e.g., "202510-224401-01" or "URL)
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

    # Get docket_type and docket_number from metadata for filtering
    docket_type = metadata.get("docket_type", "N/A")
    docket_number = metadata.get("docket_number", "N/A")
    date = metadata.get("date", "N/A")
    on_behalf_of = metadata.get("on_behalf_of", "N/A")

    try:
        mongo_client = MongoClient(mongodb_uri)
        db = mongo_client.get_database()
        collection = db["docket"]

        existing_entry = collection.find_one(
            {"metadata.document_id": doc_number})

        if existing_entry:
            existing_entry.pop("_id", None)
            # Entry already exists, skip it and don't return it
            return {
                "doc_number": doc_number,
                "status": "skipped",
                "message": "Entry already exists in database",
                "metadata": existing_entry.get("metadata", {}),
                "tier2_analysis": existing_entry.get("tier2_analysis", {}),
                "tier3_risk_assessment": existing_entry.get("tier3_risk_assessment", {}),
            }

        # Filter entries by docket_type and docket_number if provided
        query_filter = {}
        if docket_type and docket_type != "N/A":
            query_filter["metadata.docket_type"] = docket_type
        if docket_number and docket_number != "N/A":
            query_filter["metadata.docket_number"] = docket_number

        # Sort by metadata.date for chronological order within docket_type (older to newer)
        all_entries = list(collection.find(
            query_filter).sort("metadata.date", 1))
        for entry in all_entries:
            entry.pop("_id", None)

        print(f"All entries: length {len(all_entries)}")

        # Calculate next hash_id: filter by docket_type only, sort by date
        # Use the same query_filter as all_entries (filters by docket_type)
        hash_id_entries = list(collection.find(
            query_filter).sort("metadata.date", 1))

        # Calculate next hash_id
        if hash_id_entries:
            # Get the maximum hash_id from existing entries
            max_hash_id = 0
            for entry in hash_id_entries:
                if "hash_id" in entry and isinstance(entry["hash_id"], int):
                    max_hash_id = max(max_hash_id, entry["hash_id"])
            next_hash_id = max_hash_id + 1
        else:
            # No existing entries, start from 1
            next_hash_id = 1

    except Exception as e:
        return {
            "error": f"MongoDB connection error: {str(e)}",
            "doc_number": doc_number
        }

    api_key = os.environ.get(
        "CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "error": "Anthropic API key not found",
            "doc_number": doc_number
        }

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        return {
            "error": "OpenAI API key not found",
            "doc_number": doc_number
        }

    client = anthropic.Anthropic(api_key=api_key)
    openai_client = OpenAI(api_key=openai_api_key)

    # Build historical context from filtered entries with sequential numbering
    historical_context = _build_historical_context(all_entries)

    # Next entry number is simply the count of filtered entries + 1
    next_entry_number = len(all_entries) + 1

    print(f"Next entry number: {next_entry_number}")

    # Convert date to datetime object if it exists and is a string
    date_value = metadata.get("date", "N/A")
    if date_value != "N/A" and isinstance(date_value, str):
        dt = convert_date_to_datetime(date_value)
        if dt:
            date_value = dt
        # If conversion fails, keep the original string value

    entry_metadata = {
        "date": date_value,
        "document_type": metadata.get("document_type", "N/A"),
        "additional_info": metadata.get("additional_info", "N/A"),
        "on_behalf_of": metadata.get("on_behalf_of", "N/A"),
        "docket_number": metadata.get("docket_number", "N/A"),
        "document_id": doc_number,
        "docket_type": docket_type
    }

    # Estimate token count (rough estimate: 1 token ≈ 4 characters)
    estimated_tokens = len(full_text) // 4
    print(f"Estimated tokens: {estimated_tokens}")

    comprehensive_summary_data = None
    content_for_tier2 = full_text

    # Try to generate Tier2 directly with full_text first
    tier2_prompt = f"""You are a legal analyst specializing in M&A regulatory proceedings.

    Always prioritize the filing's concrete legal or procedural function over its rhetorical tone. When possible, use the filer’s own language to describe what they are asking the agency or other parties to do, and avoid vague phrasing such as "raises concerns" or "highlights issues" when you can state the specific request, effect, or role of the filing in the proceeding.

COMPLETE DOCKET HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEW ENTRY #{next_entry_number} TO ANALYZE:
Document ID: {doc_number}
Date: {date}
Type: {docket_type}
Filed By: {on_behalf_of}

CONTENT:
{content_for_tier2}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Based on the COMPLETE docket history above and this new entry, provide:

1. ENTRY SUMMARY (2-3 sentences):
   Describe this filing in terms of its regulatory or procedural function. Identify:
   (i) who filed it,
   (ii) what specific regulatory, procedural, or substantive action they are requesting (if any),
   and (iii) the main issues they are asking the agency or other decision-maker to evaluate.
   If the filer is not requesting a concrete action, explicitly state that they are urging
   consideration or re-weighting of certain factors rather than demanding a specific outcome.

2. LEGAL/REGULATORY SIGNIFICANCE (3-4 sentences): 
   Explain how this filing affects the legal and procedural posture of the case, if at all.
   Be explicit about whether it:
   • changes the scope of review, evidentiary burden, available remedies, schedule,
     or procedural rights; or
   • is primarily non-binding advocacy or commentary without direct procedural effect.
   Distinguish clearly between binding procedural or legal consequences (e.g., motions,
   orders, schedule changes, formal commitments) and persuasive advocacy (e.g., public
   comments, letters of support or opposition). Describe how this filing escalates,
   narrows, reinforces, or contradicts the themes and positions in specific prior entries
   (cite entry numbers).

3. CUMULATIVE IMPACT (3-4 sentences):
   
   Considering EVERYTHING that has happened from Entry #1 through #{next_entry_number},
   assess how this filing changes the overall risk profile and deal dynamics.
   Does it:
   • increase or decrease the probability of a formal challenge, remedies/conditions,
     or delays; or
   • mainly add weight to existing themes already present in prior entries?
   Be explicit about whether this filing introduces a new risk vector or simply reinforces
   existing ones, and state whether it tends to strengthen or weaken the deal’s position.
   Cite specific entry numbers when making comparisons or describing patterns.

Be specific and cite entry numbers when referencing prior events."""

    try:
        print("Attempting to generate tier2 analysis directly...")
        tier2_message = client.messages.create(
            model=TIER2_MODEL,
            max_tokens=1000,
            temperature=0.3,
            messages=[{"role": "user", "content": tier2_prompt}]
        )

        print(f"✓ Tier2 analysis generated directly")

        tier2_response = tier2_message.content[0].text
        tier2_input_tokens = tier2_message.usage.input_tokens
        tier2_output_tokens = tier2_message.usage.output_tokens
        tier2_cost = _estimate_cost(
            tier2_input_tokens, tier2_output_tokens, TIER2_MODEL)

    except Exception as tier2_error:
        print(f"⚠ Direct tier2 generation failed: {str(tier2_error)}")
        print("Falling back to comprehensive summary approach...")

        # FALLBACK: Generate comprehensive summary first
        comprehensive_summary_prompt = f"""You are summarizing a legal docket entry for further analysis. Create a comprehensive summary that preserves all important details.

ENTRY METADATA:
Entry Number: {next_entry_number}
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}

FULL DOCUMENT CONTENT:
{full_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a COMPREHENSIVE SUMMARY that will be used for further legal analysis. Include:

1. Document type and purpose
2. All parties involved and their positions
3. All key arguments, claims, and concerns raised
4. Any evidence, data, or exhibits referenced
5. Procedural requests or recommendations
6. Any commitments, conditions, or proposed remedies
7. Legal citations or regulatory references
8. Timeline information or deadlines mentioned

Be thorough and detailed. Preserve specific facts, numbers, names, and legal arguments. 
This summary must contain enough detail for downstream analysis of legal significance and risk assessment.

Target length: 1000-2000 words depending on complexity."""

        try:
            # Always try direct comprehensive summary first
            print("Generating comprehensive summary...")
            try:
                comprehensive_summary_message = openai_client.chat.completions.create(
                    model=COMPREHENSIVE_SUMMARY_MODEL,
                    messages=[
                        {"role": "user", "content": comprehensive_summary_prompt}
                    ]
                )

                comprehensive_summary_text = comprehensive_summary_message.choices[0].message.content.strip(
                )
                comprehensive_summary_input_tokens = comprehensive_summary_message.usage.prompt_tokens
                comprehensive_summary_output_tokens = comprehensive_summary_message.usage.completion_tokens
                comprehensive_summary_cost = _estimate_cost(
                    comprehensive_summary_input_tokens,
                    comprehensive_summary_output_tokens,
                    COMPREHENSIVE_SUMMARY_MODEL
                )

                comprehensive_summary_data = {
                    "summary": comprehensive_summary_text,
                    "tokens": {
                        "input": comprehensive_summary_input_tokens,
                        "output": comprehensive_summary_output_tokens,
                        "estimated_original": estimated_tokens
                    },
                    "cost": comprehensive_summary_cost,
                    "generated": True,
                    "method": "direct",
                    "reason": f"Fallback: Direct tier2 generation failed with error: {str(tier2_error)}"
                }

                print(
                    f"✓ Generated comprehensive summary: {estimated_tokens:,} tokens → {len(comprehensive_summary_text)} chars")

            except Exception as direct_error:
                error_str = str(direct_error)
                print("err", error_str)
                # Check if it's a token limit error
                if "context_length_exceeded" in error_str or "tokens exceed" in error_str.lower() or "string too long" in error_str.lower():
                    print(
                        f"⚠ Direct API call failed due to token limit: {error_str}")
                    print("Switching to file upload approach...")

                    comprehensive_summary_data = _generate_comprehensive_summary_with_file_upload(
                        openai_client=openai_client,
                        full_text=full_text,
                        entry_metadata=entry_metadata,
                        estimated_tokens=estimated_tokens,
                        next_entry_number=next_entry_number
                    )
                    comprehensive_summary_data["reason"] = f"Fallback + File Upload: Token limit exceeded in direct call"
                    comprehensive_summary_text = comprehensive_summary_data["summary"]

                    print(
                        f"✓ Generated comprehensive summary: {estimated_tokens:,} tokens → {len(comprehensive_summary_text)} chars")
                else:
                    # If it's not a token error, re-raise
                    raise

            # Now retry tier2 with comprehensive summary
            content_for_tier2 = comprehensive_summary_text
            tier2_prompt_fallback = f"""You are a legal analyst specializing in M&A regulatory proceedings.

COMPLETE DOCKET HISTORY (Entries 1-{len(all_entries)}):
{historical_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEW ENTRY #{next_entry_number} TO ANALYZE:
Document ID: {doc_number}

CONTENT:
{content_for_tier2}

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

            print("Generating tier2 analysis from comprehensive summary...")
            tier2_message = client.messages.create(
                model=TIER2_MODEL,
                max_tokens=1000,
                temperature=0.3,
                messages=[{"role": "user", "content": tier2_prompt_fallback}]
            )

            print(f"✓ Tier2 analysis generated from comprehensive summary")

            tier2_response = tier2_message.content[0].text
            tier2_input_tokens = tier2_message.usage.input_tokens
            tier2_output_tokens = tier2_message.usage.output_tokens
            tier2_cost = _estimate_cost(
                tier2_input_tokens, tier2_output_tokens, TIER2_MODEL)

        except Exception as e:
            print(f"Error in fallback generation: {str(e)}")
            return {
                "error": f"Both direct and fallback tier2 generation failed. Direct error: {str(tier2_error)}, Fallback error: {str(e)}",
                "doc_number": doc_number,
                "metadata": entry_metadata
            }

    tier3_prompt = f"""You are a senior legal analyst providing risk assessment for an M&A transaction regulatory review.

    Always prioritize procedural and legal consequences over rhetorical intensity or the mere volume of comments when assessing risk. Focus on filings and orders that actually change the regulatory posture, timing, or available remedies.


    COMPLETE DOCKET HISTORY (Entries 1-{next_entry_number}):
    {historical_context}

    MOST RECENT ENTRY (#{next_entry_number}):
    {tier2_response}

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Based on ALL evidence from Entry #1 through #{next_entry_number}, provide comprehensive risk assessment:

    1. DEAL CHALLENGE RISK SCORE (0-100):

    Score: [X]

    Where:
    • 0-30: Limited opposition, mostly procedural concerns, deal structure sound
    • 31-60: Multiple substantive intervenors, significant concerns but deal viable with conditions
    • 61-100: Widespread strong opposition, fundamental public interest concerns, approval unlikely

    Reasoning (4-5 sentences):
    Ground this score in filings and actions that create concrete legal or procedural exposure,
    such as complaints, enforcement activity, adverse staff recommendations, motions directed
    at blocking or conditioning the deal, formal opposition from enforcement agencies or
    state regulators, or clear signals of potential litigation. Do not inflate the score based
    solely on the volume or emotional intensity of public comments or political rhetoric unless
    they have already produced identifiable procedural consequences (e.g., expanded discovery,
    new hearings, schedule changes). Cite specific entries by number to support your score.

    2. TIMING RISK SCORE (0-100):

    Score: [X]

    Where:
    • 0-30: Standard review timeline, few intervenors, proceeding smoothly
    • 31-60: Contested case, multiple intervenors, 6-12 month timeline
    • 61-100: Highly contested, procedural disputes, likely 12+ month delay

    Reasoning (4-5 sentences):
    Ground this score in events that directly affect timing, such as schedule changes,
    extensions of statutory deadlines, motions for more time, expanded discovery,
    additional hearing days, or procedural complications that make timely resolution
    unlikely. Do not infer high timing risk solely from controversy or public interest;
    tie it to actual orders, motions, or procedural bottlenecks in the docket.
    Cite specific entries by number to support your score.

    3. KEY RISK FACTORS 
    List the 3-5 most significant risk factors that have emerged, focusing on those that
    realistically affect (i) probability of deal challenge or litigation, (ii) likelihood or
    severity of remedies/conditions, and (iii) timing of closing.

    4. TRAJECTORY ASSESSMENT (3-4 sentences): 
    Looking at the arc from Entry #1 to #{next_entry_number}, explain whether the deal is
        strengthening or weakening from a regulatory risk perspective. Identify the key
        inflection points where the posture meaningfully changed (e.g., major enforcement
        filings, significant political interventions with procedural consequences, schedule
        changes, or major commitments by the parties), and cite those entries by number.

CRITICAL: You must provide numerical scores (0-100) for both risks. Be decisive and ground all assessments in specific entries from the docket history."""

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

    content = content_for_tier2
    # max_content_length = 100000
    # if len(content) > max_content_length:
    #     content = (
    #         content[:80000] +
    #         f"\n\n[TRUNCATED - {len(content):,} total chars]\n\n" +
    #         content[-20000:]
    #     )

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

    # Calculate total cost including comprehensive summary if generated
    total_cost = tier1_cost + tier2_cost + tier3_cost
    if comprehensive_summary_data:
        total_cost += comprehensive_summary_data["cost"]

    new_entry = {
        "hash_id": next_hash_id,
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

    # Add comprehensive summary field if generated
    if comprehensive_summary_data:
        new_entry["comprehensive_summary"] = comprehensive_summary_data

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
        "comprehensive_summary": comprehensive_summary_data,
        "timestamp": datetime.now().isoformat(),
        "database_updated": True
    }

    # Add comprehensive summary to result if generated
    if comprehensive_summary_data:
        result["comprehensive_summary"] = comprehensive_summary_data

    return result


def _build_historical_context(entries: list) -> str:
    """Build historical context string from filtered entries using hash_id"""
    if not entries:
        return "No prior entries."

    context_parts = []
    # Use hash_id from entry, fallback to index if hash_id doesn't exist
    for idx, entry in enumerate(entries, start=1):
        # Use hash_id if available, otherwise use index
        hash_id = entry.get("hash_id", idx)
        metadata = entry.get("metadata", {})
        date = metadata.get("date", "N/A")
        doc_type = metadata.get("document_type", "N/A")
        summary = entry.get("summary", "")

        context_parts.append(
            f"Entry #{hash_id} ({date}) - {doc_type}:\n{summary}"
        )

    return "\n\n".join(context_parts)


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate API cost based on token usage"""
    pricing = {
        # Anthropic pricing (per 1M tokens)
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3-5-haiku-20241022": {"input": 0.8, "output": 4.0},
        "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
        # OpenAI pricing (per 1M tokens)
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.150, "output": 0.600},
        "gpt-4-turbo": {"input": 10.00, "output": 30.00},
        "gpt-5-mini-2025-08-07": {"input": 0.25, "output": 2},

    }

    if model not in pricing:
        return 0.0

    input_cost = (input_tokens / 1_000_000) * pricing[model]["input"]
    output_cost = (output_tokens / 1_000_000) * pricing[model]["output"]

    return input_cost + output_cost


if __name__ == "__main__":
    import sys

    doc_num = "202510-224026-01"
    text = "Surface Transportation BoardWashington, D.C. 20423-0001Office of EconomicsDecember 10, 2025Re: Waybill Request WB25-55I have approved the addition of the following individual to the waybill access letter inWB25-55.a) Kim Hillenbrand - Berkley Research GroupPrior payment of processing and mailing cost of $72 ($72 per signature) is required andreceived.Sincerely,Francis X. O’ConnorActing DirectorOffice of EconomicsFRANCISO'CONNORDigitally signed byFRANCIS O'CONNORDate: 2025.12.1107:57:06 -05'00'y FD 36873 310490 ENTEREDOffice of Chief Counsel December 11, 2025 Part of Public Record Waybill Agreement WB25-55I have read and understand the conditions for release of the CCWS data. I agree tocomply fully with these conditions and the provisions of this confidentiality agreement. No laterthan thirty days before the agreement expires, I will request an extension of this agreement, ifnecessary. If no extension is requested, I will return or destroy all CCWS data and certify that Ihave done so. I have the authority to sign this agreement._______________"
    # metadata ={
    #     "docket_type": "PUC",
    #     "date": "2025-10-15",
    #     "document_type": "Public Comment",
    #     "additional_info": "Kristy M.",
    #     "on_behalf_of": "PUC",
    #     "docket_number": "24-198 (PA)",
    #     "document_id": "202510-224026-01"
    # }

    metadata = {
        "date": "2025-12-11T00:00:00.000Z",
        "document_type": "Filing",
        "additional_info": "UNION PACIFIC CORPORATION AND UNION PACIFIC RAILROAD COMPANY &mdash;CONTROL&mdash; NORFOLK SOUTHERN CORPORATION AND NORFOLK SOUTHERN RAILWAY COMPANY",
        "on_behalf_of": "Surface Transportation Board",
        "docket_number": "FD-36873",
        "document_id": "https://dcms-external.s3.amazonaws.com/DCMS_External_PROD/416/310490.pdf",
        "docket_type": "stb-document"
    }

    result = analyze_docket_entry(doc_num, text, metadata)
    print(json.dumps(result, indent=2))
