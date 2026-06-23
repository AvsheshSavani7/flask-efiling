#!/usr/bin/env python3
"""
Docket Entry Enrichment
=======================
Enriches a docket entry with structured fields via Claude Haiku, then updates
docket_dashboard (step 2) when ``dashboard_docket_type`` is set.

MongoDB (production) — lookup/update by native _id only (no extra id field written):
    python enrich_entry.py --record-id 6a1f43795f9fb97307e04d8d
    python enrich_entry.py --record-id 6a1f43795f9fb97307e04d8d --docket-type stb
    python enrich_entry.py --record-id 6a1f43795f9fb97307e04d8d --skip-dashboard
    python enrich_entry.py --record-id 6a1f43795f9fb97307e04d8d --test-mode --output /tmp/out.json

Local JSON file (dev):
    python enrich_entry.py --db docket_db.json --record-id 6a1f43795f9fb97307e04d8d
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

try:
    from bson import ObjectId
    from pymongo import MongoClient
except ImportError:
    ObjectId = None  # type: ignore
    MongoClient = None  # type: ignore

from .jurisdiction_configs import get_config
from .jurisdiction_configs.base import JurisdictionConfig

# ── Paths / env (same pattern as docket_entry_analyzer.py) ─────────────────────
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = str(PROJECT_ROOT / ".env")
DEFAULT_DB = SCRIPT_DIR / "docket_db.json"
TEST_OUTPUT_DIR = SCRIPT_DIR / "enriched_test_output"


def _load_env_file(env_path: str) -> None:
    """Load environment variables from .env file."""
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value


_load_env_file(ENV_FILE)

# ── Model config ───────────────────────────────────────────────────────────────
LLM_MODEL = "claude-haiku-4-5-20251001"
COST_PER_M_INPUT = 1.00
COST_PER_M_OUTPUT = 5.00
MAX_TOKENS = 2500
FULL_CONTENT_LIMIT = 500_000

logger = logging.getLogger("docket_enrich_entry")

# ── Jurisdiction config helpers ────────────────────────────────────────────────
# Maps MongoDB metadata.docket_type values → jurisdiction registry keys
_DOCKET_TYPE_TO_JURISDICTION: Dict[str, str] = {
    "stb":                       "stb",
    "stb-document":              "stb",
    "stb-environmentalcomment":  "stb",
    "mt-psc":                    "mt-psc",
    "sd-puc":                    "sd-puc",
    "nm-prc":                    "nm-prc",
    "ne-psc":                    "ne-psc",
}


def _get_config_for_docket_type(docket_type: str) -> "JurisdictionConfig":
    """Resolve a MongoDB docket_type string to a JurisdictionConfig. Falls back to STB."""
    key = _DOCKET_TYPE_TO_JURISDICTION.get(
        (docket_type or "").strip().lower(), "stb"
    )
    return get_config(key)


def build_system_prompt(config: "JurisdictionConfig") -> str:
    """Assemble the full system prompt from a JurisdictionConfig."""
    party_names_display = ", ".join(p.title() for p in config.party_names[:4])
    return f"""You are an expert regulatory analyst reviewing docket filings for M&A transactions.
Your analysis will be used by merger arbitrage professionals to assess deal risk and prepare client reports.

CRITICAL: Your summaries must be ACTIONABLE and QUOTABLE. Clients need specific arguments, specific excerpts, and specific relief requested - not generic descriptions.

BAD (meta-description): "The filer argues the merger will harm competition and raise prices."
GOOD (substantive): "The filer argues: 'The merger will result in rate increases for residential customers, as the combined entity will lack competitive pressure to maintain current rate structures.'"

The difference: GOOD gives the actual argument a client can quote. BAD just describes that an argument exists.

CONTEXT: {config.system_context}

FILER TAXONOMY:
1. Commission - Actual {config.name} orders, notices, procedural rulings, staff recommendations
2. Party - Merger applicants ({party_names_display})
3. Intervenor - Everyone else, sub-typed as:
{config.filer_taxonomy}
4. Other

RELIEF TYPE (be precise):
- "Deny" = Wants outright rejection
- "Approve" = Supports approval
- "Approve_Conditional" = Supports approval IF conditions met
- "Deny_With_Fallback_Conditions" = Wants denial, BUT if approved, wants specific conditions (common for sophisticated intervenors)
- "Procedural" = Only procedural request (extension, intervention, discovery motion, etc.)
- "Neutral" = No specific relief requested

OPPOSITION TYPE (MANDATORY when position_on_deal is "Oppose"):
You MUST classify every opposing filer. Here's how to decide:

1. **conditional** (MOST COMMON - use as default for opposers) = They list specific conditions that would satisfy them
   - Example signals: "we request the following conditions...", "approval should be contingent on...", "we would not oppose if..."

2. **outright** = They want the deal denied entirely, no conditions would fix it
   - Example signals: "must be rejected", "cannot be remedied", "fundamentally flawed", "no conditions can address"

3. **ideological** = Opposition based on general principles, not this specific deal
   - Example signals: "we oppose all utility consolidation", "monopoly control", broad policy objections

4. **procedural** = Only objecting to process/timing, not substance
   - Example signals: "need more time", "discovery is inadequate", "procedural defects"

5. **ambiguous** = Filing states opposition but provides insufficient detail to classify

DEFAULT RULE: If they list ANY specific conditions or remedies -> "conditional"
If they explicitly say deal must be denied with no path to approval -> "outright"
If not enough information to determine -> "ambiguous"

INTERVENOR STATUS (current disposition of the filer):
- "active_opposition" = Currently opposing, no settlement reached
- "settled" = Reached settlement/stipulation/agreement with applicants
- "withdrawn" = Withdrew intervention or opposition
- "watching" = Filed to monitor but not actively opposing (Neutral/procedural stance)
- null = For Commission/Party filings or unclear status

DEPTH GUIDANCE:
- For HIGH relevance + major intervenor: Provide extensive detail (6-8 sentence summary, 3+ excerpts, full argument list)
- For HIGH relevance + individual: Standard detail (3-4 sentences)
- For MEDIUM/LOW relevance: Brief (2-3 sentences)

FOR OPPOSITION FILINGS - CAPTURE SETTLEMENT SIGNALS:
- Do they say "we would support if..." or list specific acceptable conditions? -> conditional
- Do they mention willingness to negotiate or discuss? -> likely to settle
- Do they cite irremediable harms or fundamental policy objections? -> outright/ideological, unlikely to settle
- Are conditions specific and achievable (rate caps, service guarantees) or vague/impossible? -> specific = more settleable

Respond ONLY with valid JSON:
{{
  "title": "Concise descriptive title",

  "filer_role": "Commission | Party | Intervenor | Other",
  "intervenor_type": "competitor | business_customer | retail_customer | special_interest | labor | government | environmental | consumer_advocate | agricultural | other | null",
  "filer_name": "CONSISTENT short name. For the regulator: '{config.name}'. Use the SAME name format for the same filer across all filings.",
  "filer_description": "Who they are and their stake in 1-2 sentences",

  "position_on_deal": "Support | Oppose | Neutral | Procedural",
  "opposition_type": "conditional | outright | ideological | procedural | ambiguous (REQUIRED if position is Oppose)",
  "intervenor_status": "active_opposition | settled | withdrawn | watching | null",
  "relief_type": "Deny | Approve | Approve_Conditional | Deny_With_Fallback_Conditions | Procedural | Neutral",
  "relief_requested": "Specific relief requested in their own words",
  "conditions_requested": ["List each specific condition requested, if any"],

  "proceeding_phase": "Pre-Filing | Comment | Discovery | Hearing | Post-Decision | Compliance",

  "relevance_level": "High | Medium | Low",
  "relevance_explanation": "Why this relevance level",

  "entry_summary": "SUBSTANTIVE summary with QUOTABLE CONTENT - not meta-descriptions. Include: (1) Their ACTUAL arguments in their words, (2) Specific harms alleged with numbers if cited, (3) What conditions would satisfy them (if conditional opposition), (4) Any settlement signals. A client should be able to quote this summary directly.",

  "key_arguments": [
    "The ACTUAL argument in substantive form - not 'argues about rates' but 'Transaction will increase residential rates by an estimated 15% over 5 years'",
    "Each should be quotable in a client memo"
  ],

  "key_excerpts": [
    "CRITICAL: Pull the most important 1-2 sentences verbatim from the filing that capture their core argument",
    "Prioritize excerpts that explain WHY they oppose or WHAT they want"
  ],

  "legal_regulatory_significance": "Does this change scope of review, evidentiary burden, create new issues? Be specific.",

  "requires_response": true,
  "deadline_date": "YYYY-MM-DD or null",
  "deadline_description": "What's due",

  "issue_flags": ["completeness_issue", "schedule_change", "issue_framing", "hearing_requested", "conditions_proposed"],
  "key_parties": ["parties mentioned"],

  "is_major_filing": true
}}

IMPORTANT:
- For Petitions to Deny, Comments from major intervenors, or briefs from significant filers: PROVIDE EXTENSIVE DETAIL
- Pull actual quotes that a professional could use in a client memo
- List EACH argument separately in key_arguments, not a summary of arguments
- If they say "deny the merger BUT if you approve it, require X, Y, Z" - that's Deny_With_Fallback_Conditions
- Be specific about conditions: not "behavioral remedies" but "require rate cap for 5 years post-merger"
"""


def default_docket_number(docket_type: str) -> str:
    return _get_config_for_docket_type(docket_type).docket_number


def classify_filer_role_rule_based(
    doc_type: str, by: str, docket_type: str = ""
) -> str:
    config = _get_config_for_docket_type(docket_type)
    doc_lower = (doc_type or "").lower()
    by_lower = (by or "").lower()
    if any(x in doc_lower for x in config.commission_doc_types):
        if any(x in by_lower for x in config.commission_names):
            return "Commission"
    if any(x in by_lower for x in config.commission_names):
        return "Commission"
    if any(x in by_lower for x in config.party_names):
        return "Party"
    return "Intervenor"


def select_content(entry: dict) -> Tuple[str, str]:
    """
    Returns (content_text, content_source).
    """
    content = (entry.get("content") or "").strip()

    if content:
        if len(content) <= FULL_CONTENT_LIMIT:
            return content, "content"
        tier2 = (entry.get("tier2_analysis") or {}).get("response", "").strip()
        if tier2:
            text = (
                f"[Filing content is {len(content):,} characters — using tier2 analysis as pre-summary]\n\n"
                f"{tier2}"
            )
            return text, "tier2_fallback"
        head_tail = (
            content[:100_000]
            + f"\n\n[... middle section omitted ({len(content) - 200_000:,} chars) ...]\n\n"
            + content[-100_000:]
        )
        return head_tail, "content_head_tail"

    summary = (entry.get("summary") or "").strip()
    if summary:
        return summary, "summary"

    return "", "empty"


def _record_id_str(entry: dict, record_id: Optional[str] = None) -> str:
    """MongoDB _id as string. For test-mode entries without _id, use document_id."""
    if record_id:
        return record_id
    oid = entry.get("_id")
    if oid is not None:
        return str(oid)
    doc_id = (entry.get("metadata") or {}).get("document_id", "unknown")
    return f"test-{doc_id}"


def _parse_object_id(record_id: str):
    if ObjectId is None:
        raise RuntimeError("pymongo/bson not installed")
    try:
        return ObjectId(record_id)
    except Exception as e:
        raise ValueError(f"Invalid record_id (ObjectId): {record_id}") from e


def _json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        if "$oid" in value and len(value) == 1:
            return value["$oid"]
        if "$date" in value and len(value) == 1:
            return value["$date"]
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _get_mongo_collection():
    if MongoClient is None:
        raise RuntimeError("pymongo not installed")
    uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not uri:
        raise RuntimeError("MONGODB_CONNECTION_STRING not set")
    client = MongoClient(uri)
    return client.get_database()["docket"]


def _load_entry_from_mongo(record_id: str) -> dict:
    oid = _parse_object_id(record_id)
    collection = _get_mongo_collection()
    entry = collection.find_one({"_id": oid})
    if not entry:
        raise LookupError(f"No docket entry found with _id={record_id}")
    return entry


def _find_entry_in_json_db(db: list, record_id: str) -> Tuple[int, dict]:
    oid_str = record_id.strip()
    for i, e in enumerate(db):
        raw_id = e.get("_id")
        if isinstance(raw_id, dict) and "$oid" in raw_id:
            if raw_id["$oid"] == oid_str:
                return i, e
        elif raw_id is not None and str(raw_id) == oid_str:
            return i, e
    raise LookupError(f"No entry found with _id={record_id} in local JSON DB")


def _default_test_output_path(entry: dict, record_id: Optional[str] = None) -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    doc_id = (entry.get("metadata") or {}).get("document_id", "entry")
    safe_doc = re.sub(r"[^\w.-]+", "_", str(doc_id))[:80]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = _record_id_str(entry, record_id)
    safe_id = re.sub(r"[^\w.-]+", "_", str(label))[:24]
    return TEST_OUTPUT_DIR / f"{safe_id}_{safe_doc}_{ts}.json"


def _write_test_json(
    entry: dict,
    enriched: dict,
    output_path: Path,
    record_id: Optional[str] = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_id": _record_id_str(entry, record_id),
        "enriched_at": enriched.get("enriched_at"),
        "enriched": enriched,
        "entry": _json_safe(entry),
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return output_path


def enrich(client: "anthropic.Anthropic", entry: dict, record_id: Optional[str] = None) -> Optional[dict]:
    """Run Claude Haiku extraction on one entry. Returns the enriched dict or None."""
    entry_ref = _record_id_str(entry, record_id)
    meta = entry.get("metadata", {})
    docket_type = meta.get("docket_type", "")
    doc_type = meta.get("document_type", "")
    by = meta.get("on_behalf_of", "")
    date_raw = meta.get("date", "")
    if isinstance(date_raw, datetime):
        date_str = date_raw.isoformat()
    elif isinstance(date_raw, dict):
        date_str = date_raw.get("$date", "")
    else:
        date_str = str(date_raw)
    docket_no = meta.get("docket_number") or default_docket_number(docket_type)
    filename = meta.get("filename", "")
    decision_summary = meta.get("decision_summary", "")

    content_text, content_source = select_content(entry)
    config = _get_config_for_docket_type(docket_type)
    rule_role = classify_filer_role_rule_based(doc_type, by, docket_type)
    system_prompt = build_system_prompt(config)

    parts = [
        f"Record ID: {entry_ref}",
        f"Jurisdiction: {config.name}",
        f"Docket Type: {docket_type or 'unknown'}",
        f"Document Type: {doc_type}",
        f"Filed By: {by}",
        f"Date: {date_str}",
        f"Docket: {docket_no}",
        f"Initial Classification (rule-based): {rule_role}",
        f"Content Source: {content_source}",
    ]
    parts = [p for p in parts if p]
    if filename:
        parts.append(f"Filename: {filename}")
    if decision_summary:
        parts.append(f"Decision Summary: {decision_summary}")
    if content_text:
        parts.append(f"\n--- FILING CONTENT ---\n{content_text}")
    else:
        parts.append("\n[No content available for this entry]")

    user_prompt = "\n".join(parts)

    for attempt in range(2):
        try:
            if attempt == 0:
                response = client.messages.create(
                    model=LLM_MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
            else:
                logger.info("Retrying enrichment with simplified prompt")
                simple_prompt = f"""Analyze this filing and return ONLY valid JSON.

{user_prompt}

Return JSON with these exact fields:
{{
  "title": "descriptive title",
  "filer_role": "Commission|Party|Intervenor|Other",
  "intervenor_type": "competitor|business_customer|retail_customer|special_interest|labor|government|environmental|consumer_advocate|agricultural|other|null",
  "filer_name": "name",
  "position_on_deal": "Support|Oppose|Neutral|Procedural",
  "opposition_type": "conditional|outright|ideological|procedural|ambiguous|null",
  "intervenor_status": "active_opposition|settled|withdrawn|watching|null",
  "relief_type": "Deny|Approve|Approve_Conditional|Deny_With_Fallback_Conditions|Procedural|Neutral",
  "relief_requested": "...",
  "conditions_requested": [],
  "proceeding_phase": "Pre-Filing|Comment|Discovery|Hearing|Post-Decision|Compliance",
  "relevance_level": "High|Medium|Low",
  "relevance_explanation": "...",
  "entry_summary": "2-3 sentence summary",
  "key_arguments": [],
  "key_excerpts": [],
  "legal_regulatory_significance": "...",
  "requires_response": false,
  "deadline_date": null,
  "deadline_description": "",
  "issue_flags": [],
  "key_parties": [],
  "is_major_filing": false
}}"""
                response = client.messages.create(
                    model=LLM_MODEL,
                    max_tokens=3000,
                    system="You are a regulatory filing analyst. Return ONLY valid JSON. No markdown. No text before or after the JSON object.",
                    messages=[{"role": "user", "content": simple_prompt}],
                )

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = (input_tokens / 1_000_000) * COST_PER_M_INPUT + \
                (output_tokens / 1_000_000) * COST_PER_M_OUTPUT

            raw_text = response.content[0].text.strip()
            raw_text = re.sub(r"^```json?\s*\n?", "", raw_text)
            raw_text = re.sub(r"\n?\s*```\s*$", "", raw_text)
            raw_text = raw_text.strip()

            first = raw_text.find("{")
            last = raw_text.rfind("}")
            if first >= 0 and last > first:
                raw_text = raw_text[first:last + 1]

            llm = json.loads(raw_text)

            conditions = llm.get("conditions_requested", [])
            key_args = llm.get("key_arguments", [])
            key_excerpts = llm.get("key_excerpts", [])
            issue_flags = llm.get("issue_flags", [])
            key_parties = llm.get("key_parties", [])

            return {
                "title": llm.get("title", ""),
                "filer_role": llm.get("filer_role", rule_role),
                "filer_name": llm.get("filer_name", by),
                "filer_description": llm.get("filer_description", ""),
                "position_on_deal": llm.get("position_on_deal", "Neutral"),
                "opposition_type": llm.get("opposition_type", ""),
                "intervenor_type": llm.get("intervenor_type", ""),
                "intervenor_status": llm.get("intervenor_status", ""),
                "relief_type": llm.get("relief_type", "Neutral"),
                "relief_requested": llm.get("relief_requested", ""),
                "conditions_requested": conditions if isinstance(conditions, list) else [],
                "proceeding_phase": llm.get("proceeding_phase", ""),
                "relevance_level": llm.get("relevance_level", "Medium"),
                "relevance_explanation": llm.get("relevance_explanation", ""),
                "entry_summary": llm.get("entry_summary", ""),
                "key_arguments": key_args if isinstance(key_args, list) else [],
                "key_excerpts": key_excerpts if isinstance(key_excerpts, list) else [],
                "legal_regulatory_significance": llm.get("legal_regulatory_significance", ""),
                "requires_response": llm.get("requires_response", False),
                "deadline_date": llm.get("deadline_date"),
                "deadline_description": llm.get("deadline_description", ""),
                "issue_flags": issue_flags if isinstance(issue_flags, list) else [],
                "key_parties": key_parties if isinstance(key_parties, list) else [],
                "is_major_filing": llm.get("is_major_filing", False),
                "content_source": content_source,
                "analysis_cost": round(cost, 6),
                "analysis_input_tokens": input_tokens,
                "analysis_output_tokens": output_tokens,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
            }

        except json.JSONDecodeError:
            if attempt == 0:
                logger.warning("JSON parse error during enrichment — retrying")
                continue
            logger.error("Could not parse LLM response as JSON after retry")
            return None

        except anthropic.APITimeoutError:
            if attempt == 0:
                logger.warning("Enrichment API timeout — retrying")
                continue
            logger.error("Enrichment API timed out")
            return None

        except Exception as e:
            logger.exception("Enrichment failed: %s", e)
            return None

    return None


def _run_dashboard_update(
    record_id: str,
    dashboard_docket_type: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Step 2: upsert enriched filing into docket_dashboard collection."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from docket_pipeline.process_docket_dashboard import process_docket_dashboard

    logger.info(
        "Running dashboard update for _id=%s docket_type=%s",
        record_id,
        dashboard_docket_type,
    )
    result = process_docket_dashboard(
        record_id=record_id,
        dashboard_docket_type=dashboard_docket_type,
        force=force,
    )
    if result.get("success"):
        logger.info(
            "Dashboard %s for _id=%s (total entries=%s)",
            result.get("entry_action"),
            record_id,
            result.get("entry_count"),
        )
    elif result.get("skipped"):
        logger.warning(
            "Dashboard entry skipped for _id=%s: %s",
            record_id,
            result.get("error"),
        )
    else:
        logger.error(
            "Dashboard update failed for _id=%s: %s",
            record_id,
            result.get("error"),
        )
    return result


def enrich_docket_entry(
    *,
    record_id: Optional[str] = None,
    entry: Optional[dict] = None,
    test_mode: bool = False,
    force: bool = False,
    test_output_path: Optional[Path] = None,
    local_db_path: Optional[Path] = None,
    dashboard_docket_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Enrich a docket entry by MongoDB _id (ObjectId string) or in-memory entry dict.

    Only writes the ``enriched`` subdocument to MongoDB — never a separate id field.
    test_mode=True: write JSON only, do not update MongoDB.
    When ``dashboard_docket_type`` is set and not test_mode, runs step 2 via
    process_docket_dashboard after enrichment (or when enrichment is skipped).
    """
    if anthropic is None:
        return {"success": False, "error": "anthropic package not installed"}

    try:
        if entry is None:
            if not record_id:
                return {"success": False, "error": "record_id or entry is required"}
            if local_db_path:
                with open(local_db_path, encoding="utf-8") as f:
                    db = json.load(f)
                _, entry = _find_entry_in_json_db(db, record_id)
            else:
                entry = _load_entry_from_mongo(record_id)
        elif record_id is None and entry.get("_id") is not None:
            record_id = str(entry["_id"])

        rid = _record_id_str(entry, record_id)

        if entry.get("enriched") and not force:
            result: Dict[str, Any] = {
                "success": True,
                "skipped": True,
                "message": "Entry already enriched",
                "record_id": rid,
                "enriched": entry.get("enriched"),
            }
            if dashboard_docket_type and record_id and not test_mode:
                result["dashboard"] = _run_dashboard_update(
                    record_id,
                    dashboard_docket_type,
                    force=force,
                )
            return result

        api_key = os.environ.get(
            "CLAUDE_API_KEY")
        if not api_key:
            return {"success": False, "error": "CLAUDE_API_KEY not found"}

        client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        enriched = enrich(client, entry, record_id=record_id)
        if enriched is None:
            return {
                "success": False,
                "error": "Enrichment LLM call failed",
                "record_id": rid,
            }

        if test_mode:
            out_path = test_output_path or _default_test_output_path(
                entry, record_id)
            _write_test_json(entry, enriched, out_path, record_id=record_id)
            logger.info("Test mode: wrote enrichment JSON to %s", out_path)
            return {
                "success": True,
                "test_mode": True,
                "record_id": rid,
                "enriched": enriched,
                "output_path": str(out_path),
            }

        if record_id is None:
            return {
                "success": False,
                "error": "record_id required to update MongoDB (or use test_mode=True)",
            }

        oid = _parse_object_id(record_id)
        collection = _get_mongo_collection()
        update_result = collection.update_one(
            {"_id": oid},
            {"$set": {"enriched": enriched}},
        )
        if update_result.matched_count == 0:
            return {"success": False, "error": f"No document matched _id={record_id}"}

        logger.info("Enrichment saved to MongoDB _id=%s", record_id)
        result = {
            "success": True,
            "record_id": record_id,
            "enriched": enriched,
            "modified": update_result.modified_count > 0,
        }
        if dashboard_docket_type:
            result["dashboard"] = _run_dashboard_update(
                record_id,
                dashboard_docket_type,
                force=force,
            )
        return result

    except Exception as e:
        logger.exception("enrich_docket_entry failed: %s", e)
        return {"success": False, "error": str(e), "record_id": record_id}


def main():
    parser = argparse.ArgumentParser(
        description="Enrich a single docket entry with Claude Haiku")
    parser.add_argument(
        "--record-id",
        type=str,
        required=True,
        help="MongoDB _id (ObjectId) of the entry to enrich",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Do not update MongoDB; write enrichment result to JSON",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON path (test-mode only; default: enriched_test_output/)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing enriched key without prompting",
    )
    parser.add_argument(
        "--docket-type",
        type=str,
        default="stb",
        help='Dashboard docket_metadata.docket_type for step 2 (default: stb)',
    )
    parser.add_argument(
        "--skip-dashboard",
        action="store_true",
        help="Skip docket_dashboard update after enrichment",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Optional local docket_db.json instead of MongoDB for loading entry",
    )
    args = parser.parse_args()

    if args.db and not args.test_mode:
        print(
            "[WARN] --db loads from local JSON but still updates MongoDB unless --test-mode is set.")

    entry = None
    if args.db:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"[ERROR] DB file not found: {db_path}")
            sys.exit(1)
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, list):
            print("[ERROR] docket_db.json must be a JSON array of entries.")
            sys.exit(1)
        try:
            _, entry = _find_entry_in_json_db(db, args.record_id)
        except LookupError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
        print(f"\nLoaded entry from {db_path} — _id={args.record_id}")
    else:
        print(f"\nLoading entry from MongoDB — _id={args.record_id}")

    if entry and entry.get("enriched") and not args.force and not args.test_mode:
        answer = input(
            "  Entry already has 'enriched'. Overwrite? [y/n]: ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    if entry:
        content_text, content_source = select_content(entry)
        print(f"  Content source : {content_source}")
        print(f"  Content length : {len(content_text):,} chars")

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("CLAUDE_API_KEY"):
        print(f"[ERROR] ANTHROPIC_API_KEY not found. Set it in {ENV_FILE}")
        sys.exit(1)

    result = enrich_docket_entry(
        record_id=args.record_id,
        entry=entry,
        test_mode=args.test_mode,
        force=args.force,
        test_output_path=Path(args.output) if args.output else None,
        local_db_path=Path(args.db) if args.db else None,
        dashboard_docket_type=None if args.skip_dashboard else args.docket_type,
    )

    if not result.get("success"):
        print(f"\n[FAILED] {result.get('error', 'unknown error')}")
        sys.exit(1)

    dashboard = result.get("dashboard")

    if result.get("skipped"):
        print(f"\n[SKIP] {result.get('message')}")
        if dashboard:
            _print_dashboard_result(dashboard)
        sys.exit(0)

    enriched = result["enriched"]
    print(" done.")
    print(f"  Cost           : ${enriched['analysis_cost']:.6f}")
    print(f"  Position       : {enriched['position_on_deal']}")
    print(f"  Relevance      : {enriched['relevance_level']}")

    if args.test_mode:
        print(f"\n[OK] Test mode — wrote JSON to {result.get('output_path')}")
    else:
        print(f"\n[OK] Updated MongoDB entry _id={args.record_id}")
        if dashboard:
            _print_dashboard_result(dashboard)


def _print_dashboard_result(dashboard: Dict[str, Any]) -> None:
    if dashboard.get("success"):
        print(
            f"  Dashboard      : {dashboard.get('entry_action')} "
            f"({dashboard.get('entry_count')} entries)"
        )
    elif dashboard.get("skipped"):
        print(f"  Dashboard      : skipped — {dashboard.get('error')}")
    else:
        print(f"  Dashboard      : FAILED — {dashboard.get('error')}")


if __name__ == "__main__":
    main()
