#!/usr/bin/env python3
"""
Step 2: Update Dashboard
========================
Takes an enriched entry from docket_db.json (by hash_id) and adds it to
docket_dashboard.json, maintaining docket_entries, docket_stakeholders,
and docket_conditions .

Every time a new entry is added:
  - docket_entries  : entry appended, all re-sorted by date, entry_no re-assigned
  - docket_stakeholders : fully re-aggregated from all entries
  - docket_conditions   : fully re-extracted from all entries (two-pass)

Usage:
    python3 update_dashboard.py --id 492
    python3 update_dashboard.py --id 492 --force           # replace if already exists
    python3 update_dashboard.py --id 492 --deal-id D088    # override deal_id
    python3 update_dashboard.py --id 492 --db path/to/docket_db.json
    python3 update_dashboard.py --id 492 --dashboard path/to/docket_dashboard.json
"""

import json
import re
import sys
import argparse
from collections import Counter, defaultdict
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DEFAULT_DB = SCRIPT_DIR / "docket_db.json"
DEFAULT_DASHBOARD = SCRIPT_DIR / "docket_dashboard.json"
DEFAULT_DEAL_ID = "D088"

# ── Filer name canonicalization  ───────────────
CANONICAL_NAMES = {
    # Parties
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
    # Joint filings
    "union pacific / norfolk southern": "Union Pacific / Norfolk Southern (Joint)",
    "union pacific and norfolk southern": "Union Pacific / Norfolk Southern (Joint)",
    "union pacific & norfolk southern": "Union Pacific / Norfolk Southern (Joint)",
    "union pacific corporation, union pacific railroad company, norfolk southern corporation, norfolk southern railway company": "Union Pacific / Norfolk Southern (Joint)",
    # Commission variants — STB
    "stb": "Surface Transportation Board",
    "surface transportation board": "Surface Transportation Board",
    # Commission variants — Montana PSC
    "montana public service commission": "Montana Public Service Commission",
    "montana public service commission legal and regulatory staff": "Montana Public Service Commission",
    "commission staff": "Montana Public Service Commission",
    "regulatory division": "Montana Public Service Commission",
    "mpsc": "Montana Public Service Commission",
    # Parties — NWE/Black Hills variants
    "northwestern energy": "NorthWestern Energy",
    "northwestern corporation": "NorthWestern Energy",
    "nwe group": "NorthWestern Energy",
    "nwe group inc.": "NorthWestern Energy",
    "black hills corporation": "Black Hills Corporation",
    "black hills energy": "Black Hills Corporation",
    "black hills montana gas": "Black Hills Corporation",
    # Competitor variants
    "canadian pacific railway company dba cpkc": "CPKC",
    "canadian pacific railway company (cpkc)": "CPKC",
    "canadian pacific kansas city limited": "CPKC",
    "canadian pacific kansas city": "CPKC",
    "grand trunk corporation": "Canadian National (Grand Trunk)",
    "grand trunk corporation, on behalf of itself": "Canadian National (Grand Trunk)",
    "bnsf railway company": "BNSF Railway",
    "csx transportation, inc.": "CSX Transportation",
    "csx transportation, inc": "CSX Transportation",
}

# Override misclassified intervenor_types (name_lower → correct type)
INTERVENOR_TYPE_OVERRIDES = {
    "ruth tines": "retail_customer",
    "j. vann cunningham": "retail_customer",
}

# Suffixes stripped during fuzzy normalization
_STRIP_SUFFIXES = re.compile(
    r',?\s*\b(inc\.?|llc|l\.l\.c\.?|corp\.?|corporation|company|co\.'
    r'|limited|ltd\.?|l\.?p\.?|on behalf of itself'
    r'|and its affiliates|and subsidiaries|et al\.?)\b',
    re.IGNORECASE,
)


# Department/division suffixes stripped after corporate suffixes —
# catches "X Commission Legal and Regulatory Staff", "X Board Office of the Secretary", etc.
_STRIP_DEPT_SUFFIXES = re.compile(
    r'\s+(legal and regulatory staff|regulatory staff|legal staff'
    r'|office of the secretary|office of proceedings'
    r'|bureau of investigation|division of enforcement'
    r'|regulatory division|legal division'
    r'|staff|section)\s*$',
    re.IGNORECASE,
)


# ── Name helpers  ──────────────────────────────
def _normalize_name(raw: str) -> str:
    """Return a canonical stakeholder name.

    1. Exact match in CANONICAL_NAMES (case-insensitive).
    2. Strip corporate suffixes and try again.
    3. Return the cleaned name with original casing preserved from first occurrence.
    """
    if not raw:
        return raw
    key = raw.lower().strip()

    # Direct lookup
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key]

    # Strip corporate suffixes and retry
    stripped = _STRIP_SUFFIXES.sub('', key).strip().rstrip(',').strip()
    if stripped in CANONICAL_NAMES:
        return CANONICAL_NAMES[stripped]

    # Strip department/division suffixes and retry
    dept_stripped = _STRIP_DEPT_SUFFIXES.sub('', stripped).strip()
    if dept_stripped != stripped and dept_stripped in CANONICAL_NAMES:
        return CANONICAL_NAMES[dept_stripped]

    # Return original (preserves casing) — deduplication via stripped form
    # happens in aggregate_stakeholders via the grouping key
    return raw


def _grouping_key(name: str) -> str:
    """Return a key for grouping stakeholders by name.
    """
    key = name.lower().strip()
    key = _STRIP_SUFFIXES.sub('', key).strip().rstrip(',').strip()
    key = _STRIP_DEPT_SUFFIXES.sub('', key).strip()

    # Collapse whitespace
    key = re.sub(r'\s+', ' ', key)
    return key


# ── Date parsing ───────────────────────────────────────────────────────────────
def _parse_date(raw) -> str:
    """Normalise MongoDB $date objects or ISO strings to YYYY-MM-DD."""
    if isinstance(raw, dict):
        raw = raw.get("$date", "")
    if not raw:
        return ""
    # Take only the date part of an ISO string
    return str(raw)[:10]


# ── Field mapping: docket_db enriched entry → dashboard entry ─────────────────
def convert_entry(db_entry: dict) -> dict:
    """Map a docket_db.json document (with enriched key) to a dashboard DocketEntry."""
    meta = db_entry.get("metadata", {})
    enriched = db_entry.get("enriched", {})
    hash_id = db_entry.get("hash_id")

    raw_filer = enriched.get("filer_name") or meta.get("on_behalf_of") or ""
    filer = _normalize_name(raw_filer)

    itype = enriched.get("intervenor_type") or ""
    itype = INTERVENOR_TYPE_OVERRIDES.get(filer.lower(), itype)

    conditions = enriched.get("conditions_requested", [])
    key_args = enriched.get("key_arguments", [])
    key_excerpts = enriched.get("key_excerpts", [])

    return {
        # Tracking
        "hash_id":          hash_id,
        # entry_no assigned later after sort
        "received_date":    _parse_date(meta.get("date")),
        "title":            enriched.get("title", ""),
        "filer_role":       enriched.get("filer_role", ""),
        "filer_name":       filer,
        "intervenor_type":  itype,
        "intervenor_status": enriched.get("intervenor_status", ""),
        "position_on_deal": enriched.get("position_on_deal", "Neutral"),
        "opposition_type":  enriched.get("opposition_type", ""),
        "relief_type":      enriched.get("relief_type", "Neutral"),
        "relief_requested": enriched.get("relief_requested", ""),
        "conditions_requested": conditions if isinstance(conditions, list) else [],
        "entry_summary":    enriched.get("entry_summary", ""),
        "key_arguments":    key_args if isinstance(key_args, list) else [],
        "key_excerpts":     key_excerpts if isinstance(key_excerpts, list) else [],
        "cumulative_impact": "",
        "download_link":    meta.get("url") or meta.get("document_id") or "",
        "proceeding_phase": enriched.get("proceeding_phase", ""),
        "relevance_level":  (enriched.get("relevance_level") or "medium").lower(),
        "is_major_filing":  enriched.get("is_major_filing", False),
        "requires_response": enriched.get("requires_response", False),
        "deadline_date":    enriched.get("deadline_date"),
        "legal_regulatory_significance": enriched.get("legal_regulatory_significance") or "",
        "document_type": enriched.get("document_type") or "",
        "deadline_description": enriched.get("deadline_description") or "",
    }


# ── Stakeholder aggregation ───────────────────
def aggregate_stakeholders(entries: list) -> list:
    """Re-derive full stakeholder list from all dashboard entries."""
    group_data = defaultdict(lambda: {
        "roles": [], "positions": [], "opposition_types": [],
        "intervenor_types": [], "statuses": [], "count": 0,
        "display_names": Counter(),
    })

    for e in entries:
        raw_name = (e.get("filer_name") or "").strip()
        if not raw_name:
            continue
        canon = _normalize_name(raw_name)
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

    def most_common(lst):
        if not lst:
            return ""
        return Counter(lst).most_common(1)[0][0]

    stakeholders = []
    for _gk, fd in sorted(group_data.items(), key=lambda x: -x[1]["count"]):
        display_name = fd["display_names"].most_common(1)[0][0]
        role = most_common(fd["roles"])
        status_raw = most_common(fd["statuses"])
        status = "active" if "active" in status_raw else (
            status_raw or "active")
        stakeholders.append({
            "name":           display_name,
            "role":           role,
            "filing_count":   fd["count"],
            "position":       most_common(fd["positions"]),
            "opposition_type": most_common(fd["opposition_types"]),
            "status":         status,
            "intervenor_type": most_common(fd["intervenor_types"]),
        })

    return stakeholders


# ── Condition extraction  ──────────────────────
def extract_conditions(entries: list) -> list:
    """
    Two-pass condition extraction from all dashboard entries.
    Pass 1 — deduplicate, assign asked_in, derive category + status.
    Pass 2 — find resolved_in (Commission ordering or Party settlement).
    """
    seen:        dict = {}
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
        if "approve_conditional" in relief_lower or ("approve" in relief_lower and "deny" not in relief_lower):
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

            all_mentions[norm].append({
                "entry_no":   eno,
                "date":       date,
                "filer":      filer,
                "filer_role": filer_role,
                "relief":     relief,
            })

            if norm in seen:
                continue

            seen[norm] = {
                "text":           cond_text.strip(),
                "status":         status,
                "source":         filer,
                "category":       category,
                "opposition_type": opp_type if opp_type not in ("", "None") else "",
                "relief_type":    relief,
                "asked_in":       {"entry_no": eno, "date": date, "filer": filer},
                "resolved_in":    None,
            }

    # Pass 2 — find resolution
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
                    "entry_no": m["entry_no"], "date": m["date"], "filer": m["filer"]}
                cond["status"] = "required"
                break
            if m["filer_role"] == "Party" and "approve" in m["relief"].lower():
                cond["resolved_in"] = {
                    "entry_no": m["entry_no"], "date": m["date"], "filer": m["filer"]}
                cond["status"] = "pending"
                break

        # Commission filed it → both asked and resolved are the same entry
        if asked_role == "Commission" and cond["resolved_in"] is None:
            cond["resolved_in"] = cond["asked_in"].copy()

        # Pending settlement → the filing itself is its own resolution
        if cond["status"] == "pending" and cond["resolved_in"] is None:
            cond["resolved_in"] = cond["asked_in"].copy()

    return list(seen.values())


# ── Dashboard initialiser ─────────────────────────────────────────────────────
def init_dashboard(deal_id: str, docket_number: str) -> dict:
    return {
        "deal_id": deal_id,
        "docket_metadata": {
            "docket_number":  docket_number or "FD-36873",
            "case_name":      "Union Pacific / Norfolk Southern — Proposed Merger (FD-36873)",
            "jurisdiction":   "Surface Transportation Board",
            "status":         "PENDING",
        },
        "docket_entries":      [],
        "docket_stakeholders": [],
        "docket_conditions":   [],
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Add an enriched entry to docket_dashboard.json")
    parser.add_argument("--id",        type=int, required=True,
                        help="hash_id of the entry to add")
    parser.add_argument("--force",     action="store_true",
                        help="Replace entry if already in dashboard")
    parser.add_argument("--deal-id",   type=str, default=DEFAULT_DEAL_ID,
                        help=f"Deal ID (default: {DEFAULT_DEAL_ID})")
    parser.add_argument("--db",        type=str,
                        default=str(DEFAULT_DB),        help="Path to docket_db.json")
    parser.add_argument("--dashboard", type=str,
                        default=str(DEFAULT_DASHBOARD), help="Path to docket_dashboard.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    dash_path = Path(args.dashboard)

    # ── Load source DB ────────────────────────────────────────────────────────
    if not db_path.exists():
        print(f"[ERROR] docket_db.json not found: {db_path}")
        sys.exit(1)

    with open(db_path) as f:
        db = json.load(f)

    if not isinstance(db, list):
        print("[ERROR] docket_db.json must be a JSON array.")
        sys.exit(1)

    # Find entry
    db_entry = next((e for e in db if e.get("hash_id") == args.id), None)
    if db_entry is None:
        print(f"[ERROR] No entry with hash_id={args.id} in {db_path}")
        print(f"  Available IDs: {[e.get('hash_id') for e in db]}")
        sys.exit(1)

    # Check enriched
    if not db_entry.get("enriched"):
        print(f"[ERROR] Entry hash_id={args.id} has no 'enriched' key.")
        print("  Run Step 1 first:  python3 enrich_entry.py --id", args.id)
        sys.exit(1)

    filer_display = db_entry.get("metadata", {}).get("on_behalf_of", "Unknown")
    print(f"\nEntry #{args.id} — {filer_display}")
    print(
        f"  Position   : {db_entry['enriched'].get('position_on_deal', '?')}")
    print(f"  Filer role : {db_entry['enriched'].get('filer_role', '?')}")

    # ── Load or init dashboard ────────────────────────────────────────────────
    if dash_path.exists():
        with open(dash_path) as f:
            dashboard = json.load(f)
        print(
            f"  Dashboard  : loaded ({len(dashboard.get('docket_entries', []))} existing entries)")
    else:
        docket_no = db_entry.get("metadata", {}).get(
            "docket_number", "FD-36873")
        dashboard = init_dashboard(args.deal_id, docket_no)
        print(f"  Dashboard  : initialized (new file)")

    # Ensure deal_id is set / up to date
    dashboard["deal_id"] = args.deal_id

    # ── Duplicate check ───────────────────────────────────────────────────────
    existing_idx = next(
        (i for i, e in enumerate(dashboard["docket_entries"]) if e.get(
            "hash_id") == args.id),
        None
    )

    if existing_idx is not None:
        if not args.force:
            print(f"\n[WARN] hash_id={args.id} is already in the dashboard.")
            answer = input("  Replace it? [y/n]: ").strip().lower()
            if answer != "y":
                print("Aborted. Use --force to skip this prompt.")
                sys.exit(0)
        else:
            print(f"  [--force] Replacing existing entry hash_id={args.id}")
        dashboard["docket_entries"].pop(existing_idx)

    # ── Convert + append ──────────────────────────────────────────────────────
    new_entry = convert_entry(db_entry)
    dashboard["docket_entries"].append(new_entry)

    # ── Re-sort chronologically + re-assign entry_no ──────────────────────────
    dashboard["docket_entries"].sort(key=lambda e: (
        e.get("received_date") or "", e.get("hash_id") or 0))
    for i, e in enumerate(dashboard["docket_entries"], start=1):
        e["entry_no"] = i

    total_entries = len(dashboard["docket_entries"])

    # ── Re-aggregate stakeholders ─────────────────────────────────────────────
    dashboard["docket_stakeholders"] = aggregate_stakeholders(
        dashboard["docket_entries"])

    # ── Re-extract conditions ─────────────────────────────────────────────────
    dashboard["docket_conditions"] = extract_conditions(
        dashboard["docket_entries"])

    # ── Write back ────────────────────────────────────────────────────────────
    with open(dash_path, "w") as f:
        json.dump(dashboard, f, indent=2)

    # ── Summary ───────────────────────────────────────────────────────────────
    positions = Counter(e["position_on_deal"]
                        for e in dashboard["docket_entries"])
    relevances = Counter(e["relevance_level"]
                         for e in dashboard["docket_entries"])

    print(f"\n[OK] docket_dashboard.json updated")
    print(f"  deal_id        : {args.deal_id}")
    print(f"  Total entries  : {total_entries}")
    print(f"  Stakeholders   : {len(dashboard['docket_stakeholders'])}")
    print(f"  Conditions     : {len(dashboard['docket_conditions'])}")
    print(f"  Positions      : {dict(positions)}")
    print(f"  Relevance      : {dict(relevances)}")
    print(f"  Output         : {dash_path}")


if __name__ == "__main__":
    main()
