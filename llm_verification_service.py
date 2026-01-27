"""
LLM Verification Service

A reusable service for verifying company/deal information using LLM prompts.
Can be used across different case types (EC, China, Brazil, etc.).
"""

import os
import json
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from openai import OpenAI

# Load OpenAI API Key
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def verify_country_relation(
    company_details: Any,
    country: str = "USA",
    case_type: str = "EC",
) -> Any:
    """
    Verify if companies/deals are related to a specific country using LLM.

    Args:
        company_details: Company details to verify (can be string, list, dict, etc.) - passed as-is to LLM
        country: Country to check relation to (default: "USA")
        case_type: Type of case (EC, China, Brazil, etc.) for context
        additional_context: Optional additional context to provide to LLM

    Returns:
        bool: True if companies are related to the specified country, False otherwise
    """
    # Build prompt based on case type
    if case_type.upper() == "EC":
        context_info = "European Commission merger case"

        prompt = f"""
            You are a business analyst specializing in M&A and competition law cases.

            Given the following companies from a {context_info}, determine if this deal or these companies are related to {country}.

            Company Details:
            {company_details}

            Consider the following when determining if this is related to {country}:
            - Are any of these companies headquartered in {country}?
            - Do any of these companies have significant operations, subsidiaries, or business presence in {country}?
            - Is this deal likely to have material impact on {country} markets?
            - Are any of these companies publicly traded in {country}?

            Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
            """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": f"You are an expert analyst. Respond with only 'true' or 'false' (lowercase) to indicate if companies are related to {country}."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )
            result = response.choices[0].message.content.strip().lower()

            # Parse boolean result
            if result == "true":
                return True
            elif result == "false":
                return False
            else:
                # If LLM returns something unexpected, default to False for safety
                print(
                    f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
                return False

        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            # Default to False on error to avoid false positives
            return False

    elif case_type.upper() == "UK":

        context_info = "UK CMA merger case"

        prompt = f"""
            You are a business analyst specializing in M&A and competition law cases.

            Given the following companies from a {context_info}, determine if this deal or these companies are related to {country}.

            Company Details:
            {company_details}

            Consider the following when determining if this is related to {country}:
            - Are any of these companies headquartered in {country}?
            - Do any of these companies have significant operations, subsidiaries, or business presence in {country}?
            - Is this deal likely to have material impact on {country} markets?
            - Are any of these companies publicly traded in {country}?

            Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
            """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": f"You are an expert analyst. Respond with only 'true' or 'false' (lowercase) to indicate if companies are related to {country}."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )
            result = response.choices[0].message.content.strip().lower()

            # Parse boolean result
            if result == "true":
                return True
            elif result == "false":
                return False
            else:
                # If LLM returns something unexpected, default to False for safety
                print(
                    f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
                return False

        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            # Default to False on error to avoid false positives
            return False
    elif case_type.upper() == "CHINA":
        context_info = "China SAMR merger case"

        prompt = f"""
            You are a business analyst specializing in M&A and competition law cases.

            Given the following companies from a {context_info}, determine if this deal or these companies are related to {country}.

            Company Details:
            {company_details}

            Consider the following when determining if this is related to {country}:
            - Are any of these companies headquartered in {country}?
            - Do any of these companies have significant operations, subsidiaries, or business presence in {country}?
            - Is this deal likely to have material impact on {country} markets?
            - Are any of these companies publicly traded in {country}?

            Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
            """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": f"You are an expert analyst. Respond with only 'true' or 'false' (lowercase) to indicate if companies are related to {country}."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )

            print(f"🔍 SAMR prompt: {prompt}")
            result = response.choices[0].message.content.strip().lower()

            # Parse boolean result
            if result == "true":
                return True
            elif result == "false":
                return False
            else:
                # If LLM returns something unexpected, default to False for safety
                print(
                    f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
                return False

        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            # Default to False on error to avoid false positives
            return False

    elif case_type.upper() == "CHINA-UNCONDITIONAL":
        context_info = "China-unconditional SAMR merger case"

        prompt = f"""
            You are a business analyst specializing in M&A and competition law cases.

            From the company details below, identify which company names are related to {country}.
            "Related" means the company is headquartered in {country}, has significant operations/subsidiaries in {country},
            is publicly traded in {country}, or the transaction clearly impacts {country} markets.

            Company Details:
            {company_details}

            Return ONLY a valid JSON array of company names (strings).
            - If none are related, return []
            - Do not include any explanation or extra keys
            Example: ["Apple", "Microsoft"]
            """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": f"You are an expert analyst. Return ONLY a JSON array of company names related to {country}."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=200,
            )

            print(f"🔍 SAMR prompt: {prompt}")
            content = response.choices[0].message.content.strip()

            # Strip common code-fence wrappers
            if content.startswith("```"):
                content = content.strip().strip("`")
                content = content.replace("json", "", 1).strip()

            # Extract JSON array region defensively
            start = content.find("[")
            end = content.rfind("]")
            json_str = content[start:end + 1] if start != - \
                1 and end != -1 and end > start else "[]"

            try:
                parsed = json.loads(json_str)
            except Exception as e:
                print(f"⚠️ Could not parse JSON array from LLM: {e}")
                parsed = []

            if not isinstance(parsed, list):
                print(f"⚠️ LLM returned non-list for company array, defaulting to []")
                return []

            # Normalize: keep non-empty strings, unique, preserve order
            companies: List[str] = []
            seen = set()
            for item in parsed:
                if not isinstance(item, str):
                    continue
                name = item.strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                companies.append(name)

            return companies

        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            # Default to empty list on error to avoid false positives
            return []
    elif case_type.upper() == "GERMANY":
        context_info = "Bundeskartellamt merger review (Germany)"

        prompt = f"""
You are a business analyst specializing in M&A and competition law cases.

Decide if this record is related to {country} AND the record looks NEWLY ADDED (not merely an update/extension of an older item).

Rules:
- Use the provided "today_date" to judge newness.
- If the record appears to be an older item that was updated (e.g., deadline extended, return/diploma dates, or clearly old original date), answer "false".
- Answer "true" only if BOTH:
  1) The record is USA-related ({country}) (HQ, major operations/subsidiaries, US listing, or clear US market impact)
  2) The record looks newly added relative to today_date (not a routine update)

Record details:
{company_details}

Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
""".strip()

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert analyst. Reply only 'true' or 'false'.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )

            result = response.choices[0].message.content.strip().lower()
            if result == "true":
                return True
            if result == "false":
                return False

            print(
                f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
            return False
        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            return False

    elif case_type.upper() == "BRAZIL":
        context_info = "Brazil CADE merger case"

        prompt = f"""
            You are a business analyst specializing in M&A and competition law cases.

            Given the following companies from a {context_info}, determine if this deal or these companies are related to {country}.

            Company Details:
            {company_details}

            Consider the following when determining if this is related to {country}:
            - Are any of these companies headquartered in {country}?
            - Do any of these companies have significant operations, subsidiaries, or business presence in {country}?
            - Is this deal likely to have material impact on {country} markets?
            - Are any of these companies publicly traded in {country}?

            Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
            """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": f"You are an expert analyst. Respond with only 'true' or 'false' (lowercase) to indicate if companies are related to {country}."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )
            result = response.choices[0].message.content.strip().lower()

            # Parse boolean result
            if result == "true":
                return True
            elif result == "false":
                return False
            else:
                # If LLM returns something unexpected, default to False for safety
                print(
                    f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
                return False

        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            # Default to False on error to avoid false positives
            return False
    else:
        context_info = f"{case_type} merger case"
        prompt = f"""
            You are a business analyst specializing in M&A and competition law cases.

            Given the following companies from a {context_info}, determine if this deal or these companies are related to {country}.

            Company Details:
            {company_details}

            Consider the following when determining if this is related to {country}:
            - Are any of these companies headquartered in {country}?
            - Do any of these companies have significant operations, subsidiaries, or business presence in {country}?
            - Is this deal likely to have material impact on {country} markets?
            - Are any of these companies publicly traded in {country}?

            Respond with ONLY one word: "true" or "false" (lowercase, no quotes, no explanation).
            """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system",
                        "content": f"You are an expert analyst. Respond with only 'true' or 'false' (lowercase) to indicate if companies are related to {country}."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )
            result = response.choices[0].message.content.strip().lower()

            # Parse boolean result
            if result == "true":
                return True
            elif result == "false":
                return False
            else:
                # If LLM returns something unexpected, default to False for safety
                print(
                    f"⚠️ LLM returned unexpected result: '{result}', defaulting to False")
                return False

        except Exception as e:
            print(f"⚠️ LLM Verification Error: {e}")
            # Default to False on error to avoid false positives
            return False


def verify_usa_relation(
    company_details: Any,
    case_type: str = "EC",
) -> Any:
    """
    Convenience function to verify if companies are related to USA.

    Args:
        company_details: Company details to verify (can be string, list, dict, etc.) - passed as-is to LLM
        case_type: Type of case (EC, China, Brazil, etc.) for context
        additional_context: Optional additional context to provide to LLM

    Returns:
        Usually bool. For `case_type="CHINA-UNCONDITIONAL"` returns a list of USA-related company names.
    """
    return verify_country_relation(
        company_details=company_details,
        country="USA",
        case_type=case_type,
    )
