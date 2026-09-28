"""Market value estimation from comparables and deal scoring (profit, ROI, verdict)."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import TypeVar

from ..config import PricingConfig, ReferencePrice, SearchConfig
from ..models import AIVerdict, Comparable, Evaluation, Listing, PriceEstimate, utcnow
from .text import (
    FLAG_DEFECT,
    SEVERE_FLAGS,
    detect_red_flags,
    is_wanted_ad,
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


_SOURCE_PRIORITY = {"reference": 0, "ebay_sold": 1, "mixed": 1, "kleinanzeigen": 2, "ai": 3}


def combine_estimates(*estimates: PriceEstimate | None) -> PriceEstimate:
    """Best available estimate: reference > eBay sold/mixed > Kleinanzeigen > AI.
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
    parts = [listing.title, listing.description, listing.condition]
    parts += [f"{k}: {v}" for k, v in listing.attributes.items()]
    return "\n".join(p for p in parts if p)


def listing_red_flags(listing: Listing) -> list[str]:
    """Text red flags plus structured ones (eBay seller rating)."""
    flags = detect_red_flags(_listing_text(listing))
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
        if value:
            scores.append(_ratio_score(cost / value))
        if search.purpose == "personal" and search.target_price:
            scores.append(_target_score(cost / search.target_price))
        base = max(scores) if scores else 40.0
        if auction:
            base *= 0.8  # current bid will still go up
    return round(_clamp(base - penalty, 0.0, 100.0), 1)


# ---------------------------------------------------------------------------
# Final evaluation
# ---------------------------------------------------------------------------


def _market_confidence(est: PriceEstimate) -> float:
    if est.source == "reference":
        return 0.9
    if est.source in ("ebay_sold", "mixed"):
        return min(1.0, est.sample_size / 10)
    if est.source == "kleinanzeigen":
        return min(1.0, est.sample_size / 10) * 0.8
    if est.source == "ai":
        return 0.35
    return 0.0


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


def _basis_reason(est: PriceEstimate) -> str | None:
    n = est.sample_size
    if est.source == "ebay_sold":
        return f"Оценка по {n} проданным на eBay"
    if est.source == "kleinanzeigen":
        return f"Оценка по {n} объявлениям (цены предложений со скидкой)"
    if est.source == "mixed":
        return f"Оценка по {n} продажам на eBay и объявлениям"
    if est.source == "reference" and est.market_price:
        return f"Рыночная цена из настроек: {fmt_money(est.market_price)}"
    if est.source == "ai":
        return "Рыночная цена — оценка ИИ, не подтверждена объявлениями"
    return None


def _cap_verdict(verdict: str, cap: str) -> str:
    return verdict if _VERDICT_RANK[verdict] <= _VERDICT_RANK[cap] else cap


def evaluate(
    listing: Listing,
    estimate: PriceEstimate,
    ai: AIVerdict | None,
    search: SearchConfig,
    pricing: PricingConfig,
    *,
    now: datetime | None = None,
) -> Evaluation:
    """Profit/ROI (resale) or savings (personal), verdict, 0..100 score and Russian reasons."""
    now = now or utcnow()
    purpose = search.purpose
    reasons: list[str] = []
    min_profit = _pick(search.min_profit, pricing.min_profit)
    min_roi = _pick(search.min_roi, pricing.min_roi)
    mp_eff, mr_eff = max(min_profit, 1.0), max(min_roi, 0.01)  # avoid /0 in the score

    # --- red flags -------------------------------------------------------------------
    flags = listing_red_flags(listing)
    if ai is not None:
        for f in ai.red_flags:
            f = f.strip()
            if f and f not in flags:
                flags.append(f)
        if ai.condition == "defective" and FLAG_DEFECT not in flags:
            flags.append(FLAG_DEFECT)

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

    if (
        market is not None
        and buy_cost is not None
        and not listing.is_free
        and not auction
        and buy_cost < 0.3 * market
    ):
        flags.append(FLAG_TOO_CHEAP)
    severe = [f for f in flags if f in SEVERE_FLAGS]
    soft = [f for f in flags if f not in SEVERE_FLAGS]
    conf = _confidence(est, ai)
    support = _ai_support(ai)
    ai_buy_strong = ai is not None and ai.verdict == "buy" and ai.confidence >= 0.7

    # --- resale economics / max price ---------------------------------------------------
    fees = resale_ship = 0.0
    net: float | None = None
    if market is not None and purpose == "resale":
        resale_value = market * (1 - pricing.safety_margin_percent / 100)
        fees = resale_value * (pricing.selling_fee_percent + pricing.payment_fee_percent) / 100
        resale_ship = pricing.default_shipping_cost
        net = resale_value - fees - resale_ship

    max_buy: float | None = None
    if purpose == "resale":
        if net is not None:
            max_buy = min(net - min_profit, net / (1 + min_roi)) - delivery
    elif search.target_price:
        max_buy = (min(search.target_price, market) if market else search.target_price) - delivery
    elif market is not None:
        max_buy = market * (1 - min_roi) - delivery
    if max_buy is not None:
        max_buy = float(math.floor(max_buy)) if max_buy >= 10 else round(max(max_buy, 0.0), 2)

    profit: float | None = None
    roi: float | None = None
    target = search.target_price if purpose == "personal" else None
    under_target = False

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
        if target and buy_cost <= target:
            under_target = True
            verdict = "maybe"
            score += 25.0
            reasons.append(f"Для себя: в пределах цели ≤ {fmt_money(target)}")
        score = min(45.0, score)
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
            if (
                verdict == "maybe"
                and ai_buy_strong
                and profit >= 0.8 * min_profit
                and roi >= 0.8 * min_roi
            ):
                verdict = "buy"
            score = (
                45.0 * _clamp(roi / (2 * mr_eff))
                + 35.0 * _clamp(profit / (3 * mp_eff))
                + 20.0 * conf
            )
            if profit <= 0:
                score = min(score, 20.0)
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
            score = (
                55.0 * _clamp(roi / (2 * mr_eff))
                + 25.0 * (1.0 if under_target else 0.0)
                + 20.0 * conf
            )

        basis = _basis_reason(est)
        if basis:
            reasons.append(basis)
        if "осторожную оценку" in (est.notes or ""):
            reasons.append("⚠ " + est.notes.split(". ")[-1])

    # AI-only market price is a guess: "buy" needs the AI to be sure as well
    if est.source == "ai" and verdict == "buy" and not ai_buy_strong:
        verdict = "maybe"

    # --- negotiation / bidding hint ----------------------------------------------------------
    best_offer = listing.negotiable or "BEST_OFFER" in {o.upper() for o in listing.buying_options}
    if max_buy is not None and max_buy > 0:
        if auction:
            reasons.append(f"Аукцион: ставь максимум {fmt_money(max_buy)}")
        elif best_offer or buy_cost is None:
            reasons.append(f"Торгуйся: выгодно до {fmt_money(max_buy)}")
        elif verdict != "buy" and item_price is not None and item_price > max_buy:
            reasons.append(f"Выгодно только при цене до {fmt_money(max_buy)}")

    # --- caps ----------------------------------------------------------------------------------
    cap = 100.0
    if auction:
        bids = listing.bid_count
        info = "Аукцион"
        if bids is not None:
            info += f": {bids} {_plural(bids, 'ставка', 'ставки', 'ставок')}" if bids else ": ставок пока нет"
        if left is not None and not ended:
            info += f", до конца {_fmt_duration(left)}"
        reasons.append(info)
        if ended:
            verdict, cap = "skip", min(cap, 20.0)
            reasons.append("Аукцион уже завершился")
        elif not ends_soon:
            verdict = _cap_verdict(verdict, "maybe")
            cap = min(cap, 60.0)
            reasons.append("Аукцион ещё идёт — итоговая цена будет выше")

    if ai is not None:
        if ai.confidence > 0:
            line = f"ИИ: {_VERDICT_RU[ai.verdict]} (уверенность {_pct(_clamp(ai.confidence))})"
        else:
            line = "ИИ: нет уверенного ответа"
        if ai.reasoning:
            text = " ".join(ai.reasoning.split())
            line += " — " + (text[:220].rstrip() + "…" if len(text) > 220 else text)
        reasons.append(line)
        if ai.photo_matches_description is False:
            reasons.append("⚠ Фото не совпадает с описанием")
            verdict = _cap_verdict(verdict, "maybe")
            cap = min(cap, 60.0)
        if ai.verdict == "skip" and ai.confidence >= 0.6:
            verdict = "skip"
            cap = min(cap, 25.0)

    if severe:
        verdict = "skip"
        cap = min(cap, 20.0)
        reasons.append("Серьёзные проблемы: " + ", ".join(severe))
    if soft:
        reasons.append("Обратить внимание: " + ", ".join(soft))
        score -= min(9.0, 3.0 * len(soft))

    score = round(_clamp(min(score, cap), 0.0, 100.0), 1)
    return Evaluation(
        ad_id=listing.ad_id,
        purpose=purpose,
        buy_price=buy_cost,
        estimate=est,
        ai=ai,
        max_buy_price=max_buy,
        fees=round(fees, 2),
        shipping_cost=round(resale_ship, 2),
        expected_profit=None if profit is None else round(profit, 2),
        roi=None if roi is None else round(roi, 4),
        score=score,
        verdict=verdict,  # type: ignore[arg-type]
        reasons=reasons,
        red_flags=flags,
    )
