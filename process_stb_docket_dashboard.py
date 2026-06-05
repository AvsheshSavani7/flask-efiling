#!/usr/bin/env python3
"""
Batch update docket_dashboard for STB docket records (step 2)
==============================================================
Fetches all `docket` documents where metadata.docket_type is
`stb-document` or `stb-environmentalComment`, requires `enriched`,
then runs process_docket_dashboard for each record.

Usage (from project root):
    python process_stb_docket_dashboard.py --dry-run
    python process_stb_docket_dashboard.py --limit 10
    python process_stb_docket_dashboard.py --force
    python process_stb_docket_dashboard.py
"""

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional

from pymongo import MongoClient

ENV_FILE = ".env"
COLLECTION_NAME = "docket"
DASHBOARD_DOCKET_TYPE = "stb"
STB_DOCKET_TYPES: List[str] = [
    "stb-document",
    "stb-environmentalComment",
]


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
    return client.get_database()[COLLECTION_NAME], client


def _query_filter() -> Dict[str, Any]:
    return {
        "metadata.docket_type": {"$in": STB_DOCKET_TYPES},
        "enriched": {"$exists": True, "$ne": None},
    }


def run(
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    delay_seconds: float = 0.0,
) -> Dict[str, Any]:
    from docket_pipeline.process_docket_dashboard import (
        logger,
        process_docket_dashboard,
    )

    collection, client = _get_docket_collection()
    stats: Dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "matched": 0,
        "processed": 0,
        "appended": 0,
        "replaced": 0,
        "skipped": 0,
        "failed": 0,
    }

    try:
        base_filter = {"metadata.docket_type": {"$in": STB_DOCKET_TYPES}}
        filt = _query_filter()
        total = collection.count_documents(base_filter)
        enriched_count = collection.count_documents(filt)
        to_process = enriched_count

        by_type = {}
        for dt in STB_DOCKET_TYPES:
            by_type[dt] = collection.count_documents(
                {**base_filter, "enriched": {"$exists": True, "$ne": None}}
            )

        logger.info("Collection: %s", COLLECTION_NAME)
        logger.info("STB docket types: %s", STB_DOCKET_TYPES)
        logger.info("Total STB records: %d", total)
        for dt, count in by_type.items():
            logger.info("  - %s (enriched): %d", dt, count)
        logger.info(
            "Records to process (step 2): %d (force=%s)",
            to_process,
            force,
        )

        print(f"Collection: {COLLECTION_NAME}")
        print(f"docket types: {STB_DOCKET_TYPES}")
        print(f"Total STB records: {total}")
        for dt, count in by_type.items():
            print(f"  - {dt} (enriched): {count}")
        print(f"Records to process: {to_process} (force={force})")

        if to_process == 0:
            return stats

        cursor = collection.find(
            filt,
            {"_id": 1, "metadata": 1, "deal_id": 1},
        ).sort("_id", 1)
        if limit is not None:
            cursor = cursor.limit(limit)

        for doc in cursor:
            stats["matched"] += 1
            record_id = str(doc["_id"])
            meta = doc.get("metadata") or {}
            filing_type = meta.get("docket_type", "")
            label = meta.get("document_id") or record_id

            if dry_run:
                print(
                    f"[DRY-RUN] {record_id} | {filing_type} | "
                    f"deal_id={doc.get('deal_id', '?')} | {str(label)[:60]}"
                )
                continue

            result = process_docket_dashboard(
                record_id=record_id,
                dashboard_docket_type=DASHBOARD_DOCKET_TYPE,
                force=force,
            )
            stats["processed"] += 1

            if result.get("skipped"):
                stats["skipped"] += 1
                msg = result.get("error", "skipped")
                logger.warning("[SKIP] %s | %s", record_id, msg)
                print(f"[SKIP] {record_id} | {msg}")
            elif result.get("success"):
                action = result.get("entry_action", "updated")
                if action == "appended":
                    stats["appended"] += 1
                elif action == "replaced":
                    stats["replaced"] += 1
                line = (
                    f"[OK] {record_id} | {action} | "
                    f"entries={result.get('entry_count')} | {str(label)[:50]}"
                )
                logger.info(line)
                print(line)
            else:
                stats["failed"] += 1
                err = result.get("error", "unknown")
                logger.error("[FAIL] %s | %s", record_id, err)
                print(f"[FAIL] {record_id} | {err} | {str(label)[:50]}")

            if delay_seconds > 0:
                time.sleep(delay_seconds)

        if dry_run:
            print(
                f"\n[DRY-RUN] Listed {stats['matched']} record(s). No dashboard writes."
            )

        print("\n--- Summary ---")
        for k, v in stats.items():
            if k != "success":
                print(f"  {k}: {v}")
        logger.info("Batch complete: %s", stats)

        return stats
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Batch run docket_dashboard step 2 for stb-document and "
            "stb-environmentalComment records (must have enriched)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching enriched records only; no dashboard writes",
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
        help="Replace existing dashboard entries for each docket _id",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds between records (default 0)",
    )
    args = parser.parse_args()

    try:
        summary = run(
            dry_run=args.dry_run,
            limit=args.limit,
            force=args.force,
            delay_seconds=args.delay,
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
