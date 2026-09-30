"""Free cloud AI (docs/design/CLOUD_AI.md): OpenRouter, the NVIDIA API catalog and a self-hosted
OmniRoute gateway as OpenAI-compatible endpoints for the scout and the photo check.

What lives here (everything the client, the monitor and the API share):

* provider presets (address, where to get a key, the .env variable, default limits, model picks);
* the API key: always from .env (OPENROUTER_API_KEY / NVIDIA_API_KEY / OMNIROUTE_API_KEY /
  CLOUD_API_KEY), never from config.yaml, never logged;
* `QuotaLimiter`: one per endpoint (base URL + a hash of the key) — a token bucket for the
  requests per minute, a daily counter persisted in the DB (kv_state, reset at UTC midnight),
  429 Retry-After / X-RateLimit-Reset, exponential backoff with jitter and a short circuit
  breaker after repeated 429 / 5xx. It never makes the monitor wait for quota: a call waits at
  most a few seconds (the next RPM token), otherwise it fails fast with `CloudLimited` and the
  caller takes its fallback (a local model) or the usual "AI down" path;
* the daily budget planner: photos of the top candidates first, the scout reads in big batches
  (16-20 ads per call) and switches to «только непонятные» when its share runs low;
* /key and /models parsing (free and vision models), the reasoning ("thinking") switch per
  runtime, `<think>` stripping, and the privacy filter (no seller data, no contact details).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import urlsplit

from .client import LLMError

log = logging.getLogger(__name__)

APP_TITLE = "EbeyParser"
# OpenRouter attribution headers: the app's name only (no user data, no real site)
OPENROUTER_HEADERS = {"HTTP-Referer": "http://localhost/ebeyparser", "X-Title": APP_TITLE}

CLOUD_BATCH = (16, 20)  # (first, largest) ads per scout call for big cloud models
CLOUD_TRIAGE_MAX_TOKENS = 2600  # 20 ads × 110 tokens + overhead (triage.TOKENS_PER_ITEM)
CLOUD_VISION_MAX_TOKENS = 1500  # a reasoning model that ignores "off" still fits its JSON
PHOTO_SHARE = 0.2  # of a daily limit reserved for photo checks of top candidates
MIN_PHOTOS = 5
MAX_WAIT_SECONDS = 6.0  # the longest a call waits for quota (one RPM token at 20/min is 3 s)
BACKOFF_BASE = 2.0
BACKOFF_MAX = 120.0
CIRCUIT_AFTER = 3  # consecutive 429 / 5xx / network failures
CIRCUIT_COOLDOWN = 60.0
CIRCUIT_MAX = 900.0
FALLBACK_RECENT = 1800.0  # «запасной компьютер работает» shows this long after a fallback call
PROBE_MAX_AGE = 24 * 3600.0
STATE_PREFIX = "cloud:quota:"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudPreset:
    key: str
    name: str
    base_url: str
    key_env: str  # .env variable holding the key
    key_url: str  # «Получить ключ»
    key_hint: str  # what a key looks like
    key_required: bool
    rpm: int  # requests per minute (0 = no known limit)
    daily_limit: int  # requests per UTC day (0 = no daily cap)
    daily_limit_paid: int = 0  # OpenRouter: after $10 of credits were ever bought
    text_picks: tuple[str, ...] = ()  # preferred ids for reading ads, best first
    vision_picks: tuple[str, ...] = ()  # preferred ids for photos, best first
    terms_ru: str = ""
    how_ru: str = ""  # one line: what to click to get a key

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "base_url": self.base_url, "key_env": self.key_env,
                "key_url": self.key_url, "key_hint": self.key_hint, "key_required": self.key_required,
                "rpm": self.rpm, "daily_limit": self.daily_limit, "daily_limit_paid": self.daily_limit_paid,
                "text_picks": list(self.text_picks), "vision_picks": list(self.vision_picks),
                "terms_ru": self.terms_ru, "how_ru": self.how_ru}


PRESETS: dict[str, CloudPreset] = {p.key: p for p in (
    CloudPreset(
        key="openrouter", name="OpenRouter", base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY", key_url="https://openrouter.ai/keys", key_hint="sk-or-v1-…",
        key_required=True, rpm=20, daily_limit=50, daily_limit_paid=1000,
        text_picks=("nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-3.5-lightning:free",
                    "nvidia/nemotron-3-ultra-550b-a55b:free"),
        vision_picks=("nvidia/nemotron-nano-12b-v2-vl:free", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
        terms_ru=("Бесплатно: 20 запросов в минуту и 50 в день; после разовой покупки $10 кредитов — 1000 в день. "
                  "Некоторые бесплатные модели могут сохранять запросы — это настраивается в аккаунте OpenRouter."),
        how_ru="Войди на openrouter.ai → Keys → Create Key и вставь ключ сюда",
    ),
    CloudPreset(
        key="nvidia", name="NVIDIA", base_url="https://integrate.api.nvidia.com/v1",
        key_env="NVIDIA_API_KEY", key_url="https://build.nvidia.com/settings/api-keys", key_hint="nvapi-…",
        key_required=True, rpm=40, daily_limit=0,
        text_picks=("nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3.5-lightning",
                    "nvidia/llama-3.3-nemotron-super-49b-v1.5"),
        vision_picks=("nvidia/nemotron-nano-12b-v2-vl", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"),
        terms_ru=("Бесплатный аккаунт NVIDIA Developer: около 40 запросов в минуту на все модели, дневного лимита нет. "
                  "Условия — «для пробы и прототипов», без гарантий."),
        how_ru="Войди на build.nvidia.com (бесплатный аккаунт NVIDIA Developer) → API Keys → Generate Key",
    ),
    CloudPreset(
        key="omniroute", name="OmniRoute", base_url="http://localhost:20128/v1",
        key_env="OMNIROUTE_API_KEY", key_url="https://github.com/diegosouzapw/OmniRoute", key_hint="необязательно",
        key_required=False, rpm=0, daily_limit=0,
        terms_ru=("Шлюз на твоём компьютере: сам переключается между бесплатными облаками, когда у одного "
                  "кончается лимит. Лимиты — у тех облаков, что ты в нём подключил."),
        how_ru="Запусти OmniRoute (он работает на этом компьютере) и подключи в нём бесплатные облака",
    ),
    CloudPreset(
        key="custom", name="Другой сервис", base_url="", key_env="CLOUD_API_KEY", key_url="", key_hint="",
        key_required=False, rpm=0, daily_limit=0,
        terms_ru="Любой OpenAI-совместимый облачный сервис: адрес и ключ — из его документации.",
    ),
)}

CLOUD_KINDS = tuple(PRESETS)

# name tokens for picking models from a live list when the preferred ids changed (best first)
TEXT_WISHES: tuple[tuple[str, ...], ...] = (("nemotron", "super"), ("nemotron", "lightning"), ("nemotron", "ultra"),
                                            ("llama", "70b"), ("qwen3", "235b"), ("gpt-oss", "120b"),
                                            ("deepseek",), ("nemotron",))
VISION_WISHES: tuple[tuple[str, ...], ...] = (("nemotron", "vl"), ("nemotron", "omni"), ("qwen", "vl"),
                                              ("gemma", "3"), ("vision",), ("vl",))
NON_CHAT = ("embed", "rerank", "retriever", "reward", "guard", "safety", "clip", "tts", "asr", "whisper",
            "parakeet", "canary", "sdxl", "flux", "stable-diffusion", "yolox", "paddleocr", "ocdrnet", "nemoretriever",
            "riva", "audio2face", "cosmos", "bge", "e5-", "moderation", "image-gen")


def preset(kind: str) -> CloudPreset | None:
    return PRESETS.get(kind or "")


def detect_cloud(base_url: str) -> str:
    """Cloud kind from an address someone pasted: openrouter.ai, integrate.api.nvidia.com, OmniRoute's
    default port 20128. "" = a local server (LM Studio / Ollama / llama.cpp) or unknown."""
    try:
        parts = urlsplit((base_url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    if host.endswith("api.nvidia.com"):
        return "nvidia"
    if port == 20128:
        return "omniroute"
    return ""


def cloud_kind(settings: Any) -> str:
    """The endpoint's cloud kind: the explicit `cloud` setting, else from the address."""
    kind = str(getattr(settings, "cloud", "") or "").strip()
    if kind in PRESETS:
        return kind
    if getattr(settings, "provider", "openai") == "anthropic":
        return ""
    return detect_cloud(str(getattr(settings, "base_url", "") or ""))


def key_env(kind: str) -> str:
    p = preset(kind)
    return p.key_env if p else ""


def stored_key(kind: str) -> str:
    """The key saved for a provider (.env, loaded into the process environment)."""
    env = key_env(kind)
    return os.environ.get(env, "").strip() if env else ""


def resolve_api_key(settings: Any) -> str:
    """The key to send: an explicit api_key (a test, an ${ENV} reference) or the provider's .env key."""
    explicit = str(getattr(settings, "api_key", "") or "").strip()
    if explicit:
        return explicit
    kind = cloud_kind(settings)
    return stored_key(kind) if kind else ""


def endpoint_id(base_url: str, api_key: str) -> str:
    """Stable id of one account on one endpoint: the key itself never leaves this hash."""
    raw = f"{(base_url or '').strip().rstrip('/').lower()}|{api_key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def mask_key(key: str) -> str:
    key = (key or "").strip()
    return "" if not key else ("…" + key[-4:] if len(key) > 8 else "***")


def redact_key(text: str, key: str) -> str:
    return text.replace(key, "***") if key and len(key) >= 6 else text


# ---------------------------------------------------------------------------
# Errors and "local only" (eBay ads)
# ---------------------------------------------------------------------------


class CloudLimited(LLMError):
    """The cloud endpoint may not be called now (free quota used up, 429 cooldown, circuit open,
    or the ad may not go to the cloud). Fail-fast: the caller takes its fallback. The message is
    plain Russian."""

    def __init__(self, message_ru: str, *, reason: str, retry_at: float | None = None, provider: str = ""):
        super().__init__(message_ru)
        self.message_ru = message_ru
        self.reason = reason  # rpm | daily | reserve | pace | cooldown | 429 | ebay
        self.retry_at = retry_at
        self.provider = provider


_LOCAL_ONLY: contextvars.ContextVar[str] = contextvars.ContextVar("ebeyparser_cloud_local_only", default="")
EBAY_LOCAL_RU = ("Объявления с eBay в облако не отправляю — так требуют правила eBay. "
                 "Их смотрит компьютер про запас или обычная проверка")


@contextlib.contextmanager
def local_only(reason: str | bool = "ebay") -> Iterator[None]:
    """Calls inside may not reach a cloud endpoint (eBay content: the eBay API license): a cloud
    client with a local fallback uses the fallback, one without raises CloudLimited("ebay")."""
    token = _LOCAL_ONLY.set(("ebay" if reason is True else str(reason or "")))
    try:
        yield
    finally:
        _LOCAL_ONLY.reset(token)


def local_only_reason() -> str:
    return _LOCAL_ONLY.get()


# ---------------------------------------------------------------------------
# Time helpers (quota days are UTC days: OpenRouter resets at 00:00 UTC)
# ---------------------------------------------------------------------------


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def next_utc_midnight(ts: float) -> float:
    day = datetime.fromtimestamp(ts, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(days=1)).timestamp()


def day_fraction(ts: float) -> float:
    day = datetime.fromtimestamp(ts, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, min(1.0, (ts - day.timestamp()) / 86400.0))


def parse_retry(headers: Any, now: float) -> float | None:
    """Seconds to wait from Retry-After (seconds or an HTTP date) or X-RateLimit-Reset (epoch ms,
    epoch s or seconds from now). None = the server said nothing."""
    get = headers.get if hasattr(headers, "get") else (lambda _k, _d=None: None)
    raw = get("retry-after") or get("Retry-After")
    if raw:
        text = str(raw).strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            try:
                when = parsedate_to_datetime(text)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, when.timestamp() - now)
            except (TypeError, ValueError, IndexError):
                pass
    reset = get("x-ratelimit-reset") or get("X-RateLimit-Reset")
    if reset:
        try:
            value = float(str(reset).strip())
        except ValueError:
            return None
        if value > 1e12:  # epoch milliseconds (OpenRouter)
            return max(0.0, value / 1000.0 - now)
        if value > 1e9:  # epoch seconds
            return max(0.0, value - now)
        return max(0.0, value)
    return None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Budget planner
# ---------------------------------------------------------------------------


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _int_ru(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def plan_budget(rpm: int, daily_limit: int, *, batch: int = CLOUD_BATCH[1], photo_share: float = PHOTO_SHARE,
                min_photos: int = MIN_PHOTOS) -> dict[str, Any]:
    """Split a day's requests: photo checks of the top candidates first (a reserve), the rest for
    the scout at `batch` ads per call. No daily cap (NVIDIA): only the RPM limits."""
    rpm, daily = max(0, int(rpm or 0)), max(0, int(daily_limit or 0))
    if daily:
        photos = min(daily // 2, max(min_photos, round(daily * photo_share)))
        triage_calls = daily - photos
        ads = triage_calls * batch
        text = (f"хватит на ~{_int_ru(ads)} {_plural(ads, 'объявление', 'объявления', 'объявлений')} и "
                f"{_int_ru(photos)} {_plural(photos, 'проверку', 'проверки', 'проверок')} фото в день")
    else:
        photos = triage_calls = ads = None
        text = "без дневного лимита — хватит на все объявления"
    return {"rpm": rpm, "daily_limit": daily, "photos_per_day": photos, "triage_calls_per_day": triage_calls,
            "ads_per_day": ads, "batch": batch, "text_ru": text}


def limits_text(kind: str, rpm: int, daily_limit: int, *, paid_known: bool | None = None) -> str:
    """«20 запросов в минуту · 50 в день (купи $10 кредитов один раз → 1000 в день)»."""
    parts = []
    if rpm:
        parts.append(f"{rpm} {_plural(rpm, 'запрос', 'запроса', 'запросов')} в минуту")
    if daily_limit:
        day = f"{_int_ru(daily_limit)} в день"
        p = preset(kind)
        if p and p.daily_limit_paid and daily_limit < p.daily_limit_paid:
            day += f" (купи $10 кредитов один раз → {_int_ru(p.daily_limit_paid)} в день)"
        parts.append(day)
    else:
        parts.append("без дневного лимита")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------


class QuotaLimiter:
    """Quota bookkeeping of one cloud endpoint (one account). All waits are short; anything
    longer fails fast with CloudLimited so the monitor never waits for quota."""

    def __init__(self, endpoint: str, *, kind: str = "custom", rpm: int = 0, daily_limit: int = 0,
                 store: Any = None, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = asyncio.sleep, rng: random.Random | None = None,
                 max_wait: float = MAX_WAIT_SECONDS, batch: int = CLOUD_BATCH[1], label: str = ""):
        self.endpoint = endpoint
        self.kind = kind if kind in PRESETS else "custom"
        self.label = label
        self.rpm_setting = max(0, int(rpm or 0))
        self.daily_setting = max(0, int(daily_limit or 0))
        self.store = store
        self.clock, self.wall, self._sleep = clock, wall, sleep
        self.rng = rng or random.Random()
        self.max_wait = max_wait
        self.batch = batch
        self.probe: dict[str, Any] = {}  # /key: is_free_tier, daily_limit, rpm, checked_at
        self.learned_daily = 0  # a per-day 429 came at this count (e.g. < $10 credits: 50)
        self.day = ""
        self.used = 0
        self.by_purpose: dict[str, int] = {}
        self.count_429 = 0
        self.errors = 0
        self.fallback_calls = 0
        self.strikes = 0
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.daily_exhausted_until = 0.0
        self.fallback_at = 0.0
        self.last_error = ""
        self.last_ok_at = 0.0
        self.last_headers: dict[str, str] = {}
        self.latency_ms: int | None = None
        self._tokens = float(self.rpm or 0)
        self._refill_at = clock()
        self.load()
        self._roll()

    # -- limits ---------------------------------------------------------------
    @property
    def preset(self) -> CloudPreset:
        return PRESETS[self.kind]

    @property
    def rpm(self) -> int:
        return self.rpm_setting or int(self.probe.get("rpm") or 0) or self.preset.rpm

    @property
    def daily_limit(self) -> int:
        if self.daily_setting:
            return self.daily_setting
        probed = int(self.probe.get("daily_limit") or 0)
        limit = probed or self.preset.daily_limit
        if self.learned_daily and limit:
            limit = min(limit, self.learned_daily)
        return limit

    @property
    def daily_reset(self) -> bool:
        return self.kind != "omniroute"

    def configure(self, *, rpm: int | None = None, daily_limit: int | None = None) -> None:
        if rpm is not None:
            self.rpm_setting = max(0, int(rpm))
        if daily_limit is not None:
            self.daily_setting = max(0, int(daily_limit))

    def budget(self) -> dict[str, Any]:
        return plan_budget(self.rpm, self.daily_limit, batch=self.batch)

    # -- day roll-over ----------------------------------------------------------
    def _roll(self) -> None:
        day = utc_day(self.wall())
        if day != self.day:
            if self.day:
                log.info("Облако %s: новый день, счётчик запросов обнулён", self.preset.name)
            self.day = day
            self.used = self.count_429 = self.errors = self.fallback_calls = 0
            self.by_purpose = {}
            self.daily_exhausted_until = 0.0

    def next_reset(self) -> float | None:
        return next_utc_midnight(self.wall()) if self.daily_limit and self.daily_reset else None

    # -- the scout's share -------------------------------------------------------
    def triage_budget(self) -> int | None:
        b = self.budget()
        return b["triage_calls_per_day"]

    def triage_cap(self) -> int | None:
        """How many requests the day may have used before a scout call is refused: the day's
        limit minus what photos may still need (the reserve shrinks towards midnight, so an unused
        photo reserve goes to the scout late in the day)."""
        limit = self.daily_limit
        if not limit:
            return None
        photos = self.budget()["photos_per_day"] or 0
        left = max(0, photos - self.by_purpose.get("vision", 0))
        reserve = math.ceil(left * (1.0 - day_fraction(self.wall())))
        return max(0, limit - reserve)

    def triage_allowance(self) -> float | None:
        """Scout calls allowed so far today when spread evenly over the day (+ a burst)."""
        budget = self.triage_budget()
        if not budget:
            return None
        burst = max(3.0, 0.15 * budget)
        return budget * day_fraction(self.wall()) + burst

    def quota_low(self) -> bool:
        """The scout should read only the ads where the script is blind («только непонятные»):
        its daily share runs low, or it is ahead of the even pace. Back to normal after the reset."""
        self._roll()
        budget = self.triage_budget()
        if not budget:
            return False
        used = self.by_purpose.get("triage", 0)
        soft = budget * day_fraction(self.wall()) + max(2.0, 0.05 * budget)
        cap = self.triage_cap()
        return used >= 0.75 * budget or used >= soft or (cap is not None and self.used >= cap)

    # -- checks -------------------------------------------------------------------
    def _limited(self, message: str, reason: str, retry_at: float | None) -> CloudLimited:
        return CloudLimited(message, reason=reason, retry_at=retry_at, provider=self.kind)

    def blocked(self, purpose: str = "vision") -> CloudLimited | None:
        """Why a call for `purpose` can't go now (without waiting), or None."""
        self._roll()
        now = self.wall()
        name = self.preset.name
        if self.daily_exhausted_until > now:
            return self._limited(f"Бесплатный лимит {name} на сегодня исчерпан — продолжу после обнуления",
                                 "daily", self.daily_exhausted_until)
        limit = self.daily_limit
        if limit and self.used >= limit:
            return self._limited(f"Бесплатный лимит {name} на сегодня исчерпан ({limit} запросов) — "
                                 "продолжу после обнуления", "daily", self.next_reset())
        if purpose == "triage" and limit:
            cap = self.triage_cap()
            if cap is not None and self.used >= cap:
                return self._limited(f"Остаток лимита {name} берегу для проверки фото лучших находок", "reserve",
                                     self.next_reset())
            allowance = self.triage_allowance()
            if allowance is not None and self.by_purpose.get("triage", 0) >= allowance:
                return self._limited(f"Разведчик распределяет бесплатный лимит {name} на весь день — "
                                     "остальные объявления смотрю обычным способом", "pace", None)
        wait = self.cooldown_until - now
        if wait > self.max_wait:
            text = (f"{name} попросил подождать — пауза до {_hhmm(self.cooldown_until)}"
                    if self.cooldown_reason == "429" else f"{name} сейчас не отвечает — повторю после {_hhmm(self.cooldown_until)}")
            return self._limited(text, "cooldown", self.cooldown_until)
        return None

    def _refill(self) -> None:
        rpm = self.rpm
        now = self.clock()
        if rpm <= 0:
            self._tokens, self._refill_at = 0.0, now
            return
        self._tokens = min(float(rpm), self._tokens + (now - self._refill_at) * rpm / 60.0)
        self._refill_at = now

    def rpm_wait(self) -> float:
        """Seconds until the next request fits the per-minute limit (0 = now)."""
        rpm = self.rpm
        if rpm <= 0:
            return 0.0
        self._refill()
        return 0.0 if self._tokens >= 1.0 else (1.0 - self._tokens) * 60.0 / rpm

    async def acquire(self, purpose: str = "vision", *, force: bool = False) -> None:
        """Take one request of today's quota. Waits at most `max_wait` seconds (the next RPM token
        or a short 429 pause); otherwise raises CloudLimited at once. `force`: a user's «Проверить»
        (counted, never refused by our own daily plan)."""
        if not force:
            why = self.blocked(purpose)
            if why is not None:
                raise why
        pause = max(0.0, self.cooldown_until - self.wall()) if not force else 0.0
        wait = max(pause, self.rpm_wait())
        if wait > self.max_wait and not force:
            raise self._limited(f"Лимит {self.rpm} запросов в минуту у {self.preset.name} — продолжу через минуту",
                                "rpm", self.wall() + wait)
        if wait > 0:
            await self._sleep(min(wait, self.max_wait))
            self._refill()
        if self.rpm > 0:
            self._tokens = max(0.0, self._tokens - 1.0)
        self._roll()
        self.used += 1
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1
        self.save()

    # -- outcomes ------------------------------------------------------------------
    def note_headers(self, headers: Any) -> None:
        keep = {}
        for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"):
            value = headers.get(name) if hasattr(headers, "get") else None
            if value is not None:
                keep[name] = str(value)[:40]
        if keep:
            self.last_headers = keep
            remaining, reset = _num(keep.get("x-ratelimit-remaining")), keep.get("x-ratelimit-reset")
            if remaining is not None and remaining <= 0 and reset:
                wait = parse_retry({"x-ratelimit-reset": reset}, self.wall()) or 0.0
                if 0 < wait:
                    self._cooldown(self.wall() + wait, "429")

    def note_success(self, latency_ms: int | None = None) -> None:
        self.strikes = 0
        self.last_ok_at = self.wall()
        if latency_ms is not None:
            self.latency_ms = latency_ms
        self.save()

    def _cooldown(self, until: float, reason: str) -> None:
        if until > self.cooldown_until:
            self.cooldown_until, self.cooldown_reason = until, reason

    def note_429(self, headers: Any = None, body: str = "") -> float:
        """A 429: exponential backoff with jitter (at least what the server asked for); a per-day
        429 blocks until the reset. Returns the seconds to wait before the next try."""
        self._roll()
        now = self.wall()
        self.count_429 += 1
        self.strikes += 1
        retry = parse_retry(headers or {}, now)
        low = (body or "").lower()
        per_day = any(w in low for w in ("per-day", "per day", "free-models-per-day", "daily", "requests per day"))
        if per_day or (retry is not None and retry > 3600):
            until = now + retry if retry is not None and retry > 60 else (self.next_reset() or next_utc_midnight(now))
            self.daily_exhausted_until = max(self.daily_exhausted_until, until)
            if not self.daily_setting and self.used and (not self.daily_limit or self.used < self.daily_limit):
                self.learned_daily = self.used  # e.g. 50/day on an account with < $10 of credits
            self.last_error = "дневной лимит исчерпан"
            self.save()
            return max(0.0, until - now)
        backoff = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (self.strikes - 1))) * self.rng.uniform(0.75, 1.25)
        wait = max(retry or 0.0, backoff)
        if self.strikes >= CIRCUIT_AFTER:
            wait = max(wait, min(CIRCUIT_MAX, CIRCUIT_COOLDOWN * (2 ** (self.strikes - CIRCUIT_AFTER))))
        self._cooldown(now + wait, "429")
        self.last_error = "HTTP 429"
        self.save()
        return wait

    def note_failure(self, error: str = "") -> None:
        """5xx / network / timeout: after CIRCUIT_AFTER in a row a short circuit-breaker pause."""
        self._roll()
        self.errors += 1
        self.strikes += 1
        self.last_error = (error or "")[:200]
        if self.strikes >= CIRCUIT_AFTER:
            wait = min(CIRCUIT_MAX, CIRCUIT_COOLDOWN * (2 ** (self.strikes - CIRCUIT_AFTER)))
            self._cooldown(self.wall() + wait * self.rng.uniform(0.9, 1.1), "error")
        self.save()

    def note_fallback(self) -> None:
        self._roll()
        self.fallback_at = self.wall()
        self.fallback_calls += 1
        self.save()

    def set_probe(self, info: dict[str, Any]) -> None:
        self.probe = {k: info.get(k) for k in ("is_free_tier", "daily_limit", "rpm", "credits_remaining")
                      if info.get(k) is not None}
        self.probe["checked_at"] = self.wall()
        if info.get("daily_limit") and int(info["daily_limit"]) >= PRESETS["openrouter"].daily_limit_paid:
            self.learned_daily = 0
        self.save()

    def probe_stale(self) -> bool:
        return self.kind == "openrouter" and self.wall() - float(self.probe.get("checked_at") or 0) > PROBE_MAX_AGE

    # -- persistence -----------------------------------------------------------------
    @property
    def state_key(self) -> str:
        return STATE_PREFIX + self.endpoint

    def load(self) -> None:
        if self.store is None:
            return
        try:
            row = self.store.get_state(self.state_key)
            data = json.loads(row[0]) if row and row[0] else {}
        except Exception:  # noqa: BLE001 - quota bookkeeping never breaks a call
            return
        if not isinstance(data, dict):
            return
        self.day = str(data.get("day") or "")
        for name in ("used", "count_429", "errors", "fallback_calls", "strikes", "learned_daily"):
            try:
                setattr(self, name, int(data.get(name) or 0))
            except (TypeError, ValueError):
                pass
        for name in ("cooldown_until", "daily_exhausted_until", "fallback_at", "last_ok_at"):
            value = _num(data.get(name))
            if value is not None:
                setattr(self, name, value)
        self.by_purpose = {str(k): int(v) for k, v in (data.get("by_purpose") or {}).items()
                           if isinstance(v, (int, float))}
        self.cooldown_reason = str(data.get("cooldown_reason") or "")
        self.last_error = str(data.get("last_error") or "")
        self.probe = dict(data.get("probe") or {})
        self.label = self.label or str(data.get("label") or "")

    def save(self) -> None:
        if self.store is None:
            return
        data = {"day": self.day, "used": self.used, "by_purpose": self.by_purpose, "count_429": self.count_429,
                "errors": self.errors, "fallback_calls": self.fallback_calls, "strikes": self.strikes,
                "cooldown_until": self.cooldown_until, "cooldown_reason": self.cooldown_reason,
                "daily_exhausted_until": self.daily_exhausted_until, "fallback_at": self.fallback_at,
                "last_ok_at": self.last_ok_at, "last_error": self.last_error, "probe": self.probe,
                "learned_daily": self.learned_daily, "kind": self.kind, "label": self.label}
        try:
            self.store.set_state(self.state_key, json.dumps(data))
        except Exception as exc:  # noqa: BLE001
            log.debug("cloud quota not saved: %s", exc)

    # -- the UI ------------------------------------------------------------------------
    def snapshot(self, *, fallback_configured: bool = False) -> dict[str, Any]:
        self._roll()
        now = self.wall()
        limit = self.daily_limit
        why = self.blocked("vision")
        triage_why = self.blocked("triage") if why is None else why
        reset = self.next_reset()
        remaining = max(0, limit - self.used) if limit else None
        fallback_active = fallback_configured and (why is not None or now - self.fallback_at < FALLBACK_RECENT)
        budget = self.budget()
        if why is not None:
            state = "limited"
        elif triage_why is not None or self.quota_low():
            state = "low"
        elif self.strikes:
            state = "flaky"
        else:
            state = "ok"
        used_ru = (f"Сегодня {self.used} из {_int_ru(limit)} запросов" if limit
                   else f"Сегодня {self.used} {_plural(self.used, 'запрос', 'запроса', 'запросов')}")
        return {
            "endpoint": self.endpoint, "provider": self.kind, "provider_name": self.preset.name,
            "label": self.label, "state": state,
            "used_today": self.used, "daily_limit": limit or None, "remaining_today": remaining,
            "percent": round(100 * self.used / limit) if limit else None,
            "by_purpose": dict(self.by_purpose), "rpm": self.rpm or None,
            "count_429_today": self.count_429, "errors_today": self.errors,
            "fallback_configured": fallback_configured, "fallback_active": bool(fallback_active),
            "fallback_calls_today": self.fallback_calls,
            "limited": why is not None, "limited_reason": why.reason if why else "",
            "limited_ru": why.message_ru if why else "",
            "scout_low": bool(triage_why is not None or self.quota_low()),
            "cooldown_until": _iso(self.cooldown_until) if self.cooldown_until > now else None,
            "next_reset": _iso(reset) if reset else None,
            "last_ok_at": _iso(self.last_ok_at) if self.last_ok_at else None,
            "latency_ms": self.latency_ms,
            "is_free_tier": self.probe.get("is_free_tier"),
            "limits_ru": limits_text(self.kind, self.rpm, limit),
            "budget": budget, "estimate_ru": budget["text_ru"],
            "used_ru": used_ru,
            "rate_headers": dict(self.last_headers),
        }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _hhmm(ts: float) -> str:
    try:
        from ..timefmt import clock_label  # type: ignore[attr-defined]

        return clock_label(datetime.fromtimestamp(ts, timezone.utc))
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%H:%M")


class QuotaRegistry:
    """Process-wide limiters, one per endpoint + key: the scout and the photo check on the same
    account share one quota. `attach(db)` makes the counters survive restarts."""

    def __init__(self, store: Any = None):
        self.store = store
        self._items: dict[str, QuotaLimiter] = {}

    def attach(self, store: Any) -> None:
        if store is not self.store:
            self.store = store
            self._items.clear()

    def reset(self) -> None:
        self._items.clear()

    def get(self, base_url: str, api_key: str, *, kind: str, rpm: int = 0, daily_limit: int = 0,
            label: str = "") -> QuotaLimiter:
        eid = endpoint_id(base_url, api_key)
        limiter = self._items.get(eid)
        if limiter is None:
            limiter = QuotaLimiter(eid, kind=kind, rpm=rpm, daily_limit=daily_limit, store=self.store, label=label)
            self._items[eid] = limiter
        else:
            limiter.configure(rpm=rpm, daily_limit=daily_limit)
            if label:
                limiter.label = label
        return limiter

    def for_settings(self, settings: Any) -> QuotaLimiter | None:
        kind = cloud_kind(settings)
        if not kind:
            return None
        return self.get(str(getattr(settings, "base_url", "") or ""), resolve_api_key(settings), kind=kind,
                        rpm=int(getattr(settings, "rpm", 0) or 0),
                        daily_limit=int(getattr(settings, "daily_limit", 0) or 0),
                        label=str(getattr(settings, "model", "") or ""))

    def all(self) -> list[QuotaLimiter]:
        return list(self._items.values())


_REGISTRY = QuotaRegistry()


def registry() -> QuotaRegistry:
    return _REGISTRY


# ---------------------------------------------------------------------------
# /key and /models
# ---------------------------------------------------------------------------


def _interval_seconds(text: Any) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text or ""))
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_key_info(data: Any) -> dict[str, Any]:
    """OpenRouter GET /api/v1/key → {is_free_tier, daily_limit, rpm, credits_*}; tolerant of missing
    or renamed fields (None = unknown)."""
    d = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    d = d if isinstance(d, dict) else {}
    free = d.get("is_free_tier")
    free = free if isinstance(free, bool) else None
    rl = d.get("rate_limit") if isinstance(d.get("rate_limit"), dict) else {}
    rate_rpm = None
    requests, interval = _num(rl.get("requests")), _interval_seconds(rl.get("interval"))
    if requests and interval:
        rate_rpm = int(requests * 60 / interval)
    p = PRESETS["openrouter"]
    daily = (p.daily_limit if free else p.daily_limit_paid) if free is not None else None
    return {
        "is_free_tier": free,
        "daily_limit": daily,
        "rpm": p.rpm,  # free (":free") models: 20 per minute whatever the account
        "rate_limit_rpm": rate_rpm,
        "label": str(d.get("label") or "")[:60],
        "credits_limit": _num(d.get("limit")),
        "credits_used": _num(d.get("usage")),
        "credits_remaining": _num(d.get("limit_remaining")),
    }


def _params_b(model_id: str) -> float:
    sizes = [float(x) for x in re.findall(r"(?<![0-9.a-z])(\d+(?:\.\d+)?)b(?![a-z0-9])", model_id.lower())]
    return max(sizes) if sizes else 0.0


def parse_models(data: Any, kind: str = "") -> list[dict[str, Any]]:
    """OpenAI-style /models (OpenRouter adds pricing and architecture.input_modalities) → chat
    models with free / vision flags, sorted: free first, then by size."""
    from .client import looks_like_vision_model

    items = data.get("data") if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("name") or "").strip()
        low = mid.lower()
        if not mid or mid in seen or any(w in low for w in NON_CHAT):
            continue
        seen.add(mid)
        arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
        inputs = [str(x).lower() for x in (arch.get("input_modalities") or [])]
        outputs = [str(x).lower() for x in (arch.get("output_modalities") or [])]
        if outputs and "text" not in outputs:
            continue
        pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else None
        if low.endswith(":free"):
            free = True
        elif pricing is not None:
            prices = [_num(pricing.get(k)) for k in ("prompt", "completion")]
            free = all(p is not None and p == 0 for p in prices)
        else:
            free = kind != "openrouter"  # the NVIDIA catalog / a gateway: no prices = free to try
        vision = ("image" in inputs) if inputs else looks_like_vision_model(mid)
        params = m.get("supported_parameters") if isinstance(m.get("supported_parameters"), list) else []
        ctx = m.get("context_length") or (m.get("top_provider") or {}).get("context_length") \
            if isinstance(m.get("top_provider"), dict) or m.get("context_length") else None
        out.append({"id": mid, "name": str(m.get("name") or mid)[:80], "free": bool(free), "vision": bool(vision),
                    "context": int(ctx) if isinstance(ctx, (int, float)) else None,
                    "reasoning": "reasoning" in params or "reasoning" in low or "think" in low,
                    "params_b": _params_b(mid) or None})
    out.sort(key=lambda x: (not x["free"], -(x["params_b"] or 0), x["id"]))
    return out


def _canon_id(model_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", model_id.lower().removesuffix(":free").rsplit("/", 1)[-1])


def pick_model(models: Sequence[dict[str, Any]], wishes: Sequence[str], token_wishes: Sequence[tuple[str, ...]],
               *, vision: bool) -> str | None:
    """The best available model: a preferred id (exact, then without ":free"/the publisher), then by
    name tokens ("nemotron" + "super"), then the biggest free one. None if nothing fits."""
    pool = [m for m in models if m.get("free", True) and (m.get("vision") if vision else True)]
    if not pool:
        return None
    ids = [m["id"] for m in pool]
    for wish in wishes:
        if wish in ids:
            return wish
    canon = {_canon_id(i): i for i in ids}
    for wish in wishes:
        hit = canon.get(_canon_id(wish))
        if hit:
            return hit
    for tokens in token_wishes:
        for mid in ids:
            low = mid.lower()
            if all(t in low for t in tokens):
                return mid
    return ids[0]


def pick_models(models: Sequence[dict[str, Any]], kind: str) -> dict[str, str | None]:
    """Recommended picks: a big text model for reading ads, a vision model for photos."""
    p = preset(kind)
    text_pool = [m for m in models if not (m.get("vision") and _params_b(m["id"]) and _params_b(m["id"]) < 20)]
    text = pick_model(text_pool or models, p.text_picks if p else (), TEXT_WISHES, vision=False)
    vision = pick_model(models, p.vision_picks if p else (), VISION_WISHES, vision=True)
    return {"text": text, "vision": vision}


def is_cloud_model(model_id: str) -> bool:
    """A model id of a big cloud model ("nvidia/nemotron-3-super-120b-a12b:free", …): the scout reads
    16-20 ads per call with it. Local ids of small models ("qwen/qwen3.5-2b") are not."""
    low = (model_id or "").strip().lower()
    if not low:
        return False
    size = _params_b(low)
    if size and size < 12:
        return False
    return low.endswith(":free") or low.startswith("nvidia/") or "nemotron" in low


# ---------------------------------------------------------------------------
# Thinking ("reasoning") per runtime
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThinkingControls:
    body: dict[str, Any] = field(default_factory=dict)  # extra request fields
    system_prefix: str = ""  # a first system-prompt line ("/no_think", "detailed thinking off")


# Qwen3 hybrid models (not Qwen3.5+) and Nemotron read the "/no_think" soft switch
_SOFT_SWITCH_RE = re.compile(r"qwen3(?![.\d])|nemotron", re.IGNORECASE)
THINK_OFF_PURPOSES = frozenset({"vision", "triage", "planner"})


def _nvidia_line(model: str, on: bool) -> str:
    low = model.lower()
    if "nemotron" not in low:
        return ("/think" if on else "/no_think") if _SOFT_SWITCH_RE.search(low) else ""
    if "llama" in low and "v1.5" not in low and "v1_5" not in low:
        return "detailed thinking on" if on else "detailed thinking off"  # Llama-Nemotron v1
    return "/think" if on else "/no_think"


def thinking_controls(*, provider: str, cloud: str, model: str, thinking: str = "auto",
                      purpose: str = "vision") -> ThinkingControls:
    """What to send so the model thinks (or not):
    llama.cpp / LM Studio / vLLM chat_template_kwargs.enable_thinking (+ "/no_think" for Qwen3 hybrid
    and Nemotron), Ollama think, OpenRouter reasoning.enabled, the NVIDIA catalog chat_template_kwargs
    + the Nemotron system line. "auto" = off for the photo check, the scout and the planner."""
    mode = thinking if thinking in ("on", "off") else ("off" if purpose in THINK_OFF_PURPOSES else "")
    if not mode or provider == "anthropic":
        return ThinkingControls()
    on = mode == "on"
    soft = ("/think" if on else "/no_think") if _SOFT_SWITCH_RE.search(model or "") else ""
    if cloud == "openrouter":
        return ThinkingControls({"reasoning": {"enabled": on}})
    if cloud == "nvidia":
        return ThinkingControls({"chat_template_kwargs": {"enable_thinking": on}}, _nvidia_line(model or "", on))
    if cloud in ("omniroute", "custom"):
        return ThinkingControls({}, soft)  # the upstream is unknown: only the harmless soft switch
    if provider == "ollama":
        return ThinkingControls({"think": on})
    return ThinkingControls({"chat_template_kwargs": {"enable_thinking": on}}, soft)


_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"^\s*<think(?:ing)?>", re.IGNORECASE)


def strip_think(text: str) -> str:
    """The answer without reasoning: <think>…</think> blocks, a reasoning prefix whose opening tag
    was in the prompt ("…</think>{json}"), and a stray opening tag of a cut-off answer."""
    if not text:
        return text or ""
    out = _THINK_BLOCK_RE.sub("", text)
    closes = list(_THINK_CLOSE_RE.finditer(out))
    if closes:
        out = out[closes[-1].end():]
    out = _THINK_OPEN_RE.sub("", out)
    return out.strip()


# ---------------------------------------------------------------------------
# Privacy: what may go to a cloud endpoint
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<![\w,.])(?:\+|00|0)(?:[\s()/.-]{0,2}\d){8,14}(?!\d)")
_POSTCODE_RE = re.compile(r"^\s*\d{4,5}\s+")
SELLER_ATTRIBUTE_KEYS = ("Nutzertyp", "Aktiv seit", "Bewertung", "Anzeigen des Verkäufers", "Antwortzeit")


def redact_contacts(text: str) -> str:
    """Phone numbers and e-mail addresses in an ad text → placeholders (the code's own red-flag rules
    run on the full text locally; the model only needs to know that a contact is there)."""
    if not text:
        return text or ""
    text = _EMAIL_RE.sub("[E-Mail]", text)
    return _PHONE_RE.sub(lambda m: "[Telefon]" if len(re.sub(r"\D", "", m.group(0))) >= 9 else m.group(0), text)


def ad_city(location: str) -> str:
    """'10115 Mitte' → 'Mitte', 'Berlin - Mitte' stays: the ad's own place without the postal code."""
    return _POSTCODE_RE.sub("", location or "").strip()


def cloud_listing(listing: Any) -> Any:
    """The ad as a cloud model may see it: title, description, price, item attributes and photos;
    no seller name / ratings / seller attributes, the place without the postal code, no distance
    (it tells where the user lives), contacts in the text masked."""
    attrs = {k: v for k, v in (listing.attributes or {}).items() if k not in SELLER_ATTRIBUTE_KEYS}
    return listing.model_copy(update={
        "seller_name": "", "seller_type": "unknown", "seller_feedback_percent": None, "seller_feedback_score": None,
        "attributes": attrs, "location": ad_city(listing.location), "postal_code": None, "distance_km": None,
        "title": redact_contacts(listing.title), "description": redact_contacts(listing.description),
        "search_name": "",
    })


# ---------------------------------------------------------------------------
# Demo ads for «Проверить» (5 German ads: a PC with a hidden GPU, a typo, a wanted ad, a pram, a lot)
# ---------------------------------------------------------------------------


def demo_ads() -> list[Any]:
    from ..models import Listing
    from .triage import sample_ads

    ads = sample_ads()
    ads.append(Listing(ad_id="scout-sample-4", url="https://www.kleinanzeigen.de/s-anzeige/scout-sample-4",
                       title="Konvolut Nintendo Spiele und Konsole", price=80.0, price_text="80 € VB", negotiable=True,
                       description="Nachlass vom Dachboden: Nintendo Switch OLED weiß, 3 Spiele, dazu alte Kabel. "
                                   "Nur Abholung."))
    return ads


def is_limited(exc: BaseException | None) -> bool:
    return isinstance(exc, CloudLimited)


def first_line(texts: Iterable[str]) -> str:
    return next((t for t in texts if t), "")


__all__ = [
    "APP_TITLE", "CLOUD_BATCH", "CLOUD_KINDS", "CLOUD_TRIAGE_MAX_TOKENS", "CLOUD_VISION_MAX_TOKENS", "CloudLimited",
    "CloudPreset", "EBAY_LOCAL_RU", "OPENROUTER_HEADERS", "PRESETS", "QuotaLimiter", "QuotaRegistry",
    "ThinkingControls", "ad_city", "cloud_kind", "cloud_listing", "demo_ads", "detect_cloud", "endpoint_id",
    "is_cloud_model", "key_env", "limits_text", "local_only", "local_only_reason", "mask_key", "parse_key_info",
    "parse_models", "parse_retry", "pick_models", "plan_budget", "preset", "redact_contacts", "registry",
    "resolve_api_key", "stored_key", "strip_think", "thinking_controls",
]
