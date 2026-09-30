"""Turn deals into human-readable Russian messages.

Three output formats share one data extraction step (`_collect`):
plain text (e-mail alternative part), e-mail HTML (inline CSS, table layout
that survives Gmail) and Telegram HTML (photo caption, <= 1024 chars).
"""

from __future__ import annotations

import html
import ipaddress
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote, urlsplit

from ..models import AIVerdict, DealView, Evaluation, Listing, PriceEstimate

TELEGRAM_CAPTION_LIMIT = 1024
MAX_EMAIL_DEALS = 30  # keep digest e-mails below Gmail's ~100 KB clipping limit
FOOTER_TEXT = "EbeyParser · бот-охотник за выгодными объявлениями"

_VERDICT_LABELS = {"buy": "Покупать", "maybe": "Подумать", "skip": "Пропустить"}
_VERDICT_EMOJI = {"buy": "🟢", "maybe": "🟡", "skip": "⚪"}
_SOURCE_NAMES = {"kleinanzeigen": "Kleinanzeigen", "ebay": "eBay"}
UNCHECKED_WARNING = "⚠ ФОТО НЕ ПРОВЕРЕНЫ ИИ — проверь сам"
_ESTIMATE_SOURCES = {
    "reference": "справочная цена",
    "history": "история цен",
    "kleinanzeigen": "Kleinanzeigen",
    "ebay_sold": "продажи eBay",
    "mixed": "Kleinanzeigen + eBay",
    "ai": "оценка ИИ",
    "none": "",
}
_CONDITION_LABELS = {
    "new": "новое",
    "like_new": "как новое",
    "good": "хорошее",
    "used": "б/у",
    "defective": "неисправно",
}
_WS_RE = re.compile(r"\s+")


def _now() -> datetime:
    """Current UTC time (patched in tests)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return Decimal(str(v))


def _group(n: int) -> str:
    return f"{n:,}".replace(",", ".")  # German thousands separator


def format_money(value: float | None, *, russian: bool = False) -> str:
    """German-style euro amount: 1234.5 -> "1.235 €", 7.5 -> "7,50 €", None -> "—".
    russian=True: the web app's format (lib/format.js money()): "1 235 €" with non-breaking
    spaces and "−" for negatives (format_money_ru).

    Whole numbers and amounts >= 100 € are shown without cents."""
    d = _to_decimal(value)
    if d is None:
        return "—"
    sep, space, minus = (" ", " ", "−") if russian else (".", " ", "-")
    cents = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(cents) >= 100 or cents == cents.to_integral_value():
        q = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        body = _group(abs(int(q))).replace(".", sep)
    else:
        q = cents
        whole, frac = f"{abs(q):.2f}".split(".")
        body = f"{_group(int(whole)).replace('.', sep)},{frac}"
    return f"{minus if q < 0 else ''}{body}{space}€"


def format_money_ru(value: float | None) -> str:
    """Russian money for API texts the web app shows as is: 1065 -> "1 065 €" (non-breaking spaces)."""
    return format_money(value, russian=True)


def format_percent(value: float | None) -> str:
    """Fraction as whole percent: 0.643 -> "64 %", None -> "—"."""
    d = _to_decimal(value)
    if d is None:
        return "—"
    q = (d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(q)} %"


def format_km(value: float | None) -> str:
    d = _to_decimal(value)
    if d is None:
        return ""
    if abs(d) < 10:
        q = d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        s = f"{q:.1f}".replace(".", ",").removesuffix(",0")
    else:
        s = str(int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    return f"{s} км"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form for n: (1 предложение, 2 предложения, 5 предложений)."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def deals_count_phrase(n: int) -> str:
    """"1 выгодное предложение", "3 выгодных предложения", "5 выгодных предложений"."""
    return f"{n} " + plural_ru(n, "выгодное предложение", "выгодных предложения", "выгодных предложений")


def more_deals_phrase(n: int) -> str:
    return f"…и ещё {n} " + plural_ru(n, "предложение", "предложения", "предложений")


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def format_time_left(ends_at: datetime, now: datetime | None = None) -> str:
    """"45 мин", "2 ч 15 мин", "3 дня 4 ч"; "" if already over."""
    seconds = (_utc(ends_at) - _utc(now or _now())).total_seconds()
    if seconds <= 0:
        return ""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {mins} мин" if mins else f"{hours} ч"
    days, hours = divmod(hours, 24)
    out = f"{days} " + plural_ru(days, "день", "дня", "дней")
    return f"{out} {hours} ч" if hours else out


def _format_end_clock(ends_at: datetime, now: datetime) -> str:
    """Auction end in the user's time zone (general.timezone, not the host's):
    "сегодня в 21:30" / "завтра в 09:05" / "28.09 в 21:30"."""
    from ..timefmt import to_local

    end = to_local(_utc(ends_at))
    today = to_local(_utc(now)).date()
    if end.date() == today:
        day = "сегодня"
    elif end.date() == today + timedelta(days=1):
        day = "завтра"
    else:
        day = end.strftime("%d.%m")
    return f"{day} в {end:%H:%M}"


def verdict_label(verdict: str) -> str:
    """buy -> "Покупать", maybe -> "Подумать", skip -> "Пропустить"."""
    return _VERDICT_LABELS.get(verdict, verdict)


def source_name(source: str) -> str:
    return _SOURCE_NAMES.get(source, source or "Kleinanzeigen")


def _sources_phrase(deals: list[DealView]) -> str:
    names = []
    for d in deals:
        name = source_name(d.listing.source)
        if name not in names:
            names.append(name)
    return " и ".join(sorted(names, key=lambda n: n != "Kleinanzeigen")) or "Kleinanzeigen"


def default_title(deals: list[DealView]) -> str:
    if not deals:
        return "EbeyParser: новых предложений нет"
    return f"Найдено {deals_count_phrase(len(deals))}"


def _squash(text: str | None) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _clip(text: str | None, limit: int) -> str:
    """Collapse whitespace and cut to `limit` chars with an ellipsis."""
    s = _squash(text)
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    return s[: max(limit - 1, 0)].rstrip() + "…"


def safe_url(url: str | None) -> str | None:
    """Only absolute http(s) URLs are linked; anything else (javascript:, junk) is dropped."""
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    if any(c in url for c in ' \n\r\t"<>'):
        return None
    return url


def is_local_url(url: str | None) -> bool:
    """True for URLs that only work on this machine (localhost, 127.0.0.1, bare
    host names). Telegram rejects such URLs in inline buttons."""
    if not url:
        return True
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    if host in ("localhost", "0.0.0.0") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # e.g. "http://myserver:8000"
    return ip.is_loopback or ip.is_unspecified or ip.is_link_local


def deal_web_url(deal: DealView, web_base_url: str | None) -> str | None:
    """Link to the deal page of the EbeyParser web UI."""
    if not web_base_url or not web_base_url.strip():
        return None
    base = web_base_url.strip().rstrip("/")
    return safe_url(f"{base}/deal/{quote(deal.listing.ad_id, safe='')}")


def open_link_text(deal: DealView) -> str:
    return "Открыть на eBay" if deal.listing.source == "ebay" else "Открыть объявление"


# --------------------------------------------------------------------------- #
# Data extraction shared by all renderers
# --------------------------------------------------------------------------- #


@dataclass
class _DealInfo:
    title: str
    source: str  # "Kleinanzeigen" / "eBay"
    url: str | None
    open_text: str
    web_url: str | None
    photo: str | None
    is_free: bool
    price_main: str  # "450 €" / "бесплатно" / "цена не указана"
    price_short: str  # "450 € VB"
    price_full: str  # "450 € VB (торг)"
    negotiable_text: str = ""  # "VB (торг)" / "Preisvorschlag (торг)"
    shipping_cost: str = ""  # "доставка 5,99 €" / "бесплатная доставка"
    auction: bool = False
    auction_line: str = ""  # "7 ставок · заканчивается через 1 ч 40 мин (сегодня в 21:30)"
    auction_soon: bool = False  # ends within an hour
    purpose: str = "resale"
    market_value: float | None = None
    market_note: str = ""  # "продажи eBay, выборка: 14"
    market_range: str = ""  # "470–560 €"
    profit_label: str = "Прибыль"
    profit: float | None = None
    profit_extra: str = ""  # "ROI 32 %" / "на 32 % дешевле рынка"
    max_buy: str = ""  # "Выгодно до 450 €" / "Максимальная ставка 450 €"
    costs: str = ""  # "комиссии 8 € + доставка 10 €"
    score: int | None = None
    verdict: str | None = None
    location: str = ""  # "10115 Mitte · 8 км"
    posted: str = ""
    shipping: str = ""
    seller: str = ""
    condition: str = ""  # seller-declared, e.g. "Gebraucht"
    ai: bool = False
    ai_product: str = ""
    ai_condition: str = ""
    ai_photo: str = ""  # "✅ фото соответствует описанию"
    ai_confidence: str = ""
    ai_reasoning: str = ""
    second: str = ""  # "Второе мнение (claude-opus-5): покупать, 80 %"
    second_reasoning: str = ""
    red_flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    haggle: str = ""  # "Торгуйся: предложи 400 € → прибыль ≈ 120 €" (replaces the asking-price profit)
    offer_money: str = ""  # "400 €"
    offer_profit: float | None = None  # profit / savings at the suggested offer
    unchecked: bool = False  # AI was down: photos NOT checked
    extras: list[str] = field(default_factory=list)  # lines other features add (notify.extras), e.g. a build project

    @property
    def market_str(self) -> str:
        return f"≈ {format_money(self.market_value)}" if self.market_value is not None else ""

    @property
    def market_details(self) -> str:
        rng = f"диапазон {self.market_range}" if self.market_range else ""
        return "; ".join(b for b in (self.market_note, rng) if b)

    @property
    def verdict_label(self) -> str:
        return verdict_label(self.verdict) if self.verdict else ""

    @property
    def price_label(self) -> str:
        return "Текущая ставка" if self.auction else "Цена"


def _price_parts(listing: Listing, ev: Evaluation | None, auction: bool) -> tuple[bool, str, str, str, str]:
    """-> (is_free, main, short, full, negotiable_text)."""
    # The ad's own price is shown (shipping is listed separately); evaluation's
    # buy_price is a fallback for ads without a number.
    buy = listing.price if listing.price is not None else (ev.buy_price if ev is not None else None)
    if listing.is_free or (buy is not None and buy == 0 and not auction):
        return True, "бесплатно", "бесплатно", "бесплатно", ""
    main = "цена не указана" if buy is None else format_money(buy)
    if auction:
        short = f"ставка {main}" if buy is not None else "аукцион"
        return False, main, short, main, ""
    if listing.source == "ebay" and "BEST_OFFER" in listing.buying_options:
        neg = "Preisvorschlag (торг)"
        return False, main, f"{main} · Preisvorschlag", f"{main} · {neg}", neg
    if listing.negotiable:
        sep = ", " if buy is None else " "
        return False, main, f"{main}{sep}VB", f"{main}{sep}VB (торг)", "VB (торг)"
    return False, main, main, main, ""


def _market(ev: Evaluation | None, ai: AIVerdict | None) -> tuple[float | None, str, str]:
    est: PriceEstimate | None = ev.estimate if ev is not None else None
    if est is not None and est.market_price is not None:
        note = _ESTIMATE_SOURCES.get(est.source, est.source)
        if est.sample_size > 0:
            note = f"{note}, выборка: {est.sample_size}" if note else f"выборка: {est.sample_size}"
        rng = ""
        if est.low is not None and est.high is not None and est.high > est.low:
            rng = f"{format_money(est.low).removesuffix(' €')}–{format_money(est.high)}"
        return est.market_price, note, rng
    if ai is not None and ai.estimated_market_price is not None:
        return ai.estimated_market_price, _ESTIMATE_SOURCES["ai"], ""
    return None, "", ""


def _auction(listing: Listing) -> tuple[str, bool]:
    """-> (auction line, ends within an hour)."""
    bits = []
    if listing.bid_count is not None:
        n = listing.bid_count
        bits.append(f"{n} " + plural_ru(n, "ставка", "ставки", "ставок") if n > 0 else "ставок пока нет")
    soon = False
    if listing.ends_at is not None:
        now = _now()
        left = format_time_left(listing.ends_at, now)
        if left:
            bits.append(f"заканчивается через {left} ({_format_end_clock(listing.ends_at, now)})")
            soon = (_utc(listing.ends_at) - _utc(now)) <= timedelta(hours=1)
        else:
            bits.append("аукцион завершён")
    return " · ".join(bits) or "аукцион", soon


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = _squash(item)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _second_opinion(ai2: AIVerdict) -> str:
    who = f"Второе мнение ({_squash(ai2.model)})" if _squash(ai2.model) else "Второе мнение"
    text = f"{who}: {verdict_label(ai2.verdict).lower()}"
    if ai2.confidence:
        text += f", {format_percent(ai2.confidence)}"
    return text


def is_unchecked(ev: Evaluation | None) -> bool:
    """AI was enabled but unavailable, and the deal would be a buy: photos NOT checked."""
    if ev is None or getattr(ev, "ai_checked", None) is not False:
        return False
    return bool(getattr(ev, "would_buy", False)) or ev.verdict == "buy"


def offer_terms(ev: Evaluation | None, listing: Listing) -> tuple[float, float | None, float | None] | None:
    """Haggle deals: (suggested offer, profit or savings at that offer, ROI at that offer).

    `expected_profit` is computed for the asking price; paying the offer instead saves
    exactly (asking - offer), the rest of the calculation is unchanged."""
    if ev is None or getattr(ev, "action", "") != "haggle" or getattr(ev, "offer_price", None) is None:
        return None
    offer = float(ev.offer_price)
    if ev.expected_profit is None or listing.price is None:
        return offer, None, None
    profit = ev.expected_profit + (listing.price - offer)
    delivery = (ev.buy_price - listing.price) if ev.buy_price is not None else 0.0
    cost = offer + max(0.0, delivery)
    return offer, profit, (profit / cost if cost > 0 else None)


def haggle_phrase(ev: Evaluation | None, listing: Listing) -> str:
    """"Торгуйся: предложи 400 € → прибыль ≈ 120 €" ("" for other deals)."""
    terms = offer_terms(ev, listing)
    if terms is None:
        return ""
    offer, profit, _ = terms
    line = f"Торгуйся: предложи {format_money(offer)}"
    if profit is not None:
        word = "экономия" if ev is not None and ev.purpose == "personal" else "прибыль"
        line += f" → {word} ≈ {format_money(profit)}"
    return line


def _collect(deal: DealView, web_base_url: str | None = None) -> _DealInfo:
    lst = deal.listing
    ev = deal.evaluation
    ai = ev.ai if ev is not None else None
    auction = "AUCTION" in lst.buying_options
    free, main, short, full, neg = _price_parts(lst, ev, auction)
    info = _DealInfo(
        title=_squash(lst.title) or "Без названия",
        source=source_name(lst.source),
        url=safe_url(lst.url),
        open_text=open_link_text(deal),
        web_url=deal_web_url(deal, web_base_url),
        photo=next((u for u in (safe_url(x) for x in lst.image_urls) if u), None),
        is_free=free,
        price_main=main,
        price_short=short,
        price_full=full,
        negotiable_text=neg,
        auction=auction,
        purpose=ev.purpose if ev is not None else "resale",
    )
    if auction:
        info.auction_line, info.auction_soon = _auction(lst)
    if lst.shipping_cost is not None:
        info.shipping_cost = (
            "бесплатная доставка" if lst.shipping_cost <= 0 else f"доставка {format_money(lst.shipping_cost)}"
        )
    info.market_value, info.market_note, info.market_range = _market(ev, ai)

    if ev is not None:
        info.profit_label = "Экономия" if info.purpose == "personal" else "Прибыль"
        info.profit = ev.expected_profit
        if info.purpose == "personal" and ev.expected_profit is not None and info.market_value:
            share = abs(ev.expected_profit) / info.market_value
            word = "дешевле" if ev.expected_profit >= 0 else "дороже"
            info.profit_extra = f"на {format_percent(share)} {word} рынка"
        elif ev.roi is not None:
            info.profit_extra = f"ROI {format_percent(ev.roi)}"
        if ev.max_buy_price is not None:
            label = "Максимальная ставка" if auction else "Выгодно до"
            info.max_buy = f"{label} {format_money(ev.max_buy_price)}"
        costs = []
        if ev.fees:
            costs.append(f"комиссии {format_money(ev.fees)}")
        if ev.shipping_cost:
            costs.append(f"доставка {format_money(ev.shipping_cost)}")
        info.costs = " + ".join(costs)
        score = Decimal(str(ev.score or 0.0)).quantize(Decimal("1"), ROUND_HALF_UP)
        info.score = max(0, min(100, int(score)))
        info.verdict = ev.verdict
        info.unchecked = is_unchecked(ev)
        terms = offer_terms(ev, lst)
        if terms is not None:
            info.haggle = haggle_phrase(ev, lst)
            info.offer_money = format_money(terms[0])
            info.offer_profit = terms[1]
        # the haggle line already says it: drop the engine's own "Торгуйся…/Предложи…" reasons
        reasons = [r for r in ev.reasons if not (info.haggle and r.startswith(("Торгуйся", "Предложи")))]
        info.reasons = _dedupe(reasons)[:3]
        if ev.ai_second is not None:
            info.second = _second_opinion(ev.ai_second)
            info.second_reasoning = _squash(ev.ai_second.reasoning)

    loc = [x for x in (_squash(lst.location) or _squash(lst.postal_code), format_km(lst.distance_km)) if x]
    info.location = " · ".join(loc)
    info.posted = _squash(lst.posted_at_text)
    if lst.shipping_possible is True and not info.shipping_cost:
        info.shipping = "возможна отправка"
    elif lst.shipping_possible is False:
        info.shipping = "только самовывоз"
    seller = {"private": "частный продавец", "commercial": "коммерческий продавец"}.get(lst.seller_type, "")
    if lst.seller_feedback_percent is not None:
        pct = f"{lst.seller_feedback_percent:.1f}".replace(".", ",").removesuffix(",0")
        fb = f"{pct} % положительных отзывов"
        if lst.seller_feedback_score is not None:
            n = lst.seller_feedback_score
            fb += f" ({_group(n)} " + plural_ru(n, "оценка", "оценки", "оценок") + ")"
        seller = f"{seller}, {fb}" if seller else f"продавец: {fb}"
    info.seller = seller
    from .extras import lines_for

    info.extras = lines_for(lst, ev)
    info.condition = _squash(lst.condition) or _squash(lst.attributes.get("Zustand", ""))

    if ai is not None:
        info.ai = True
        info.ai_product = _squash(ai.product)
        info.ai_condition = _CONDITION_LABELS.get(ai.condition, "")
        if ai.photo_matches_description is True:
            info.ai_photo = "✅ фото соответствует описанию"
        elif ai.photo_matches_description is False:
            info.ai_photo = "❌ фото НЕ соответствует описанию"
        else:
            info.ai_photo = "❔ соответствие фото не проверено"
        if ai.confidence:
            info.ai_confidence = f"уверенность {format_percent(ai.confidence)}"
        info.ai_reasoning = _squash(ai.reasoning)

    ai2 = ev.ai_second if ev is not None else None
    info.red_flags = _dedupe(
        [*(ev.red_flags if ev else []), *(ai.red_flags if ai else []), *(ai2.red_flags if ai2 else [])]
    )
    return info


def _profit_line(info: _DealInfo) -> str:
    """"≈ 122 € · ROI 32 %" (without the label)."""
    if info.profit is None:
        return ""
    s = f"≈ {format_money(info.profit)}"
    return f"{s} · {info.profit_extra}" if info.profit_extra else s


def deal_headline(deal: DealView) -> str:
    """One-line summary, e.g. "RTX 3090 — 450 € → прибыль ≈ 160 € · Kleinanzeigen";
    haggle deals: "… — 450 € VB · Торгуйся: предложи 400 € → прибыль ≈ 120 € · …"."""
    info = _collect(deal)
    line = f"{_clip(info.title, 80)} — {info.price_short}"
    if info.haggle:
        line += f" · {info.haggle}"
    elif info.profit is not None:
        line += f" → {info.profit_label.lower()} ≈ {format_money(info.profit)}"
    line = f"{line} · {info.source}"
    return f"{UNCHECKED_WARNING} · {line}" if info.unchecked else line


SUPER_PREFIX = "🔥 Супер-находка"


def super_title(deal: DealView) -> str:
    """Headline of a «Супер-находка» alert: "🔥 Супер-находка: RTX 3080 за 150 € → прибыль ≈ 180 €"."""
    info = _collect(deal)
    head = f"{SUPER_PREFIX}: {_clip(info.title, 60)} за {info.price_short}"
    if info.profit is not None:
        head += f" → {info.profit_label.lower()} ≈ {format_money(info.profit)}"
    return head


def email_subject(deals: list[DealView]) -> str:
    """"🔥 1 выгодное предложение: RTX 3090 за 450 €" / "🔥 5 выгодных предложений на Kleinanzeigen"."""
    n = len(deals)
    if n == 0:
        return "EbeyParser: новых предложений нет"
    unchecked = any(is_unchecked(d.evaluation) for d in deals)
    warn = f"{UNCHECKED_WARNING}: " if unchecked else ""
    if n == 1:
        lst = deals[0].listing
        info = _collect(deals[0])
        head = f"{warn or '🔥 '}{deals_count_phrase(1)}: {_clip(info.title, 60)}"
        if info.is_free:
            return f"{head} — бесплатно"
        if info.auction:
            left = format_time_left(lst.ends_at) if lst.ends_at else ""
            return f"{head} — {info.price_short}" + (f", осталось {left}" if left else "")
        if info.price_main == "цена не указана":
            return head + (f" — предложи {info.offer_money}" if info.offer_money else "")
        if info.offer_money:
            return f"{head} за {info.price_short} — предложи {info.offer_money}"
        return f"{head} за {info.price_short}"
    return f"{warn or '🔥 '}{deals_count_phrase(n)} на {_sources_phrase(deals)}"


# --------------------------------------------------------------------------- #
# Plain text
# --------------------------------------------------------------------------- #


def _text_block(index: int, info: _DealInfo) -> list[str]:
    pad = "   "
    lines = [f"{index}. [{info.source}] {info.title}"]
    if info.unchecked:
        lines.append(f"{pad}{UNCHECKED_WARNING}")
    if info.verdict:
        lines.append(f"{pad}Вердикт: {info.verdict_label} · оценка {info.score}/100")
    price = f"{pad}{info.price_label}: {info.price_full}"
    if info.shipping_cost:
        price += f" + {info.shipping_cost}"
    lines.append(price)
    if info.auction:
        lines.append(f"{pad}🔨 {info.auction_line}")
    if info.market_str:
        extra = f" ({info.market_details})" if info.market_details else ""
        lines.append(f"{pad}Рынок: {info.market_str}{extra}")
    if info.haggle:
        lines.append(f"{pad}🤝 {info.haggle}")
    elif info.profit is not None:
        extra = f" (учтены {info.costs})" if info.costs else ""
        lines.append(f"{pad}{info.profit_label}: {_profit_line(info)}{extra}")
    if info.max_buy:
        lines.append(f"{pad}🎯 {info.max_buy}")
    lines += [f"{pad}{x}" for x in info.extras]
    where = " · ".join(x for x in (info.location, info.posted, info.shipping) if x)
    if where:
        lines.append(f"{pad}Где: {where}")
    if info.condition:
        lines.append(f"{pad}Состояние (по продавцу): {info.condition}")
    if info.seller:
        lines.append(f"{pad}Продавец: {info.seller}")
    if info.ai:
        ai_bits = [
            x
            for x in (
                info.ai_product,
                f"состояние: {info.ai_condition}" if info.ai_condition else "",
                info.ai_photo,
                info.ai_confidence,
            )
            if x
        ]
        lines.append(f"{pad}ИИ: " + " · ".join(ai_bits))
        if info.ai_reasoning:
            lines.append(f"{pad}    {_clip(info.ai_reasoning, 600)}")
    if info.second:
        lines.append(f"{pad}{info.second}")
        if info.second_reasoning:
            lines.append(f"{pad}    {_clip(info.second_reasoning, 300)}")
    lines += [f"{pad}⚠ {_clip(f, 200)}" for f in info.red_flags[:5]]
    lines += [f"{pad}• {_clip(r, 200)}" for r in info.reasons]
    if info.url:
        lines.append(f"{pad}{info.open_text}: {info.url}")
    if info.web_url:
        lines.append(f"{pad}Подробнее в EbeyParser: {info.web_url}")
    return lines


def render_text(deals: list[DealView], *, title: str | None = None, web_base_url: str | None = None) -> str:
    """Plain-text version of the notification (e-mail alternative part, logs)."""
    heading = _squash(title) or default_title(deals)
    lines = [heading, "=" * min(max(len(heading), 3), 60), ""]
    if not deals:
        lines += ["Новых предложений нет.", ""]
    shown = deals[:MAX_EMAIL_DEALS]
    for i, deal in enumerate(shown, 1):
        lines += _text_block(i, _collect(deal, web_base_url))
        lines.append("")
    if len(deals) > len(shown):
        lines += [more_deals_phrase(len(deals) - len(shown)) + " — смотрите в EbeyParser.", ""]
    lines += ["—" * 20, FOOTER_TEXT]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# E-mail HTML (inline CSS, table layout, Gmail friendly)
# --------------------------------------------------------------------------- #

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_CHIP_COLORS = {"buy": ("#dcfce7", "#166534"), "maybe": ("#fef3c7", "#92400e"), "skip": ("#e5e7eb", "#374151")}
_SOURCE_COLORS = {"eBay": ("#dbeafe", "#1e40af"), "Kleinanzeigen": ("#ecfccb", "#3f6212")}


def _e(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _eh(text: str | None) -> str:
    """Escape and keep "450 €" together on one line."""
    return _e(text).replace(" €", "&nbsp;€")


def _chip(text_html: str, bg: str, fg: str) -> str:
    return (
        f'<span style="display:inline-block;margin:0 6px 6px 0;padding:4px 10px;border-radius:999px;'
        f"background:{bg};color:{fg};font-family:{_FONT};font-size:12px;font-weight:bold;"
        f'line-height:16px;white-space:nowrap;">{text_html}</span>'
    )


def _button(url: str, text: str, primary: bool) -> str:
    style = (
        "background:#2563eb;color:#ffffff;border:1px solid #2563eb;"
        if primary
        else "background:#f1f5f9;color:#1e293b;border:1px solid #cbd5e1;"
    )
    return (
        f'<a href="{_e(url)}" target="_blank" style="display:inline-block;margin:4px 8px 4px 0;'
        f"padding:10px 16px;border-radius:8px;{style}font-family:{_FONT};font-size:14px;"
        f'font-weight:bold;line-height:18px;text-decoration:none;">{_e(text)}</a>'
    )


def _row(label: str, value_html: str) -> str:
    return (
        f'<tr><td valign="top" style="padding:3px 12px 3px 0;font-family:{_FONT};font-size:13px;'
        f'color:#6b7280;white-space:nowrap;width:1%;">{_e(label)}</td>'
        f'<td valign="top" style="padding:3px 0;font-family:{_FONT};font-size:13px;color:#1f2933;">'
        f"{value_html}</td></tr>"
    )


def _box(inner_html: str, bg: str, border: str, color: str) -> str:
    return (
        f'<div style="padding:10px 12px;background:{bg};border-left:4px solid {border};border-radius:6px;'
        f'font-family:{_FONT};font-size:13px;line-height:19px;color:{color};">{inner_html}</div>'
    )


def _email_card(info: _DealInfo) -> str:
    pad = "padding-left:20px;padding-right:20px;"
    out = [
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background:#ffffff;border:1px solid #e2e6ec;border-radius:12px;'
        'border-collapse:separate;" bgcolor="#ffffff">'
    ]
    # Photo on top
    if info.photo:
        img = (
            f'<img src="{_e(info.photo)}" width="598" alt="{_e(_clip(info.title, 100))}" '
            'style="display:block;width:100%;max-width:598px;height:auto;border:0;'
            'border-radius:12px 12px 0 0;background:#f1f5f9;">'
        )
        if info.url:
            img = f'<a href="{_e(info.url)}" target="_blank" style="text-decoration:none;">{img}</a>'
        out.append(f'<tr><td style="padding:0;line-height:0;font-size:0;">{img}</td></tr>')
    # Chips: verdict, score, source, purpose, auction/VB
    chips = []
    if info.verdict:
        bg, fg = _CHIP_COLORS.get(info.verdict, _CHIP_COLORS["skip"])
        chips.append(_chip(_e(info.verdict_label), bg, fg))
    if info.score is not None:
        if info.score >= 75:
            bg, fg = "#dcfce7", "#166534"
        elif info.score >= 50:
            bg, fg = "#fef3c7", "#92400e"
        else:
            bg, fg = "#f1f5f9", "#475569"
        chips.append(_chip(f"Оценка {info.score}/100", bg, fg))
    chips.append(_chip(_e(info.source), *_SOURCE_COLORS.get(info.source, ("#f1f5f9", "#475569"))))
    if info.purpose == "personal":
        chips.append(_chip("Для себя", "#ede9fe", "#5b21b6"))
    if info.auction:
        chips.append(_chip("🔨 Аукцион", "#ffedd5", "#9a3412"))
    elif info.negotiable_text:
        chips.append(_chip(_e(info.negotiable_text), "#f1f5f9", "#475569"))
    if info.unchecked:
        out.append(
            '<tr><td style="padding:16px 20px 0 20px;">'
            + _box(f"<b>{_e(UNCHECKED_WARNING)}</b>: нейросеть была недоступна, фото и описание никто не "
                   "сравнил. Посмотри объявление внимательно, прежде чем писать продавцу.",
                   "#fef2f2", "#fecaca", "#991b1b")
            + "</td></tr>"
        )
    out.append(f'<tr><td style="padding:16px 20px 0 20px;">{"".join(chips)}</td></tr>')
    # Title
    title_html = _e(_clip(info.title, 200))
    if info.url:
        title_html = f'<a href="{_e(info.url)}" target="_blank" style="color:#111827;text-decoration:none;">{title_html}</a>'
    out.append(
        f'<tr><td style="{pad}padding-top:4px;font-family:{_FONT};font-size:18px;'
        f'font-weight:bold;line-height:24px;color:#111827;">{title_html}</td></tr>'
    )
    # Big price line
    price = ""
    if info.auction:
        price += f'<div style="font-family:{_FONT};font-size:12px;color:#6b7280;">Текущая ставка</div>'
    price += (
        f'<span style="font-family:{_FONT};font-size:28px;font-weight:800;line-height:36px;color:#111827;">'
        f"{_eh(info.price_main)}</span>"
    )
    small = [x for x in (f"+ {info.shipping_cost}" if info.shipping_cost else "",
                         f"рынок {info.market_str}" if info.market_str else "") if x]
    if small:
        price += (
            f'<span style="font-family:{_FONT};font-size:14px;color:#6b7280;">&nbsp;&nbsp;'
            f"{' · '.join(_eh(s) for s in small)}</span>"
        )
    out.append(f'<tr><td style="{pad}padding-top:6px;">{price}</td></tr>')
    # Auction countdown
    if info.auction:
        color = "#b91c1c" if info.auction_soon else "#9a3412"
        out.append(
            f'<tr><td style="{pad}padding-top:4px;font-family:{_FONT};font-size:14px;font-weight:bold;'
            f'color:{color};">🔨 {_e(info.auction_line)}</td></tr>'
        )
    # Profit badge + max buy price
    badges = []
    if info.haggle:
        badges.append(
            f'<span style="display:inline-block;margin:0 6px 6px 0;padding:6px 14px;border-radius:16px;'
            f"background:#16a34a;color:#ffffff;font-family:{_FONT};font-size:15px;font-weight:bold;"
            f'line-height:20px;">🤝 {_eh(info.haggle)}</span>'
        )
    elif info.profit is not None:
        positive = info.profit > 0
        bg, fg = ("#16a34a", "#ffffff") if positive else ("#fee2e2", "#991b1b")
        text = f"{info.profit_label} {'+' if positive else ''}{format_money(info.profit)}"
        if info.profit_extra:
            text += f" · {info.profit_extra}"
        badges.append(
            f'<span style="display:inline-block;margin:0 6px 6px 0;padding:6px 14px;border-radius:16px;'
            f"background:{bg};color:{fg};font-family:{_FONT};font-size:15px;font-weight:bold;"
            f'line-height:20px;">{_eh(text)}</span>'
        )
    if info.max_buy:
        badges.append(
            f'<span style="display:inline-block;margin:0 6px 6px 0;padding:5px 12px;border-radius:999px;'
            f"background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;font-family:{_FONT};font-size:14px;"
            f'font-weight:bold;line-height:20px;">🎯 {_eh(info.max_buy)}</span>'
        )
    if badges:
        out.append(f'<tr><td style="{pad}padding-top:10px;">{"".join(badges)}</td></tr>')
    for extra in info.extras:
        out.append(f'<tr><td style="{pad}padding-top:4px;font-family:{_FONT};font-size:14px;color:#1f2937;">'
                   f"{_e(extra)}</td></tr>")
    # Details table
    price_val = _eh(info.price_full) + (f" + {_eh(info.shipping_cost)}" if info.shipping_cost else "")
    rows = [_row(info.price_label, price_val)]
    if info.market_str:
        details = f' <span style="color:#6b7280;">({_eh(info.market_details)})</span>' if info.market_details else ""
        rows.append(_row("Рынок", _eh(info.market_str) + details))
    if info.costs:
        rows.append(_row("Учтено", _eh(info.costs)))
    if info.location:
        rows.append(_row("Где", "📍 " + _eh(info.location)))
    if info.posted:
        rows.append(_row("Опубликовано", _e(info.posted)))
    if info.shipping:
        rows.append(_row("Доставка", _e(info.shipping)))
    if info.condition:
        rows.append(_row("Состояние", _e(info.condition)))
    if info.seller:
        rows.append(_row("Продавец", _e(info.seller)))
    out.append(
        f'<tr><td style="{pad}padding-top:8px;"><table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;">{"".join(rows)}</table></td></tr>'
    )
    # AI opinion(s)
    if info.ai or info.second:
        body = '<div style="font-weight:bold;margin-bottom:4px;">🤖 Мнение ИИ</div>'
        if info.ai:
            head_bits = [
                x
                for x in (
                    info.ai_product and f"<b>{_e(info.ai_product)}</b>",
                    info.ai_condition and f"состояние: {_e(info.ai_condition)}",
                    info.ai_confidence and _e(info.ai_confidence),
                )
                if x
            ]
            if head_bits:
                body += f'<div style="margin-bottom:4px;">{" · ".join(head_bits)}</div>'
            body += f'<div style="margin-bottom:4px;">{_e(info.ai_photo)}</div>'
            if info.ai_reasoning:
                body += f'<div style="color:#334155;">{_e(_clip(info.ai_reasoning, 700))}</div>'
        if info.second:
            sep = "margin-top:8px;padding-top:8px;border-top:1px solid #bae6fd;" if info.ai else ""
            body += f'<div style="{sep}font-weight:bold;">🧠 {_e(info.second)}</div>'
            if info.second_reasoning:
                body += f'<div style="color:#334155;">{_e(_clip(info.second_reasoning, 400))}</div>'
        out.append(f'<tr><td style="{pad}padding-top:12px;">{_box(body, "#f0f9ff", "#38bdf8", "#0f172a")}</td></tr>')
    # Red flags
    if info.red_flags:
        flags = "".join(f'<div style="margin:2px 0;">⚠ {_e(_clip(f, 200))}</div>' for f in info.red_flags[:5])
        out.append(f'<tr><td style="{pad}padding-top:10px;">{_box(flags, "#fffbeb", "#f59e0b", "#92400e")}</td></tr>')
    # Top reasons
    if info.reasons:
        items = "".join(
            f'<tr><td valign="top" style="padding:2px 8px 2px 0;font-family:{_FONT};font-size:13px;'
            f'color:#16a34a;">•</td><td style="padding:2px 0;font-family:{_FONT};font-size:13px;'
            f'line-height:19px;color:#334155;">{_eh(_clip(r, 200))}</td></tr>'
            for r in info.reasons
        )
        out.append(
            f'<tr><td style="{pad}padding-top:10px;"><table role="presentation" cellpadding="0" '
            f'cellspacing="0" border="0">{items}</table></td></tr>'
        )
    # Buttons
    buttons = []
    if info.url:
        buttons.append(_button(info.url, info.open_text, primary=True))
    if info.web_url:
        buttons.append(_button(info.web_url, "Подробнее в EbeyParser", primary=False))
    out.append(f'<tr><td style="padding:14px 20px 18px 20px;">{"".join(buttons)}</td></tr>')
    out.append("</table>")
    return "".join(out)


def render_email_html(deals: list[DealView], *, title: str | None = None, web_base_url: str | None = None) -> str:
    """Full HTML e-mail: header, one card per deal, footer. Inline CSS only."""
    heading = _squash(title) or default_title(deals)
    shown = deals[:MAX_EMAIL_DEALS]
    count = f"{deals_count_phrase(len(deals))} на {_sources_phrase(deals)}" if deals else "новых предложений нет"
    preheader = deal_headline(deals[0]) if deals else heading
    cards = "".join(
        f'<tr><td style="padding:0 0 16px 0;">{_email_card(_collect(d, web_base_url))}</td></tr>' for d in shown
    )
    if len(deals) > len(shown):
        more = _e(more_deals_phrase(len(deals) - len(shown)) + " — смотрите в EbeyParser.")
        cards += (
            f'<tr><td align="center" style="padding:0 0 16px 0;font-family:{_FONT};font-size:14px;'
            f'color:#475569;">{more}</td></tr>'
        )
    return (
        "<!DOCTYPE html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        f"<title>{_e(heading)}</title></head>"
        '<body style="margin:0;padding:0;background:#eef1f5;">'
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">'
        f"{_e(preheader)}</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background:#eef1f5;" bgcolor="#eef1f5">'
        '<tr><td align="center" style="padding:16px 8px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;">'
        # Header
        '<tr><td style="padding:0 0 16px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background:#1e293b;border-radius:12px;" bgcolor="#1e293b">'
        f'<tr><td style="padding:18px 20px;font-family:{_FONT};">'
        '<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#94a3b8;'
        'font-weight:bold;">🔥 EbeyParser</div>'
        '<div style="font-size:22px;line-height:28px;font-weight:bold;color:#ffffff;margin-top:4px;">'
        f"{_e(heading)}</div>"
        f'<div style="font-size:14px;color:#cbd5e1;margin-top:4px;">{_e(count)}</div>'
        "</td></tr></table></td></tr>"
        f"{cards}"
        # Footer
        f'<tr><td align="center" style="padding:8px 0 24px 0;font-family:{_FONT};font-size:12px;'
        f'color:#94a3b8;">{_e(FOOTER_TEXT)}</td></tr>'
        "</table></td></tr></table></body></html>"
    )


# --------------------------------------------------------------------------- #
# Telegram (HTML parse_mode, photo caption <= 1024 chars)
# --------------------------------------------------------------------------- #


def _tg(text: str | None) -> str:
    # Telegram HTML only needs &, <, > escaped in text.
    return html.escape(text or "", quote=False)


def _tg_attr(text: str) -> str:
    return html.escape(text, quote=False).replace('"', "&quot;")


def telegram_length(text: str) -> int:
    """Length in UTF-16 code units (how Telegram counts). Measured on the raw
    HTML, which is conservative: tags do not count on Telegram's side."""
    return len(text.encode("utf-16-le")) // 2


def _tg_clip(text: str, budget: int) -> str:
    """Escape `text` and cut it so that the escaped form fits `budget` code units."""
    limit = budget
    while limit > 0:
        out = _tg(_clip(text, limit))
        size = telegram_length(out)
        if size <= budget:
            return out
        limit -= max(1, size - budget)
    return ""


# Progressively leaner layouts:
# (title, reasons, reason_len, flags, flag_len, reasoning_cap, details)
_TG_LEVELS = (
    (160, 3, 160, 3, 140, 500, True),
    (140, 3, 120, 3, 110, 300, True),
    (120, 2, 100, 2, 100, 200, True),
    (100, 1, 90, 1, 90, 120, True),
    (90, 0, 0, 1, 80, 0, True),
    (80, 0, 0, 0, 0, 0, False),
)


def _tg_build(info: _DealInfo, level: tuple, with_url: bool) -> str:
    title_len, n_reasons, reason_len, n_flags, flag_len, reasoning_cap, details = level
    lines: list[str] = []
    if info.unchecked:
        lines.append(f"<b>{_tg(UNCHECKED_WARNING)}</b>")
    head = []
    if info.verdict:
        head.append(f"{_VERDICT_EMOJI.get(info.verdict, '')} <b>{_tg(info.verdict_label)}</b>")
    if info.score is not None:
        head.append(f"{info.score}/100")
    head.append(_tg(info.source))
    if info.purpose == "personal":
        head.append("для себя")
    lines.append(" · ".join(head))
    lines.append(f"<b>{_tg_clip(info.title, title_len)}</b>")
    price = "💶 "
    if info.auction:
        price += "ставка "
    price += f"<b>{_tg(info.price_main)}</b>"
    if info.negotiable_text:
        price += f" {_tg(info.negotiable_text)}"
    if info.shipping_cost:
        price += f" + {_tg(info.shipping_cost)}"
    if info.market_str:
        price += f" · рынок {_tg(info.market_str)}"
        if details and info.market_note:
            price += f" ({_tg(info.market_note)})"
    lines.append(price)
    if info.auction:
        lines.append(f"🔨 {_tg(info.auction_line)}")
    if info.haggle:
        line = f"🤝 Торгуйся: предложи <b>{_tg(info.offer_money)}</b>"
        if info.offer_profit is not None:
            word = "экономия" if info.purpose == "personal" else "прибыль"
            line += f" → {word} ≈ <b>{_tg(format_money(info.offer_profit))}</b>"
        lines.append(line)
    elif info.profit is not None:
        line = f"📈 {info.profit_label} ≈ <b>{_tg(format_money(info.profit))}</b>"
        if info.profit_extra:
            line += f" · {_tg(info.profit_extra)}"
        lines.append(line)
    if info.max_buy:
        lines.append(f"🎯 {_tg(info.max_buy)}")
    lines += [_tg_clip(x, 200) for x in info.extras]
    if details and info.location:
        lines.append(f"📍 {_tg_clip(info.location, 80)}")
    if details and info.ai:
        bits = [x for x in (_tg_clip(info.ai_product, 80), _tg(info.ai_photo)) if x]
        lines.append("🤖 " + " · ".join(bits))
    reasoning_at = len(lines)
    if info.second:
        lines.append(f"🧠 {_tg_clip(info.second, 120)}")
    lines += [f"⚠ {_tg_clip(f, flag_len)}" for f in info.red_flags[:n_flags]]
    lines += [f"• {_tg_clip(r, reason_len)}" for r in info.reasons[:n_reasons]]
    links = []
    if info.url and with_url:
        links.append(f'<a href="{_tg_attr(info.url)}">{_tg(info.open_text)}</a>')
    if info.web_url:
        if is_local_url(info.web_url):
            links.append(f"EbeyParser: {_tg(info.web_url)}")  # plain text: Telegram won't link localhost
        else:
            links.append(f'<a href="{_tg_attr(info.web_url)}">Подробнее в EbeyParser</a>')
    if links:
        lines.append("🔗 " + " · ".join(links))

    # The local model's reasoning takes whatever room is left (up to its cap).
    if info.ai_reasoning and reasoning_cap:
        room = TELEGRAM_CAPTION_LIMIT - telegram_length("\n".join(lines)) - len("\n<i></i>")
        budget = min(reasoning_cap, room)
        if budget >= 40:
            lines.insert(reasoning_at, f"<i>{_tg_clip(info.ai_reasoning, budget)}</i>")
    return "\n".join(lines)


def render_telegram(deal: DealView, *, web_base_url: str | None = None) -> str:
    """Telegram HTML message for one deal; always fits a photo caption (1024)."""
    info = _collect(deal, web_base_url)
    for with_url in (True, False):  # a monster URL is dropped last (the button still has it)
        for level in _TG_LEVELS:
            text = _tg_build(info, level, with_url)
            if telegram_length(text) <= TELEGRAM_CAPTION_LIMIT:
                return text
    # Unreachable in practice: every part above is length-capped.
    return f"<b>{_tg_clip(info.title, 200)}</b>\n💶 {_tg(info.price_short)}"


# --------------------------------------------------------------------------- #
# Sample data for "send test notification"
# --------------------------------------------------------------------------- #


def sample_deals() -> list[DealView]:
    """Three realistic fake deals: an eBay auction ending soon (resale, buy),
    RAM for a home AI server (personal, buy) and a doubtful Mac mini (maybe)."""
    now = _now()
    gpu = DealView(
        listing=Listing(
            ad_id="ebay-187654321098",
            source="ebay",
            url="https://www.ebay.de/itm/187654321098",
            title="NVIDIA GeForce RTX 3080 Founders Edition 10GB – funktioniert einwandfrei",
            price=310.0,
            price_text="EUR 310,00",
            location="Leipzig",
            posted_at_text="",
            description="Karte lief nur im Gaming-PC, nie Mining. OVP vorhanden, Privatverkauf.",
            image_urls=["https://placehold.co/800x600/1f2937/ffffff/png?text=RTX+3080+FE"],
            shipping_possible=True,
            seller_name="pc-schrauber-le",
            seller_type="private",
            condition="Gebraucht",
            buying_options=["AUCTION"],
            bid_count=7,
            ends_at=now + timedelta(hours=1, minutes=40),
            shipping_cost=6.99,
            seller_feedback_percent=99.6,
            seller_feedback_score=412,
            search_name="GPU RTX 30xx",
        ),
        evaluation=Evaluation(
            ad_id="ebay-187654321098",
            purpose="resale",
            buy_price=316.99,
            estimate=PriceEstimate(market_price=480.0, low=440.0, high=520.0, sample_size=23, source="ebay_sold"),
            ai=AIVerdict(
                product="NVIDIA GeForce RTX 3080 Founders Edition 10GB",
                photo_matches_description=True,
                condition="good",
                verdict="buy",
                confidence=0.82,
                reasoning="На фото оригинальная FE с коробкой, следов майнинга не видно. "
                "Проданные на eBay уходят за 440–520 €, запас по цене хороший.",
                model="qwen2.5vl:7b",
            ),
            ai_second=AIVerdict(product="RTX 3080 FE", verdict="buy", confidence=0.8, model="claude-opus-5"),
            max_buy_price=375.0,
            shipping_cost=7.0,
            expected_profit=128.0,
            roi=0.40,
            score=86.0,
            verdict="buy",
            reasons=[
                "Текущая ставка на 34 % ниже цены проданных на eBay",
                "Продавец с 99,6 % положительных отзывов",
                "Есть оригинальная коробка — легче перепродать",
            ],
        ),
    )
    ram = DealView(
        listing=Listing(
            ad_id="2934567812",
            url="https://www.kleinanzeigen.de/s-anzeige/64gb-ddr4-3200-corsair-vengeance-lpx/2934567812-225-3331",
            title="64GB (2x32GB) DDR4 3200 Corsair Vengeance LPX RAM",
            price=95.0,
            price_text="95 € VB",
            negotiable=True,
            location="10247 Friedrichshain",
            postal_code="10247",
            distance_km=6.2,
            posted_at_text="Heute, 09:14",
            description="Verkaufe meinen Arbeitsspeicher nach Upgrade. Lief stabil mit XMP. Abholung oder Versand.",
            image_urls=["https://placehold.co/800x600/0f766e/ffffff/png?text=64GB+DDR4"],
            shipping_possible=True,
            seller_type="private",
            attributes={"Zustand": "Gut"},
            search_name="RAM для AI-сервера",
        ),
        evaluation=Evaluation(
            ad_id="2934567812",
            purpose="personal",
            buy_price=95.0,
            estimate=PriceEstimate(market_price=140.0, low=125.0, high=160.0, sample_size=9, source="kleinanzeigen"),
            ai=AIVerdict(
                product="Corsair Vengeance LPX 64GB (2x32GB) DDR4-3200 CL16",
                photo_matches_description=True,
                condition="good",
                verdict="buy",
                confidence=0.74,
                reasoning="Комплект 2×32 ГБ как на фото, маркировка совпадает. Для AI-сервера на AM4 — отличный "
                "вариант, 64 ГБ хватит для локальных моделей 30B в квантовании.",
            ),
            max_buy_price=115.0,
            expected_profit=45.0,
            roi=0.47,
            score=78.0,
            verdict="buy",
            reasons=[
                "Дешевле рынка на 32 %",
                "Самовывоз в 6 км, можно проверить на месте",
                "Ниже вашей целевой цены 115 €",
            ],
        ),
    )
    mac = DealView(
        listing=Listing(
            ad_id="2931122334",
            url="https://www.kleinanzeigen.de/s-anzeige/mac-mini-m1-16gb-512gb/2931122334-228-3490",
            title="Mac mini M1 16GB RAM 512GB SSD",
            price=520.0,
            price_text="520 € VB",
            negotiable=True,
            location="14467 Potsdam",
            postal_code="14467",
            distance_km=31.0,
            posted_at_text="Gestern, 21:47",
            description="Mac mini M1, 16GB, 512GB. Nur Selbstabholung. Keine Rechnung.",
            image_urls=["https://placehold.co/800x600/6b7280/ffffff/png?text=Mac+mini+M1"],
            shipping_possible=False,
            seller_type="private",
            search_name="Mac mini",
        ),
        evaluation=Evaluation(
            ad_id="2931122334",
            purpose="resale",
            buy_price=520.0,
            estimate=PriceEstimate(market_price=600.0, low=560.0, high=650.0, sample_size=17, source="mixed"),
            ai=AIVerdict(
                product="Apple Mac mini (M1, 2020)",
                photo_matches_description=False,
                condition="used",
                red_flags=["На фото наклейка 8 ГБ / 256 ГБ, в тексте 16 ГБ / 512 ГБ"],
                verdict="maybe",
                confidence=0.55,
                reasoning="Конфигурация на фото и в тексте расходится. Если это действительно 16/512, "
                "цена хорошая, но нужно проверить «Об этом Mac» при встрече.",
            ),
            max_buy_price=470.0,
            expected_profit=48.0,
            roi=0.09,
            score=58.0,
            verdict="maybe",
            reasons=["Цена на 13 % ниже рынка", "Прибыль небольшая, но есть торг (VB)"],
            red_flags=["Нет чека — сложнее перепродать"],
        ),
    )
    return [gpu, ram, mac]
