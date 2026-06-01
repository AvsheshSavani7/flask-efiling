"""
Shared logging utilities used across all scraper and monitor scripts.

Usage:
    from log_utils import (
        cleanup_old_logs,
        refresh_log_file,
        make_get_log_file,
        ensure_script_logger,
        refresh_script_log,
        ISTLogFormatter,
    )

    logger, get_log_file = ensure_script_logger("my_script")
    LOG_FILE = refresh_script_log(logger, get_log_file)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
PERSISTENT_LOG_DIR = "/var/data/logs"

logger = logging.getLogger("log_utils")


def today_ist_date_str() -> str:
    """Today's date (YYYY-MM-DD) in IST — matches scraper log file names."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def make_get_log_file(
    script_name: str,
    persistent_log_dir: str = PERSISTENT_LOG_DIR,
) -> Callable[[], str]:
    """Return a callable that resolves today's date-wise log path (IST)."""

    def get_log_file() -> str:
        base = persistent_log_dir if os.path.isdir("/var/data") else "."
        log_dir = os.path.join(base, script_name)
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f"{today_ist_date_str()}.log")

    return get_log_file


class ISTLogFormatter(logging.Formatter):
    """Log timestamps in IST (UTC+5:30), same as ACCC/FTC/EC scrapers."""

    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


def get_handler_log_path(target_logger: logging.Logger) -> Optional[str]:
    """Absolute path of the logger's RotatingFileHandler, if any."""
    for handler in target_logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return os.path.abspath(handler.baseFilename)
    return None


def refresh_log_file(target_logger, current_log_file, get_log_file_fn):
    """Switch the logger's RotatingFileHandler to today's date-based log file.

    Call at the start of each scraper run (and optionally during long runs)
    so gunicorn workers roll over to the correct IST date file.

    *current_log_file* should be the path the handler is actually writing to
    (use :func:`get_handler_log_path` or a module-level ``LOG_FILE`` updated
    on the previous run). Do not pass today's path from :func:`make_get_log_file`
    alone before comparing — that skips rollover in long-lived workers.

    Returns the new log file path (absolute).
    """
    new_log_file = os.path.abspath(get_log_file_fn())

    current_abs: Optional[str] = None
    if current_log_file:
        current_abs = os.path.abspath(current_log_file)
    if not current_abs:
        current_abs = get_handler_log_path(target_logger)

    if current_abs == new_log_file:
        return new_log_file

    for handler in target_logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.close()
            handler.baseFilename = new_log_file
            handler.stream = handler._open()
            break

    return new_log_file


def refresh_script_log(
    target_logger: logging.Logger,
    get_log_file_fn: Callable[[], str],
    current_log_file: Optional[str] = None,
) -> str:
    """Roll over to today's IST log file using the handler's current path."""
    baseline = current_log_file or get_handler_log_path(target_logger)
    return refresh_log_file(target_logger, baseline, get_log_file_fn)


def ensure_script_logger(
    script_name: str,
    *,
    log_level: Optional[str] = None,
    log_max_bytes: Optional[int] = None,
    log_backup_count: Optional[int] = None,
    log_retention_days: Optional[int] = None,
    persistent_log_dir: str = PERSISTENT_LOG_DIR,
    add_stream_handler: bool = True,
) -> Tuple[logging.Logger, Callable[[], str]]:
    """Create or reuse a script logger with IST timestamps and dated log files."""
    level_name = (log_level or os.getenv("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = log_max_bytes or int(
        os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024))
    )
    backup_count = log_backup_count or int(os.getenv("LOG_BACKUP_COUNT", "3"))
    retention_days = log_retention_days or int(
        os.getenv("LOG_RETENTION_DAYS", "30")
    )

    get_log_file = make_get_log_file(script_name, persistent_log_dir)
    log = logging.getLogger(script_name)
    log.setLevel(level)

    if not log.handlers:
        formatter = ISTLogFormatter("%(asctime)s | %(levelname)s | %(message)s")
        fh = RotatingFileHandler(
            get_log_file(),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        log.addHandler(fh)
        if add_stream_handler:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(formatter)
            log.addHandler(sh)

    log.propagate = False
    cleanup_old_logs(os.path.dirname(get_log_file()), retention_days)
    return log, get_log_file


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
                file_date = datetime.strptime(date_part, "%Y-%m-%d").replace(
                    tzinfo=IST
                )
                if file_date < cutoff:
                    os.remove(path)
                    logger.info("Deleted old log file: %s", path)
            except Exception as exc:
                logger.warning("Could not cleanup log file %s: %s", filename, exc)

        elif filename.startswith("debug_screenshot_") and filename.endswith(".png"):
            try:
                ts_part = filename.replace("debug_screenshot_", "").replace(
                    ".png", ""
                )
                file_date = datetime.strptime(ts_part, "%Y%m%d_%H%M%S").replace(
                    tzinfo=IST
                )
                if file_date < cutoff:
                    os.remove(path)
                    logger.info("Deleted old screenshot: %s", path)
            except Exception as exc:
                logger.warning(
                    "Could not cleanup screenshot %s: %s", filename, exc
                )
