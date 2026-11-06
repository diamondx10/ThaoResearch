"""Price tracking — stores the most recent price per commodity in data/prices.db."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from utils import paths

logger = logging.getLogger("thaoresearch.utils.price_tracker")

def db_dir() -> Path:
    """Resolved data directory, read at call time (the user can move it)."""
    return paths.DATA_DIR


def db_path() -> Path:
    return db_dir() / "prices.db"


def __getattr__(name: str):
    """Back-compat for callers still reading ``price_tracker.DB_PATH``."""
    if name == "DB_PATH":
        return db_path()
    if name == "DB_DIR":
        return db_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
MAX_HISTORY = 30

# Registry of all open connections keyed by ``threading.get_ident()``.
# See utils/database.py for rationale (Clear Cache needs to close the
# scheduler thread's connection from the GUI thread).
_lock = threading.Lock()
_connections: dict = {}  # thread_id (int) -> sqlite3.Connection


def _get_connection() -> sqlite3.Connection:
    tid = threading.get_ident()
    conn = _connections.get(tid)
    if conn is None:
        db_dir().mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path()), timeout=10)
        conn.row_factory = sqlite3.Row
        with _lock:
            _connections[tid] = conn
    return conn


def close_all_connections() -> int:
    """Close every open connection in every thread. Returns count closed."""
    with _lock:
        items = list(_connections.items())
        _connections.clear()
    closed = 0
    for tid, conn in items:
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.close()
            closed += 1
        except Exception:
            pass
    return closed


@contextmanager
def get_cursor() -> Iterator[sqlite3.Cursor]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    with get_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS commodity_prices (
                commodity TEXT PRIMARY KEY,
                last_price REAL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                history TEXT
            )
            """
        )


def get_last_price(commodity: str) -> Optional[float]:
    """Return the most recent price for a commodity, or None if never seen."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT last_price FROM commodity_prices WHERE commodity = ?", (commodity,)
        )
        row = cur.fetchone()
        return float(row["last_price"]) if row and row["last_price"] is not None else None


def get_history(commodity: str) -> List[float]:
    """Return the most recent N prices for a commodity (oldest first)."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT history FROM commodity_prices WHERE commodity = ?", (commodity,)
        )
        row = cur.fetchone()
        if not row or not row["history"]:
            return []
        try:
            return json.loads(row["history"])
        except (json.JSONDecodeError, TypeError):
            return []


def record_price(
    commodity: str, price: float
) -> Tuple[Optional[float], Optional[float]]:
    """Record a new price. Returns (previous_price, delta_pct) where delta_pct is None on first sighting."""
    history = get_history(commodity)
    previous = history[-1] if history else None
    delta_pct: Optional[float] = None
    if previous and previous > 0:
        delta_pct = (price - previous) / previous * 100.0

    history.append(price)
    history = history[-MAX_HISTORY:]

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO commodity_prices (commodity, last_price, last_updated, history)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(commodity) DO UPDATE SET
                last_price = excluded.last_price,
                last_updated = excluded.last_updated,
                history = excluded.history
            """,
            (commodity, price, datetime.utcnow().isoformat(), json.dumps(history)),
        )
    return previous, delta_pct
