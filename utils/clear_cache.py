"""Helpers to wipe local cache files used by ThaoResearch.

The Dashboard exposes a "Clear cache" button that resets the application to
a fresh state:
    - data/history.db  (sent-articles DB — used for deduplication)
    - data/prices.db   (commodity price history)
    - logs/*           (all log files)

This is intentionally destructive (the user has to confirm). It is safe to
call while the app is running — connections will be reopened on next use.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger("thaoresearch.utils.clear_cache")

DATA_DIR = Path("data")
LOGS_DIR = Path("logs")


def _safe_remove(p: Path) -> bool:
    """Try to remove a file. Returns True if it was removed."""
    try:
        if p.is_file() or p.is_symlink():
            p.unlink()
            return True
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.warning("Failed to remove %s: %s", p, e)
    return False


def clear_local_data(
    data_dir: Path = DATA_DIR,
    logs_dir: Path = LOGS_DIR,
    include_logs: bool = True,
) -> List[Tuple[str, str]]:
    """Remove the local DBs (and optionally logs).

    Returns a list of (path, status) tuples describing what happened:
        ("data/history.db", "removed")
        ("logs/app.log",    "removed")
        ("logs/",           "not found")
    """
    results: List[Tuple[str, str]] = []

    # SQLite DBs
    for db_name in ("history.db", "prices.db"):
        db_path = data_dir / db_name
        if db_path.exists():
            if _safe_remove(db_path):
                results.append((str(db_path), "removed"))
            else:
                results.append((str(db_path), "failed"))
        else:
            results.append((str(db_path), "not found"))

    # Logs (keep the directory itself)
    if include_logs:
        if logs_dir.exists():
            for entry in sorted(logs_dir.iterdir()):
                try:
                    if entry.is_file() or entry.is_symlink():
                        entry.unlink()
                        results.append((str(entry), "removed"))
                    elif entry.is_dir():
                        shutil.rmtree(entry)
                        results.append((str(entry), "removed (dir)"))
                except Exception as e:
                    logger.warning("Failed to remove %s: %s", entry, e)
                    results.append((str(entry), "failed"))
        else:
            results.append((str(logs_dir), "not found"))

    return results
