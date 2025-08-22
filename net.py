from __future__ import annotations

import time
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ACCEPT_LANG = "nl-NL,nl;q=0.9,en;q=0.8"
REQUEST_TIMEOUT = 20
POLITE_DELAY_SEC = 1.2


class DummyResponse:
    """Fallback response object returned when HTTP requests fail."""

    def __init__(self, url: str, status_code: int = 0, text: str = "", content: bytes = b""):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = content


def make_session() -> requests.Session:
    """Create a requests session with retry and desktop browser headers."""
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(
        {
            "User-Agent": DESKTOP_UA,
            "Accept-Language": ACCEPT_LANG,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return s


def polite_get(session: requests.Session, url: str, stop_event: threading.Event) -> requests.Response | DummyResponse:
    """GET a URL respecting stop signals."""
    if stop_event.is_set():
        return DummyResponse(url=url)
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY_SEC)
        return resp
    except Exception:
        return DummyResponse(url=url)


def fetch_bytes(session: requests.Session, url: str, stop_event: threading.Event) -> bytes | None:
    """Fetch raw bytes from a URL, respecting stop signals."""
    if not url or stop_event.is_set():
        return None
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            time.sleep(POLITE_DELAY_SEC / 2)
            return r.content
    except Exception:
        pass
    return None

