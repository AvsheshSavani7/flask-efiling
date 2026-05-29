#!/usr/bin/env python3
"""
CCI India — Cases Approved with Modification scraper
====================================================
https://www.cci.gov.in/combination/cases-approved-with-modification

Usage:
    python orders_approved_with_modification_scraper.py
    python orders_approved_with_modification_scraper.py --headed
    python orders_approved_with_modification_scraper.py --dry-run
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Optional

from cci_common import (
    SOURCE_APPROVED_MOD,
    build_approved_mod_update_fields,
    build_skeleton_doc_for_source,
    fetch_order_pdf_url,
    parse_approved_with_modification_table,
)
from cci_scraper_runtime import CciScraperConfig, run_cci_datatable_scraper

LIST_URL = "https://www.cci.gov.in/combination/cases-approved-with-modification"


def _fetch_detail(page, row: Dict[str, Any], detail_url: str) -> Dict[str, Optional[str]]:
    order_url = fetch_order_pdf_url(page, detail_url)
    return {"approved_with_modification_url": order_url}


def _persist(
    collection,
    row: Dict[str, Any],
    detail_url: str,
    detail_data: Dict[str, Optional[str]],
    existing: Optional[Dict[str, Any]],
    is_new_record: bool,
    now_iso: str,
) -> None:
    reg_no = row["combination_registration_no"]
    order_url = detail_data.get("approved_with_modification_url")

    if is_new_record:
        doc = build_skeleton_doc_for_source(
            row,
            SOURCE_APPROVED_MOD,
            detail_url,
            now_iso,
            approved_with_modification_url=order_url,
        )
        collection.insert_one(doc)
    else:
        fields = build_approved_mod_update_fields(
            row, detail_url, order_url, now_iso, existing
        )
        collection.update_one(
            {"combination_registration_no": reg_no},
            {"$set": fields},
        )


CONFIG = CciScraperConfig(
    list_url=LIST_URL,
    script_name="cci_approved_with_modification",
    source_key=SOURCE_APPROVED_MOD,
    source_label="Approved with Modification",
    title="CCI Approved with Modification scraper",
    parse_table=parse_approved_with_modification_table,
    cutoff=None,
    cutoff_field="",
    single_page=True,
    fetch_detail=_fetch_detail,
    persist_record=_persist,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CCI Cases Approved with Modification scraper"
    )
    parser.add_argument("--headed", action="store_true",
                        help="Run browser headed")
    parser.add_argument("--dry-run", action="store_true",
                        help="No DB or email")
    args = parser.parse_args()
    run_cci_datatable_scraper(CONFIG, headed=args.headed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
