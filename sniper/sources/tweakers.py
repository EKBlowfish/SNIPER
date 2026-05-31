"""Tweakers Vraag & Aanbod source — Dutch tech enthusiast marketplace.

Tweakers requires cookie consent before showing results, so we use
``curl_cffi`` to impersonate a real browser.  The search form uses
``keyword=`` (not ``query=``).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total, parse_money_to_eur
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)


class TweakersSource(Source):
    name = "Tweakers"
    key_prefix = "TW"
    BASE = "https://tweakers.net"

    def _fetch(self, session: requests.Session, url: str, stop_event: threading.Event) -> tuple[str, int]:
        """Try curl_cffi first (to pass the consent gate), then fall back."""
        if stop_event.is_set():
            return "", 0
        try:
            from curl_cffi import requests as cf_requests

            s = cf_requests.Session(impersonate="chrome131")
            s.headers.update({"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"})
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
            log.warning("Tweakers curl_cffi failed: %s", e)

        r = polite_get(session, url, stop_event)
        return (getattr(r, "text", "") or "", getattr(r, "status_code", 0))

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        # Tweakers V&A search uses ``keyword=`` parameter.
        url = f"{self.BASE}/aanbod/zoeken/?keyword={enc}"
        html, status = self._fetch(session, url, stop_event)

        soup = BeautifulSoup(html, "html.parser")

        # Results are in a <table class="listing valisting ...">
        table = soup.select_one("table.listing")
        rows = table.select("tr") if table else []

        emit.status(f"Tweakers HTTP {status} — {len(rows)} listings")

        added = 0
        seen: set[str] = set()
        for row in rows:
            if stop_event.is_set():
                break

            # Title is in <p class="title"> > <a>
            title_a = row.select_one("p.title a[href]")
            if not title_a:
                continue
            href = title_a.get("href", "")
            title = title_a.get_text(" ", strip=True)
            if not title:
                continue

            # Extract ad ID from URL like /aanbod/4148448/...
            m = re.search(r"/aanbod/(\d+)/", href)
            if not m:
                continue
            ad_id = m.group(1)
            if ad_id in seen:
                continue
            seen.add(ad_id)

            link = href if href.startswith("http") else self.BASE + href

            # Price is in <td class="vaprice">
            price_td = row.select_one("td.vaprice")
            price_eur = parse_money_to_eur(price_td.get_text(" ", strip=True)) if price_td else None

            # Thumbnail is in <td class="pwimage"> > <img>
            thumb = None
            img = row.select_one("td.pwimage img")
            if img:
                thumb = img.get("src") or img.get("data-src")
                if thumb and thumb.startswith("//"):
                    thumb = "https:" + thumb

            emit.upsert(Item(
                key=f"TW:{ad_id}",
                source="Tweakers",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="\U0001f6d2 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Tweakers listings added: {added}")
