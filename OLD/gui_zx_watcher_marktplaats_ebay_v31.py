#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZX Spectrum Watcher — Marktplaats + eBay (thumbnails, deeper rows, thread-safe DB)
- Real scrapers (Marktplaats ad pages + eBay search cards)
- Stream results row-by-row while fetching (incremental updates)
- EUR normalization (item + shipping when available)
- Type column (🛒 Buy Now / 🧷 Auction)
- 📈 Trend as text sparkline from SQLite price history
- Thumbnails per row (leftmost tree column), taller rows for visibility
- Status bar with live progress; auto-fetch every 15 minutes (toggle)

Requires: requests, beautifulsoup4, Pillow
"""

import os
import re
import io
import csv
import time
import queue
import threading
import sqlite3
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Tuple
from urllib.parse import urljoin

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk

# --------------------------- Config ---------------------------------
APP_TITLE = "ZX Spectrum Watcher — Marktplaats + eBay"
DB_PATH = os.getenv("ZX_DB_PATH", "ads.sqlite3")
USER_AGENT = os.getenv("ZX_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_DELAY = float(os.getenv("ZX_DELAY", "1.0"))   # polite delay between requests (sec)
AUTO_FETCH_SEC = int(os.getenv("ZX_AUTO_SEC", "900")) # 15 minutes default
THUMB_SIZE = (56, 56)

# Search URLs (tweak if needed)
MP_SEARCH_URL = os.getenv("MP_SEARCH_URL",
    "https://www.marktplaats.nl/l/computers-en-software/vintage-computers/q/zx+spectrum/")
EBAY_SEARCH_URL = os.getenv("EBAY_SEARCH_URL",
    "https://www.ebay.nl/sch/i.html?_nkw=zx+spectrum&_sacat=11189")

# Static FX (you can wire a live FX API if you want)
USD_TO_EUR = float(os.getenv("FX_USD_TO_EUR", "0.90"))
GBP_TO_EUR = float(os.getenv("FX_GBP_TO_EUR", "1.17"))
AUD_TO_EUR = float(os.getenv("FX_AUD_TO_EUR", "0.60"))
CAD_TO_EUR = float(os.getenv("FX_CAD_TO_EUR", "0.68"))

# --------------------------- HTTP session ---------------------------
s = requests.Session()
s.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "nl-NL,nl;q=0.8,en-US;q=0.5,en;q=0.3",
})

# --------------------------- Helpers --------------------------------
# Broadened MP result path pattern (site layout changes often)
MP_RESULT_RE = re.compile(r"/v/.*/m\d+-", re.I)
MP_AD_ID_RE = re.compile(r"/m(\d+)-")

@dataclass
class Row:
    key: str           # stable key (e.g., "MP:123456" or "EBAY:987654321")
    source: str        # Marktplaats / eBay
    title: str
    price_eur: Optional[float]
    ship_eur: Optional[float]
    total_eur: Optional[float]
    type_text: str     # 🛒 Buy Now / 🧷 Auction
    trend: str         # small text sparkline from history
    link: str
    thumb_url: str = ""
    thumb_bytes: Optional[bytes] = None  # raw bytes downloaded in worker; Tk image is made on UI thread

# money parsing with rough currency detection → EUR
CUR_MAP = {
    '€': 1.0, 'eur': 1.0,
    '£': GBP_TO_EUR, 'gbp': GBP_TO_EUR,
    '$': USD_TO_EUR, 'usd': USD_TO_EUR,
    'aud': AUD_TO_EUR, 'cad': CAD_TO_EUR,
}

def _to_float(num_str: str) -> Optional[float]:
    try:
        if "," in num_str and "." in num_str:
            if num_str.rfind(",") > num_str.rfind("."):
                num_str = num_str.replace(".", "").replace(",", ".")
            else:
                num_str = num_str.replace(",", "")
        else:
            num_str = num_str.replace(".", "").replace(",", ".")
        return float(num_str)
    except Exception:
        return None

def parse_money_to_eur(txt: str) -> Optional[float]:
    if not txt:
        return None
    t = txt.strip()
    low = t.lower()
    if any(w in low for w in ["gratis", "free"]):
        return 0.0
    m = re.search(r"(€|eur|£|gbp|\$|usd|aud|cad)\s*([\d .,,]+)", low, re.I)
    if m:
        cur = m.group(1).lower()
        amount = _to_float(m.group(2))
        if amount is None:
            return None
        rate = CUR_MAP.get(cur, 1.0)
        return round(amount * rate, 2)
    m2 = re.search(r"([\d .,,]+)", t)
    if m2:
        return _to_float(m2.group(1))
    return None

BLOCKS = ["▁","▂","▃","▄","▅","▆","▇"]

def sparkline(values: List[float], width: int = 10) -> str:
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return "—"
    if len(vals) > width:
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return BLOCKS[0] * len(vals)
    out = []
    for v in vals:
        idx = int((v - lo) / (hi - lo) * (len(BLOCKS) - 1))
        out.append(BLOCKS[idx])
    return "".join(out)

# --------------------------- DB -------------------------------------
SCHEMA = {
    "ads": (
        "CREATE TABLE IF NOT EXISTS ads ("
        "  key TEXT PRIMARY KEY,"
        "  source TEXT, title TEXT, link TEXT,"
        "  last_price REAL, last_ship REAL, last_total REAL,"
        "  type TEXT, first_seen TEXT, last_seen TEXT"
        ")"
    ),
    "price_history": (
        "CREATE TABLE IF NOT EXISTS price_history ("
        "  key TEXT, seen_at TEXT, price REAL"
        ")"
    ),
}

class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.ensure()

    def ensure(self):
        with self.lock:
            c = self.conn.cursor()
            for sql in SCHEMA.values():
                c.execute(sql)
            self.conn.commit()

    def upsert_row(self, key: str, source: str, title: str, link: str,
                   price: Optional[float], ship: Optional[float], total: Optional[float],
                   typ: str):
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT key FROM ads WHERE key=?", (key,))
            exists = c.fetchone() is not None
            if exists:
                c.execute(
                    "UPDATE ads SET source=?, title=?, link=?, last_price=?, last_ship=?, last_total=?, type=?, last_seen=? WHERE key=?",
                    (source, title, link, price, ship, total, typ, now, key)
                )
            else:
                c.execute(
                    "INSERT INTO ads(key, source, title, link, last_price, last_ship, last_total, type, first_seen, last_seen)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (key, source, title, link, price, ship, total, typ, now, now)
                )
            c.execute("INSERT INTO price_history(key, seen_at, price) VALUES (?,?,?)", (key, now, price))
            self.conn.commit()

    def history(self, key: str) -> List[float]:
        with self.lock:
            c = self.conn.cursor()
            rows = c.execute("SELECT price FROM price_history WHERE key=? ORDER BY seen_at", (key,)).fetchall()
        return [r[0] for r in rows if r[0] is not None]

# --------------------------- Scrapers --------------------------------

def polite_get(url: str) -> Optional[requests.Response]:
    try:
        r = s.get(url, timeout=25)
        return r  # return even on non-200 for diagnostics/parsing
    except requests.RequestException:
        return None

# ---- Marktplaats ----

def mp_search() -> List[str]:
    resp = polite_get(MP_SEARCH_URL)
    if not resp or not getattr(resp, "text", ""):
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    urls: set[str] = set()

    # (A) Anchor extraction
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if MP_RESULT_RE.search(href):
            urls.add(urljoin("https://www.marktplaats.nl", href))

    # (B) Regex fallback over raw HTML
    for m in re.findall(r'href="(\/v\/[^" >]+m\d+-[^" >]+)"', resp.text, flags=re.I):
        urls.add(urljoin("https://www.marktplaats.nl", m))

    # (C) JSON-LD itemListElement fallback
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            import json
            data = json.loads(sc.string) if sc.string else None
            if isinstance(data, dict) and "itemListElement" in data:
                for el in data.get("itemListElement", []):
                    item = el.get("item") if isinstance(el, dict) else None
                    url = (item or {}).get("url") if isinstance(item, dict) else None
                    if url and MP_RESULT_RE.search(url):
                        urls.add(urljoin("https://www.marktplaats.nl", url))
        except Exception:
            pass

    # De-dup by ad ID
    by_id = {}
    for u in urls:
        m = MP_AD_ID_RE.search(u)
        if m:
            by_id[m.group(1)] = u
    return sorted(by_id.values())


def _first_img_url(soup: BeautifulSoup) -> str:
    for sel in ["figure img", "img"]:
        img = soup.select_one(sel)
        if img:
            if img.get("src"):
                return img["src"]
            if img.get("data-src"):
                return img["data-src"]
    return ""


def mp_parse_ad(url: str) -> Optional['Row']:
    resp = polite_get(url)
    if not resp or not getattr(resp, "text", ""):
        return None
    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    price_text = ""
    for tag in soup.find_all(["span", "div", "strong", "p"]):
        t = tag.get_text(" ", strip=True)
        if any(k in t for k in ["€", "Bieden", "Gereserveerd", "EUR"]):
            if len(t) <= 48:
                price_text = t
                break
    price = parse_money_to_eur(price_text)
    typ = "🛒 Buy Now" if price is not None else "🧷 Auction"

    ship = None
    ship_anchor = soup.find(string=re.compile(r"PostNL|Verzenden", re.I))
    if ship_anchor and ship_anchor.parent:
        around = ship_anchor.parent.get_text(" ", strip=True)
        m = re.search(r"(€|eur)\s*([\d .,,]+)", around, re.I)
        if m:
            ship = parse_money_to_eur(m.group(0))

    total = (price or 0) + (ship or 0)

    m = MP_AD_ID_RE.search(url)
    adid = m.group(1) if m else str(abs(hash(url)))
    key = f"MP:{adid}"

    thumb_url = _first_img_url(soup)
    thumb_bytes = None
    if thumb_url:
        try:
            tr = polite_get(thumb_url)
            if getattr(tr, "content", None):
                thumb_bytes = tr.content
        except Exception:
            thumb_bytes = None

    return Row(
        key=key, source="Marktplaats", title=title or "(no title)",
        price_eur=price, ship_eur=ship, total_eur=total if (price is not None or ship is not None) else None,
        type_text=typ, trend="", link=url, thumb_url=thumb_url, thumb_bytes=thumb_bytes
    )

# ---- eBay ----

def ebay_search() -> List['Row']:
    resp = polite_get(EBAY_SEARCH_URL)
    if not resp or not getattr(resp, "text", ""):
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    rows: List[Row] = []

    # Broadened selectors and fallback
    cards = soup.select('[data-testid="item"], .s-item, li.s-item')
    regex_links = re.findall(r'href="(https?://www\.ebay\.[^"]+/itm/\d+[^\"]*)"', resp.text, flags=re.I)

    if not cards and regex_links:
        for link in regex_links:
            m = re.search(r"/(\d{9,})(?:$|\?)", link)
            item_id = m.group(1) if m else str(abs(hash(link)))
            rows.append(Row(
                key=f"EBAY:{item_id}", source="eBay", title="(eBay item)",
                price_eur=None, ship_eur=None, total_eur=None,
                type_text="🛒 Buy Now", trend="", link=link,
                thumb_url="", thumb_bytes=None
            ))
        return rows

    for c in cards:
        a = c.find("a", href=True)
        if not a:
            continue
        link = a["href"]
        title_el = c.find(attrs={"role": "heading"}) or c.find("h3") or a
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = c.find(class_=re.compile(r"^s-item__price")) or c.find(string=re.compile(r"(€|EUR|\$|USD|£|GBP)", re.I))
        price_text = price_el.get_text(strip=True) if hasattr(price_el, "get_text") else (price_el.strip() if price_el else "")
        price = parse_money_to_eur(price_text)

        ship_text = ""
        ship_el = c.find(class_=re.compile(r"s-item__shipping"))
        if ship_el:
            ship_text = ship_el.get_text(strip=True)
        ship = parse_money_to_eur(ship_text)

        join = " ".join([price_text, ship_text]).lower()
        typ = "🛒 Buy Now"
        if any(k in join for k in ["auction", "bieding", "biedingen", "veiling"]):
            typ = "🧷 Auction"
        if any(k in join for k in ["buy it now", "nu kopen", "bin"]):
            typ = "🛒 Buy Now"

        img = c.find("img")
        thumb_url = (img.get("src") or img.get("data-src")) if img else ""
        thumb_bytes = None
        if thumb_url:
            try:
                tr = polite_get(thumb_url)
                if getattr(tr, "content", None):
                    thumb_bytes = tr.content
            except Exception:
                thumb_bytes = None

        m = re.search(r"/(\d{9,})(?:$|\?)", link)
        item_id = m.group(1) if m else str(abs(hash(link)))
        key = f"EBAY:{item_id}"
        total = (price or 0) + (ship or 0)

        rows.append(Row(
            key=key, source="eBay", title=title or "(no title)",
            price_eur=price, ship_eur=ship, total_eur=total if (price is not None or ship is not None) else None,
            type_text=typ, trend="", link=link, thumb_url=thumb_url or "", thumb_bytes=thumb_bytes
        ))
    return rows

# --------------------------- GUI ------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1380x860")

        # State
        self.include_ebay = tk.BooleanVar(value=True)
        self.auto_fetch_var = tk.BooleanVar(value=True)
        self.threshold_var = tk.DoubleVar(value=300.0)
        self.fetching = False
        self.rows_map = {}  # key -> Treeview iid
        self.q = queue.Queue()
        self.thumb_cache: dict[str, ImageTk.PhotoImage] = {}

        # DB
        self.store = Store(DB_PATH)

        # UI
        self._build_ui()

        # Start
        self.after(100, self.fetch_now)
        self.after(AUTO_FETCH_SEC * 1000, self._auto_tick)
        self.after(120, self._process_queue)

    def _build_ui(self):
        # Taller rows for thumbnails
        style = ttk.Style(self)
        style.configure('Treeview', rowheight=max(THUMB_SIZE[1]+8, 60))

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Fetch Now", command=self.fetch_now).pack(side="left")
        ttk.Label(top, text="Threshold (€) ≤").pack(side="left", padx=(12,0))
        ttk.Entry(top, width=8, textvariable=self.threshold_var).pack(side="left", padx=(4,12))
        ttk.Checkbutton(top, text="Include eBay", variable=self.include_ebay).pack(side="left")
        ttk.Checkbutton(top, text="Auto-fetch every 15 min", variable=self.auto_fetch_var).pack(side="left", padx=(12,0))
        ttk.Button(top, text="Open Ad", command=self._open_selected).pack(side="left", padx=(12,0))
        ttk.Button(top, text="Export CSV", command=self._export_csv).pack(side="left", padx=(8,0))

        # Use tree+headings so we can put thumbnail images in the #0 column
        cols = ("Title","Price €","Ship €","Total €","Type","📈 Trend","Source","Link")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", height=28, selectmode="browse")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(8,0))
        self.tree.heading('#0', text='🖼️')
        self.tree.column('#0', width=68, anchor=tk.CENTER)
        for c in cols:
            self.tree.heading(c, text=c, command=lambda cc=c: self._sort(cc, False))
        self.tree.column("Title", width=560, anchor=tk.W)
        self.tree.column("Price €", width=110, anchor=tk.CENTER)
        self.tree.column("Ship €", width=100, anchor=tk.CENTER)
        self.tree.column("Total €", width=120, anchor=tk.CENTER)
        self.tree.column("Type", width=120, anchor=tk.CENTER)
        self.tree.column("📈 Trend", width=200, anchor=tk.CENTER)
        self.tree.column("Source", width=100, anchor=tk.CENTER)
        self.tree.column("Link", width=300, anchor=tk.W)

        # Status bar at bottom
        status_frame = ttk.Frame(self, padding=(8,4))
        status_frame.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(status_frame, orient="horizontal", mode="determinate", length=280)
        self.progress.pack(side="right")

    # ---------------- Fetch -----------------
    def fetch_now(self):
        if self.fetching:
            return
        self.fetching = True
        self._set_status("Starting fetch…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            # Marktplaats discovery with HTTP status reporting
            mp_resp = polite_get(MP_SEARCH_URL)
            mp_status = getattr(mp_resp, "status_code", "n/a")
            mp_urls = mp_search()
            total_units = (len(mp_urls) if mp_urls else 0) + (1 if self.include_ebay.get() else 0)
            done = 0
            self.q.put(("STATUS", f"MP search HTTP {mp_status} — found {len(mp_urls)} URLs", (done, max(total_units,1))))

            if not mp_urls:
                self.q.put(("STATUS", "Marktplaats returned 0 results — site layout or blocking? Continuing with eBay…", (0, max(total_units,1))))

            for url in mp_urls:
                time.sleep(REQUEST_DELAY)
                row = mp_parse_ad(url)
                if row:
                    self._upsert_row(row)
                done += 1
                title = (row.title[:50] + '…') if (row and len(row.title) > 50) else (row.title if row else '…')
                self.q.put(("STATUS", f"MP {done}/{total_units}: {title}", (done, total_units)))

            # eBay streaming
            if self.include_ebay.get():
                time.sleep(REQUEST_DELAY)
                eb_rows = ebay_search()
                if not eb_rows:
                    self.q.put(("STATUS", "eBay parser found 0 cards — selector change suspected.", (done+1, max(total_units,1))))
                for r in eb_rows:
                    self._upsert_row(r)
                done += 1
                self.q.put(("STATUS", f"eBay listings added: {len(eb_rows)}", (done, max(total_units,1))))

            if not self.rows_map:
                self.q.put(("STATUS", "No listings parsed. Try increasing delay, check network, or adjust selectors.", None))
            else:
                self.q.put(("STATUS", f"Done. {len(self.rows_map)} rows.", None))
        except Exception as e:
            self.q.put(("ERROR", str(e)))
        finally:
            self.q.put(("DONE", None))

    # UI-thread helper to mutate table + DB
    def _upsert_row(self, row: Row):
        total = row.total_eur if row.total_eur is not None else ((row.price_eur or 0) + (row.ship_eur or 0))
        self.store.upsert_row(
            key=row.key, source=row.source, title=row.title, link=row.link,
            price=row.price_eur, ship=row.ship_eur, total=total, typ=row.type_text
        )
        hist = self.store.history(row.key)
        row.trend = sparkline(hist, width=14)
        self.q.put(("UPSERT", row))

    def _process_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "STATUS":
                    _, text, prog = msg
                    self._set_status(text, prog)
                elif kind == "UPSERT":
                    _, row = msg
                    self._ui_upsert(row)
                elif kind == "ERROR":
                    _, err = msg
                    messagebox.showerror("Error", err)
                elif kind == "DONE":
                    self.fetching = False
        except queue.Empty:
            pass
        self.after(150, self._process_queue)

    def _photo_from_bytes(self, blob: Optional[bytes]) -> Optional[ImageTk.PhotoImage]:
        if not blob:
            return None
        try:
            img = Image.open(io.BytesIO(blob)).convert("RGB")
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _ui_upsert(self, row: Row):
        # Build/refresh thumbnail PhotoImage on UI thread
        if row.thumb_bytes:
            tkimg = self._photo_from_bytes(row.thumb_bytes)
            if tkimg:
                self.thumb_cache[row.key] = tkimg
        values = (
            row.title,
            f"{row.price_eur:.2f}" if row.price_eur is not None else "—",
            f"{row.ship_eur:.2f}" if row.ship_eur is not None else "—",
            f"{row.total_eur:.2f}" if row.total_eur is not None else (
                f"{(row.price_eur or 0) + (row.ship_eur or 0):.2f}" if (row.price_eur is not None or row.ship_eur is not None) else "—"
            ),
            row.type_text,
            row.trend or "—",
            row.source,
            row.link,
        )
        if row.key in self.rows_map:
            iid = self.rows_map[row.key]
            self.tree.item(iid, text="", image=self.thumb_cache.get(row.key), values=values)
        else:
            iid = self.tree.insert("", "end", text="", image=self.thumb_cache.get(row.key), values=values)
            self.rows_map[row.key] = iid

        try:
            thr = float(self.threshold_var.get())
            total = row.total_eur if row.total_eur is not None else ((row.price_eur or 0) + (row.ship_eur or 0))
            if total is not None and total <= thr:
                self.tree.item(iid, tags=("ok",))
                self.tree.tag_configure("ok", background="#eaffea")
            else:
                self.tree.item(iid, tags=("",))
        except Exception:
            pass

    def _set_status(self, msg: str, progress: Optional[Tuple[int,int]] = None):
        self.status_var.set(msg)
        if progress:
            cur, tot = progress
            self.progress["maximum"] = max(tot or 1, 1)
            self.progress["value"] = cur or 0
        else:
            self.progress["value"] = 0
        self.update_idletasks()

    def _open_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Open Ad", "Select a row first.")
            return
        url = self.tree.set(sel[0], "Link")
        if url:
            webbrowser.open(url)

    def _export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Title","Price €","Ship €","Total €","Type","Trend","Source","Link"])
            for iid in self.tree.get_children(""):
                w.writerow(self.tree.item(iid, "values"))
        messagebox.showinfo("Export CSV", f"Saved to: {path}")

    def _sort(self, col, reverse):
        data = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        if col in ("Price €","Ship €","Total €"):
            def tofloat(x):
                try:
                    return float(str(x).replace(",","."))
                except Exception:
                    return float('inf')
            data.sort(key=lambda t: tofloat(t[0]), reverse=reverse)
        else:
            data.sort(key=lambda t: t[0], reverse=reverse)
        for i, (_, k) in enumerate(data):
            self.tree.move(k, "", i)
        self.tree.heading(col, command=lambda: self._sort(col, not reverse))

    def _auto_tick(self):
        if self.auto_fetch_var.get() and not self.fetching:
            self.fetch_now()
        self.after(AUTO_FETCH_SEC * 1000, self._auto_tick)

if __name__ == "__main__":
    app = App()
    app.mainloop()
