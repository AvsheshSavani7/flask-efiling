"""
deal_match_llm.py — Shared LLM deal-matching engine.

Usage:
    from deal_match_llm import llm_match_deal_id

    # ACCC — V3 and V4 differ (step-1 label is more specific)
    deal_id = llm_match_deal_id(
        regulator_name="ACCC Australia",
        case_sections={"ACCC CASE TITLE TO MATCH": title},
        source_label="the ACCC title",
        source_label_step1="the ACCC title (both acquirer and target / vendors)",
    )

    # CADE — V3 and V4 are the same, so only source_label needed
    deal_id = llm_match_deal_id(
        regulator_name="CADE Brazil",
        case_sections={
            "INTERESSADOS TEXT (translated to English)": translated_text,
            "ORIGINAL TEXT (Portuguese)":               interessados_text,
        },
        source_label="the interessados text",
    )

    # NZ — three case sections, V3 = V4
    deal_id = llm_match_deal_id(
        regulator_name="NZ Commerce Commission",
        case_sections={
            "CASE TITLE":       title,
            "PARTIES":          parties,
            "DESCRIPTION":      description,
        },
        source_label="the NZ case text (title, parties, description)",
    )

    # EC FS — pre-joined companies string, V3 = V4
    deal_id = llm_match_deal_id(
        regulator_name="EC Foreign Subsidies",
        case_sections={"CASE COMPANIES (from case title)": " / ".join(companies)},
        source_label="the EC Foreign Subsidies case companies",
        deals=deals,  # optional — fetched from MongoDB if omitted
    )

Always returns str | None (deal_id or None).

Partial one-side match (FRPMD):
    result = llm_match_partial_deal(...)
    # (deal_id, "acquirer"|"target") or None
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from mongodb_connection import get_deals_collection

load_dotenv(".env")

logger = logging.getLogger(__name__)

MODEL = "gpt-5.6-terra"

SYSTEM_MESSAGE = (
    "You are an expert M&A deal identifier and matcher. "
    "Respond only with Match: DEAL_ID or None."
)

PARTIAL_SYSTEM_MESSAGE = (
    "You are an expert M&A deal identifier and matcher. "
    "Respond only with Match: DEAL_ID|acquirer, Match: DEAL_ID|target, or None."
)

# ---------------------------------------------------------------------------
# Deals fetching & formatting
# ---------------------------------------------------------------------------


def fetch_open_deals() -> list[dict[str, Any]]:
    """Fetch Open/Unknown deals from MongoDB."""
    collection = get_deals_collection()
    if collection is None:
        logger.warning(
            "get_deals_collection() returned None; cannot fetch deals")
        return []

    status_filter = {
        "$or": [
            {"deal_status": {"$in": ["Open", "Unknown"]}},
            {"deal_status": None},
            {"deal_status": {"$exists": False}},
        ]
    }
    deals = list(collection.find(status_filter))
    for d in deals:
        if "_id" in d:
            d["deal_id"] = str(d["_id"])
            d.pop("_id", None)
    logger.info("Fetched %d open/unknown deals from MongoDB", len(deals))
    return deals


def format_deals_text(deals: list[dict[str, Any]]) -> str:
    """Convert deal list to the text block inserted into prompts."""
    lines = []
    for d in deals:
        deal_id = d.get("deal_id") or str(d.get("_id", ""))
        target = d.get("target") or d.get("target_name", "N/A")
        acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
        line = f"Deal ID: {deal_id} | Target: {target} | Acquirer: {acquirer}"

        target_aliases = d.get("target_aliases") or []
        parent_aliases = d.get("parent_aliases") or []
        if target_aliases:
            line += f" | Target Aliases: {', '.join(str(a) for a in target_aliases)}"
        if parent_aliases:
            line += f" | Parent Aliases: {', '.join(str(a) for a in parent_aliases)}"

        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_llm(prompt: str, system_message: str = SYSTEM_MESSAGE) -> str:
    """Call gpt-5.6-terra and return the raw stripped response string."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)
    res = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
    )
    content = (res.choices[0].message.content or "").strip()
    tokens_used = getattr(res.usage, "total_tokens",
                          "N/A") if res.usage else "N/A"
    logger.info("LLM raw response: %s (tokens=%s)", content[:200], tokens_used)
    return content


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def parse_deal_id(content: str) -> str | None:
    """
    Parse LLM response and return only the deal_id string.

    Handles:
      - Standard:  Match: DEAL_ID
      - Legacy:    Match: DEAL_ID|COMPANY|(target|acquirer)
    Returns None for "None" or any non-match response.
    """
    if not content:
        return None
    stripped = content.strip()
    if stripped.lower() == "none":
        return None
    if not stripped.lower().startswith("match:"):
        logger.info("LLM result: no match prefix in response")
        return None

    after_colon = stripped.split(":", 1)[1].strip()
    deal_id = after_colon.split("|")[0].strip()
    if not deal_id:
        logger.warning("LLM returned 'Match:' but deal_id is empty")
        return None

    logger.info("LLM result: deal_id=%s", deal_id)
    return deal_id


def parse_partial_deal_match(content: str) -> tuple[str, str] | None:
    """
    Parse one-side LLM response.

    Expected: Match: DEAL_ID|acquirer  or  Match: DEAL_ID|target
    Also accepts 'acquire' as acquirer. Returns (deal_id, side) or None.
    """
    if not content:
        return None
    stripped = content.strip()
    if stripped.lower() == "none":
        return None
    if not stripped.lower().startswith("match:"):
        logger.info("LLM partial result: no match prefix in response")
        return None

    after_colon = stripped.split(":", 1)[1].strip()
    parts = [p.strip() for p in after_colon.split("|") if p.strip()]
    if len(parts) < 2:
        logger.warning("LLM partial match missing side: %s", stripped[:200])
        return None

    deal_id = parts[0].strip()
    side_raw = parts[1].strip().lower()
    if side_raw in ("acquire", "acquirer", "a"):
        side = "acquirer"
    elif side_raw in ("target", "t"):
        side = "target"
    else:
        logger.warning("LLM partial match invalid side=%s", parts[1])
        return None

    if not deal_id:
        logger.warning("LLM returned partial Match: but deal_id is empty")
        return None

    logger.info("LLM partial result: deal_id=%s side=%s", deal_id, side)
    return deal_id, side


# ---------------------------------------------------------------------------
# Prompt builder (also used by the test harness)
# ---------------------------------------------------------------------------

def build_match_prompt(
    regulator_name: str,
    case_sections: dict[str, str],
    source_label: str,
    source_label_step1: str,
    deals_text: str,
) -> str:
    """Render the full user-role prompt. Used by both llm_match_deal_id and the test harness."""
    case_block = "\n\n".join(
        f"{label}:\n{value}" for label, value in case_sections.items()
    )
    return f"""You are an expert M&A deal matcher. Determine whether this {regulator_name} case directly refers to a specific deal in our deals database.

DEALS DATABASE:
{deals_text}

{case_block}

MATCHING INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned from {source_label_step1}.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in {source_label}. This rule does NOT exclude names listed in Target Aliases or Acquirer/Parent Aliases — those are explicit deal-side names.
3. For each deal, define the two sides as:
   - Target side = Target field + all Target Aliases
   - Acquirer side = Acquirer field + all Acquirer Parent Aliases.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from {source_label}:
   - one match for the Acquirer side
   - one match for the Target side
5. Do not return a match if only one side is present, even if that single company is an exact match.
6. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - "Inc." vs "Incorporated"
   - "Corp." vs "Corporation"
   - "Ltd" vs "Limited"
   - obvious spacing/casing differences
7. Do not match based only on sector, business type, article topic, indirect association, or partial deal overlap.
8. If {source_label} does not directly name both companies for the same deal, return None.

RESPONSE FORMAT:
-If BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY: Match: DEAL_ID
-If no deal satisfies this rule, respond exactly: None"""


def build_partial_match_prompt(
    regulator_name: str,
    case_sections: dict[str, str],
    source_label: str,
    source_label_step1: str,
    deals_text: str,
) -> str:
    """Prompt for one-side (FRPMD) matching after both-sides FRMD/FRRMD failed."""
    case_block = "\n\n".join(
        f"{label}:\n{value}" for label, value in case_sections.items()
    )
    return f"""You are an expert M&A deal matcher. A previous check already confirmed that this {regulator_name} case does not name BOTH sides of any deal. Now determine whether EXACTLY ONE side of a deal in our database is directly named.

DEALS DATABASE:
{deals_text}

{case_block}

MATCHING INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned from {source_label_step1}.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in {source_label}. This rule does NOT exclude names listed in Target Aliases or Acquirer/Parent Aliases — those are explicit deal-side names.
3. For each deal, define the two sides as:
   - Target side = Target field + all Target Aliases
   - Acquirer side = Acquirer field + all Acquirer Parent Aliases.
4. A deal is a valid PARTIAL match only if EXACTLY ONE side of that deal is confidently matched from {source_label}:
   - either the Acquirer side, OR
   - the Target side
   — not both.
5. Do not return a match if both sides of the same deal are present.
6. If more than one deal has a valid one-side match, return None (ambiguous).
7. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - "Inc." vs "Incorporated"
   - "Corp." vs "Corporation"
   - "Ltd" vs "Limited"
   - obvious spacing/casing differences
8. Do not match based only on sector, business type, article topic, or indirect association.

RESPONSE FORMAT:
-If exactly one deal has exactly one side matched, respond EXACTLY: Match: DEAL_ID|acquirer
  or EXACTLY: Match: DEAL_ID|target
-If no deal satisfies this rule, respond exactly: None"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def llm_match_deal_id(
    regulator_name: str,
    case_sections: dict[str, str],
    source_label: str,
    source_label_step1: str = "",
    deals: list[dict[str, Any]] | None = None,
) -> str | None:
    """
    Match a regulatory case to a deal using LLM.

    Args:
        regulator_name:     Display name shown in the prompt e.g. "CADE Brazil".
        case_sections:      Ordered dict of {display_label: text_value}.
                            Labels appear as section headers in the prompt.
        source_label:       V4 — used in steps 2–8.
                            e.g. "the interessados text", "the ACCC title".
        source_label_step1: V3 — used in step 1 only. Defaults to source_label
                            when they are the same (most regulators).
                            Pass explicitly only when step 1 needs extra wording,
                            e.g. "the ACCC title (both acquirer and target / vendors)".
        deals:              Pre-loaded deal list. Fetched from MongoDB if None.

    Returns:
        deal_id string if a match is found, None otherwise.
    """
    step1_label = source_label_step1 or source_label

    if deals is None:
        deals = fetch_open_deals()

    if not deals:
        logger.info("[%s] No deals available for matching", regulator_name)
        return None

    deals_text = format_deals_text(deals)
    prompt = build_match_prompt(
        regulator_name=regulator_name,
        case_sections=case_sections,
        source_label=source_label,
        source_label_step1=step1_label,
        deals_text=deals_text,
    )

    logger.info(
        "[%s] Calling LLM deal match (model=%s, deals=%d)...",
        regulator_name, MODEL, len(deals),
    )
    raw = call_llm(prompt)
    return parse_deal_id(raw)


def llm_match_partial_deal(
    regulator_name: str,
    case_sections: dict[str, str],
    source_label: str,
    source_label_step1: str = "",
    deals: list[dict[str, Any]] | None = None,
) -> tuple[str, str] | None:
    """
    One-side LLM match used after FRMD and FRRMD both fail.

    Returns (deal_id, side) where side is "acquirer" or "target", or None.
    """
    step1_label = source_label_step1 or source_label

    if deals is None:
        deals = fetch_open_deals()

    if not deals:
        logger.info("[%s] No deals available for partial matching", regulator_name)
        return None

    deals_text = format_deals_text(deals)
    prompt = build_partial_match_prompt(
        regulator_name=regulator_name,
        case_sections=case_sections,
        source_label=source_label,
        source_label_step1=step1_label,
        deals_text=deals_text,
    )

    logger.info(
        "[%s] Calling LLM partial deal match (model=%s, deals=%d)...",
        regulator_name, MODEL, len(deals),
    )
    raw = call_llm(prompt, system_message=PARTIAL_SYSTEM_MESSAGE)
    return parse_partial_deal_match(raw)
