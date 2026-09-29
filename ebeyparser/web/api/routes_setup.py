"""Onboarding helpers: places, categories, strategy presets, request estimate, and turning
the answers into searches (preview without saving, apply = save everything at once)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from ...config import SearchConfig
from ...scraper.categories import (
    DEFAULT_BUDGET,
    DEFAULT_LOCATION,
    DEFAULT_RADIUS_KM,
    INTERVAL_STEPS,
    RADIUS_CHOICES,
    RECOMMENDED_IDS,
    RESULT_PAGES_SHARE,
    Category,
    CategoryList,
    RequestEstimate,
    SetupAnswers,
    answers_from_searches,
    builtin_by_id,
    builtin_categories,
    FALLBACK_NOTE,
    describe_error,
    estimate_requests,
    is_category_scan,
    load_cached_categories,
    merge_plan,
    merge_searches,
    save_cached_categories,
    searches_from_answers,
    snap_radius,
    suggest_interval,
)
from .context import ApiContext, get_ctx
from .errors import validation_error
from .locations import place_label, search_places
from .presenters import iso, search_ids, search_view
from .schemas import ERRORS, EstimateOut, SetupIn
from .validation import search_problems

log = logging.getLogger(__name__)
router = APIRouter()

DISCOVERY_TIMEOUT = 25.0
BUDGET_RANGE = (50, 1500, 10)
PRESETS: dict[str, dict[str, Any]] = {
    "careful": {"title_ru": "Осторожно", "line_ru": "Только очень выгодное, меньше уведомлений", "icon": "shield",
                "min_profit": 60.0, "min_roi": 0.35, "safety_margin_percent": 15.0, "min_comparables": 8,
                "notify_min_score": 80.0},
    "balanced": {"title_ru": "Сбалансированно", "line_ru": "Золотая середина", "icon": "scale",
                 "min_profit": 40.0, "min_roi": 0.25, "safety_margin_percent": 10.0, "min_comparables": 6,
                 "notify_min_score": 70.0, "default": True},
    "aggressive": {"title_ru": "Агрессивно", "line_ru": "Больше находок, больше проверять самому", "icon": "rocket",
                   "min_profit": 25.0, "min_roi": 0.15, "safety_margin_percent": 7.0, "min_comparables": 4,
                   "notify_min_score": 60.0},
}
WISHLIST_SUGGESTIONS = [  # search words stay German (that is what sellers write); hint_ru explains them
    {"item": "RTX 3090", "max_price": 550, "hint_ru": "видеокарта"},
    {"item": "RTX 3060 12GB", "max_price": 200, "hint_ru": "видеокарта"},
    {"item": "DDR4 ECC 64GB", "max_price": 90, "hint_ru": "серверная память"},
    {"item": "Ryzen 9 5950X", "max_price": 250, "hint_ru": "процессор"},
    {"item": "Netzteil 1000W", "max_price": 80, "hint_ru": "блок питания 1000 Вт"},
    {"item": "Server Gehäuse", "max_price": 60, "hint_ru": "серверный корпус"},
]
CATEGORY_ICONS = {
    173: "smartphone", 278: "laptop", 279: "gamepad-2", 225: "cpu", 245: "camera", 172: "headphones",
    285: "tablet", 228: "pc-case", 175: "tv", 227: "disc-3", 176: "washing-machine", 217: "bike",
    74: "guitar", 84: "hammer", 168: "plug", 161: "zap",
}


# ---------------------------------------------------------------- locations
@router.get("/locations", summary="Города Германии и районы Берлина (без сети): ?q=Ber / 10115 / Кёльн")
async def locations(q: str = Query("", max_length=80), limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    return {"query": q, "items": search_places(q, limit)}


# --------------------------------------------------------------- categories
def category_view(cat: Category) -> dict[str, Any]:
    return {
        "id": cat.id, "name_de": cat.name_de, "name_ru": cat.name_ru, "label": cat.label, "parent": cat.parent,
        "resale_friendly": cat.resale_friendly, "recommended": cat.recommended, "verified": cat.verified,
        "min_price": cat.min_price, "count": cat.count, "url": cat.url, "icon": CATEGORY_ICONS.get(cat.id, "tag"),
    }


async def categories_for(ctx: ApiContext, location: str, radius_km: int, *, live: bool = False) -> tuple[CategoryList, str]:
    """(list, source): memory / data/categories.json cache or built-in without requests;
    live=True asks the site (1–2 requests) and caches a good answer for 7 days."""
    location = location.strip()
    if not location:
        return builtin_categories(), "builtin"
    key = (location.lower(), radius_km)
    cache: dict[tuple[str, int], CategoryList] = getattr(ctx.app.state, "category_cache", None) or {}
    ctx.app.state.category_cache = cache
    data_dir = ctx.config.data_path
    if not live:
        if key in cache:
            return cache[key], "cache"
        cached = load_cached_categories(data_dir, location, radius_km)
        if cached is not None:
            cache[key] = cached
            return cached, "cache"
        return builtin_categories(), "builtin"
    discover = ctx.category_discovery or _default_discovery(ctx)
    try:
        result = await asyncio.wait_for(discover(location, radius_km), DISCOVERY_TIMEOUT)
        if not isinstance(result, CategoryList):
            result = CategoryList(result or builtin_categories())
        if not len(result):
            result = CategoryList(builtin_categories(), error=result.error or "Сайт не прислал список категорий — "
                                                                             "показываю обычный")
    except asyncio.TimeoutError:
        result = CategoryList(builtin_categories(),
                              error="Kleinanzeigen не ответил вовремя — показываю обычный список категорий")
    except Exception as exc:  # noqa: BLE001 - discovery never breaks onboarding
        log.warning("category discovery failed: %s", exc)
        result = CategoryList(builtin_categories(), error=describe_error(exc) + FALLBACK_NOTE)
    if result.live:
        cache[key] = result
        save_cached_categories(data_dir, location, radius_km, result)
        return result, "live"
    return result, "builtin"


def _default_discovery(ctx: ApiContext) -> Any:
    async def discover(location: str, radius_km: int) -> CategoryList:
        from ...runtime import http_state_path
        from ...scraper.categories import discover_categories, discovery_client

        cfg = ctx.config
        client = discovery_client(cfg.general, state_path=http_state_path(cfg.data_path))
        try:
            return await discover_categories(client, location, radius_km)
        finally:
            await client.aclose()

    return discover


@router.get("/categories", summary="Категории Kleinanzeigen: встроенный список, кэш или ?live=1 (запрос к сайту)")
async def categories(
    location: str = Query("", max_length=80),
    radius_km: int = Query(DEFAULT_RADIUS_KM, ge=0, le=500),
    live: bool = Query(False),
    ctx: ApiContext = Depends(get_ctx),
) -> dict[str, Any]:
    radius = snap_radius(radius_km)
    result, source = await categories_for(ctx, location, radius, live=live)
    return {
        "items": [category_view(c) for c in result],
        "source": source,
        "live": result.live,
        "error_ru": result.error or None,
        "fetched_at": iso(getattr(result, "fetched_at", None)),
        "url": result.url or None,
        "location": location,
        "radius_km": radius,
        "recommended_ids": list(RECOMMENDED_IDS),
    }


# ---------------------------------------------------------------- estimate
def estimate_view(est: RequestEstimate) -> dict[str, Any]:
    cap = est.cap_per_hour
    load = int(round(100 * est.pages_per_hour / cap)) if cap else 0
    if not cap or est.pages_per_hour <= cap * RESULT_PAGES_SHARE + 1e-9:
        level = "ok"
    elif not est.tight:
        level = "warn"
    else:
        level = "danger"
    if not est.searches:
        short = "Поисков пока нет — нагрузки на сайт нет"
    elif level == "ok":
        short = "Безопасно — блокировки маловероятны"
    elif level == "warn":
        short = "Нагрузка заметная — лучше проверять реже"
    else:
        short = f"Слишком часто — возможна блокировка. Проверяй раз в {est.suggested_interval} мин"
    return {
        "interval_minutes": est.interval_minutes,
        "suggested_interval": est.suggested_interval,
        "searches": est.searches,
        # one wording everywhere: "~N из 150 страниц в час" (result pages; the rest of the cap evaluates ads)
        "pages_label_ru": f"~{est.pages_per_hour} из {cap} страниц в час" if cap else f"~{est.pages_per_hour} страниц в час",
        "short_ru": short,
        "category_scans": est.category_scans,
        "keyword_searches": est.keyword_searches,
        "pages_per_hour": est.pages_per_hour,
        "requests_per_hour": est.per_hour,
        "requests_per_day": est.per_day,
        "cap_per_hour": cap,
        "load_percent": load,
        "evaluation_per_hour": est.evaluation_per_hour,
        "tight": est.tight,
        "level": level,
        "text_ru": est.describe(),
    }


def load_counts(searches: list[SearchConfig]) -> tuple[int, int]:
    """(category scans, keyword searches) among the enabled Kleinanzeigen searches — what the
    load on the site depends on (eBay goes through its own API)."""
    enabled = [s for s in searches if s.enabled and s.source == "kleinanzeigen"]
    scans = sum(1 for s in enabled if is_category_scan(s))
    return scans, len(enabled) - scans


def config_estimate(ctx: ApiContext, interval: float | None = None,
                    searches: list[SearchConfig] | None = None) -> dict[str, Any]:
    """The load card of «Поиски» (and of the wizard's preview for the searches it will leave)."""
    cfg = ctx.config
    scans, words = load_counts(cfg.searches if searches is None else searches)
    est = estimate_requests(scans, interval or cfg.general.interval_minutes, cfg.general, keyword_searches=words)
    return estimate_view(est)


@router.get("/setup/estimate", response_model=EstimateOut,
            summary="Нагрузка на Kleinanzeigen: для текущих поисков или ?categories=7&keywords=1&interval=30")
async def setup_estimate(
    categories: int | None = Query(None, ge=0, le=200),
    keywords: int | None = Query(None, ge=0, le=200),
    interval: float | None = Query(None, ge=1, le=1440),
    ctx: ApiContext = Depends(get_ctx),
) -> dict[str, Any]:
    if categories is None and keywords is None:
        return config_estimate(ctx, interval)
    cats, words = categories or 0, keywords or 0
    general = ctx.config.general
    chosen = interval or float(max(suggest_interval(cats, general, keyword_searches=words), 5))
    return estimate_view(estimate_requests(cats, chosen, general, keyword_searches=words))


@router.get("/setup/options", summary="Всё для мастера: радиусы, пресеты стратегии, подсказки, текущие ответы")
async def setup_options(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    cfg = ctx.config
    answers = answers_from_searches(cfg.searches, cfg.pricing.min_profit)
    if not cfg.searches:
        answers.max_price = DEFAULT_BUDGET
    personal_scans = any(s.purpose == "personal" for s in cfg.searches if is_category_scan(s))
    return {
        "radius_choices": list(RADIUS_CHOICES),
        "interval_steps": list(INTERVAL_STEPS),
        "defaults": {"location": DEFAULT_LOCATION, "radius_km": DEFAULT_RADIUS_KM, "max_price": DEFAULT_BUDGET,
                     "strategy": "balanced", "purpose": "resale"},
        "budget_range": {"min": BUDGET_RANGE[0], "max": BUDGET_RANGE[1], "step": BUDGET_RANGE[2]},
        "presets": PRESETS,
        "wishlist_suggestions": WISHLIST_SUGGESTIONS,
        "recommended_category_ids": list(RECOMMENDED_IDS),
        "current": {
            "location": answers.location,
            "location_label": place_label(answers.location),
            "radius_km": answers.radius_km,
            "category_ids": list(answers.category_ids) if cfg.searches else list(RECOMMENDED_IDS),
            "purpose": "personal" if personal_scans else answers.purpose,
            "max_price": answers.max_price,
            "min_profit": answers.min_profit,
            "wishlist": [{"item": item, "max_price": price} for item, price in answers.wishlist],
            "interval_minutes": cfg.general.interval_minutes,
            "strategy": _current_strategy(ctx),
            "existing_searches": len(cfg.searches),
        },
    }


def _current_strategy(ctx: ApiContext) -> str:
    p = ctx.config.pricing
    for key, preset in PRESETS.items():
        if (abs(p.min_profit - preset["min_profit"]) < 0.01 and abs(p.min_roi - preset["min_roi"]) < 1e-6
                and abs(p.safety_margin_percent - preset["safety_margin_percent"]) < 0.01
                and p.min_comparables == preset["min_comparables"]):
            return key
    return "custom"


# ---------------------------------------------------------- preview / apply
def _pricing_values(body: SetupIn, ctx: ApiContext) -> dict[str, Any]:
    """pricing.* / notifications.min_score the answers set (strategy preset, then explicit values)."""
    values: dict[str, Any] = {}
    if body.strategy and body.strategy in PRESETS:
        preset = PRESETS[body.strategy]
        values.update({f"pricing.{k}": preset[k] for k in ("min_profit", "min_roi", "safety_margin_percent",
                                                          "min_comparables")})
        values["notifications.min_score"] = preset["notify_min_score"]
    if body.pricing is not None:
        for key, value in body.pricing.model_dump(exclude_none=True).items():
            values[f"pricing.{key}"] = value
    if body.min_profit is not None:
        values["pricing.min_profit"] = body.min_profit
    if body.notify_min_score is not None:
        values["notifications.min_score"] = body.notify_min_score
    if body.set_max_capital and body.max_price and body.purpose != "personal":
        values["pricing.max_capital"] = body.max_price
    return values


def build_searches(body: SetupIn, ctx: ApiContext) -> tuple[list[SearchConfig], SetupAnswers, dict[str, str]]:
    cfg = ctx.config
    problems: dict[str, str] = {}
    location = " ".join(body.location.split())[:80]
    if not location:
        problems["location"] = "Укажи город или почтовый индекс"
    if body.max_price == 0:
        problems["max_price"] = "Максимальная цена должна быть больше нуля"
    pricing = _pricing_values(body, ctx)
    global_min_profit = float(pricing.get("pricing.min_profit", cfg.pricing.min_profit))
    wishlist = [(" ".join(w.item.split())[:80], w.max_price or None) for w in body.wishlist if w.item.strip()]
    answers = SetupAnswers(
        location=location,
        radius_km=snap_radius(body.radius_km),
        category_ids=list(dict.fromkeys(i for i in body.category_ids if i > 0)),
        purpose="personal" if body.purpose == "personal" else "resale",
        max_price=body.max_price,
        min_profit=body.min_profit if body.min_profit is not None else global_min_profit,
        wishlist=wishlist,
        interval_minutes=body.interval_minutes,
    )
    known: dict[int, Category] = {}
    for cid in answers.category_ids:
        cat = builtin_by_id(cid)
        if cat is None:
            name = " ".join(str(body.category_names.get(str(cid)) or "").split())[:80] or f"Категория {cid}"
            cat = Category(cid, name)
        known[cid] = cat
    generated = searches_from_answers(answers, known.values(), global_min_profit=global_min_profit) \
        if not problems else []
    haggle = {(" ".join(w.item.split())[:80]).lower(): w.haggle for w in body.wishlist}
    fixed: list[SearchConfig] = []
    for search in generated:
        if search.purpose == "personal" and search.query and not haggle.get(search.query.lower(), True) \
                and search.target_price:
            search = search.model_copy(update={"max_price": search.target_price})
        fixed.append(search)
    if not problems and not fixed:
        problems["category_ids"] = "Выбери хотя бы одну категорию — или добавь, что ищешь для себя"
    for search in fixed:
        for problem in search_problems(search):
            problems.setdefault("searches", problem)
    return fixed, answers, problems


def _preview(body: SetupIn, ctx: ApiContext) -> dict[str, Any]:
    """Exactly what POST /setup with the same body will do: the searches it creates (category
    scans + wishlist), which existing ones it updates / keeps / replaces, the interval and the
    load of the resulting config (the same numbers the «Поиски» load card will show)."""
    cfg = ctx.config
    searches, answers, problems = build_searches(body, ctx)
    final = merge_searches(cfg.searches, searches, replace_all=body.replace)
    scans, words = load_counts(final)
    suggested = suggest_interval(scans, cfg.general, keyword_searches=words)
    interval = body.interval_minutes or float(max(suggested, 5))
    estimate = estimate_view(estimate_requests(scans, interval, cfg.general, keyword_searches=words))
    ids = search_ids(searches)
    plan = merge_plan(cfg.searches, searches, replace_all=body.replace)
    return {
        "searches": [search_view(s, sid, None, baseline_first_run=cfg.general.baseline_first_run)
                     for s, sid in zip(searches, ids)],
        "count": len(searches),
        "category_scans": sum(1 for s in searches if is_category_scan(s)),
        "wishlist_searches": sum(1 for s in searches if s.purpose == "personal" and s.query),
        "interval_minutes": interval,
        "suggested_interval": suggested,
        "estimate": estimate,
        "pricing": _pricing_values(body, ctx),
        "existing_count": len(cfg.searches),
        "replace": body.replace,
        **plan,  # created / updated / kept / replaced: lists of search names
        "result_count": len(final),
        "summary_ru": _plan_text(plan, len(searches)),
        "problems": problems,
        "valid": not problems,
    }


def _plan_text(plan: dict[str, list[str]], new: int) -> str:
    """«Добавлю 3 поиска, 2 текущих останутся» / «Заменю твои 4 поиска на 3 новых»."""
    from ...scraper.categories import _plural

    def n(count: int) -> str:
        return f"{count} {_plural(count, 'поиск', 'поиска', 'поисков')}"

    if plan["replaced"]:
        return f"Заменю твои {n(len(plan['replaced']) + len(plan['updated']))} на новые"
    parts = []
    if plan["created"]:
        parts.append(f"добавлю {n(len(plan['created']))}")
    if plan["updated"]:
        parts.append(f"обновлю {n(len(plan['updated']))}")
    kept = len(plan["kept"])
    if kept:
        parts.append(f"{kept} {_plural(kept, 'текущий останется', 'текущих останутся', 'текущих останутся')}")
    text = ", ".join(parts) or "ничего не изменится"
    return text[:1].upper() + text[1:]


@router.post("/setup/preview", responses=ERRORS,
             summary="Ответы мастера -> поиски + оценка нагрузки + интервал (ничего не сохраняет)")
async def setup_preview(body: SetupIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return _preview(body, ctx)


async def _apply(body: SetupIn, ctx: ApiContext) -> dict[str, Any]:
    ctx.require_editable()
    preview = _preview(body, ctx)
    if preview["problems"]:
        raise validation_error(preview["problems"], "Проверь ответы")
    searches, _, _ = build_searches(body, ctx)
    cfg = ctx.config
    plan = merge_plan(cfg.searches, searches, replace_all=body.replace)
    merged = merge_searches(cfg.searches, searches, replace_all=body.replace)
    ctx.save_searches(merged, backup=True, reason="setup")
    updates: dict[str, Any] = dict(preview["pricing"])
    if preview["interval_minutes"] != cfg.general.interval_minutes:
        updates["general.interval_minutes"] = preview["interval_minutes"]
    if updates:
        ctx.write_values(updates, reason="setup")
    steps = ["location", "categories", "money"]
    if preview["wishlist_searches"]:
        steps.append("wishlist")
    ctx.mark_done(*steps)
    if body.complete_onboarding:
        ctx.update_onboarding(completed=True)
    from .routes_monitor import cooldown_view, http_hosts, only_kleinanzeigen, start_run

    cooldown = cooldown_view(await http_hosts(ctx))
    started = False
    if body.start_run and not (cooldown is not None and only_kleinanzeigen(ctx)):
        started = start_run(ctx, strict=False)  # during a site's pause the monitor starts right after it
    ids = search_ids(merged)
    message = (f"Сохранено поисков: {len(searches)}. Первая проверка только изучит цены — "
               "уведомления начнутся со следующей." if cfg.general.baseline_first_run
               else f"Сохранено поисков: {len(searches)}.")
    if cooldown is not None:
        message += f" {cooldown['text_ru']} — первая проверка начнётся после паузы."
    stats = ctx.db.search_stats()
    return {
        "saved": len(searches),
        "searches": [search_view(s, sid, stats.get(s.name), baseline_first_run=cfg.general.baseline_first_run)
                     for s, sid in zip(merged, ids)],
        "interval_minutes": ctx.config.general.interval_minutes,
        "estimate": config_estimate(ctx),  # == the «Поиски» load card from now on
        **plan,  # created / updated / kept / replaced (names); replaced only when replace=true
        "summary_ru": preview["summary_ru"],
        "applied": sorted(updates),
        "run_started": started,
        "cooldown": cooldown,  # a site asked for a pause: the launch message must say so
        "message_ru": message,
    }


@router.post("/setup", responses=ERRORS,
             summary="Сохранить ответы мастера разом: поиски, интервал, стратегия; опционально — запустить проверку")
async def setup_apply(body: SetupIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await _apply(body, ctx)


@router.post("/searches/preview", responses=ERRORS, summary="То же, что /setup/preview")
async def searches_preview(body: SetupIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return _preview(body, ctx)


@router.post("/searches/apply", responses=ERRORS, summary="То же, что POST /setup (replace — заменить или добавить)")
async def searches_apply(body: SetupIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await _apply(body, ctx)


__all__ = ["PRESETS", "categories_for", "config_estimate", "estimate_view", "router"]
