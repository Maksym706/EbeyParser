"""Polite HTTP client shared by all scrapers.

One `httpx.AsyncClient` (cookies persist, desktop Chrome headers), a lock and a
random pause per host so we never hammer a site, retries with exponential
backoff, and detection of bot-protection / captcha pages.

Ban protection (v0.2):
- an hourly page budget per host (sliding window). When it is used up the client
  raises `RateBudgetExceeded` right away instead of sleeping for up to an hour;
- a growing cooldown per host after a block (403, 429 after retries, captcha):
  1 h, 2 h, 4 h, 12 h, ... (`block_cooldown_hours`), reset after a clean day.
  While a host cools down every page request fails fast with `BlockedError`.
  Cooldowns and the page window survive restarts via a small JSON state file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Literal, Sequence
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

HOUR = 3600.0
CLEAN_DAY = 24 * HOUR  # no block for this long after a cooldown ended -> strikes reset
DEFAULT_MAX_REQUESTS_PER_HOUR = 150
DEFAULT_COOLDOWN_HOURS: tuple[float, ...] = (1.0, 2.0, 4.0, 12.0)
IMAGE_BUDGET_FACTOR = 10  # images get their own, generous cap: 10x the page cap
STATE_VERSION = 1
STATE_SAVE_INTERVAL = 30.0  # seconds between routine state writes (blocks are saved at once)

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

Kind = Literal["document", "image"]


class BlockedError(Exception):
    """The site refused us: HTTP 403, persistent 429, or a captcha / bot-check page —
    or the host is still cooling down after such a block (`cooling_down=True`, no
    request was sent). `cooldown_until` (aware UTC) says when requests resume."""

    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        status_code: int | None = None,
        host: str = "",
        cooldown_until: datetime | None = None,
        cooling_down: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.host = host
        self.cooldown_until = cooldown_until
        self.cooling_down = cooling_down


class RateBudgetExceeded(Exception):
    """Our own hourly page budget for a host is used up; NO request was sent.

    Not a block — nothing is wrong with the site. Callers should DEFER the rest of
    the work (keep what they already have, continue on a later pass) instead of
    stopping like on BlockedError. `retry_at` (aware UTC) is when the next page
    fits into the sliding window again."""

    def __init__(self, message: str, *, host: str, limit: int, retry_at: datetime, url: str = "") -> None:
        super().__init__(message)
        self.host = host
        self.limit = limit
        self.retry_at = retry_at
        self.url = url


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


def _host_of(url_or_host: str) -> str:
    if "://" in url_or_host:
        return (urlsplit(url_or_host).hostname or "").lower()
    return url_or_host.strip().lower()


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def format_duration(seconds: float) -> str:
    """3720 -> '1 ч 2 мин' (Russian, for user-facing messages)."""
    if seconds <= 0:
        return "меньше минуты"
    minutes = max(1, round(seconds / 60))  # never "ещё 0 мин"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч" if hours else f"{minutes} мин"


def _local_time(ts: float, now: float) -> str:
    """Wall-clock time in the user's time zone: '14:05' today, '29.09 03:10' otherwise."""
    when, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    return f"{when:%H:%M}" if when.date() == today.date() else f"{when:%d.%m %H:%M}"


@dataclass
class _HostState:
    """Block history of one host (persisted)."""

    strikes: int = 0  # blocks in a row (reset after a clean day)
    cooldown_until: float = 0.0  # epoch seconds
    last_block_at: float = 0.0
    last_reason: str = ""


class PoliteClient:
    """Rate-limited, retrying HTTP client. Use as `async with PoliteClient() as c:`.

    `max_requests_per_hour` caps HTML pages per host (None/0 = unlimited, the default
    for direct construction; `from_config` applies general.max_requests_per_hour).
    `state_path` persists cooldowns and the page window (JSON); without it they live
    in memory only. `clock` returns epoch seconds and exists for tests."""

    BACKOFF_BASE = 2.0  # seconds; attempt n waits BACKOFF_BASE * 2**n (+ jitter)

    def __init__(
        self,
        delay_range: tuple[float, float] = (3.0, 7.0),
        timeout: float = 25.0,
        user_agent: str | None = None,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_requests_per_hour: int | None = None,
        image_requests_per_hour: int | None = None,
        cooldown_hours: Sequence[float] | None = None,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        lo, hi = (max(0.0, float(v)) for v in delay_range)
        self.delay_range: tuple[float, float] = (min(lo, hi), max(lo, hi))
        self.max_retries = max(0, int(max_retries))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.max_requests_per_hour = int(max_requests_per_hour) if max_requests_per_hour else None
        if image_requests_per_hour:
            self.image_requests_per_hour: int | None = int(image_requests_per_hour)
        elif self.max_requests_per_hour:
            self.image_requests_per_hour = self.max_requests_per_hour * IMAGE_BUDGET_FACTOR
        else:
            self.image_requests_per_hour = None
        steps = DEFAULT_COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours
        self.cooldown_hours: tuple[float, ...] = tuple(max(0.0, float(h)) for h in steps)
        self.state_path = Path(state_path) if state_path else None
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}  # host -> monotonic time of last response
        self._pages: dict[str, deque[float]] = {}  # host -> epoch times of page requests (last hour)
        self._images: dict[str, deque[float]] = {}
        self._hosts: dict[str, _HostState] = {}
        self._last_save = float("-inf")
        self._dirty = False
        self.last_url: str | None = None
        self.last_status: int | None = None
        self._load_state()
        self._client = httpx.AsyncClient(
            http2=False,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": self.user_agent, "Accept-Language": ACCEPT_LANGUAGE},
            transport=transport,
        )

    @classmethod
    def from_config(cls, general: GeneralConfig, state_path: str | Path | None = None) -> "PoliteClient":
        lo, hi = general.request_delay_seconds
        cooldown = getattr(general, "block_cooldown_hours", None)
        return cls(
            delay_range=(lo, hi),
            timeout=general.request_timeout_seconds,
            user_agent=general.user_agent,
            max_requests_per_hour=getattr(general, "max_requests_per_hour", DEFAULT_MAX_REQUESTS_PER_HOUR),
            cooldown_hours=DEFAULT_COOLDOWN_HOURS if cooldown is None else cooldown,
            state_path=state_path,
        )

    # -- public API ---------------------------------------------------------

    async def get_text(self, url: str, *, referer: str | None = None) -> str:
        """GET an HTML page. Raises BlockedError on bot protection / during a cooldown,
        RateBudgetExceeded when this host's hourly page budget is used up."""
        response = await self._request(url, referer=referer, kind="document")
        self.last_url, self.last_status = str(response.url), response.status_code  # for diagnostics
        text = response.text
        if _blocked_url(response.url) or looks_blocked(text):
            host = (urlsplit(url).hostname or "").lower()
            raise self._blocked(
                host, "document", f"страница защиты от ботов ({response.url})",
                url=str(response.url), status_code=response.status_code,
            )
        return text

    async def get_bytes(self, url: str) -> bytes:
        """GET binary content (images). Own, generous hourly cap; blocks don't start a cooldown."""
        response = await self._request(url, referer=None, kind="image")
        return response.content

    def host_status(self) -> dict[str, dict[str, Any]]:
        """Per host: pages/images in the last hour, limits, cooldown — for the status page.

        {"www.kleinanzeigen.de": {"requests_last_hour": 37, "images_last_hour": 0,
          "limit_per_hour": 150, "remaining": 113, "blocked": False, "cooldown_until": None,
          "cooldown_seconds": 0.0, "strikes": 0, "last_block_at": None, "last_block_reason": "",
          "note": "..."}}  (datetimes are aware UTC)"""
        now = self._clock()
        hosts = sorted(set(self._pages) | set(self._images) | set(self._hosts))
        out: dict[str, dict[str, Any]] = {}
        for host in hosts:
            pages = self._window(self._pages, host, now)
            images = self._window(self._images, host, now)
            state = self._state(host, now)
            cooling = state.cooldown_until > now
            limit = self.max_requests_per_hour
            info: dict[str, Any] = {
                "requests_last_hour": len(pages),
                "images_last_hour": len(images),
                "limit_per_hour": limit,
                "remaining": max(0, limit - len(pages)) if limit else None,
                "blocked": cooling,
                "cooldown_until": _utc(state.cooldown_until) if cooling else None,
                "cooldown_seconds": max(0.0, state.cooldown_until - now) if cooling else 0.0,
                "strikes": state.strikes,
                "last_block_at": _utc(state.last_block_at) if state.last_block_at else None,
                "last_block_reason": state.last_reason,
            }
            if cooling:
                info["note"] = (f"пауза после блокировки ещё {format_duration(state.cooldown_until - now)}"
                                f" (до {_local_time(state.cooldown_until, now)})")
            elif limit and len(pages) >= limit:
                info["note"] = f"лимит {limit} страниц в час исчерпан"
            else:
                info["note"] = f"страниц за час: {len(pages)}" + (f" из {limit}" if limit else "")
            out[host] = info
        return out

    def cooldown_remaining(self, url_or_host: str) -> float:
        """Seconds until page requests to this host are allowed again (0 = not cooling down)."""
        now = self._clock()
        state = self._hosts.get(_host_of(url_or_host))
        return max(0.0, state.cooldown_until - now) if state else 0.0

    def budget_left(self, url_or_host: str) -> int | None:
        """HTML pages still allowed to this host in the current hour (None = unlimited)."""
        if not self.max_requests_per_hour:
            return None
        used = len(self._window(self._pages, _host_of(url_or_host), self._clock()))
        return max(0, self.max_requests_per_hour - used)

    async def aclose(self) -> None:
        self._save_state(force=True)
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

    # -- ban protection -----------------------------------------------------

    @staticmethod
    def _window(windows: dict[str, deque[float]], host: str, now: float) -> deque[float]:
        window = windows.setdefault(host, deque())
        while window and window[0] <= now - HOUR:
            window.popleft()
        return window

    def _state(self, host: str, now: float) -> _HostState:
        state = self._hosts.setdefault(host, _HostState())
        if state.strikes and state.cooldown_until and now - state.cooldown_until >= CLEAN_DAY:
            state.strikes = 0  # a clean day after the last cooldown: start from the first step again
            self._dirty = True
        return state

    def _check_access(self, host: str, url: str, kind: Kind) -> None:
        """Fail fast (no request) while cooling down or when the hourly budget is used up."""
        now = self._clock()
        if kind == "document":
            state = self._state(host, now)
            if state.cooldown_until > now:
                reason = f" ({state.last_reason})" if state.last_reason else ""
                raise BlockedError(
                    f"{host}: пауза после блокировки{reason} — ещё {format_duration(state.cooldown_until - now)}"
                    f" (до {_local_time(state.cooldown_until, now)}), запросы к сайту не отправляются",
                    url=url, host=host, cooldown_until=_utc(state.cooldown_until), cooling_down=True,
                )
        limit = self.max_requests_per_hour if kind == "document" else self.image_requests_per_hour
        if not limit:
            return
        window = self._window(self._pages if kind == "document" else self._images, host, now)
        if len(window) >= limit:
            retry_at = window[0] + HOUR
            what = "страниц" if kind == "document" else "картинок"
            raise RateBudgetExceeded(
                f"{host}: лимит {limit} {what} в час исчерпан — продолжу через {format_duration(retry_at - now)}"
                f" (около {_local_time(retry_at, now)})",
                host=host, limit=limit, retry_at=_utc(retry_at), url=url,
            )

    def _count(self, host: str, kind: Kind, n: int = 1) -> None:
        if n <= 0:
            return
        now = self._clock()
        window = self._window(self._pages if kind == "document" else self._images, host, now)
        window.extend([now] * n)
        if kind == "document":
            self._dirty = True

    def _blocked(self, host: str, kind: Kind, reason: str, *, url: str, status_code: int | None) -> BlockedError:
        """BlockedError for a fresh block; page blocks also start/extend the host's cooldown."""
        if kind != "document":  # an image CDN refusing one picture is not a ban of the site
            return BlockedError(f"{reason}: {url}", url=url, status_code=status_code, host=host)
        now = self._clock()
        state = self._state(host, now)
        state.strikes += 1
        hours = self.cooldown_hours[min(state.strikes, len(self.cooldown_hours)) - 1] if self.cooldown_hours else 0.0
        state.cooldown_until = max(state.cooldown_until, now + hours * HOUR)
        state.last_block_at = now
        state.last_reason = reason if len(reason) <= 80 else reason[:77] + "..."
        self._dirty = True
        self._save_state(force=True)
        if hours > 0:
            pause = (f" — {host} на паузе {format_duration(hours * HOUR)} (до {_local_time(state.cooldown_until, now)}),"
                     f" блокировка №{state.strikes} подряд")
        else:
            pause = ""
        log.warning("Blocked by %s (%s), strike %d, cooldown %.1f h", host, reason, state.strikes, hours)
        return BlockedError(
            f"{reason}{pause}", url=url, status_code=status_code, host=host,
            cooldown_until=_utc(state.cooldown_until) if hours > 0 else None,
        )

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            hosts = data.get("hosts", {}) if isinstance(data, dict) else {}
            now = self._clock()
            for host, raw in hosts.items():
                if not isinstance(raw, dict):
                    continue
                self._hosts[host] = _HostState(
                    strikes=int(raw.get("strikes", 0) or 0),
                    cooldown_until=float(raw.get("cooldown_until", 0) or 0),
                    last_block_at=float(raw.get("last_block_at", 0) or 0),
                    last_reason=str(raw.get("last_reason", "") or ""),
                )
                recent = sorted(float(t) for t in raw.get("requests", []) or [] if now - float(t) < HOUR)
                if recent:
                    self._pages[host] = deque(recent)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("Не удалось прочитать %s (%s) — начинаю с чистого состояния", self.state_path, exc)
            self._hosts.clear()
            self._pages.clear()

    def _save_state(self, force: bool = False) -> None:
        if self.state_path is None or not self._dirty:
            return
        now = self._clock()
        if not force and now - self._last_save < STATE_SAVE_INTERVAL:
            return
        hosts: dict[str, Any] = {}
        for host in sorted(set(self._hosts) | set(self._pages)):
            state = self._hosts.get(host, _HostState())
            requests = [round(t, 3) for t in self._window(self._pages, host, now)]
            if not (requests or state.strikes or state.cooldown_until > now or state.last_block_at):
                continue
            hosts[host] = {
                "strikes": state.strikes,
                "cooldown_until": state.cooldown_until,
                "last_block_at": state.last_block_at,
                "last_reason": state.last_reason,
                "requests": requests,
            }
        payload = {"version": STATE_VERSION, "saved_at": now, "hosts": hosts}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:  # e.g. the file is locked by an antivirus on Windows: retry next time
            log.warning("Не удалось сохранить %s: %s", self.state_path, exc)
            return
        self._last_save = now
        self._dirty = False

    # -- internals ----------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:  # separate method so tests can patch it
        await asyncio.sleep(seconds)

    def _headers(self, url: str, referer: str | None, kind: Kind) -> dict[str, str]:
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

    async def _request(self, url: str, *, referer: str | None, kind: Kind) -> httpx.Response:
        host = (urlsplit(url).hostname or "").lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        headers = self._headers(url, referer, kind)
        async with lock:  # serialize all requests to one host
            self._check_access(host, url, kind)  # inside the lock: another request may just have been blocked
            await self._wait_turn(host)
            attempt = 0
            try:
                while True:
                    self._count(host, kind)  # every attempt hits the site, retries included
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
                    self._count(host, kind, len(response.history))  # followed redirects are requests too
                    status = response.status_code
                    if status == 403:
                        raise self._blocked(host, kind, "HTTP 403", url=url, status_code=status)
                    if status == 429 or status >= 500:
                        if attempt >= self.max_retries:
                            if status == 429:
                                raise self._blocked(host, kind, "HTTP 429 (слишком много запросов)",
                                                    url=url, status_code=status)
                            response.raise_for_status()
                        log.warning("GET %s -> HTTP %d, retry %d/%d", url, status, attempt + 1, self.max_retries)
                        await self._backoff(attempt, _retry_after(response))
                        attempt += 1
                        continue
                    response.raise_for_status()
                    return response
            finally:
                if kind == "document":
                    self._save_state()
