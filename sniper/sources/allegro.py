"""Allegro.pl source — Poland's largest marketplace.

Allegro uses DataDome bot protection that blocks both plain ``requests``
and ``curl_cffi``.  This source attempts the fetch but logs the block and
skips gracefully.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.config import FX
from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total
from sniper.sources.base import Emitter, Source

log = logging.getLogger(__name__)

_PLN_TO_EUR = FX.get("PLN", 0.23)  # ~0.23 EUR per PLN


class AllegroSource(Source):
    name = "Allegro"
    key_prefix = "ALG"
    BASE = "https://allegro.pl"

    def _fetch(self, session: requests.Session, url: str, stop_event: threading.Event) -> tuple[str, int]:
        """Try curl_cffi first, then fall back to requests."""
        if stop_event.is_set():
            return "", 0
        try:
            from curl_cffi import requests as cf_requests

            s = cf_requests.Session(impersonate="chrome131")
            s.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
            try:
                s.get(f"{self.BASE}/", timeout=15)
                time.sleep(1.5)
            except Exception:
                pass
            r = s.get(url, timeout=20)
            time.sleep(1.0)
            html = r.text or ""

            # Detect DataDome interception
            if r.status_code == 403 or (len(html) < 2000 and "enable JS" in html):
                log.info("Allegro blocked by DataDome (HTTP %s)", r.status_code)
                return ("", 403)

            return (html, r.status_code)
        except ImportError:
            pass
        except Exception as e:
            log.warning("Allegro curl_cffi failed: %s", e)

        r = polite_get(session, url, stop_event)
        return (getattr(r, "text", "") or "", getattr(r, "status_code", 0))

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        enc = quote_plus(term)
        url = f"{self.BASE}/listing?string={enc}"
        html, status = self._fetch(session, url, stop_event)

        if status == 403 or not html:
            emit.status(f"Allegro HTTP {status} — blocked by DataDome bot protection (skipping)")
            return

        soup = BeautifulSoup(html, "html.parser")

        # Allegro listing cards
        cards = (
            soup.select("[data-role='offer']")
            or soup.select("article[data-item]")
            or soup.select("[class*='opbox-listing'] article")
            or soup.select("a[href*='/oferta/']")
        )
        emit.status(f"Allegro HTTP {status} — {len(cards)} offers")

        added = 0
        seen: set[str] = set()
        for card in cards:
            if stop_event.is_set():
                break

            link_el = card if card.name == "a" else card.select_one("a[href]")
            if not link_el:
                continue
            href = link_el.get("href", "")

            # Offer IDs from /oferta/title-12345678
            m = re.search(r"-(\d{8,})", href)
            if not m:
                m = re.search(r"/(\d{8,})", href)
            if not m:
                continue
            offer_id = m.group(1)
            if offer_id in seen:
                continue
            seen.add(offer_id)

            link = href.split("?")[0]  # Strip tracking params
            if not link.startswith("http"):
                link = self.BASE + link

            title_el = card.select_one("h2") or card.select_one("[class*='title']")
            title = title_el.get_text(" ", strip=True) if title_el else link_el.get_text(" ", strip=True)[:100]
            if not title:
                continue

            price_el = card.select_one("[class*='price']") or card.select_one("span[aria-label*='cena']")
            price_eur = _parse_pln(price_el.get_text(" ", strip=True)) if price_el else None

            thumb = None
            img = card.select_one("img")
            if img:
                thumb = img.get("src") or img.get("data-src")
                if thumb and thumb.startswith("//"):
                    thumb = "https:" + thumb

            emit.upsert(Item(
                key=f"ALG:{offer_id}",
                source="Allegro",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="\U0001f6d2 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Allegro listings added: {added}")


def _parse_pln(text: str):
    """Parse a PLN price ('123,45 zl') and convert to EUR."""
    m = re.search(r"([\d\s.,]+)\s*(?:zl|PLN)", text, re.I)
    if not m:
        # Fallback: any number
        m = re.search(r"([\d.,]+)", text)
    if not m:
        return None
    try:
        val = m.group(1).replace(" ", "").replace(",", ".")
        return round(float(val) * _PLN_TO_EUR, 2)
    except ValueError:
        return None
