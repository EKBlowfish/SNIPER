"""ZX Spectrum Watcher — scrapes Marktplaats + eBay for listings.

Package layout:
    sniper.config    — configuration constants and environment overrides
    sniper.models    — the Item dataclass
    sniper.store     — thread-safe SQLite persistence (Store)
    sniper.net       — HTTP session helpers
    sniper.scraper   — search/parse logic and the background fetch worker
    sniper.settings  — user settings persisted to disk
    sniper.web       — Flask web application
    sniper.desktop   — Tkinter desktop application
"""

__version__ = "2.0.0"
