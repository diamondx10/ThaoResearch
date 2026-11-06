"""Helpers to wipe local cache files used by ThaoResearch.

The Dashboard exposes a "Clear cache" button that resets the application to
a fresh state:
    - data/history.db  (sent-articles DB — used for deduplication)
    - data/prices.db   (commodity price history)
    - logs/app.log + app_*.log archives

This is intentionally destructive (the user has to confirm). It is safe to
call while the app is running — connections will be reopened on next use.

The naive ``Path.unlink()`` approach fails on Windows when the SQLite file
is still open (either by our scheduler thread or by the GUI's log tail
reader). To make ``Clear cache`` bullet-proof we:

    1. Close every open SQLite connection (history + prices) across all
       threads BEFORE touching the file. SQLite holds an OS-level lock
       on Windows; the file can't be deleted while it's open.
    2. Flush + close every rotating file handler (logger) BEFORE trying
       to unlink ``app.log``. Same Windows file-locking story.
    3. Try ``Path.unlink()``. If a stubborn handle still holds the file,
       fall back to ``os.replace`` over a temp path (which always works
       on Windows because rename doesn't need delete access on the
       source).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from utils import paths

logger = logging.getLogger("thaoresearch.utils.clear_cache")


def __getattr__(name: str):
    """Resolve DATA_DIR / LOGS_DIR against ``utils.paths`` at access time.

    These used to be ``APP_ROOT / "data"`` and ``APP_ROOT / "logs"``, which
    went stale the moment the data folder became user-selectable -- Clear
    Cache would then report "not found" while the real databases sat
    untouched somewhere else.
    """
    if name == "DATA_DIR":
        return paths.DATA_DIR
    if name == "LOGS_DIR":
        return paths.LOG_DIR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _force_close_handlers():
    """Flush + close every rotating file handler under the ``thaoresearch``
    logger tree so ``app.log`` can be unlinked on Windows.

    Mirrors the logic in ``utils.logger.rotate_log_file`` so we don't have
    to rotate first. We just close the handlers; ``setup_logger`` will
    reopen them lazily on the next ``get_logger()`` call.
    """
    try:
        from logging.handlers import RotatingFileHandler
    except ImportError:  # pragma: no cover
        return 0
    closed = 0
    candidates = list(logging.root.manager.loggerDict.keys()) + ["thaoresearch"]
    for name in candidates:
        try:
            sub = logging.getLogger(name)
        except Exception:
            continue
        for h in list(sub.handlers):
            try:
                h.flush()
            except Exception:
                pass
            if isinstance(h, RotatingFileHandler):
                try:
                    h.close()
                    closed += 1
                except Exception:
                    pass
                try:
                    sub.removeHandler(h)
                except Exception:
                    pass
    return closed


def _force_remove(p: Path) -> bool:
    """Try hard to remove ``p``. Returns True on success.

    Strategy:
      1. If it's a symlink, unlink directly (no follow).
      2. Try ``Path.unlink()`` (raises PermissionError if locked).
      3. Force a ``gc.collect()`` so any lingering Python refs to
         the file's Connection are released, then retry unlink.
      4. Fallback: ``os.replace`` to a uniquely-named temp file in
         TEMP. On Windows ``os.replace`` to a *different* filename
         succeeds even when the source has open handles — rename
         doesn't need delete access on the source, only write on the
         destination dir.
    """
    import gc as _gc

    for attempt in range(3):
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
                return True
        except FileNotFoundError:
            return False
        except IsADirectoryError:
            return False
        except (PermissionError, OSError):
            # Force GC then retry — APScheduler may still hold a
            # Python reference to a Connection that survived our
            # ``close_all_connections()`` call.
            try:
                _gc.collect()
            except Exception:
                pass
            import time as _time
            _time.sleep(0.1)

    # Fallback: rename to a uniquely-named temp file in TEMP, then delete
    # that. On Windows ``os.replace`` to a DIFFERENT filename always
    # succeeds even if the source is open — rename doesn't need delete
    # access on the source, only write on the destination dir. We pick a
    # unique name so we don't collide with an existing temp file.
    try:
        tmp_dir = Path(tempfile.gettempdir())
        for i in range(10):
            tmp = tmp_dir / f"{p.name}.trash_{os.getpid()}_{i}"
            if not tmp.exists():
                break
        os.replace(str(p), str(tmp))
        logger.info("Removed %s via rename-to-temp fallback (%s)", p, tmp)
        # Try to clean up the temp copy too, but don't fail if it's locked.
        try:
            tmp.unlink()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("Fallback rename also failed for %s: %s", p, e)
        return False


def clear_local_data(
    data_dir: Optional[Path] = None,
    logs_dir: Optional[Path] = None,
    include_logs: bool = True,
) -> List[Tuple[str, str]]:
    """Remove the local DBs (and optionally logs).

    ``data_dir`` / ``logs_dir`` default to the *current* resolved locations
    rather than a snapshot taken at import, so Clear Cache still finds the
    databases after the user moves the data folder.

    Returns a list of (path, status) tuples describing what happened:
        ("data/history.db", "removed")
        ("logs/app.log",    "removed")
        ("logs/",           "not found")
    """
    data_dir = data_dir or paths.DATA_DIR
    logs_dir = logs_dir or paths.LOG_DIR
    results: List[Tuple[str, str]] = []

    # 1. Close SQLite connections (history + prices) so the OS lets us
    #    unlink the DB files. This MUST happen before any unlink attempt.
    try:
        from utils import database, price_tracker

        n_hist = database.close_all_connections()
        n_prices = price_tracker.close_all_connections()
        logger.info(
            "Clear cache: closed %d history + %d price_tracker connections",
            n_hist, n_prices,
        )
    except Exception as e:
        logger.debug("close_all_connections failed: %s", e)

    # 2. Close logger handlers so ``app.log`` is no longer held open.
    if include_logs:
        try:
            n_handlers = _force_close_handlers()
            logger.info("Clear cache: closed %d rotating log handlers", n_handlers)
        except Exception as e:
            logger.debug("close log handlers failed: %s", e)

    # 3. SQLite DBs
    for db_name in ("history.db", "prices.db"):
        db_path = data_dir / db_name
        if db_path.exists():
            if _force_remove(db_path):
                results.append((str(db_path), "removed"))
            else:
                results.append((str(db_path), "failed"))
        else:
            results.append((str(db_path), "not found"))

    # 4. Logs (keep the directory itself). Reopen handlers after this so
    #    subsequent log lines land in a fresh ``app.log``.
    if include_logs:
        if logs_dir.exists():
            for entry in sorted(logs_dir.iterdir()):
                try:
                    if entry.is_file() or entry.is_symlink():
                        if _force_remove(entry):
                            results.append((str(entry), "removed"))
                        else:
                            results.append((str(entry), "failed"))
                    elif entry.is_dir():
                        shutil.rmtree(entry)
                        results.append((str(entry), "removed (dir)"))
                except Exception as e:
                    logger.warning("Failed to remove %s: %s", entry, e)
                    results.append((str(entry), "failed"))
        else:
            results.append((str(logs_dir), "not found"))

    # 5. Reinitialize the open DBs and log handlers so the rest of the app
    #    keeps working without restart. ``init_db`` is safe to call —
    #    ``CREATE TABLE IF NOT EXISTS``.
    try:
        from utils import database as _db
        _db.init_db()
    except Exception as e:
        logger.debug("re-init history DB failed: %s", e)
    try:
        from utils import price_tracker as _pt
        _pt.init_db()
    except Exception as e:
        logger.debug("re-init prices DB failed: %s", e)
    if include_logs:
        try:
            from utils.logger import setup_logger
            setup_logger()
        except Exception as e:
            logger.debug("re-setup logger failed: %s", e)

    return results
