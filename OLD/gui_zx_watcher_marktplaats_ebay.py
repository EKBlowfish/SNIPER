#!/usr/bin/env python3
# -*- coding: utf-8 -*-


"""
GUI Marktplaats + eBay ZX Spectrum Watcher (Tkinter)
- Scrapes:
    * Marktplaats (Vintage Computers, query: "zx spectrum")
    * eBay (default: eBay NL, category Vintage Computing, query: "zx spectrum") [optional toggle]
- Follows each Marktplaats ad page for details.
- For eBay, uses search cards (title/price/link) to stay light and resilient.
- Stores history in SQLite and detects:
    * new listings (by source-prefixed ad_id)
    * price drops (numeric only)
- GUI lets you:
    * Include eBay toggle
    * Fetch Now
    * Price threshold filter (≤)
    * Open Ad
    * Export CSV
    * Email on-screen report (optional SMTP)

Python: 3.10+
"""

import os
import re
import csv
import time
import queue
import threading
import sqlite3
import smtplib
import webbrowser
from email.mime.text import MIMEText
from dataclasses import dataclass
from typing import Optional, List, Tuple, Iterable
from urllib.parse import urljoin
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

EU_AMS = ZoneInfo("Europe/Amsterdam")

# --- Config (env) ---
MP_SEARCH_URL = os.getenv("MP_SEARCH_URL", "https://www.marktplaats.nl/l/computers-en-software/vintage-computers/q/zx+spectrum/")
RESULT_AD_URL_PATTERN = re.compile(r"/v/computers-en-software/vintage-computers/m\d+-")
MP_AD_ID_PATTERN = re.compile(r"/m(\d+)-")

# eBay defaults: NL site, category 11189 (Vintage Computing) if present
EBAY_SEARCH_URL = os.getenv("EBAY_SEARCH_URL",
    "https://www.ebay.nl/sch/i.html?_nkw=zx+spectrum&_sacat=11189&LH_BIN=0")  # include auctions & BIN

DB_PATH = os.getenv("ZX_WATCH_DB", "zx_marktplaats.sqlite3")
USER_AGENT = os.getenv("ZX_WATCH_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_DELAY_SEC = float(os.getenv("ZX_WATCH_DELAY", "1.0"))
DEFAULT_THRESHOLD = float(os.getenv("ZX_WATCH_MAX_PRICE", "300"))

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
    ad_id: str          # prefixed: "MP:1234567890" or "EBAY:ITEMID"
    source: str         # "Marktplaats" or "eBay"
    url: str
    title: str
    price_eur: Optional[float]  # None if bidding/unknown
    price_text: str
    location: str
    seller: str
    posted_on: str
    last_seen: datetime

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

def euros_from_text(txt: str) -> Optional[float]:
    if not txt:
        return None
    low = txt.lower()
    for bad in ("bieden", "gereserveerd", "zie omschrijving", "gratis"):
        if bad in low:
            return None
    # Accept formats like "EUR 249,00", "€ 249,00", "€249.00", "249,00 EUR"
    m = re.search(r"(?:€|eur)\s*([\d\.\s]+)[,\.](\d{2})", txt, re.I)
    if m:
        euros = re.sub(r"[^\d]", "", m.group(1))
        cents = m.group(2)
        try:
            return float(f"{int(euros)}.{int(cents)}")
        except ValueError:
            return None
    m2 = re.search(r"(?:€|eur)\s*([\d\.\s]+)\b", txt, re.I)
    if m2:
        euros = re.sub(r"[^\d]", "", m2.group(1))
        try:
            return float(euros)
        except ValueError:
            return None
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
    # dedupe by ad id
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

    price_eur = euros_from_text(price_text)

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

    return Ad(
        ad_id=ad_id,
        source="Marktplaats",
        url=url,
        title=title,
        price_eur=price_eur,
        price_text=price_text or "",
        location=location,
        seller=seller,
        posted_on=posted,
        last_seen=datetime.now(EU_AMS),
    )

# --- eBay scraping (result cards only) ---
def ebay_parse_search(url: str) -> List[Ad]:
    resp = polite_get(url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    items: List[Ad] = []
    # eBay search cards often have attributes data-testid="item" or class "s-item"
    cards = soup.select('[data-testid="item"], .s-item')
    for c in cards:
        # URL
        a = c.find("a", href=True)
        if not a:
            continue
        url = a["href"]
        # Title
        title = text_or_blank(c.find(attrs={"role": "heading"}) or c.find("h3") or a)
        if not title:
            continue
        # Price text
        price_el = c.find(attrs={"data-testid": "ux-summary-seller-shipping"})
        price_text = ""
        if not price_el:
            price_el = c.find(class_=re.compile(r"^s-item__price"))
        price_text = text_or_blank(price_el) if price_el else text_or_blank(c.find(string=re.compile(r"€|EUR", re.I)))
        price_eur = euros_from_text(price_text)

        # Seller/location not easily available on cards; leave blank
        location = ""
        seller = ""

        # ID: sometimes present as data-view or data-id; else hash of URL
        item_id = ""
        for attr in ("data-view", "data-id", "data-listing-id"):
            if c.has_attr(attr):
                item_id = c[attr]
                break
        if not item_id:
            m = re.search(r"/(\d{9,})\?", url)
            item_id = m.group(1) if m else str(abs(hash(url)))

        ad = Ad(
            ad_id=f"EBAY:{item_id}",
            source="eBay",
            url=url,
            title=title,
            price_eur=price_eur,
            price_text=price_text or "",
            location=location,
            seller=seller,
            posted_on="",
            last_seen=datetime.now(EU_AMS),
        )
        items.append(ad)
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
        self.title("ZX Spectrum Watcher — Marktplaats + eBay")
        self.geometry("1180x660")
        self.minsize(1000, 560)

        # State
        self.threshold_var = tk.DoubleVar(value=DEFAULT_THRESHOLD)
        self.include_ebay = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Idle")
        self.queue = queue.Queue()
        self.fetching = False

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

        self.btn_email = ttk.Button(top, text="Email Report", command=self.on_email)
        self.btn_email.pack(side="left", padx=(12,0))

        self.btn_export = ttk.Button(top, text="Export CSV", command=self.on_export)
        self.btn_export.pack(side="left", padx=(8,0))

        self.btn_open = ttk.Button(top, text="Open Ad", command=self.on_open_ad)
        self.btn_open.pack(side="left", padx=(8,0))

        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=(16,0))

        # Table
        cols = ("source", "ad_id", "title", "price", "seller", "location", "posted", "url", "flags")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=20)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        self.tree.heading("source", text="Source")
        self.tree.heading("ad_id", text="Ad ID")
        self.tree.heading("title", text="Title")
        self.tree.heading("price", text="Price")
        self.tree.heading("seller", text="Seller")
        self.tree.heading("location", text="Location")
        self.tree.heading("posted", text="Posted")
        self.tree.heading("url", text="URL")
        self.tree.heading("flags", text="Flags")

        self.tree.column("source", width=100, anchor="center")
        self.tree.column("ad_id", width=130, anchor="w")
        self.tree.column("title", width=360, anchor="w")
        self.tree.column("price", width=110, anchor="center")
        self.tree.column("seller", width=130, anchor="w")
        self.tree.column("location", width=130, anchor="w")
        self.tree.column("posted", width=140, anchor="w")
        self.tree.column("url", width=260, anchor="w")
        self.tree.column("flags", width=120, anchor="w")

        # Initial fetch
        self.after(200, self.on_fetch)
        self.after(120, self.process_queue)

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

                # Marktplaats URLs
                mp_urls = mp_parse_search(MP_SEARCH_URL)
                for url in mp_urls:
                    time.sleep(REQUEST_DELAY_SEC)
                    ad = mp_parse_ad(url)
                    if not ad or not ad.ad_id:
                        continue
                    is_new, old_price, new_price = upsert_ad(conn, ad)
                    flags = []
                    if is_new:
                        flags.append("NEW")
                        new_items += 1
                    elif new_price is not None and old_price is not None and new_price < old_price:
                        flags.append("DROP")
                        price_drops += 1
                    thr = float(self.threshold_var.get())
                    if ad.price_eur is not None and ad.price_eur <= thr:
                        flags.append(f"≤€{int(thr)}")
                    rows.append((ad, " ".join(flags)))

                # eBay (optional)
                if bool(self.include_ebay.get()):
                    time.sleep(REQUEST_DELAY_SEC)
                    ebay_ads = ebay_parse_search(EBAY_SEARCH_URL)
                    for ad in ebay_ads:
                        # treat eBay items similarly (store in DB, detect drops)
                        is_new, old_price, new_price = upsert_ad(conn, ad)
                        flags = ["eBay"]
                        if is_new:
                            flags.append("NEW")
                            new_items += 1
                        elif new_price is not None and old_price is not None and new_price < old_price:
                            flags.append("DROP")
                            price_drops += 1
                        thr = float(self.threshold_var.get())
                        if ad.price_eur is not None and ad.price_eur <= thr:
                            flags.append(f"≤€{int(thr)}")
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
                    self.log_status(f"Done. {len(rows)} listings. New: {new_ct}, Drops: {drop_ct}")
                elif kind == "ERROR":
                    _, err = item
                    messagebox.showerror("Error", f"Fetch failed:\n{err}")
                elif kind == "DONE":
                    self.fetching = False
        except queue.Empty:
            pass
        self.after(150, self.process_queue)

    def populate_table(self, rows: List[tuple]):
        self.tree.delete(*self.tree.get_children())
        for ad, flags in rows:
            price_display = ad.price_text or "—"
            self.tree.insert("", "end", values=(
                ad.source, ad.ad_id, ad.title, price_display, ad.seller, ad.location, ad.posted_on, ad.url, flags
            ))

    def on_open_ad(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Open Ad", "Select a row first.")
            return
        url = self.tree.item(sel[0], "values")[7]
        if url:
            webbrowser.open(url)

    def on_export(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["source","ad_id","title","price","seller","location","posted","url","flags"])
            for iid in self.tree.get_children():
                w.writerow(self.tree.item(iid, "values"))
        messagebox.showinfo("Export CSV", f"Saved to:\n{path}")

    def on_email(self):
        lines = []
        now = datetime.now(EU_AMS).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"ZX Spectrum Watch — {now}")
        lines.append(f"Marktplaats: {MP_SEARCH_URL}")
        lines.append(f"eBay: {EBAY_SEARCH_URL if self.include_ebay.get() else '(disabled)'}")
        lines.append("")
        for iid in self.tree.get_children():
            src, ad_id, title, price, seller, location, posted, url, flags = self.tree.item(iid, "values")
            lines.append(f"- [{src}] {title} — {price} — {location} — {url} [{flags}]")
        report = "\n".join(lines)
        ok = send_email(report)
        if ok:
            messagebox.showinfo("Email", "Report sent.")
        else:
            messagebox.showwarning("Email", "Email not sent. Check SMTP settings in environment variables.")

if __name__ == "__main__":
    app = App()
    app.mainloop()
