"""Marktplaats source — discovers ad URLs then fetches each ad page."""

from __future__ import annotations

import threading

import requests

from sniper.net import polite_get
from sniper.scraper import build_search_urls, discover_mp_urls, parse_mp_ad, truncate
from sniper.sources.base import Emitter, Source


class MarktplaatsSource(Source):
    name = "Marktplaats"
    key_prefix = "MP"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        url = build_search_urls(term)[0]
        r = polite_get(session, url, stop_event)
        html = getattr(r, "text", "") or ""
        status = getattr(r, "status_code", 0)
        urls = discover_mp_urls(html) if html else []
        emit.status(f"Marktplaats HTTP {status} — {len(urls)} ads")

        total = len(urls)
        for i, ad_url in enumerate(urls, 1):
            if stop_event.is_set():
                break
            item, http_status = parse_mp_ad(session, ad_url, stop_event)
            if http_status == 404:
                emit.delete(item.key)
                emit.status(f"Marktplaats {i}/{total}: removed (404)", i, total)
                continue
            emit.upsert(item)
            emit.status(f"Marktplaats {i}/{total}: {truncate(item.title, 70)}", i, total)
