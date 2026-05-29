#!/usr/bin/env python3
"""
CCI India — Orders Section 31 scraper
=====================================
https://www.cci.gov.in/combination/orders-section31

Usage:
    python orders_section31_scraper.py
    python orders_section31_scraper.py --headed
    python orders_section31_scraper.py --dry-run
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Optional

from cci_common import (
    SOURCE_SECTION31,
    build_section31_update_fields,
    build_skeleton_doc_for_source,
    fetch_section31_detail_pdfs,
    parse_section31_table,
    section31_cutoff_date,
)
from cci_scraper_runtime import CciScraperConfig, run_cci_datatable_scraper

LIST_URL = "https://www.cci.gov.in/combination/orders-section31"


def _fetch_detail(page, row: Dict[str, Any], detail_url: str) -> Dict[str, Optional[str]]:
    summary_url, order_url = fetch_section31_detail_pdfs(page, detail_url)
    return {"section31_summary_url": summary_url, "section31_order_url": order_url}


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
    summary_url = detail_data.get("section31_summary_url")
    order_url = detail_data.get("section31_order_url")

    if is_new_record:
        doc = build_skeleton_doc_for_source(
            row,
            SOURCE_SECTION31,
            detail_url,
            now_iso,
            section31_summary_url=summary_url,
            section31_order_url=order_url,
        )
        collection.insert_one(doc)
    else:
        fields = build_section31_update_fields(
            row, detail_url, summary_url, order_url, now_iso, existing
        )
        collection.update_one(
            {"combination_registration_no": reg_no},
            {"$set": fields},
        )


CONFIG = CciScraperConfig(
    list_url=LIST_URL,
    script_name="cci_section31",
    source_key=SOURCE_SECTION31,
    source_label="Orders Section 31",
    title="CCI Orders Section 31 scraper",
    parse_table=parse_section31_table,
    cutoff=section31_cutoff_date(),
    cutoff_field="date_of_notification",
    fetch_detail=_fetch_detail,
    persist_record=_persist,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="CCI Orders Section 31 scraper")
    parser.add_argument("--headed", action="store_true", help="Run browser headed")
    parser.add_argument("--dry-run", action="store_true", help="No DB or email")
    args = parser.parse_args()
    run_cci_datatable_scraper(CONFIG, headed=args.headed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
