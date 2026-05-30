"""
Database layer — all SQLite operations for the ReadingTime agent.

Manages four tables:

    books         — every book that has ever been on the shelf
    signals       — behavioural signals (liked / neutral) for profiling
    profile       — user preference snapshot (single row, id=1)
    system_state  — key-value flags (e.g. "agent_is_deleting")

All SQL is encapsulated here.  No other module writes raw SQL.
JSON fields (tags, features) are auto-serialized/deserialized.

Usage:
    from readingtime.database import db
    db.init_db()
    db.add_book(...)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from readingtime.config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """Return an ISO-8601 UTC timestamp string for the current moment."""
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj: Any) -> str:
    """Serialize obj to a compact JSON string."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(text: Optional[str]) -> Any:
    """Deserialize a JSON string, returning None for empty input."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """SQLite database wrapper for ReadingTime.

    Module-level singleton ``db`` is created at import time — always use it
    instead of creating new instances.
    """

    def __init__(self) -> None:
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        """Lazy-connect to the SQLite database file.

        The path is ``~/.readingtime/readingtime.db`` by convention.
        Callers can also set ``READINGTIME_DB`` env var to override.
        """
        if self._conn is None:
            import os

            db_path = os.getenv("READINGTIME_DB", "")
            if db_path:
                path = Path(db_path)
            else:
                path = Path("~/.readingtime/readingtime.db").expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(path),
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            logger.debug("Connected to database at %s", path)
        return self._conn

    # -- lifecycle -----------------------------------------------------------

    def init_db(self) -> None:
        """Create all tables if they don't already exist.  Idempotent."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                author          TEXT,
                filename        TEXT NOT NULL UNIQUE,
                added_at        DATETIME NOT NULL,
                removed_at      DATETIME,
                removal_type    TEXT,
                source          TEXT,
                source_id       TEXT,
                language        TEXT DEFAULT 'en',
                tags            TEXT,
                page_count      INTEGER,
                is_protected    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id     INTEGER REFERENCES books(id),
                signal      TEXT NOT NULL,
                features    TEXT,
                created_at  DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profile (
                id              INTEGER PRIMARY KEY,
                liked_tags      TEXT,
                liked_authors   TEXT,
                neutral_tags    TEXT,
                lang_pref       TEXT DEFAULT 'en',
                updated_at      DATETIME
            );

            CREATE TABLE IF NOT EXISTS system_state (
                key     TEXT PRIMARY KEY,
                value   TEXT
            );
            """
        )
        self.conn.commit()
        logger.info("Database tables ensured")

    def close(self) -> None:
        """Close the database connection (called on shutdown)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("Database connection closed")

    # -- books ---------------------------------------------------------------

    def add_book(
        self,
        title: str,
        filename: str,
        *,
        author: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        language: str = "en",
        tags: Optional[list[str]] = None,
        page_count: Optional[int] = None,
    ) -> int:
        """Insert a new book row.  Returns the new row id."""
        cursor = self.conn.execute(
            """
            INSERT INTO books (title, author, filename, added_at, source, source_id,
                               language, tags, page_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                author,
                filename,
                _now(),
                source,
                source_id,
                language,
                _json_dumps(tags or []),
                page_count,
            ),
        )
        self.conn.commit()
        logger.info("Book added: %s (id=%d)", filename, cursor.lastrowid)
        return cursor.lastrowid

    def get_current_books(self) -> list[dict[str, Any]]:
        """Return all books currently on the shelf (removed_at IS NULL)."""
        rows = self.conn.execute(
            "SELECT * FROM books WHERE removed_at IS NULL ORDER BY added_at"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_book_by_filename(self, filename: str) -> Optional[dict[str, Any]]:
        """Look up a book by its filename (basename, no path)."""
        row = self.conn.execute(
            "SELECT * FROM books WHERE filename = ?", (filename,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_book_by_id(self, book_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def mark_removed(self, filename: str, removal_type: str) -> bool:
        """Set ``removed_at`` and ``removal_type`` for a book that left the shelf.

        Returns True if a row was updated, False if the book wasn't found.
        """
        cursor = self.conn.execute(
            """
            UPDATE books SET removed_at = ?, removal_type = ?
            WHERE filename = ? AND removed_at IS NULL
            """,
            (_now(), removal_type, filename),
        )
        self.conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info("Book marked removed: %s (%s)", filename, removal_type)
        else:
            logger.warning("Attempted to mark removed a missing book: %s", filename)
        return updated

    def get_book_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return all books (on-shelf + removed), newest first."""
        rows = self.conn.execute(
            "SELECT * FROM books ORDER BY added_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def extend_protection(self, filename: str, days: int = 7) -> bool:
        """Set is_protected=1 on a book (e.g. file was locked → user reading)."""
        cursor = self.conn.execute(
            "UPDATE books SET is_protected = 1 WHERE filename = ?",
            (filename,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # -- signals -------------------------------------------------------------

    def record_signal(
        self,
        book_id: int,
        signal: str,
        features: Optional[dict] = None,
    ) -> int:
        """Record a behavioural signal ('liked' or 'neutral')."""
        cursor = self.conn.execute(
            """
            INSERT INTO signals (book_id, signal, features, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (book_id, signal, _json_dumps(features or {}), _now()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_recent_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT s.*, b.title, b.author, b.filename
            FROM signals s LEFT JOIN books b ON s.book_id = b.id
            ORDER BY s.created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- profile -------------------------------------------------------------

    def upsert_profile(
        self,
        liked_tags: Optional[list[str]] = None,
        liked_authors: Optional[list[str]] = None,
        neutral_tags: Optional[list[str]] = None,
        lang_pref: str = "en",
    ) -> None:
        """Insert or update the single profile row (id=1)."""
        self.conn.execute(
            """
            INSERT INTO profile (id, liked_tags, liked_authors, neutral_tags,
                                 lang_pref, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                liked_tags    = COALESCE(excluded.liked_tags, profile.liked_tags),
                liked_authors = COALESCE(excluded.liked_authors, profile.liked_authors),
                neutral_tags  = COALESCE(excluded.neutral_tags, profile.neutral_tags),
                lang_pref     = COALESCE(excluded.lang_pref, profile.lang_pref),
                updated_at    = excluded.updated_at
            """,
            (
                _json_dumps(liked_tags or []),
                _json_dumps(liked_authors or []),
                _json_dumps(neutral_tags or []),
                lang_pref,
                _now(),
            ),
        )
        self.conn.commit()

    def get_profile(self) -> Optional[dict[str, Any]]:
        """Return the profile row as a dict, or None if no profile yet."""
        row = self.conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        if row is None:
            return None
        d = _row_to_dict(row)
        # Deserialize JSON lists
        d["liked_tags"] = _json_loads(row["liked_tags"]) or []
        d["liked_authors"] = _json_loads(row["liked_authors"]) or []
        d["neutral_tags"] = _json_loads(row["neutral_tags"]) or []
        return d

    # -- system_state --------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        """Write a key-value flag (used to distinguish system vs user deletes)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def clear_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM system_state WHERE key = ?", (key,))
        self.conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict.

    JSON text fields (tags, features) are auto-deserialized.
    """
    d = dict(row)
    for key in ("tags", "features"):
        if key in d and isinstance(d[key], str):
            d[key] = _json_loads(d[key])
    return d


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
db = Database()
