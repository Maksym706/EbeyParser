"""App state, onboarding progress, settings, secrets, phone access, restart."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ... import __version__
from ...config import AppConfig
from ...models import utcnow
from ...scraper.categories import RADIUS_CHOICES, answers_from_searches, scan_name, snap_radius
from ..security import COOKIE_MAX_AGE, COOKIE_NAME, TOKEN_FILE, TOKEN_PARAM, ensure_token, is_loopback, local_addresses
from .context import BOOTSTRAPPED_KEY, ONBOARDING_DRAFT_KEY, ONBOARDING_STEPS, ApiContext, dig, get_ctx
from .errors import ApiError, validation_error
from .locations import place_label
from ...timefmt import when_label
from .presenters import iso, search_ids
from .schemas import (
    ERRORS,
    AccessIn,
    AppOut,
    OnboardingCompleteIn,
    OnboardingOut,
    OnboardingSkipIn,
    SecretsIn,
)

log = logging.getLogger(__name__)
router = APIRouter()

MAX_DRAFT_BYTES = 64_000
DEMO_IDS_CACHE: list[str] = []

# ----------------------------------------------------------------- settings rules
SECTIONS = ("general", "pricing", "ai", "notifications", "ebay", "web")
READ_ONLY_KEYS = frozenset({
    "general.data_dir",
    "ai.api_key", "ai.second_opinion.api_key", "ai.scout.api_key",
    "notifications.email.username", "notifications.email.password", "notifications.email.from_addr",
    "notifications.email.to_addrs", "notifications.telegram.bot_token", "notifications.telegram.chat_id",
    "ebay.client_id", "ebay.client_secret", "ebay.oauth_token",
})
RESTART_KEYS = frozenset({"web.host", "web.port", "web.run_monitor", "web.allowed_hosts", "general.data_dir"})
# key -> (min, max) for numbers; None = no bound on that side
RANGES: dict[str, tuple[float | None, float | None]] = {
    "general.interval_minutes": (5, 1440),
    "general.max_pages": (1, 20),
    "general.request_timeout_seconds": (5, 300),
    "general.max_new_per_search": (1, 500),
    "general.max_comps_lookups_per_run": (0, 1000),
    "general.max_details_per_run": (0, 1000),
    "general.max_ai_per_run": (0, 1000),
    "general.min_listing_price": (0, 100000),
    "general.max_requests_per_hour": (0, 5000),
    "pricing.min_profit": (0, 100000),
    "pricing.min_roi": (0, 10),
    "pricing.selling_fee_percent": (0, 50),
    "pricing.payment_fee_percent": (0, 50),
    "pricing.default_shipping_cost": (0, 1000),
    "pricing.safety_margin_percent": (0, 50),
    "pricing.asking_price_discount": (0.1, 1.5),
    "pricing.comps_limit": (1, 200),
    "pricing.vb_expected_discount": (0, 0.9),
    "pricing.max_capital": (1, 1000000),
    "pricing.min_comparables": (1, 100),
    "pricing.max_price_spread": (0, 10),
    "pricing.paypal_fixed_fee": (0, 100),
    "pricing.history_days": (1, 365),
    "pricing.history_min_points": (1, 100),
    "ai.max_images": (0, 10),
    "ai.timeout_seconds": (5, 1800),
    "ai.temperature": (0, 2),
    "ai.max_tokens": (50, 16000),
    "ai.image_max_side": (128, 4096),
    "ai.min_prefilter_score": (0, 100),
    "ai.second_opinion.min_score": (0, 100),
    "ai.second_opinion.max_per_run": (0, 1000),
    "ai.second_opinion.max_images": (0, 10),
    "ai.second_opinion.timeout_seconds": (5, 1800),
    "ai.vision_wait_minutes": (0, 1440),
    "ai.rpm": (0, 100000),
    "ai.daily_limit": (0, 10000000),
    "ai.scout.rpm": (0, 100000),
    "ai.scout.daily_limit": (0, 10000000),
    "ai.scout.timeout_seconds": (5, 1800),
    "ai.scout.temperature": (0, 2),
    "ai.scout.max_tokens": (200, 16000),
    "ai.scout.batch_size": (0, 32),  # 0 = by the model's size
    "ai.scout.min_batch": (1, 32),
    "ai.scout.max_batch": (0, 32),  # 0 = by the model's size
    "ai.scout.max_per_hour": (0, 20000),
    "ai.scout.pass_share": (0.05, 1),
    "ai.scout.min_interest": (0, 10),
    "ai.scout.backlog_hours": (0, 72),
    "ai.scout.bundle_discount": (0, 0.9),
    "ai.scout.pc_discount": (0, 0.9),
    "ai.scout.min_priced_share": (0, 1),
    "notifications.super_deals.min_profit": (0, 100000),
    "notifications.super_deals.min_roi": (0, 10),
    "notifications.super_deals.min_score": (0, 100),
    "notifications.daily_top.hour": (0, 23),
    "notifications.daily_top.per_search": (1, 20),
    "notifications.min_score": (0, 100),
    "notifications.max_alerts_per_hour": (0, 1000),
    "notifications.heartbeat_hour": (0, 23),
    "notifications.email.smtp_port": (1, 65535),
    "web.port": (1, 65535),
}
# a clearer message than "не меньше 0" for the money / percent settings
RANGE_MESSAGES: dict[str, str] = {
    "pricing.min_profit": "Прибыль не может быть отрицательной",
    "pricing.min_roi": "ROI — от 0 до 1000 %",
    "pricing.safety_margin_percent": "Запас на торг и риск — от 0 до 50 %",
    "pricing.selling_fee_percent": "Комиссия — от 0 до 50 %",
    "pricing.payment_fee_percent": "Комиссия — от 0 до 50 %",
    "pricing.default_shipping_cost": "Доставка — от 0 до 1000 €",
    "pricing.max_capital": "Бюджет должен быть больше нуля",
    "pricing.paypal_fixed_fee": "Комиссия — от 0 до 100 €",
    "general.min_listing_price": "Цена не может быть отрицательной",
    "general.interval_minutes": "Проверять можно не чаще раза в 5 минут и не реже раза в сутки",
}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """{"ai": {"model": "x", "second_opinion": {"enabled": true}}} -> dotted leaves."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and dotted not in ("pricing.reference_prices",):
            if not value:
                continue
            out.update(_flatten(value, dotted))
        else:
            out[dotted] = value
    return out


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    *parents, last = dotted.split(".")
    for key in parents:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[last] = value


def _known_key(model: dict[str, Any], dotted: str) -> bool:
    node: Any = model
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def settings_view(ctx: ApiContext) -> dict[str, Any]:
    cfg = ctx.config
    dump = cfg.model_dump(mode="json")
    for key in READ_ONLY_KEYS:
        if key == "general.data_dir":
            continue
        *parents, last = key.split(".")
        node = dump
        for part in parents:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(last, None)
    answers = answers_from_searches(cfg.searches, cfg.pricing.min_profit)
    from ...notify.base import missing_settings

    missing = missing_settings(cfg.notifications)
    dump["ebay"]["configured"] = cfg.ebay.configured
    bound = ctx.bind_host if ctx.bind_host is not None else cfg.web.host
    pending = []
    if (bound or "") != cfg.web.host:
        pending.append("web.host")
    return {
        "editable": ctx.config_path is not None and ctx.config_writable(),
        "config_path": str(ctx.config_path) if ctx.config_path else None,
        "env_path": str(ctx.env_path) if ctx.env_path else None,
        "region": {"location": answers.location, "location_label": place_label(answers.location),
                   "radius_km": answers.radius_km, "radius_choices": list(RADIUS_CHOICES)},
        **{section: dump[section] for section in SECTIONS},
        "secrets": ctx.secrets_status(),
        "channels": {
            "email": {"enabled": cfg.notifications.email.enabled, "missing": missing.get("email", [])},
            "telegram": {"enabled": cfg.notifications.telegram.enabled, "missing": missing.get("telegram", [])},
        },
        "restart_pending": pending,
    }


def _problems(cfg: AppConfig, keys: list[str]) -> dict[str, str]:
    """Checks the pydantic models don't do; only for the keys being changed."""
    problems: dict[str, str] = {}
    flat = _flatten(cfg.model_dump(mode="json"))
    for key in keys:
        value = flat.get(key)
        bounds = RANGES.get(key)
        if bounds and isinstance(value, (int, float)) and not isinstance(value, bool):
            low, high = bounds
            if low is not None and value < low:
                problems[key] = RANGE_MESSAGES.get(key, f"не меньше {low:g}")
            elif high is not None and value > high:
                problems[key] = RANGE_MESSAGES.get(key, f"не больше {high:g}")
    if "general.timezone" in keys:
        from ...timefmt import is_valid_timezone

        if not is_valid_timezone(cfg.general.timezone):
            problems["general.timezone"] = "Неизвестный часовой пояс — например, Europe/Berlin"
    ai = cfg.ai
    touched = set(keys)
    if touched & {"ai.base_url", "ai.provider", "ai.enabled"} and ai.provider != "anthropic":
        if ai.base_url and urlsplit(ai.base_url).scheme not in ("http", "https"):
            problems["ai.base_url"] = "адрес должен начинаться с http://, например http://localhost:1234/v1"
    if touched & {"ai.scout.base_url", "ai.scout.enabled"} and ai.scout.base_url.strip() \
            and urlsplit(ai.scout.base_url).scheme not in ("http", "https"):
        problems["ai.scout.base_url"] = "адрес должен начинаться с http://, например http://127.0.0.1:8080/v1"
    if touched & {"ai.scout.min_batch", "ai.scout.max_batch"} and ai.scout.max_batch \
            and ai.scout.min_batch > ai.scout.max_batch:
        problems["ai.scout.min_batch"] = "минимум не может быть больше максимума"
    if "ai.model" in touched and not ai.model.strip():
        problems["ai.model"] = "укажи модель, например qwen/qwen2.5-vl-7b"
    if "general.request_delay_seconds" in touched:
        low, high = cfg.general.request_delay_seconds
        if low < 0 or high < low or high > 120:
            problems["general.request_delay_seconds"] = "два числа от 0 до 120 секунд, первое не больше второго"
    if "general.block_cooldown_hours" in touched and any(h <= 0 for h in cfg.general.block_cooldown_hours):
        problems["general.block_cooldown_hours"] = "паузы должны быть больше нуля"
    if "notifications.verdicts" in touched and not cfg.notifications.verdicts:
        problems["notifications.verdicts"] = "выбери хотя бы «Покупать»"
    if "ai.second_opinion.verdicts" in touched and not ai.second_opinion.verdicts:
        problems["ai.second_opinion.verdicts"] = "выбери хотя бы один вердикт"
    from ...notify.base import missing_settings

    missing = missing_settings(cfg.notifications)
    if "notifications.telegram.enabled" in touched and missing.get("telegram"):
        problems["notifications.telegram.enabled"] = "Сначала подключи бота: вставь его ключ и нажми Start в Telegram"
    if "notifications.email.enabled" in touched and missing.get("email"):
        labels = {"smtp_host": "почтовый сервер", "smtp_port": "порт", "username": "адрес почты",
                  "password": "пароль приложения", "from_addr": "отправитель", "to_addrs": "куда слать"}
        problems["notifications.email.enabled"] = ("Сначала заполни почту: не хватает "
                                                   + ", ".join(labels.get(m, m) for m in missing["email"]))
    if "web.host" in touched and not cfg.web.host.strip():
        problems["web.host"] = "укажи адрес, например 127.0.0.1"
    if "ai.second_opinion.enabled" in touched and ai.second_opinion.enabled and ai.second_opinion.provider == "anthropic" \
            and not ai.second_opinion.api_key:
        problems["ai.second_opinion.enabled"] = "Сначала вставь ключ Claude (Anthropic)"
    return problems


def patch_settings(ctx: ApiContext, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a partial settings change, write only the changed keys (comments kept), apply live."""
    if not isinstance(patch, dict) or not patch:
        raise ApiError(400, "bad_request", "Пустое изменение настроек")
    region = patch.pop("region", None)
    errors: dict[str, str] = {}
    current = ctx.config.model_dump(mode="python")
    changes: dict[str, Any] = {}
    for section, values in patch.items():
        if section not in SECTIONS:
            errors[section] = "неизвестный раздел настроек"
            continue
        if not isinstance(values, dict):
            errors[section] = "нужен объект {поле: значение}"
            continue
        for key, value in _flatten(values, section).items():
            if key in READ_ONLY_KEYS:
                errors[key] = ("Ключи и пароли меняются на своих экранах подключения" if key != "general.data_dir"
                               else "Папку с данными отсюда поменять нельзя")
            elif not _known_key(current, key):
                errors[key] = "неизвестная настройка"
            else:
                changes[key] = value
    if errors:
        raise validation_error(errors, "Неизвестные или защищённые настройки")
    merged = ctx.config.model_dump(mode="python")
    for key, value in changes.items():
        _set_dotted(merged, key, value)
    try:
        validated = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise validation_error(exc) from exc
    problems = _problems(validated, list(changes))
    if problems:
        raise validation_error(problems)
    flat = _flatten(validated.model_dump(mode="json"))
    updates = {key: flat.get(key, dig(validated.model_dump(mode="json"), key)) for key in changes}
    applied: list[str] = []
    if updates:
        ctx.require_editable()
        ctx.write_values(updates, reason="settings")
        applied += list(updates)
        if any(k.startswith("ai.") for k in updates):
            ctx.mark_done("ai")
        if any(k.startswith(("pricing.", "notifications.min_score")) for k in updates):
            ctx.mark_done("money")
    if region:
        applied += _patch_region(ctx, region)
    restart = sorted(k for k in applied if k in RESTART_KEYS)
    return {"applied": applied, "restart_required": restart}


def _patch_region(ctx: ApiContext, region: Any) -> list[str]:
    """Move the Kleinanzeigen searches of the default region to a new place / radius (scan
    names follow: "Handy & Telefon · Hamburg 50 км")."""
    if not isinstance(region, dict):
        raise validation_error({"region": "нужен объект {location, radius_km}"})
    unknown = set(region) - {"location", "radius_km"}
    if unknown:
        raise validation_error({f"region.{k}": "неизвестная настройка" for k in unknown})
    cfg = ctx.config
    answers = answers_from_searches(cfg.searches, cfg.pricing.min_profit)
    location = " ".join(str(region.get("location") or answers.location).split())[:80]
    try:
        radius = snap_radius(int(region.get("radius_km", answers.radius_km)))
    except (TypeError, ValueError):
        raise validation_error({"region.radius_km": "нужно целое число километров"}) from None
    if not location:
        raise validation_error({"region.location": "укажи город или индекс"})
    old_loc = (answers.location or "").strip().lower()
    changed: list[Any] = []
    names = {s.name for s in cfg.searches}
    for search in cfg.searches:
        if search.source != "kleinanzeigen" or search.url or (search.location or "").strip().lower() != old_loc:
            changed.append(search)
            continue
        update: dict[str, Any] = {"location": location, "radius_km": radius or None}
        if search.category_name and search.name == scan_name(search.category_name, search.location, search.radius_km):
            new_name = scan_name(search.category_name, location, radius)
            if new_name not in names:
                names.discard(search.name)
                names.add(new_name)
                update["name"] = new_name
        changed.append(search.model_copy(update=update))
    ctx.save_searches(changed, backup=True, reason="region")
    ctx.mark_done("location")
    return ["region.location", "region.radius_km"]


# --------------------------------------------------------------------- app
def _learning(ctx: ApiContext, mon: dict[str, Any] | None = None) -> dict[str, Any]:
    """First passes of new searches only learn prices. `mon` (monitor_view without learning):
    the message then says honestly when the first alerts can come — after the next check (at
    HH:MM), after a site's pause, or only after «Проверить сейчас» when automatic checks are off."""
    cfg = ctx.config
    stats = ctx.db.search_stats()
    enabled = [(s, sid) for s, sid in zip(cfg.searches, search_ids(cfg.searches)) if s.enabled]
    learning = []
    for search, sid in enabled:
        info = stats.get(search.name) or {}
        last, baseline = info.get("last_run_at"), info.get("baseline_at")
        if last is None and not info.get("ads"):
            state = "pending" if cfg.general.baseline_first_run else "active"
        elif baseline is not None and last is not None and abs((last - baseline).total_seconds()) < 1:
            state = "learned"
        else:
            state = "active"
        if state != "active":
            learning.append({"id": sid, "name": search.name, "state": state})
    total = len(enabled)
    points = 0
    try:
        points = ctx.db.count_price_points()
    except Exception:  # noqa: BLE001
        pass
    message = ""
    eta_at: Any = None
    if learning and total:
        learned = sum(1 for s in learning if s["state"] == "learned")
        done = total - len(learning) + learned
        if done:
            head = f"Изучаю рынок: готово {done} из {total} · собрано {points} {_prices_word(points)}"
        elif points:
            head = f"Изучаю рынок · собрано {points} {_prices_word(points)}"
        else:
            head = "Изучаю рынок — первая проверка ещё впереди"
        tail, eta_at = _first_alerts(mon)
        message = f"{head}. {tail}"
    return {"baseline_first_run": cfg.general.baseline_first_run, "learning": bool(learning), "searches": learning,
            "enabled_searches": total, "price_points": points, "message_ru": message,
            "first_alerts_at": iso(eta_at), "first_alerts_label": when_label(eta_at) if eta_at else ""}


def _prices_word(n: int) -> str:
    from ...scraper.categories import _plural

    return _plural(n, "цена", "цены", "цен")


def _first_alerts(mon: dict[str, Any] | None) -> tuple[str, Any]:
    """('Первые уведомления — после паузы, около 21:40', when) for the learning note."""
    from datetime import datetime

    if not mon:
        return "Первые уведомления — после следующей проверки.", None
    if not mon.get("available") or mon.get("state") == "stopped":
        return "Автопроверка выключена — первые уведомления после проверок по кнопке «Проверить сейчас».", None
    if mon.get("paused"):
        return "Проверки на паузе — продолжи их, чтобы пошли уведомления.", None
    candidates = []
    for raw in ((mon.get("cooldown") or {}).get("until"), mon.get("next_run_at")):
        if raw:
            try:
                candidates.append(datetime.fromisoformat(str(raw)))
            except ValueError:
                pass
    when = max(candidates) if candidates else None
    if mon.get("cooldown") and when is not None:
        return f"Первые уведомления — после паузы, около {when_label(when)}.", when
    if when is not None:
        return f"Первые уведомления — после следующей проверки, около {when_label(when)}.", when
    return "Первые уведомления — после следующей проверки.", None


def onboarding_view(ctx: ApiContext) -> dict[str, Any]:
    cfg = ctx.config
    state = ctx.onboarding_state()
    done_marks, skipped = set(state["done"]), set(state["skipped"])
    searches = cfg.searches
    from ...notify.base import missing_settings

    computed = {
        "location": any(s.location for s in searches),
        "categories": bool(searches),
        "money": any(s.max_price is not None or s.target_price is not None for s in searches),
        "wishlist": any(s.purpose == "personal" for s in searches),
        "ai": cfg.ai.enabled,
        "telegram": cfg.notifications.telegram.enabled and not missing_settings(cfg.notifications).get("telegram"),
        "ebay": cfg.ebay.configured,
    }
    steps = []
    for key, title, required in ONBOARDING_STEPS:
        done = computed.get(key, False) or key in done_marks
        steps.append({"key": key, "title_ru": title, "required": required, "done": done,
                      "skipped": key in skipped and not done})
    next_step = next((s["key"] for s in steps if not s["done"] and not s["skipped"]), None)
    return {"steps": steps, "completed_at": state["completed_at"], "next_step": next_step,
            "required_done": all(s["done"] for s in steps if s["required"]),
            # a re-run of the wizard asks «Заменить текущие поиски или добавить?» when > 0
            "existing_searches": len(searches), "existing_search_names": [s.name for s in searches]}


def is_onboarded(ctx: ApiContext, view: dict[str, Any] | None = None) -> bool:
    """Onboarding finished, or config.yaml exists with ≥1 search and the AI on or explicitly
    skipped. A config made before the web onboarding (CLI wizard, by hand) with searches counts
    as onboarded even without AI."""
    view = view or onboarding_view(ctx)
    if view["completed_at"]:
        return True
    exists = ctx.config_path is not None and ctx.config_path.is_file()
    if not exists or not ctx.config.searches:
        return False
    try:
        bootstrapped = ctx.db.get_state(BOOTSTRAPPED_KEY) is not None
    except Exception:  # noqa: BLE001
        bootstrapped = False
    if not bootstrapped:
        return True
    return ctx.config.ai.enabled or any(s["key"] == "ai" and s["skipped"] for s in view["steps"])


def demo_ids() -> list[str]:
    if not DEMO_IDS_CACHE:
        from ...demo import demo_deals

        DEMO_IDS_CACHE.extend(listing.ad_id for listing, _ in demo_deals())
    return DEMO_IDS_CACHE


def demo_count(ctx: ApiContext) -> int:
    try:
        return ctx.db.count_deals_v1(ad_ids=demo_ids(), include_ignored=True)
    except Exception:  # noqa: BLE001
        return 0


@router.get("/app", response_model=AppOut, summary="Состояние приложения: онбординг, возможности, демо")
async def app_info(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...runtime import pillow_missing
    from ...notify.base import missing_settings

    from .routes_monitor import monitor_view

    cfg = ctx.config
    view = onboarding_view(ctx)
    mon = ctx.monitor
    missing = missing_settings(cfg.notifications)
    mon_view = await monitor_view(ctx, with_hosts=False)
    demo = demo_count(ctx)
    token_mode = bool(getattr(ctx.app.state, "access_token", None))
    return {
        "version": __version__,
        "onboarded": is_onboarded(ctx, view),
        "config_exists": bool(ctx.config_path and ctx.config_path.is_file()),
        "config_writable": ctx.config_writable(),
        "config_path": str(ctx.config_path) if ctx.config_path else None,
        "onboarding": view,
        "first_run": mon_view["learning"],
        "features": {
            "monitor": mon is not None and hasattr(mon, "run_once"),
            "pause": callable(getattr(mon, "pause", None)),
            "check_url": callable(getattr(mon, "evaluate_url", None)),
            "ai": cfg.ai.enabled,
            "second_opinion": cfg.ai.second_opinion.enabled,
            "telegram": cfg.notifications.telegram.enabled and not missing.get("telegram"),
            "email": cfg.notifications.email.enabled and not missing.get("email"),
            "ebay": cfg.ebay.configured,
            "pillow": not pillow_missing(),
            "restart": callable(getattr(ctx.app.state, "restart_callback", None)),
            "sse": True,
        },
        "demo": {"loaded": demo > 0, "count": demo},
        "counts": {
            "searches": len(cfg.searches),
            "searches_enabled": sum(1 for s in cfg.searches if s.enabled),
            "listings": ctx.db.count_deals_v1(include_ignored=True),
            "deals_good": ctx.db.count_deals_v1(verdicts=["buy", "maybe"]),
        },
        "monitor": {k: mon_view[k] for k in ("available", "running", "paused", "loop", "state", "state_ru",
                                             "next_run_at", "next_run_label", "cooldown")},
        "timezone": cfg.general.timezone,
        "access": {"token_required": token_mode,
                   "bind_host": ctx.bind_host if ctx.bind_host is not None else cfg.web.host},
        "server_time": utcnow().isoformat(),
    }


@router.get("/onboarding", response_model=OnboardingOut, summary="Шаги онбординга")
async def onboarding_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return onboarding_view(ctx)


@router.post("/onboarding/skip", response_model=OnboardingOut, responses=ERRORS,
             summary="Пропустить шаг онбординга (или вернуть: skipped=false)")
async def onboarding_skip(body: OnboardingSkipIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    if body.skipped:
        ctx.update_onboarding(skip=body.step)
    else:
        ctx.update_onboarding(unskip=body.step)
    return onboarding_view(ctx)


@router.post("/onboarding/complete", response_model=OnboardingOut, summary="Онбординг завершён (или сброшен)")
async def onboarding_complete(body: OnboardingCompleteIn | None = None,
                              ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    completed = body.completed if body is not None else True
    ctx.update_onboarding(completed=completed)
    if not completed:
        ctx.set_json("onboarding", {"skipped": [], "done": [], "completed_at": None})
    return onboarding_view(ctx)


@router.get("/onboarding/draft", summary="Черновик ответов онбординга (что угодно, JSON)")
async def onboarding_draft_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"draft": ctx.get_json(ONBOARDING_DRAFT_KEY, None)}


@router.put("/onboarding/draft", summary="Сохранить черновик онбординга (до 64 КБ JSON)")
async def onboarding_draft_put(request: Request, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_DRAFT_BYTES:
        raise ApiError(413, "too_large", "Черновик слишком большой")
    try:
        data = await request.json() if raw else None
    except ValueError:
        raise ApiError(400, "bad_request", "Нужен JSON") from None
    draft = data.get("draft", data) if isinstance(data, dict) else data
    ctx.set_json(ONBOARDING_DRAFT_KEY, draft)
    return {"draft": draft, "saved_at": utcnow().isoformat()}


@router.delete("/onboarding/draft", summary="Удалить черновик онбординга")
async def onboarding_draft_delete(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.set_json(ONBOARDING_DRAFT_KEY, None)
    return {"draft": None}


# ----------------------------------------------------------------- settings
@router.get("/settings", summary="Все настройки по разделам; секреты только как {set, masked}")
async def settings_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return settings_view(ctx)


@router.patch("/settings", responses=ERRORS,
              summary="Изменить настройки: {раздел: {поле: значение}}; ошибки — error.fields {поле: текст}")
async def settings_patch(patch: dict[str, Any] = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    result = patch_settings(ctx, dict(patch))
    return {**settings_view(ctx), **result}


@router.patch("/settings/{section}", responses=ERRORS, summary="Изменить один раздел: {поле: значение}")
async def settings_patch_section(section: str, patch: dict[str, Any] = Body(...),
                                 ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    if section not in (*SECTIONS, "region"):
        raise ApiError(404, "not_found", f"Нет раздела настроек «{section}»")
    result = patch_settings(ctx, {section: dict(patch)})
    return {**settings_view(ctx), **result}


_TG_TOKEN_RE = re.compile(r"^\d{5,12}:[\w-]{20,}$")
_TG_CHAT_RE = re.compile(r"^(-?\d{3,20}|@\w{4,})$")


def check_secret(name: str, value: str) -> str | None:
    if not value:
        return None
    if name == "telegram_bot_token" and not _TG_TOKEN_RE.match(value):
        return "похоже, скопировалось не всё — ключ выглядит так: 123456789:AA…"
    if name == "telegram_chat_id" and not _TG_CHAT_RE.match(value):
        return "Номер чата — это число, например 123456789"
    if name in ("smtp_user",) and "@" not in value:
        return "нужен адрес почты"
    if name == "notify_email" and not all("@" in part for part in value.split(",") if part.strip()):
        return "адреса через запятую, например me@gmail.com"
    if name in ("openrouter_api_key", "nvidia_api_key", "omniroute_api_key", "cloud_api_key") and (
            len(value) < 6 or any(ch.isspace() for ch in value)):
        return "похоже, скопировалось не всё — вставь ключ целиком, без пробелов"
    return None


@router.put("/secrets", responses=ERRORS,
            summary="Записать секреты в .env (в ответе только {set, masked}); null — не менять, \"\" — удалить")
async def secrets_put(body: SecretsIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    values = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump().items()}
    problems = {k: msg for k, v in values.items() if v and (msg := check_secret(k, v))}
    if problems:
        raise validation_error(problems)
    written = ctx.write_secrets(values)
    steps = []
    if {"telegram_bot_token", "telegram_chat_id"} & set(written):
        steps.append("telegram")
    if {"ebay_client_id", "ebay_client_secret", "ebay_oauth_token"} & set(written) and ctx.config.ebay.configured:
        steps.append("ebay")
    if steps:
        ctx.mark_done(*steps)
    return {"updated": written, "secrets": ctx.secrets_status()}


# ------------------------------------------------------------ phone access
def _tailscale_ips() -> list[str]:
    net = ipaddress.ip_network("100.64.0.0/10")
    out = []
    for addr in sorted(local_addresses()):
        try:
            if ipaddress.ip_address(addr) in net:
                out.append(addr)
        except ValueError:
            continue
    return out


def _lan_ips() -> list[str]:
    out = []
    for addr in sorted(local_addresses()):
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback and ip not in ipaddress.ip_network("100.64.0.0/10"):
            out.append(addr)
    return out


def access_view(ctx: ApiContext) -> dict[str, Any]:
    cfg = ctx.config.web
    host = cfg.host.strip()
    tailscale = _tailscale_ips()
    if is_loopback(host):
        mode = "local"
    elif host in ("0.0.0.0", "::", ""):
        mode = "lan"
    elif host in tailscale or host.startswith("100."):
        mode = "tailscale"
    else:
        mode = "custom"
    live_token = getattr(ctx.app.state, "access_token", None)
    token = live_token
    if mode != "local" and not token:
        token = ensure_token(ctx.config.data_path)  # the link the phone will need after the restart
    required = mode != "local" or bool(live_token)
    port = cfg.port
    query = f"/?{TOKEN_PARAM}={token}" if token and mode != "local" else "/"
    urls: list[dict[str, str]] = []
    if mode == "lan":
        urls += [{"label": f"Домашняя сеть ({ip})", "url": f"http://{ip}:{port}{query}"} for ip in _lan_ips()]
        urls += [{"label": f"Tailscale ({ip})", "url": f"http://{ip}:{port}{query}"} for ip in tailscale]
    elif mode in ("tailscale", "custom"):
        urls.append({"label": "С телефона", "url": f"http://{host}:{port}{query}"})
    urls += [{"label": f"Имя {h}", "url": f"http://{h}:{port}{query}"} for h in cfg.allowed_hosts if h != "*"]
    bound = ctx.bind_host if ctx.bind_host is not None else host
    return {
        "mode": mode,
        "host": host,
        "port": port,
        "bound_host": bound,
        "restart_required": (bound or "") != host,
        "token_required": required,
        "token": token if required else None,
        "token_file": str(ctx.config.data_path / TOKEN_FILE),
        "urls": urls,
        "qr_payload": urls[0]["url"] if urls else None,
        "tailscale_ips": tailscale,
        "lan_ips": _lan_ips(),
        "allowed_hosts": list(cfg.allowed_hosts),
    }


@router.get("/access", summary="Доступ с телефона: режим, ссылки с ключом, данные для QR")
async def access_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return access_view(ctx)


@router.put("/access", responses=ERRORS, summary="Сменить режим доступа (нужен перезапуск программы)")
async def access_put(body: AccessIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    if body.mode == "local":
        host = "127.0.0.1"
    elif body.mode == "lan":
        host = "0.0.0.0"
    else:
        host = (body.host or "").strip() or next(iter(_tailscale_ips()), "")
        if not host:
            raise ApiError(422, "tailscale_not_found",
                           "Tailscale не найден на этом компьютере — установи Tailscale и войди в аккаунт, "
                           "либо впиши адрес компьютера в Tailscale (100.x.y.z) вручную",
                           fields={"host": "Tailscale не найден — впиши адрес 100.x.y.z или установи Tailscale"})
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.match(r"^[A-Za-z0-9.-]+$", host):
                raise validation_error({"host": "нужен IP-адрес или имя компьютера"}) from None
    updates: dict[str, Any] = {"web.host": host}
    if body.port is not None:
        updates["web.port"] = body.port
    if body.allowed_hosts is not None:
        hosts = [h.strip().lower() for h in body.allowed_hosts if h.strip()]
        if any(not re.match(r"^[a-z0-9.*:-]+$", h) for h in hosts):
            raise validation_error({"allowed_hosts": "только имена вида my-pc.tail1234.ts.net"})
        updates["web.allowed_hosts"] = hosts
    if body.mode != "local":
        ensure_token(ctx.config.data_path)
    ctx.write_values(updates, reason="access")
    return access_view(ctx)


@router.post("/access/rotate-token", summary="Новый ключ доступа (старые ссылки перестанут работать)")
async def access_rotate(ctx: ApiContext = Depends(get_ctx)) -> JSONResponse:
    path = ctx.config.data_path / TOKEN_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ApiError(500, "write_failed", "Не получилось заменить ключ доступа — закрой другие программы, "
                       "которые могут держать папку с данными, и попробуй ещё раз", details=f"{path}: {exc}") from exc
    token = ensure_token(ctx.config.data_path)
    live = bool(getattr(ctx.app.state, "access_token", None))
    if live:
        ctx.app.state.access_token = token
    response = JSONResponse({**access_view(ctx), "rotated": True})
    if live:  # keep this browser signed in
        response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return response


@router.post("/system/restart", status_code=202, summary="Перезапустить программу (новые web.host/port)")
async def system_restart(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    callback = getattr(ctx.app.state, "restart_callback", None)
    if not callable(callback):
        raise ApiError(503, "unavailable", "Перезапуск отсюда недоступен — закрой окно программы и запусти её снова")
    asyncio.get_running_loop().call_later(0.5, callback)
    return {"restarting": True, "message_ru": "Перезапускаю… Страница переподключится сама"}
