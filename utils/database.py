"""SQLite database for deduplication and history."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from utils.paths import DATA_DIR

DB_DIR = DATA_DIR
DB_PATH = DB_DIR / "history.db"

# Registry of all open connections keyed by ``threading.get_ident()``.
# Using a plain dict + lock (instead of ``threading.local``) so that
# ``close_all_connections()`` can close every connection from any thread —
# important for ``Clear Cache``, where the GUI thread needs to close the
# scheduler thread's connection before unlinking the SQLite file on
# Windows (which locks open files).
_lock = threading.Lock()
_connections: dict = {}  # thread_id (int) -> sqlite3.Connection


def _get_connection() -> sqlite3.Connection:
    """Return a connection for the current thread (one per thread)."""
    tid = threading.get_ident()
    conn = _connections.get(tid)
    if conn is None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        with _lock:
            _connections[tid] = conn
    return conn


def close_all_connections() -> int:
    """Close every open connection in every thread. Returns count closed.

    Call before deleting the SQLite file so Windows lets us unlink it.
    """
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
    """Context manager for a database cursor."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Create tables if they don't exist."""
    with get_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_articles (
                article_hash TEXT PRIMARY KEY,
                article_title TEXT,
                article_url TEXT,
                commodity TEXT,
                is_price_alert INTEGER DEFAULT 0,
                is_urgent INTEGER DEFAULT 0,
                source TEXT,
                pub_date TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_to_telegram INTEGER DEFAULT 1,
                sent_to_desktop INTEGER DEFAULT 1,
                article_summary TEXT DEFAULT ''
            )
            """
        )
        # Migration: add article_summary to older DBs that predate this column.
        try:
            cur.execute("ALTER TABLE sent_articles ADD COLUMN article_summary TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sent_at ON sent_articles(sent_at DESC)
            """
        )


def is_sent(article_hash: str) -> bool:
    """Return True if this article hash has been marked sent before."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sent_articles WHERE article_hash = ? LIMIT 1",
            (article_hash,),
        )
        return cur.fetchone() is not None


def mark_sent(
    article_hash: str,
    article_title: str = "",
    article_url: str = "",
    commodity: str = "",
    is_price_alert: bool = False,
    is_urgent: bool = False,
    source: str = "",
    pub_date: Optional[datetime] = None,
    sent_to_telegram: bool = True,
    sent_to_desktop: bool = True,
    article_summary: str = "",
) -> None:
    """Insert article into history (idempotent)."""
    pub_iso = pub_date.isoformat() if pub_date else None
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT OR IGNORE INTO sent_articles (
                article_hash, article_title, article_url, commodity,
                is_price_alert, is_urgent, source, pub_date,
                sent_to_telegram, sent_to_desktop, article_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_hash,
                article_title,
                article_url,
                commodity,
                1 if is_price_alert else 0,
                1 if is_urgent else 0,
                source,
                pub_iso,
                1 if sent_to_telegram else 0,
                1 if sent_to_desktop else 0,
                article_summary or "",
            ),
        )


def get_history(
    limit: int = 200,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
) -> List[Tuple]:
    """Return recent history rows as tuples.

    status_filter: 'price' | 'urgent' | 'keyword' | None
    search: case-insensitive substring match on title
    """
    query = "SELECT * FROM sent_articles WHERE 1=1"
    params: list = []

    if status_filter == "price":
        query += " AND is_price_alert = 1"
    elif status_filter == "urgent":
        query += " AND is_urgent = 1"
    elif status_filter == "keyword":
        query += " AND is_price_alert = 0"

    if search:
        query += " AND LOWER(article_title) LIKE ?"
        params.append(f"%{search.lower()}%")

    query += " ORDER BY sent_at DESC LIMIT ?"
    params.append(limit)

    with get_cursor() as cur:
        cur.execute(query, params)
        return [tuple(row) for row in cur.fetchall()]


def count_sent() -> int:
    """Return total count of sent articles."""
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM sent_articles")
        row = cur.fetchone()
        return int(row["n"]) if row else 0
