from __future__ import annotations

import threading
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Item:
    key: str
    source: str
    title: str
    link: str
    price_eur: Optional[float]
    ship_eur: Optional[float]
    total_eur: Optional[float]
    type: str  # "🛒 Buy Now" | "🧷 Auction" | ""
    thumb_url: Optional[str] = None
    thumb_bytes: Optional[bytes] = None
    trend: str = ""


class Store:
    """Thread-safe SQLite wrapper used for storing ad and price data."""

    def __init__(self, db_path: str):
        """Open a SQLite connection and ensure the schema exists."""
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they are missing."""
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
            # Speed up lookups of price history for a single key by indexing the
            # key and timestamp columns. Without this index SQLite would scan the
            # entire table for each query, which becomes increasingly slow as the
            # history grows.
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
        """Return up to ``limit`` most recent prices for the given ad key."""
        with self.lock, self.conn:
            rows = [
                r[0]
                for r in self.conn.execute(
                    """
                    SELECT price FROM price_history
                    WHERE key=?
                    ORDER BY seen_at DESC
                    LIMIT ?
                    """,
                    (key, limit),
                )
            ]
        # Query returns rows in reverse chronological order; flip to ascending
        # so callers receive prices from oldest to newest.
        rows.reverse()
        return rows

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self.lock:
            self.conn.close()

