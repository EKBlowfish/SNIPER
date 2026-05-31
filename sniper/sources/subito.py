"""Subito.it source — Italy's main classifieds site.

Subito is a Next.js app that embeds listing data in ``__NEXT_DATA__``.
We use ``curl_cffi`` for the fetch (plain requests often get blocked),
then parse the embedded JSON directly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)


class SubitoSource(Source):
    name = "Subito"
    key_prefix = "SUB"
    BASE = "https://www.subito.it"

    def _fetch(self, session: requests.Session, url: str, stop_event: threading.Event) -> tuple[str, int]:
        """Try curl_cffi first, then fall back to requests."""
        if stop_event.is_set():
            return "", 0
        try:
            from curl_cffi import requests as cf_requests

            s = cf_requests.Session(impersonate="chrome131")
            s.headers.update({"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
            try:
                s.get(f"{self.BASE}/", timeout=15)
                time.sleep(1.2)
            except Exception:
                pass
            r = s.get(url, timeout=20)
            time.sleep(1.0)
            return (r.text or "", r.status_code)
        except ImportError:
            pass
        except Exception as e:
            log.warning("Subito curl_cffi failed: %s", e)

        r = polite_get(session, url, stop_event)
        return (getattr(r, "text", "") or "", getattr(r, "status_code", 0))

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        url = f"{self.BASE}/annunci-italia/vendita/usato/?q={enc}"
        html, status = self._fetch(session, url, stop_event)

        items = _extract_items(html)
        emit.status(f"Subito HTTP {status} — {len(items)} ads")

        added = 0
        for item_data in items:
            if stop_event.is_set():
                break

            # Skip non-ad items (promoted banners, etc.)
            if item_data.get("kind") != "AdItem":
                continue

            # Extract ad ID from URN like "id:ad:UUID:list:648906859"
            urn = item_data.get("urn", "")
            ad_id = _extract_id(urn)
            title = (item_data.get("subject") or "").strip()
            if not ad_id or not title:
                continue

            urls = item_data.get("urls") or {}
            link = urls.get("default") or urls.get("mobile") or ""
            if not link:
                continue

            price_eur = _extract_price(item_data)
            ship_eur = _extract_shipping(item_data)

            thumb = None
            images = item_data.get("images") or []
            if images and isinstance(images[0], dict):
                cdn_base = images[0].get("cdnBaseUrl", "")
                if cdn_base:
                    thumb = cdn_base + "?rule=gallery-small"

            emit.upsert(Item(
                key=f"SUB:{ad_id}",
                source="Subito",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=ship_eur,
                total_eur=compute_total(price_eur, ship_eur),
                type="\U0001f6d2 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Subito listings added: {added}")


def _extract_items(html: str) -> list:
    """Pull item data from the __NEXT_DATA__ JSON embedded in the page."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        data = json.loads(script.string)
        return (
            data.get("props", {})
            .get("pageProps", {})
            .get("initialState", {})
            .get("items", {})
            .get("originalList", [])
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log.debug("Subito __NEXT_DATA__ parse failed: %s", e)
        return []


def _extract_id(urn: str) -> str:
    """Extract numeric ad ID from a Subito URN string.

    URN format: ``id:ad:UUID:list:648906859``
    """
    m = re.search(r":(\d{6,})$", urn)
    if m:
        return m.group(1)
    # Fallback: any long number in the URN
    m = re.search(r"(\d{6,})", urn)
    return m.group(1) if m else ""


def _extract_price(item: dict) -> Optional[float]:
    """Extract the price from the /price feature."""
    features = item.get("features") or {}
    price_feat = features.get("/price")
    if not isinstance(price_feat, dict):
        return None
    values = price_feat.get("values") or []
    if values and isinstance(values[0], dict):
        key = values[0].get("key")
        if key is not None:
            try:
                return round(float(str(key).replace(",", ".")), 2)
            except (ValueError, TypeError):
                pass
    return None


def _extract_shipping(item: dict) -> Optional[float]:
    """Extract shipping cost from the /item_shipping_cost_tuttosubito feature."""
    features = item.get("features") or {}
    ship_feat = features.get("/item_shipping_cost_tuttosubito")
    if not isinstance(ship_feat, dict):
        return None
    values = ship_feat.get("values") or []
    if values and isinstance(values[0], dict):
        key = values[0].get("key")
        if key is not None:
            try:
                return round(float(str(key).replace(",", ".")), 2)
            except (ValueError, TypeError):
                pass
    return None
