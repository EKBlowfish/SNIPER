#!/usr/bin/env python3
# -*- coding: utf-8 -*-


"""
ZX Spectrum Watcher v3 — Marktplaats + eBay
- Thumbnails, inline price graph, auto-fetch (15 min)
- Column sorting by click
- Prices normalized to EUR, plus estimated shipping-to-NL
- "Type" column (🛒 Buy Now / 🧷 Auction)
- Total € column (item + shipping)

Requires: requests, beautifulsoup4, pillow, matplotlib
"""

import os
import re
import io
import csv
import time
import queue
import threading
import sqlite3
import smtplib
import webbrowser
from email.mime.text import MIMEText
from dataclasses import dataclass
from typing import Optional, List, Tuple
from urllib.parse import urljoin
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from PIL import Image, ImageTk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

EU_AMS = ZoneInfo("Europe/Amsterdam")

# --- Config (env) ---
MP_SEARCH_URL = os.getenv("MP_SEARCH_URL", "https://www.marktplaats.nl/l/computers-en-software/vintage-computers/q/zx+spectrum/")
RESULT_AD_URL_PATTERN = re.compile(r"/v/computers-en-software/vintage-computers/m\d+-")
MP_AD_ID_PATTERN = re.compile(r"/m(\d+)-")

EBAY_SEARCH_URL = os.getenv("EBAY_SEARCH_URL",
    "https://www.ebay.nl/sch/i.html?_nkw=zx+spectrum&_sacat=11189&LH_BIN=0")

DB_PATH = os.getenv("ZX_WATCH_DB", "zx_marktplaats.sqlite3")
USER_AGENT = os.getenv("ZX_WATCH_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_DELAY_SEC = float(os.getenv("ZX_WATCH_DELAY", "1.0"))
DEFAULT_THRESHOLD = float(os.getenv("ZX_WATCH_MAX_PRICE", "300"))
AUTO_FETCH_MIN = int(os.getenv("ZX_WATCH_AUTO_MIN", "15"))  # minutes

# FX: conversion to EUR (user-editable)
USD_TO_EUR = float(os.getenv("FX_USD_TO_EUR", "0.90"))   # default approx
GBP_TO_EUR = float(os.getenv("FX_GBP_TO_EUR", "1.17"))   # default approx
AUD_TO_EUR = float(os.getenv("FX_AUD_TO_EUR", "0.60"))
CAD_TO_EUR = float(os.getenv("FX_CAD_TO_EUR", "0.68"))

EMAIL_TO = os.getenv("ZX_WATCH_EMAIL_TO", "")
EMAIL_FROM = os.getenv("ZX_WATCH_EMAIL_FROM", "")
SMTP_HOST = os.getenv("ZX_WATCH_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("ZX_WATCH_SMTP_PORT", "587"))
SMTP_USER = os.getenv("ZX_WATCH_SMTP_USER", "")
SMTP_PASS = os.getenv("ZX_WATCH_SMTP_PASS", "")
USE_TLS = os.getenv("ZX_WATCH_SMTP_TLS", "1") == "1"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.8,en-US;q=0.5,en;q=0.3"})

@dataclass
class Ad:
    ad_id: str          # "MP:..." or "EBAY:..."
    source: str         # "Marktplaats" or "eBay"
    url: str
    title: str
    price_eur: Optional[float]
    price_text: str
    ship_eur: Optional[float]
    total_eur: Optional[float]
    type_text: str      # "🛒 Buy Now" or "🧷 Auction" (or unknown)
    location: str
    seller: str
    posted_on: str
    last_seen: datetime
    thumb_url: str = ""

# --- Utils ---
def polite_get(url: str) -> Optional[requests.Response]:
    try:
        r = session.get(url, timeout=25)
        if r.status_code == 200:
            return r
        return None
    except requests.RequestException:
        return None

def text_or_blank(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(strip=True)) if el else ""

def parse_money_to_eur(txt: str) -> Optional[float]:
    """Parse a money string and convert to EUR using simple heuristics and FX env defaults."""
    if not txt:
        return None
    t = txt.strip()
    low = t.lower()
    # Free / Gratis
    if "free" in low or "gratis" in low:
        return 0.0
    # Common formats: symbols and codes
    # EUR
    m = re.search(r"(?:€|eur)\s*([\d\.,\s]+)", t, re.I)
    if m:
        val = m.group(1)
        val = val.replace(".", "").replace(" ", "").replace(",", ".")
        try:
            return float(val)
        except:
            pass
    # GBP
    m = re.search(r"(?:£|gbp)\s*([\d\.,\s]+)", t, re.I)
    if m:
        val = m.group(1).replace(",", "").replace(" ", "").replace(".", "")
        # If decimal commas are used, fallback to replacing last two digits
        raw = m.group(1).replace(",", "").replace(" ", "")
        try:
            num = float(raw.replace(".", ""))  # crude; we try safer below
        except:
            num = None
        # better parse similar to EUR handling:
        val2 = m.group(1).replace(",", "").replace(" ", "")
        try:
            num2 = float(val2)
        except:
            # try "1.234,56" style
            val3 = m.group(1).replace(".", "").replace(",", ".").replace(" ", "")
            try:
                num2 = float(val3)
            except:
                num2 = None
        if num2 is not None:
            return round(num2 * GBP_TO_EUR, 2)
    # USD
    m = re.search(r"(?:\$|usd)\s*([\d\.,\s]+)", t, re.I)
    if m:
        val = m.group(1).replace(",", "").replace(" ", "")
        try:
            return round(float(val) * USD_TO_EUR, 2)
        except:
            pass
    # AUD
    m = re.search(r"(?:aud)\s*([\d\.,\s]+)", t, re.I)
    if m:
        val = m.group(1).replace(",", "").replace(" ", "")
        try:
            return round(float(val) * AUD_TO_EUR, 2)
        except:
            pass
    # CAD
    m = re.search(r"(?:cad)\s*([\d\.,\s]+)", t, re.I)
    if m:
        val = m.group(1).replace(",", "").replace(" ", "")
        try:
            return round(float(val) * CAD_TO_EUR, 2)
        except:
            pass
    # Plain number; assume EUR
    m = re.search(r"([\d\.\s]+),(\d{2})", t)
    if m:
        euros = re.sub(r"[^\d]", "", m.group(1))
        cents = m.group(2)
        try:
            return float(f"{int(euros)}.{int(cents)}")
        except:
            return None
    m = re.search(r"\b(\d[\d\.\,]*)\b", t)
    if m:
        val = m.group(1).replace(".", "").replace(",", ".")
        try:
            return float(val)
        except:
            return None
    return None

def first_image_url_from_soup(soup: BeautifulSoup) -> str:
    sel = ["img[src*='marktplaats']", "figure img", ".image img", "img"]
    for css in sel:
        img = soup.select_one(css)
        if img and img.get("src"):
            return img["src"]
        if img and img.get("data-src"):
            return img["data-src"]
    return ""

def fetch_thumbnail(url: str, size=(80, 80)) -> Optional[ImageTk.PhotoImage]:
    if not url:
        return None
    try:
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            return None
        image = Image.open(io.BytesIO(r.content)).convert("RGB")
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)
    except Exception:
        return None

# --- Marktplaats scraping ---
def mp_parse_search(url: str) -> List[str]:
    resp = polite_get(url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if RESULT_AD_URL_PATTERN.search(href):
            full = urljoin("https://www.marktplaats.nl", href)
            urls.add(full)
    by_id = {}
    for u in urls:
        m = MP_AD_ID_PATTERN.search(u)
        if m:
            by_id[m.group(1)] = u
    return sorted(by_id.values())

def mp_parse_ad(url: str) -> Optional[Ad]:
    resp = polite_get(url)
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.find("h1")
    title = text_or_blank(h1)
    # Price text
    price_text = ""
    for tag in soup.find_all(["span", "div", "strong", "p"]):
        t = tag.get_text(" ", strip=True)
        if "€" in t or "Bieden" in t or "Gereserveerd" in t:
            if len(t) <= 40 and ("€" in t or t.lower() in ("bieden", "gereserveerd")):
                price_text = t
                break
    if not price_text:
        b = soup.find(string=re.compile(r"\bBieden\b", re.I))
        price_text = b.strip() if b else ""
    price_eur = parse_money_to_eur(price_text)

    # Shipping (PostNL costs)
    ship_eur = None
    ship_block = soup.find(string=re.compile(r"PostNL", re.I))
    if ship_block:
        around = ship_block.parent.get_text(" ", strip=True)
        m = re.search(r"€\s*([\d\.,]+)", around)
        if m:
            ship_eur = parse_money_to_eur(m.group(0))

    # Type
    type_text = "🛒 Buy Now" if (price_eur is not None) else "🧷 Auction"

    location = ""
    for candidate in soup.find_all(text=True):
        s = str(candidate).strip()
        if len(s) <= 40 and re.match(r"^[A-ZÁÉÍÓÚÄÖÜÅØÆËÏÖÜÕÑÇ][\w\s\.'-]{1,}$", s):
            parent_text = candidate.find_parent().get_text(" ", strip=True).lower() if candidate and candidate.find_parent() else ""
            if "jaar actief" in parent_text or "beantwoordt" in parent_text:
                location = s
                break

    seller = ""
    seller_hdr = soup.find(string=re.compile(r"Overige advertenties van", re.I))
    if seller_hdr:
        parent = seller_hdr.find_parent()
        if parent:
            for a in parent.find_all("a"):
                nm = text_or_blank(a)
                if nm and len(nm) <= 40 and "advertenties" not in nm.lower():
                    seller = nm
                    break

    posted = ""
    for el in soup.find_all(["div", "span", "p"], string=re.compile(r"\bsinds\b", re.I)):
        posted = text_or_blank(el)
        if posted:
            break
    if not posted and h1:
        sib = h1.find_next(string=re.compile(r"\bsinds\b", re.I))
        if sib:
            posted = text_or_blank(sib.parent if hasattr(sib, "parent") else h1)

    m = MP_AD_ID_PATTERN.search(url)
    mp_id = m.group(1) if m else ""
    ad_id = f"MP:{mp_id}" if mp_id else f"MP:{abs(hash(url))}"

    thumb_url = first_image_url_from_soup(soup)
    total = (price_eur or 0) + (ship_eur or 0)

    return Ad(
        ad_id=ad_id,
        source="Marktplaats",
        url=url,
        title=title,
        price_eur=price_eur,
        price_text=price_text or "",
        ship_eur=ship_eur,
        total_eur=total if (price_eur is not None or ship_eur is not None) else None,
        type_text=type_text,
        location=location,
        seller=seller,
        posted_on=posted,
        last_seen=datetime.now(EU_AMS),
        thumb_url=thumb_url,
    )

# --- eBay search cards ---
def ebay_parse_search(url: str) -> List[Ad]:
    resp = polite_get(url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    items: List[Ad] = []
    cards = soup.select('[data-testid="item"], .s-item')
    for c in cards:
        a = c.find("a", href=True)
        if not a:
            continue
        link = a["href"]
        title = text_or_blank(c.find(attrs={"role": "heading"}) or c.find("h3") or a)
        if not title:
            continue
        # Price & type
        price_el = c.find(class_=re.compile(r"^s-item__price")) or c.find(string=re.compile(r"(€|EUR|\$|USD|£|GBP)", re.I))
        price_text = text_or_blank(price_el) if hasattr(price_el, "get_text") else (price_el.strip() if price_el else "")
        price_eur = parse_money_to_eur(price_text)

        # Shipping
        ship_text = ""
        ship_el = c.find(class_=re.compile(r"s-item__shipping"))
        if ship_el:
            ship_text = text_or_blank(ship_el)
        ship_eur = parse_money_to_eur(ship_text)

        # Type (auction vs BIN): look for "Biedingen" / "Auction" cues or "Buy it now"
        type_text = "🛒 Buy Now"
        type_hint = text_or_blank(c.find(class_=re.compile(r"s-item__purchase-options|s-item__sep")))
        joined = " ".join([price_text, ship_text, type_hint]).lower()
        if any(k in joined for k in ["auction", "bieding", "biedingen", "veiling"]):
            type_text = "🧷 Auction"
        if any(k in joined for k in ["buy it now", "nu kopen", "bin"]):
            type_text = "🛒 Buy Now"

        # Thumb
        img = c.find("img")
        thumb_url = img["src"] if img and img.get("src") else (img.get("data-src") if img else "")

        # ID
        item_id = ""
        for attr in ("data-view", "data-id", "data-listing-id"):
            if c.has_attr(attr):
                item_id = c[attr]
                break
        if not item_id:
            m = re.search(r"/(\d{9,})\?", link)
            item_id = m.group(1) if m else str(abs(hash(link)))

        total = (price_eur or 0) + (ship_eur or 0)

        items.append(Ad(
            ad_id=f"EBAY:{item_id}",
            source="eBay",
            url=link,
            title=title,
            price_eur=price_eur,
            price_text=price_text or "",
            ship_eur=ship_eur,
            total_eur=total if (price_eur is not None or ship_eur is not None) else None,
            type_text=type_text,
            location="",
            seller="",
            posted_on="",
            last_seen=datetime.now(EU_AMS),
            thumb_url=thumb_url or "",
        ))
    return items

# --- DB ---
def ensure_db(conn: sqlite3.Connection):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ads (
        ad_id TEXT PRIMARY KEY,
        url TEXT,
        title TEXT,
        seller TEXT,
        location TEXT,
        posted_on TEXT,
        first_seen TEXT,
        last_seen TEXT,
        last_price_numeric REAL,
        last_price_text TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        ad_id TEXT,
        seen_at TEXT,
        price_numeric REAL,
        price_text TEXT
    )""")
    conn.commit()

def upsert_ad(conn: sqlite3.Connection, ad: Ad) -> Tuple[bool, Optional[float], Optional[float]]:
    cur = conn.cursor()
    cur.execute("SELECT last_price_numeric FROM ads WHERE ad_id=?", (ad.ad_id,))
    row = cur.fetchone()
    is_new = row is None
    old_price = row[0] if row else None
    if is_new:
        conn.execute("""INSERT INTO ads(ad_id, url, title, seller, location, posted_on,
                      first_seen, last_seen, last_price_numeric, last_price_text)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (ad.ad_id, ad.url, ad.title, ad.seller, ad.location, ad.posted_on,
                      ad.last_seen.isoformat(), ad.last_seen.isoformat(),
                      ad.price_eur if ad.price_eur is not None else None,
                      ad.price_text))
    else:
        conn.execute("""UPDATE ads SET url=?, title=?, seller=?, location=?, posted_on=?,
                      last_seen=?, last_price_numeric=?, last_price_text=? WHERE ad_id=?""",
                     (ad.url, ad.title, ad.seller, ad.location, ad.posted_on,
                      ad.last_seen.isoformat(),
                      ad.price_eur if ad.price_eur is not None else old_price,
                      ad.price_text or "",
                      ad.ad_id))
    conn.execute("""INSERT INTO price_history(ad_id, seen_at, price_numeric, price_text)
                    VALUES (?, ?, ?, ?)""",
                 (ad.ad_id, ad.last_seen.isoformat(),
                  ad.price_eur if ad.price_eur is not None else None,
                  ad.price_text or ""))
    conn.commit()
    return is_new, old_price, ad.price_eur

def load_price_history(conn: sqlite3.Connection, ad_id: str) -> List[Tuple[datetime, Optional[float]]]:
    rows = conn.execute("SELECT seen_at, price_numeric FROM price_history WHERE ad_id=? ORDER BY seen_at", (ad_id,)).fetchall()
    out = []
    for seen_at, price_numeric in rows:
        try:
            dt = datetime.fromisoformat(seen_at)
        except Exception:
            continue
        out.append((dt, price_numeric))
    return out

# --- Email ---
def send_email(report: str) -> bool:
    to = EMAIL_TO or os.getenv("ZX_WATCH_EMAIL_TO", "")
    from_ = EMAIL_FROM or os.getenv("ZX_WATCH_EMAIL_FROM", "")
    host = SMTP_HOST or os.getenv("ZX_WATCH_SMTP_HOST", "")
    port = int(SMTP_PORT or os.getenv("ZX_WATCH_SMTP_PORT", "587"))
    user = SMTP_USER or os.getenv("ZX_WATCH_SMTP_USER", "")
    pw = SMTP_PASS or os.getenv("ZX_WATCH_SMTP_PASS", "")
    use_tls = USE_TLS
    if not (host and from_ and to and user and pw):
        return False
    msg = MIMEText(report, _charset="utf-8")
    msg["Subject"] = "ZX Spectrum Watch (Marktplaats + eBay)"
    msg["From"] = from_
    msg["To"] = to
    try:
        if use_tls:
            server = smtplib.SMTP(host, port)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(host, port)
        server.login(user, pw)
        server.send_message(msg)
        server.quit()
        return True
    except Exception:
        return False

# --- GUI ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ZX Spectrum Watcher — Marktplaats + eBay (v3)")
        self.geometry("1380x820")
        self.minsize(1180, 680)

        # State
        self.threshold_var = tk.DoubleVar(value=DEFAULT_THRESHOLD)
        self.include_ebay = tk.BooleanVar(value=True)
        self.auto_fetch = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Idle")
        self.queue = queue.Queue()
        self.fetching = False
        self.thumb_cache = {}

        # Top controls
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        self.btn_fetch = ttk.Button(top, text="Fetch Now", command=self.on_fetch)
        self.btn_fetch.pack(side="left")

        ttk.Label(top, text="Threshold (€) ≤").pack(side="left", padx=(12,0))
        self.entry_threshold = ttk.Entry(top, width=7, textvariable=self.threshold_var)
        self.entry_threshold.pack(side="left", padx=(4,12))

        self.chk_ebay = ttk.Checkbutton(top, text="Include eBay", variable=self.include_ebay)
        self.chk_ebay.pack(side="left")

        self.chk_auto = ttk.Checkbutton(top, text="Auto-fetch every 15 min", variable=self.auto_fetch)
        self.chk_auto.pack(side="left", padx=(12,0))

        self.btn_email = ttk.Button(top, text="Email Report", command=self.on_email)
        self.btn_email.pack(side="left", padx=(12,0))

        self.btn_export = ttk.Button(top, text="Export CSV", command=self.on_export)
        self.btn_export.pack(side="left", padx=(8,0))

        self.btn_open = ttk.Button(top, text="Open Ad", command=self.on_open_ad)
        self.btn_open.pack(side="left", padx=(8,0))

        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=(16,0))

        # Split: table + chart
        mid = ttk.Panedwindow(self, orient="vertical")
        mid.pack(fill="both", expand=True, padx=8, pady=8)

        tbl_frame = ttk.Frame(mid)
        chart_frame = ttk.Frame(mid, height=260)
        mid.add(tbl_frame, weight=3)
        mid.add(chart_frame, weight=1)

        # Table
        cols = ("thumb", "source", "type", "ad_id", "title", "price_eur", "ship_eur", "total_eur",
                "seller", "location", "posted", "url", "flags")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", height=22)
        self.tree.pack(fill="both", expand=True)

        headers = {
            "thumb": "📷 Thumb",
            "source": "Source",
            "type": "Type",
            "ad_id": "Ad ID",
            "title": "Title",
            "price_eur": "Item €",
            "ship_eur": "Ship €",
            "total_eur": "Total €",
            "seller": "Seller",
            "location": "Location",
            "posted": "Posted",
            "url": "URL",
            "flags": "Flags"
        }
        for key, label in headers.items():
            self.tree.heading(key, text=label, command=lambda k=key: self.sort_by(k, False))

        widths = {
            "thumb": 90, "source": 100, "type": 110, "ad_id": 140, "title": 440,
            "price_eur": 100, "ship_eur": 90, "total_eur": 110,
            "seller": 140, "location": 140, "posted": 150, "url": 260, "flags": 150
        }
        anchors = {
            "thumb": "center", "source": "center", "type": "center",
            "ad_id": "w", "title": "w", "price_eur": "center", "ship_eur": "center",
            "total_eur": "center", "seller": "w", "location": "w", "posted": "w", "url": "w", "flags": "w"
        }
        for k in cols:
            self.tree.column(k, width=widths[k], anchor=anchors[k])

        # Selection binding -> chart
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)

        # Matplotlib chart
        self.fig = Figure(figsize=(6, 2.6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("Price history")
        self.ax.set_xlabel("Date")
        self.ax.set_ylabel("€")
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Sorting state
        self._sort_reverses = {k: False for k in cols}

        # Initial fetch & auto
        self.after(200, self.on_fetch)
        self.after(AUTO_FETCH_MIN * 60 * 1000, self.auto_fetch_tick)

        # Queue processor
        self.after(120, self.process_queue)

    def sort_by(self, col, reverse):
        # Extract column values and sort
        data = []
        for iid in self.tree.get_children(""):
            vals = self.tree.item(iid, "values")
            # Map columns to index
            idx = {
                "thumb": 0, "source": 1, "type": 2, "ad_id": 3, "title": 4, "price_eur": 5,
                "ship_eur": 6, "total_eur": 7, "seller": 8, "location": 9, "posted": 10,
                "url": 11, "flags": 12
            }[col]
            key = vals[idx]
            # For numeric columns, sort as floats
            if col in ("price_eur", "ship_eur", "total_eur"):
                try:
                    sortkey = float(key) if key not in ("", "—") else float("inf")
                except:
                    sortkey = float("inf")
            else:
                sortkey = key
            data.append((sortkey, iid, vals))
        data.sort(reverse=reverse)
        for i, (_, iid, _) in enumerate(data):
            self.tree.move(iid, "", i)
        self._sort_reverses[col] = not reverse

    def log_status(self, msg: str):
        self.status_var.set(msg)
        self.update_idletasks()

    def on_fetch(self):
        if self.fetching:
            return
        try:
            _ = float(self.threshold_var.get())
        except tk.TclError:
            self.threshold_var.set(DEFAULT_THRESHOLD)
        self.fetching = True
        self.log_status("Fetching…")

        def worker():
            try:
                conn = sqlite3.connect(DB_PATH)
                ensure_db(conn)

                rows = []
                new_items = 0
                price_drops = 0

                # Marktplaats
                mp_urls = mp_parse_search(MP_SEARCH_URL)
                for url in mp_urls:
                    time.sleep(REQUEST_DELAY_SEC)
                    ad = mp_parse_ad(url)
                    if not ad or not ad.ad_id:
                        continue
                    is_new, old_price, new_price = upsert_ad(conn, ad)
                    flags = []
                    if is_new:
                        flags.append("✨ NEW")
                        new_items += 1
                    elif new_price is not None and old_price is not None and new_price < old_price:
                        flags.append("🔻 DROP")
                        price_drops += 1
                    thr = float(self.threshold_var.get())
                    if ad.total_eur is not None and ad.total_eur <= thr:
                        flags.append(f"✅ ≤€{int(thr)}")
                    rows.append((ad, " ".join(flags)))

                # eBay (optional)
                if bool(self.include_ebay.get()):
                    time.sleep(REQUEST_DELAY_SEC)
                    ebay_ads = ebay_parse_search(EBAY_SEARCH_URL)
                    for ad in ebay_ads:
                        is_new, old_price, new_price = upsert_ad(conn, ad)
                        flags = ["🛒 eBay" if "Buy" in ad.type_text else "🧷 eBay"]
                        if is_new:
                            flags.append("✨ NEW")
                            new_items += 1
                        elif new_price is not None and old_price is not None and new_price < old_price:
                            flags.append("🔻 DROP")
                            price_drops += 1
                        thr = float(self.threshold_var.get())
                        if ad.total_eur is not None and ad.total_eur <= thr:
                            flags.append(f"✅ ≤€{int(thr)}")
                        rows.append((ad, " ".join(flags)))

                conn.close()
                self.queue.put(("RESULTS", rows, new_items, price_drops))
            except Exception as e:
                self.queue.put(("ERROR", str(e)))
            finally:
                self.queue.put(("DONE", None))

        threading.Thread(target=worker, daemon=True).start()

    def process_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "RESULTS":
                    _, rows, new_ct, drop_ct = item
                    self.populate_table(rows)
                    self.log_status(f"Done. {len(rows)} listings. ✨ New: {new_ct}, 🔻 Drops: {drop_ct}")
                elif kind == "ERROR":
                    _, err = item
                    messagebox.showerror("Error", f"Fetch failed:\n{err}")
                elif kind == "DONE":
                    self.fetching = False
        except queue.Empty:
            pass
        self.after(200, self.process_queue)

    def populate_table(self, rows: List[tuple]):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.thumb_cache.clear()

        for ad, flags in rows:
            # thumbnail
            img = fetch_thumbnail(ad.thumb_url) if ad.thumb_url else None
            if img:
                self.thumb_cache[ad.ad_id] = img

            price_display = f"{ad.price_eur:.2f}" if ad.price_eur is not None else "—"
            ship_display  = f"{ad.ship_eur:.2f}" if ad.ship_eur is not None else "—"
            total_display = f"{ad.total_eur:.2f}" if ad.total_eur is not None else "—"

            self.tree.insert("", "end", iid=ad.ad_id, image=self.thumb_cache.get(ad.ad_id), values=(
                "", ad.source, ad.type_text, ad.ad_id, ad.title, price_display, ship_display, total_display,
                ad.seller, ad.location, ad.posted_on, ad.url, flags
            ))
        # Show tree + headings so images appear in first column
        self.tree.configure(show="tree headings")

    def on_row_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        ad_id = sel[0]
        conn = sqlite3.connect(DB_PATH)
        ensure_db(conn)
        hist = load_price_history(conn, ad_id)
        conn.close()

        self.ax.clear()
        self.ax.set_title("Price history")
        self.ax.set_xlabel("Date")
        self.ax.set_ylabel("€")
        xs = [dt for (dt, p) in hist if p is not None]
        ys = [p for (dt, p) in hist if p is not None]
        if xs and ys:
            self.ax.plot(xs, ys, marker="o")
            self.ax.grid(True, alpha=0.3)
        else:
            self.ax.text(0.5, 0.5, "No numeric price history yet", ha="center", va="center", transform=self.ax.transAxes)
        self.canvas.draw_idle()

    def on_open_ad(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Open Ad", "Select a row first.")
            return
        url = self.tree.item(sel[0], "values")[11]
        if url:
            webbrowser.open(url)

    def on_export(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["source","type","ad_id","title","item_eur","ship_eur","total_eur","seller","location","posted","url","flags"])
            for iid in self.tree.get_children():
                vals = self.tree.item(iid, "values")
                # values: ("", source, type, ad_id, title, item, ship, total, seller, location, posted, url, flags)
                w.writerow(vals[1:])
        messagebox.showinfo("Export CSV", f"Saved to:\n{path}")

    def on_email(self):
        lines = []
        now = datetime.now(EU_AMS).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"ZX Spectrum Watch — {now}")
        lines.append(f"Marktplaats: {MP_SEARCH_URL}")
        lines.append(f"eBay: {EBAY_SEARCH_URL if self.include_ebay.get() else '(disabled)'}")
        lines.append("")
        for iid in self.tree.get_children():
            _, src, typ, _, title, item, ship, total, _, location, _, url, flags = self.tree.item(iid, "values")
            lines.append(f"- [{src}] {typ} {title} — item €{item}, ship €{ship}, total €{total} — {location} — {url} [{flags}]")
        report = "\n".join(lines)
        ok = send_email(report)
        if ok:
            messagebox.showinfo("Email", "Report sent.")
        else:
            messagebox.showwarning("Email", "Email not sent. Check SMTP settings.")

    def auto_fetch_tick(self):
        if bool(self.auto_fetch.get()) and not self.fetching:
            self.on_fetch()
        self.after(AUTO_FETCH_MIN * 60 * 1000, self.auto_fetch_tick)

if __name__ == "__main__":
    app = App()
    app.mainloop()
