"""Polite HTTP client shared by all scrapers.

One `httpx.AsyncClient` (cookies persist, desktop Chrome headers), a lock and a
random pause per host so we never hammer a site, retries with exponential
backoff, and detection of bot-protection / captcha pages.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from types import TracebackType
from typing import Literal
from urllib.parse import urlsplit

import httpx

from ..config import GeneralConfig

log = logging.getLogger(__name__)

CHROME_MAJOR = "140"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "de-DE,de;q=0.9,en;q=0.8"
ACCEPT_DOCUMENT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
ACCEPT_IMAGE = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

IMAGE_HOST_DELAY = 0.3  # max pause between requests to image CDN hosts ("img.*")
MAX_BACKOFF = 120.0

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Markers that only ever appear on challenge / block pages.
_STRONG_BLOCK_MARKERS = (
    "captcha-delivery.com",  # DataDome block page iframe
    "window._cf_chl_opt",  # Cloudflare JS challenge
    "cf-browser-verification",
    "cf-challenge-running",
    "px-captcha",  # PerimeterX
    "_incapsula_resource",  # Imperva
    "incapsula incident id",
    "splashui/captcha",  # eBay captcha
    "/distil_r_captcha",
)
# Markers that are suspicious but might occur in scripts of normal pages
# (e.g. a reCAPTCHA-protected contact form). Only trusted in the <title> or on
# small pages without any normal content.
_WEAK_BLOCK_MARKERS = (
    "captcha",
    "zugriff verweigert",
    "access denied",
    "access to this page has been denied",
    "just a moment...",
    "checking your browser",
    "attention required! | cloudflare",
    "pardon our interruption",
    "sicherheitsabfrage",
    "sicherheitsüberprüfung",
    "kein roboter",
    "not a robot",
    "are you a robot",
    "bist du ein mensch",
    "ungewöhnliche aktivität",
    "unusual traffic",
    "request blocked",
    "zugriff gesperrt",
)
# Present on real Kleinanzeigen / eBay result or ad pages.
_CONTENT_MARKERS = (
    "srchrslt-adtable",
    'class="aditem',
    "viewad-title",
    "viewad-description",
    "srp-results",
    's-item__',
    "s-card__",
)
_SMALL_PAGE = 30_000


class BlockedError(Exception):
    """The site refused us: HTTP 403, persistent 429, or a captcha / bot-check page."""

    def __init__(self, message: str, *, url: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


def looks_blocked(html: str) -> bool:
    """Heuristic: is this a captcha / bot-protection page rather than real content?"""
    if not html:
        return False
    low = html.lower()
    if any(m in low for m in _STRONG_BLOCK_MARKERS):
        return True
    title_match = _TITLE_RE.search(low)
    title = title_match.group(1) if title_match else ""
    if any(m in title for m in _WEAK_BLOCK_MARKERS):
        return True
    if any(m in low for m in _CONTENT_MARKERS):
        return False
    return len(low) < _SMALL_PAGE and any(m in low for m in _WEAK_BLOCK_MARKERS)


def _blocked_url(url: httpx.URL) -> bool:
    """Redirected to a captcha endpoint (eBay: /splashui/captcha, DataDome, ...)."""
    return "captcha" in url.path.lower() or "captcha" in (url.host or "").lower()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "").strip()
    try:
        return float(value) if value else None
    except ValueError:
        return None  # HTTP-date form: ignore, use our own backoff


class PoliteClient:
    """Rate-limited, retrying HTTP client. Use as `async with PoliteClient() as c:`."""

    BACKOFF_BASE = 2.0  # seconds; attempt n waits BACKOFF_BASE * 2**n (+ jitter)

    def __init__(
        self,
        delay_range: tuple[float, float] = (3.0, 7.0),
        timeout: float = 25.0,
        user_agent: str | None = None,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        lo, hi = (max(0.0, float(v)) for v in delay_range)
        self.delay_range: tuple[float, float] = (min(lo, hi), max(lo, hi))
        self.max_retries = max(0, int(max_retries))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}  # host -> monotonic time of last response
        self.last_url: str | None = None
        self.last_status: int | None = None
        self._client = httpx.AsyncClient(
            http2=False,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": self.user_agent, "Accept-Language": ACCEPT_LANGUAGE},
            transport=transport,
        )

    @classmethod
    def from_config(cls, general: GeneralConfig) -> "PoliteClient":
        lo, hi = general.request_delay_seconds
        return cls(
            delay_range=(lo, hi),
            timeout=general.request_timeout_seconds,
            user_agent=general.user_agent,
        )

    # -- public API ---------------------------------------------------------

    async def get_text(self, url: str, *, referer: str | None = None) -> str:
        """GET an HTML page. Raises BlockedError on bot protection."""
        response = await self._request(url, referer=referer, kind="document")
        self.last_url, self.last_status = str(response.url), response.status_code  # for diagnostics
        text = response.text
        if _blocked_url(response.url) or looks_blocked(text):
            raise BlockedError(
                f"bot protection page at {response.url}", url=str(response.url),
                status_code=response.status_code,
            )
        return text

    async def get_bytes(self, url: str) -> bytes:
        """GET binary content (images)."""
        response = await self._request(url, referer=None, kind="image")
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- internals ----------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:  # separate method so tests can patch it
        await asyncio.sleep(seconds)

    def _headers(self, url: str, referer: str | None, kind: Literal["document", "image"]) -> dict[str, str]:
        host = urlsplit(url).hostname or ""
        ref_host = urlsplit(referer).hostname if referer else None
        if ref_host is None:
            site = "none" if kind == "document" else "cross-site"
        elif ref_host == host:
            site = "same-origin"
        elif ref_host.split(".")[-2:] == host.split(".")[-2:]:
            site = "same-site"
        else:
            site = "cross-site"
        headers: dict[str, str] = {}
        if kind == "document":
            headers.update(
                {
                    "Accept": ACCEPT_DOCUMENT,
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": site,
                    "Sec-Fetch-User": "?1",
                }
            )
        else:
            headers.update(
                {
                    "Accept": ACCEPT_IMAGE,
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": site,
                }
            )
        if "Chrome/" in self.user_agent:  # client hints only make sense for Chrome UAs
            major = re.search(r"Chrome/(\d+)", self.user_agent)
            ver = major.group(1) if major else CHROME_MAJOR
            headers["sec-ch-ua"] = f'"Chromium";v="{ver}", "Not=A?Brand";v="24", "Google Chrome";v="{ver}"'
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = '"Windows"'
        if referer:
            headers["Referer"] = referer
        return headers

    def _pick_delay(self, host: str) -> float:
        lo, hi = self.delay_range
        delay = random.uniform(lo, hi) if hi > 0 else 0.0
        if host.startswith("img."):
            delay = min(delay, IMAGE_HOST_DELAY)
        return delay

    async def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is None:
            return
        remaining = self._pick_delay(host) - (time.monotonic() - last)
        if remaining > 0:
            await self._sleep(remaining)

    async def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = self.BACKOFF_BASE * (2**attempt) + random.uniform(0, self.BACKOFF_BASE / 2)
        if retry_after is not None:
            delay = max(delay, retry_after)
        await self._sleep(min(delay, MAX_BACKOFF))

    async def _request(
        self, url: str, *, referer: str | None, kind: Literal["document", "image"]
    ) -> httpx.Response:
        host = (urlsplit(url).hostname or "").lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        headers = self._headers(url, referer, kind)
        async with lock:  # serialize all requests to one host
            await self._wait_turn(host)
            attempt = 0
            while True:
                try:
                    response = await self._client.get(url, headers=headers)
                except httpx.TransportError as exc:
                    self._last_request[host] = time.monotonic()
                    if attempt >= self.max_retries:
                        raise
                    log.warning("GET %s failed (%s), retry %d/%d", url, exc, attempt + 1, self.max_retries)
                    await self._backoff(attempt, None)
                    attempt += 1
                    continue
                self._last_request[host] = time.monotonic()
                status = response.status_code
                if status == 403:
                    raise BlockedError(f"HTTP 403 for {url}", url=url, status_code=status)
                if status == 429 or status >= 500:
                    if attempt >= self.max_retries:
                        if status == 429:
                            raise BlockedError(f"HTTP 429 (rate limited) for {url}", url=url, status_code=status)
                        response.raise_for_status()
                    log.warning("GET %s -> HTTP %d, retry %d/%d", url, status, attempt + 1, self.max_retries)
                    await self._backoff(attempt, _retry_after(response))
                    attempt += 1
                    continue
                response.raise_for_status()
                return response
