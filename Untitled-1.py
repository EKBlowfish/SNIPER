#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZX Spectrum Watcher — Marktplaats + eBay
Single-file Tkinter desktop app that scrapes listings and shows them in a GUI.

Deps: requests, beautifulsoup4, Pillow
Tested on Python 3.10+

Features:
- Toolbar: Fetch Now (F5), Stop (Esc), Open Ad, Export CSV
- Results table: ttk.Treeview with image in #0 column, sortable headers
- Console pane: timestamped log in a vertical PanedWindow under the table
- Status bar: status text + progress bar (indeterminate/determinate), mirrored to window title
 - Auto-fetch interval configurable via env vars
- Threaded worker + Queue messages (STATUS, UPSERT, ERROR, DONE)
- SQLite storage with locking and price history; sparkline trend column
- EUR currency parsing and FX conversion
"""

from __future__ import annotations

import os
import re
import io
import sys
import csv
import time
import json
import math
import queue
import atexit
import sqlite3
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# =========================
# Config
# =========================

APP_NAME = "ZX Spectrum Watcher — Marktplaats + eBay"
DEFAULT_WINDOW_SIZE = "1280x800"

# Networking/session
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ACCEPT_LANG = "nl-NL,nl;q=0.9,en;q=0.8"

SEARCH_MP = "https://www.marktplaats.nl/l/computers-en-software/vintage-computers/q/zx+spectrum/"
SEARCH_EBAY = "https://www.ebay.nl/sch/i.html?_nkw=zx+spectrum&_sacat=11189"

REQUEST_TIMEOUT = 20
POLITE_DELAY_SEC = 1.2         # delay between requests
RETRY_BACKOFF = [0.0, 1.0, 2.5]  # seconds per retry attempt (0, 1, 2.5)
THUMB_SIZE = (56, 56)

# Auto-fetch every N minutes (configurable via env)
#   AUTO_FETCH_ENABLED: "1"/"true" to enable (default True)
#   AUTO_FETCH_MINUTES: interval in minutes (default 15)
AUTO_FETCH_ENABLED = os.getenv("AUTO_FETCH_ENABLED", "true").lower() in ("1", "true", "yes")
AUTO_FETCH_MINUTES = int(os.getenv("AUTO_FETCH_MINUTES", "15"))
AUTO_FETCH_MS = AUTO_FETCH_MINUTES * 60 * 1000

# EUR conversion (override via env if desired, e.g. FX_GBP_EUR=1.16)
FX = {
    "EUR": float(os.getenv("FX_EUR_EUR", "1.0")),
    "GBP": float(os.getenv("FX_GBP_EUR", "1.16")),
    "USD": float(os.getenv("FX_USD_EUR", "0.92")),
    "AUD": float(os.getenv("FX_AUD_EUR", "0.60")),
    "CAD": float(os.getenv("FX_CAD_EUR", "0.68")),
}
CURRENCY_SYMBOLS = {
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "$": "USD", "USD": "USD", "US$": "USD",
    "AUD": "AUD", "A$": "AUD",
    "CAD": "CAD", "C$": "CAD",
}

# Row highlighting for "deal" threshold (total ≤ threshold)
DEAL_THRESHOLD_EUR = 60.0

DB_FILE = "ads.sqlite3"

# Queue message types
MSG_STATUS = "STATUS"
MSG_UPSERT = "UPSERT"
MSG_ERROR = "ERROR"
MSG_DONE = "DONE"

# =========================
# Helpers & Data classes
# =========================

@dataclass
class Item:
    key: str
    source: str
    title: str
    link: str
    price_eur: Optional[float]
    ship_eur: Optional[float]
    total_eur: Optional[float]
    type: str  # "🛒 Buy Now" | "🧷 Auction" | ""
    thumb_url: Optional[str] = None
    thumb_bytes: Optional[bytes] = None
    trend: str = ""

# =========================
# DB Store with locking
# =========================

class Store:
    def __init__(self, db_path: str):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._ensure_schema()

    def _ensure_schema(self):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ads(
                    key TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    link TEXT,
                    last_price REAL,
                    last_ship REAL,
                    last_total REAL,
                    type TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_history(
                    key TEXT,
                    seen_at TEXT,
                    price REAL
                )
            """)
            cur.close()

    def upsert_item(self, it: Item) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT key FROM ads WHERE key = ?", (it.key,))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute("""
                    UPDATE ads SET
                        source=?, title=?, link=?, last_price=?, last_ship=?, last_total=?, type=?, last_seen=?
                    WHERE key=?
                """, (it.source, it.title, it.link, it.price_eur, it.ship_eur, it.total_eur, it.type, now, it.key))
            else:
                cur.execute("""
                    INSERT INTO ads(key, source, title, link, last_price, last_ship, last_total, type, first_seen, last_seen)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (it.key, it.source, it.title, it.link, it.price_eur, it.ship_eur, it.total_eur, it.type, now, now))
            # Append to history if we have a price figure (store total if available, else price)
            price_for_hist = it.total_eur if (it.total_eur is not None) else it.price_eur
            if price_for_hist is not None:
                cur.execute("INSERT INTO price_history(key, seen_at, price) VALUES(?,?,?)",
                            (it.key, now, price_for_hist))
            self.conn.commit()
            cur.close()

    def get_price_history(self, key: str, limit: int = 32) -> List[float]:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT price FROM price_history
                WHERE key=?
                ORDER BY seen_at ASC
            """, (key,))
            rows = [r[0] for r in cur.fetchall()]
            cur.close()
        if len(rows) > limit:
            # Downsample if too long: take evenly spaced
            idxs = [int(i * (len(rows)-1) / (limit-1)) for i in range(limit)]
            rows = [rows[i] for i in idxs]
        return rows

    def close(self):
        with self.lock:
            self.conn.close()

# =========================
# Currency & parsing
# =========================

MONEY_RE = re.compile(r"([€£$]|EUR|GBP|USD|AUD|CAD|US\$|A\$|C\$)?\s*([0-9]{1,3}(?:[.,\s][0-9]{3})*|[0-9]+)(?:[.,]([0-9]{1,2}))?", re.I)

def parse_money_to_eur(txt: Optional[str]) -> Optional[float]:
    """Parse money string and convert to EUR using static FX rates."""
    if not txt:
        return None
    s = txt.strip()
    # Identify currency code/symbol
    curr = None
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in s:
            curr = code
            break
    # Explicit code at the end e.g., "12.34 USD"
    if not curr:
        for code in ("EUR", "GBP", "USD", "AUD", "CAD"):
            if re.search(rf"\b{code}\b", s, re.I):
                curr = code
                break
    if not curr:
        curr = "EUR"  # assume EUR if unknown on nl-NL sites

    m = MONEY_RE.search(s)
    if not m:
        return None

    _, intpart, frac = m.groups()
    # Remove thousands separators: dots, commas, spaces
    clean_int = re.sub(r"[.,\s]", "", intpart)
    value = float(clean_int)
    if frac:
        value += float(frac) / (10 ** len(frac))

    fx = FX.get(curr, 1.0)
    return round(value * fx, 2)

def compute_total(price_eur: Optional[float], ship_eur: Optional[float]) -> Optional[float]:
    if price_eur is None and ship_eur is None:
        return None
    return round((price_eur or 0.0) + (ship_eur or 0.0), 2)

# =========================
# Sparkline
# =========================

SPARK_BARS = "▁▂▃▄▅▆▇"

def sparkline(values: List[float]) -> str:
    if not values:
        return ""
    vmin = min(values)
    vmax = max(values)
    if vmin == vmax:
        return SPARK_BARS[0] * min(len(values), 16)
    out = []
    for v in values[-16:]:  # last up to 16 points
        idx = int((v - vmin) / (vmax - vmin) * (len(SPARK_BARS) - 1))
        out.append(SPARK_BARS[idx])
    return "".join(out)

# =========================
# HTTP helpers
# =========================

class DummyResponse:
    def __init__(self, url: str, status_code: int = 0, text: str = "", content: bytes = b""):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = content

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": DESKTOP_UA,
        "Accept-Language": ACCEPT_LANG,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    })
    return s

def polite_get(session: requests.Session, url: str, stop_event: threading.Event) -> requests.Response | DummyResponse:
    """Get with simple retry/backoff. Always return a response-like object."""
    for i, back in enumerate(RETRY_BACKOFF):
        if stop_event.is_set():
            break
        if back:
            time.sleep(back)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            time.sleep(POLITE_DELAY_SEC)
            return resp
        except Exception:
            # Continue to next retry
            continue
    # If all retries failed:
    return DummyResponse(url=url, status_code=0, text="", content=b"")

def fetch_bytes(session: requests.Session, url: str, stop_event: threading.Event) -> Optional[bytes]:
    if not url:
        return None
    if stop_event.is_set():
        return None
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY_SEC / 2)
        if getattr(r, "status_code", 0) == 200:
            return r.content
    except Exception:
        return None
    return None

# =========================
# Scrapers
# =========================

MP_AD_RE = re.compile(r"/v/[^\"'\s]+/(m\d+)-", re.I)
MP_ITEMLIST_RE = re.compile(r'"itemListElement"\s*:\s*(\[[^\]]*\])', re.S)

EBAY_ITM_ID_RE = re.compile(r"/itm/(\d+)")
EBAY_ID_FROM_DATA_RE = re.compile(r'"itemId"\s*:\s*"(\d+)"')

def discover_mp_urls(html: str) -> List[str]:
    urls: List[str] = []

    # 1) scan anchors
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/v/" in href and "/m" in href:
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://www.marktplaats.nl" + href
            urls.append(href)

    # 2) regex over raw HTML
    for m in MP_AD_RE.finditer(html):
        ad_id = m.group(1)
        # try to reconstruct URL if not already collected
        # We can't get the full path reliably; skip if already present
        # (anchors likely caught most)
        # This step still helps if anchors were missing but regex hits include a full path
    urls = list(dict.fromkeys(urls))  # de-dup preserve order

    # 3) JSON-LD itemListElement fallback
    mjson = MP_ITEMLIST_RE.search(html)
    if mjson:
        try:
            arr = json.loads(mjson.group(1))
            for el in arr:
                item = el.get("item") or {}
                u = item.get("@id") or item.get("url")
                if isinstance(u, str) and "/v/" in u:
                    if u.startswith("//"):
                        u = "https:" + u
                    elif u.startswith("/"):
                        u = "https://www.marktplaats.nl" + u
                    urls.append(u)
        except Exception:
            pass

    # Final de-dup
    urls = list(dict.fromkeys(urls))
    return urls

def parse_mp_ad(session: requests.Session, url: str, stop_event: threading.Event) -> Item:
    r = polite_get(session, url, stop_event)
    status = getattr(r, "status_code", 0)
    html = getattr(r, "text", "") or ""
    ad_id_match = re.search(r"/(m\d+)-", url)
    ad_id = ad_id_match.group(1) if ad_id_match else f"m{abs(hash(url))%10**10}"
    key = f"MP:{ad_id}"

    title = ""
    price_eur = None
    ship_eur = None
    type_s = ""

    if status == 200 and html:
        soup = BeautifulSoup(html, "html.parser")

        # Title
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
        if not title:
            mt = soup.find("meta", property="og:title")
            if mt and mt.get("content"):
                title = mt["content"].strip()

        # Price or auction detection
        # Look for typical price containers
        price_text = ""
        # Common: data-test="price" or span with "asking-price" or meta itemprop
        price_el = (soup.select_one('[data-test="price"]')
                    or soup.find("span", string=re.compile(r"€|EUR|Prijs|Vraagprijs", re.I))
                    or soup.find(attrs={"itemprop": "price"}))
        if price_el:
            price_text = price_el.get_text(" ", strip=True)
        # Dutch "Bieden" => auction
        if re.search(r"\bbieden\b", html, re.I):
            type_s = "🧷 Auction"
        else:
            type_s = "🛒 Buy Now"

        price_eur = parse_money_to_eur(price_text)

        # Shipping: look for Verzenden/PostNL or shipping price near "Verzenden"
        ship_text = ""
        ship_candidates = soup.find_all(string=re.compile(r"Verzenden|PostNL|Verzendkosten", re.I))
        if ship_candidates:
            # Check nearby for numbers
            for s in ship_candidates:
                parent_txt = s.parent.get_text(" ", strip=True) if hasattr(s, "parent") else str(s)
                mny = MONEY_RE.search(parent_txt)
                if mny:
                    ship_text = parent_txt
                    break
        if not ship_text:
            # Sometimes shipping shows as "Ophalen" (pickup) no price; keep None
            pass
        ship_eur = parse_money_to_eur(ship_text)

        # Thumbnail: og:image first
        thumb_url = None
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            thumb_url = og["content"]
        if not thumb_url:
            imgel = soup.find("img")
            if imgel and imgel.get("src"):
                thumb_url = imgel["src"]
        if thumb_url and thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

    else:
        title = f"(HTTP {status})"

    total_eur = compute_total(price_eur, ship_eur)
    return Item(
        key=key,
        source="Marktplaats",
        title=title or "",
        link=url,
        price_eur=price_eur,
        ship_eur=ship_eur,
        total_eur=total_eur,
        type=type_s,
        thumb_url=locals().get("thumb_url", None),
    )

def parse_ebay_results(session: requests.Session, html: str) -> List[Item]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    cards.extend(soup.select('[data-testid="item"]'))
    cards.extend(soup.select(".s-item"))
    cards.extend(soup.select("li.s-item"))

    items: List[Item] = []

    seen_ids = set()

    def get_text(el, sel) -> str:
        t = el.select_one(sel)
        return t.get_text(" ", strip=True) if t else ""

    def pick_thumb(el) -> Optional[str]:
        img = el.select_one("img")
        if img:
            u = img.get("src") or img.get("data-src") or img.get("data-image-url")
            if u:
                if u.startswith("//"):
                    return "https:" + u
                return u
        return None

    for el in cards:
        link_el = el.select_one("a[href]")
        if not link_el:
            continue
        url = link_el.get("href") or ""
        m = EBAY_ITM_ID_RE.search(url)
        item_id = m.group(1) if m else None
        if not item_id:
            # try embedded json
            m2 = EBAY_ID_FROM_DATA_RE.search(str(el))
            if m2:
                item_id = m2.group(1)
        if not item_id:
            continue
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        title = (get_text(el, ".s-item__title") or
                 get_text(el, "[data-testid='title']") or
                 link_el.get_text(" ", strip=True))
        price_text = (get_text(el, ".s-item__price") or
                      get_text(el, "[data-testid='price']"))
        ship_text = (get_text(el, ".s-item__shipping") or
                     get_text(el, "[data-testid='shipping']"))
        # Type detection
        type_s = ""
        typetxt = (get_text(el, ".s-item__purchaseOptions") or
                   get_text(el, "[data-testid='purchase-options']") or "").lower()
        if "auction" in typetxt or "bied" in typetxt or "bid" in typetxt:
            type_s = "🧷 Auction"
        elif "koop" in typetxt or "buy" in typetxt or "nu kopen" in typetxt or "buy it now" in typetxt:
            type_s = "🛒 Buy Now"
        # fallback from title
        if not type_s:
            if re.search(r"\bbid\b|\bauction\b|\bbieden\b", (title or "").lower()):
                type_s = "🧷 Auction"
            else:
                type_s = "🛒 Buy Now"

        price_eur = parse_money_to_eur(price_text)
        ship_eur = parse_money_to_eur(ship_text)
        total_eur = compute_total(price_eur, ship_eur)
        thumb_url = pick_thumb(el)

        items.append(Item(
            key=f"EBAY:{item_id}",
            source="eBay",
            title=title,
            link=url,
            price_eur=price_eur,
            ship_eur=ship_eur,
            total_eur=total_eur,
            type=type_s,
            thumb_url=thumb_url
        ))

    # Fallback regex if no cards
    if not items:
        for m in EBAY_ITM_ID_RE.finditer(html):
            iid = m.group(1)
            items.append(Item(
                key=f"EBAY:{iid}",
                source="eBay",
                title="",
                link=f"https://www.ebay.nl/itm/{iid}",
                price_eur=None,
                ship_eur=None,
                total_eur=None,
                type=""
            ))
    return items

# =========================
# Worker Thread
# =========================

def worker_fetch(qout: "queue.Queue[Dict[str, Any]]", stop_event: threading.Event, db: Store):
    session = make_session()

    try:
        # MARKTPLAATS
        r = polite_get(session, SEARCH_MP, stop_event)
        html = getattr(r, "text", "") or ""
        status = getattr(r, "status_code", 0)
        urls = discover_mp_urls(html) if html else []

        qout.put({ "type": MSG_STATUS, "text": f"MP search HTTP {status} — found {len(urls)} URLs" })
        total = len(urls)
        for i, url in enumerate(urls, 1):
            if stop_event.is_set():
                break
            it = parse_mp_ad(session, url, stop_event)
            # fetch image bytes (optional)
            it.thumb_bytes = fetch_bytes(session, it.thumb_url, stop_event) if it.thumb_url else None

            # store to DB and compute trend
            try:
                db.upsert_item(it)
                hist = db.get_price_history(it.key)
                it.trend = sparkline(hist)
            except Exception as e:
                qout.put({ "type": MSG_ERROR, "text": f"DB upsert error for {it.key}: {e}" })

            qout.put({
                "type": MSG_UPSERT,
                "item": it,
            })
            qout.put({ "type": MSG_STATUS, "text": f"MP {i}/{total}: {truncate(it.title, 80)}", "current": i, "total": total })

        # EBAY
        if not stop_event.is_set():
            r2 = polite_get(session, SEARCH_EBAY, stop_event)
            html2 = getattr(r2, "text", "") or ""
            status2 = getattr(r2, "status_code", 0)
            ebay_items = parse_ebay_results(session, html2) if html2 else []
            qout.put({ "type": MSG_STATUS, "text": f"eBay search HTTP {status2} — parsed {len(ebay_items)} results" })
            count_added = 0
            for it in ebay_items:
                if stop_event.is_set():
                    break
                # fetch thumb (optional)
                it.thumb_bytes = fetch_bytes(session, it.thumb_url, stop_event) if it.thumb_url else None
                try:
                    db.upsert_item(it)
                    hist = db.get_price_history(it.key)
                    it.trend = sparkline(hist)
                except Exception as e:
                    qout.put({ "type": MSG_ERROR, "text": f"DB upsert error for {it.key}: {e}" })
                qout.put({ "type": MSG_UPSERT, "item": it })
                count_added += 1
            qout.put({ "type": MSG_STATUS, "text": f"eBay listings added: {count_added}" })

    except Exception as e:
        qout.put({ "type": MSG_ERROR, "text": f"Worker error: {e}" })
    finally:
        qout.put({ "type": MSG_DONE, "text": "Fetch complete" })

# =========================
# Tkinter App
# =========================

def safe_str(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    return s.replace("\n", " ").replace("\r", " ")

def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

class ZXWatcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry(DEFAULT_WINDOW_SIZE)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # State
        self.db = Store(DB_FILE)
        atexit.register(self.db.close)

        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.fetch_running = False

        self.rows_by_key: Dict[str, str] = {}  # key -> iid
        self.thumb_cache: Dict[str, ImageTk.PhotoImage] = {}

        self._build_ui()
        self._bind_keys()

        # start queue polling
        self.after(200, self._poll_queue)

        # auto-fetch
        if AUTO_FETCH_ENABLED:
            self.after(500, self.fetch_now)
            self.after(AUTO_FETCH_MS, self._auto_fetch_loop)

    # ---------- UI construction ----------
    def _build_ui(self):
        # Toolbar
        toolbar = ttk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        self.btn_fetch = ttk.Button(toolbar, text="Fetch Now (F5)", command=self.fetch_now)
        self.btn_fetch.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_stop = ttk.Button(toolbar, text="Stop (Esc)", command=self.stop_fetch, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.btn_open = ttk.Button(toolbar, text="Open Ad", command=self.open_ad)
        self.btn_open.pack(side=tk.LEFT)

        self.btn_export = ttk.Button(toolbar, text="Export CSV", command=self.export_csv)
        self.btn_export.pack(side=tk.LEFT, padx=(6,0))

        # Paned vertical: table (top) + console (bottom)
        paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0,6))

        # Table frame
        table_frame = ttk.Frame(paned)
        paned.add(table_frame, weight=3)

        # Treeview with image in #0
        columns = ("Title", "Price €", "Ship €", "Total €", "Type", "📈 Trend", "Source", "Link")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="tree headings", selectmode="browse")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Scrollbars
        yscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=yscroll.set)

        # Style row height for thumbnails
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=THUMB_SIZE[1] + 6)  # a bit taller
        style.map("Treeview", background=[("selected", "#d0e7ff")])
        # tag for deals
        style.configure("Deal.Treeview", background="#e8ffe8")  # unused but keep
        self.tree.tag_configure("deal", background="#eaffea")

        # Headings and column widths
        self.tree.heading("#0", text="Image", command=lambda c="#0": self.sort_by(c, False))
        self.tree.column("#0", width=THUMB_SIZE[0] + 20, stretch=False, anchor=tk.CENTER)

        col_widths = {
            "Title": 460, "Price €": 90, "Ship €": 90, "Total €": 95,
            "Type": 110, "📈 Trend": 120, "Source": 95, "Link": 300
        }
        for c in columns:
            self.tree.heading(c, text=c, command=lambda col=c: self.sort_by(col, False))
            self.tree.column(c, width=col_widths.get(c, 120), stretch=(c in ("Title","Link","📈 Trend")), anchor=(tk.W if c in ("Title","📈 Trend","Link") else tk.CENTER))

        # Console pane
        console_frame = ttk.Frame(paned)
        paned.add(console_frame, weight=1)

        self.console = tk.Text(console_frame, wrap="word", height=8)
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        console_scroll = ttk.Scrollbar(console_frame, orient=tk.VERTICAL, command=self.console.yview)
        console_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.console.configure(yscrollcommand=console_scroll.set)
        self.console.configure(state=tk.DISABLED, font=("Consolas", 10))

        # Status bar
        status_frame = ttk.Frame(self)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=4)

        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=220)
        self.progress.pack(side=tk.RIGHT, padx=6, pady=4)

        # Double-click open ad
        self.tree.bind("<Double-1>", lambda e: self.open_ad())

    def _bind_keys(self):
        self.bind("<F5>", lambda e: self.fetch_now())
        self.bind("<Escape>", lambda e: self.stop_fetch())

    # ---------- Logging / status ----------
    def log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.console.configure(state=tk.NORMAL)
        self.console.insert(tk.END, line)
        self.console.see(tk.END)
        self.console.configure(state=tk.DISABLED)

    def set_status(self, text: str, current: Optional[int] = None, total: Optional[int] = None):
        self.status_var.set(text)
        self.title(f"{APP_NAME} — {text}")
        if total and current is not None:
            # determinate
            self.progress.configure(mode="determinate", maximum=total, value=current)
        elif total:
            # we know total but not current
            self.progress.configure(mode="determinate", maximum=total, value=0)
        else:
            # indeterminate
            self.progress.configure(mode="indeterminate")
            if not self.fetch_running:
                # Not running? don't start animation
                pass
        if self.fetch_running and str(self.progress["mode"]) == "indeterminate":
            try:
                self.progress.start(80)
            except tk.TclError:
                pass
        else:
            try:
                self.progress.stop()
            except tk.TclError:
                pass

    # ---------- Fetch control ----------
    def fetch_now(self):
        if self.fetch_running:
            self.log("Fetch already running.")
            return
        self.stop_event.clear()
        self.fetch_running = True
        self.btn_fetch.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.set_status("Fetching…")
        self.progress.configure(mode="indeterminate")
        try:
            self.progress.start(80)
        except tk.TclError:
            pass
        self.log("Starting fetch worker thread.")
        self.worker_thread = threading.Thread(target=worker_fetch, args=(self.queue, self.stop_event, self.db), daemon=True)
        self.worker_thread.start()

    def stop_fetch(self):
        if not self.fetch_running:
            return
        self.log("Stop requested. Waiting for worker to exit…")
        self.stop_event.set()

    def _auto_fetch_loop(self):
        # Schedule periodic fetch
        if not self.fetch_running:
            self.fetch_now()
        self.after(AUTO_FETCH_MS, self._auto_fetch_loop)

    # ---------- Queue processing ----------
    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _handle_message(self, msg: Dict[str, Any]):
        mtype = msg.get("type")
        if mtype == MSG_STATUS:
            text = msg.get("text", "")
            cur = msg.get("current")
            tot = msg.get("total")
            if cur is not None and tot:
                self.set_status(text, cur, tot)
            else:
                self.set_status(text)
            self.log(text)
        elif mtype == MSG_ERROR:
            text = msg.get("text", "")
            self.log(f"ERROR: {text}")
        elif mtype == MSG_UPSERT:
            it: Item = msg["item"]
            self._insert_or_update_row(it)
        elif mtype == MSG_DONE:
            self.fetch_running = False
            self.btn_fetch.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)
            self.set_status("Idle")
            try:
                self.progress.stop()
            except tk.TclError:
                pass
            self.log("Fetch completed.")
        else:
            self.log(f"Unknown message: {msg}")

    # ---------- Table ops ----------
    def _insert_or_update_row(self, it: Item):
        # Prepare values
        vals = (
            safe_str(it.title),
            "" if it.price_eur is None else f"{it.price_eur:.2f}",
            "" if it.ship_eur is None else f"{it.ship_eur:.2f}",
            "" if it.total_eur is None else f"{it.total_eur:.2f}",
            safe_str(it.type),
            safe_str(it.trend),
            safe_str(it.source),
            safe_str(it.link),
        )
        tags = ()
        if it.total_eur is not None and it.total_eur <= DEAL_THRESHOLD_EUR:
            tags = ("deal",)

        iid = self.rows_by_key.get(it.key)
        photo = None
        if it.thumb_bytes:
            try:
                im = Image.open(io.BytesIO(it.thumb_bytes)).convert("RGBA")
                im.thumbnail(THUMB_SIZE, Image.LANCZOS)
                photo = ImageTk.PhotoImage(im)
                self.thumb_cache[it.key] = photo  # keep alive
            except Exception:
                pass

        if iid and self.tree.exists(iid):
            # Update existing
            self.tree.item(iid, text="", values=vals, tags=tags, image=(photo or self.thumb_cache.get(it.key)))
        else:
            iid = self.tree.insert("", tk.END, text="", values=vals, image=(photo or ""), tags=tags)
            self.rows_by_key[it.key] = iid

    def sort_by(self, col: str, descending: bool):
        # Determine column index
        def numeric_or_text(v: str):
            try:
                return float(v.replace(",", "").strip())
            except Exception:
                return v.lower()

        if col == "#0":
            # Can't really sort by image meaningfully; ignore
            return

        col_index = None
        cols = ("Title", "Price €", "Ship €", "Total €", "Type", "📈 Trend", "Source", "Link")
        for i, c in enumerate(cols):
            if c == col:
                col_index = i
                break
        if col_index is None:
            return

        # Gather data
        data = []
        for iid in self.tree.get_children(""):
            vals = self.tree.item(iid, "values")
            keyval = vals[col_index]
            if col in ("Price €", "Ship €", "Total €"):
                try:
                    key = float(keyval) if keyval != "" else float("inf")
                except Exception:
                    key = float("inf")
            else:
                key = keyval.lower()
            data.append((key, iid))

        data.sort(reverse=descending, key=lambda x: x[0])

        # Reorder
        for index, (_, iid) in enumerate(data):
            self.tree.move(iid, "", index)

        # Toggle heading sort next time
        self.tree.heading(col, command=lambda: self.sort_by(col, not descending))

    # ---------- Actions ----------
    def open_ad(self):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        link = self.tree.item(iid, "values")[-1]
        if link:
            webbrowser.open(link)

    def export_csv(self):
        # Export currently displayed rows
        path = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            cols = ("Title", "Price €", "Ship €", "Total €", "Type", "📈 Trend", "Source", "Link")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for iid in self.tree.get_children(""):
                    vals = self.tree.item(iid, "values")
                    w.writerow(vals)
            self.log(f"Exported CSV to: {path}")
            messagebox.showinfo("Export CSV", f"Exported to {path}")
        except Exception as e:
            self.log(f"ERROR exporting CSV: {e}")
            messagebox.showerror("Export CSV", f"Failed to export: {e}")

    # ---------- Shutdown ----------
    def on_close(self):
        try:
            self.stop_event.set()
        except Exception:
            pass
        try:
            if self.worker_thread and self.worker_thread.is_alive():
                # Don't hang: detach daemon thread
                pass
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self.destroy()

# =========================
# Main
# =========================

def main():
    app = ZXWatcherApp()
    app.mainloop()

if __name__ == "__main__":
    main()
