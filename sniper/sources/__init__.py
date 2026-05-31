"""Marketplace source registry.

Add a new site by writing a :class:`~sniper.sources.base.Source` subclass in
this package and appending an instance to ``ALL_SOURCES``.
"""

from __future__ import annotations

from typing import List, Optional

from sniper.sources.base import Source
from sniper.sources.allegro import AllegroSource
from sniper.sources.catawiki import CatawikiSource
from sniper.sources.ebay import EbaySource
from sniper.sources.gumtree import GumtreeSource
from sniper.sources.kleinanzeigen import KleinanzeigenSource
from sniper.sources.leboncoin import LeboncoinSource
from sniper.sources.marktplaats import MarktplaatsSource
from sniper.sources.shpock import ShpockSource
from sniper.sources.subito import SubitoSource
from sniper.sources.tweakers import TweakersSource
from sniper.sources.tweedehands import TweedehandsSource
from sniper.sources.vinted import VintedSource
from sniper.sources.wallapop import WallapopSource

ALL_SOURCES: List[Source] = [
    MarktplaatsSource(),
    EbaySource(),
    KleinanzeigenSource(),
    VintedSource(),
    TweedehandsSource(),
    TweakersSource(),
    CatawikiSource(),
    LeboncoinSource(),
    WallapopSource(),
    ShpockSource(),
    SubitoSource(),
    GumtreeSource(),
    AllegroSource(),
]

_BY_NAME = {s.name: s for s in ALL_SOURCES}


def get_sources(names: Optional[List[str]] = None) -> List[Source]:
    """Return source instances for *names* (or all, preserving order)."""
    if not names:
        return list(ALL_SOURCES)
    return [_BY_NAME[n] for n in names if n in _BY_NAME]


def source_names() -> List[str]:
    """Return the names of all registered sources."""
    return [s.name for s in ALL_SOURCES]
