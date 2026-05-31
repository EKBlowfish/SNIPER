"""2dehands.be source — Belgian classifieds (sister site to Marktplaats).

2dehands is a Next.js app that embeds listing data in a ``__NEXT_DATA__``
script tag, so we parse the JSON directly rather than scraping the DOM.
"""

from __future__ import annotations

import json
import logging
import threading
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)


class TweedehandsSource(Source):
    name = "2dehands"
    key_prefix = "2DH"
    BASE = "https://www.2dehands.be"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        url = f"{self.BASE}/q/{enc}/"
        r = polite_get(session, url, stop_event)
        html = getattr(r, "text", "") or ""
        status = getattr(r, "status_code", 0)

        listings = _extract_listings(html)
        emit.status(f"2dehands HTTP {status} — {len(listings)} listings")

        added = 0
        for item_data in listings:
            if stop_event.is_set():
                break

            item_id = item_data.get("itemId", "")
            title = (item_data.get("title") or "").strip()
            if not item_id or not title:
                continue

            vip_url = item_data.get("vipUrl") or ""
            link = vip_url if vip_url.startswith("http") else self.BASE + vip_url

            price_eur = _extract_price(item_data.get("priceInfo"))
            listing_type = _extract_type(item_data.get("priceInfo"))

            thumb = None
            image_urls = item_data.get("imageUrls") or []
            if image_urls:
                thumb = image_urls[0]
                if thumb.startswith("//"):
                    thumb = "https:" + thumb
            if not thumb:
                pictures = item_data.get("pictures") or []
                if pictures and isinstance(pictures[0], dict):
                    thumb = pictures[0].get("url", "")
                    if thumb and thumb.startswith("//"):
                        thumb = "https:" + thumb

            emit.upsert(Item(
                key=f"2DH:{item_id}",
                source="2dehands",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type=listing_type,
                thumb_url=thumb or None,
            ))
            added += 1

        emit.status(f"2dehands listings added: {added}")


def _extract_listings(html: str) -> list:
    """Pull listing data from the __NEXT_DATA__ JSON embedded in the page."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        data = json.loads(script.string)
        return (
            data.get("props", {})
            .get("pageProps", {})
            .get("searchRequestAndResponse", {})
            .get("listings", [])
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log.debug("2dehands __NEXT_DATA__ parse failed: %s", e)
        return []


def _extract_price(price_info: dict | None) -> float | None:
    """Convert priceInfo.priceCents to EUR."""
    if not price_info or not isinstance(price_info, dict):
        return None
    cents = price_info.get("priceCents")
    if cents is None:
        return None
    try:
        return round(int(cents) / 100.0, 2)
    except (ValueError, TypeError):
        return None


def _extract_type(price_info: dict | None) -> str:
    """Determine listing type from priceInfo.priceType."""
    if not price_info or not isinstance(price_info, dict):
        return ""
    price_type = (price_info.get("priceType") or "").upper()
    if "BID" in price_type:
        return "\U0001f9f7 Auction"  # pin emoji
    return "\U0001f6d2 Buy Now"  # cart emoji
