"""Shpock source — European mobile marketplace (popular in AT/DE/UK).

Shpock is a Next.js app backed by Apollo GraphQL.  The search results
are embedded in ``__NEXT_DATA__`` as an Apollo cache, so we parse the
page HTML and extract items from the cache rather than calling the
GraphQL API directly (whose schema changes frequently).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.config import FX
from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)

# CDN base for Shpock media thumbnails.
SHPOCK_MEDIA_CDN = "https://m1.secondhandapp.at/2.0"


class ShpockSource(Source):
    name = "Shpock"
    key_prefix = "SP"

    def _fetch(self, term: str, stop_event: threading.Event) -> tuple[str, int]:
        """Fetch the Shpock search page with curl_cffi or plain requests."""
        if stop_event.is_set():
            return "", 0
        enc = quote_plus(term)
        url = f"https://www.shpock.com/en-gb/results?q={enc}"
        try:
            from curl_cffi import requests as cf_requests

            s = cf_requests.Session(impersonate="chrome131")
            s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
            r = s.get(url, timeout=20)
            time.sleep(1.0)
            return (r.text or "", r.status_code)
        except ImportError:
            pass
        except Exception as e:
            log.warning("Shpock curl_cffi failed: %s", e)

        from sniper.net import make_session

        session = make_session()
        r = polite_get(session, url, stop_event)
        return (getattr(r, "text", "") or "", getattr(r, "status_code", 0))

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        if stop_event.is_set():
            return

        html, status = self._fetch(term, stop_event)
        items = _extract_items(html)
        emit.status(f"Shpock HTTP {status} — {len(items)} results")

        added = 0
        for item_data in items:
            if stop_event.is_set():
                break

            sid = item_data.get("id")
            title = (item_data.get("title") or "").strip()
            if not sid or not title:
                continue

            price_eur = _price_to_eur(item_data)
            path = item_data.get("path") or item_data.get("canonicalURL") or ""
            if path.startswith("http"):
                link = path
            elif path:
                link = f"https://www.shpock.com{path}"
            else:
                link = f"https://www.shpock.com/en-gb/i/{sid}"

            thumb = _get_thumb(item_data)

            emit.upsert(Item(
                key=f"SP:{sid}",
                source="Shpock",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="\U0001f6d2 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Shpock listings added: {added}")


def _extract_items(html: str) -> List[Dict[str, Any]]:
    """Extract item data from Shpock's ``__NEXT_DATA__`` Apollo cache."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        data = json.loads(script.string)
        apollo = (
            data.get("props", {})
            .get("pageProps", {})
            .get("apolloState", {})
        )
        if not apollo:
            return []

        # Collect all ItemSummary objects from the Apollo cache.
        items: List[Dict[str, Any]] = []
        for key, value in apollo.items():
            if not isinstance(value, dict):
                continue
            if value.get("__typename") == "ItemSummary":
                # Resolve media refs to get thumbnail IDs
                media = value.get("media") or []
                resolved_media = []
                for m in media:
                    if isinstance(m, dict) and "__ref" in m:
                        ref_data = apollo.get(m["__ref"], {})
                        resolved_media.append(ref_data)
                    elif isinstance(m, dict):
                        resolved_media.append(m)
                value["_resolved_media"] = resolved_media
                items.append(value)

        return items
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log.debug("Shpock __NEXT_DATA__ parse failed: %s", e)
        return []


def _price_to_eur(item: Dict[str, Any]) -> Optional[float]:
    """Convert Shpock price (flat number + currency string) to EUR."""
    price = item.get("price")
    if price is None:
        return None
    currency = (item.get("currency") or "eur").upper()
    try:
        return round(float(price) * FX.get(currency, 1.0), 2)
    except (ValueError, TypeError):
        return None


def _get_thumb(item: Dict[str, Any]) -> Optional[str]:
    """Build a thumbnail URL from the resolved media list."""
    media = item.get("_resolved_media") or item.get("media") or []
    for m in media:
        if isinstance(m, dict):
            # Direct thumbnailUrl (from old schema)
            thumb = m.get("thumbnailUrl")
            if thumb:
                return thumb
            # Build URL from media ID
            media_id = m.get("id")
            if media_id:
                return f"{SHPOCK_MEDIA_CDN}/{media_id}"
    return None
