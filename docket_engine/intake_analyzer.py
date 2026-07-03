"""
Intake Analyzer — Merger Arbitrage Intake Note Generator
=========================================================
Shared utility for all docket scrapers in docket_engine/.

Calls GPT-4.1-mini with a fixed prompt to produce a structured intake note
from a docket filing's comprehensive_summary. Mirrors the n8n flow logic
at the code level so every jurisdiction scraper gets the same output.

Usage:
    from docket_engine.intake_analyzer import generate_intake_note

    note = generate_intake_note(comprehensive_summary="...")
    # Returns dict or None on failure:
    # {
    #   "Filing":    "...",
    #   "Type":      "...",
    #   "Summary":   "...",
    #   "Relevance": "High/Medium/Low – ..."
    # }
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

# Ensure project root is on sys.path so imports work when run as a script
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger(__name__)  # → docket_engine.intake_analyzer

INTAKE_MODEL = "gpt-4.1-mini"

_INTAKE_PROMPT_TEMPLATE = """You are an analyst supporting a merger arbitrage strategy.
You will be given the text of a regulatory filing or docket submission related to a merger.

Text: {summary}

Your task:
1. Identify the type of filing (e.g., public comment, staff report, ALJ recommendation, company data response, AG intervention).
2. Summarize the main content in 1–2 sentences.
3. Assess the relevance for merger arbitrage (High / Medium / Low) based on whether the filing:
   - materially affects approval risk or deal timing (High),
   - reinforces narrative/political dynamics without adding new facts (Medium),
   - or is operational/procedural detail with minimal impact (Low).
4. Output only a concise intake note in bullet form, with "Relevance" highlighted up front.
Format your response exact like below json this:
{{
  "Filing": "Deal Name or Parties",
  "Type": "filing type",
  "Summary": "1–2 sentence content summary",
  "Relevance": "High/Medium/Low – short justification"
}}"""


def generate_intake_note(
    comprehensive_summary: str,
) -> Optional[dict]:
    """
    Generate a merger arbitrage intake note from a docket comprehensive_summary.

    Args:
        comprehensive_summary: The summary text returned by analyze_docket_entry().

    Returns:
        Parsed dict with keys Filing, Type, Summary, Relevance.
        Returns None if the summary is empty or the API call fails.
    """
    if not comprehensive_summary or not comprehensive_summary.strip():
        logger.warning("intake_analyzer: empty comprehensive_summary — skipping.")
        return None

    api_key = os.environ.get("OPENAI_API_KEY_DOCKET")
    if not api_key:
        logger.warning(
            "intake_analyzer: OPENAI_API_KEY_DOCKET not set — skipping intake note."
        )
        return None

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("intake_analyzer: openai package not installed — skipping.")
        return None

    client = OpenAI(api_key=api_key)
    prompt = _INTAKE_PROMPT_TEMPLATE.format(summary=comprehensive_summary.strip())

    try:
        logger.info(f"  Generating intake note via {INTAKE_MODEL}...")
        response = client.chat.completions.create(
            model=INTAKE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
        note = json.loads(raw)

        logger.info(
            f"  Intake note — Relevance: {note.get('Relevance', '?')}"
        )
        return note

    except json.JSONDecodeError as e:
        logger.warning(f"  intake_analyzer: JSON parse failed: {e} | raw={raw[:200]!r}")
        return None
    except Exception as e:
        logger.warning(f"  intake_analyzer: API call failed: {e}")
        return None
