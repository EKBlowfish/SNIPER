"""Thread-safe SQLite persistence for listings and their price history."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List

from sniper.models import Item

log = logging.getLogger(__name__)


class Store:
    """Thread-safe SQLite wrapper used for storing ad and price data."""

    def __init__(self, db_path: str):
        """Open a SQLite connection and ensure the schema exists."""
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they are missing."""
        with self.lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ads(
                    key TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    link TEXT,
                    last_price REAL,
                    last_ship REAL,
                    last_total REAL,
                    type TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history(
                    key TEXT,
                    seen_at TEXT,
                    price REAL
                )
                """
            )
            # Speed up per-key price-history lookups; without this index SQLite
            # scans the whole table on every query, which gets slow as history
            # grows.
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_price_history_key_seen_at
                ON price_history (key, seen_at)
                """
            )

    def upsert_item(self, it: Item) -> None:
        """Insert or update an Item and record its price history."""
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO ads(key, source, title, link, last_price, last_ship, last_total, type, first_seen, last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    source=excluded.source,
                    title=excluded.title,
                    link=excluded.link,
                    last_price=excluded.last_price,
                    last_ship=excluded.last_ship,
                    last_total=excluded.last_total,
                    type=excluded.type,
                    last_seen=excluded.last_seen
                """,
                (
                    it.key,
                    it.source,
                    it.title,
                    it.link,
                    it.price_eur,
                    it.ship_eur,
                    it.total_eur,
                    it.type,
                    now,
                    now,
                ),
            )
            price_for_hist = it.total_eur if (it.total_eur is not None) else it.price_eur
            if price_for_hist is not None:
                self.conn.execute(
                    "INSERT INTO price_history(key, seen_at, price) VALUES(?,?,?)",
                    (it.key, now, price_for_hist),
                )

    def get_price_history(self, key: str, limit: int = 32) -> List[float]:
        """Return up to ``limit`` most recent prices (oldest-first) for an ad key."""
        with self.lock, self.conn:
            return [
                r[0]
                for r in self.conn.execute(
                    """
                    SELECT price FROM (
                        SELECT price, seen_at FROM price_history
                        WHERE key=?
                        ORDER BY seen_at DESC
                        LIMIT ?
                    ) sub
                    ORDER BY seen_at ASC
                    """,
                    (key, limit),
                )
            ]

    def delete_item(self, key: str) -> None:
        """Remove an ad and its price history from the database."""
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM price_history WHERE key=?", (key,))
            self.conn.execute("DELETE FROM ads WHERE key=?", (key,))

    def get_stale_keys(self, max_age_days: int = 7, limit: int = 10) -> List[str]:
        """Return keys for listings not seen in *max_age_days*, oldest first.

        Returns at most *limit* keys so the caller can spread cleanup across
        multiple fetch cycles instead of doing a big batch delete.
        """
        with self.lock, self.conn:
            cutoff = datetime.now(timezone.utc).isoformat()
            return [
                r[0]
                for r in self.conn.execute(
                    """
                    SELECT key FROM ads
                    WHERE last_seen < datetime(?, '-' || ? || ' days')
                    ORDER BY last_seen ASC
                    LIMIT ?
                    """,
                    (cutoff, max_age_days, limit),
                )
            ]

    def purge_untitled(self) -> int:
        """Delete listings that have no usable title.

        Older scraper runs could persist placeholder rows (a link but no
        title or price) when a results page failed to parse. This cleans
        those out and returns the number of rows removed.
        """
        with self.lock, self.conn:
            keys = [
                r[0]
                for r in self.conn.execute(
                    "SELECT key FROM ads WHERE title IS NULL OR TRIM(title) = ''"
                )
            ]
            for key in keys:
                self.conn.execute("DELETE FROM price_history WHERE key=?", (key,))
                self.conn.execute("DELETE FROM ads WHERE key=?", (key,))
        if keys:
            log.info("Purged %d untitled listing(s).", len(keys))
        return len(keys)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self.lock:
            self.conn.close()
