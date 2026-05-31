"""Wallapop source — Spanish marketplace expanding across Europe.

Wallapop's API is protected by CloudFront WAF, which blocks both plain
``requests`` and ``curl_cffi``.  This source logs the block and skips
gracefully until a workaround is found.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import requests

from sniper.config import FX
from sniper.models import Item
from sniper.net import REQUEST_TIMEOUT
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)


class WallapopSource(Source):
    name = "Wallapop"
    key_prefix = "WP"
    API = "https://api.wallapop.com/api/v3/general/search"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        if stop_event.is_set():
            return

        # Try curl_cffi first (better TLS fingerprint)
        data = self._try_curl_cffi(term, stop_event)
        if data is None:
            data = self._try_requests(session, term, stop_event, emit)
        if data is None:
            return

        items = data.get("search_objects") or []
        emit.status(f"Wallapop — {len(items)} results")

        added = 0
        for v in items:
            if stop_event.is_set():
                break
            wid = v.get("id")
            title = (v.get("title") or "").strip()
            if not wid or not title:
                continue

            price_eur = _price_to_eur(v)
            url = v.get("web_slug")
            link = f"https://es.wallapop.com/item/{url}" if url else f"https://es.wallapop.com/item/{wid}"

            images = v.get("images") or []
            photo = images[0].get("medium") if images else None
            if not photo and images:
                photo = images[0].get("original") or images[0].get("small")

            ship_eur = None
            shipping = v.get("shipping") or {}
            if shipping.get("item_is_shippable"):
                cost = shipping.get("buyer_shipping_cost") or {}
                if isinstance(cost, dict) and cost.get("amount"):
                    try:
                        ship_eur = round(float(cost["amount"]), 2)
                    except (ValueError, TypeError):
                        pass

            emit.upsert(Item(
                key=f"WP:{wid}",
                source="Wallapop",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=ship_eur,
                total_eur=compute_total(price_eur, ship_eur),
                type="\U0001f6d2 Buy Now",
                thumb_url=photo,
            ))
            added += 1

        emit.status(f"Wallapop listings added: {added}")

    def _try_curl_cffi(self, term: str, stop_event: threading.Event):
        """Attempt the API call with curl_cffi for a better TLS fingerprint."""
        if stop_event.is_set():
            return None
        try:
            from curl_cffi import requests as cf_requests
            import time

            s = cf_requests.Session(impersonate="chrome131")
            r = s.get(
                self.API,
                params={
                    "keywords": term,
                    "filters_source": "search_box",
                    "latitude": "52.3676",
                    "longitude": "4.9041",
                },
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "X-DeviceOS": "0",
                    "Origin": "https://es.wallapop.com",
                    "Referer": "https://es.wallapop.com/",
                },
                timeout=20,
            )
            time.sleep(1.0)
            if r.status_code == 200:
                return r.json()
            log.info("Wallapop curl_cffi HTTP %s", r.status_code)
        except ImportError:
            pass
        except Exception as e:
            log.debug("Wallapop curl_cffi failed: %s", e)
        return None

    def _try_requests(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter):
        """Fall back to plain requests (usually 403)."""
        if stop_event.is_set():
            return None
        try:
            r = session.get(
                self.API,
                params={
                    "keywords": term,
                    "filters_source": "search_box",
                    "latitude": "52.3676",
                    "longitude": "4.9041",
                },
                headers={
                    "Accept": "application/json",
                    "X-DeviceOS": "0",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                emit.status(f"Wallapop HTTP {r.status_code} — blocked by CloudFront WAF (skipping)")
                return None
            return r.json()
        except (requests.RequestException, OSError) as e:
            emit.error(f"Wallapop request failed: {e}")
        except ValueError:
            emit.status("Wallapop — blocked (no JSON in response)")
        return None


def _price_to_eur(item: Dict[str, Any]) -> Optional[float]:
    price = item.get("price")
    currency = item.get("currency_code") or item.get("currency", "EUR")
    if price is None:
        return None
    try:
        return round(float(price) * FX.get(currency, 1.0), 2)
    except (ValueError, TypeError):
        return None
