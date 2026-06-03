#!/usr/bin/env python3
"""
CPUC Tier1 Analyzer
===================
Generates only tier1 summaries for CPUC documents.
For documents > 10k tokens, first creates a comprehensive summary,
then uses that to generate the tier1 summary.
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
TIER1_MODEL = "claude-haiku-4-5-20251001"


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


def _generate_comprehensive_summary_with_file_upload(
    openai_client: OpenAI,
    full_text: str,
    entry_metadata: Dict[str, str],
    estimated_tokens: int
) -> Dict[str, Any]:
    """
    Generate comprehensive summary by uploading file to OpenAI.
    Use this when content is too large for direct API calls.

    Args:
        openai_client: OpenAI client instance
        full_text: Full document text
        entry_metadata: Document metadata
        estimated_tokens: Estimated token count

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
                    "content": f"""You are summarizing a legal/regulatory document for further analysis. Create a comprehensive summary that preserves all important details.

DOCUMENT METADATA:
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}
Docket: {entry_metadata['docket_number']}

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
This summary must contain enough detail for downstream analysis.

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


def generate_tier1_summary(
    doc_number: str,
    full_text: str,
    metadata: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Generate tier1 summary for a document.

    Args:
        doc_number: The document ID/number
        full_text: The full text content of the document
        metadata: Optional metadata dict with keys: date, document_type, 
                 additional_info, on_behalf_of, docket_number, docket_type, document_id

    Returns:
        Dictionary containing tier1 summary and cost information
    """

    _load_env_file(ENV_FILE)

    if metadata is None:
        metadata = {}

    # Connect to MongoDB
    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        return {
            "error": "MongoDB connection string not found in .env",
            "doc_number": doc_number
        }

    try:
        mongo_client = MongoClient(mongodb_uri)
        db = mongo_client.get_database()
        collection = db["docket"]

        # Check if entry already exists
        existing_entry = collection.find_one(
            {"metadata.document_id": doc_number})

        if existing_entry:
            existing_entry.pop("_id", None)
            print(f"✓ Found existing entry in MongoDB for doc: {doc_number}")
            return {
                "doc_number": doc_number,
                "status": "existing",
                "entry": existing_entry
            }

    except Exception as e:
        return {
            "error": f"MongoDB connection error: {str(e)}",
            "doc_number": doc_number
        }

    # Get API keys
    api_key = os.environ.get(
        "CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "error": "Anthropic API key not found",
            "doc_number": doc_number
        }

    openai_api_key = os.environ.get("OPENAI_API_KEY_DOCKET")
    if not openai_api_key:
        return {
            "error": "OpenAI API key not found",
            "doc_number": doc_number
        }

    client = anthropic.Anthropic(api_key=api_key)
    openai_client = OpenAI(api_key=openai_api_key)

    entry_metadata = {
        "date": metadata.get("date", "N/A"),
        "document_type": metadata.get("document_type", "N/A"),
        "additional_info": metadata.get("additional_info", "N/A"),
        "on_behalf_of": metadata.get("on_behalf_of", "N/A"),
        "docket_number": metadata.get("docket_number", "N/A"),
        "document_id": doc_number,
        "docket_type": metadata.get("docket_type", "N/A")
    }

    # Estimate token count (rough estimate: 1 token ≈ 4 characters)
    estimated_tokens = len(full_text) // 4
    print(f"Estimated tokens: {estimated_tokens}")

    comprehensive_summary_data = None
    content_for_tier1 = full_text

    # Try to generate Tier1 Summary directly first
    tier1_prompt = f"""You are extracting key facts from a legal/regulatory document. Be concise and factual.

DOCUMENT METADATA:
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}
Docket: {entry_metadata['docket_number']}

CONTENT:
{content_for_tier1}

Extract the key facts in 3-5 bullet points (max 500 words total):
- What type of filing is this?
- Who filed it and what do they want?
- What are the main arguments/concerns raised?
- Any commitments, recommendations, or conclusions?

Be factual and concise. Focus on substantive content, not procedural details."""

    try:
        print("Attempting to generate tier1 summary directly...")
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

        print(f"✓ Tier1 summary generated directly")

        # Create new entry for MongoDB
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
            "total_analysis_cost": tier1_cost,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }

        # Save to MongoDB
        try:
            collection.insert_one(new_entry)
            print(f"✓ Saved entry to MongoDB")
        except Exception as e:
            print(f"Warning: Failed to save to MongoDB: {str(e)}")

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
            "total_cost": tier1_cost,
            "timestamp": datetime.now().isoformat(),
            "original_content_length": len(full_text),
            "summary_length": len(tier1_summary),
            "database_updated": True
        }

        print(f"Total cost: ${tier1_cost:.4f}")
        return result

    except Exception as tier1_error:
        print(f"⚠ Direct tier1 generation failed: {str(tier1_error)}")
        print("Falling back to comprehensive summary approach...")

        # FALLBACK: Generate comprehensive summary first
        comprehensive_summary_prompt = f"""You are summarizing a legal/regulatory document for further analysis. Create a comprehensive summary that preserves all important details.

DOCUMENT METADATA:
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}
Docket: {entry_metadata['docket_number']}

FULL DOCUMENT CONTENT:
{full_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a COMPREHENSIVE SUMMARY that will be used for further analysis. Include:

1. Document type and purpose
2. All parties involved and their positions
3. All key arguments, claims, and concerns raised
4. Any evidence, data, or exhibits referenced
5. Procedural requests or recommendations
6. Any commitments, conditions, or proposed remedies
7. Legal citations or regulatory references
8. Timeline information or deadlines mentioned

Be thorough and detailed. Preserve specific facts, numbers, names, and legal arguments. 
This summary must contain enough detail for downstream analysis.

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
                    "reason": f"Fallback: Direct tier1 generation failed with error: {str(tier1_error)}"
                }

                print(
                    f"✓ Generated comprehensive summary: {estimated_tokens:,} tokens → {len(comprehensive_summary_text)} chars")

            except Exception as direct_error:
                error_str = str(direct_error)
                # Check if it's a token limit error
                if "context_length_exceeded" in error_str or "tokens exceed" in error_str.lower():
                    print(
                        f"⚠ Direct API call failed due to token limit: {error_str}")
                    print("Switching to file upload approach...")
                    comprehensive_summary_data = _generate_comprehensive_summary_with_file_upload(
                        openai_client=openai_client,
                        full_text=full_text,
                        entry_metadata=entry_metadata,
                        estimated_tokens=estimated_tokens
                    )
                    comprehensive_summary_data["reason"] = f"Fallback + File Upload: Token limit exceeded in direct call"
                    comprehensive_summary_text = comprehensive_summary_data["summary"]

                    print(
                        f"✓ Generated comprehensive summary: {estimated_tokens:,} tokens → {len(comprehensive_summary_text)} chars")
                else:
                    # If it's not a token error, re-raise
                    raise

            # Now generate tier1 summary from comprehensive summary
            content_for_tier1 = comprehensive_summary_text
            tier1_prompt_fallback = f"""You are extracting key facts from a legal/regulatory document. Be concise and factual.

DOCUMENT METADATA:
Type: {entry_metadata['document_type']}
Date: {entry_metadata['date']}
Filed By: {entry_metadata['on_behalf_of']}
Info: {entry_metadata['additional_info']}
Docket: {entry_metadata['docket_number']}

CONTENT:
{content_for_tier1}

Extract the key facts in 3-5 bullet points (max 500 words total):
- What type of filing is this?
- Who filed it and what do they want?
- What are the main arguments/concerns raised?
- Any commitments, recommendations, or conclusions?

Be factual and concise. Focus on substantive content, not procedural details."""

            print("Generating tier1 summary from comprehensive summary...")
            tier1_message = client.messages.create(
                model=TIER1_MODEL,
                max_tokens=1000,
                temperature=0.1,
                messages=[{"role": "user", "content": tier1_prompt_fallback}]
            )

            tier1_summary = tier1_message.content[0].text.strip()
            tier1_input_tokens = tier1_message.usage.input_tokens
            tier1_output_tokens = tier1_message.usage.output_tokens
            tier1_cost = _estimate_cost(
                tier1_input_tokens, tier1_output_tokens, TIER1_MODEL)

            # Calculate total cost
            total_cost = tier1_cost + comprehensive_summary_data["cost"]

            print(f"✓ Tier1 summary generated from comprehensive summary")

            # Create new entry for MongoDB
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
                "total_analysis_cost": total_cost,
                "comprehensive_summary": comprehensive_summary_data if comprehensive_summary_data else full_text,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }

            # Save to MongoDB
            try:
                collection.insert_one(new_entry)
                print(f"✓ Saved entry to MongoDB")
            except Exception as e:
                print(f"Warning: Failed to save to MongoDB: {str(e)}")

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
                "comprehensive_summary": comprehensive_summary_data,
                "total_cost": total_cost,
                "timestamp": datetime.now().isoformat(),
                "original_content_length": len(full_text),
                "summary_length": len(tier1_summary),
                "database_updated": True
            }

            print(f"Total cost: ${total_cost:.4f}")
            return result

        except Exception as e:
            print(f"Error in fallback generation: {str(e)}")
            return {
                "error": f"Both direct and fallback tier1 generation failed. Direct error: {str(tier1_error)}, Fallback error: {str(e)}",
                "doc_number": doc_number,
                "metadata": entry_metadata
            }


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
        "gpt-5-mini-2025-08-07": {"input": 0.25, "output": 2.0},
    }

    if model not in pricing:
        return 0.0

    input_cost = (input_tokens / 1_000_000) * pricing[model]["input"]
    output_cost = (output_tokens / 1_000_000) * pricing[model]["output"]

    return input_cost + output_cost


if __name__ == "__main__":
    # Example usage
    import sys

    if len(sys.argv) > 1:
        doc_num = sys.argv[1]
        text = sys.argv[2] if len(sys.argv) > 2 else "Sample text"
    else:
        doc_num = "test-doc-001"
        text = "This is a test document for tier1 summary generation."

    result = generate_tier1_summary(doc_num, text)
    print(json.dumps(result, indent=2))
