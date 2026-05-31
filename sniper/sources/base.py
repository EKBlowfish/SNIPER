"""Source plug-in interface and the Emitter that funnels results to UI + DB.

Each marketplace is a :class:`Source`. A source only knows how to turn a
search term into :class:`~sniper.models.Item` objects and hand them to an
:class:`Emitter`; the Emitter owns the shared side-effects (thumbnail fetch,
DB upsert, trend sparkline, broadcast) so every source behaves identically.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional

import requests

from sniper.config import MSG_DELETE, MSG_ERROR, MSG_STATUS, MSG_UPSERT
from sniper.models import Item
from sniper.net import fetch_bytes
from sniper.scraper import sparkline
from sniper.store import Store


class Emitter:
    """Routes a source's output to the database and the UI broadcast callback."""

    def __init__(
        self,
        broadcast: Callable[[str, Dict[str, Any]], None],
        db: Store,
        session: requests.Session,
        stop_event: threading.Event,
    ):
        self.broadcast = broadcast
        self.db = db
        self.session = session
        self.stop_event = stop_event

    def status(self, text: str, current: Optional[int] = None, total: Optional[int] = None) -> None:
        data: Dict[str, Any] = {"text": text}
        if current is not None:
            data["current"] = current
        if total is not None:
            data["total"] = total
        self.broadcast(MSG_STATUS, data)

    def error(self, text: str) -> None:
        self.broadcast(MSG_ERROR, {"text": text})

    def upsert(self, item: Item) -> None:
        """Fetch the thumbnail, persist, compute the trend, then broadcast."""
        if item.thumb_url and not item.thumb_bytes:
            item.thumb_bytes = fetch_bytes(self.session, item.thumb_url, self.stop_event)
        try:
            self.db.upsert_item(item)
            item.trend = sparkline(self.db.get_price_history(item.key))
        except sqlite3.Error as e:
            self.broadcast(MSG_ERROR, {"text": f"DB error for {item.key}: {e}"})
        self.broadcast(MSG_UPSERT, {"item": item})

    def delete(self, key: str) -> None:
        try:
            self.db.delete_item(key)
        except sqlite3.Error as e:
            self.broadcast(MSG_ERROR, {"text": f"DB delete error for {key}: {e}"})
        self.broadcast(MSG_DELETE, {"key": key})


class Source:
    """Base class for a marketplace source."""

    name: str = ""
    key_prefix: str = ""

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        """Fetch listings for *term* and feed them to *emit*."""
        raise NotImplementedError
