"""Web dashboard (FastAPI + Jinja2): deals, searches, status and a small JSON API.

The monitor and the notifiers are injected (duck-typed) so this module does not
depend on the scraper / AI / notify packages.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Mapping
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
from ..config import AppConfig, SearchConfig, save_searches
from ..db import Database
from ..models import DEAL_STATUSES, AIVerdict, DealView, RunSummary, utcnow

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
    "other": "Другое",
}
PRICE_SOURCE_LABELS = {
    "reference": "справочная цена из конфига",
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
    "Не настроен ни один канал уведомлений. Включи email или Telegram "
    "в config.yaml (раздел notifications) и перезапусти приложение."
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
        return "/?" + urlencode(params) if params else "/"

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
        href=f"/deal/{quote(listing.ad_id, safe='')}",
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


# =============================================================================
def create_app(
    config: AppConfig,
    db: Database,
    *,
    config_path: Path | None = None,
    monitor: Any | None = None,
    notifiers_factory: Callable[[], list[Any]] | None = None,
    start_monitor: bool = False,
) -> FastAPI:
    """Build the dashboard app. With `start_monitor` the monitor loop runs in the app lifespan."""

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
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.config = config
    app.state.db = db
    app.state.config_path = Path(config_path) if config_path else None
    app.state.monitor = monitor
    app.state.notifiers_factory = notifiers_factory
    app.state.monitor_task = None
    app.state.run_task = None
    app.state.static_version = _static_version()

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
                    return JSONResponse({"detail": "Запрос с чужого сайта отклонён"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        wants_html = not path.startswith(("/api/", "/static/")) and exc.status_code in (403, 404)
        if not wants_html:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        title = "Страница не найдена" if exc.status_code == 404 else "Доступ запрещён"
        message = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found", "Forbidden") else ""
        return render(request, "error.html", {"code": exc.status_code, "title": title, "message": message}, exc.status_code)

    # ================================================================= pages
    @app.get("/", response_class=HTMLResponse)
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
        })

    @app.get("/deal/{ad_id}", response_class=HTMLResponse)
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

    @app.get("/searches", response_class=HTMLResponse)
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
        }, status_code)

    @app.get("/status", response_class=HTMLResponse)
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
            "db_path": getattr(db, "path", ""),
        })

    # --------------------------------------------------- search form actions
    def _require_editable() -> Path:
        path = app.state.config_path
        if path is None:
            raise HTTPException(403, "Поиски только для чтения: приложение запущено без файла config.yaml")
        return path

    def _persist(searches: list[SearchConfig]) -> None:
        path = _require_editable()
        try:
            save_searches(path, searches)
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

    @app.post("/searches/save", response_class=HTMLResponse)
    async def searches_save(request: Request) -> Response:
        _require_editable()
        form = await request.form()
        original = str(form.get("original_name") or "").strip() or None
        values, search, errors = parse_search_form(form, form.getlist)
        if search is not None:
            errors = _upsert_search(original, search)
        if errors or search is None:
            return _render_searches(request, form_values=values, errors=errors, editing=original, status_code=400)
        return RedirectResponse("/searches?" + urlencode({"saved": search.name}), status_code=303)

    @app.post("/searches/delete")
    async def searches_delete(request: Request) -> Response:
        _require_editable()
        name = str((await request.form()).get("name") or "")
        current = list(app.state.config.searches)
        remaining = [s for s in current if s.name != name]
        if len(remaining) == len(current):
            raise HTTPException(404, f"Поиск «{name}» не найден")
        _persist(remaining)
        return RedirectResponse("/searches?" + urlencode({"deleted": name}), status_code=303)

    @app.post("/searches/toggle")
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
        return RedirectResponse("/searches?" + urlencode({"toggled": name}), status_code=303)

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
            raise HTTPException(503, "Монитор не подключён — запусти приложение командой `ebeyparser run`")
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

    return app
