"""Rotating file logger + console handler.

Format:
    File:    2026-08-08 15:30:07,123 [INFO] thaoresearch: message
    Console: [INFO] thaoresearch: message (no timestamps, for cleaner live view)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from utils import paths

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 5

# Set when the log directory turns out to be unwritable, so the GUI can say
# "running without a log file" instead of the app dying on startup.
FILE_LOGGING_ERROR: Optional[str] = None


def log_dir() -> Path:
    """Current log directory, read from ``utils.paths`` at call time.

    Never snapshot this at import: ``main()`` may call
    ``paths.set_data_dir()`` for ``--data-dir`` after this module is already
    imported, and a stale module-level copy would point at the old location.
    """
    return paths.LOG_DIR


def log_file() -> Path:
    return log_dir() / "app.log"


def __getattr__(name: str):
    """Keep ``logger.LOG_DIR`` / ``logger.LOG_FILE`` working for older callers."""
    if name == "LOG_DIR":
        return log_dir()
    if name == "LOG_FILE":
        return log_file()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _current_log_files(log_dir_: Optional[Path] = None) -> list:
    """Return all `app*.log` files in the logs dir (newest first by mtime).

    Used by the Dashboard log tail reader to pick the most recent log file
    (after a user-initiated Clear Log rotate).
    """
    log_dir_ = log_dir_ or log_dir()
    if not log_dir_.exists():
        return []
    files = []
    for p in log_dir_.iterdir():
        if not p.is_file():
            continue
        if p.name == "app.log" or (p.name.startswith("app_") and p.name.endswith(".log")):
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def latest_log_path(log_dir_: Optional[Path] = None) -> Optional[Path]:
    """Return the path to the newest app*.log file, or None if none exist.

    `app.log` is preferred when its mtime is the most recent; otherwise
    the newest `app_<timestamp>.log` archive is returned.
    """
    files = _current_log_files(log_dir_)
    return files[0] if files else None


def rotate_log_file(log_dir_: Optional[Path] = None) -> Optional[Path]:
    """Move the current `app.log` to `app_<YYYYMMDD_HHMMSS>.log`.

    Returns the path of the rotated (archive) file, or None if `app.log`
    didn't exist. After this call the next log line will recreate a fresh
    `app.log` automatically (the new RotatingFileHandler is reattached
    below).

    Safe to call while the logger is actively writing — we flush + close
    every RotatingFileHandler attached to the thaoresearch logger tree so
    the rename works on Windows (which locks open files).
    """
    log_dir_ = log_dir_ or log_dir()
    try:
        log_dir_.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    current = log_dir_ / "app.log"
    if not current.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = log_dir_ / f"app_{ts}.log"

    # Flush + close every RotatingFileHandler under the thaoresearch tree
    # so Windows lets us rename the open file.
    handlers_to_close = []
    for name in list(logging.root.manager.loggerDict) + ["thaoresearch"]:
        sub = logging.getLogger(name)
        for h in list(sub.handlers):
            try:
                h.flush()
            except Exception:
                pass
            if isinstance(h, RotatingFileHandler):
                handlers_to_close.append((sub, h))
    for sub, h in handlers_to_close:
        try:
            h.close()
        except Exception:
            pass
        try:
            sub.removeHandler(h)
        except Exception:
            pass

    try:
        current.rename(archive)
    except Exception:
        # Fallback: copy + truncate so we never lose the active log.
        try:
            import shutil

            shutil.copy2(current, archive)
            current.write_text("", encoding="utf-8")
        except Exception:
            return None

    # Rebuild handlers — setup_logger() opens a fresh `app.log`.
    global _initialized
    _initialized = False
    setup_logger(name="thaoresearch")
    return archive


_CONSOLE_FORMAT = "[%(levelname)s] %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def setup_logger(
    name: str = "thaoresearch",
    level: int = logging.INFO,
    verbose: bool = False,
) -> logging.Logger:
    """Set up and return the named logger.

    - INFO (or DEBUG if verbose) to rotating file logs/app.log
    - INFO+ to stderr, format simplified for live console
    """
    global _initialized
    logger = logging.getLogger(name)
    if _initialized:
        logger.setLevel(logging.DEBUG if verbose else level)
        return logger

    logger.setLevel(logging.DEBUG if verbose else level)
    logger.propagate = False

    # File handler (rotating).
    #
    # This used to be the single worst line in the app: an unguarded
    # ``LOG_DIR.mkdir()`` raising PermissionError [WinError 5] whenever the
    # exe sat somewhere non-writable (Program Files, a read-only share, a
    # locked USB stick). Release builds are --windowed, so the traceback went
    # nowhere and the user saw the program simply not start.
    #
    # paths.DATA_DIR is write-probed now, so this should already be writable;
    # we still degrade to console-only rather than take the app down, and
    # record why so the UI can show it.
    global FILE_LOGGING_ERROR
    FILE_LOGGING_ERROR = None
    target_dir = log_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target_dir / "app.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
        logger.addHandler(file_handler)
    except Exception as e:
        FILE_LOGGING_ERROR = f"{target_dir}: {type(e).__name__}: {e}"

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(console_handler)

    _initialized = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Get a child logger under 'thaoresearch' namespace."""
    if name:
        return logging.getLogger(f"thaoresearch.{name}")
    return logging.getLogger("thaoresearch")


def mask_token(token: str, visible: int = 4) -> str:
    """Mask a secret token, showing only first/last few chars.

    >>> mask_token("1234567890abcdef")
    '1234***cdef'
    """
    if not token or len(token) <= visible * 2:
        return "***"
    return f"{token[:visible]}***{token[-visible:]}"
