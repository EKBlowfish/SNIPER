from __future__ import annotations

"""Network helpers for performing polite HTTP requests.

This module centralizes logic for creating a ``requests.Session`` configured
with retry behavior and a desktop-like user agent.  It also exposes thin
wrappers that respect a ``threading.Event`` so that long-running network
operations can be cancelled cleanly from other threads.
"""

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
    """Fallback response object returned when HTTP requests fail.

    The object mimics the small portion of the :class:`requests.Response`
    interface used by the application so callers can continue operating on a
    predictable object even when a request raises an exception or is skipped
    due to a stop signal.
    """

    def __init__(self, url: str, status_code: int = 0, text: str = "", content: bytes = b""):
        """Store basic response attributes."""
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = content


def make_session() -> requests.Session:
    """Return a configured :class:`requests.Session` instance.

    The session uses a desktop browser user-agent string and automatically
    retries a handful of transient HTTP errors.  Only ``GET`` requests are
    allowed, matching the usage pattern in this project.

    Returns:
        ``requests.Session`` ready for issuing GET requests.
    """

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


def polite_get(
    session: requests.Session, url: str, stop_event: threading.Event
) -> requests.Response | DummyResponse:
    """Issue a GET request while honouring a stop signal.

    Args:
        session: The :class:`requests.Session` used to perform the request.
        url: Target URL to fetch.
        stop_event: When set, the request is skipped and a ``DummyResponse`` is
            returned instead.

    Returns:
        The :class:`requests.Response` from ``requests`` or ``DummyResponse`` if
        the operation was aborted or raised an exception.
    """

    if stop_event.is_set():
        return DummyResponse(url=url)
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY_SEC)
        return resp
    except Exception:
        return DummyResponse(url=url)


def fetch_bytes(
    session: requests.Session, url: str, stop_event: threading.Event
) -> bytes | None:
    """Download binary content from a URL.

    Args:
        session: Active :class:`requests.Session`.
        url: Resource to fetch.  If ``None`` or empty, no request is made.
        stop_event: Optional cancellation signal.

    Returns:
        Raw bytes of the response if the request succeeds with HTTP 200,
        otherwise ``None``.
    """

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

