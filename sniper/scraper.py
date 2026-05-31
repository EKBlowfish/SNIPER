"""Search/parse logic and the background fetch worker.

Shared by both the desktop (Tkinter) and web (Flask) front-ends: each supplies
its own ``broadcast`` callback so worker events can be routed to a Tk queue or
an SSE stream without the scraper knowing the difference.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from sniper.config import (
    CURRENCY_SYMBOLS,
    FX,
    MSG_DELETE,
    MSG_DONE,
    MSG_ERROR,
    MSG_STATUS,
    MSG_UPSERT,
)
from sniper.models import Item
from sniper.net import fetch_bytes, make_session, polite_get
from sniper.store import Store

log = logging.getLogger(__name__)

# =========================
# Currency & parsing
# =========================

MONEY_RE = re.compile(
    r"([€£$]|EUR|GBP|USD|AUD|CAD|US\$|A\$|C\$)?\s*"
    r"([0-9]{1,3}(?:[.,\s][0-9]{3})*|[0-9]+)"
    r"(?:[.,]([0-9]{1,2}))?",
    re.I,
)


def parse_money_to_eur(txt: Optional[str]) -> Optional[float]:
    """Parse a money string and convert to EUR using static FX rates."""
    if not txt:
        return None
    s = txt.strip()
    curr = None
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in s:
            curr = code
            break
    if not curr:
        for code in ("EUR", "GBP", "USD", "AUD", "CAD"):
            if re.search(rf"\b{code}\b", s, re.I):
                curr = code
                break
    if not curr:
        curr = "EUR"

    m = MONEY_RE.search(s)
    if not m:
        return None

    _, intpart, frac = m.groups()
    clean_int = re.sub(r"[.,\s]", "", intpart)
    value = float(clean_int)
    if frac:
        value += float(frac) / (10 ** len(frac))

    fx = FX.get(curr, 1.0)
    return round(value * fx, 2)


def compute_total(price_eur: Optional[float], ship_eur: Optional[float]) -> Optional[float]:
    """Combine price and shipping into a rounded total."""
    if price_eur is None and ship_eur is None:
        return None
    return round((price_eur or 0.0) + (ship_eur or 0.0), 2)


# =========================
# Sparkline
# =========================

SPARK_BARS = "▁▂▃▄▅▆▇"


def sparkline(values: List[float]) -> str:
    """Render a tiny sparkline graph from a list of numbers."""
    values = [v for v in values if v is not None]
    if not values:
        return ""
    vmin = min(values)
    vmax = max(values)
    if vmin == vmax:
        return SPARK_BARS[0] * min(len(values), 16)
    out = []
    for v in values[-16:]:
        idx = int((v - vmin) / (vmax - vmin) * (len(SPARK_BARS) - 1))
        out.append(SPARK_BARS[idx])
    return "".join(out)


def truncate(s: str, n: int) -> str:
    """Truncate a string to *n* characters with an ellipsis."""
    return s if len(s) <= n else s[: n - 1] + "…"


# =========================
# URL helpers
# =========================


EBAY_DOMAIN = "www.ebay.de"

def build_search_urls(term: str) -> tuple[str, str]:
    """Return (marktplaats_url, ebay_url) for the given search term."""
    enc = quote_plus(term)
    mp = f"https://www.marktplaats.nl/l/computers-en-software/vintage-computers/q/{enc}/"
    ebay = f"https://{EBAY_DOMAIN}/sch/i.html?_nkw={enc}&_sacat=11189"
    return mp, ebay


# =========================
# Marktplaats
# =========================

MP_AD_RE = re.compile(r"/v/[^\"'\s]+/(m\d+)-", re.I)
MP_ITEMLIST_RE = re.compile(r'"itemListElement"\s*:\s*(\[[^\]]*\])', re.S)


def discover_mp_urls(html: str) -> List[str]:
    """Extract Marktplaats ad URLs from a search result page.

    Multiple extraction methods (anchors, regex, JSON-LD) can find different
    URL paths for the *same* ad.  We deduplicate by ad-ID (the ``mNNNNN``
    portion) so each listing is visited only once.
    """
    urls: List[str] = []

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/v/" in href and "/m" in href:
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://www.marktplaats.nl" + href
            urls.append(href)

    for m in MP_AD_RE.finditer(html):
        path = m.group(0)
        full = "https://www.marktplaats.nl" + path if path.startswith("/") else path
        urls.append(full)

    mjson = MP_ITEMLIST_RE.search(html)
    if mjson:
        try:
            arr = json.loads(mjson.group(1))
            for el in arr:
                if not isinstance(el, dict):
                    continue
                item = el.get("item") or {}
                if not isinstance(item, dict):
                    continue
                u = item.get("@id") or item.get("url")
                if isinstance(u, str) and "/v/" in u:
                    if u.startswith("//"):
                        u = "https:" + u
                    elif u.startswith("/"):
                        u = "https://www.marktplaats.nl" + u
                    urls.append(u)
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass

    # Deduplicate by ad-ID (e.g. m2397872985) — different extraction methods
    # often find different URL paths for the same listing.
    seen_ids: set[str] = set()
    deduped: List[str] = []
    for u in urls:
        m = re.search(r"/(m\d+)-", u)
        ad_id = m.group(1) if m else u  # fall back to full URL if no ID
        if ad_id not in seen_ids:
            seen_ids.add(ad_id)
            deduped.append(u)
    return deduped


def _extract_mp_jsonld(soup: BeautifulSoup) -> dict:
    """Extract the Product JSON-LD block from a Marktplaats ad page."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _extract_mp_shipping_cents(html: str) -> Optional[int]:
    """Extract the cheapest shipping price in cents from embedded config."""
    m = re.search(r'"shipping_price"\s*:\s*"(\{.*?\})"', html)
    if not m:
        return None
    try:
        raw = m.group(1).replace('\\"', '"').replace("\\u002F", "/")
        prices = json.loads(raw)
        cents = [v for v in prices.values() if isinstance(v, (int, float)) and v > 0]
        return min(cents) if cents else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def parse_mp_ad(session: requests.Session, url: str, stop_event: threading.Event) -> tuple[Item, int]:
    """Fetch and parse a single Marktplaats ad into an (Item, http_status) tuple."""
    r = polite_get(session, url, stop_event)
    status = getattr(r, "status_code", 0)
    html = getattr(r, "text", "") or ""
    ad_id_match = re.search(r"/(m\d+)-", url)
    ad_id = ad_id_match.group(1) if ad_id_match else f"m{abs(hash(url)) % 10**10}"
    key = f"MP:{ad_id}"

    title = ""
    price_eur = None
    ship_eur = None
    type_s = ""
    thumb_url = None

    if status == 200 and html:
        soup = BeautifulSoup(html, "html.parser")

        # ---- Title ----
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
        if not title:
            mt = soup.find("meta", property="og:title")
            if mt and mt.get("content"):
                title = mt["content"].strip()

        # ---- Type: auction vs buy-now ----
        if re.search(r"\bbieden\b", html, re.I):
            type_s = "🧷 Auction"
        else:
            type_s = "🛒 Buy Now"

        # ---- Price (primary: JSON-LD Product.offers.price) ----
        jsonld = _extract_mp_jsonld(soup)
        offers = jsonld.get("offers") or {}
        if isinstance(offers, dict):
            raw_price = offers.get("price")
            currency = (offers.get("priceCurrency") or "EUR").upper()
            if raw_price is not None:
                try:
                    price_val = float(raw_price)
                    fx = FX.get(currency, 1.0)
                    price_eur = round(price_val * fx, 2)
                except (ValueError, TypeError):
                    pass

        # Fallback: HTML selectors (older MP layout)
        if price_eur is None:
            price_el = (
                soup.select_one('[data-test="price"]')
                or soup.find("span", string=re.compile(r"€|EUR|Prijs|Vraagprijs", re.I))
                or soup.find(attrs={"itemprop": "price"})
            )
            if price_el:
                price_eur = parse_money_to_eur(price_el.get_text(" ", strip=True))

        # ---- Shipping (primary: embedded shipping_price JSON in cents) ----
        ship_cents = _extract_mp_shipping_cents(html)
        if ship_cents is not None:
            ship_eur = round(ship_cents / 100.0, 2)

        # Fallback: HTML text near "Verzenden"
        if ship_eur is None:
            ship_candidates = soup.find_all(string=re.compile(r"Verzenden|PostNL|Verzendkosten", re.I))
            for s in ship_candidates:
                parent_txt = s.parent.get_text(" ", strip=True) if hasattr(s, "parent") else str(s)
                mny = MONEY_RE.search(parent_txt)
                if mny:
                    ship_eur = parse_money_to_eur(parent_txt)
                    break

        # ---- Thumbnail ----
        jsonld_images = jsonld.get("image") or []
        if isinstance(jsonld_images, list) and jsonld_images:
            thumb_url = jsonld_images[0]
        if not thumb_url:
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                thumb_url = og["content"]
        if not thumb_url:
            imgel = soup.find("img")
            if imgel and imgel.get("src"):
                thumb_url = imgel["src"]
        if thumb_url and thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

        if not title:
            title = jsonld.get("name", "")

    else:
        title = f"(HTTP {status})"

    total_eur = compute_total(price_eur, ship_eur)
    return Item(
        key=key,
        source="Marktplaats",
        title=title or "",
        link=url,
        price_eur=price_eur,
        ship_eur=ship_eur,
        total_eur=total_eur,
        type=type_s,
        thumb_url=thumb_url,
    ), status


# =========================
# eBay
# =========================

EBAY_ITM_ID_RE = re.compile(r"/itm/(\d+)")
EBAY_ID_FROM_DATA_RE = re.compile(r'"itemId"\s*:\s*"(\d+)"')

# Card containers across eBay's various result-page templates (classic
# ".s-item", the newer ".s-card"/".su-card-container", and grid variants).
EBAY_CARD_SELECTORS = [
    "li.s-item",
    ".s-item",
    "[data-testid='item']",
    "li.s-card",
    ".s-card",
    "li.su-card-container",
    ".su-card-container",
]

# Title/price/shipping selectors, tried in order, across those templates.
EBAY_TITLE_SELECTORS = [
    ".s-item__title",
    ".s-card__title",
    "[data-testid='title']",
    "[role='heading']",
    ".su-styled-text.primary",
]
EBAY_PRICE_SELECTORS = [
    ".s-item__price",
    ".s-card__price",
    "[data-testid='price']",
    ".su-styled-text.positive",
]
EBAY_SHIP_SELECTORS = [
    ".s-item__shipping",
    ".s-item__logisticsCost",
    ".s-card__shipping",
    "[data-testid='shipping']",
]
EBAY_TYPE_SELECTORS = [
    ".s-item__purchaseOptions",
    ".s-card__purchase-options",
    "[data-testid='purchase-options']",
]

# Placeholder / non-listing titles eBay injects into the results.
EBAY_TITLE_NOISE = {"shop on ebay", "results matching fewer words"}
EBAY_TITLE_PREFIX_RE = re.compile(r"^\s*new listing\s*", re.I)


def _first_text(el, selectors: List[str]) -> str:
    """Return text from the first matching selector, else ''."""
    for sel in selectors:
        t = el.select_one(sel)
        if t:
            txt = t.get_text(" ", strip=True)
            if txt:
                return txt
    return ""


def _clean_ebay_title(raw: str, link_el) -> str:
    """Normalise an eBay title, falling back to the link text."""
    title = EBAY_TITLE_PREFIX_RE.sub("", raw or "").strip()
    if not title and link_el is not None:
        title = EBAY_TITLE_PREFIX_RE.sub("", link_el.get_text(" ", strip=True)).strip()
    return title


def parse_ebay_results(session: requests.Session, html: str) -> List[Item]:
    """Parse an eBay search results page into a list of Items.

    Cards without a usable title are skipped rather than stored as blank
    rows — older versions emitted those whenever the markup changed.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Collect candidate cards from every known template, de-duplicated by
    # object identity so a card matched by two selectors is processed once.
    cards = []
    seen_cards = set()
    for sel in EBAY_CARD_SELECTORS:
        for el in soup.select(sel):
            if id(el) not in seen_cards:
                seen_cards.add(id(el))
                cards.append(el)

    items: List[Item] = []
    seen_ids = set()

    def pick_thumb(el) -> Optional[str]:
        img = el.select_one("img")
        if img:
            u = img.get("src") or img.get("data-src") or img.get("data-image-url")
            if u:
                return "https:" + u if u.startswith("//") else u
        return None

    for el in cards:
        link_el = el.select_one("a[href]")
        if not link_el:
            continue
        url = link_el.get("href") or ""
        m = EBAY_ITM_ID_RE.search(url)
        item_id = m.group(1) if m else None
        if not item_id:
            m2 = EBAY_ID_FROM_DATA_RE.search(str(el))
            if m2:
                item_id = m2.group(1)
        if not item_id or item_id in seen_ids:
            continue

        title = _clean_ebay_title(_first_text(el, EBAY_TITLE_SELECTORS), link_el)
        # Skip placeholder cards and anything we couldn't extract a title for.
        if not title or title.lower() in EBAY_TITLE_NOISE:
            continue
        seen_ids.add(item_id)

        price_text = _first_text(el, EBAY_PRICE_SELECTORS)
        ship_text = _first_text(el, EBAY_SHIP_SELECTORS)

        typetxt = _first_text(el, EBAY_TYPE_SELECTORS).lower()
        if "auction" in typetxt or "bied" in typetxt or "bid" in typetxt:
            type_s = "🧷 Auction"
        elif "koop" in typetxt or "buy" in typetxt or "nu kopen" in typetxt:
            type_s = "🛒 Buy Now"
        elif re.search(r"\bbid\b|\bauction\b|\bbieden\b", title.lower()):
            type_s = "🧷 Auction"
        else:
            type_s = "🛒 Buy Now"

        price_eur = parse_money_to_eur(price_text)
        ship_eur = parse_money_to_eur(ship_text)
        total_eur = compute_total(price_eur, ship_eur)

        items.append(Item(
            key=f"EBAY:{item_id}",
            source="eBay",
            title=title,
            link=url,
            price_eur=price_eur,
            ship_eur=ship_eur,
            total_eur=total_eur,
            type=type_s,
            thumb_url=pick_thumb(el),
        ))

    return items


# =========================
# eBay fetch helper
# =========================

def _fetch_ebay_html(
    session: requests.Session,
    url: str,
    stop_event: threading.Event,
) -> tuple[str, int]:
    """Fetch an eBay search page, using curl_cffi if available.

    eBay aggressively blocks plain ``requests`` with HTTP 403.  The
    ``curl_cffi`` library spoofs a real browser TLS fingerprint, which
    bypasses that check.  If the library is not installed, we fall back
    to the normal session (which will likely get a 403).
    """
    if stop_event.is_set():
        return "", 0
    try:
        from curl_cffi import requests as cf_requests  # type: ignore[import-untyped]
        import time as _time

        log.info("Fetching eBay via curl_cffi: %s", url)

        # Use a session for persistent cookies.  Warming up with the
        # homepage first makes eBay much less likely to serve a
        # CAPTCHA on the subsequent search request.
        s = cf_requests.Session(impersonate="chrome131")
        s.headers.update({"Accept-Language": "en-US,en;q=0.9,de;q=0.8,nl;q=0.7"})
        try:
            s.get(f"https://{EBAY_DOMAIN}/", timeout=15)
            _time.sleep(1.5)
        except Exception:
            pass  # best-effort warmup

        r = s.get(url, timeout=20)
        _time.sleep(1.2)
        html = r.text or ""
        log.info("curl_cffi eBay result: HTTP %s, %d bytes", r.status_code, len(html))

        # Detect interception pages ("Pardon Our Interruption")
        if r.status_code == 200 and len(html) < 50_000:
            if "Pardon Our Interruption" in html or "captcha" in html.lower():
                log.warning("eBay returned CAPTCHA page — treating as 403")
                return ("", 403)

        return (html, r.status_code)
    except ImportError:
        log.warning("curl_cffi not installed — falling back to requests for eBay")
    except Exception as e:
        log.warning("curl_cffi eBay fetch failed: %s — falling back to requests", e)

    # Fallback to plain requests (will likely 403)
    r2 = polite_get(session, url, stop_event)
    return (getattr(r2, "text", "") or "", getattr(r2, "status_code", 0))


# =========================
# Worker
# =========================


def worker_fetch(
    broadcast: Callable[[str, Dict[str, Any]], None],
    stop_event: threading.Event,
    db: Store,
    term: str,
    source_names: Optional[List[str]] = None,
):
    """Background thread: run each enabled source and broadcast updates.

    ``term`` is the search text and ``source_names`` selects which registered
    marketplaces to run (default: all). ``broadcast(event_type, data)`` is the
    transport supplied by the caller, so both the Tkinter and Flask apps can
    route events their own way.
    """
    # Imported lazily to avoid an import cycle: sources depend on this module.
    from sniper.sources import get_sources
    from sniper.sources.base import Emitter

    session = make_session()
    emit = Emitter(broadcast, db, session, stop_event)

    try:
        for source in get_sources(source_names):
            if stop_event.is_set():
                break
            try:
                source.search(session, term, stop_event, emit)
            except (requests.RequestException, OSError, ValueError) as e:
                emit.error(f"{source.name} error: {e}")

        # Gradually clean up stale listings not seen in 7+ days.
        # We remove up to 10 per cycle to avoid a big UI disruption.
        if not stop_event.is_set():
            stale = db.get_stale_keys(max_age_days=7, limit=10)
            for key in stale:
                emit.delete(key)
            if stale:
                broadcast(MSG_STATUS, {"text": f"Cleaned {len(stale)} stale listing(s)"})
    finally:
        broadcast(MSG_DONE, {"text": "Fetch complete"})
