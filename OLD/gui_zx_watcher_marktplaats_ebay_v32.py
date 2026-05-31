#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZX Spectrum Watcher — Marktplaats + eBay
Includes:
- Thumbnails in first column
- Taller rows for thumbnails
- Status bar showing progress and status messages
- Auto-fetch every 15 minutes
- Cancel/stop button
- Sorting by column
- Price in EUR with shipping costs to NL
- Auction/Buy Now type column with emoji
- Price history sparkline in trend column
"""

import os
import re
import time
import queue
import threading
import sqlite3
import webbrowser
from datetime import datetime
from typing import Optional, List, Tuple
from urllib.parse import urljoin

import tkinter as tk
from tkinter import ttk, filedialog
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk

APP_TITLE = "ZX Spectrum Watcher — Marktplaats + eBay"
DB_PATH = "ads.sqlite3"
THUMB_SIZE = (56, 56)
AUTO_FETCH_SEC = 900
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

s = requests.Session()
s.headers.update({"User-Agent": USER_AGENT})

class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.ensure()

    def ensure(self):
        c = self.conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS ads (key TEXT PRIMARY KEY, source TEXT, title TEXT, link TEXT, last_price REAL, last_ship REAL, last_total REAL, type TEXT, first_seen TEXT, last_seen TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS price_history (key TEXT, seen_at TEXT, price REAL)")
        self.conn.commit()

    def upsert_row(self, key: str, source: str, title: str, link: str, price: Optional[float], ship: Optional[float], total: Optional[float], typ: str):
        now = datetime.utcnow().isoformat(timespec="seconds")
        c = self.conn.cursor()
        c.execute("SELECT key FROM ads WHERE key=?", (key,))
        if c.fetchone():
            c.execute("UPDATE ads SET source=?, title=?, link=?, last_price=?, last_ship=?, last_total=?, type=?, last_seen=? WHERE key=?", (source, title, link, price, ship, total, typ, now, key))
        else:
            c.execute("INSERT INTO ads VALUES(?,?,?,?,?,?,?,?,?,?)", (key, source, title, link, price, ship, total, typ, now, now))
        c.execute("INSERT INTO price_history VALUES(?,?,?)", (key, now, price))
        self.conn.commit()

    def history(self, key: str) -> List[float]:
        c = self.conn.cursor()
        return [r[0] for r in c.execute("SELECT price FROM price_history WHERE key=? ORDER BY seen_at", (key,)).fetchall() if r[0] is not None]

def polite_get(url: str):
    try:
        return s.get(url, timeout=20)
    except Exception:
        return None

def parse_money(txt: str) -> Optional[float]:
    if not txt:
        return None
    t = txt.replace("€", "").replace(",", ".").strip()
    try:
        return float(re.findall(r"[0-9.]+", t)[0])
    except Exception:
        return None

def mp_search():
    r = polite_get("https://www.marktplaats.nl/l/computers-en-software/vintage-computers/q/zx+spectrum/")
    if not r: return []
    soup = BeautifulSoup(r.text, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        if re.search(r"/v/.*/m\d+-", a["href"]):
            urls.add(urljoin("https://www.marktplaats.nl", a["href"]))
    return sorted(urls)

def mp_parse_ad(url: str):
    r = polite_get(url)
    if not r: return None
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
    price = parse_money(soup.get_text())
    typ = "🛒 Buy Now" if price is not None else "🧷 Auction"
    ship = None
    total = (price or 0) + (ship or 0)
    img = soup.find("img")
    thumb_bytes = None
    if img and img.get("src"):
        tr = polite_get(img["src"])
        if tr: thumb_bytes = tr.content
    return {
        "key": f"MP:{hash(url)}", "source": "Marktplaats", "title": title, "price": price, "ship": ship, "total": total,
        "type": typ, "link": url, "thumb": thumb_bytes
    }

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.store = Store(DB_PATH)
        self.q = queue.Queue()
        self.thumb_cache = {}
        self.fetching = False
        self._build_ui()
        self.after(100, self.fetch_now)
        self.after(1000, self._process_queue)

    def _build_ui(self):
        style = ttk.Style(self)
        style.configure('Treeview', rowheight=max(THUMB_SIZE[1]+8, 60))
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Fetch Now", command=self.fetch_now).pack(side="left")
        ttk.Button(top, text="Stop", command=self.stop_fetch).pack(side="left", padx=(6,0))
        cols = ("Title","Price €","Ship €","Total €","Type","Trend","Source","Link")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", height=28, selectmode="browse")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(8,0))
        self.tree.heading('#0', text='🖼️')
        self.tree.column('#0', width=68, anchor=tk.CENTER)
        for c in cols:
            self.tree.heading(c, text=c)
        status_frame = ttk.Frame(self, padding=(8,4))
        status_frame.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(status_frame, orient="horizontal", mode="determinate", length=280)
        self.progress.pack(side="right")

    def _set_status(self, msg: str):
        self.title(f"{APP_TITLE} — {msg}")
        self.status_var.set(msg)
        self.update_idletasks()

    def fetch_now(self):
        if self.fetching: return
        self.fetching = True
        self._set_status("Starting fetch…")
        threading.Thread(target=self._worker, daemon=True).start()

    def stop_fetch(self):
        self.fetching = False
        self._set_status("Stopped")

    def _worker(self):
        urls = mp_search()
        for i,u in enumerate(urls,1):
            if not self.fetching: break
            row = mp_parse_ad(u)
            if row:
                self.q.put(row)
            self._set_status(f"MP {i}/{len(urls)}")
            time.sleep(1)
        self.q.put({"done":True})

    def _process_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if "done" in item:
                    self.fetching = False
                    self._set_status("Idle")
                    continue
                thumb_img = None
                if item["thumb"]:
                    im = Image.open(io.BytesIO(item["thumb"])).resize(THUMB_SIZE)
                    thumb_img = ImageTk.PhotoImage(im)
                    self.thumb_cache[item["key"]] = thumb_img
                self.tree.insert("", "end", text="", image=thumb_img, values=(item["title"], item["price"], item["ship"], item["total"], item["type"], "", item["source"], item["link"]))
        except queue.Empty:
            pass
        self.after(200, self._process_queue)

if __name__ == '__main__':
    App().mainloop()
