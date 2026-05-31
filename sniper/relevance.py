"""Relevance heuristic: is a listing an actual ZX Spectrum *computer*?

Marketplace searches for "zx spectrum" return a lot of noise — games on tape,
power supplies, manuals, joysticks, even unrelated MSX lots. This module
classifies a listing title so the UI can focus on the actual machines.

It's deliberately simple and keyword-based; tune the lists below as needed.
The bias is toward *precision* (don't show games) over recall, since the whole
point is to cut through the noise.
"""

from __future__ import annotations

# Strong signals that the listing IS the computer. Note "48k"/"128k" are NOT
# here: games are routinely titled "... ZX Spectrum 48K" (the machine they run
# on), so a bare model number is too weak on its own.
STRONG_COMPUTER_TERMS = (
    "computer", "console", "ordenador", "rechner",
    "+2", "+3", "+2a", "+2b",
    "spectrum plus", "spectrum+", "toastrack",
)

# Model numbers — only meaningful when "Spectrum" leads the title (see below).
WEAK_MODEL_TERMS = ("48k", "48 k", "128k", "128 k", "16k")

# Games / software (incl. well-known Spectrum game publishers). Their presence
# means "game", unless a STRONG computer term is also present (a boxed machine
# bundled with games).
GAME_TERMS = (
    "game", "games", "spiel", "spiele", "spel", "spellen",
    "juego", "juegos", "gioco", "giochi",
    "tape", "cassette", "cassettes", "kassette", "cassetten",
    "software", "diskette",
    "mastertronic", "ultimate play", "ocean software", "imagine software",
    "gremlin graphics", "codemasters", "bug-byte", "melbourne house",
)

# Essentially never the computer, even alongside "spectrum" — accessories,
# spare parts, chips/boards, and clones (Harlequin) rather than a real machine.
STRONG_EXCLUDE_TERMS = (
    "power supply", "powersupply", "netzteil", "voeding", "psu",
    "power adapter", "joystick", "membrane", "keyboard mat",
    "replacement key", "sticker", "t-shirt", "tshirt", " mug",
    "eprom", "faceplate", "repro", "reproduction", "overlay", "poster",
    " rom ", "rom ic", "dual rom", " ula ", "diag", "harlequin",
)

# If "spectrum"/"speccy" appears within this many characters, the listing leads
# with the machine name rather than a game title.
_EARLY_MENTION = 22


def _early_spectrum_mention(t: str) -> bool:
    positions = [p for p in (t.find("spectrum"), t.find("speccy")) if p != -1]
    return bool(positions) and min(positions) <= _EARLY_MENTION


def is_spectrum_computer(title: str) -> bool:
    """Return True if *title* looks like an actual ZX Spectrum computer."""
    t = (title or "").lower().strip()
    if "spectrum" not in t and "speccy" not in t:
        return False
    if any(term in t for term in STRONG_EXCLUDE_TERMS):
        return False
    if any(term in t for term in STRONG_COMPUTER_TERMS):
        return True
    if any(term in t for term in GAME_TERMS):
        return False
    # "Sinclair ZX Spectrum 48K" (machine name first, with a model) — keep.
    if _early_spectrum_mention(t) and any(term in t for term in WEAK_MODEL_TERMS):
        return True
    return False
