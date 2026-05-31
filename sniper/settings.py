"""User settings persisted to a JSON file on disk.

Stores the things a user tweaks in the UI — search term, theme, filters,
sort order and notification preferences — so they survive a restart.
"""

from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from typing import Any, Dict

from sniper.config import DEFAULT_SEARCH_ITEM, SETTINGS_FILE

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "search_term": DEFAULT_SEARCH_ITEM,
    "theme": "light",  # "light" | "dark"
    "filters": {
        "text": "",
        "show_mp": True,
        "show_ebay": True,
        "show_ka": True,
        "show_vinted": True,
        "price_min": None,
        "price_max": None,
        "deals_only": False,
        "spectrums_only": True,
    },
    "sort": {
        "column": None,
        "descending": False,
    },
    "notify": {
        "enabled": False,
        "sound": False,
    },
}

_lock = threading.Lock()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings() -> Dict[str, Any]:
    """Return saved settings merged over defaults (defaults fill any gaps)."""
    with _lock:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return deepcopy(DEFAULTS)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read settings (%s); using defaults.", e)
            return deepcopy(DEFAULTS)
    if not isinstance(stored, dict):
        return deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, stored)


def save_settings(partial: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``partial`` into the saved settings, persist, and return the result."""
    current = load_settings()
    merged = _deep_merge(current, partial or {})
    with _lock:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
        except OSError as e:
            log.warning("Could not write settings: %s", e)
    return merged
