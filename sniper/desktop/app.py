#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZX Spectrum Watcher — Marktplaats + eBay
Tkinter desktop app that scrapes listings and shows them in a GUI.

Deps: requests, beautifulsoup4, Pillow
Tested on Python 3.10+
"""

from __future__ import annotations

import io
import csv
import logging
import queue
import atexit
import threading
import webbrowser
from datetime import datetime
from typing import Any, Dict, Optional

from PIL import Image, ImageTk, UnidentifiedImageError

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from sniper.config import (
    APP_NAME,
    AUTO_FETCH_ENABLED,
    AUTO_FETCH_MINUTES,
    DB_FILE,
    DEAL_THRESHOLD_EUR,
    DEFAULT_SEARCH_ITEM,
    ENABLED_SOURCES,
    MSG_DELETE,
    MSG_DONE,
    MSG_ERROR,
    MSG_STATUS,
    MSG_UPSERT,
)
from sniper.models import Item
from sniper.scraper import worker_fetch
from sniper.store import Store

log = logging.getLogger(__name__)

# =========================
# Config
# =========================

WINDOW_TITLE = f"{APP_NAME} — Marktplaats + eBay"
DEFAULT_WINDOW_SIZE = "1280x800"
THUMB_SIZE = (56, 56)
AUTO_FETCH_MS = AUTO_FETCH_MINUTES * 60 * 1000

# =========================
# Tkinter App
# =========================

def safe_str(x: Any) -> str:
    """Coerce any value to a single-line string."""
    if x is None:
        return ""
    s = str(x)
    return s.replace("\n", " ").replace("\r", " ")

def truncate(s: str, n: int) -> str:
    """Truncate a string to *n* characters with an ellipsis."""
    return s if len(s) <= n else s[: n - 1] + "…"

class ZXWatcherApp(tk.Tk):
    """Main Tkinter GUI application."""

    def __init__(self):
        """Initialize window, state and start background tasks."""
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry(DEFAULT_WINDOW_SIZE)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # State
        self.db = Store(DB_FILE)
        atexit.register(self.db.close)

        self.search_term = DEFAULT_SEARCH_ITEM

        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.fetch_running = False

        self.rows_by_key: Dict[str, str] = {}  # key -> iid
        self.row_data: Dict[str, Item] = {}  # key -> Item (for filtering)
        self.detached: set[str] = set()  # iids currently hidden by filter
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
        """Construct all UI widgets."""
        # Toolbar
        toolbar = ttk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        ttk.Label(toolbar, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        self.search_entry = ttk.Entry(toolbar, width=28)
        self.search_entry.insert(0, self.search_term)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 4))
        self.search_entry.bind("<Return>", lambda e: self._submit_search())

        self.btn_fetch = ttk.Button(toolbar, text="Go (F5)", command=self._submit_search)
        self.btn_fetch.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_stop = ttk.Button(toolbar, text="Stop (Esc)", command=self.stop_fetch, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.btn_open = ttk.Button(toolbar, text="Open Ad", command=self.open_ad)
        self.btn_open.pack(side=tk.LEFT)

        self.btn_export = ttk.Button(toolbar, text="Export CSV", command=self.export_csv)
        self.btn_export.pack(side=tk.LEFT, padx=(6, 0))

        self.btn_clear = ttk.Button(toolbar, text="Clear", command=self._clear_results)
        self.btn_clear.pack(side=tk.LEFT, padx=(6, 0))

        # Filter bar
        filter_bar = ttk.Frame(self)
        filter_bar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4))

        ttk.Label(filter_bar, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
        self.filter_entry = ttk.Entry(filter_bar, width=24)
        self.filter_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.filter_entry.bind("<KeyRelease>", lambda e: self._apply_filter())

        self.show_mp = tk.BooleanVar(value=True)
        self.show_ebay = tk.BooleanVar(value=True)
        ttk.Checkbutton(filter_bar, text="Marktplaats", variable=self.show_mp, command=self._apply_filter).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Checkbutton(filter_bar, text="eBay", variable=self.show_ebay, command=self._apply_filter).pack(side=tk.LEFT, padx=(0, 8))

        self.count_var = tk.StringVar(value="0 listings")
        ttk.Label(filter_bar, textvariable=self.count_var).pack(side=tk.RIGHT, padx=(0, 4))

        # Paned vertical: table (top) + console (bottom)
        paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

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
        """Attach keyboard shortcuts."""
        self.bind("<F5>", lambda e: self._submit_search())
        self.bind("<Escape>", lambda e: self.stop_fetch())

    # ---------- Logging / status ----------
    def log(self, msg: str):
        """Append a timestamped message to the console pane."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.console.configure(state=tk.NORMAL)
        self.console.insert(tk.END, line)
        self.console.see(tk.END)
        self.console.configure(state=tk.DISABLED)

    def set_status(self, text: str, current: Optional[int] = None, total: Optional[int] = None):
        """Update status bar text and progress indicator."""
        self.status_var.set(text)
        self.title(f"{WINDOW_TITLE} — {text}")
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
    def _broadcast_to_queue(self, event_type: str, data: Dict[str, Any]) -> None:
        """Bridge callback for scraper.worker_fetch — pushes events to the GUI queue."""
        msg = dict(data)
        msg["type"] = event_type
        self.queue.put(msg)

    def fetch_now(self):
        """Start the background fetch worker if not already running."""
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
        self.worker_thread = threading.Thread(
            target=worker_fetch,
            args=(self._broadcast_to_queue, self.stop_event, self.db, self.search_term, ENABLED_SOURCES),
            daemon=True,
        )
        self.worker_thread.start()

    def stop_fetch(self):
        """Signal the worker thread to stop."""
        if not self.fetch_running:
            return
        self.log("Stop requested. Waiting for worker to exit…")
        self.stop_event.set()

    def _auto_fetch_loop(self):
        """Periodically trigger a fetch."""
        if not self.fetch_running:
            self.fetch_now()
        self.after(AUTO_FETCH_MS, self._auto_fetch_loop)

    # ---------- Queue processing ----------
    def _poll_queue(self):
        """Check for messages from the worker thread."""
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _handle_message(self, msg: Dict[str, Any]):
        """Handle a single message from the worker thread."""
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
        elif mtype == MSG_DELETE:
            key = msg["key"]
            self._remove_row(key)
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
        """Insert a new row or update an existing one in the Treeview."""
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
                self.thumb_cache[it.key] = photo  # prevent GC
            except (UnidentifiedImageError, OSError) as e:
                log.debug("Failed to load thumbnail for %s: %s", it.key, e)

        self.row_data[it.key] = it

        if iid and (self.tree.exists(iid) or iid in self.detached):
            # Update existing (may be detached by filter)
            if iid in self.detached:
                self.tree.reattach(iid, "", tk.END)
                self.detached.discard(iid)
            self.tree.item(iid, text="", values=vals, tags=tags, image=(photo or self.thumb_cache.get(it.key)))
        else:
            iid = self.tree.insert("", tk.END, text="", values=vals, image=(photo or ""), tags=tags)
            self.rows_by_key[it.key] = iid

        # Re-apply active filter to this row
        self._apply_filter_to_row(it.key, iid)
        self._update_count()

    def _remove_row(self, key: str):
        """Remove a row from the Treeview and all tracking dicts."""
        iid = self.rows_by_key.pop(key, None)
        self.row_data.pop(key, None)
        self.thumb_cache.pop(key, None)
        if iid:
            if iid in self.detached:
                self.tree.reattach(iid, "", tk.END)
                self.detached.discard(iid)
            self.tree.delete(iid)
        self._update_count()

    def sort_by(self, col: str, descending: bool):
        """Sort the Treeview rows by a given column."""
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
                except ValueError:
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
    def _submit_search(self):
        """Read the search entry, update URLs, clear table, and fetch."""
        term = self.search_entry.get().strip()
        if not term:
            return
        self.search_term = term
        self._clear_results()
        self.log(f"Search term set to: {term}")
        self.fetch_now()

    def _clear_results(self):
        """Remove all rows from the table."""
        # Reattach any detached rows so delete can find them
        for iid in self.detached:
            self.tree.reattach(iid, "", tk.END)
        self.detached.clear()
        self.tree.delete(*self.tree.get_children())
        self.rows_by_key.clear()
        self.row_data.clear()
        self._update_count()

    def _apply_filter(self):
        """Show/hide rows based on the filter text and source checkbuttons."""
        query = self.filter_entry.get().strip().lower()
        show_mp = self.show_mp.get()
        show_ebay = self.show_ebay.get()

        # Reattach everything first
        for iid in list(self.detached):
            self.tree.reattach(iid, "", tk.END)
        self.detached.clear()

        # Detach rows that don't match
        for key, iid in self.rows_by_key.items():
            it = self.row_data.get(key)
            if not it:
                continue
            title_match = not query or query in (it.title or "").lower()
            source_match = (
                (it.source == "Marktplaats" and show_mp)
                or (it.source == "eBay" and show_ebay)
                or it.source not in ("Marktplaats", "eBay")
            )
            if not (title_match and source_match):
                self.tree.detach(iid)
                self.detached.add(iid)

        self._update_count()

    def _apply_filter_to_row(self, key: str, iid: str):
        """Apply the current filter to a single row (used on insert/update)."""
        query = self.filter_entry.get().strip().lower()
        show_mp = self.show_mp.get()
        show_ebay = self.show_ebay.get()
        it = self.row_data.get(key)
        if not it:
            return
        title_match = not query or query in (it.title or "").lower()
        source_match = (
            (it.source == "Marktplaats" and show_mp)
            or (it.source == "eBay" and show_ebay)
            or it.source not in ("Marktplaats", "eBay")
        )
        if not (title_match and source_match):
            self.tree.detach(iid)
            self.detached.add(iid)
        elif iid in self.detached:
            self.tree.reattach(iid, "", tk.END)
            self.detached.discard(iid)

    def _update_count(self):
        """Update the row count label."""
        visible = len(self.tree.get_children(""))
        total = len(self.rows_by_key)
        if visible == total:
            self.count_var.set(f"{total} listings")
        else:
            self.count_var.set(f"{visible} of {total} listings")

    def open_ad(self):
        """Open the selected listing in a web browser."""
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        link = self.tree.item(iid, "values")[-1]
        if link:
            webbrowser.open(link)

    def export_csv(self):
        """Export the visible rows to a CSV file."""
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
        """Handle window close: stop worker and close DB."""
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=3)
        self.db.close()
        self.destroy()

# =========================
# Main
# =========================

def main():
    """Entry point for launching the GUI application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app = ZXWatcherApp()
    app.mainloop()

if __name__ == "__main__":
    main()
