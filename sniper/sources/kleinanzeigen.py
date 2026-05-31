"""Kleinanzeigen.de source — parses server-rendered classifieds cards."""

from __future__ import annotations

import threading
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from sniper.models import Item
from sniper.net import polite_get
from sniper.scraper import compute_total, parse_money_to_eur
from sniper.sources.base import Emitter, Source


class KleinanzeigenSource(Source):
    name = "Kleinanzeigen"
    key_prefix = "KA"
    BASE = "https://www.kleinanzeigen.de"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        slug = quote(term.strip().lower().replace(" ", "-"))
        url = f"{self.BASE}/s-{slug}/k0"
        r = polite_get(session, url, stop_event)
        html = getattr(r, "text", "") or ""
        status = getattr(r, "status_code", 0)

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("article.aditem")
        emit.status(f"Kleinanzeigen HTTP {status} — {len(cards)} cards")

        added = 0
        for card in cards:
            if stop_event.is_set():
                break
            adid = card.get("data-adid")
            if not adid:
                continue

            # Skip "Gesuch" (wanted) ads — they're buyers, not listings.
            top = card.select_one(".aditem-main--top--left")
            if top and "gesuch" in top.get_text(" ", strip=True).lower():
                continue

            title_el = (
                card.select_one("a.ellipsis")
                or card.select_one(".text-module-begin a")
                or card.select_one("h2 a")
            )
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title:
                continue

            href = card.get("data-href") or (title_el.get("href") if title_el else "")
            link = self.BASE + href if href.startswith("/") else href

            price_el = card.select_one(
                ".aditem-main--middle--price-shipping--price, .aditem-main--middle--price"
            )
            price_eur = parse_money_to_eur(price_el.get_text(" ", strip=True)) if price_el else None

            thumb = None
            img = card.select_one("img")
            if img:
                thumb = img.get("src") or img.get("data-imgsrc") or img.get("data-src")
                if thumb and thumb.startswith("//"):
                    thumb = "https:" + thumb

            emit.upsert(Item(
                key=f"KA:{adid}",
                source="Kleinanzeigen",
                title=title,
                link=link,
                price_eur=price_eur,
                ship_eur=None,
                total_eur=compute_total(price_eur, None),
                type="🛒 Buy Now",
                thumb_url=thumb,
            ))
            added += 1

        emit.status(f"Kleinanzeigen listings added: {added}")
