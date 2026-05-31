#!/usr/bin/env python3
"""Flask web version of ZX Spectrum Watcher."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

from sniper.config import (
    APP_NAME,
    AUTO_FETCH_ENABLED,
    AUTO_FETCH_MINUTES,
    DB_FILE,
    DEAL_THRESHOLD_EUR,
    ENABLED_SOURCES,
    MSG_DELETE,
    MSG_DONE,
    MSG_UPSERT,
)
from sniper.models import Item
from sniper.relevance import is_spectrum_computer
from sniper.scraper import sparkline, worker_fetch
from sniper.settings import load_settings, save_settings
from sniper.sources import source_names
from sniper.store import Store

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
store = Store(DB_FILE)
# One-off cleanup of any title-less placeholder rows from older scraper runs.
store.purge_untitled()

search_term: str = load_settings().get("search_term") or "zx spectrum"

worker_thread: Optional[threading.Thread] = None
stop_event = threading.Event()
fetch_running = False
fetch_lock = threading.Lock()

# SSE clients — each is a Queue that receives (event_type, data_dict)
sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()

# Thumbnail cache: key -> bytes
thumb_cache: Dict[str, bytes] = {}
thumb_lock = threading.Lock()

# Auto-fetch timer
auto_fetch_timer: Optional[threading.Timer] = None


# ---------------------------------------------------------------------------
# SSE broadcasting
# ---------------------------------------------------------------------------

def broadcast(event_type: str, data: Dict[str, Any]) -> None:
    """Push an SSE event to all connected clients and handle side-effects."""
    global fetch_running

    # Cache thumbnails from UPSERT items
    if event_type == MSG_UPSERT:
        it: Item = data.get("item")
        if it and it.thumb_bytes:
            with thumb_lock:
                thumb_cache[it.key] = it.thumb_bytes

    # Serialise for SSE
    if event_type == MSG_UPSERT:
        it = data["item"]
        payload = _item_to_dict(it)
    elif event_type == MSG_DELETE:
        payload = {"key": data["key"]}
    elif event_type == MSG_DONE:
        with fetch_lock:
            fetch_running = False
        payload = {"text": data.get("text", "Fetch complete")}
    else:
        payload = data

    with sse_lock:
        dead = []
        for i, q in enumerate(sse_clients):
            try:
                q.put_nowait((event_type.lower(), payload))
            except queue.Full:
                dead.append(i)
        for i in reversed(dead):
            sse_clients.pop(i)


def _item_to_dict(it: Item) -> dict:
    """Convert an Item to a JSON-friendly dict for the frontend."""
    is_deal = it.total_eur is not None and it.total_eur <= DEAL_THRESHOLD_EUR
    return {
        "key": it.key,
        "source": it.source,
        "title": it.title or "",
        "link": it.link or "",
        "price_eur": it.price_eur,
        "ship_eur": it.ship_eur,
        "total_eur": it.total_eur,
        "type": it.type or "",
        "trend": it.trend or "",
        "thumb_url": f"/api/thumbnail/{it.key}" if it.thumb_url or it.thumb_bytes else "",
        "is_deal": is_deal,
        "is_relevant": is_spectrum_computer(it.title or ""),
    }


# ---------------------------------------------------------------------------
# Fetch control
# ---------------------------------------------------------------------------

def start_fetch() -> bool:
    """Start a background fetch. Returns True if started, False if already running."""
    global worker_thread, fetch_running

    with fetch_lock:
        if fetch_running:
            return False
        fetch_running = True

    stop_event.clear()
    worker_thread = threading.Thread(
        target=worker_fetch,
        args=(broadcast, stop_event, store, search_term, ENABLED_SOURCES),
        daemon=True,
    )
    worker_thread.start()
    return True


def stop_current_fetch() -> None:
    """Signal the worker to stop."""
    stop_event.set()


def _auto_fetch_loop() -> None:
    """Recurring auto-fetch timer callback."""
    global auto_fetch_timer
    with fetch_lock:
        running = fetch_running
    if not running:
        start_fetch()
    auto_fetch_timer = threading.Timer(AUTO_FETCH_MINUTES * 60, _auto_fetch_loop)
    auto_fetch_timer.daemon = True
    auto_fetch_timer.start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/listings")
def api_listings():
    """Return all stored listings as JSON."""
    rows = []
    with store.lock, store.conn:
        cursor = store.conn.execute(
            "SELECT key, source, title, link, last_price, last_ship, last_total, type "
            "FROM ads WHERE TRIM(COALESCE(title, '')) != '' ORDER BY last_seen DESC"
        )
        for row in cursor:
            key, source, title, link, price, ship, total, typ = row
            hist = store.get_price_history(key)
            trend = sparkline(hist)
            is_deal = total is not None and total <= DEAL_THRESHOLD_EUR
            has_thumb = key in thumb_cache
            rows.append({
                "key": key,
                "source": source or "",
                "title": title or "",
                "link": link or "",
                "price_eur": price,
                "ship_eur": ship,
                "total_eur": total,
                "type": typ or "",
                "trend": trend,
                "thumb_url": f"/api/thumbnail/{key}" if has_thumb else "",
                "is_deal": is_deal,
                "is_relevant": is_spectrum_computer(title or ""),
            })
    return jsonify({
        "listings": rows,
        "search_term": search_term,
        "fetch_running": fetch_running,
        "deal_threshold_eur": DEAL_THRESHOLD_EUR,
        "sources": source_names(),
    })


@app.route("/api/listings/csv")
def api_csv():
    """Export listings as CSV download."""
    output = io.StringIO()
    w = csv.writer(output)
    cols = ("Title", "Price EUR", "Ship EUR", "Total EUR", "Type", "Trend", "Source", "Link")
    w.writerow(cols)
    with store.lock, store.conn:
        cursor = store.conn.execute(
            "SELECT title, last_price, last_ship, last_total, type, key, source, link "
            "FROM ads WHERE TRIM(COALESCE(title, '')) != '' ORDER BY last_seen DESC"
        )
        for row in cursor:
            title, price, ship, total, typ, key, source, link = row
            hist = store.get_price_history(key)
            trend = sparkline(hist)
            w.writerow([
                title or "",
                f"{price:.2f}" if price is not None else "",
                f"{ship:.2f}" if ship is not None else "",
                f"{total:.2f}" if total is not None else "",
                typ or "",
                trend,
                source or "",
                link or "",
            ])
    resp = Response(output.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=listings.csv"
    return resp


@app.route("/api/search", methods=["POST"])
def api_search():
    """Set search term and start fetch."""
    global search_term
    data = request.get_json(silent=True) or {}
    term = (data.get("term") or "").strip()
    if not term:
        return jsonify({"error": "No search term provided"}), 400
    search_term = term
    save_settings({"search_term": term})
    started = start_fetch()
    return jsonify({"ok": True, "started": started, "term": search_term})


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    """Start a fetch with current search term."""
    started = start_fetch()
    return jsonify({"ok": True, "started": started})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Signal the worker to stop."""
    stop_current_fetch()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Read or update persisted user settings (theme, filters, sort, notify)."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        return jsonify(save_settings(data))
    return jsonify(load_settings())


@app.route("/api/events")
def api_events():
    """SSE endpoint — streams events to the browser."""
    client_queue: queue.Queue = queue.Queue(maxsize=200)
    with sse_lock:
        sse_clients.append(client_queue)

    def stream():
        try:
            while True:
                try:
                    event_type, data = client_queue.get(timeout=30)
                    yield f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                try:
                    sse_clients.remove(client_queue)
                except ValueError:
                    pass

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.route("/api/thumbnail/<path:key>")
def api_thumbnail(key: str):
    """Serve a cached thumbnail image."""
    with thumb_lock:
        data = thumb_cache.get(key)
    if not data:
        # Return 1x1 transparent PNG
        pixel = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return Response(pixel, mimetype="image/png")
    return Response(data, mimetype="image/jpeg")


@app.route("/api/fetch-thumb/<path:key>")
def api_fetch_thumb(key: str):
    """Try to discover and cache a thumbnail for an item that has none.

    Visits the item's listing page, extracts the og:image (or first img),
    fetches the image bytes, caches them, and returns the image.
    """
    import re
    from bs4 import BeautifulSoup
    from sniper.net import make_session, fetch_bytes

    # Already cached?
    with thumb_lock:
        if key in thumb_cache:
            return Response(thumb_cache[key], mimetype="image/jpeg")

    # Look up the listing link from the DB
    with store.lock, store.conn:
        row = store.conn.execute("SELECT link FROM ads WHERE key=?", (key,)).fetchone()
    if not row or not row[0]:
        return Response(status=404)

    link = row[0]
    stop = threading.Event()
    session = make_session()

    try:
        # Try curl_cffi first for better compatibility
        try:
            from curl_cffi import requests as cf_requests
            r = cf_requests.get(link, impersonate="chrome131", timeout=15)
            html = r.text or ""
            status = r.status_code
        except (ImportError, Exception):
            from sniper.net import polite_get
            r = polite_get(session, link, stop)
            html = getattr(r, "text", "") or ""
            status = getattr(r, "status_code", 0)

        if status != 200 or not html:
            return Response(status=404)

        soup = BeautifulSoup(html, "html.parser")

        # Try og:image first (most reliable)
        thumb_url = None
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            thumb_url = og["content"]
        # Fallback: first real image
        if not thumb_url:
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if any(skip in src.lower() for skip in ["logo", "icon", "sprite", "pixel", "1x1", "blank"]):
                    continue
                thumb_url = src
                break

        if not thumb_url:
            return Response(status=404)

        if thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url
        elif thumb_url.startswith("/"):
            # Build absolute URL from the listing domain
            m = re.match(r"(https?://[^/]+)", link)
            if m:
                thumb_url = m.group(1) + thumb_url

        img_bytes = fetch_bytes(session, thumb_url, stop)
        if not img_bytes:
            return Response(status=404)

        # Cache it
        with thumb_lock:
            thumb_cache[key] = img_bytes

        return Response(img_bytes, mimetype="image/jpeg")

    except Exception as e:
        log.debug("fetch-thumb %s failed: %s", key, e)
        return Response(status=404)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Auto-fetch on startup
    if AUTO_FETCH_ENABLED:
        timer = threading.Timer(1.0, lambda: (start_fetch(), _auto_fetch_loop()))
        timer.daemon = True
        timer.start()

    log.info("%s — web UI at http://127.0.0.1:5000", APP_NAME)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
