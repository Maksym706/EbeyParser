"""Deals feed, detail, pipeline (Избранное → Написал → Купил → Продал), market history,
today's summary, statistics and CSV export."""

from __future__ import annotations

import base64
import csv
import inspect
import io
import logging
import math
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from ...models import utcnow
from ...timefmt import local_tz, to_local
from .context import ApiContext, get_ctx, parse_since
from .errors import ApiError, validation_error
from .presenters import (
    ACTION_LABELS,
    API_STATUSES,
    COMP_SOURCE_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    VERDICT_LABELS,
    aware,
    deal_card,
    deal_detail,
    iso,
    market_price,
    offer_terms,
    realized_profit,
    rnd,
    search_ids,
)
from .routes_monitor import accepted
from .schemas import ERRORS, DealPatchIn, DealsPage, ReevaluateIn, SeenIn

log = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_LIMIT = 30
MAX_LIMIT = 200
VERDICTS = ("buy", "maybe", "skip", "none")
ACTIONS = ("buy", "haggle", "bid", "watch", "skip")
GOOD_ACTIONS = ["buy", "haggle", "bid"]
SORTS = ("best", "score", "fresh", "newest", "profit", "price", "roi", "distance", "ending", "updated")


# ------------------------------------------------------------------ filters
def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


def _number(params: Any, key: str, errors: dict[str, str], low: float | None = None) -> float | None:
    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        errors[key] = "нужно число"
        return None
    if not math.isfinite(value) or (low is not None and value < low):
        errors[key] = f"не меньше {low:g}" if low is not None else "нужно число"
        return None
    return value


def deal_filters(ctx: ApiContext, params: Any) -> tuple[dict[str, Any], str]:
    """Query parameters -> db.find_deals filters + sort; 422 with {field: message} when wrong."""
    errors: dict[str, str] = {}
    filters: dict[str, Any] = {}
    verdict = _csv(params.get("verdict")) or ["good"]
    if verdict == ["all"]:
        pass
    else:
        wanted: list[str] = []
        for v in verdict:
            if v == "good":
                wanted += ["buy", "maybe"]
            elif v in VERDICTS:
                wanted.append(v)
            else:
                errors["verdict"] = "good, buy, maybe, skip, none или all"
        filters["verdicts"] = list(dict.fromkeys(wanted))
    actions = _csv(params.get("action"))
    if any(a not in ACTIONS for a in actions):
        errors["action"] = "buy, haggle, bid, watch или skip"
    elif actions:
        filters["actions"] = actions
    status = _csv(params.get("status"))
    if status == ["any"]:
        filters["include_ignored"] = True
    elif status and status != ["active"]:
        if any(s not in API_STATUSES for s in status):
            errors["status"] = ", ".join(API_STATUSES) + ", active или any"
        else:
            filters["statuses"] = status
    purpose = params.get("purpose") or ""
    if purpose:
        if purpose not in ("resale", "personal"):
            errors["purpose"] = "resale или personal"
        filters["purpose"] = purpose
    source = params.get("source") or ""
    if source:
        if source not in ("kleinanzeigen", "ebay"):
            errors["source"] = "kleinanzeigen или ebay"
        filters["source"] = source
    searches = _csv(params.get("search"))
    if searches:
        by_id = dict(zip(search_ids(ctx.config.searches), (s.name for s in ctx.config.searches)))
        filters["search_names"] = [by_id.get(s, s) for s in searches]
    q = (params.get("q") or "").strip()[:200]
    if q:
        filters["q"] = q
    for key, target, low in (("min_score", "min_score", 0), ("min_profit", "min_profit", None),
                             ("min_price", "min_price", 0), ("max_price", "max_price", 0), ("max_km", "max_km", 0)):
        value = _number(params, key, errors, low)
        if value is not None:
            filters[target] = value
    if filters.get("min_price") is not None and filters.get("max_price") is not None \
            and filters["min_price"] > filters["max_price"]:
        errors["min_price"] = "«Цена от» больше, чем «Цена до»"
    shipping = _bool(params.get("shipping"))
    if shipping is not None:
        filters["shipping"] = shipping
    if _bool(params.get("no_flags")):
        filters["no_flags"] = True
    if _bool(params.get("unseen")):
        filters["unseen"] = True
    if _bool(params.get("actionable")):
        filters["actionable"] = True
    ai_checked = _bool(params.get("ai_checked"))
    if ai_checked is not None:
        filters["ai_checked"] = ai_checked
    since_raw = params.get("since")
    if since_raw:
        since = parse_since(since_raw)
        if since is None and since_raw.strip().lower() not in ("all", "всё", "все"):
            errors["since"] = "например 1h, 24h, 7d или дата ISO"
        elif since is not None:
            filters["since"] = since
    sort = params.get("sort") or "best"
    if sort not in SORTS:
        errors["sort"] = ", ".join(SORTS)
    if errors:
        raise validation_error(errors, "Неверные фильтры")
    return filters, sort


def _page(params: Any) -> tuple[int, int]:
    try:
        limit = int(params.get("limit") or params.get("page_size") or DEFAULT_LIMIT)
    except ValueError:
        raise validation_error({"limit": "нужно целое число"}) from None
    limit = max(1, min(MAX_LIMIT, limit))
    offset = 0
    cursor = params.get("cursor")
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
            offset = int(decoded.removeprefix("o:"))
        except (ValueError, UnicodeDecodeError):
            raise validation_error({"cursor": "неверный курсор"}) from None
    elif params.get("page"):
        try:
            offset = (max(1, int(params["page"])) - 1) * limit
        except ValueError:
            raise validation_error({"page": "нужно целое число"}) from None
    elif params.get("offset"):
        try:
            offset = max(0, int(params["offset"]))
        except ValueError:
            raise validation_error({"offset": "нужно целое число"}) from None
    return limit, max(0, offset)


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _facets(ctx: ApiContext, filters: dict[str, Any]) -> dict[str, Any]:
    """Counts for the filter chips, each with the other filters applied."""
    db = ctx.db

    def count(**override: Any) -> int:
        f = {k: v for k, v in filters.items() if k not in override or override[k] is not None}
        f.update({k: v for k, v in override.items() if v is not None})
        for key in [k for k, v in override.items() if v is None]:
            f.pop(key, None)
        return db.count_deals_v1(**f)

    return {
        "verdict": {"good": count(verdicts=["buy", "maybe"]), "buy": count(verdicts=["buy"]),
                    "maybe": count(verdicts=["maybe"]), "all": count(verdicts=None)},
        "action": {a: count(actions=[a]) for a in ACTIONS},  # the derived action (never "")
        "purpose": {"resale": count(purpose="resale"), "personal": count(purpose="personal")},
        "unseen": count(unseen=True),
        "no_flags": count(no_flags=True),
        "near_10km": count(max_km=10.0),
        "shipping": count(shipping=True),
        "hidden": count(statuses=["ignored"]),
    }


# -------------------------------------------------------------------- feed
@router.get("/deals", response_model=DealsPage, responses=ERRORS,
            summary="Лента: фильтры verdict/action/actionable/purpose/source/search/status/q/min_score/min_profit/"
                    "min_price/max_price/max_km/shipping/no_flags/unseen/ai_checked/since; sort best|fresh|profit|price|roi|"
                    "distance|ending|updated; limit+offset | page | cursor; facets=1")
async def deals_list(request: Request, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    params = request.query_params
    filters, sort = deal_filters(ctx, params)
    limit, offset = _page(params)
    rows, total = ctx.db.find_deals(sort=sort, limit=limit, offset=offset, **filters)
    items = [deal_card(deal, extras, config=ctx.config) for deal, extras in rows]
    more = offset + len(items) < total
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "page": offset // limit + 1,
        "pages": max(1, math.ceil(total / limit)),
        "next_cursor": _cursor(offset + limit) if more else None,
        "facets": _facets(ctx, filters) if _bool(params.get("facets")) else None,
    }


def _get(ctx: ApiContext, ad_id: str) -> tuple[Any, dict[str, Any]]:
    found = ctx.db.get_deal_extras(ad_id)
    if found is None:
        raise ApiError(404, "not_found", "Объявление не найдено — возможно, удалено из базы")
    return found


@router.get("/deals/{ad_id}", responses=ERRORS,
            summary="Сделка целиком: фото, расчёт прибыли, аналоги, ИИ, продавец, сообщение продавцу, этап сделки")
async def deal_get(ad_id: str, seen: bool = Query(False, description="отметить как просмотренную"),
                   ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    deal, extras = _get(ctx, ad_id)
    if seen and extras.get("seen_at") is None:
        ctx.db.mark_seen([ad_id])
        deal, extras = _get(ctx, ad_id)
    return deal_detail(deal, extras, ctx.config)


def publish_update(ctx: ApiContext, ad_id: str) -> dict[str, Any]:
    deal, extras = _get(ctx, ad_id)
    ctx.hub.publish("deal_updated", {"ad_id": ad_id, "card": deal_card(deal, extras, config=ctx.config)})
    return deal_detail(deal, extras, ctx.config)


@router.patch("/deals/{ad_id}", responses=ERRORS,
              summary="Статус (new/starred/contacted/bought/sold/ignored), заметка, цены покупки/продажи, "
                      "доп. расходы, причина скрытия")
async def deal_patch(ad_id: str, body: DealPatchIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    deal, extras = _get(ctx, ad_id)
    fields = body.model_fields_set
    if not fields:
        raise ApiError(400, "bad_request", "Нечего менять")
    money_problems = _money_problems(body, deal)
    if money_problems:
        raise validation_error(money_problems, "Проверь суммы")
    changes: dict[str, Any] = {}
    for key in ("bought_price", "sold_price", "extra_costs", "bought_at", "sold_at", "hidden_reason"):
        if key in fields:
            changes[key] = getattr(body, key)
    if "note" in fields:
        changes["note"] = body.note or ""
    status = body.status if "status" in fields and body.status else None
    now = utcnow()
    if status is not None:
        changes["status"] = status
        listing, ev = deal.listing, deal.evaluation
        if status == "contacted" and extras.get("contacted_at") is None:
            changes["contacted_at"] = now
        if status in ("bought", "sold"):
            if extras.get("bought_at") is None and "bought_at" not in fields:
                changes["bought_at"] = now
            if extras.get("bought_price") is None and "bought_price" not in fields:
                terms = offer_terms(ev, listing)
                default = terms[0] if terms else (ev.buy_price if ev and ev.buy_price is not None else listing.price)
                changes["bought_price"] = default
        if status == "sold":
            if extras.get("sold_price") is None and changes.get("sold_price") is None:
                raise validation_error({"sold_price": "укажи, за сколько продал"})
            if extras.get("sold_at") is None and "sold_at" not in fields:
                changes["sold_at"] = now
        else:
            if extras.get("sold_at") is not None and "sold_at" not in fields:
                changes["sold_at"] = None
            if status in ("new", "starred", "contacted") and extras.get("bought_at") is not None \
                    and "bought_at" not in fields:
                changes["bought_at"] = None
        if status != "ignored" and "hidden_reason" not in fields:
            changes["hidden_reason"] = None
    ctx.db.update_deal_state(ad_id, **changes)
    return publish_update(ctx, ad_id)


MAX_MONEY = 1_000_000.0


def _money_problems(body: DealPatchIn, deal: Any) -> dict[str, str]:
    """Bought / sold prices and extra costs: never negative, never absurd, never clamped."""
    problems: dict[str, str] = {}
    free = bool(deal.listing.is_free)
    checks = (("bought_price", "Цена покупки", not free), ("sold_price", "Цена продажи", True),
              ("extra_costs", "Доп. расходы", False))
    for key, label, positive in checks:
        value = getattr(body, key)
        if key not in body.model_fields_set or value is None:
            continue
        if not math.isfinite(value):
            problems[key] = "Нужно число"
        elif value < 0:
            problems[key] = f"{label} не может быть меньше нуля"
        elif value > MAX_MONEY:
            problems[key] = "Слишком большая сумма — проверь, нет ли лишних нулей"
        elif positive and value == 0:
            problems[key] = ("Укажи, за сколько купил — 0 € бывает только у бесплатных вещей" if key == "bought_price"
                             else "Укажи, за сколько продал")
    return problems


@router.post("/deals/{ad_id}/seen", responses=ERRORS, summary="Отметить сделку просмотренной")
async def deal_seen(ad_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    _get(ctx, ad_id)
    return {"ad_id": ad_id, "marked": ctx.db.mark_seen([ad_id])}


@router.post("/deals/seen", summary="Отметить несколько сделок просмотренными: {ids: [...]}")
async def deals_seen(body: SeenIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"marked": ctx.db.mark_seen(body.ids)}


@router.post("/deals/{ad_id}/reevaluate", status_code=202, responses=ERRORS,
             summary="Переоценить (в фоне): 202 + задача; refetch=true — заново открыть объявление")
async def deal_reevaluate(ad_id: str, body: ReevaluateIn | None = None,
                          ctx: ApiContext = Depends(get_ctx)) -> JSONResponse:
    deal, _ = _get(ctx, ad_id)
    mon = ctx.monitor
    if mon is None or not (callable(getattr(mon, "evaluate_listing", None)) or callable(getattr(mon, "evaluate_url", None))):
        raise ApiError(503, "unavailable", "Переоценка сейчас недоступна: программа запущена без фоновых проверок. "
                                           "Перезапусти программу")
    refetch = bool(body and body.refetch)
    listing = deal.listing
    search = ctx.config.search_by_name(listing.search_name)
    purpose = deal.evaluation.purpose if deal.evaluation else (search.purpose if search else "resale")

    async def work(job: Any) -> dict[str, Any]:
        from .routes_monitor import _deal_result

        async with ctx.check_lock:
            if search is not None and not refetch and callable(getattr(mon, "evaluate_listing", None)):
                job.progress("market", "Ищу цены похожих…")
                kwargs = {"progress": job.progress} if "progress" in inspect.signature(
                    mon.evaluate_listing).parameters else {}
                await mon.evaluate_listing(listing, search, **kwargs)
            else:
                job.progress("fetch", "Открываю объявление…")
                kwargs = {"progress": job.progress} if "progress" in inspect.signature(
                    mon.evaluate_url).parameters else {}
                await mon.evaluate_url(listing.url, purpose=purpose,
                                       target_price=search.target_price if search else None, **kwargs)
            job.progress("done", "Готово")
        return _deal_result(ctx, ad_id)

    return accepted(ctx.jobs.start("reevaluate", work, params={"refetch": refetch}, ad_id=ad_id))


# ------------------------------------------------------------------- market
def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    pos = (len(data) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


@router.get("/deals/{ad_id}/market", responses=ERRORS,
            summary="История цен этого товара: точки + медиана/p25/p75, линия медианы, «выгодно до», это объявление")
async def deal_market(ad_id: str, days: int = Query(60, ge=0, le=730, description="0 = вся история"),
                      ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...pricing.estimator import comparable_fits, history_lookup_key, listing_identity

    deal, _ = _get(ctx, ad_id)
    listing, ev = deal.listing, deal.evaluation
    ident = listing_identity(listing)
    key = history_lookup_key(ident)
    base = {
        "ad_id": ad_id, "days": days, "product_key": key,
        "product": ident.key.query() if ident.key is not None else "",
        "this_ad": {"price": rnd(listing.price), "first_seen": iso(listing.first_seen), "url": listing.url},
        "max_buy_price": rnd(ev.max_buy_price) if ev else None,
        "offer_price": rnd(ev.offer_price) if ev else None,
        "market_price": rnd(market_price(ev)),
    }
    if key is None:
        return {**base, "available": False, "points": [], "stats": None, "median_line": [],
                "caption_ru": "Модель товара не распознана — истории цен нет, оценка по похожим объявлениям"}
    since = utcnow() - timedelta(days=days) if days else None
    rows = ctx.db.price_history_spans(key, since, limit=2000)
    points: list[dict[str, Any]] = []
    for comp, first, last in rows:
        try:
            fits = comparable_fits(listing.title, comp)
        except Exception:  # noqa: BLE001
            fits = True
        this = bool(comp.url) and comp.url == listing.url
        if not fits and not this:
            continue
        points.append({
            "price": rnd(comp.price), "sold": comp.sold, "source": comp.source,
            "source_label": COMP_SOURCE_LABELS.get(comp.source, comp.source), "title": comp.title, "url": comp.url,
            "first_seen": iso(first), "seen_at": iso(last),
            "days_listed": round(max(0.0, (aware(last) - aware(first)).total_seconds() / 86400), 1),
            "this_ad": this,
        })
    points.sort(key=lambda p: p["seen_at"] or "")
    others = [p for p in points if not p["this_ad"]]
    prices = [p["price"] for p in others if p["price"] is not None]
    stats: dict[str, Any] | None = None
    if prices:
        listed = [p["days_listed"] for p in others if not p["sold"]]
        stats = {
            "count": len(prices), "sold_count": sum(1 for p in others if p["sold"]),
            "median": rnd(percentile(prices, 0.5)), "p25": rnd(percentile(prices, 0.25)),
            "p75": rnd(percentile(prices, 0.75)), "low": rnd(min(prices)), "high": rnd(max(prices)),
            "avg_days_listed": round(sum(listed) / len(listed), 1) if listed else None,
        }
    median_line: list[dict[str, Any]] = []
    by_day: dict[date, list[float]] = {}
    for p in others:
        day = aware(datetime.fromisoformat(p["seen_at"])).astimezone(local_tz()).date()
        by_day.setdefault(day, []).append(p["price"])
    window = timedelta(days=14)
    for day in sorted(by_day):
        values = [v for d, vs in by_day.items() if day - window < d <= day for v in vs]
        median_line.append({"date": day.isoformat(), "median": rnd(percentile(values, 0.5)), "n": len(values)})
    caption = "Истории цен ещё мало — оценка по похожим объявлениям"
    if stats and stats["count"] >= 3:
        span = f"за {days} дней" if days else "за всё время"
        caption = f"Рынок ~{stats['median']:.0f} € по {stats['count']} объявлениям {span}"
        if stats["avg_days_listed"] is not None:
            days_listed = stats["avg_days_listed"]
            caption += (" · обычно уходят в тот же день" if days_listed < 1
                        else f" · держатся в среднем {days_listed:.0f} дн.")
    return {**base, "available": bool(stats), "points": points, "stats": stats, "median_line": median_line,
            "caption_ru": caption}


@router.get("/deals/{ad_id}/price-history", responses=ERRORS, include_in_schema=False)
async def deal_price_history(ad_id: str, days: int = Query(60, ge=0, le=730),
                             ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await deal_market(ad_id, days, ctx)


# ------------------------------------------------------------------ summary
def local_midnight(now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(local_tz())
    return datetime.combine(local.date(), dtime(0), tzinfo=local_tz())


def _profit_of(card: dict[str, Any]) -> float:
    value = card.get("profit_at_offer") if card.get("action") == "haggle" else card.get("profit")
    return float(value or 0.0)


@router.get("/summary/today", summary="Для шапки ленты: сегодня найдено, потенциал, лучшая находка, обучение, статус")
async def summary_today(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from .routes_app import onboarding_view
    from .routes_monitor import monitor_view

    db = ctx.db
    midnight = local_midnight()
    rows, count = db.find_deals(actionable=True, since=midnight, sort="score", limit=500)
    cards = [deal_card(d, e, config=ctx.config) for d, e in rows]
    resale = [c for c in cards if c["purpose"] == "resale"]
    personal = [c for c in cards if c["purpose"] == "personal"]
    enabled = [s for s in ctx.config.searches if s.enabled]
    personal_only = bool(enabled) and all(s.purpose == "personal" for s in enabled)
    seen_today = len(db.activity_since(midnight)["listings"])
    best = cards[0] if cards else None
    day_ago = utcnow() - timedelta(days=1)
    mon = await monitor_view(ctx, with_hosts=False)
    onboarding = onboarding_view(ctx)
    checklist = [{"key": s["key"], "title_ru": s["title_ru"], "done": s["done"]}
                 for s in onboarding["steps"] if s["key"] in ("ai", "telegram", "ebay", "categories")]
    followups = sum(1 for r in db.pipeline_rows() if r["status"] == "contacted" and r["contacted_at"] is not None
                    and r["contacted_at"] < utcnow() - timedelta(hours=24))
    headline = (f"{len(personal)} находки для себя" if personal_only
                else f"Найдено {count} выгодных")
    return {
        "since": midnight.isoformat(),
        "count": count,
        "resale_count": len(resale),
        "personal_count": len(personal),
        "potential_profit": rnd(sum(max(0.0, _profit_of(c)) for c in resale)),
        "potential_savings": rnd(sum(max(0.0, _profit_of(c)) for c in personal)),
        "ads_seen": seen_today,
        "best": best,
        "personal_only": personal_only,
        "headline_ru": headline,
        "quiet_day": bool(mon["loop"] or mon["available"]) and db.count_deals_v1(
            actionable=True, since=day_ago) == 0 and bool(enabled),
        "unseen_good": db.count_deals_v1(actionable=True, unseen=True),
        "followups": followups,
        "learning": mon["learning"],
        "monitor": {k: mon[k] for k in ("available", "running", "paused", "state", "state_ru", "next_run_at",
                                        "next_run_label", "cooldown", "progress", "interval_minutes")},
        "last_run": mon["last_summary"],
        "setup_checklist": {"items": checklist, "done": sum(1 for c in checklist if c["done"]),
                            "total": len(checklist)},
    }


# ----------------------------------------------------------------- pipeline
def _paid(bought_price: Any, price: Any) -> float:
    """What a bought item cost: the saved purchase price (even 0 for a free one), else the ad price."""
    if bought_price is not None:
        return float(bought_price)
    return float(price or 0)


COLUMNS = (("starred", "Избранное"), ("contacted", "Написал"), ("bought", "Купил"), ("sold", "Продал"))


def _older(when: datetime | None, now: datetime, **delta: float) -> bool:
    return when is not None and now - aware(when) > timedelta(**delta)


def pipeline_summary(ctx: ApiContext, months: int = 6) -> dict[str, Any]:
    rows = ctx.db.pipeline_rows()
    now = utcnow()
    local_now = now.astimezone(local_tz())
    sold = [r for r in rows if r["status"] == "sold"]
    stock = [r for r in rows if r["status"] == "bought"]

    def real(r: dict[str, Any]) -> float | None:
        return realized_profit("sold", r)

    month_keys = []
    y, m = local_now.year, local_now.month
    for _ in range(max(1, months)):
        month_keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    by_month: dict[str, dict[str, Any]] = {k: {"month": k, "profit": 0.0, "sold": 0} for k in month_keys}
    for r in sold:
        when = r["sold_at"] or r["updated_at"]
        profit = real(r)
        if when is None or profit is None:
            continue
        key = aware(when).astimezone(local_tz()).strftime("%Y-%m")
        if key in by_month:
            by_month[key]["profit"] += profit
            by_month[key]["sold"] += 1
    earned = [{**v, "profit": rnd(v["profit"])} for v in reversed(list(by_month.values()))]
    invested = sum(float(_paid(r["bought_price"], r["price"])) for r in stock)
    expected = sum(float(r["expected_profit"] or 0) for r in stock if r["purpose"] != "personal")
    pairs = [(real(r), r["expected_profit"]) for r in sold
             if real(r) is not None and r["expected_profit"] not in (None, 0)]
    accuracy = None
    if len(pairs) >= 3:
        delta = sum((a - p) / abs(p) for a, p in pairs) / len(pairs)
        pct = round(delta * 100)
        accuracy = {"n": len(pairs), "avg_delta_percent": pct,
                    "message_ru": f"факт в среднем {'+' if pct > 0 else '−' if pct < 0 else ''}{abs(pct)} % от прогноза"}
    return {
        "earned_month": earned[-1]["profit"] if earned else 0.0,
        "earned_by_month": earned,
        "realized_total": rnd(sum(real(r) or 0.0 for r in sold)),
        "sold_count": len(sold),
        "invested": rnd(invested),
        "in_stock": len(stock),
        "expected_in_stock": rnd(expected),
        "accuracy": accuracy,
        "followups": sum(1 for r in rows if r["status"] == "contacted" and _older(r["contacted_at"], now, hours=24)),
        "stale_stock": sum(1 for r in stock if _older(r["bought_at"], now, days=21)),
    }


@router.get("/pipeline/summary", summary="Мои сделки: заработано по месяцам, вложено, ожидаемая прибыль, точность")
async def pipeline_summary_get(months: int = Query(6, ge=1, le=36), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return pipeline_summary(ctx, months)


@router.get("/pipeline", summary="Мои сделки по колонкам (карточки) + сводка")
async def pipeline(limit: int = Query(100, ge=1, le=500), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    columns = []
    for key, title in COLUMNS:
        rows, total = ctx.db.find_deals(statuses=[key], verdicts=None, sort="updated", limit=limit)
        cards = [deal_card(d, e, config=ctx.config) for d, e in rows]
        if key == "bought":
            amount = sum(float(_paid(c["bought_price"], c["price"])) for c in cards)
        elif key == "sold":
            amount = sum(float(c["realized_profit"] or 0) for c in cards)
        else:
            amount = sum(float(c["price"] or 0) for c in cards)
        columns.append({"key": key, "title_ru": title, "count": total, "amount": rnd(amount), "items": cards})
    hidden = ctx.db.count_deals_v1(statuses=["ignored"], verdicts=None)
    return {"columns": columns, "hidden": hidden, "summary": pipeline_summary(ctx)}


# -------------------------------------------------------------------- stats
def _day(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return aware(datetime.fromisoformat(str(raw))).astimezone(local_tz()).date()
    except ValueError:
        return None


def _period(activity: dict[str, list[Any]], since: datetime) -> dict[str, Any]:
    def after_ts(raw: Any) -> bool:
        try:
            return aware(datetime.fromisoformat(str(raw))) >= since
        except ValueError:
            return False

    deals = [d for d in activity["deals"] if after_ts(d[0])]
    buys = [d for d in deals if d[1] == "buy"]
    potential = sum(float(d[3] or 0) for d in buys if d[2] == "resale" and (d[3] or 0) > 0
                    and d[4] not in ("ignored",))
    bought = [b for b in activity["bought"] if after_ts(b[0])]
    sold = [s for s in activity["sold"] if after_ts(s[0])]
    realized = sum(float(s[2] or 0) - float(s[1] or 0) - float(s[3] or 0) for s in sold
                   if s[1] is not None and s[2] is not None)
    return {
        "ads_seen": sum(1 for t in activity["listings"] if after_ts(t)),
        "evaluated": sum(1 for t in activity["evaluated"] if after_ts(t)),
        "deals": len(buys),
        "good": sum(1 for d in deals if d[5] in GOOD_ACTIONS or d[1] == "buy"),
        "potential_profit": rnd(potential),
        "notified": sum(1 for t in activity["notified"] if after_ts(t)),
        "bought": len(bought),
        "spent": rnd(sum(float(b[1] or 0) for b in bought)),
        "sold": len(sold),
        "realized_profit": rnd(realized),
    }


@router.get("/stats/overview", summary="Сегодня и за 7 дней: объявлений, выгодных, потенциал, куплено, реальная прибыль")
async def stats_overview(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    midnight = local_midnight()
    week = utcnow() - timedelta(days=7)
    activity = ctx.db.activity_since(min(midnight, week))
    return {"today": _period(activity, midnight), "week": _period(activity, week), "totals": ctx.db.stats(),
            "since_today": midnight.isoformat(), "since_week": week.isoformat()}


@router.get("/stats/timeseries", summary="По дням (Берлин): объявлений, оценено, выгодных, уведомлений, прибыль")
async def stats_timeseries(days: int = Query(14, ge=1, le=90), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    today = utcnow().astimezone(local_tz()).date()
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, dtime(0), tzinfo=local_tz())
    activity = ctx.db.activity_since(since)
    series: dict[date, dict[str, Any]] = {
        first + timedelta(days=i): {"date": (first + timedelta(days=i)).isoformat(), "ads_seen": 0, "evaluated": 0,
                                    "deals": 0, "maybe": 0, "notified": 0, "potential_profit": 0.0, "bought": 0,
                                    "sold": 0, "realized_profit": 0.0}
        for i in range(days)
    }

    def bump(raw: Any, key: str, amount: float = 1) -> None:
        d = _day(raw)
        if d in series:
            series[d][key] += amount

    for t in activity["listings"]:
        bump(t, "ads_seen")
    for t in activity["evaluated"]:
        bump(t, "evaluated")
    for first_seen, verdict, purpose, profit, status, _action in activity["deals"]:
        bump(first_seen, "deals" if verdict == "buy" else "maybe")
        if verdict == "buy" and purpose == "resale" and (profit or 0) > 0 and status != "ignored":
            bump(first_seen, "potential_profit", float(profit))
    for t in activity["notified"]:
        bump(t, "notified")
    for when, _price in activity["bought"]:
        bump(when, "bought")
    for when, bought, sold, extra in activity["sold"]:
        bump(when, "sold")
        if bought is not None and sold is not None:
            bump(when, "realized_profit", float(sold) - float(bought) - float(extra or 0))
    items = [{**v, "potential_profit": rnd(v["potential_profit"]), "realized_profit": rnd(v["realized_profit"])}
             for _, v in sorted(series.items())]
    return {"days": days, "items": items}


# ------------------------------------------------------------------- export
CSV_COLUMNS = (
    ("id", "ID"), ("title", "Название"), ("url", "Ссылка"), ("source", "Источник"), ("search_name", "Поиск"),
    ("status", "Статус"), ("verdict", "Вердикт"), ("action", "Действие"), ("score", "Балл"), ("price", "Цена"),
    ("market_price", "Рынок"), ("profit", "Ожид. прибыль"), ("roi", "ROI"), ("offer_price", "Предложить"),
    ("max_buy_price", "Выгодно до"), ("bought_price", "Купил за"), ("bought_at", "Когда купил"),
    ("sold_price", "Продал за"), ("sold_at", "Когда продал"), ("extra_costs", "Доп. расходы"),
    ("realized_profit", "Реальная прибыль"), ("first_seen", "Найдено"), ("location", "Место"),
    ("distance_km", "Км"), ("note", "Заметка"),
)


CSV_LABELS = {"verdict": VERDICT_LABELS, "action": ACTION_LABELS, "source": SOURCE_LABELS}
CSV_TIMES = ("bought_at", "sold_at", "first_seen")  # local time (general.timezone), not ISO UTC


@router.get("/export/deals.csv", summary="Сделки в CSV (Excel: ; и запятая в числах). Фильтры как у /deals; "
                                         "по умолчанию — все этапы «Мои сделки»")
async def export_deals(request: Request, excel: bool = Query(True), ctx: ApiContext = Depends(get_ctx)) -> Response:
    params = dict(request.query_params)
    if not any(k in params for k in ("status", "verdict", "action", "search", "q", "since")):
        params.update({"status": "starred,contacted,bought,sold", "verdict": "all"})
    filters, sort = deal_filters(ctx, params)
    rows, _ = ctx.db.find_deals(sort="updated" if "sort" not in params else sort, limit=100_000, **filters)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";" if excel else ",")
    writer.writerow([label for _, label in CSV_COLUMNS])
    for deal, extras in rows:
        card = deal_card(deal, extras, config=ctx.config)
        line = []
        for key, _ in CSV_COLUMNS:
            value = card.get(key)
            if key == "status":
                value = STATUS_LABELS.get(str(value), value)
            elif key in CSV_LABELS:  # Russian words for Excel, not machine keys
                value = CSV_LABELS[key].get(str(value), value)
            elif key in CSV_TIMES and value:
                value = to_local(datetime.fromisoformat(str(value))).strftime("%d.%m.%Y %H:%M")
            if isinstance(value, float) and excel:
                value = f"{value:.2f}".replace(".", ",")
            line.append("" if value is None else value)
        writer.writerow(line)
    body = ("﻿" if excel else "") + buf.getvalue()
    stamp = utcnow().astimezone(local_tz()).strftime("%Y-%m-%d")
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="ebeyparser-deals-{stamp}.csv"'})
