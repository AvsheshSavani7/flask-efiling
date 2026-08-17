#!/usr/bin/env python3
"""
Batch enrich FCC ECFS docket records (by docket number)
=======================================================
Runs docket_pipeline enrichment (Claude Haiku) on `docket` documents where
metadata.docket_type is `fcc-ecfs` AND metadata.docket_number is one of the
listed FCC proceedings, then updates docket_dashboard (step 2).

--docket-number is required. Allowed values come from ENRICHMENT_BY_NUMBER
in docket_pipeline/jurisdiction_configs/__init__.py:

    26-134  Amazon / Globalstar     (fcc-gsat)
    26-56   SoftBank / DBRG / Zayo  (fcc-dbrg-zayo)
    26-40   SoftBank / DBRG / WOW   (fcc-dbrg-wow)

Usage (from project root):
    python enrich_fcc_ecfs_docket_records.py --docket-number 26-134 --dry-run
    python enrich_fcc_ecfs_docket_records.py --docket-number 26-56 --limit 5
    python enrich_fcc_ecfs_docket_records.py --docket-number 26-40
"""

import argparse
import os
import sys
import time
from typing import Any, Dict, Optional

from pymongo import MongoClient

from docket_pipeline.jurisdiction_configs import ENRICHMENT_BY_NUMBER

ENV_FILE = ".env"
COLLECTION_NAME = "docket"
DOCKET_TYPE = "fcc-ecfs"
ALLOWED = ENRICHMENT_BY_NUMBER[DOCKET_TYPE]


def _load_env_file(env_path: str) -> None:
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


def _get_docket_collection():
    _load_env_file(ENV_FILE)
    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        raise ValueError("MONGODB_CONNECTION_STRING not found in .env")
    client = MongoClient(mongodb_uri)
    db_name = (os.environ.get("MONGODB_DATABASE_NAME") or "").strip()
    db = client.get_database(db_name) if db_name else client.get_database()
    return db[COLLECTION_NAME], client


def _query_filter(
    docket_number: str,
    only_missing: bool,
    require_content: bool,
    require_deal_id: bool,
) -> Dict[str, Any]:
    filt: Dict[str, Any] = {
        "metadata.docket_type": DOCKET_TYPE,
        "metadata.docket_number": docket_number,
    }
    if require_content:
        filt["content"] = {"$exists": True, "$nin": [None, ""]}
    if require_deal_id:
        filt["deal_id"] = {"$exists": True, "$nin": [None, ""]}
    if only_missing:
        filt["$or"] = [
            {"enriched": {"$exists": False}},
            {"enriched": None},
        ]
    return filt


def run(
    docket_number: str,
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    only_missing: bool = True,
    delay_seconds: float = 1.0,
    skip_dashboard: bool = False,
    require_content: bool = True,
    require_deal_id: bool = True,
) -> Dict[str, Any]:
    from docket_pipeline.enrich_entry import enrich_docket_entry

    dashboard_type = ALLOWED[docket_number]
    collection, client = _get_docket_collection()
    stats = {
        "success": True,
        "dry_run": dry_run,
        "skip_dashboard": skip_dashboard,
        "docket_type": DOCKET_TYPE,
        "docket_number": docket_number,
        "dashboard_docket_type": dashboard_type,
        "matched": 0,
        "processed": 0,
        "enriched": 0,
        "skipped": 0,
        "failed": 0,
    }

    dash_arg = None if skip_dashboard else dashboard_type

    try:
        base_filter = {
            "metadata.docket_type": DOCKET_TYPE,
            "metadata.docket_number": docket_number,
        }
        filt = _query_filter(
            docket_number=docket_number,
            only_missing=only_missing and not force,
            require_content=require_content,
            require_deal_id=require_deal_id,
        )
        total = collection.count_documents(base_filter)
        to_process = collection.count_documents(filt)

        print(f"Collection: {COLLECTION_NAME}")
        print(f"docket_type: {DOCKET_TYPE}")
        print(f"docket_number: {docket_number}")
        print(f"config / dashboard type: {dashboard_type}")
        print(f"Total fcc-ecfs records for docket {docket_number}: {total}")
        print(
            f"Records to process: {to_process} "
            f"(only_missing={only_missing}, force={force}, "
            f"skip_dashboard={skip_dashboard}, "
            f"require_content={require_content}, require_deal_id={require_deal_id})"
        )

        if to_process == 0:
            return stats

        cursor = collection.find(
            filt, {"_id": 1, "metadata": 1, "enriched": 1, "deal_id": 1}
        ).sort("_id", 1)
        if limit is not None:
            cursor = cursor.limit(limit)

        for doc in cursor:
            stats["matched"] += 1
            record_id = str(doc["_id"])
            meta = doc.get("metadata") or {}
            label = meta.get("document_id") or record_id

            if dry_run:
                has_enriched = bool(doc.get("enriched"))
                has_deal = bool(doc.get("deal_id"))
                print(
                    f"[DRY-RUN] {record_id} | {DOCKET_TYPE} | {docket_number} | "
                    f"enriched={'yes' if has_enriched else 'no'} | "
                    f"deal_id={'yes' if has_deal else 'no'} | {str(label)[:60]}"
                )
                continue

            result = enrich_docket_entry(
                record_id=record_id,
                test_mode=False,
                force=force,
                dashboard_docket_type=dash_arg,
            )
            stats["processed"] += 1

            if result.get("skipped"):
                stats["skipped"] += 1
                print(f"[SKIP] {record_id} | already enriched")
            elif result.get("success"):
                stats["enriched"] += 1
                enriched = result.get("enriched") or {}
                line = (
                    f"[OK] {record_id} | {enriched.get('position_on_deal', '?')} | "
                    f"{enriched.get('relevance_level', '?')} | {str(label)[:50]}"
                )
                dashboard = result.get("dashboard")
                if dashboard and dashboard.get("success"):
                    line += f" | dashboard={dashboard.get('entry_action')}"
                elif dashboard and dashboard.get("skipped"):
                    line += " | dashboard=skipped"
                elif dashboard and not dashboard.get("success"):
                    line += " | dashboard=fail"
                print(line)
            else:
                stats["failed"] += 1
                print(
                    f"[FAIL] {record_id} | {result.get('error', 'unknown')} | "
                    f"{str(label)[:50]}"
                )

            if delay_seconds > 0:
                time.sleep(delay_seconds)

        if dry_run:
            print(
                f"\n[DRY-RUN] Listed {stats['matched']} record(s). "
                "No LLM calls or writes."
            )

        print("\n--- Summary ---")
        for k, v in stats.items():
            if k != "success":
                print(f"  {k}: {v}")

        return stats
    finally:
        client.close()


def main():
    allowed_list = ", ".join(sorted(ALLOWED))
    parser = argparse.ArgumentParser(
        description="Batch enrich fcc-ecfs docket records (one docket number)",
    )
    parser.add_argument(
        "--docket-number",
        required=True,
        help=f"Required. One of: {allowed_list}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching records only; no enrichment",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max records to process",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enrich even if enriched key already exists",
    )
    parser.add_argument(
        "--all",
        dest="only_missing",
        action="store_false",
        help="Process all matching records (still skips in enrich_docket_entry unless --force)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between records (default 1.0)",
    )
    parser.add_argument(
        "--skip-dashboard",
        action="store_true",
        help="Enrich only (step 1); do not update docket_dashboard",
    )
    parser.add_argument(
        "--no-require-content",
        action="store_true",
        help="Include records with missing/empty content",
    )
    parser.add_argument(
        "--no-require-deal-id",
        action="store_true",
        help="Include records without deal_id (dashboard step will fail)",
    )
    args = parser.parse_args()

    docket_number = args.docket_number.strip()
    if docket_number not in ALLOWED:
        print(
            f"[ERROR] Unknown docket-number {docket_number!r}. "
            f"Allowed: {allowed_list}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        summary = run(
            docket_number=docket_number,
            dry_run=args.dry_run,
            limit=args.limit,
            force=args.force,
            only_missing=args.only_missing,
            delay_seconds=args.delay,
            skip_dashboard=args.skip_dashboard,
            require_content=not args.no_require_content,
            require_deal_id=not args.no_require_deal_id,
        )
        if not summary.get("success"):
            sys.exit(1)
        if summary.get("failed", 0) > 0:
            sys.exit(1)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
