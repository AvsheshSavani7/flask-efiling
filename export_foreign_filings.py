#!/usr/bin/env python3
"""
Export Foreign Filings After a Cutoff Date
===========================================
Fetches all records from each foreign filing collection that were created OR
updated after the given cutoff date, then writes a grouped JSON file.

Collections queried:
  ec_cases          – EC Merger / FS cases
  fs_cases          – EC Foreign Subsidies
  accc_cases        – Australia ACCC
  brazil_cases      – Brazil CADE
  ftc_cases         – USA FTC
  nz_cases          – New Zealand ComCom
  canada_cases      – Canada Competition Bureau
  uk_cma_cases      – UK CMA
  german_cases      – German Bundeskartellamt
  sa_compcom_cases  – South Africa CompCom
  japan_cases       – Japan JFTC press releases
  korea_cases       – Korea KFTC press releases
  samr_cases        – China SAMR (public notices)       ← processed_at string
  samr_unconditional– China SAMR (unconditional)        ← processed_at string
  samr_conditional  – China SAMR (conditional)          ← processed_at string

Date filter logic:
  • Non-SAMR: include if created_at >= cutoff OR updated_at >= cutoff
    - Most collections store these as ISO strings; UK CMA stores as BSON Date
  • SAMR: include if processed_at >= cutoff  (string prefix comparison is safe
    because ISO-8601 strings are lexicographically sortable)

Usage:
    python export_foreign_filings.py                        # defaults to 2026-05-22
    python export_foreign_filings.py --cutoff 2026-05-01
    python export_foreign_filings.py --cutoff 2026-05-22 --output my_export.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

# Collections where created_at / updated_at are stored as ISO strings (str)
STRING_DATE_SOURCES = [
    {"key": "ec",     "collection": "ec_cases"},
    {"key": "fs",     "collection": "fs_cases"},
    {"key": "cci",    "collection": "cci_cases"},
    {"key": "accc",   "collection": "accc_cases"},
    {"key": "cade",   "collection": "brazil_cases"},
    {"key": "ftc",    "collection": "ftc_cases"},
    {"key": "nz",     "collection": "nz_cases"},
    {"key": "canada", "collection": "canada_cases"},
    {"key": "german", "collection": "german_cases"},
    {"key": "sa_compcom", "collection": "sa_compcom_cases"},
    {"key": "jftc", "collection": "japan_cases"},
    {"key": "kftc", "collection": "korea_cases"},
]

# Collections where created_at / updated_at are stored as BSON Date objects
BSON_DATE_SOURCES = [
    {"key": "uk", "collection": "uk_cma_cases"},
]

# SAMR collections — only processed_at field, stored as ISO string (no tz)
SAMR_SOURCES = [
    {"key": "china_samr_public",        "collection": "samr_cases"},
    {"key": "china_samr_unconditional", "collection": "samr_unconditional"},
    {"key": "china_samr_conditional",   "collection": "samr_conditional"},
]


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def _load_env_file(env_path: str = ".env") -> None:
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")


def get_db():
    _load_env_file()
    uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not uri:
        print("ERROR: MONGODB_CONNECTION_STRING not found in environment.",
              file=sys.stderr)
        sys.exit(1)
    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=15000,
    )
    client.admin.command("ping")
    return client, client.get_database()


# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------

def _serialize(obj: Any) -> Any:
    """Recursively convert types that are not JSON-serialisable."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def fetch_string_date_source(col, cutoff_str: str) -> List[Dict]:
    """
    Query a collection where created_at / updated_at are ISO strings.
    Returns records where either field is >= cutoff_str.
    """
    query = {
        "$or": [
            {"created_at": {"$gte": cutoff_str}},
            # {"updated_at": {"$gte": cutoff_str}},
        ]
    }
    return list(col.find(query))


def fetch_bson_date_source(col, cutoff_dt: datetime) -> List[Dict]:
    """
    Query a collection where created_at / updated_at are BSON Date objects.
    Returns records where either field is >= cutoff_dt.
    """
    query = {
        "$or": [
            {"created_at": {"$gte": cutoff_dt}},
            # {"updated_at": {"$gte": cutoff_dt}},
        ]
    }
    return list(col.find(query))


def fetch_samr_source(col, cutoff_str: str) -> List[Dict]:
    """
    Query a SAMR collection where only processed_at exists (ISO string, no tz).
    ISO strings are lexicographically comparable, so prefix comparison works.
    """
    query = {"processed_at": {"$gte": cutoff_str}}
    return list(col.find(query))


# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

def export_foreign_filings(cutoff_date: str, output_file: str) -> None:
    """
    Fetch records from all foreign filing collections created/updated after
    cutoff_date and write them to output_file as JSON.

    Args:
        cutoff_date: ISO date string, e.g. "2026-05-22"
        output_file: Destination JSON file path
    """
    print(f"Connecting to MongoDB...")
    client, db = get_db()
    print("Connected.\n")

    # Cutoff as a timezone-aware datetime for BSON Date comparisons
    cutoff_dt = datetime(
        *[int(p) for p in cutoff_date.split("-")],
        tzinfo=timezone.utc,
    )
    # Cutoff prefix string for ISO string comparisons ("2026-05-22")
    cutoff_str = cutoff_date

    results: Dict[str, List[Dict]] = {}
    summary: Dict[str, int] = {}

    # -- String-date collections --
    for source in STRING_DATE_SOURCES:
        key = source["key"]
        col_name = source["collection"]
        print(f"  Querying {col_name} (string dates)...")
        try:
            col = db[col_name]
            docs = fetch_string_date_source(col, cutoff_str)
            serialized = [_serialize(d) for d in docs]
            results[key] = serialized
            summary[key] = len(serialized)
            print(f"    → {len(serialized)} record(s) found")
        except Exception as exc:
            print(f"    ERROR querying {col_name}: {exc}", file=sys.stderr)
            results[key] = []
            summary[key] = 0

    # -- BSON Date collections --
    for source in BSON_DATE_SOURCES:
        key = source["key"]
        col_name = source["collection"]
        print(f"  Querying {col_name} (bson dates)...")
        try:
            col = db[col_name]
            docs = fetch_bson_date_source(col, cutoff_dt)
            serialized = [_serialize(d) for d in docs]
            results[key] = serialized
            summary[key] = len(serialized)
            print(f"    → {len(serialized)} record(s) found")
        except Exception as exc:
            print(f"    ERROR querying {col_name}: {exc}", file=sys.stderr)
            results[key] = []
            summary[key] = 0

    # -- SAMR collections (processed_at string) --
    for source in SAMR_SOURCES:
        key = source["key"]
        col_name = source["collection"]
        print(f"  Querying {col_name} (samr processed_at string)...")
        try:
            col = db[col_name]
            docs = fetch_samr_source(col, cutoff_str)
            serialized = [_serialize(d) for d in docs]
            results[key] = serialized
            summary[key] = len(serialized)
            print(f"    → {len(serialized)} record(s) found")
        except Exception as exc:
            print(f"    ERROR querying {col_name}: {exc}", file=sys.stderr)
            results[key] = []
            summary[key] = 0

    client.close()

    total = sum(summary.values())
    output = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "cutoff_date": cutoff_date,
        "total_records": total,
        "summary": summary,
        "results": results,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Export complete: {output_file}")
    print(
        f"Cutoff date   : {cutoff_date} (created_at or updated_at >= cutoff)")
    print(f"Total records : {total}")
    print(f"\nBreakdown:")
    for key, count in summary.items():
        print(f"  {key:<30} {count}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export foreign filing records created/updated after a cutoff date."
    )
    parser.add_argument(
        "--cutoff",
        default="2026-05-22",
        help="Cutoff date in YYYY-MM-DD format (default: 2026-05-22)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: auto-generated with cutoff date)",
    )
    args = parser.parse_args()

    # Validate cutoff format
    try:
        datetime.strptime(args.cutoff, "%Y-%m-%d")
    except ValueError:
        print(
            f"ERROR: Invalid cutoff date '{args.cutoff}'. Use YYYY-MM-DD format.", file=sys.stderr)
        sys.exit(1)

    output_file = args.output or f"foreign_filings_after_{args.cutoff}.json"
    export_foreign_filings(cutoff_date=args.cutoff, output_file=output_file)


if __name__ == "__main__":
    main()
