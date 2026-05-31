"""Gumtree UK source — UK classifieds, lots of ZX Spectrum hardware."""

from __future__ import annotations

import re
import threading
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.config import FX
from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source


class GumtreeSource(Source):
    name = "Gumtree"
    key_prefix = "GT"
    BASE = "https://www.gumtree.com"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        url = f"{self.BASE}/search?search_category=all&q={enc}"
        r = polite_get(session, url, stop_event)
        html = getattr(r, "text", "") or ""
        status = getattr(r, "status_code", 0)

        soup = BeautifulSoup(html, "html.parser")

        cards = (
            soup.select("[data-q='search-result']")
            or soup.select("article.listing-maxi")
            or soup.select("[class*='result-card']")
            or soup.select("a[href*='/p/']")
        )
        emit.status(f"Gumtree HTTP {status} — {len(cards)} ads")

        added = 0
        seen = set()
        for card in cards:
            if stop_event.is_set():
                break

            link_el = card if card.name == "a" else card.select_one("a[href]")
            if not link_el:
                continue
            href = link_el.get("href", "")

            m = re.search(r"/(\d{8,})", href)
            if not m:
                # Try shorter IDs
                m = re.search(r"/p/([^/]+)/(\d+)", href)
                if m:
                    ad_id = m.group(2)
                else:
                    continue
            else:
                ad_id = m.group(1)

            if ad_id in seen:
                continue
            seen.add(ad_id)

            link = href if href.startswith("http") else self.BASE + href

            title_el = card.select_one("h2") or card.select_one("[class*='title']") or card.select_one("[data-q='tile-title']")
            title = title_el.get_text(" ", strip=True) if title_el else link_el.get_text(" ", strip=True)[:100]
            if not title:
                continue

            price_el = card.select_one("[class*='price']") or card.select_one("[data-q='tile-price']")
            price_eur = _parse_gbp(price_el.get_text(" ", strip=True)) if price_el else None

            thumb = None
            img = card.select_one("img")
            if img:
                thumb = img.get("src") or img.get("data-src")
                if thumb and thumb.startswith("//"):
                    thumb = "https:" + thumb

            emit.upsert(Item(
                key=f"GT:{ad_id}",
                source="Gumtree",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="🛒 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Gumtree listings added: {added}")


def _parse_gbp(text: str):
    """Parse a GBP price and convert to EUR."""
    m = re.search(r"£\s*([\d.,]+)", text)
    if not m:
        return None
    try:
        val = m.group(1).replace(",", "")
        return round(float(val) * FX.get("GBP", 1.16), 2)
    except ValueError:
        return None
