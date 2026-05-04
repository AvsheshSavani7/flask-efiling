"""
Shared logging utilities used across all scraper and monitor scripts.

Usage:
    from log_utils import cleanup_old_logs

    cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)
"""

import os
import logging
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger("log_utils")


def cleanup_old_logs(log_dir: str, days: int = 30) -> None:
    """Delete date-wise log files (and their rotated backups) older than *days*.

    Handles filenames like:
        2026-05-01.log
        2026-05-01.log.1
        2026-05-01.log.2
    """
    if not os.path.isdir(log_dir):
        return

    cutoff = datetime.now(IST) - timedelta(days=days)

    for filename in os.listdir(log_dir):
        path = os.path.join(log_dir, filename)

        if filename.endswith(".log") or ".log." in filename:
            try:
                date_part = filename.split(".log")[0]
                file_date = datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=IST)
                if file_date < cutoff:
                    os.remove(path)
                    logger.info(f"Deleted old log file: {path}")
            except Exception as exc:
                logger.warning(f"Could not cleanup log file {filename}: {exc}")

        elif filename.startswith("debug_screenshot_") and filename.endswith(".png"):
            try:
                ts_part = filename.replace("debug_screenshot_", "").replace(".png", "")
                file_date = datetime.strptime(ts_part, "%Y%m%d_%H%M%S").replace(tzinfo=IST)
                if file_date < cutoff:
                    os.remove(path)
                    logger.info(f"Deleted old screenshot: {path}")
            except Exception as exc:
                logger.warning(f"Could not cleanup screenshot {filename}: {exc}")
