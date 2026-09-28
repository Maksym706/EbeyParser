"""Market value estimation from comparables and deal scoring (profit, ROI, verdict)."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Sequence, TypeVar

from ..config import PricingConfig, ReferencePrice, SearchConfig
from ..models import AIVerdict, Comparable, Evaluation, Listing, PriceEstimate, utcnow
from .text import (
    FLAG_BOX_ONLY,
    FLAG_DEFECT,
    FLAG_DELETED,
    FLAG_LOCKED,
    FLAG_RESERVED,
    FLAG_TOO_GOOD,
    FLAG_WANTED,
    SEVERE_FLAGS,
    detect_red_flags,
    is_remote_only,
    is_wanted_ad,
    make_search_query,
    matched_exclude_keyword,
    matches_keywords,
    normalize,
)

T = TypeVar("T")

COMPS_CAP = 30
AUCTION_SOON = timedelta(hours=3)

FLAG_LOW_RATING = "Низкий рейтинг продавца"
FLAG_FEW_REVIEWS = "Мало отзывов у продавца"
FLAG_TOO_CHEAP = "Подозрительно низкая цена"

_VERDICT_RU = {"buy": "покупать", "maybe": "возможно", "skip": "не брать"}
_VERDICT_RANK = {"skip": 0, "maybe": 1, "buy": 2}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def fmt_money(value: float) -> str:
    """German style: "1.234 €"; cents only below 10 € ("7,50 €")."""
    if abs(value) < 10 and round(value, 2) != round(value):
        return f"{value:.2f}".replace(".", ",") + " €"
    return f"{round(value):,}".replace(",", ".") + " €"


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (like numpy's default)."""
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _clean_pairs(items: list[tuple[float, T]]) -> list[tuple[float, T]]:
    """Drop non-positive values, IQR outliers (n>=4) and values < 25% of the median."""
    kept = sorted(
        (
            (float(v), x)
            for v, x in items
            if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
        ),
        key=lambda t: t[0],
    )
    if len(kept) >= 4:
        vals = [v for v, _ in kept]
        q1, q3 = _percentile(vals, 0.25), _percentile(vals, 0.75)
        iqr = max(q3 - q1, 0.1 * median(vals))  # identical prices shouldn't reject neighbours
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        kept = [(v, x) for v, x in kept if lo <= v <= hi]
    if kept:
        med = median(v for v, _ in kept)
        kept = [(v, x) for v, x in kept if v >= 0.25 * med]
    return kept


def robust_prices(prices: list[float]) -> list[float]:
    """Sorted prices without junk: non-positive, IQR outliers, < 25% of the median."""
    return [v for v, _ in _clean_pairs([(p, None) for p in prices])]


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------


def _is_sold(c: Comparable) -> bool:
    return c.sold or c.source in ("ebay_sold", "reference")


def _asking_where(comps: list[Comparable]) -> str:
    sources = {c.source for c in comps if not _is_sold(c)}
    if sources == {"kleinanzeigen"}:
        return " на Kleinanzeigen"
    if sources == {"ebay"}:
        return " на eBay"
    return ""


# Words that mean "a whole computer" or "a part/accessory", not the component itself.
_BUNDLE_WORDS = frozenset({
    "pc", "gamingpc", "rechner", "computer", "komplettsystem", "komplett", "tower", "laptop",
    "notebook", "setup", "konvolut", "bundle", "system", "workstation", "server", "barebone",
})
_PART_WORDS = frozenset({
    "kuehler", "cooler", "kuehlung", "waterblock", "wasserblock", "wasserkuehler", "wasserkuehlung",
    "eisblock", "eiswolf", "backplate", "halterung", "stuetze", "luefter", "fan", "fans", "shroud",
    "kabel", "adapter", "riser", "bracket", "anti", "sag", "gehaeuse", "netzteil", "mainboard",
})
# Accessories sold "for" a product: a phone case must not count as a phone.
_ACCESSORY_WORDS = frozenset({
    "huelle", "handyhuelle", "schutzhuelle", "silikonhuelle", "case", "cover", "bumper", "folie",
    "schutzfolie", "panzerglas", "schutzglas", "displayschutz", "displayschutzfolie", "ladekabel",
    "ladegeraet", "ladestation", "dockingstation", "kabel", "adapter", "halterung", "tasche",
    "armband", "ersatzteil", "ersatzteile", "skin", "sticker", "aufkleber",
})
_ACCESSORY_SUFFIXES = ("huelle", "schutzfolie", "ladekabel", "ladegeraet", "panzerglas")


_LOT_WORDS = frozenset({"konvolut", "bundle", "sammlung", "paket", "lot", "restposten"})


def _accessory_words(words: list[str]) -> set[str]:
    return {w for w in words if w in _ACCESSORY_WORDS or w.endswith(_ACCESSORY_SUFFIXES)}


_SUFFIXES = frozenset({"ti", "super", "xt", "xtx", "pro", "max", "plus", "ultra", "mini"})
# sellers often leave the brand out: "PS5 Disc" for "Sony PS5", "Mac mini" for "Apple Mac mini"
_OPTIONAL_BRANDS = frozenset({
    "apple", "sony", "samsung", "lenovo", "dell", "hp", "asus", "acer", "msi", "gigabyte", "nvidia",
    "amd", "intel", "microsoft", "nintendo", "bosch", "makita", "dewalt", "dyson", "canon", "nikon",
    "lg", "xiaomi", "google", "huawei", "philips", "logitech", "razer", "corsair", "geforce", "radeon",
})
_COMPONENT_QUERY_RE = re.compile(
    r"\b(?:rtx|gtx|rx|arc|radeon|geforce|ddr\d|ryzen|i[3579]|xeon|threadripper)\b|\b\d{4,5}[a-z]{0,2}\b"
)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text)


def _has_word(words: list[str], squashed: str, token: str) -> bool:
    return any(w.startswith(token) for w in words) or (token.isalnum() and _squash(token) in squashed and any(
        c.isdigit() for c in token))


def comparable_is_relevant(query: str, title: str) -> bool:
    """Is `title` the same kind of item as `query` (not a PC with it inside, a cooler for it,
    the Ti version of it, a broken one or someone looking to buy it)?"""
    q_words = normalize(query).split()
    t_norm = normalize(title)
    t_words = t_norm.split()
    t_squashed = _squash(t_norm)
    if not q_words or not t_words:
        return False
    # every query word must be there (numbers exactly, words by prefix: "grafikkarte" ~ "grafikkarten")
    missing = [w for w in q_words if w not in _OPTIONAL_BRANDS and not _has_word(t_words, t_squashed, w)]
    if missing and (len(q_words) <= 3 or len(missing) > 1 or any(c.isdigit() for m in missing for c in m)):
        return False
    q_set = set(q_words)
    # "rtx 3080" must not match "rtx 3080 ti" / "3080ti"; "iphone 13" not "iphone 13 pro"
    for i, w in enumerate(t_words):
        if any(c.isdigit() for c in w):
            base = re.match(r"^([a-z]*\d+)([a-z]+)?$", w)
            glued = base.group(2) if base else None
            if glued in _SUFFIXES and glued not in q_set and base.group(1) in q_set:
                return False
            if w in q_set and i + 1 < len(t_words) and t_words[i + 1] in _SUFFIXES and t_words[i + 1] not in q_set:
                return False
    if _COMPONENT_QUERY_RE.search(" ".join(q_words)) and not q_set & _BUNDLE_WORDS:
        if set(t_words) & _BUNDLE_WORDS or "gamingpc" in t_squashed:
            return False
    if not q_set & _PART_WORDS and set(t_words) & _PART_WORDS:
        return False
    if not _accessory_words(q_words) and _accessory_words(t_words):
        return False
    if is_wanted_ad(title) or any(f in SEVERE_FLAGS for f in detect_red_flags(title)):
        return False
    return True


def relevant_comparables(query: str, comps: list[Comparable]) -> list[Comparable]:
    """Drop comparables that are a different product (see comparable_is_relevant)."""
    return [c for c in comps if comparable_is_relevant(query, c.title)]


def estimate_from_comparables(
    comps: list[Comparable], *, asking_price_discount: float = 0.85, query: str = ""
) -> PriceEstimate:
    """Median of cleaned comparable prices. Real sales count at face value, asking
    prices are multiplied by `asking_price_discount` (people ask more than they get)."""
    discount = asking_price_discount if asking_price_discount > 0 else 1.0
    usable = [c for c in comps if c.price and c.price > 0]
    pairs = [(c.price if _is_sold(c) else c.price * discount, c) for c in usable]
    kept = _clean_pairs(pairs)
    n = len(kept)
    if n < 3:
        return PriceEstimate(
            source="none",
            sample_size=n,
            query=query,
            comparables=sorted(usable, key=lambda c: c.price)[:COMPS_CAP],
            notes=f"Мало данных: {len(usable)} похожих предложений, после очистки {n} (нужно ≥3)",
        )

    vals = [v for v, _ in kept]
    market = median(vals)
    kept_comps = [c for _, c in kept]
    sold_n = sum(1 for c in kept_comps if _is_sold(c))
    ask_n = n - sold_n
    source = "mixed" if sold_n and ask_n else ("ebay_sold" if sold_n else "kleinanzeigen")

    disc_pct = round((1 - discount) * 100)
    disc_txt = f" (со скидкой {disc_pct}%)" if disc_pct else ""
    ask_txt = f"{ask_n} объявлений{_asking_where(kept_comps)}{disc_txt}"
    if source == "ebay_sold":
        notes = f"Медиана {sold_n} проданных на eBay"
    elif source == "kleinanzeigen":
        notes = f"Медиана {ask_txt}"
    else:
        notes = f"Медиана {sold_n} проданных на eBay и {ask_txt}"
    dropped = len(usable) - n
    if dropped:
        notes += f"; отброшено выбросов: {dropped}"

    if n > COMPS_CAP:  # keep the most typical ones for display
        kept = sorted(kept, key=lambda t: abs(t[0] - market))[:COMPS_CAP]
    return PriceEstimate(
        market_price=round(market, 2),
        low=round(_percentile(vals, 0.25), 2),
        high=round(_percentile(vals, 0.75), 2),
        sample_size=n,
        source=source,
        query=query,
        comparables=sorted((c for _, c in kept), key=lambda c: c.price),
        notes=notes,
    )


HISTORY_HALF_LIFE_DAYS = 14.0  # a price seen two weeks ago counts half as much as today's


def _weighted_quantile(vals: list[float], weights: list[float], q: float) -> float:
    """Quantile of sorted `vals` with weights (midpoint rule; equal weights = plain median)."""
    total = sum(weights)
    if total <= 0:
        return _percentile(vals, q)
    pos: list[float] = []
    acc = 0.0
    for w in weights:
        pos.append((acc + w / 2) / total)
        acc += w
    if q <= pos[0]:
        return vals[0]
    for i in range(1, len(vals)):
        if q <= pos[i]:
            t = (q - pos[i - 1]) / (pos[i] - pos[i - 1]) if pos[i] > pos[i - 1] else 0.0
            return vals[i - 1] + t * (vals[i] - vals[i - 1])
    return vals[-1]


def estimate_from_history(
    query: str,
    points: Sequence[Comparable | tuple[Comparable, datetime]],
    *,
    asking_price_discount: float = 0.85,
    min_points: int = 6,
    days: int | None = None,
    half_life_days: float = HISTORY_HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> PriceEstimate | None:
    """Market price from prices we have already seen (search results, earlier comparables) —
    no extra requests. `points` are comparables, optionally with the time they were last seen:
    fresher prices weigh more (half-life `half_life_days`). Sold prices count at face value,
    asking prices get `asking_price_discount`. None unless at least `min_points` of them are
    the same product as `query`."""
    now = now or utcnow()
    dated = [(p, None) if isinstance(p, Comparable) else (p[0], p[1]) for p in points]
    relevant = [
        (c, seen) for c, seen in dated
        if c.price and c.price > 0 and comparable_is_relevant(query, c.title)
    ]
    if not query or len(relevant) < max(1, min_points):
        return None
    discount = asking_price_discount if asking_price_discount > 0 else 1.0
    kept = _clean_pairs([(c.price if _is_sold(c) else c.price * discount, (c, seen)) for c, seen in relevant])
    n = len(kept)
    if n < 3:
        return None
    half_life = max(half_life_days, 0.1)
    ages = [max(0.0, (now - _aware(seen)).total_seconds() / 86400) if seen else 0.0 for _, (_, seen) in kept]
    weights = [0.5 ** (age / half_life) for age in ages]
    vals = [v for v, _ in kept]
    market = _weighted_quantile(vals, weights, 0.5)
    comps = [c for _, (c, _) in kept]
    sold_n = sum(1 for c in comps if _is_sold(c))
    notes = f"История: медиана {n} {_plural(n, 'цены', 'цен', 'цен')}"
    if days:
        notes += f" за {days} {_plural(days, 'день', 'дня', 'дней')}"
    if sold_n and sold_n < n:
        notes += f" (из них {sold_n} продаж на eBay)"
    elif sold_n:
        notes += " (продажи на eBay)"
    if any(age > 1 for age in ages):
        notes += ", свежие цены весят больше"
    if len(relevant) > n:
        notes += f"; отброшено выбросов: {len(relevant) - n}"
    shown = kept
    if n > COMPS_CAP:  # keep the most typical ones for display
        shown = sorted(kept, key=lambda t: abs(t[0] - market))[:COMPS_CAP]
    return PriceEstimate(
        market_price=round(market, 2),
        low=round(_weighted_quantile(vals, weights, 0.25), 2),
        high=round(_weighted_quantile(vals, weights, 0.75), 2),
        sample_size=n,
        source="history",
        query=query,
        comparables=sorted((c for _, (c, _) in shown), key=lambda c: c.price),
        notes=notes,
        history_days=days,
        age_days=round(sum(a * w for a, w in zip(ages, weights)) / sum(weights), 1),
    )


def _has_phrase(phrase: str, text: str) -> bool:
    """Whole-word match of a normalized phrase in normalized text."""
    return re.search(rf"(?<![^\W_]){re.escape(phrase)}(?![^\W_])", text) is not None


def find_reference_price(title: str, refs: list[ReferencePrice]) -> float | None:
    """Price of the most specific reference whose keywords all appear (as whole words)
    in the title and none of whose `exclude` words do."""
    text = normalize(title)
    best: tuple[int, int] | None = None
    best_price: float | None = None
    for ref in refs:
        kws = [k for k in (normalize(x) for x in ref.keywords) if k]
        if not kws or not all(_has_phrase(k, text) for k in kws):
            continue
        if any(_has_phrase(e, text) for e in (normalize(x) for x in ref.exclude) if e):
            continue
        key = (len(kws), sum(len(k) for k in kws))
        if best is None or key > best:
            best, best_price = key, float(ref.price)
    return best_price


_SOURCE_PRIORITY = {"reference": 0, "ebay_sold": 1, "mixed": 1, "history": 2, "kleinanzeigen": 2, "ai": 3}


def combine_estimates(*estimates: PriceEstimate | None) -> PriceEstimate:
    """Best available estimate: reference > eBay sold/mixed > history/Kleinanzeigen > AI.
    Comparables of all estimates are merged for display."""
    present = [e for e in estimates if e is not None]
    merged: list[Comparable] = []
    seen: set[tuple[str, float]] = set()
    for e in present:
        for c in e.comparables:
            key = (c.url or c.title, round(c.price, 2))
            if key not in seen:
                seen.add(key)
                merged.append(c)
    merged.sort(key=lambda c: c.price)
    query = next((e.query for e in present if e.query), "")
    candidates = [
        e
        for e in present
        if e.market_price is not None and e.market_price > 0 and e.source in _SOURCE_PRIORITY
    ]
    if not candidates:
        return PriceEstimate(
            source="none",
            query=query,
            comparables=merged[:COMPS_CAP],
            notes="Нет данных о рыночной цене",
        )
    best = min(candidates, key=lambda e: (_SOURCE_PRIORITY[e.source], -e.sample_size))
    return best.model_copy(update={"comparables": merged, "query": best.query or query})


# ---------------------------------------------------------------------------
# Listing helpers
# ---------------------------------------------------------------------------


def _listing_text(listing: Listing) -> str:
    """Title first (wanted-ad check uses the first line), then everything descriptive."""
    parts = [listing.title, listing.description, listing.condition, " · ".join(listing.tags)]
    parts += [f"{k}: {v}" for k, v in listing.attributes.items()]
    return "\n".join(p for p in parts if p)


_DELETED_TAGS = frozenset({"geloescht", "anzeige geloescht", "deleted"})


def listing_red_flags(listing: Listing) -> list[str]:
    """Text red flags (title, description, condition, tags) plus structured ones
    (a "Gelöscht" tag, eBay seller rating)."""
    flags = detect_red_flags(_listing_text(listing))
    if any(normalize(t) in _DELETED_TAGS for t in listing.tags) and FLAG_DELETED not in flags:
        flags.append(FLAG_DELETED)
    score, percent = listing.seller_feedback_score, listing.seller_feedback_percent
    if score is not None and score < 10:
        flags.append(FLAG_FEW_REVIEWS)
    elif percent is not None and percent < 97:
        flags.append(FLAG_LOW_RATING)
    return flags


def _is_auction(listing: Listing) -> bool:
    opts = {o.upper() for o in listing.buying_options}
    return "AUCTION" in opts and "FIXED_PRICE" not in opts


def _time_left(listing: Listing, now: datetime) -> timedelta | None:
    if listing.ends_at is None:
        return None
    ends = listing.ends_at
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return ends - now


def _fmt_duration(td: timedelta) -> str:
    minutes = max(0, int(td.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 48 * 60:
        h, m = divmod(minutes, 60)
        return f"{h} ч {m} мин" if m else f"{h} ч"
    return f"{minutes // 1440} дн."


def _pick(override: float | None, default: float) -> float:
    return float(override) if override is not None else float(default)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------


def prefilter(listing: Listing, search: SearchConfig) -> tuple[bool, list[str]]:
    """Cheap checks before any extra HTTP/AI work. Returns (keep, reasons in Russian)."""
    reasons: list[str] = []
    text = f"{listing.title}\n{listing.description}"
    if is_wanted_ad(listing.title):
        reasons.append("Это объявление о поиске/покупке, а не продажа")
    hits = [kw for kw in search.exclude_keywords if kw.strip() and matched_exclude_keyword(text, [kw])]
    if hits:
        reasons.append("Стоп-слова: " + ", ".join(f"«{kw}»" for kw in hits))
    if search.include_keywords and not matches_keywords(text, search.include_keywords, []):
        reasons.append("Нет ключевых слов: " + ", ".join(f"«{kw}»" for kw in search.include_keywords))
    price = listing.price
    if price is not None:
        if search.min_price is not None and price < search.min_price:
            reasons.append(f"Цена {fmt_money(price)} ниже минимума {fmt_money(search.min_price)}")
        if search.max_price is not None and price > search.max_price:
            reasons.append(f"Цена {fmt_money(price)} выше максимума {fmt_money(search.max_price)}")
        if search.purpose == "personal" and search.target_price and price > search.target_price * 1.3:
            reasons.append(
                f"Слишком дорого: {fmt_money(price)} при цели {fmt_money(search.target_price)}"
            )
    return (not reasons, reasons)


def is_auction(listing: Listing) -> bool:
    """eBay auction without "Sofort-Kaufen": the shown price is the current bid."""
    return _is_auction(listing)


def below_min_price(listing: Listing, floor: float | None) -> str | None:
    """Reason to skip a paid ad cheaper than `floor` (junk), or None. Free ads and auctions
    (the current bid will still go up) always pass."""
    price = listing.price
    if not floor or floor <= 0 or listing.is_free or price is None or _is_auction(listing):
        return None
    if price >= floor:
        return None
    return f"Цена {fmt_money(price)} ниже порога {fmt_money(floor)} — мелочь не проверяю"


def history_worthy(listing: Listing) -> bool:
    """May this ad's price go into the price history? Only real offers with a real price."""
    price = listing.price
    if listing.is_free or price is None or not math.isfinite(price) or price <= 1:
        return False
    if _is_auction(listing) or is_wanted_ad(listing.title):
        return False
    if not is_single_item(listing.title, listing.description):
        return False
    return not any(f in SEVERE_FLAGS for f in listing_red_flags(listing))


def _identity() -> object | None:
    """pricing.identity (product identity rules) when it is installed."""
    try:
        from . import identity
    except ImportError:
        return None
    return identity


def is_single_item(title: str, description: str = "") -> bool:
    """One item of one product — not a bundle, a whole PC, a part or an accessory.
    Uses pricing.identity.classify_kind when available, else a word list."""
    identity = _identity()
    classify = getattr(identity, "classify_kind", None)
    if classify is not None:
        try:
            return classify(title, description) == "single"
        except Exception:  # noqa: BLE001 - a helper bug must not break the monitor
            pass
    words = normalize(title).split()
    return not (set(words) & _LOT_WORDS or _accessory_words(words))


def market_says_no_deal(ev: Evaluation) -> bool:
    """Market price and buy price are known and the verdict is "skip" even without AI.
    The AI can't turn that into a deal (it may only lower the market price or add red
    flags), so the ad page and the AI call can be saved."""
    return (
        ev.verdict == "skip"
        and ev.buy_price is not None
        and ev.estimate.market_price is not None
        and ev.estimate.market_price > 0
    )


def no_deal_reasons(ev: Evaluation) -> list[str]:
    """Reasons of an early "no deal" decision, the main one first."""
    reasons = list(ev.reasons)
    main = next((r for r in reasons if r.startswith(
        ("Серьёзные проблемы", "Аукцион уже завершился", "Ставка уже выше", "Дороже твоего бюджета"))), None)
    if main is not None:
        return [main] + [r for r in reasons if r is not main]
    market = fmt_money(ev.estimate.market_price or 0.0)
    profit = ev.expected_profit or 0.0
    head = f"По рынку: ~{market}, выгоды нет" if profit <= 0 else (
        f"По рынку: ~{market}, выгода слишком мала (≈ {fmt_money(profit)})"
    )
    return [head] + reasons


def _ratio_score(ratio: float) -> float:
    """price/value -> 0..100: ≤0.4 → 100, 1.0 → 20, ≥1.3 → 0."""
    if ratio <= 0.4:
        return 100.0
    if ratio <= 1.0:
        return 20.0 + (1.0 - ratio) / 0.6 * 80.0
    return max(0.0, 20.0 * (1.3 - ratio) / 0.3)


def _target_score(ratio: float) -> float:
    """price/target -> 0..100: ≤0.7 → 100, 1.0 → 60, ≥1.3 → 0."""
    if ratio <= 0.7:
        return 100.0
    if ratio <= 1.0:
        return 60.0 + (1.0 - ratio) / 0.3 * 40.0
    return max(0.0, 60.0 * (1.3 - ratio) / 0.3)


def _resale_economics(market: float, pricing: PricingConfig) -> tuple[float, float, float]:
    """(net proceeds, fees, safety margin) of reselling at `market`. Fees are charged on the
    resale price itself (+ the fixed PayPal fee); the safety margin (haggling, risk, time)
    is a separate deduction."""
    margin = market * max(0.0, pricing.safety_margin_percent) / 100
    fees = market * (pricing.selling_fee_percent + pricing.payment_fee_percent) / 100
    fees += max(0.0, pricing.paypal_fixed_fee)
    net = market - margin - fees - pricing.default_shipping_cost
    return net, fees, margin


def _max_item_price(
    market: float | None, search: SearchConfig, pricing: PricingConfig, delivery: float = 0.0
) -> float | None:
    """Highest price (or auction bid) that still meets the profit targets / personal target."""
    min_profit = _pick(search.min_profit, pricing.min_profit)
    min_roi = _pick(search.min_roi, pricing.min_roi)
    if search.purpose == "resale":
        if market is None:
            return None
        net = _resale_economics(market, pricing)[0]
        return min(net - min_profit, net / (1 + min_roi)) - delivery
    if search.target_price:
        return (min(search.target_price, market) if market else search.target_price) - delivery
    if market is not None:
        return market * (1 - min_roi) - delivery
    return None


def _round_offer(value: float) -> float:
    """Offers are round numbers: down to 5 € (cents below 10 €)."""
    if value >= 10:
        return float(5 * math.floor(value / 5))
    return round(max(value, 0.0), 2)


def prefilter_score(
    listing: Listing, estimate: PriceEstimate | None, search: SearchConfig, pricing: PricingConfig
) -> float:
    """0..100 quick guess whether the listing is worth the local-AI time."""
    flags = listing_red_flags(listing)
    if any(f in SEVERE_FLAGS for f in flags):
        return 5.0
    penalty = min(15.0, 5.0 * len(flags))
    auction = _is_auction(listing)
    if listing.is_free:
        base = 85.0
    elif listing.price is None or (listing.price <= 1 and not auction):
        base = 35.0  # unknown price: could be anything
    else:
        cost = listing.price + (listing.shipping_cost or 0.0)
        value = search.reference_price
        if not value and estimate is not None and estimate.market_price:
            value = estimate.market_price
        if not value:
            value = find_reference_price(listing.title, pricing.reference_prices)
        scores: list[float] = []
        if value and auction:
            # the current bid says little: what matters is the room up to our maximum bid
            max_bid = _max_item_price(value, search, pricing)
            scores.append(80.0 * _clamp((max_bid - cost) / max_bid) if max_bid and max_bid > 0 else 0.0)
        elif value:
            scores.append(_ratio_score(cost / value))
        if search.purpose == "personal" and search.target_price:
            scores.append(_target_score(cost / search.target_price) * (0.8 if auction else 1.0))
        base = max(scores) if scores else (32.0 if auction else 40.0)
    return round(_clamp(base - penalty, 0.0, 100.0), 1)


# ---------------------------------------------------------------------------
# Final evaluation
# ---------------------------------------------------------------------------

TOO_CHEAP_RATIO = 0.4  # buy cost < 40 % of the market: too good to be true
WEAK_SCORE_CAP = 65.0  # thin / scattered market data or unchecked photos: no confident alert
AI_ONLY_SCORE_CAP = 55.0  # market price only guessed by the AI
_SAMPLE_SOURCES = frozenset({"ebay_sold", "mixed", "kleinanzeigen", "history"})

FLAG_AI_BUNDLE = "ИИ: это комплект/набор, а не один товар"
FLAG_AI_PC = "ИИ: это целый компьютер, а не комплектующая"
FLAG_AI_LAPTOP = "ИИ: это ноутбук, а не комплектующая"
FLAG_AI_PART = "ИИ: это запчасть, а не товар целиком"
FLAG_AI_ACCESSORY = "ИИ: это аксессуар, а не сам товар"
_AI_ITEM_FLAGS = {
    "bundle": FLAG_AI_BUNDLE,
    "complete_pc": FLAG_AI_PC,
    "laptop": FLAG_AI_LAPTOP,
    "part": FLAG_AI_PART,
    "accessory": FLAG_AI_ACCESSORY,
    "box_only": FLAG_BOX_ONLY,
    "wanted": FLAG_WANTED,
}
_AI_VETO_FLAGS = frozenset(_AI_ITEM_FLAGS.values())


def _is_severe(flag: str) -> bool:
    return flag in SEVERE_FLAGS or flag in _AI_VETO_FLAGS


def _spread(est: PriceEstimate) -> float | None:
    """Interquartile range of the comparables relative to the market price."""
    if est.market_price and est.market_price > 0 and est.low is not None and est.high is not None:
        return max(0.0, (est.high - est.low) / est.market_price)
    return None


def _market_confidence(est: PriceEstimate) -> float:
    """0..1: how much the market price can be trusted (source, sample size, spread, age)."""
    if est.source == "reference":
        return 0.9
    if est.source == "ai":
        return 0.35
    if est.source in ("ebay_sold", "mixed"):
        c = min(1.0, est.sample_size / 10)
    elif est.source in ("kleinanzeigen", "history"):
        c = min(1.0, est.sample_size / 10) * 0.8  # asking prices
    else:
        return 0.0
    spread = _spread(est)
    if spread is not None and spread > 0.25:
        c *= max(0.5, 1.0 - (spread - 0.25))
    if est.source == "history" and est.age_days:
        c *= 0.6 + 0.4 * 0.5 ** (est.age_days / HISTORY_HALF_LIFE_DAYS)
    return c


def _ai_support(ai: AIVerdict | None) -> float:
    """How strongly the AI backs the deal: buy → confidence, maybe → 0.6×, skip → 0."""
    if ai is None:
        return 0.0
    factor = {"buy": 1.0, "maybe": 0.6, "skip": 0.0}[ai.verdict]
    return factor * _clamp(ai.confidence)


def _confidence(est: PriceEstimate, ai: AIVerdict | None) -> float:
    c = _market_confidence(est)
    if ai is not None and ai.confidence > 0:  # confidence 0 = AI gave no real answer
        c = 0.5 * c + 0.5 * _ai_support(ai)
    return c


def _weak_evidence(est: PriceEstimate, pricing: PricingConfig) -> str | None:
    """Why the market data is too thin or too scattered for a "buy", or None."""
    if est.source not in _SAMPLE_SOURCES or est.market_price is None:
        return None
    n, need = est.sample_size, max(1, pricing.min_comparables)
    if n < need:
        return f"⚠ Мало данных о рынке: {n} {_plural(n, 'цена', 'цены', 'цен')} (нужно ≥ {need})"
    spread = _spread(est)
    if spread is not None and spread > pricing.max_price_spread:
        return (f"⚠ Цены похожих сильно разбросаны ({fmt_money(est.low or 0)}–{fmt_money(est.high or 0)})"
                " — рынок неясен")
    return None


def _resale_strength(profit: float, roi: float, min_profit: float, min_roi: float) -> float:
    """0..100 economics score: exactly 70 at the user's min_profit/min_roi, 100 at 3× them,
    proportionally less below, ≤ 10 for a loss."""
    if profit <= 0:
        return _clamp(10.0 + 10.0 * profit / min_profit, 0.0, 10.0)
    r = min(profit / min_profit, roi / min_roi)
    if r >= 1.0:
        return min(100.0, 70.0 + 15.0 * (r - 1.0))
    return 70.0 * r


def _personal_strength(roi: float, under_target: bool, min_roi: float) -> float:
    if under_target or roi >= min_roi:
        return 70.0 + 30.0 * _clamp(max(roi, 0.0) / (2 * min_roi))
    if roi > 0:
        return 35.0 + 30.0 * _clamp(roi / min_roi)
    return _clamp(20.0 + 40.0 * roi, 0.0, 20.0)


def _basis_reason(est: PriceEstimate) -> str | None:
    n = est.sample_size
    if est.source == "ebay_sold":
        return f"Оценка по {n} проданным на eBay"
    if est.source == "kleinanzeigen":
        return f"Оценка по {n} объявлениям (цены предложений со скидкой)"
    if est.source == "mixed":
        return f"Оценка по {n} продажам на eBay и объявлениям"
    if est.source == "history":
        days = est.history_days
        period = f" за {days} {_plural(days, 'день', 'дня', 'дней')}" if days else ""
        return f"История: {n} {_plural(n, 'цена', 'цены', 'цен')}{period}"
    if est.source == "reference" and est.market_price:
        return f"Рыночная цена из настроек: {fmt_money(est.market_price)}"
    if est.source == "ai":
        return "Рыночная цена — оценка ИИ, не подтверждена объявлениями"
    return None


def _cap_verdict(verdict: str, cap: str) -> str:
    return verdict if _VERDICT_RANK[verdict] <= _VERDICT_RANK[cap] else cap


# laptop product lines: "ThinkPad T480 i5" or "MacBook Pro 2020" are laptops, not components
_LAPTOP_LINES = frozenset({
    "thinkpad", "macbook", "xps", "latitude", "elitebook", "probook", "zbook", "zenbook", "vivobook",
    "expertbook", "legion", "ideapad", "yoga", "surface", "chromebook", "inspiron", "vostro", "precision",
    "pavilion", "omen", "envy", "spectre", "predator", "aspire", "swift", "matebook", "galaxybook", "gram",
})


def _component_search(listing: Listing, search: SearchConfig, est: PriceEstimate) -> bool:
    """Are we pricing a PC component (GPU, CPU, RAM...)? Then a laptop is the wrong item."""
    query = normalize(search.query or est.query or make_search_query(listing.title))
    words = set(query.split())
    return bool(_COMPONENT_QUERY_RE.search(query)) and not words & (_BUNDLE_WORDS | _LAPTOP_LINES)


def _ai_structured_flags(
    ai: AIVerdict, listing: Listing, search: SearchConfig, est: PriceEstimate
) -> tuple[list[str], list[str]]:
    """Hard vetoes from the AI's structured extraction: (flags, extra reasons)."""
    flags: list[str] = []
    reasons: list[str] = []
    label = _AI_ITEM_FLAGS.get(ai.item_type)
    if label and ai.confidence >= 0.5 and (ai.item_type != "laptop" or _component_search(listing, search, est)):
        flags.append(label)
    if ai.locked:
        flags.append(FLAG_LOCKED)
    defects = [d.strip() for d in ai.defects if d and d.strip()]
    if defects:
        flags.append(FLAG_DEFECT)
        reasons.append("ИИ видит дефекты: " + "; ".join(defects[:4]))
    return flags, reasons


def evaluate(
    listing: Listing,
    estimate: PriceEstimate,
    ai: AIVerdict | None,
    search: SearchConfig,
    pricing: PricingConfig,
    *,
    now: datetime | None = None,
    ai_expected: bool = False,
) -> Evaluation:
    """Profit/ROI (resale) or savings (personal), verdict, action, 0..100 score and Russian
    reasons. `ai_expected`: the AI is enabled for this ad — if it gave no answer, the photos
    were not checked and the ad can't be a "buy"."""
    now = now or utcnow()
    purpose = search.purpose
    reasons: list[str] = []
    min_profit = _pick(search.min_profit, pricing.min_profit)
    min_roi = _pick(search.min_roi, pricing.min_roi)
    mp_eff, mr_eff = max(min_profit, 1.0), max(min_roi, 0.01)  # avoid /0 in the score
    ai_answered = ai is not None and ai.confidence > 0

    # --- market value ----------------------------------------------------------------
    est = estimate if estimate is not None else PriceEstimate()
    if search.reference_price:
        est = est.model_copy(
            update={
                "market_price": float(search.reference_price),
                "low": None,
                "high": None,
                "source": "reference",
                "notes": "Рыночная цена задана в настройках поиска",
            }
        )
    elif not est.market_price and ai is not None and ai.estimated_market_price:
        est = est.model_copy(
            update={
                "market_price": float(ai.estimated_market_price),
                "low": None,
                "high": None,
                "sample_size": 0,
                "source": "ai",
                "notes": "Оценка ИИ, не подтверждена объявлениями",
            }
        )
    market = est.market_price if est.market_price and est.market_price > 0 else None

    # --- red flags -------------------------------------------------------------------
    flags = listing_red_flags(listing)
    ai_reasons: list[str] = []
    if ai is not None:
        for f in ai.red_flags:
            f = f.strip()
            if f and f not in flags:
                flags.append(f)
        if ai.condition == "defective" and FLAG_DEFECT not in flags:
            flags.append(FLAG_DEFECT)
        if ai_answered:
            extra, ai_reasons = _ai_structured_flags(ai, listing, search, est)
            flags += [f for f in extra if f not in flags]

    # --- auction ---------------------------------------------------------------------
    auction = _is_auction(listing)
    left = _time_left(listing, now) if auction else None
    ended = left is not None and left.total_seconds() <= 0
    ends_soon = left is not None and not ended and left <= AUCTION_SOON

    # --- what we pay -----------------------------------------------------------------
    delivery = listing.shipping_cost if listing.shipping_cost and listing.shipping_cost > 0 else 0.0
    placeholder = False
    item_price: float | None
    if listing.is_free:
        item_price = 0.0
    elif listing.price is not None and listing.price <= 1 and not auction:
        item_price, placeholder = None, True  # "1 € VB" is a lure, not a price
    else:
        item_price = listing.price
    buy_cost = None if item_price is None else round(item_price + delivery, 2)

    too_cheap = (
        market is not None and buy_cost is not None and not listing.is_free and not auction
        and buy_cost < TOO_CHEAP_RATIO * market
    )
    if too_cheap:
        # far below market and the seller won't meet: the classic "Nur Versand" scam
        flags.append(FLAG_TOO_GOOD if is_remote_only(_listing_text(listing)) else FLAG_TOO_CHEAP)
    severe = [f for f in flags if _is_severe(f)]
    soft = [f for f in flags if not _is_severe(f) and f != FLAG_TOO_CHEAP]
    conf = _confidence(est, ai)
    factor = min(1.0, 0.4 + conf)  # score scales with how sure we are about the market
    support = _ai_support(ai)

    # --- resale economics / max price ---------------------------------------------------
    fees = resale_ship = 0.0
    net: float | None = None
    if market is not None and purpose == "resale":
        net, fees, _ = _resale_economics(market, pricing)
        resale_ship = pricing.default_shipping_cost
    max_buy = _max_item_price(market, search, pricing, delivery)
    if max_buy is not None:
        max_buy = float(math.floor(max_buy)) if max_buy >= 10 else round(max(max_buy, 0.0), 2)

    profit: float | None = None
    roi: float | None = None
    target = search.target_price if purpose == "personal" else None
    negotiable = listing.negotiable or "BEST_OFFER" in {o.upper() for o in listing.buying_options}
    action = ""
    offer: float | None = None
    no_alert = False
    cap = 100.0

    # --- base verdict + score --------------------------------------------------------------
    if buy_cost is None:
        if placeholder and listing.price is not None:
            reasons.append(f"Цена {fmt_money(listing.price)} похожа на заглушку — уточни у продавца")
        else:
            reasons.append("Цена не указана — уточни у продавца")
        if market is not None:
            reasons.append(f"Рынок ≈ {fmt_money(market)}")
        ok_ai = ai is not None and ai.verdict in ("buy", "maybe") and ai.confidence >= 0.5
        verdict = "maybe" if ok_ai else "skip"
        score = min(40.0, 15.0 * _market_confidence(est) + 25.0 * support)
    elif market is None:
        reasons.append("Не хватило данных для оценки рыночной цены")
        ok_ai = ai is not None and ai.verdict in ("buy", "maybe") and ai.confidence > 0
        verdict = "maybe" if ok_ai else "skip"
        score = 30.0 * support
        if target and buy_cost <= target and not auction:
            verdict = "maybe"
            score += 25.0
            reasons.append(f"Для себя: в пределах цели ≤ {fmt_money(target)}")
        score = min(45.0, score)
    elif auction:
        # the current bid is not the price: never compute profit from it, never "buy"
        bid = item_price or 0.0
        reasons.append(f"Текущая ставка {fmt_money(bid)}, рынок ≈ {fmt_money(market)}")
        if max_buy is None or max_buy <= 0 or bid >= max_buy:
            verdict = "skip"
            limit = f" {fmt_money(max_buy)}" if max_buy and max_buy > 0 else ""
            reasons.append(f"Ставка уже выше разумного максимума{limit} — выгоды не будет")
            score = 10.0
        else:
            verdict, action = "maybe", "bid"
            reasons.append(f"Аукцион: ставь максимум {fmt_money(max_buy)} (сейчас {fmt_money(bid)})")
            score = 60.0 * _clamp((max_buy - bid) / max_buy) * factor
        cap = min(cap, 60.0)
        basis = _basis_reason(est)
        if basis:
            reasons.append(basis)
    else:
        diff = (market - buy_cost) / market
        price_txt = f"Цена {fmt_money(item_price or 0.0)}"
        if delivery:
            price_txt += f" + доставка {fmt_money(delivery)}"
        if listing.is_free:
            reasons.append(f"Отдают бесплатно — рынок ≈ {fmt_money(market)}")
        elif diff >= 0.005:
            reasons.append(f"{price_txt} — на {_pct(diff)} ниже рынка (~{fmt_money(market)})")
        elif diff <= -0.005:
            reasons.append(f"{price_txt} — на {_pct(-diff)} выше рынка (~{fmt_money(market)})")
        else:
            reasons.append(f"{price_txt} — на уровне рынка (~{fmt_money(market)})")

        if purpose == "resale":
            assert net is not None
            profit = net - buy_cost
            roi = profit / max(buy_cost, 1.0)
            if profit >= 0:
                txt = f"Чистая прибыль ≈ {fmt_money(profit)}"
                txt += " (вещь бесплатная)" if listing.is_free else f" (ROI {_pct(roi)})"
                if fees or resale_ship:
                    txt += f", учтены комиссии {fmt_money(fees)} и отправка {fmt_money(resale_ship)}"
                reasons.append(txt)
            else:
                reasons.append(f"Убыток ≈ {fmt_money(-profit)} — перепродавать невыгодно")
            if profit >= min_profit and roi >= min_roi:
                verdict = "buy"
            elif profit > 0 and (profit >= 0.5 * min_profit or roi >= 0.5 * min_roi):
                verdict = "maybe"
            else:
                verdict = "skip"
            strength = _resale_strength(profit, roi, mp_eff, mr_eff)
        else:
            profit = market - buy_cost
            roi = profit / market
            under_target = target is not None and buy_cost <= target
            if target:
                if under_target:
                    reasons.append(
                        f"Для себя: экономия {fmt_money(profit)} (цель ≤ {fmt_money(target)})"
                        if profit >= 0
                        else f"Для себя: в пределах цели ≤ {fmt_money(target)}, но дороже рынка"
                    )
                else:
                    reasons.append(f"Выше цели: {fmt_money(buy_cost)} > {fmt_money(target)}")
            elif profit >= 0:
                reasons.append(f"Для себя: экономия {fmt_money(profit)} ({_pct(roi)} от рынка)")
            else:
                reasons.append(f"Для себя: дороже рынка на {fmt_money(-profit)}")
            if under_target or roi >= min_roi:
                verdict = "buy"
                if roi < -0.1:  # within target, but clearly cheaper elsewhere
                    verdict = "maybe"
                    reasons.append("Дороже рынка — можно найти дешевле")
            elif (target and buy_cost <= target * 1.15) or roi > 0:
                verdict = "maybe"
            else:
                verdict = "skip"
            strength = _personal_strength(roi, under_target, mr_eff)
            if verdict == "maybe":
                strength = min(strength, 60.0)

        # VB / Preisvorschlag: a small haggle turns it into a deal
        if (
            verdict != "buy" and negotiable and max_buy is not None and max_buy > 0
            and item_price is not None and item_price > max_buy >= item_price * (1 - pricing.vb_expected_discount)
        ):
            offer = _round_offer(max_buy)
            offer_cost = offer + delivery
            verdict, action = "buy", "haggle"
            if purpose == "resale":
                assert net is not None
                o_profit = net - offer_cost
                strength = _resale_strength(o_profit, o_profit / max(offer_cost, 1.0), mp_eff, mr_eff)
                reasons.append(f"Торгуйся: предложи {fmt_money(offer)} — тогда прибыль ≈ {fmt_money(o_profit)}")
            else:
                o_save = market - offer_cost
                strength = _personal_strength(o_save / market, bool(target and offer_cost <= target), mr_eff)
                reasons.append(f"Торгуйся: предложи {fmt_money(offer)} — экономия ≈ {fmt_money(o_save)}")
        score = strength * factor

        basis = _basis_reason(est)
        if basis:
            reasons.append(basis)
        if "осторожную оценку" in (est.notes or ""):
            reasons.append("⚠ " + est.notes.split(". ")[-1])

    # --- negotiation hint ------------------------------------------------------------------
    if max_buy is not None and max_buy > 0 and not auction and action != "haggle":
        if negotiable or buy_cost is None:
            reasons.append(f"Торгуйся: выгодно до {fmt_money(max_buy)}")
            if verdict == "maybe" and item_price is not None and item_price > max_buy:
                action, offer = "haggle", _round_offer(max_buy)
        elif verdict != "buy" and item_price is not None and item_price > max_buy:
            reasons.append(f"Выгодно только при цене до {fmt_money(max_buy)}")

    # --- budget ----------------------------------------------------------------------------
    if pricing.max_capital and verdict != "skip":
        pay = (offer + delivery) if action == "haggle" and offer is not None else buy_cost
        if pay is not None and pay > pricing.max_capital:
            verdict = "skip"
            cap = min(cap, 30.0)
            reasons.append(f"Дороже твоего бюджета ({fmt_money(pricing.max_capital)})")

    # --- caps ----------------------------------------------------------------------------------
    if auction:
        bids = listing.bid_count
        info = "Аукцион"
        if bids is not None:
            info += f": {bids} {_plural(bids, 'ставка', 'ставки', 'ставок')}" if bids else ": ставок пока нет"
        if left is not None and not ended:
            info += f", до конца {_fmt_duration(left)}"
        reasons.append(info)
        verdict = _cap_verdict(verdict, "maybe")
        if ended:
            verdict, cap = "skip", min(cap, 20.0)
            reasons.append("Аукцион уже завершился")
        elif not ends_soon:
            reasons.append("Аукцион ещё идёт — итоговая цена будет выше")

    weak = _weak_evidence(est, pricing) if buy_cost is not None else None
    if weak and verdict != "skip":
        verdict = _cap_verdict(verdict, "maybe")
        cap = min(cap, WEAK_SCORE_CAP)
        reasons.append(weak)
    if est.source == "ai" and market is not None:
        verdict = _cap_verdict(verdict, "maybe")
        cap = min(cap, AI_ONLY_SCORE_CAP)
        no_alert = True
        reasons.append("⚠ Рынок оценён только ИИ — проверь цены сам")
    if too_cheap and FLAG_TOO_GOOD not in flags:
        verdict = _cap_verdict(verdict, "maybe")
        cap = min(cap, 60.0)
        action = "watch"
        reasons.append("⚠ Подозрительно дёшево — проверь, не развод")
    if FLAG_RESERVED in flags:
        verdict = _cap_verdict(verdict, "maybe")
        cap = min(cap, 60.0)
        no_alert = True
        action = "watch"

    if ai is not None:
        if ai.confidence > 0:
            line = f"ИИ: {_VERDICT_RU[ai.verdict]} (уверенность {_pct(_clamp(ai.confidence))})"
        else:
            line = "ИИ: нет уверенного ответа"
        if ai.reasoning:
            text = " ".join(ai.reasoning.split())
            line += " — " + (text[:220].rstrip() + "…" if len(text) > 220 else text)
        reasons.append(line)
        reasons += ai_reasons
        if ai.photo_matches_description is False:
            reasons.append("⚠ Фото не совпадает с описанием")
            verdict = _cap_verdict(verdict, "maybe")
            cap = min(cap, 60.0)
        if ai_answered and ai.stock_photos:
            reasons.append("⚠ Фото похожи на каталожные / из интернета")
            verdict = _cap_verdict(verdict, "maybe")
            cap = min(cap, 60.0)
        if ai.verdict == "skip" and ai.confidence >= 0.6:
            verdict = "skip"
            cap = min(cap, 25.0)

    ai_checked: bool | None = True if ai_answered else (False if ai_expected else None)
    if ai_checked is False and verdict == "buy":
        verdict = "maybe"
        cap = min(cap, WEAK_SCORE_CAP)
        reasons.append("⚠ Фото НЕ проверены ИИ (нейросеть не ответила)")

    if severe:
        verdict = "skip"
        cap = min(cap, 20.0)
        reasons.append("Серьёзные проблемы: " + ", ".join(severe))
    if soft:
        reasons.append("Обратить внимание: " + ", ".join(soft))
        score -= min(9.0, 3.0 * len(soft))

    if verdict == "skip":
        action = "skip"
    elif auction:
        action = "bid" if action == "bid" else "watch"
    elif not action:
        action = "buy" if verdict == "buy" else "watch"
    if action != "haggle":
        offer = None

    score = round(_clamp(min(score, cap), 0.0, 100.0), 1)
    return Evaluation(
        ad_id=listing.ad_id,
        purpose=purpose,
        buy_price=buy_cost,
        estimate=est,
        ai=ai,
        max_buy_price=max_buy,
        action=action,  # type: ignore[arg-type]
        offer_price=offer,
        ai_checked=ai_checked,
        no_alert=no_alert,
        fees=round(fees, 2),
        shipping_cost=round(resale_ship, 2),
        expected_profit=None if profit is None else round(profit, 2),
        roi=None if roi is None else round(roi, 4),
        score=score,
        verdict=verdict,  # type: ignore[arg-type]
        reasons=reasons,
        red_flags=flags,
    )
