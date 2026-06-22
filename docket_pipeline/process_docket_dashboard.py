#!/usr/bin/env python3
"""
Process docket entry → update docket_dashboard (MongoDB)
========================================================
Load a `docket` record by ObjectId (must already have `enriched`), then upsert
into the `docket_dashboard` collection.

Dashboard identity: deal_id + docket_metadata.docket_number
Entry dedupe within docket_entries: docket_record_id (docket._id string)

Usage (from project root):
    python docket_pipeline/process_docket_dashboard.py \\
        --record-id 6a1f43795f9fb97307e04d8d --docket-type stb

    python docket_pipeline/process_docket_dashboard.py \\
        --record-id 6a1f43795f9fb97307e04d8d --docket-type stb --force
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from bson import ObjectId
    from pymongo import MongoClient
    from pymongo.collection import Collection
except ImportError:
    ObjectId = None  # type: ignore
    MongoClient = None  # type: ignore
    Collection = None  # type: ignore

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None  # type: ignore

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = str(PROJECT_ROOT / ".env")

DOCKET_COLLECTION = "docket"
DASHBOARD_COLLECTION = "docket_dashboard"

LLM_DEDUP_MODEL = "claude-haiku-4-5-20251001"

IST = timezone(timedelta(hours=5, minutes=30))
SCRIPT_NAME = "process_docket_dashboard"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

# ── Filer name canonicalization ────────────────────────────────────────────────
CANONICAL_NAMES = {
    # STB — Parties (UP/NS variants)
    "union pacific": "Union Pacific",
    "union pacific corporation": "Union Pacific",
    "union pacific railroad": "Union Pacific",
    "union pacific railroad company": "Union Pacific",
    "union pacific corporation and union pacific railroad company": "Union Pacific",
    "union pacific corporation, union pacific railroad company": "Union Pacific",
    "norfolk southern": "Norfolk Southern",
    "norfolk southern corporation": "Norfolk Southern",
    "norfolk southern railway": "Norfolk Southern",
    "norfolk southern railway company": "Norfolk Southern",
    "norfolk southern corporation and norfolk southern railway company": "Norfolk Southern",
    "union pacific / norfolk southern": "Union Pacific / Norfolk Southern (Joint)",
    "union pacific and norfolk southern": "Union Pacific / Norfolk Southern (Joint)",
    "union pacific & norfolk southern": "Union Pacific / Norfolk Southern (Joint)",
    "union pacific corporation, union pacific railroad company, norfolk southern corporation, norfolk southern railway company": "Union Pacific / Norfolk Southern (Joint)",
    # STB — Commission
    "stb": "Surface Transportation Board",
    "surface transportation board": "Surface Transportation Board",
    # STB — Competitors
    "canadian pacific railway company dba cpkc": "CPKC",
    "canadian pacific railway company (cpkc)": "CPKC",
    "canadian pacific kansas city limited": "CPKC",
    "canadian pacific kansas city": "CPKC",
    "grand trunk corporation": "Canadian National (Grand Trunk)",
    "grand trunk corporation, on behalf of itself": "Canadian National (Grand Trunk)",
    "bnsf railway company": "BNSF Railway",
    "csx transportation, inc.": "CSX Transportation",
    "csx transportation, inc": "CSX Transportation",
    # MT-PSC — Commission variants
    "montana public service commission": "Montana Public Service Commission",
    "montana public service commission legal and regulatory staff": "Montana Public Service Commission",
    "commission staff": "Montana Public Service Commission",
    "regulatory division": "Montana Public Service Commission",
    "mpsc": "Montana Public Service Commission",
    "montana psc": "Montana Public Service Commission",
    # MT-PSC — Parties (NWE/Black Hills variants)
    "northwestern energy": "NorthWestern Energy",
    "northwestern corporation": "NorthWestern Energy",
    "nwe group": "NorthWestern Energy",
    "nwe group inc.": "NorthWestern Energy",
    "nwe group, inc.": "NorthWestern Energy",
    "black hills corporation": "Black Hills Corporation",
    "black hills energy": "Black Hills Corporation",
    "black hills montana gas": "Black Hills Corporation",
}

INTERVENOR_TYPE_OVERRIDES = {
    "ruth tines": "retail_customer",
    "j. vann cunningham": "retail_customer",
}

_STRIP_SUFFIXES = re.compile(
    r",?\s*\b(inc\.?|llc|l\.l\.c\.?|corp\.?|corporation|company|co\."
    r"|limited|ltd\.?|l\.?p\.?|on behalf of itself"
    r"|and its affiliates|and subsidiaries|et al\.?)\b",
    re.IGNORECASE,
)

# Strips department/division suffixes after corporate suffix stripping —
# catches "X Commission Legal and Regulatory Staff", "X Board Office of the Secretary", etc.
_STRIP_DEPT_SUFFIXES = re.compile(
    r"\s+(legal and regulatory staff|regulatory staff|legal staff"
    r"|office of the secretary|office of proceedings"
    r"|bureau of investigation|division of enforcement"
    r"|regulatory division|legal division"
    r"|staff|section)\s*$",
    re.IGNORECASE,
)

JURISDICTION_BY_DASHBOARD_TYPE = {
    "stb": "Surface Transportation Board",
    "mt-psc": "Montana Public Service Commission",
    "sd-puc": "South Dakota Public Utilities Commission",
    "nm-prc": "New Mexico Public Regulation Commission",
}

CASE_NAME_BY_DASHBOARD_TYPE = {
    "stb": "Union Pacific / Norfolk Southern — Proposed Merger (FD-36873)",
    "mt-psc": "Montana Public Service Commission - Proposed Merger (2025.10.078)",
    "sd-puc": "South Dakota PUC - NorthWestern / Black Hills Merger (GE25-001)",
    "nm-prc": "New Mexico Public Regulation Commission - TXNM / Blackstone Merger (25-00060-UT)",
}


def _load_env_file(env_path: str) -> None:
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_env_file(ENV_FILE)


def _get_log_file() -> str:
    base = "/var/data/logs" if os.path.isdir(
        "/var/data") else str(SCRIPT_DIR / "logs")
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


def _configure_logging() -> logging.Logger:
    log = logging.getLogger(SCRIPT_NAME)
    if log.handlers:
        return log
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    formatter = _ISTFormatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(
        _get_log_file(),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    log.addHandler(sh)
    log.propagate = False
    return log


logger = _configure_logging()


def _parse_object_id(record_id: str) -> Any:
    if ObjectId is None:
        raise RuntimeError("pymongo/bson not installed")
    try:
        return ObjectId(record_id.strip())
    except Exception as e:
        raise ValueError(f"Invalid record_id (ObjectId): {record_id}") from e


def _parse_date(raw: Any) -> str:
    """Normalize any incoming date to YYYY-MM-DD for consistent string sorting.

    Handles:
      - datetime object           → strftime
      - MongoDB {"$date": "..."}  → unwrap then parse
      - "YYYY-MM-DD[Ttime...]"    → slice to 10 chars (already ISO)
      - "DD/MM/YYYY"              → reformat to YYYY-MM-DD
    """
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, dict):
        raw = raw.get("$date", "")
    if not raw:
        return ""
    s = str(raw).strip()
    # Already ISO: YYYY-MM-DD or YYYY-MM-DDThh:mm:ss...
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # DD/MM/YYYY or DD-MM-YYYY (day first — as used in MT-PSC source data)
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", s)
    if m:
        day, month, year = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{year}-{month}-{day}"
    return s[:10]


def _normalize_name(
    raw: str,
    name_map: Optional[Dict[str, str]] = None,
    known_stakeholders: Optional[List[str]] = None,
    deal_context: str = "",
) -> str:
    """Return a canonical stakeholder name via a 5-step resolution chain.

    Steps 1-3 are rule-based (fast, free). Steps 4-5 use the LLM name map
    cache backed by MongoDB — only active when name_map is provided.
    """
    if not raw:
        return raw
    key = raw.lower().strip()

    # 1. Exact match in CANONICAL_NAMES
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key]

    # 2. Strip corporate suffixes and retry
    stripped = _STRIP_SUFFIXES.sub("", key).strip().rstrip(",").strip()
    if stripped in CANONICAL_NAMES:
        return CANONICAL_NAMES[stripped]

    # 3. Strip department/division suffixes and retry
    dept_stripped = _STRIP_DEPT_SUFFIXES.sub("", stripped).strip()
    if dept_stripped != stripped and dept_stripped in CANONICAL_NAMES:
        return CANONICAL_NAMES[dept_stripped]

    # 4. Check LLM name map cache (resolved in prior runs, stored in MongoDB)
    if name_map is not None and key in name_map:
        return name_map[key]

    # 5. LLM fallback — compare against known stakeholders in this docket
    if name_map is not None and known_stakeholders:
        matched = _llm_match_name(raw, known_stakeholders, deal_context)
        if matched:
            name_map[key] = matched
        else:
            name_map[key] = raw  # Cache self-mapping so we don't re-ask
        return name_map[key]

    return raw


def _grouping_key(name: str) -> str:
    key = name.lower().strip()
    key = _STRIP_SUFFIXES.sub("", key).strip().rstrip(",").strip()
    key = _STRIP_DEPT_SUFFIXES.sub("", key).strip()
    return re.sub(r"\s+", " ", key)


def _llm_match_name(
    new_name: str,
    known_stakeholders: List[str],
    deal_context: str,
) -> Optional[str]:
    """Ask Claude Haiku whether new_name matches any existing stakeholder.

    Returns the matched canonical name, or None if it is a new entity.
    Only called when rule-based normalization fails and known stakeholders exist.
    """
    if _anthropic is None or not known_stakeholders:
        return None
    api_key = os.environ.get(
        "CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    numbered = "\n".join(f"{i + 1}. {n}" for i,
                         n in enumerate(known_stakeholders))
    prompt = (
        f"You are matching stakeholder names in a regulatory docket.\n\n"
        f"Deal: {deal_context}\n\n"
        f'A new filing was submitted by: "{new_name}"\n\n'
        f"Here are the existing stakeholders already identified in this docket:\n"
        f"{numbered}\n\n"
        f"Is the new filer the same entity as any existing stakeholder? Consider:\n"
        f"- Corporate suffixes (Inc., LLC, Corp.) are irrelevant\n"
        f"- Department/division names (Legal Staff, Office of the Secretary) "
        f"within the same org = same entity\n"
        f"- Abbreviations (PSC = Public Service Commission, NWE = NorthWestern Energy)\n"
        f"- Parent/subsidiary relationships filing under different corporate names\n\n"
        f"Reply with ONLY one of:\n"
        f'- The number of the matching stakeholder (e.g. "3")\n'
        f'- "NEW" if this is a genuinely different entity'
    )

    try:
        client = _anthropic.Anthropic(api_key=api_key, timeout=30.0)
        resp = client.messages.create(
            model=LLM_DEDUP_MODEL,
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = resp.content[0].text.strip()
        if answer.upper() == "NEW":
            return None
        num = re.search(r"\d+", answer)
        if num:
            idx = int(num.group()) - 1
            if 0 <= idx < len(known_stakeholders):
                matched = known_stakeholders[idx]
                logger.info("LLM dedup: %r → %r", new_name, matched)
                return matched
        return None
    except Exception as e:
        logger.warning("LLM dedup error for %r: %s", new_name, e)
        return None


def _get_mongo_client() -> Any:
    if MongoClient is None:
        raise RuntimeError("pymongo not installed")
    uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not uri:
        raise RuntimeError("MONGODB_CONNECTION_STRING not set")
    return MongoClient(uri)


def _ensure_dashboard_index(collection: Collection) -> None:
    collection.create_index(
        [("deal_id", 1), ("docket_metadata.docket_number", 1)],
        unique=True,
        name="uniq_deal_docket_number",
    )


def _dashboard_filter(deal_id: str, docket_number: str) -> Dict[str, Any]:
    return {
        "deal_id": deal_id,
        "docket_metadata.docket_number": docket_number,
    }


def _init_dashboard(
    deal_id: str,
    docket_number: str,
    dashboard_docket_type: str,
) -> Dict[str, Any]:
    jurisdiction = JURISDICTION_BY_DASHBOARD_TYPE.get(
        dashboard_docket_type.lower(),
        "",
    )
    case_name = CASE_NAME_BY_DASHBOARD_TYPE.get(
        dashboard_docket_type.lower(),
        "",
    )
    return {
        "deal_id": deal_id,
        "docket_metadata": {
            "docket_number": docket_number,
            "docket_type": dashboard_docket_type,
            "case_name": case_name,
            "jurisdiction": jurisdiction,
            "status": "OPEN",
        },
        "docket_entries": [],
        "docket_stakeholders": [],
        "docket_conditions": [],
    }


def convert_entry(
    docket_doc: dict,
    docket_record_id: str,
    name_map: Optional[Dict[str, str]] = None,
    known_stakeholders: Optional[List[str]] = None,
    deal_context: str = "",
) -> dict:
    """Map docket document (with enriched) to a dashboard docket_entries item."""
    meta = docket_doc.get("metadata") or {}
    enriched = docket_doc.get("enriched") or {}

    raw_filer = enriched.get("filer_name") or meta.get("on_behalf_of") or ""
    filer = _normalize_name(raw_filer, name_map,
                            known_stakeholders, deal_context)

    itype = enriched.get("intervenor_type") or ""
    itype = INTERVENOR_TYPE_OVERRIDES.get(filer.lower(), itype)

    conditions = enriched.get("conditions_requested", [])
    key_args = enriched.get("key_arguments", [])
    key_excerpts = enriched.get("key_excerpts", [])

    return {
        "docket_record_id": docket_record_id,
        "source_docket_type": meta.get("docket_type") or "",
        "hash_id": docket_doc.get("hash_id"),
        "received_date": _parse_date(meta.get("date")),
        "title": enriched.get("title", ""),
        "filer_role": enriched.get("filer_role", ""),
        "filer_name": filer,
        "intervenor_type": itype,
        "position_on_deal": enriched.get("position_on_deal", "Neutral"),
        "opposition_type": enriched.get("opposition_type", ""),
        "relief_type": enriched.get("relief_type", "Neutral"),
        "relief_requested": enriched.get("relief_requested", ""),
        "conditions_requested": conditions if isinstance(conditions, list) else [],
        "entry_summary": enriched.get("entry_summary", ""),
        "key_arguments": key_args if isinstance(key_args, list) else [],
        "key_excerpts": key_excerpts if isinstance(key_excerpts, list) else [],
        "legal_regulatory_significance": enriched.get("legal_regulatory_significance", ""),
        "cumulative_impact": "",
        "download_link": meta.get("url") or meta.get("document_id") or "",
        "document_type": meta.get("document_type") or "",
        "proceeding_phase": enriched.get("proceeding_phase", ""),
        "relevance_level": (enriched.get("relevance_level") or "medium").lower(),
        "deadline_date": enriched.get("deadline_date"),
        "deadline_description": enriched.get("deadline_description", ""),
        "presents_new_info": 0,
    }


def aggregate_stakeholders(
    entries: list,
    name_map: Optional[Dict[str, str]] = None,
    deal_context: str = "",
) -> list:
    group_data = defaultdict(
        lambda: {
            "roles": [],
            "positions": [],
            "opposition_types": [],
            "intervenor_types": [],
            "statuses": [],
            "count": 0,
            "display_names": Counter(),
        }
    )

    # Accumulates canonical names seen so far — used by LLM dedup as context
    known: List[str] = []

    for e in entries:
        raw_name = (e.get("filer_name") or "").strip()
        if not raw_name:
            continue
        canon = _normalize_name(raw_name, name_map, known, deal_context)
        if canon not in known:
            known.append(canon)
        gk = _grouping_key(canon)
        fd = group_data[gk]
        fd["count"] += 1
        fd["display_names"][canon] += 1

        if e.get("filer_role"):
            fd["roles"].append(e["filer_role"])
        if e.get("position_on_deal"):
            fd["positions"].append(e["position_on_deal"])
        if e.get("opposition_type") and e["opposition_type"] not in ("", "None"):
            fd["opposition_types"].append(e["opposition_type"])

        itype = e.get("intervenor_type") or ""
        itype = INTERVENOR_TYPE_OVERRIDES.get(canon.lower(), itype)
        if itype and itype not in ("", "None"):
            fd["intervenor_types"].append(itype)

        raw_status = e.get("intervenor_status") or ""
        if raw_status and raw_status not in ("", "None"):
            fd["statuses"].append(raw_status)

    def most_common(lst: list) -> str:
        if not lst:
            return ""
        return Counter(lst).most_common(1)[0][0]

    stakeholders = []
    for _gk, fd in sorted(group_data.items(), key=lambda x: -x[1]["count"]):
        display_name = fd["display_names"].most_common(1)[0][0]
        status_raw = most_common(fd["statuses"])
        status = "active" if "active" in status_raw else (
            status_raw or "active")
        stakeholders.append(
            {
                "name": display_name,
                "role": most_common(fd["roles"]),
                "filing_count": fd["count"],
                "position": most_common(fd["positions"]),
                "opposition_type": most_common(fd["opposition_types"]),
                "status": status,
                "intervenor_type": most_common(fd["intervenor_types"]),
            }
        )
    return stakeholders


def extract_conditions(entries: list) -> list:
    seen: dict = {}
    all_mentions: dict = defaultdict(list)

    for e in entries:
        conds = e.get("conditions_requested", [])
        if not conds:
            continue

        filer = e.get("filer_name") or "Unknown"
        relief = e.get("relief_type") or ""
        opp_type = e.get("opposition_type") or ""
        filer_role = e.get("filer_role") or ""
        date = e.get("received_date") or ""
        eno = e.get("entry_no", 0)

        relief_lower = relief.lower()
        if "approve_conditional" in relief_lower or (
            "approve" in relief_lower and "deny" not in relief_lower
        ):
            category = "practical"
        elif "deny_with_fallback" in relief_lower:
            category = "fallback"
        elif opp_type in ("ideological", "outright") or "deny" in relief_lower:
            category = "demand"
        else:
            category = "practical" if filer_role == "Commission" else "proposed"

        status = "proposed"
        if filer_role == "Commission":
            status = "required"
        elif "approve" in relief_lower and "deny" not in relief_lower:
            status = "pending"

        for cond_text in conds:
            if not isinstance(cond_text, str) or not cond_text.strip():
                continue
            norm = cond_text.strip().lower()[:80]

            all_mentions[norm].append(
                {
                    "entry_no": eno,
                    "date": date,
                    "filer": filer,
                    "filer_role": filer_role,
                    "relief": relief,
                }
            )

            if norm in seen:
                continue

            seen[norm] = {
                "text": cond_text.strip(),
                "status": status,
                "source": filer,
                "category": category,
                "opposition_type": opp_type if opp_type not in ("", "None") else "",
                "relief_type": relief,
                "asked_in": {"entry_no": eno, "date": date, "filer": filer},
                "resolved_in": None,
            }

    for norm, cond in seen.items():
        mentions = all_mentions[norm]
        asked_role = None
        for m in mentions:
            if m["entry_no"] == cond["asked_in"]["entry_no"]:
                asked_role = m["filer_role"]
                break

        for m in mentions:
            if m["entry_no"] == cond["asked_in"]["entry_no"]:
                continue
            if m["filer_role"] == "Commission":
                cond["resolved_in"] = {
                    "entry_no": m["entry_no"],
                    "date": m["date"],
                    "filer": m["filer"],
                }
                cond["status"] = "required"
                break
            if m["filer_role"] == "Party" and "approve" in m["relief"].lower():
                cond["resolved_in"] = {
                    "entry_no": m["entry_no"],
                    "date": m["date"],
                    "filer": m["filer"],
                }
                cond["status"] = "pending"
                break

        if asked_role == "Commission" and cond["resolved_in"] is None:
            cond["resolved_in"] = cond["asked_in"].copy()

        if cond["status"] == "pending" and cond["resolved_in"] is None:
            cond["resolved_in"] = cond["asked_in"].copy()

    return list(seen.values())


def _sort_and_number_entries(entries: List[dict]) -> None:
    entries.sort(
        key=lambda e: (
            e.get("received_date") or "",
            e.get("docket_record_id") or "",
        )
    )
    for i, entry in enumerate(entries, start=1):
        entry["entry_no"] = i


def _validate_docket_doc(docket_doc: dict) -> Tuple[str, str]:
    deal_id = docket_doc.get("deal_id")
    if not deal_id or not str(deal_id).strip():
        raise ValueError("docket record missing deal_id")

    meta = docket_doc.get("metadata") or {}
    docket_number = meta.get("docket_number")
    if not docket_number or not str(docket_number).strip():
        raise ValueError("docket record missing metadata.docket_number")

    return str(deal_id).strip(), str(docket_number).strip()


def _merge_dashboard_entry(
    dashboard: dict,
    new_entry: dict,
    docket_record_id: str,
    *,
    force: bool,
) -> Tuple[str, bool]:
    """
    Returns (action, replaced) where action is 'appended' | 'replaced' | 'skipped'.
    """
    entries = dashboard.setdefault("docket_entries", [])
    existing_idx = next(
        (
            i
            for i, e in enumerate(entries)
            if e.get("docket_record_id") == docket_record_id
        ),
        None,
    )

    if existing_idx is not None:
        if not force:
            return "skipped", False
        entries.pop(existing_idx)
        entries.append(new_entry)
        return "replaced", True

    entries.append(new_entry)
    return "appended", False


def process_docket_dashboard(
    *,
    record_id: str,
    dashboard_docket_type: str,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Upsert one enriched docket filing into docket_dashboard.

    Returns a result dict with success, stats, and error message when applicable.
    """
    record_id = record_id.strip()
    dashboard_docket_type = dashboard_docket_type.strip()
    if not dashboard_docket_type:
        return {"success": False, "error": "docket-type is required"}

    oid = _parse_object_id(record_id)
    client = _get_mongo_client()

    try:
        db = client.get_database()
        docket_coll = db[DOCKET_COLLECTION]
        dashboard_coll = db[DASHBOARD_COLLECTION]

        _ensure_dashboard_index(dashboard_coll)

        docket_doc = docket_coll.find_one({"_id": oid})
        if not docket_doc:
            return {
                "success": False,
                "error": f"No docket record found with _id={record_id}",
            }

        deal_id, docket_number = _validate_docket_doc(docket_doc)
        docket_record_id = str(docket_doc["_id"])
        meta = docket_doc.get("metadata") or {}
        filer_label = meta.get("on_behalf_of") or meta.get(
            "document_id") or record_id

        logger.info(
            "Processing docket _id=%s deal_id=%s docket_number=%s filer=%s",
            record_id,
            deal_id,
            docket_number,
            filer_label,
        )

        if not docket_doc.get("enriched"):
            return {
                "success": False,
                "error": (
                    "docket record has no enriched data; "
                    "run enrich_entry.py first"
                ),
                "record_id": record_id,
            }

        dash_filter = _dashboard_filter(deal_id, docket_number)
        dashboard = dashboard_coll.find_one(dash_filter)

        if dashboard is None:
            dashboard = _init_dashboard(
                deal_id, docket_number, dashboard_docket_type)
            logger.info(
                "Created new dashboard deal_id=%s docket_number=%s docket_type=%s",
                deal_id,
                docket_number,
                dashboard_docket_type,
            )
        else:
            meta_block = dashboard.setdefault("docket_metadata", {})
            if not meta_block.get("docket_type"):
                meta_block["docket_type"] = dashboard_docket_type

        dashboard["deal_id"] = deal_id

        # Load persisted name map and build deal context for LLM dedup
        name_map: Dict[str, str] = dict(dashboard.get("_name_map") or {})
        dash_meta = dashboard.get("docket_metadata", {})
        deal_context = (
            f"{dash_meta.get('case_name', '')} ({dash_meta.get('jurisdiction', '')})"
        )
        known_stakeholders = [
            s["name"] for s in dashboard.get("docket_stakeholders", []) if s.get("name")
        ]

        new_entry = convert_entry(
            docket_doc, docket_record_id, name_map, known_stakeholders, deal_context
        )
        action, _ = _merge_dashboard_entry(
            dashboard,
            new_entry,
            docket_record_id,
            force=force,
        )

        if action == "skipped":
            return {
                "success": False,
                "skipped": True,
                "error": (
                    f"Entry docket_record_id={docket_record_id} already in dashboard; "
                    "use --force to replace"
                ),
                "record_id": record_id,
                "deal_id": deal_id,
                "docket_number": docket_number,
            }

        entries = dashboard["docket_entries"]
        _sort_and_number_entries(entries)
        dashboard["docket_stakeholders"] = aggregate_stakeholders(
            entries, name_map, deal_context
        )
        dashboard["docket_conditions"] = extract_conditions(entries)
        dashboard["updated_at"] = datetime.now(timezone.utc)
        # Persist resolved names back to MongoDB
        dashboard["_name_map"] = name_map

        write_doc = {k: v for k, v in dashboard.items() if k != "_id"}
        result = dashboard_coll.replace_one(
            dash_filter, write_doc, upsert=True)

        logger.info(
            "Dashboard %s entry %s | entries=%d stakeholders=%d conditions=%d "
            "matched=%d modified=%d upserted=%s",
            "updated" if result.matched_count else "inserted",
            action,
            len(entries),
            len(dashboard["docket_stakeholders"]),
            len(dashboard["docket_conditions"]),
            result.matched_count,
            result.modified_count,
            bool(result.upserted_id),
        )

        return {
            "success": True,
            "record_id": record_id,
            "deal_id": deal_id,
            "docket_number": docket_number,
            "dashboard_docket_type": dashboard_docket_type,
            "entry_action": action,
            "entry_count": len(entries),
            "stakeholder_count": len(dashboard["docket_stakeholders"]),
            "condition_count": len(dashboard["docket_conditions"]),
            "position_on_deal": (docket_doc.get("enriched") or {}).get(
                "position_on_deal"
            ),
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
        }

    except ValueError as e:
        logger.error("Validation error: %s", e)
        return {"success": False, "error": str(e), "record_id": record_id}
    except Exception as e:
        logger.exception("process_docket_dashboard failed: %s", e)
        return {"success": False, "error": str(e), "record_id": record_id}
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update docket_dashboard from an enriched docket record",
    )
    parser.add_argument(
        "--record-id",
        type=str,
        required=True,
        help="MongoDB _id (ObjectId) of the docket entry",
    )
    parser.add_argument(
        "--docket-type",
        type=str,
        required=True,
        help='Dashboard docket_metadata.docket_type (e.g. "stb")',
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing dashboard entry for this docket _id",
    )
    args = parser.parse_args()

    result = process_docket_dashboard(
        record_id=args.record_id,
        dashboard_docket_type=args.docket_type,
        force=args.force,
    )

    if result.get("skipped"):
        logger.warning("%s", result.get("error"))
        sys.exit(2)

    if not result.get("success"):
        logger.error("%s", result.get("error", "unknown error"))
        sys.exit(1)

    logger.info(
        "[OK] record_id=%s deal_id=%s docket_number=%s action=%s entries=%s",
        result.get("record_id"),
        result.get("deal_id"),
        result.get("docket_number"),
        result.get("entry_action"),
        result.get("entry_count"),
    )
    print(
        f"[OK] Dashboard updated — {result.get('entry_action')} entry "
        f"(total {result.get('entry_count')} entries, "
        f"{result.get('stakeholder_count')} stakeholders, "
        f"{result.get('condition_count')} conditions)"
    )


if __name__ == "__main__":
    main()
