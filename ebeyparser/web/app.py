"""Web dashboard (FastAPI + Jinja2): deals, searches, status and a small JSON API.

The monitor and the notifiers are injected (duck-typed) so this module does not
depend on the scraper / AI / notify packages.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import re
import shutil
from collections.abc import Awaitable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config import AppConfig, ConfigError, SearchConfig, load_config
from ..db import Database
from ..models import DEAL_STATUSES, AIVerdict, DealView, RunSummary, utcnow
from ..notify.render import haggle_phrase, offer_terms
from ..scraper.categories import (
    DEFAULT_BUDGET,
    RADIUS_CHOICES,
    Category,
    CategoryList,
    SetupAnswers,
    answers_from_searches,
    builtin_by_id,
    builtin_categories,
    RESULT_PAGES_SHARE,
    describe_error,
    load_cached_categories,
    merge_searches,
    request_budgets,
    save_cached_categories,
    searches_from_answers,
    snap_radius,
)
from .configfile import (
    read_env_file,
    save_searches_block,
    update_yaml_values,
    write_env_values,
)
from .localai import LMSTUDIO_URL, OLLAMA_URL, detect_local_ai, probe_json
from .spa import CLASSIC_PREFIX, index_response, install_spa, wants_spa
from .security import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    TOKEN_HEADER,
    TOKEN_PARAM,
    HostGuard,
    ensure_token,
    is_loopback,
    token_matches,
    trusted_hosts,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
PLACEHOLDER_IMG = "/static/placeholder.svg"

PAGE_SIZE = 24
API_MAX_LIMIT = 500
AI_HEALTH_TIMEOUT = 8.0
NOTIFY_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 10.0
DISCOVERY_TIMEOUT = 25.0
SETUP_MIN_WISH_ROWS = 3

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ: Any = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - missing tzdata
    LOCAL_TZ = timezone.utc

# ----------------------------------------------------------------- labels (RU)
VERDICT_LABELS = {"buy": "Покупать", "maybe": "Подумать", "skip": "Пропустить", "none": "Не оценено"}
STATUS_LABELS = {
    "new": "Новое",
    "starred": "В избранном",
    "contacted": "Написал продавцу",
    "bought": "Куплено",
    "sold": "Продано",
    "ignored": "Скрыто",
}
PURPOSE_LABELS = {"resale": "Перепродажа", "personal": "Для себя"}
SOURCE_LABELS = {"kleinanzeigen": "Kleinanzeigen", "ebay": "eBay"}
AI_CONDITION_LABELS = {
    "new": "Новое",
    "like_new": "Как новое",
    "good": "Хорошее",
    "used": "Б/у, есть следы использования",
    "defective": "Неисправно / на запчасти",
    "unclear": "Неясно по фото",
}
COMP_SOURCE_LABELS = {
    "ebay_sold": "eBay · продано",
    "ebay": "eBay · цена",
    "kleinanzeigen": "Kleinanzeigen",
    "reference": "Справочная",
    "history": "История цен",
    "other": "Другое",
}
PRICE_SOURCE_LABELS = {
    "reference": "справочная цена из конфига",
    "history": "история цен",
    "kleinanzeigen": "объявления Kleinanzeigen",
    "ebay_sold": "реальные продажи eBay",
    "mixed": "продажи eBay + объявления",
    "ai": "оценка ИИ",
    "none": "нет данных",
}
BUYING_OPTION_LABELS = {"FIXED_PRICE": "Купить сейчас", "AUCTION": "Аукцион", "BEST_OFFER": "Предложить цену"}
EBAY_CONDITION_LABELS = {"NEW": "Новое", "USED": "Б/у", "UNSPECIFIED": "Не указано"}
SELLER_TYPE_LABELS = {"private": "Частное лицо", "commercial": "Коммерческий продавец", "unknown": "Не указано"}

VERDICT_TABS: list[tuple[str, str]] = [
    ("good", "Лучшие"),
    ("buy", "Покупать"),
    ("maybe", "Подумать"),
    ("all", "Все"),
]
SORTS: list[tuple[str, str]] = [
    ("score", "По баллу"),
    ("newest", "Сначала новые"),
    ("profit", "По прибыли"),
    ("price", "Сначала дешёвые"),
]
STATUS_FILTERS: list[tuple[str, str]] = [
    ("", "Без скрытых"),
    ("new", "Новые"),
    ("starred", "Избранное"),
    ("contacted", "Написал продавцу"),
    ("bought", "Куплено"),
    ("ignored", "Скрытые"),
    ("any", "Все вместе со скрытыми"),
]
NO_CHANNELS_MESSAGE = (
    "Не настроен ни один канал уведомлений — подключи Telegram или почту в «Настройки» → «Уведомления»."
)


# ------------------------------------------------------------------ formatting
def _group(number: str) -> str:
    return number.replace(",", " ")


def fmt_money(value: float | None, sign: bool = False, dash: str = "—") -> str:
    """1234.5 -> '1 234,50 €', 450 -> '450 €'; sign=True adds '+' for positives."""
    if value is None:
        return dash
    v = float(value)
    if abs(v) < 0.005:
        return "0\u00a0€"
    rounded = round(v)
    if abs(v - rounded) < 0.005:
        body = _group(f"{abs(rounded):,d}")
    else:
        body = _group(f"{abs(v):,.2f}".replace(".", "#")).replace("#", ",")
    prefix = "−" if v < 0 else ("+" if sign else "")
    return f"{prefix}{body}\u00a0€"


def fmt_percent(value: float | None, sign: bool = False) -> str:
    """Fraction -> percent: 1.16 -> '116 %'."""
    if value is None:
        return "—"
    pct = round(value * 100)
    prefix = "−" if pct < 0 else ("+" if sign and pct > 0 else "")
    return f"{prefix}{abs(pct)} %"


def fmt_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return _group(f"{int(value):,d}")
    return f"{value:.{digits}f}".replace(".", ",")


def plural(n: int | float, one: str, few: str, many: str) -> str:
    """Russian plural form for n: 1 ставка, 2 ставки, 5 ставок."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_datetime(dt: datetime | None) -> str:
    """'сегодня, 21:05' / 'вчера, 14:20' / '25.09.2026, 10:00' in Berlin time."""
    if dt is None:
        return "—"
    local = _aware(dt).astimezone(LOCAL_TZ)
    today = utcnow().astimezone(LOCAL_TZ).date()
    if local.date() == today:
        return f"сегодня, {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"вчера, {local:%H:%M}"
    if local.date() == today + timedelta(days=1):
        return f"завтра, {local:%H:%M}"
    return local.strftime("%d.%m.%Y, %H:%M")


def fmt_span(seconds: float) -> str:
    """Duration in words: '2 ч 15 мин', '45 мин', '3 дн.'."""
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
    days = hours // 24
    return f"{days} {plural(days, 'день', 'дня', 'дней')}"


def fmt_ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    delta = (utcnow() - _aware(dt)).total_seconds()
    if delta < 60:
        return "только что"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    local = _aware(dt).astimezone(LOCAL_TZ)
    if hours < 48 and local.date() == utcnow().astimezone(LOCAL_TZ).date() - timedelta(days=1):
        return f"вчера в {local:%H:%M}"
    days = hours // 24
    return f"{days} {plural(days, 'день', 'дня', 'дней')} назад"


def fmt_until(dt: datetime | None) -> str:
    if dt is None:
        return ""
    delta = (_aware(dt) - utcnow()).total_seconds()
    if delta <= 0:
        return "завершён"
    return f"через {fmt_span(delta)}"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} с" if sec else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def mask_email(address: str) -> str:
    """'maksem706@gmail.com' -> 'ma***@gmail.com'."""
    address = (address or "").strip()
    if not address:
        return ""
    if "@" not in address:
        return mask_secret(address)
    local, domain = address.split("@", 1)
    return f"{local[:2]}***@{domain}"


def mask_secret(value: str | int | None, keep: int = 2) -> str:
    """'123456789' -> '12***89'; short values are fully masked."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2 + 1:
        return "***"
    return f"{text[:keep]}***{text[-keep:]}"


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _to_int(raw: Any, default: int) -> int:
    value = _to_float(raw)
    return int(value) if value is not None else default


def _num_text(value: float | int | None) -> str:
    """Number for form inputs: 25.0 -> '25', 0.5 -> '0.5'."""
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


# ---------------------------------------------------------------------- icons
# Lucide-style 24x24 stroke icons, inlined so the UI works offline.
ICONS: dict[str, str] = {
    "tag": '<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"/><circle cx="7.5" cy="7.5" r="1.2" fill="currentColor"/>',
    "search": '<circle cx="11" cy="11" r="7.5"/><path d="m21 21-4.3-4.3"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "star": '<path d="m12 2.5 2.9 6.1 6.6.8-4.9 4.6 1.3 6.6L12 17.3l-5.9 3.3 1.3-6.6-4.9-4.6 6.6-.8Z"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "external": '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "clock": '<circle cx="12" cy="12" r="9.5"/><path d="M12 6.5V12l3.5 2"/>',
    "truck": '<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2M15 18H9M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.62l-3.48-4.35A1 1 0 0 0 17.52 8H14"/><circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>',
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
    "sparkles": '<path d="M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9Z"/><path d="M19 3v4M17 5h4M5 17v4M3 19h4"/>',
    "alert": '<path d="m21.7 18-8-14a2 2 0 0 0-3.5 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4M12 17h.01"/>',
    "plus": '<path d="M5 12h14M12 5v14"/>',
    "pencil": '<path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    "trash": '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "gavel": '<path d="m14 13-7.5 7.5a2.12 2.12 0 0 1-3-3L11 10M16 16l6-6M8 8l6-6M9 7l8 8M21 11l-8-8"/>',
    "target": '<circle cx="12" cy="12" r="9.5"/><circle cx="12" cy="12" r="5.5"/><circle cx="12" cy="12" r="1.5"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "info": '<circle cx="12" cy="12" r="9.5"/><path d="M12 16v-4M12 8h.01"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    "message": '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    "arrow-left": '<path d="m12 19-7-7 7-7M19 12H5"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "package": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
    "trending": '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
    "wallet": '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5M3 12a9 3 0 0 0 18 0"/>',
    "settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
}


def icon(name: str, cls: str = "") -> Markup:
    body = ICONS.get(name, ICONS["info"])
    klass = f"icon icon-{name}" + (f" {cls}" if cls else "")
    return Markup(
        f'<svg class="{klass}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


# --------------------------------------------------------------------- filters
@dataclass
class DealFilters:
    """Dashboard / API filters, parsed leniently from query parameters."""

    verdict: str = "good"  # good (buy+maybe) | buy | maybe | skip | all
    purpose: str = ""  # "" | resale | personal
    source: str = ""  # "" | kleinanzeigen | ebay
    search: str = ""
    status: str = ""  # "" (all but ignored) | a deal status | any
    q: str = ""
    sort: str = "score"
    min_score: float | None = None

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> DealFilters:
        def get(key: str) -> str:
            return str(params.get(key) or "").strip()

        verdict = get("verdict") or "good"
        if verdict not in {"good", "buy", "maybe", "skip", "all"}:
            verdict = "good"
        purpose = get("purpose")
        source = get("source")
        status = get("status")
        sort = get("sort") or "score"
        min_score = _to_float(get("min_score"))
        return cls(
            verdict=verdict,
            purpose=purpose if purpose in PURPOSE_LABELS else "",
            source=source if source in SOURCE_LABELS else "",
            search=get("search") or get("search_name"),
            status=status if status in DEAL_STATUSES or status == "any" else "",
            q=get("q")[:200],
            sort=sort if sort in dict(SORTS) else "score",
            min_score=max(0.0, min(100.0, min_score)) if min_score is not None else None,
        )

    def db_kwargs(self) -> dict[str, Any]:
        verdict: str | list[str] | None
        if self.verdict == "good":
            verdict = ["buy", "maybe"]
        elif self.verdict == "all":
            verdict = None
        else:
            verdict = self.verdict
        return {
            "verdict": verdict,
            "min_score": self.min_score,
            "search_name": self.search or None,
            "status": self.status if self.status in DEAL_STATUSES else None,
            "purpose": self.purpose or None,
            "q": self.q or None,
            "include_ignored": self.status == "any",
        }

    def params(self, **overrides: Any) -> dict[str, str]:
        """Non-default values as query parameters."""
        data = replace(self, **{k: v for k, v in overrides.items() if k != "page"})
        out: dict[str, str] = {}
        if data.verdict != "good":
            out["verdict"] = data.verdict
        for key in ("purpose", "source", "search", "status", "q"):
            if getattr(data, key):
                out[key] = getattr(data, key)
        if data.sort != "score":
            out["sort"] = data.sort
        if data.min_score is not None:
            out["min_score"] = _num_text(data.min_score)
        page = overrides.get("page")
        if page and int(page) > 1:
            out["page"] = str(page)
        return out

    def url(self, **overrides: Any) -> str:
        params = self.params(**overrides)
        return f"{CLASSIC_PREFIX}?" + urlencode(params) if params else CLASSIC_PREFIX

    @property
    def panel_active(self) -> int:
        """How many filters inside the collapsible panel are set (for the mobile badge)."""
        return sum(
            bool(v)
            for v in (self.purpose, self.source, self.search, self.status, self.sort != "score",
                      self.min_score is not None)
        )


def query_deals(db: Database, filters: DealFilters, limit: int, offset: int) -> tuple[list[DealView], int]:
    kwargs = filters.db_kwargs()
    if filters.source:
        kwargs["source"] = filters.source
    items = db.list_deals(**kwargs, sort=filters.sort, limit=limit, offset=offset) if limit > 0 else []
    return items, db.count_deals(**kwargs)


# ------------------------------------------------------------ deal view-models
@dataclass
class ProfitInfo:
    kind: str  # "profit" | "savings"
    value: float
    text: str
    extra: str  # "ROI 56 %" / "−42 % от рынка"
    positive: bool


@dataclass
class DealCard:
    deal: DealView
    id: str
    href: str
    title: str
    url: str
    source: str
    source_label: str
    images: list[str]
    verdict: str
    verdict_label: str
    score: int | None
    purpose: str
    purpose_label: str
    status: str
    status_label: str
    note: str
    price_text: str
    price_prefix: str
    is_free: bool
    negotiable: bool
    shipping_text: str
    market_text: str
    profit: ProfitInfo | None
    max_buy_label: str
    max_buy_text: str
    max_buy_ok: bool | None
    location: str
    ago: str
    first_seen_iso: str
    summary: str
    flags: list[str]
    is_auction: bool
    bids_text: str
    ends_text: str
    ends_iso: str
    ends_soon: bool
    condition: str
    seller_rating: str
    search_name: str
    buying_options: list[str] = field(default_factory=list)
    haggle: str = ""  # "Торгуйся: предложи 400 € → прибыль ≈ 120 €"
    unchecked: bool = False  # the AI was down when this ad was evaluated: photos NOT checked

    @property
    def image(self) -> str:
        return self.images[0] if self.images else PLACEHOLDER_IMG


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def _market_price(deal: DealView) -> float | None:
    ev = deal.evaluation
    if ev is None:
        return None
    if ev.estimate.market_price is not None:
        return ev.estimate.market_price
    return ev.ai.estimated_market_price if ev.ai else None


def present_deal(deal: DealView) -> DealCard:
    listing, ev = deal.listing, deal.evaluation
    source = getattr(listing, "source", "kleinanzeigen") or "kleinanzeigen"
    buying = list(getattr(listing, "buying_options", []) or [])
    is_auction = "AUCTION" in buying

    # price
    if listing.is_free:
        price_text = "Бесплатно"
    elif listing.price is None:
        price_text = "VB" if listing.negotiable else (listing.price_text or "Цена не указана")
    else:
        price_text = fmt_money(listing.price)
    price_prefix = "Ставка" if is_auction else ""

    shipping_cost = getattr(listing, "shipping_cost", None)
    if shipping_cost:
        shipping_text = f"+ {fmt_money(shipping_cost)} доставка"
    elif shipping_cost == 0 and source == "ebay":
        shipping_text = "бесплатная доставка"
    elif listing.shipping_possible is False:
        shipping_text = "только самовывоз"
    elif listing.shipping_possible:
        shipping_text = "есть доставка"
    else:
        shipping_text = ""

    market = _market_price(deal)
    market_text = f"рынок ~{fmt_money(round(market))}" if market else ""

    profit: ProfitInfo | None = None
    max_buy_label = max_buy_text = ""
    max_buy_ok: bool | None = None
    if ev is not None and ev.expected_profit is not None:
        p = ev.expected_profit
        if ev.purpose == "personal":
            extra = fmt_percent(-p / market) + " от рынка" if market and p > 0 else ""
            text = f"экономия {fmt_money(round(p))}" if p >= 0 else f"дороже рынка на {fmt_money(round(-p))}"
            profit = ProfitInfo("savings", p, text, extra, p > 0)
        else:
            if ev.roi is not None:
                extra = f"ROI {fmt_percent(ev.roi)}"
            elif (ev.buy_price or 0) == 0 and p > 0:
                extra = "даром"
            else:
                extra = ""
            profit = ProfitInfo("profit", p, fmt_money(round(p), sign=True), extra, p > 0)
    terms = offer_terms(ev, listing)
    if terms is not None and terms[1] is not None:  # haggle: show the profit at the suggested offer
        offer, at_offer, _ = terms
        if ev is not None and ev.purpose == "personal":
            text = f"предложи {fmt_money(offer)} → экономия {fmt_money(round(at_offer))}"
            profit = ProfitInfo("savings", at_offer, text, "", at_offer > 0)
        else:
            text = f"предложи {fmt_money(offer)} → {fmt_money(round(at_offer), sign=True)}"
            profit = ProfitInfo("profit", at_offer, text, "", at_offer > 0)  # short: must fit the card
    max_buy = getattr(ev, "max_buy_price", None) if ev else None
    if max_buy is not None and max_buy > 0 and not listing.is_free:
        max_buy_label = "Макс. ставка" if is_auction else "Выгодно до"
        max_buy_text = fmt_money(max_buy)
        if listing.price is not None:
            max_buy_ok = listing.price <= max_buy

    flags: list[str] = []
    summary = ""
    if ev is not None:
        flags = list(ev.red_flags)
        for verdict in (ev.ai, getattr(ev, "ai_second", None)):
            if verdict is not None:
                flags += verdict.red_flags
        if ev.ai and ev.ai.reasoning:
            summary = ev.ai.reasoning
        elif ev.reasons:
            summary = " · ".join(ev.reasons[:2])
    if not summary:
        summary = " ".join(listing.description.split())[:220]

    location = listing.location
    if listing.distance_km is not None:
        location = f"{location} · {fmt_number(listing.distance_km)} км" if location else f"{fmt_number(listing.distance_km)} км"

    ends_at = getattr(listing, "ends_at", None)
    bid_count = getattr(listing, "bid_count", None)
    bids_text = f"{bid_count} {plural(bid_count, 'ставка', 'ставки', 'ставок')}" if bid_count is not None else ""
    ends_soon = bool(ends_at and 0 < (_aware(ends_at) - utcnow()).total_seconds() < 3 * 3600)

    fb_pct = getattr(listing, "seller_feedback_percent", None)
    fb_score = getattr(listing, "seller_feedback_score", None)
    seller_rating = ""
    if fb_pct is not None:
        seller_rating = f"{fmt_number(fb_pct)} %"
        if fb_score is not None:
            seller_rating += f" · {fmt_number(fb_score)} {plural(fb_score, 'отзыв', 'отзыва', 'отзывов')}"

    condition = (getattr(listing, "condition", "") or listing.attributes.get("Zustand", "")).strip()
    verdict_key = ev.verdict if ev else "none"
    purpose = ev.purpose if ev else "resale"
    return DealCard(
        deal=deal,
        id=listing.ad_id,
        href=f"{CLASSIC_PREFIX}/deal/{quote(listing.ad_id, safe='')}",
        title=listing.title,
        url=listing.url,
        source=source,
        source_label=SOURCE_LABELS.get(source, source),
        images=[u for u in listing.image_urls if u],
        verdict=verdict_key,
        verdict_label=VERDICT_LABELS.get(verdict_key, verdict_key),
        score=round(ev.score) if ev else None,
        purpose=purpose,
        purpose_label=PURPOSE_LABELS.get(purpose, purpose),
        status=deal.status,
        status_label=STATUS_LABELS.get(deal.status, deal.status),
        note=deal.note,
        price_text=price_text,
        price_prefix=price_prefix,
        is_free=listing.is_free,
        negotiable=listing.negotiable and listing.price is not None,
        shipping_text=shipping_text,
        market_text=market_text,
        profit=profit,
        max_buy_label=max_buy_label,
        max_buy_text=max_buy_text,
        max_buy_ok=max_buy_ok,
        location=location,
        ago=fmt_ago(listing.first_seen),
        first_seen_iso=_aware(listing.first_seen).isoformat(),
        summary=summary,
        flags=_dedupe(flags),
        is_auction=is_auction,
        bids_text=bids_text,
        ends_text=fmt_until(ends_at) if ends_at else "",
        ends_iso=_aware(ends_at).isoformat() if ends_at else "",
        ends_soon=ends_soon,
        condition=condition,
        seller_rating=seller_rating,
        search_name=listing.search_name,
        buying_options=buying,
        haggle=haggle_phrase(ev, listing),
        unchecked=getattr(ev, "ai_checked", None) is False,
    )


def profit_breakdown(deal: DealView, config: AppConfig) -> dict[str, Any] | None:
    """Rows explaining expected profit: market -> margin -> fees -> shipping -> buy -> net."""
    ev = deal.evaluation
    if ev is None:
        return None
    market = _market_price(deal)
    buy = ev.buy_price if ev.buy_price is not None else deal.listing.price
    if market is None or buy is None:
        return {"rows": [], "total": None, "missing": True}
    listing_shipping = getattr(deal.listing, "shipping_cost", None) or 0.0
    buy_note = f"вкл. доставку {fmt_money(listing_shipping)}" if listing_shipping else ""
    est = ev.estimate
    market_note = ""
    if est.sample_size:
        market_note = f"{est.sample_size} {plural(est.sample_size, 'сравнение', 'сравнения', 'сравнений')}"
        if est.low is not None and est.high is not None:
            market_note += f", {fmt_money(est.low)} – {fmt_money(est.high)}"
    elif ev.estimate.market_price is None and ev.ai:
        market_note = "оценка ИИ"

    rows: list[dict[str, Any]] = [{"label": "Рыночная цена", "value": market, "note": market_note, "kind": "base"}]
    if ev.purpose == "personal":
        rows.append({"label": "Цена покупки", "value": -buy, "note": buy_note, "kind": "minus"})
        computed = market - buy
        total_label = "Экономия" if (ev.expected_profit if ev.expected_profit is not None else computed) >= 0 else "Переплата"
    else:
        margin_pct = config.pricing.safety_margin_percent
        margin = market * margin_pct / 100
        fee_pct = config.pricing.selling_fee_percent + config.pricing.payment_fee_percent
        rows.append({"label": f"Запас на торг и риск ({fmt_number(margin_pct)}\u00a0%)", "value": -margin, "note": "", "kind": "minus"})
        rows.append({
            "label": "Комиссии при продаже",
            "value": -ev.fees,
            "note": f"{fmt_number(fee_pct, 2)}\u00a0%" if fee_pct else "частным продавцам 0\u00a0%",
            "kind": "minus",
        })
        rows.append({"label": "Твоя доставка покупателю", "value": -ev.shipping_cost, "note": "", "kind": "minus"})
        rows.append({"label": "Цена покупки", "value": -buy, "note": buy_note, "kind": "minus"})
        computed = market - margin - ev.fees - ev.shipping_cost - buy
        total_label = "Чистая прибыль"
    total = ev.expected_profit if ev.expected_profit is not None else computed
    if abs(total - computed) >= 0.5:
        rows.append({"label": "Прочие поправки оценки", "value": total - computed, "note": "", "kind": "minus"})
    roi = ev.roi if ev.roi is not None else (total / buy if buy else None)
    return {
        "rows": rows,
        "total": total,
        "total_label": total_label,
        "roi": roi,
        "purpose": ev.purpose,
        "missing": False,
        "source": PRICE_SOURCE_LABELS.get(est.source, est.source),
        "notes": est.notes,
        "query": est.query,
    }


def ai_panel(verdict: AIVerdict | None, title: str) -> dict[str, Any] | None:
    if verdict is None:
        return None
    match = verdict.photo_matches_description
    return {
        "title": title,
        "v": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict.verdict, verdict.verdict),
        "condition": AI_CONDITION_LABELS.get(verdict.condition, verdict.condition),
        "match": "yes" if match else ("no" if match is False else "unknown"),
        "confidence": max(0, min(100, round(verdict.confidence * 100))),
        "market": fmt_money(verdict.estimated_market_price) if verdict.estimated_market_price else "",
    }


# ------------------------------------------------------------ monitor helpers
def _monitor_running(app: FastAPI) -> bool:
    monitor = app.state.monitor
    if monitor is None:
        return False
    task: asyncio.Task | None = getattr(app.state, "run_task", None)
    return bool(getattr(monitor, "is_running", False)) or (task is not None and not task.done())


def _summary_dict(summary: RunSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    if isinstance(summary, BaseModel):
        return summary.model_dump(mode="json")
    return dict(summary) if isinstance(summary, Mapping) else None


def monitor_state(app: FastAPI) -> dict[str, Any]:
    monitor = app.state.monitor
    if monitor is None:
        return {"available": False, "running": False, "loop": False, "next_run_at": None, "last_summary": None}
    next_run = getattr(monitor, "next_run_at", None)
    loop_task: asyncio.Task | None = getattr(app.state, "monitor_task", None)
    return {
        "available": True,
        "running": _monitor_running(app),
        "loop": loop_task is not None and not loop_task.done(),
        "next_run_at": _aware(next_run).isoformat() if isinstance(next_run, datetime) else None,
        "last_summary": _summary_dict(getattr(monitor, "last_summary", None)),
    }


def monitor_pill(state: dict[str, Any]) -> tuple[str, str]:
    if not state["available"]:
        return "off", "Монитор выключен"
    if state["running"]:
        return "running", "Идёт проверка…"
    if state["next_run_at"]:
        seconds = (datetime.fromisoformat(state["next_run_at"]) - utcnow()).total_seconds()
        if seconds <= 60:
            return "idle", "Проверка вот-вот начнётся"
        return "idle", f"Следующая проверка через {fmt_span(seconds)}"
    if state["loop"]:
        return "idle", "Монитор запущен"
    return "paused", "Автопроверка выключена"


async def ai_status(app: FastAPI) -> dict[str, Any]:
    ai = app.state.config.ai
    base = {"enabled": ai.enabled, "provider": ai.provider, "base_url": ai.base_url, "model": ai.model}
    if not ai.enabled:
        return {**base, "ok": False, "model_available": False, "error": "ИИ выключен в конфиге"}
    fn = getattr(app.state.monitor, "ai_health", None)
    if fn is None:
        return {**base, "ok": False, "model_available": None, "error": "Монитор не подключён — проверить ИИ-сервер нельзя"}
    try:
        result = await asyncio.wait_for(fn(), timeout=AI_HEALTH_TIMEOUT)
    except asyncio.TimeoutError:
        return {**base, "ok": False, "model_available": None, "error": f"ИИ-сервер не ответил за {AI_HEALTH_TIMEOUT:g} с"}
    except Exception as exc:  # health checks must never break the page
        return {**base, "ok": False, "model_available": None, "error": str(exc) or type(exc).__name__}
    return {**base, **(result if isinstance(result, Mapping) else {})}


async def http_limits(app: FastAPI) -> list[dict[str, Any]]:
    """Per-site request budget and block cooldowns for the status page: from the monitor
    (`http_status()`) when it has data, else read-only from data/http_state.json."""
    from ..runtime import http_state_path

    data: Any = None
    fn = getattr(app.state.monitor, "http_status", None)
    if callable(fn):
        try:
            data = fn()
            if inspect.isawaitable(data):
                data = await data
        except Exception:  # status must never break the page
            log.exception("monitor.http_status failed")
            data = None
    cfg: AppConfig = app.state.config
    path = http_state_path(cfg.data_path)
    if not data and path.is_file():  # monitor idle / its client not created yet: read the shared file
        from ..scraper.http import PoliteClient

        client = PoliteClient.from_config(cfg.general, state_path=path)
        try:
            data = client.host_status()
        except Exception:
            log.exception("reading %s failed", path)
        finally:
            client.state_path = None  # read-only: never overwrite the monitor's newer state
            await client.aclose()
    rows: list[dict[str, Any]] = []
    for host, info in sorted((data or {}).items()) if isinstance(data, Mapping) else []:
        if not isinstance(info, Mapping):
            continue
        until = info.get("cooldown_until")
        last = info.get("last_block_at")
        limit = info.get("limit_per_hour")
        used = info.get("requests_last_hour") or 0
        rows.append({
            "host": host,
            "requests": used,
            "images": info.get("images_last_hour") or 0,
            "limit": limit,
            "blocked": bool(info.get("blocked")),
            "until": fmt_datetime(until) if isinstance(until, datetime) else "",
            "exhausted": bool(limit) and used >= limit,
            "strikes": info.get("strikes") or 0,
            "reason": str(info.get("last_block_reason") or ""),
            "last_block": fmt_ago(last) if isinstance(last, datetime) else "",
            "note": str(info.get("note") or ""),
        })
    return rows


def keyword_only(config: AppConfig) -> bool:
    """Kleinanzeigen searches exist, but none scans a category (the v0.1 setup)."""
    from ..scraper.categories import category_id_from_url

    enabled = [s for s in config.searches if s.enabled and s.source == "kleinanzeigen"]
    return bool(enabled) and not any(s.category_id or (s.url and category_id_from_url(s.url)) for s in enabled)


_QUEUE_KEYS = ("pending", "queue", "queued", "backlog", "waiting")
_EXPIRED_KEYS = ("expired_24h", "overdue_24h", "expired_day", "expired", "overdue")


def _first_int(data: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


async def backlog_info(app: FastAPI) -> dict[str, int | None] | None:
    """Evaluation queue for the status page: monitor.backlog_status() if it exists, else the
    database counts. {"queue": N, "expired_24h": M}; None when nothing is known."""
    data: Any = None
    fn = getattr(app.state.monitor, "backlog_status", None)
    if callable(fn):
        try:
            data = fn()
            if inspect.isawaitable(data):
                data = await data
        except Exception:
            log.exception("monitor.backlog_status failed")
            data = None
    queue = expired = None
    if isinstance(data, Mapping):
        queue, expired = _first_int(data, _QUEUE_KEYS), _first_int(data, _EXPIRED_KEYS)
    elif isinstance(data, (tuple, list)) and len(data) >= 2:
        queue, expired = (int(v) if isinstance(v, (int, float)) else None for v in data[:2])
    db = app.state.db
    try:
        if queue is None and callable(getattr(db, "pending_count", None)):
            queue = int(db.pending_count())
        if expired is None and callable(getattr(db, "expired_since", None)):
            expired = int(db.expired_since(utcnow() - timedelta(days=1)))
    except Exception:
        log.exception("backlog counts failed")
    if queue is None and expired is None:
        return None
    return {"queue": queue, "expired_24h": expired}


async def _guarded(coro: Any, label: str) -> Any:
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s failed", label)
        return None


async def _stop_task(task: asyncio.Task | None, timeout: float) -> None:
    if task is None or task.done():
        return
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


# ------------------------------------------------------------- searches (form)
SEARCH_FIELD_LABELS = {
    "name": "Название",
    "source": "Источник",
    "enabled": "Включён",
    "url": "URL поиска",
    "query": "Запрос",
    "location": "Где",
    "location_id": "ID места",
    "radius_km": "Радиус, км",
    "category_id": "Категория",
    "min_price": "Цена от",
    "max_price": "Цена до",
    "purpose": "Цель",
    "include_keywords": "Обязательные слова",
    "exclude_keywords": "Слова-исключения",
    "reference_price": "Рыночная цена",
    "target_price": "Мой лимит цены",
    "min_profit": "Мин. прибыль",
    "min_roi": "Мин. ROI",
    "max_pages": "Страниц за проверку",
    "ebay_category_ids": "Категории eBay",
    "buying_options": "Формат продажи",
    "ebay_conditions": "Состояние",
    "local_pickup_only": "Только самовывоз",
    "ending_within_hours": "Заканчиваются в течение, ч",
}
_TEXT_FIELDS = ("name", "url", "query", "location")
_INT_FIELDS = ("location_id", "radius_km", "category_id", "max_pages")
_FLOAT_FIELDS = ("min_price", "max_price", "reference_price", "target_price", "min_profit", "ending_within_hours")
_LIST_FIELDS = ("include_keywords", "exclude_keywords", "ebay_category_ids")


def _split_list(raw: str) -> list[str]:
    parts = raw.replace("\n", ",").replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def search_form_values(search: SearchConfig | None) -> dict[str, Any]:
    if search is None:
        search = SearchConfig(name="")
    values: dict[str, Any] = {
        "name": search.name,
        "source": search.source,
        "enabled": search.enabled,
        "purpose": search.purpose,
        "url": search.url or "",
        "query": search.query,
        "location": search.location,
        "min_roi": _num_text(search.min_roi * 100 if search.min_roi is not None else None),
        "buying_options": list(search.buying_options),
        "ebay_conditions": list(search.ebay_conditions),
        "local_pickup_only": search.local_pickup_only,
    }
    for key in _INT_FIELDS + _FLOAT_FIELDS:
        values[key] = _num_text(getattr(search, key))
    for key in _LIST_FIELDS:
        values[key] = ", ".join(getattr(search, key))
    return values


def parse_search_form(form: Mapping[str, Any], getlist: Callable[[str], list[str]]) -> tuple[dict[str, Any], SearchConfig | None, list[str]]:
    """HTML form -> (raw values for re-render, validated SearchConfig or None, errors)."""
    errors: list[str] = []
    raw = {key: str(form.get(key) or "") for key in SEARCH_FIELD_LABELS}
    values: dict[str, Any] = dict(raw)
    values["enabled"] = bool(form.get("enabled"))
    values["local_pickup_only"] = bool(form.get("local_pickup_only"))
    values["buying_options"] = [v for v in getlist("buying_options") if v in BUYING_OPTION_LABELS]
    values["ebay_conditions"] = [v for v in getlist("ebay_conditions") if v in EBAY_CONDITION_LABELS]

    data: dict[str, Any] = {
        "name": raw["name"].strip(),
        "source": raw["source"] if raw["source"] in SOURCE_LABELS else "kleinanzeigen",
        "enabled": values["enabled"],
        "purpose": raw["purpose"] if raw["purpose"] in PURPOSE_LABELS else "resale",
        "url": raw["url"].strip() or None,
        "query": raw["query"].strip(),
        "location": raw["location"].strip(),
        "buying_options": values["buying_options"],
        "ebay_conditions": values["ebay_conditions"],
        "local_pickup_only": values["local_pickup_only"],
    }
    for key in _INT_FIELDS + _FLOAT_FIELDS + ("min_roi",):
        text = raw[key].strip()
        if not text:
            data[key] = None
            continue
        number = _to_float(text)
        if number is None or (key in _INT_FIELDS and not number.is_integer()):
            kind = "целое число" if key in _INT_FIELDS else "число"
            errors.append(f"«{SEARCH_FIELD_LABELS[key]}»: нужно {kind}")
            continue
        if number < 0:
            errors.append(f"«{SEARCH_FIELD_LABELS[key]}»: не может быть отрицательным")
            continue
        if key == "min_roi":
            number = number / 100  # the form asks for percent
        data[key] = int(number) if key in _INT_FIELDS else number
    for key in _LIST_FIELDS:
        data[key] = _split_list(raw[key])
    if errors:
        return values, None, errors
    try:
        search = SearchConfig.model_validate(data)
    except ValidationError as exc:
        return values, None, _validation_messages(exc)
    errors = search_problems(search)
    return values, (None if errors else search), errors


def _validation_messages(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        key = str(err.get("loc", ["?"])[0])
        out.append(f"«{SEARCH_FIELD_LABELS.get(key, key)}»: {err.get('msg', 'неверное значение')}")
    return out


def search_problems(search: SearchConfig) -> list[str]:
    """Semantic checks on top of the pydantic model."""
    errors: list[str] = []
    if not search.name.strip():
        errors.append("Укажи название поиска")
    source = getattr(search, "source", "kleinanzeigen")
    if search.url:
        parts = urlsplit(search.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            errors.append("URL должен начинаться с https://")
        elif source == "kleinanzeigen" and "kleinanzeigen.de" not in parts.netloc:
            errors.append("URL должен вести на kleinanzeigen.de")
    if source == "ebay":
        if not search.query and not getattr(search, "ebay_category_ids", []):
            errors.append("Для eBay укажи запрос или категорию eBay")
        if getattr(search, "local_pickup_only", False) and not (search.location and search.radius_km):
            errors.append("Для самовывоза укажи почтовый индекс и радиус")
    elif not (search.url or search.query or search.category_id):
        errors.append("Укажи URL поиска с kleinanzeigen.de или запрос (или категорию)")
    if search.min_price is not None and search.max_price is not None and search.min_price > search.max_price:
        errors.append("«Цена от» больше, чем «Цена до»")
    return errors


def describe_search(search: SearchConfig) -> dict[str, Any]:
    """Human-friendly facts for a search card."""
    price = ""
    if search.min_price is not None or search.max_price is not None:
        lo = fmt_money(search.min_price) if search.min_price is not None else ""
        hi = fmt_money(search.max_price) if search.max_price is not None else ""
        price = f"{lo} – {hi}" if lo and hi else (f"от {lo}" if lo else f"до {hi}")
    where = search.location
    if search.radius_km:
        where = f"{where} + {search.radius_km} км" if where else f"радиус {search.radius_km} км"
    return {
        "s": search,
        "source": getattr(search, "source", "kleinanzeigen"),
        "source_label": SOURCE_LABELS.get(getattr(search, "source", "kleinanzeigen"), "Kleinanzeigen"),
        "purpose_label": PURPOSE_LABELS.get(search.purpose, search.purpose),
        "price": price,
        "where": where,
        "buying": [BUYING_OPTION_LABELS.get(b, b) for b in getattr(search, "buying_options", [])],
        "conditions": [EBAY_CONDITION_LABELS.get(c, c) for c in getattr(search, "ebay_conditions", [])],
        "min_roi": fmt_percent(search.min_roi) if search.min_roi is not None else "",
    }


# ------------------------------------------------------------- setup wizard
def setup_form_values(answers: SetupAnswers, *, replace: bool = True) -> dict[str, Any]:
    """Wizard answers -> raw strings for the /setup form."""
    return {
        "location": answers.location,
        "radius": answers.radius_km,
        "purpose": answers.purpose,
        "max_price": _num_text(answers.max_price),
        "min_profit": _num_text(answers.min_profit),
        "category_ids": list(answers.category_ids),
        "wishlist": [(item, _num_text(price)) for item, price in answers.wishlist],
        "interval_minutes": _num_text(answers.interval_minutes),
        "replace": replace,
    }


def parse_setup_form(
    form: Mapping[str, Any], getlist: Callable[[str], list[str]], *, default_min_profit: float
) -> tuple[SetupAnswers, dict[str, Any], list[str]]:
    """/setup form (POST body or GET query) -> (answers, raw values for re-render, errors)."""
    errors: list[str] = []
    location = " ".join(str(form.get("location") or "").split())[:80]
    if not location:
        errors.append("Укажи город или почтовый индекс")
    radius_raw = str(form.get("radius") or "").strip()
    radius = _to_float(radius_raw) if radius_raw else 30.0
    if radius is None or radius < 0:
        errors.append("«Радиус»: нужно целое число километров")
        radius = 30.0
    purpose = str(form.get("purpose") or "resale")
    purpose = purpose if purpose in PURPOSE_LABELS else "resale"

    def money(key: str, label: str, default: float | None) -> float | None:
        text = str(form.get(key) or "").strip()
        if not text:
            return default
        value = _to_float(text.replace("€", ""))
        if value is None or value < 0:
            errors.append(f"«{label}»: нужно число, например 400")
            return default
        return value

    max_price = money("max_price", "Максимальная цена за вещь", None)
    if max_price == 0:
        errors.append("«Максимальная цена за вещь» должна быть больше нуля")
    min_profit = money("min_profit", "Минимальная прибыль", default_min_profit)
    interval = money("interval_minutes", "Как часто проверять", None)
    if interval is not None and not 5 <= interval <= 1440:
        errors.append("«Как часто проверять»: от 5 до 1440 минут (чаще 10 минут не советую)")
        interval = None
    category_ids: list[int] = []
    for raw in getlist("category"):
        value = _to_float(raw)
        if value is not None and value.is_integer() and value > 0 and int(value) not in category_ids:
            category_ids.append(int(value))
    items, prices = getlist("wish_item"), getlist("wish_price")
    rows: list[tuple[str, str]] = []
    wishlist: list[tuple[str, float | None]] = []
    for i, item in enumerate(items[:30]):
        item = " ".join(str(item).split())[:80]
        price_text = str(prices[i] if i < len(prices) else "").strip()
        if not item:
            continue
        rows.append((item, price_text))
        price = _to_float(price_text.replace("€", "")) if price_text else None
        if price_text and (price is None or price <= 0):
            errors.append(f"«{item}»: цена должна быть числом больше нуля")
            continue
        wishlist.append((item, price))
    answers = SetupAnswers(
        location=location,
        radius_km=snap_radius(int(radius)),
        category_ids=category_ids,
        purpose=purpose,
        max_price=max_price,
        min_profit=min_profit if min_profit is not None else default_min_profit,
        wishlist=wishlist,
        interval_minutes=interval,
    )
    values = setup_form_values(answers, replace=bool(form.get("replace")))
    for key in ("max_price", "min_profit", "interval_minutes"):
        values[key] = str(form.get(key) or "").strip()
    values["wishlist"] = rows
    return answers, values, errors


def setup_category_groups(categories: list[Category], selected: list[int]) -> list[tuple[str, list[Category]]]:
    """Checkbox groups for the /setup page; selected ids missing from the list are appended."""
    known = {c.id for c in categories}
    missing = [builtin_by_id(cid) or Category(cid, f"Категория {cid}") for cid in selected if cid not in known]
    resale = [c for c in categories if c.resale_friendly]
    other = [c for c in categories if not c.resale_friendly] + missing
    groups = [("Хорошо перепродаются", resale), ("Другие категории", other)]
    return [(title, cats) for title, cats in groups if cats]


# --------------------------------------------------------------------- pagination
def page_links(page: int, pages: int) -> list[int | None]:
    """[1, None, 4, 5, 6, None, 10] — None is an ellipsis."""
    if pages <= 7:
        return list(range(1, pages + 1))
    wanted = {1, pages, page - 1, page, page + 1}
    out: list[int | None] = []
    last = 0
    for p in sorted(x for x in wanted if 1 <= x <= pages):
        if p - last > 1:
            out.append(None)
        out.append(p)
        last = p
    return out


# -------------------------------------------------------------------- API bodies
class StatusUpdate(BaseModel):
    status: str
    note: str | None = None


def _static_version() -> str:
    digest = hashlib.md5(usedforsecurity=False)
    if STATIC_DIR.is_dir():
        for path in sorted(STATIC_DIR.iterdir()):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


def _mask_ebay(config: AppConfig) -> dict[str, Any]:
    ebay = getattr(config, "ebay", None)
    if ebay is None:
        return {"available": False}
    configured = bool(getattr(ebay, "configured", False))
    if getattr(ebay, "client_id", ""):
        credentials = f"App ID {mask_secret(ebay.client_id, 4)}"
    elif getattr(ebay, "oauth_token", ""):
        credentials = "OAuth-токен (живёт ~2 часа)"
    else:
        credentials = "не заданы"
    return {
        "available": True,
        "configured": configured,
        "credentials": credentials,
        "marketplace": getattr(ebay, "marketplace_id", ""),
        "sandbox": getattr(ebay, "sandbox", False),
    }


def notification_channels(config: AppConfig) -> list[dict[str, Any]]:
    email = config.notifications.email
    tg = config.notifications.telegram
    email_missing = [
        label
        for label, value in (("smtp_host", email.smtp_host), ("username", email.username),
                             ("password", email.password), ("to_addrs", email.to_addrs))
        if not value
    ]
    tg_missing = [label for label, value in (("bot_token", tg.bot_token), ("chat_id", tg.chat_id)) if not value]
    return [
        {
            "name": "Email",
            "icon": "mail",
            "enabled": email.enabled,
            "details": [
                ("Получатели", ", ".join(mask_email(a) for a in email.to_addrs) or "—"),
                ("Отправитель", mask_email(email.from_addr or email.username) or "—"),
                ("SMTP", f"{email.smtp_host}:{email.smtp_port}" + (" (SSL)" if email.use_ssl else " (STARTTLS)")),
            ],
            "missing": email_missing if email.enabled else [],
        },
        {
            "name": "Telegram",
            "icon": "send",
            "enabled": tg.enabled,
            "details": [
                ("Chat ID", mask_secret(tg.chat_id) or "—"),
                ("Токен бота", "задан" if tg.bot_token else "не задан"),
            ],
            "missing": tg_missing if tg.enabled else [],
        },
    ]


def _error_body(request: Request, code: str, message: str) -> dict[str, Any]:
    """Middleware refusals: {"detail"} for the old API, plus {"error"} (the /api/v1 shape)."""
    body: dict[str, Any] = {"detail": message}
    if request.url.path.startswith("/api/v1"):
        body["error"] = {"code": code, "message_ru": message}
    return body


# =============================================================================
def create_app(
    config: AppConfig,
    db: Database,
    *,
    config_path: Path | None = None,
    monitor: Any | None = None,
    notifiers_factory: Callable[[], list[Any]] | None = None,
    start_monitor: bool = False,
    category_discovery: Callable[[str, int], Awaitable[Any]] | None = None,
    bind_host: str | None = None,
    access_token: str | None = None,
    ai_probe: Callable[[str], Any] | None = None,
) -> FastAPI:
    """Build the dashboard app. With `start_monitor` the monitor loop runs in the app lifespan.

    `category_discovery(location, radius_km)`: live category list for /setup, only on the
    "update from the site" button (default: 1–2 requests to kleinanzeigen.de, cached in
    data/categories.json for 7 days). `bind_host` (default web.host): when it is not a
    loopback address every request needs `access_token` (default: data/web_token.txt)
    and /api/docs is off. `ai_probe(url)`: JSON GET used to find LM Studio / Ollama."""
    from ..timefmt import apply_config as apply_timezone

    apply_timezone(config)
    loopback = is_loopback(bind_host if bind_host is not None else config.web.host)
    if access_token is None and not loopback:
        access_token = ensure_token(config.data_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        stop = asyncio.Event()
        app.state.stop_event = stop
        if start_monitor and monitor is not None and hasattr(monitor, "run_forever"):
            app.state.monitor_task = asyncio.create_task(
                _guarded(monitor.run_forever(stop), "monitor loop"), name="ebeyparser-monitor"
            )
        try:
            yield
        finally:
            stop.set()
            api = getattr(app.state, "api", None)
            if api is not None:  # end open SSE streams and running jobs (/api/v1)
                api.hub.close()
                api.jobs.cancel_all()
            await _stop_task(app.state.monitor_task, SHUTDOWN_TIMEOUT)
            app.state.monitor_task = None
            await _stop_task(app.state.run_task, 0)
            aclose = getattr(monitor, "aclose", None)
            if start_monitor and aclose is not None:
                with suppress(Exception):
                    await aclose()

    app = FastAPI(
        title="EbeyParser",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if loopback else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if loopback else None,
    )
    app.state.access_token = access_token or None
    app.state.config = config
    app.state.db = db
    app.state.config_path = Path(config_path) if config_path else None
    app.state.monitor = monitor
    app.state.notifiers_factory = notifiers_factory
    app.state.monitor_task = None
    app.state.run_task = None
    app.state.static_version = _static_version()
    app.state.category_cache = {}  # (location, radius) -> CategoryList

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    install_spa(app)  # new UI: /app assets, "/" and every other page path (see spa.py)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.filters.update(
        money=fmt_money,
        pct=fmt_percent,
        num=fmt_number,
        ago=fmt_ago,
        until=fmt_until,
        dt=fmt_datetime,
        duration=fmt_duration,
    )
    env.globals.update(
        plural=plural,
        icon=icon,
        VERDICT_LABELS=VERDICT_LABELS,
        STATUS_LABELS=STATUS_LABELS,
        PURPOSE_LABELS=PURPOSE_LABELS,
        SOURCE_LABELS=SOURCE_LABELS,
        COMP_SOURCE_LABELS=COMP_SOURCE_LABELS,
        SELLER_TYPE_LABELS=SELLER_TYPE_LABELS,
        BUYING_OPTION_LABELS=BUYING_OPTION_LABELS,
        EBAY_CONDITION_LABELS=EBAY_CONDITION_LABELS,
        PLACEHOLDER_IMG=PLACEHOLDER_IMG,
        app_version=__version__,
    )

    def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        state = monitor_state(app)
        kind, text = monitor_pill(state)
        base = {
            "config": app.state.config,
            "static_v": app.state.static_version,
            "monitor": state,
            "pill_kind": kind,
            "pill_text": text,
            "path": request.url.path,
        }
        return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def same_origin_guard(request: Request, call_next: Callable) -> Response:
        """Reject state-changing requests coming from other websites (CSRF)."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                allowed = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")}
                if urlsplit(origin).netloc not in allowed - {""}:
                    return JSONResponse(_error_body(request, "foreign_origin", "Запрос с чужого сайта отклонён"),
                                        status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def access_token_guard(request: Request, call_next: Callable) -> Response:
        """Network mode: every page and API call needs the token (cookie, header or ?token=)."""
        token: str | None = app.state.access_token
        path = request.url.path
        if not token or path.startswith(("/static/", "/app/")):
            return await call_next(request)
        if token_matches(request.query_params.get(TOKEN_PARAM), token):
            if request.method == "GET" and not path.startswith("/api/"):
                # remember the token in a cookie and drop it from the address bar
                clean = request.url.remove_query_params(TOKEN_PARAM)
                response: Response = RedirectResponse(clean.path + (f"?{clean.query}" if clean.query else ""), 303)
            else:
                response = await call_next(request)
            response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
            return response
        if token_matches(request.cookies.get(COOKIE_NAME), token) or token_matches(
                request.headers.get(TOKEN_HEADER), token):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse(_error_body(request, "unauthorized",
                                            "Нужен ключ доступа: открой ссылку с ?token=… из окна программы"),
                                status_code=401)
        return render(request, "error.html", {
            "code": 401,
            "title": "Нужен ключ доступа",
            "message": "Панель открыта для домашней сети, поэтому вход только по ссылке с ключом. "
                       "Скопируй адрес с ?token=… из окна программы при запуске "
                       "(ключ также лежит в файле data/web_token.txt) и открой его один раз — "
                       "браузер запомнит ключ.",
        }, 401)

    extra_hosts = getattr(config.web, "allowed_hosts", None) or []
    app.add_middleware(HostGuard,  # network mode: plain IP addresses pass too (the token guards)
                       allowed_hosts=trusted_hosts(bind_host if bind_host is not None else config.web.host,
                                                   extra_hosts),
                       allow_ip_literals=not loopback)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        if exc.status_code == 404 and wants_spa(request):
            return index_response()  # client-side route of the new UI (/deal/123, /settings/ai, …)
        wants_html = not path.startswith(("/api/", "/static/", "/app/")) and exc.status_code in (403, 404)
        if not wants_html:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        title = "Страница не найдена" if exc.status_code == 404 else "Доступ запрещён"
        message = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found", "Forbidden") else ""
        return render(request, "error.html", {"code": exc.status_code, "title": title, "message": message}, exc.status_code)

    # ================================================================= pages
    @app.get(CLASSIC_PREFIX, response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        params = request.query_params
        filters = DealFilters.from_params(params)
        page = max(1, _to_int(params.get("page"), 1))
        items, total = query_deals(db, filters, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        if page > pages and total:
            page = pages
            items, total = query_deals(db, filters, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        tab_counts = {
            key: (total if key == filters.verdict else query_deals(db, replace(filters, verdict=key), 0, 0)[1])
            for key, _ in VERDICT_TABS
        }
        stats = db.stats()
        runs = db.list_runs(limit=1)
        return render(request, "dashboard.html", {
            "nav": "deals",
            "filters": filters,
            "cards": [present_deal(d) for d in items],
            "total": total,
            "page": page,
            "pages": pages,
            "page_links": page_links(page, pages),
            "tabs": VERDICT_TABS,
            "tab_counts": tab_counts,
            "sorts": SORTS,
            "status_filters": STATUS_FILTERS,
            "search_names": db.search_names(),
            "stats": stats,
            "last_run": runs[0] if runs else None,
            "has_searches": bool(app.state.config.searches),
            "keyword_only": keyword_only(app.state.config),
            "editable": app.state.config_path is not None,
        })

    @app.get(CLASSIC_PREFIX + "/deal/{ad_id}", response_class=HTMLResponse)
    async def deal_page(request: Request, ad_id: str) -> HTMLResponse:
        deal = db.get_deal(ad_id)
        if deal is None:
            raise HTTPException(404, "Такого объявления нет в базе — возможно, его удалили или ссылка неверная.")
        ev = deal.evaluation
        search = app.state.config.search_by_name(deal.listing.search_name)
        comps = sorted(ev.estimate.comparables, key=lambda c: (not c.sold, c.price)) if ev else []
        return render(request, "deal.html", {
            "nav": "deals",
            "card": present_deal(deal),
            "deal": deal,
            "ev": ev,
            "breakdown": profit_breakdown(deal, app.state.config),
            "ai_local": ai_panel(ev.ai if ev else None, "Локальная модель"),
            "ai_second": ai_panel(getattr(ev, "ai_second", None) if ev else None, "Второе мнение"),
            "comparables": comps,
            "search": search,
            "statuses": [(s, STATUS_LABELS[s]) for s in DEAL_STATUSES],
            "ai_enabled": app.state.config.ai.enabled,
        })

    @app.get(CLASSIC_PREFIX + "/searches", response_class=HTMLResponse)
    async def searches_page(request: Request) -> HTMLResponse:
        return _render_searches(request)

    def _render_searches(
        request: Request,
        *,
        form_values: dict[str, Any] | None = None,
        errors: list[str] | None = None,
        editing: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        params = request.query_params
        cfg: AppConfig = app.state.config
        if form_values is None:
            editing = params.get("edit") or None
            existing = cfg.search_by_name(editing) if editing else None
            if editing and existing is None:
                editing = None
            form_values = search_form_values(existing)
        cards = []
        for s in cfg.searches:
            info = describe_search(s)
            info["deals"] = db.count_deals(search_name=s.name, include_ignored=True)
            info["buy"] = db.count_deals(search_name=s.name, verdict="buy")
            info["href"] = DealFilters(verdict="all", search=s.name).url()
            cards.append(info)
        notice = ""
        if params.get("saved"):
            notice = f"Поиск «{params['saved']}» сохранён"
        elif params.get("deleted"):
            notice = f"Поиск «{params['deleted']}» удалён"
        elif params.get("toggled"):
            notice = f"Поиск «{params['toggled']}» обновлён"
        elif params.get("setup"):
            count = _to_int(params.get("setup"), 0)
            notice = (f"Готово: сохранено {count} {plural(count, 'поиск', 'поиска', 'поисков')}. "
                      "Нажми «Проверить сейчас» на странице «Сделки» или подожди автоматической проверки")
        return render(request, "searches.html", {
            "nav": "searches",
            "cards": cards,
            "editable": app.state.config_path is not None,
            "config_path": app.state.config_path,
            "form": form_values,
            "errors": errors or [],
            "editing": editing,
            "form_open": bool(errors) or bool(editing) or params.get("new") == "1" or not cfg.searches,
            "notice": notice,
            "labels": SEARCH_FIELD_LABELS,
            "builtin_categories": builtin_categories(),
        }, status_code)

    @app.get(CLASSIC_PREFIX + "/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> HTMLResponse:
        cfg: AppConfig = app.state.config
        runs = db.list_runs(limit=30)
        second = getattr(cfg.ai, "second_opinion", None)
        return render(request, "status.html", {
            "nav": "status",
            "runs": runs,
            "ai": await ai_status(app),
            "second": second,
            "ebay": _mask_ebay(cfg),
            "channels": notification_channels(cfg),
            "has_notifiers": notifiers_factory is not None,
            "stats": db.stats(),
            "searches_enabled": sum(1 for s in cfg.searches if s.enabled),
            "http": await http_limits(app),
            "backlog": await backlog_info(app),
            "cooldown_steps": list(getattr(cfg.general, "block_cooldown_hours", []) or []),
            "hourly_cap": getattr(cfg.general, "max_requests_per_hour", None),
            "db_path": getattr(db, "path", ""),
        })

    # ------------------------------------------------------------ setup wizard
    async def _default_discovery(location: str, radius_km: int) -> CategoryList:
        from ..runtime import http_state_path
        from ..scraper.categories import discover_categories, discovery_client

        cfg: AppConfig = app.state.config
        client = discovery_client(cfg.general, state_path=http_state_path(cfg.data_path))
        try:
            return await discover_categories(client, location, radius_km)
        finally:
            await client.aclose()

    async def _categories_for(location: str, radius_km: int, *, live: bool = False) -> CategoryList:
        """No requests by default: the cached live list (memory, then data/categories.json,
        7 days) or the built-in one. `live=True` asks the site and caches a good answer."""
        location = location.strip()
        if not location:
            return builtin_categories()
        key = (location.lower(), radius_km)
        cache: dict[tuple[str, int], CategoryList] = app.state.category_cache
        data_dir = app.state.config.data_path
        if not live:
            if key in cache:
                return cache[key]
            cached = load_cached_categories(data_dir, location, radius_km)
            if cached is not None:
                cache[key] = cached
                return cached
            return builtin_categories()
        discover = category_discovery or _default_discovery
        try:
            result = await asyncio.wait_for(discover(location, radius_km), DISCOVERY_TIMEOUT)
            if not isinstance(result, CategoryList):
                result = CategoryList(result or builtin_categories())
            if not len(result):
                result = CategoryList(builtin_categories(), error=result.error or "список категорий пуст")
        except asyncio.TimeoutError:
            result = CategoryList(builtin_categories(), error=f"kleinanzeigen.de не ответил за {DISCOVERY_TIMEOUT:g} с")
        except Exception as exc:  # discovery must never break the page
            log.warning("category discovery failed: %s", exc)
            result = CategoryList(builtin_categories(), error=describe_error(exc))
        if result.live:
            cache[key] = result
            save_cached_categories(data_dir, location, radius_km, result)
        return result

    def _render_setup(
        request: Request,
        answers: SetupAnswers,
        values: dict[str, Any],
        categories: CategoryList,
        *,
        errors: list[str] | None = None,
        status_code: int = 200,
        refreshed: bool = False,
    ) -> HTMLResponse:
        cfg: AppConfig = app.state.config
        rows = list(values["wishlist"])
        rows += [("", "")] * max(1, SETUP_MIN_WISH_ROWS - len(rows))
        suggested = answers.suggested_interval(cfg.general)
        interval = _to_float(values.get("interval_minutes")) or float(suggested)
        estimate = answers.estimate(interval, cfg.general)
        scan_pages, keyword_pages, extra, cap = request_budgets(cfg.general)
        return render(request, "setup.html", {
            "nav": "setup",
            "form": values,
            "rows": rows,
            "groups": setup_category_groups(list(categories), answers.category_ids),
            "categories": categories,
            "refreshed": refreshed,
            "radius_choices": sorted({*RADIUS_CHOICES, answers.radius_km}),
            "errors": errors or [],
            "editable": app.state.config_path is not None,
            "config_path": app.state.config_path,
            "existing_count": len(cfg.searches),
            "default_min_profit": cfg.pricing.min_profit,
            "default_budget": DEFAULT_BUDGET,
            "suggested_interval": suggested,
            "current_interval": cfg.general.interval_minutes,
            "estimate": estimate,
            "budget": {"scan_pages": scan_pages, "keyword_pages": keyword_pages, "extra": extra, "cap": cap,
                       "share": RESULT_PAGES_SHARE},
        }, status_code)

    @app.get(CLASSIC_PREFIX + "/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> HTMLResponse:
        params = request.query_params
        cfg: AppConfig = app.state.config
        if "location" in params:  # "update categories" re-submits the form as GET: keep what was typed
            answers, values, _ = parse_setup_form(params, params.getlist, default_min_profit=cfg.pricing.min_profit)
        else:
            answers = answers_from_searches(cfg.searches, cfg.pricing.min_profit)
            if answers.max_price is None and not cfg.searches:
                answers.max_price = DEFAULT_BUDGET
            values = setup_form_values(answers, replace=True)
            if cfg.searches:  # keep the user's interval unless it is too aggressive
                values["interval_minutes"] = _num_text(max(cfg.general.interval_minutes,
                                                           answers.suggested_interval(cfg.general)))
        live = bool(params.get("refresh"))
        categories = await _categories_for(answers.location, answers.radius_km, live=live)
        return _render_setup(request, answers, values, categories, refreshed=live)

    @app.post(CLASSIC_PREFIX + "/setup", response_class=HTMLResponse)
    async def setup_save(request: Request) -> Response:
        path = _require_editable()
        cfg: AppConfig = app.state.config
        form = await request.form()
        answers, values, errors = parse_setup_form(form, form.getlist, default_min_profit=cfg.pricing.min_profit)
        categories = await _categories_for(answers.location, answers.radius_km)
        known = {c.id: c for c in categories}
        for cid in answers.category_ids:  # categories shown on the page but not cached any more
            if cid not in known and builtin_by_id(cid) is None:
                name = " ".join(str(form.get(f"cat_name_{cid}") or "").split())[:80] or f"Категория {cid}"
                known[cid] = Category(cid, name)
        generated: list[SearchConfig] = []
        if not errors:
            generated = searches_from_answers(answers, known.values(), global_min_profit=cfg.pricing.min_profit)
            if not generated:
                errors.append("Выбери хотя бы одну категорию или добавь вещь в список «для себя»")
            for search in generated:
                errors.extend(search_problems(search))
        if errors:
            return _render_setup(request, answers, values, categories, errors=errors, status_code=400)
        merged = merge_searches(cfg.searches, generated, replace_all=bool(values["replace"]))
        if path.is_file():
            try:
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
            except OSError as exc:
                log.warning("backup of %s failed: %s", path, exc)
        interval = answers.interval_minutes or float(answers.suggested_interval(cfg.general))
        if interval != cfg.general.interval_minutes:
            _write_settings({"general.interval_minutes": interval})
        _persist(merged)
        return RedirectResponse(f"{CLASSIC_PREFIX}/searches?" + urlencode({"setup": len(generated)}), status_code=303)

    # ---------------------------------------------------------------- settings
    def _write_settings(updates: dict[str, Any], env: dict[str, str] | None = None) -> None:
        """Write config values (comment-preserving) / secrets to .env, then reload the
        non-search sections into the running config and tell the monitor."""
        path = _require_editable()
        try:
            if env:
                write_env_values(path.parent / ".env", env)
            if updates:
                update_yaml_values(path, updates)
            fresh = load_config(path)
        except (OSError, ConfigError) as exc:
            raise HTTPException(500, f"Не удалось записать {path}: {exc}") from exc
        cfg: AppConfig = app.state.config
        for section in ("general", "pricing", "ai", "notifications", "ebay", "web"):
            if hasattr(fresh, section):
                setattr(cfg, section, getattr(fresh, section))
        update = getattr(app.state.monitor, "update_config", None)
        if update is not None:
            try:
                update(cfg)
            except Exception:
                log.exception("monitor.update_config failed")

    def _render_settings(request: Request, *, errors: list[str] | None = None, status_code: int = 200,
                         form: dict[str, Any] | None = None) -> HTMLResponse:
        cfg: AppConfig = app.state.config
        path = app.state.config_path
        env = read_env_file(path.parent / ".env") if path else {}
        tg = cfg.notifications.telegram
        saved = request.query_params.get("saved", "")
        notices = {"ai": "Настройки нейросети сохранены", "telegram": "Telegram сохранён",
                   "pricing": "Настройки выгоды сохранены"}
        pricing = cfg.pricing
        values = {
            "ai_enabled": cfg.ai.enabled, "provider": cfg.ai.provider, "base_url": cfg.ai.base_url,
            "model": cfg.ai.model,
            "tg_enabled": tg.enabled, "chat_id": env.get("TELEGRAM_CHAT_ID") or tg.chat_id,
            "min_profit": _num_text(pricing.min_profit), "min_roi": _num_text(round(pricing.min_roi * 100, 1)),
            "max_capital": _num_text(getattr(pricing, "max_capital", None)),
            "vb_discount": _num_text(round(float(getattr(pricing, "vb_expected_discount", 0.10) or 0) * 100, 1)),
            **(form or {}),
        }
        return render(request, "settings.html", {
            "nav": "setup",
            "v": values,
            "errors": errors or [],
            "notice": notices.get(saved, ""),
            "editable": path is not None,
            "config_path": path,
            "token_set": bool(tg.bot_token),
            "has_notifiers": notifiers_factory is not None,
            "has_monitor": getattr(app.state.monitor, "ai_health", None) is not None,
            "has_vb": hasattr(pricing, "vb_expected_discount"),
            "has_capital": hasattr(pricing, "max_capital"),
        }, status_code)

    @app.get(CLASSIC_PREFIX + "/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        return _render_settings(request)

    @app.post(CLASSIC_PREFIX + "/settings/ai", response_class=HTMLResponse)
    async def settings_ai(request: Request) -> Response:
        _require_editable()
        form = await request.form()
        provider = str(form.get("provider") or "openai")
        base_url = str(form.get("base_url") or "").strip()
        model = str(form.get("model") or "").strip()
        errors = []
        if provider not in ("openai", "ollama"):
            errors.append("Неизвестный сервер нейросети")
        if base_url and urlsplit(base_url).scheme not in ("http", "https"):
            errors.append("Адрес сервера должен начинаться с http://, например http://localhost:1234/v1")
        if not model:
            errors.append("Укажи модель, например qwen/qwen2.5-vl-7b")
        if errors:
            return _render_settings(request, errors=errors, status_code=400, form={
                "provider": provider, "base_url": base_url, "model": model, "ai_enabled": bool(form.get("enabled"))})
        if not base_url:
            base_url = LMSTUDIO_URL if provider == "openai" else OLLAMA_URL
        _write_settings({"ai.enabled": bool(form.get("enabled")), "ai.provider": provider,
                         "ai.base_url": base_url, "ai.model": model})
        return RedirectResponse(f"{CLASSIC_PREFIX}/settings?saved=ai#ai", status_code=303)

    @app.post(CLASSIC_PREFIX + "/settings/telegram", response_class=HTMLResponse)
    async def settings_telegram(request: Request) -> Response:
        path = _require_editable()
        form = await request.form()
        token = str(form.get("bot_token") or "").strip()
        chat_id = str(form.get("chat_id") or "").strip()
        enabled = bool(form.get("enabled"))
        errors = []
        if token and not re.match(r"^\d{5,}:[\w-]{20,}$", token):
            errors.append("Токен бота выглядит как 123456789:AAH… — скопируй его из сообщения @BotFather целиком")
        if chat_id and not re.match(r"^(-?\d{3,}|@\w{4,})$", chat_id):
            errors.append("chat_id — это число, например 123456789 (его пишет @userinfobot)")
        has_token = bool(token or app.state.config.notifications.telegram.bot_token)
        if enabled and not (has_token and (chat_id or app.state.config.notifications.telegram.chat_id)):
            errors.append("Чтобы включить Telegram, нужны и токен бота, и chat_id")
        if errors:
            return _render_settings(request, errors=errors, status_code=400,
                                    form={"tg_enabled": enabled, "chat_id": chat_id})
        env = {}
        if token:
            env["TELEGRAM_BOT_TOKEN"] = token
        if chat_id:
            env["TELEGRAM_CHAT_ID"] = chat_id
        updates: dict[str, Any] = {"notifications.telegram.enabled": enabled}
        if env:  # config references the secrets, the values live in .env
            updates["notifications.telegram.bot_token"] = "${TELEGRAM_BOT_TOKEN}"
            updates["notifications.telegram.chat_id"] = "${TELEGRAM_CHAT_ID}"
        _write_settings(updates, env)
        log.info("Telegram settings saved (%s)", path.parent / ".env")
        return RedirectResponse(f"{CLASSIC_PREFIX}/settings?saved=telegram#telegram", status_code=303)

    @app.post(CLASSIC_PREFIX + "/settings/pricing", response_class=HTMLResponse)
    async def settings_pricing(request: Request) -> Response:
        _require_editable()
        form = await request.form()
        pricing = app.state.config.pricing
        errors: list[str] = []
        raw = {k: str(form.get(k) or "").strip() for k in ("min_profit", "min_roi", "max_capital", "vb_discount")}

        def number(key: str, label: str, *, low: float = 0, high: float | None = None) -> float | None:
            if not raw[key]:
                return None
            value = _to_float(raw[key].replace("€", "").replace("%", ""))
            if value is None or value < low or (high is not None and value > high):
                errors.append(f"«{label}»: нужно число" + (f" от {low:g} до {high:g}" if high is not None else ""))
                return None
            return value

        min_profit = number("min_profit", "Минимальная прибыль")
        min_roi = number("min_roi", "Минимальный ROI", high=1000)
        max_capital = number("max_capital", "Максимум на одну покупку")
        vb = number("vb_discount", "Скидка при торге (VB)", high=90)
        if errors:
            return _render_settings(request, errors=errors, status_code=400, form=raw)
        updates: dict[str, Any] = {
            "pricing.min_profit": min_profit if min_profit is not None else pricing.min_profit,
            "pricing.min_roi": round(min_roi / 100, 4) if min_roi is not None else pricing.min_roi,
        }
        if hasattr(pricing, "max_capital"):
            updates["pricing.max_capital"] = max_capital  # empty = no limit (null)
        if hasattr(pricing, "vb_expected_discount") and vb is not None:
            updates["pricing.vb_expected_discount"] = round(vb / 100, 4)
        _write_settings(updates)
        return RedirectResponse(f"{CLASSIC_PREFIX}/settings?saved=pricing#pricing", status_code=303)

    @app.get("/api/ai/check")
    async def api_ai_check() -> dict[str, Any]:
        return await ai_status(app)

    @app.get("/api/ai/detect")
    async def api_ai_detect() -> dict[str, Any]:
        servers = await asyncio.to_thread(detect_local_ai, ai_probe or probe_json)
        return {"servers": [s.as_dict() for s in servers]}

    # --------------------------------------------------- search form actions
    def _require_editable() -> Path:
        path = app.state.config_path
        if path is None:
            raise HTTPException(403, "Поиски сейчас только для чтения — перезапусти программу обычным способом")
        return path

    def _persist(searches: list[SearchConfig]) -> None:
        path = _require_editable()
        try:
            save_searches_block(path, searches)  # only the searches block: comments elsewhere survive
        except OSError as exc:
            raise HTTPException(500, f"Не удалось записать {path}: {exc}") from exc
        cfg: AppConfig = app.state.config
        cfg.searches = searches
        update = getattr(app.state.monitor, "update_config", None)
        if update is not None:
            try:
                update(cfg)
            except Exception:
                log.exception("monitor.update_config failed")

    def _upsert_search(original: str | None, search: SearchConfig) -> list[str]:
        """Create (original=None) or replace a search. Returns errors (conflicts)."""
        current = list(app.state.config.searches)
        names = [s.name for s in current]
        if original is not None and original not in names:
            return [f"Поиск «{original}» не найден"]
        if search.name in names and search.name != original:
            return [f"Поиск с названием «{search.name}» уже есть"]
        if original is None:
            current.append(search)
        else:
            current[names.index(original)] = search
        _persist(current)
        return []

    @app.post(CLASSIC_PREFIX + "/searches/save", response_class=HTMLResponse)
    async def searches_save(request: Request) -> Response:
        _require_editable()
        form = await request.form()
        original = str(form.get("original_name") or "").strip() or None
        values, search, errors = parse_search_form(form, form.getlist)
        if search is not None:
            errors = _upsert_search(original, search)
        if errors or search is None:
            return _render_searches(request, form_values=values, errors=errors, editing=original, status_code=400)
        return RedirectResponse(f"{CLASSIC_PREFIX}/searches?" + urlencode({"saved": search.name}), status_code=303)

    @app.post(CLASSIC_PREFIX + "/searches/delete")
    async def searches_delete(request: Request) -> Response:
        _require_editable()
        name = str((await request.form()).get("name") or "")
        current = list(app.state.config.searches)
        remaining = [s for s in current if s.name != name]
        if len(remaining) == len(current):
            raise HTTPException(404, f"Поиск «{name}» не найден")
        _persist(remaining)
        return RedirectResponse(f"{CLASSIC_PREFIX}/searches?" + urlencode({"deleted": name}), status_code=303)

    @app.post(CLASSIC_PREFIX + "/searches/toggle")
    async def searches_toggle(request: Request) -> Response:
        _require_editable()
        name = str((await request.form()).get("name") or "")
        current = list(app.state.config.searches)
        for i, s in enumerate(current):
            if s.name == name:
                current[i] = s.model_copy(update={"enabled": not s.enabled})
                break
        else:
            raise HTTPException(404, f"Поиск «{name}» не найден")
        _persist(current)
        return RedirectResponse(f"{CLASSIC_PREFIX}/searches?" + urlencode({"toggled": name}), status_code=303)

    # =================================================================== API
    @app.get("/api/deals")
    async def api_deals(request: Request) -> dict[str, Any]:
        params = request.query_params
        filters = DealFilters.from_params(params)
        limit = max(0, min(API_MAX_LIMIT, _to_int(params.get("limit"), 50)))
        offset = max(0, _to_int(params.get("offset"), 0))
        items, total = query_deals(db, filters, limit, offset)
        return {
            "items": [d.model_dump(mode="json") for d in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/deals/{ad_id}")
    async def api_deal(ad_id: str) -> dict[str, Any]:
        deal = db.get_deal(ad_id)
        if deal is None:
            raise HTTPException(404, "Объявление не найдено")
        return deal.model_dump(mode="json")

    @app.post("/api/deals/{ad_id}/status")
    async def api_set_status(ad_id: str, body: StatusUpdate) -> dict[str, Any]:
        if body.status not in DEAL_STATUSES:
            raise HTTPException(400, f"Неизвестный статус «{body.status}». Допустимо: {', '.join(DEAL_STATUSES)}")
        if db.get_listing(ad_id) is None:
            raise HTTPException(404, "Объявление не найдено")
        note = body.note[:5000] if body.note is not None else None
        db.set_status(ad_id, body.status, note)
        deal = db.get_deal(ad_id)
        assert deal is not None
        return deal.model_dump(mode="json")

    @app.get("/api/stats")
    async def api_stats() -> dict[str, Any]:
        return db.stats()

    @app.get("/api/runs")
    async def api_runs(request: Request) -> list[dict[str, Any]]:
        limit = max(1, min(200, _to_int(request.query_params.get("limit"), 20)))
        return [r.model_dump(mode="json") for r in db.list_runs(limit=limit)]

    @app.post("/api/run")
    async def api_run() -> JSONResponse:
        mon = app.state.monitor
        if mon is None or not hasattr(mon, "run_once"):
            raise HTTPException(503, "Проверки сейчас недоступны: программа запущена без фоновых проверок. "
                                     "Перезапусти программу")
        if _monitor_running(app):
            raise HTTPException(409, "Проверка уже идёт — дождись её окончания")
        app.state.run_task = asyncio.create_task(_guarded(mon.run_once(), "manual run"), name="ebeyparser-run-once")
        return JSONResponse({"started": True}, status_code=202)

    @app.get("/api/health")
    async def api_health(request: Request) -> dict[str, Any]:
        check_ai = request.query_params.get("ai", "1").lower() not in ("0", "false", "no")
        state = monitor_state(app)
        kind, text = monitor_pill(state)
        return {
            "ok": True,
            "version": __version__,
            "monitor": {**state, "pill": {"kind": kind, "text": text}},
            "ai": await ai_status(app) if check_ai else None,
        }

    @app.post("/api/notify/test")
    async def api_notify_test() -> dict[str, Any]:
        from ..demo import demo_deals

        notifiers: list[Any] = []
        if notifiers_factory is not None:
            try:
                notifiers = list(notifiers_factory() or [])
            except Exception as exc:
                raise HTTPException(500, f"Не удалось создать каналы уведомлений: {exc}") from exc
        if not notifiers:
            raise HTTPException(400, NO_CHANNELS_MESSAGE)
        best = sorted(demo_deals(), key=lambda pair: pair[1].score, reverse=True)[:2]
        deals = [
            # demo images are data: URIs, which mail/Telegram clients reject — send without them
            DealView(
                listing=listing.model_copy(update={"image_urls": [u for u in listing.image_urls if not u.startswith("data:")]}),
                evaluation=evaluation,
            )
            for listing, evaluation in best
        ]
        results: dict[str, str] = {}
        for notifier in notifiers:
            name = str(getattr(notifier, "name", "") or type(notifier).__name__)
            try:
                await asyncio.wait_for(notifier.send(deals, title="EbeyParser: тестовое уведомление"), NOTIFY_TIMEOUT)
                results[name] = "ok"
            except asyncio.TimeoutError:
                results[name] = f"нет ответа за {NOTIFY_TIMEOUT:g} с"
            except Exception as exc:
                results[name] = str(exc) or type(exc).__name__
        return {"results": results}

    # ---------------------------------------------------------- searches API
    @app.get("/api/searches")
    async def api_searches() -> dict[str, Any]:
        return {
            "items": [s.model_dump(mode="json") for s in app.state.config.searches],
            "editable": app.state.config_path is not None,
        }

    def _check_api_search(search: SearchConfig) -> SearchConfig:
        search = search.model_copy(update={"name": search.name.strip()})
        problems = search_problems(search)
        if problems:
            raise HTTPException(400, "; ".join(problems))
        return search

    @app.post("/api/searches", status_code=201)
    async def api_create_search(search: SearchConfig) -> dict[str, Any]:
        _require_editable()
        search = _check_api_search(search)
        if app.state.config.search_by_name(search.name) is not None:
            raise HTTPException(409, f"Поиск с названием «{search.name}» уже есть")
        _upsert_search(None, search)
        return search.model_dump(mode="json")

    @app.put("/api/searches/{name:path}")
    async def api_update_search(name: str, search: SearchConfig) -> dict[str, Any]:
        _require_editable()
        search = _check_api_search(search)
        if app.state.config.search_by_name(name) is None:
            raise HTTPException(404, f"Поиск «{name}» не найден")
        if search.name != name and app.state.config.search_by_name(search.name) is not None:
            raise HTTPException(409, f"Поиск с названием «{search.name}» уже есть")
        _upsert_search(name, search)
        return search.model_dump(mode="json")

    @app.delete("/api/searches/{name:path}")
    async def api_delete_search(name: str) -> dict[str, Any]:
        _require_editable()
        current = list(app.state.config.searches)
        remaining = [s for s in current if s.name != name]
        if len(remaining) == len(current):
            raise HTTPException(404, f"Поиск «{name}» не найден")
        _persist(remaining)
        return {"deleted": name}

    # ------------------------------------------------------------ JSON API v1 (SPA)
    from .api import mount_api_v1

    mount_api_v1(app, category_discovery=category_discovery, ai_probe=ai_probe,
                 bind_host=bind_host if bind_host is not None else config.web.host)
    return app
