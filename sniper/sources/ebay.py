"""eBay source — uses the curl_cffi-backed fetch to get past eBay's 403s."""

from __future__ import annotations

import threading

import requests

from sniper.scraper import _fetch_ebay_html, build_search_urls, parse_ebay_results
from sniper.sources.base import Emitter, Source


class EbaySource(Source):
    name = "eBay"
    key_prefix = "EBAY"

    def search(self, session: requests.Session, term: str, stop_event: threading.Event, emit: Emitter) -> None:
        url = build_search_urls(term)[1]
        html, status = _fetch_ebay_html(session, url, stop_event)
        items = parse_ebay_results(session, html) if html else []
        emit.status(f"eBay HTTP {status} — {len(items)} results")
        for item in items:
            if stop_event.is_set():
                break
            emit.upsert(item)
        emit.status(f"eBay listings added: {len(items)}")
