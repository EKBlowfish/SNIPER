"""Vinted source — uses the site's JSON catalog API.

The catalog endpoint needs the ``access_token_web`` cookie that's only set
after loading the site, so we bootstrap the session with a homepage GET. We
also force ``Accept-Encoding: gzip`` because Vinted otherwise replies with
brotli, which plain ``requests`` can't decode without an extra package.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import requests

from sniper.config import FX
from sniper.models import Item
from sniper.net import REQUEST_TIMEOUT
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source


class VintedSource(Source):
    name = "Vinted"
    key_prefix = "VINTED"
    HOST = "https://www.vinted.nl"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        if stop_event.is_set():
            return

        # Bootstrap the auth cookie by loading the site once.
        try:
            session.get(self.HOST + "/", timeout=REQUEST_TIMEOUT)
        except (requests.RequestException, OSError):
            pass

        try:
            r = session.get(
                self.HOST + "/api/v2/catalog/items",
                params={"search_text": term, "per_page": "48", "order": "newest_first"},
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept-Encoding": "gzip, deflate",
                    "Referer": f"{self.HOST}/catalog?search_text={quote_plus(term)}",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.RequestException, OSError) as e:
            emit.error(f"Vinted request failed: {e}")
            return

        try:
            data = r.json()
        except ValueError:
            emit.status(f"Vinted HTTP {r.status_code} — no JSON (blocked?)")
            return

        items = data.get("items") or []
        emit.status(f"Vinted HTTP {r.status_code} — {len(items)} results")

        added = 0
        for v in items:
            if stop_event.is_set():
                break
            vid = v.get("id")
            title = (v.get("title") or "").strip()
            if not vid or not title:
                continue

            price_eur = _price_to_eur(v.get("price"), v)
            url = v.get("url") or f"{self.HOST}/items/{vid}"
            photo = (v.get("photo") or {}).get("url")

            emit.upsert(Item(
                key=f"VINTED:{vid}",
                source="Vinted",
                title=title,
                link=url,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="🛒 Buy Now",
                thumb_url=photo,
            ))
            added += 1

        emit.status(f"Vinted listings added: {added}")


def _price_to_eur(price: Any, item: Dict[str, Any]) -> Optional[float]:
    """Vinted prices come as {amount, currency_code} (or a bare number)."""
    if isinstance(price, dict):
        amount = price.get("amount")
        currency = price.get("currency_code", "EUR")
    else:
        amount = price
        currency = item.get("currency", "EUR")
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return round(value * FX.get(currency, 1.0), 2)
