"""
LLM Verification Service

A reusable service for verifying company/deal information using LLM prompts.
Can be used across different case types (EC, China, Brazil, etc.).
"""

import os
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
) -> bool:
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
) -> bool:
    """
    Convenience function to verify if companies are related to USA.

    Args:
        company_details: Company details to verify (can be string, list, dict, etc.) - passed as-is to LLM
        case_type: Type of case (EC, China, Brazil, etc.) for context
        additional_context: Optional additional context to provide to LLM

    Returns:
        bool: True if companies are related to USA, False otherwise
    """
    return verify_country_relation(
        company_details=company_details,
        country="USA",
        case_type=case_type,
    )
