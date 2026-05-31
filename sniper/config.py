"""Central configuration for ZX Spectrum Watcher.

All tunables live here (with environment-variable overrides) so the scraper,
web app and desktop app share a single source of truth.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root = parent of this package directory. Anchoring data files here
# keeps them in the repo root no matter what working directory the app is
# launched from.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_FILE = os.getenv("DB_FILE", os.path.join(BASE_DIR, "ads.sqlite3"))
SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(BASE_DIR, "settings.json"))

# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

APP_NAME = "ZX Spectrum Watcher"

# ---------------------------------------------------------------------------
# Search / fetch behaviour
# ---------------------------------------------------------------------------

DEFAULT_SEARCH_ITEM = os.getenv("SEARCH_ITEM", "zx spectrum")

AUTO_FETCH_ENABLED = os.getenv("AUTO_FETCH_ENABLED", "true").lower() in ("1", "true", "yes")
AUTO_FETCH_MINUTES = int(os.getenv("AUTO_FETCH_MINUTES", "15"))

# A listing whose total is at or below this is highlighted as a "deal".
DEAL_THRESHOLD_EUR = float(os.getenv("DEAL_THRESHOLD_EUR", "60"))

# Marketplaces to scrape, by name (see sniper.sources). Order is fetch order.
ENABLED_SOURCES = [
    s.strip()
    for s in os.getenv(
        "ENABLED_SOURCES",
        "Marktplaats,eBay,Kleinanzeigen,Vinted,2dehands,Tweakers,Catawiki,"
        "Leboncoin,Wallapop,Shpock,Subito,Gumtree,Allegro",
    ).split(",")
    if s.strip()
]

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

FX = {
    "EUR": float(os.getenv("FX_EUR_EUR", "1.0")),
    "GBP": float(os.getenv("FX_GBP_EUR", "1.16")),
    "USD": float(os.getenv("FX_USD_EUR", "0.92")),
    "AUD": float(os.getenv("FX_AUD_EUR", "0.60")),
    "CAD": float(os.getenv("FX_CAD_EUR", "0.68")),
    "PLN": float(os.getenv("FX_PLN_EUR", "0.23")),
}

CURRENCY_SYMBOLS = {
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "$": "USD", "USD": "USD", "US$": "USD",
    "AUD": "AUD", "A$": "AUD",
    "CAD": "CAD", "C$": "CAD",
}

# ---------------------------------------------------------------------------
# Worker -> UI message types
# ---------------------------------------------------------------------------

MSG_STATUS = "STATUS"
MSG_UPSERT = "UPSERT"
MSG_DELETE = "DELETE"
MSG_ERROR = "ERROR"
MSG_DONE = "DONE"
