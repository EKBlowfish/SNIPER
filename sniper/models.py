"""Data models shared across the scraper, store and UIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Item:
    """A single marketplace listing."""

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
