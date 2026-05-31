"""Leboncoin.fr source — France's largest classifieds site.

Leboncoin is a Next.js app that embeds search results as JSON in
``__NEXT_DATA__``.  We use ``curl_cffi`` for the initial fetch to
bypass bot detection, then parse the embedded JSON directly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)


class LeboncoinSource(Source):
    name = "Leboncoin"
    key_prefix = "LBC"
    BASE = "https://www.leboncoin.fr"

    def _fetch(self, session: requests.Session, url: str, stop_event: threading.Event) -> tuple[str, int]:
        """Try curl_cffi first, then fall back to requests."""
        if stop_event.is_set():
            return "", 0
        try:
            from curl_cffi import requests as cf_requests
            import time

            s = cf_requests.Session(impersonate="chrome131")
            s.headers.update({"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
            try:
                s.get(f"{self.BASE}/", timeout=15)
                time.sleep(1.0)
            except Exception:
                pass
            r = s.get(url, timeout=20)
            time.sleep(1.0)
            return (r.text or "", r.status_code)
        except ImportError:
            pass
        except Exception as e:
            log.warning("Leboncoin curl_cffi failed: %s", e)

        r = polite_get(session, url, stop_event)
        return (getattr(r, "text", "") or "", getattr(r, "status_code", 0))

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        url = f"{self.BASE}/recherche?text={enc}"
        html, status = self._fetch(session, url, stop_event)

        ads = _extract_ads(html)
        emit.status(f"Leboncoin HTTP {status} — {len(ads)} ads")

        added = 0
        for ad in ads:
            if stop_event.is_set():
                break

            list_id = ad.get("list_id")
            title = (ad.get("subject") or "").strip()
            if not list_id or not title:
                continue

            link = ad.get("url") or f"{self.BASE}/ad/offres/{list_id}"
            if not link.startswith("http"):
                link = self.BASE + link

            price_eur = _extract_price(ad)

            thumb = None
            images = ad.get("images")
            if isinstance(images, dict):
                thumb = images.get("thumb_url") or images.get("small_url")
            elif isinstance(images, list) and images:
                thumb = images[0] if isinstance(images[0], str) else None

            emit.upsert(Item(
                key=f"LBC:{list_id}",
                source="Leboncoin",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="\U0001f6d2 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Leboncoin listings added: {added}")


def _extract_ads(html: str) -> list:
    """Pull ad data from the __NEXT_DATA__ JSON embedded in the page."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        data = json.loads(script.string)
        return (
            data.get("props", {})
            .get("pageProps", {})
            .get("searchData", {})
            .get("ads", [])
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log.debug("Leboncoin __NEXT_DATA__ parse failed: %s", e)
        return []


def _extract_price(ad: dict) -> Optional[float]:
    """Extract price in EUR from an ad dict.

    Leboncoin provides ``price_cents`` (integer) and ``price`` (list of ints).
    """
    cents = ad.get("price_cents")
    if cents is not None:
        try:
            return round(int(cents) / 100.0, 2)
        except (ValueError, TypeError):
            pass

    price_list = ad.get("price")
    if isinstance(price_list, list) and price_list:
        try:
            return round(float(price_list[0]), 2)
        except (ValueError, TypeError, IndexError):
            pass

    return None
