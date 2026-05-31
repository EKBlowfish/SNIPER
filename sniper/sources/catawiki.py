"""Catawiki source — European auction house with vintage computing lots.

Catawiki blocks plain ``requests`` with HTTP 403, so we use ``curl_cffi``
to impersonate a real browser.  The search endpoint ``/buyer/search?q=``
redirects to ``/en/s?q=`` and returns a Next.js page with lot data
embedded in ``__NEXT_DATA__``.
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

from sniper.config import FX
from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)


class CatawikiSource(Source):
    name = "Catawiki"
    key_prefix = "CW"
    BASE = "https://www.catawiki.com"

    def _fetch(self, url: str, stop_event: threading.Event) -> tuple[str, int]:
        """Fetch via curl_cffi (required — plain requests get 403)."""
        if stop_event.is_set():
            return "", 0
        try:
            from curl_cffi import requests as cf_requests

            s = cf_requests.Session(impersonate="chrome131")
            s.headers.update({"Accept-Language": "en-US,en;q=0.9"})
            try:
                s.get(f"{self.BASE}/", timeout=15)
                time.sleep(1.2)
            except Exception:
                pass
            r = s.get(url, timeout=20, allow_redirects=True)
            time.sleep(1.0)
            return (r.text or "", r.status_code)
        except ImportError:
            log.warning("curl_cffi not installed — Catawiki requires it to bypass bot protection")
            return "", 0
        except Exception as e:
            log.warning("Catawiki curl_cffi failed: %s", e)
            return "", 0

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        # /buyer/search redirects to /en/s?q= and works through bot protection.
        url = f"{self.BASE}/buyer/search?q={enc}"
        html, status = self._fetch(url, stop_event)

        if not html:
            emit.status(f"Catawiki HTTP {status} — blocked or unavailable (curl_cffi required)")
            return

        lots = _extract_lots(html)
        emit.status(f"Catawiki HTTP {status} — {len(lots)} lots")

        added = 0
        for lot in lots:
            if stop_event.is_set():
                break

            lot_id = lot.get("id")
            title = (lot.get("title") or "").strip()
            if not lot_id or not title:
                continue

            lot_url = lot.get("url") or ""
            link = lot_url if lot_url.startswith("http") else self.BASE + lot_url

            # Price: check buyNow first, then live auction bid
            price_eur = _extract_price(lot)

            thumb = lot.get("thumbImageUrl") or lot.get("originalImageUrl")

            # Determine type: auction vs buy-now
            buy_now = lot.get("buyNow")
            if buy_now and isinstance(buy_now, dict):
                listing_type = "\U0001f6d2 Buy Now"
            else:
                listing_type = "\U0001f9f7 Auction"

            emit.upsert(Item(
                key=f"CW:{lot_id}",
                source="Catawiki",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type=listing_type,
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Catawiki listings added: {added}")


def _extract_lots(html: str) -> list:
    """Pull lot data from the __NEXT_DATA__ JSON embedded in the page."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        data = json.loads(script.string)
        return (
            data.get("props", {})
            .get("pageProps", {})
            .get("searchLots", {})
            .get("lots", [])
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log.debug("Catawiki __NEXT_DATA__ parse failed: %s", e)
        return []


def _extract_price(lot: dict) -> Optional[float]:
    """Extract price from a Catawiki lot.

    Buy-now lots have ``buyNow.price_eur``.  Live auction lots may have
    current-bid data in ``live``.  Many upcoming lots have no price yet.
    """
    buy_now = lot.get("buyNow")
    if isinstance(buy_now, dict):
        price = buy_now.get("price_eur") or buy_now.get("price")
        if price is not None:
            try:
                return round(float(price), 2)
            except (ValueError, TypeError):
                pass

    live = lot.get("live")
    if isinstance(live, dict):
        bid = live.get("currentBidAmount") or live.get("current_bid_amount")
        if bid is not None:
            try:
                return round(float(bid), 2)
            except (ValueError, TypeError):
                pass

    return None
