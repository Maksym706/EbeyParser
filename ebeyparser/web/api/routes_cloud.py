"""Free cloud AI (docs/design/CLOUD_AI.md): the presets and the current choice, the live check a
user runs from their own machine («Проверить»: key, free / vision models, limits, a tiny scout
batch and a demo photo), and saving «Где работает нейросеть» (cloud / local / cloud + local).

The key only ever goes to .env (like the Telegram token); responses show it masked."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from ...ai import cloud as cloud_ai
from ...config import LLMSettings
from ...errors_ru import ai_problem
from .context import CLOUD_SECRET, ApiContext, get_ctx, mask
from .errors import validation_error
from .presenters import rnd
from .schemas import ERRORS, CloudSaveIn, CloudTestIn

log = logging.getLogger(__name__)
router = APIRouter()

PROBE_TIMEOUT = 20.0
PRIVACY_RU = ("В облако уходят только название, описание, цена, характеристики и фото объявления — без имени "
              "продавца, его телефона и почты, без твоего адреса, заметок и переписки.")


def _mode(ctx: ApiContext) -> str:
    ai = ctx.config.ai
    if not cloud_ai.cloud_kind(ai):
        return "local"
    return "hybrid" if ai.fallback.usable else "cloud"


def _keys(ctx: ApiContext) -> dict[str, dict[str, Any]]:
    status = ctx.secrets_status()
    return {kind: {"set": status[name]["set"], "masked": status[name]["masked"], "env": status[name]["env"]}
            for kind, name in CLOUD_SECRET.items()}


def cloud_status(ctx: ApiContext) -> dict[str, Any]:
    """The «Облако» block: from the running monitor, else from the config and the stored counters."""
    fn = getattr(ctx.monitor, "cloud_status", None)
    if callable(fn):
        try:
            view = fn()
            if isinstance(view, dict):
                return view
        except Exception:  # noqa: BLE001 - a status block never breaks a page
            log.exception("cloud status failed")
    from ...monitor import cloud_roles

    cloud_ai.registry().attach(ctx.db)
    return cloud_ai.cloud_view(cloud_roles(ctx.config), send_ebay=ctx.config.ai.cloud_send_ebay)


def cloud_view(ctx: ApiContext) -> dict[str, Any]:
    ai, sc = ctx.config.ai, ctx.config.ai.scout
    kind = cloud_ai.cloud_kind(ai)
    return {
        "mode": _mode(ctx),
        "presets": [p.as_dict() for p in cloud_ai.PRESETS.values()],
        "current": {
            "provider": kind or None,
            "base_url": ai.base_url if kind else "",
            "vision_model": ai.model if kind else "",
            "text_model": (sc.model or ai.model) if kind and sc.enabled else "",
            "scout_enabled": sc.enabled,
            "ai_enabled": ai.enabled,
            "rpm": ai.rpm,
            "daily_limit": ai.daily_limit,
            "fallback": ai.fallback.model_dump(),
            "scout_fallback": sc.fallback.model_dump(),
            "send_ebay": ai.cloud_send_ebay,
        },
        "keys": _keys(ctx),
        "status": cloud_status(ctx),
        "privacy_ru": PRIVACY_RU,
        "ebay_ru": cloud_ai.EBAY_RULE_RU,
    }


@router.get("/ai/cloud", summary="Бесплатная нейросеть в облаке: провайдеры, текущий выбор, ключи (скрыты), расход")
async def cloud_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return cloud_view(ctx)


# ---------------------------------------------------------------------------- «Проверить»
def _sec(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds:.1f}".replace(".", ",") + " с"


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int, Any, httpx.Headers]:
    resp = await client.get(url)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data, resp.headers


def _settings(provider: str, base_url: str, key: str, model: str, *, timeout: float, max_images: int = 0) -> LLMSettings:
    return LLMSettings(provider="openai", base_url=base_url, model=model, api_key=key, cloud=provider,  # type: ignore[arg-type]
                       max_images=max_images, timeout_seconds=timeout, temperature=0.1,
                       max_tokens=cloud_ai.CLOUD_TRIAGE_MAX_TOKENS if not max_images else 700)


async def _triage_check(ctx: ApiContext, settings: LLMSettings) -> dict[str, Any]:
    from ...ai.client import VisionLLM
    from ...ai.triage import TriageEngine, check_sample

    out: dict[str, Any] = {"ok": False, "model": settings.model, "seconds": None, "sec_per_ad": None, "answered": 0,
                           "total": 5, "hidden_gpu": False, "typo_fixed": False, "wanted_seen": False, "error_ru": ""}
    llm = VisionLLM(settings, transport=ctx.http_transport, purpose="triage", force_quota=True)
    try:
        engine = TriageEngine(llm, model=settings.model, batch_size=5, min_batch=5, max_batch=5,
                              max_tokens=settings.max_tokens, max_per_hour=0)
        started = time.monotonic()
        run = await engine.triage(cloud_ai.demo_ads())
        seconds = time.monotonic() - started
    finally:
        await llm.aclose()
    out["seconds"] = rnd(seconds, 1)
    if run.error:
        out["error_ru"] = ai_problem("openai", settings.base_url, settings.model, run.error, server_ok=None,
                                     cloud=settings.cloud)
        return out
    sample = check_sample(run)
    out.update({k: sample[k] for k in ("answered", "hidden_gpu", "typo_fixed", "wanted_seen", "items")})
    out["answered"] = run.ai_count
    if run.ai_count:
        out["sec_per_ad"] = rnd(seconds / run.ai_count, 2)
    quality = sum(bool(out[k]) for k in ("hidden_gpu", "typo_fixed", "wanted_seen"))
    out["ok"] = run.ai_count >= 4 and quality >= 2
    if not out["ok"]:
        out["error_ru"] = ("Модель ответила, но читает объявления плохо — выбери другую (например, Nemotron 3 Super)"
                           if run.ai_count else "Модель ответила не в том формате — выбери другую модель")
    return out


async def _photo_check(ctx: ApiContext, settings: LLMSettings) -> dict[str, Any]:
    from ...ai.client import VisionLLM
    from ...ai.evaluator import AIEvaluator
    from .routes_connect import sample_image, sample_listing

    out: dict[str, Any] = {"ok": False, "model": settings.model, "seconds": None, "product": "", "recognised": False,
                           "error_ru": ""}
    llm = VisionLLM(settings, transport=ctx.http_transport, purpose="vision", force_quota=True)
    try:
        started = time.monotonic()
        verdict = await AIEvaluator(llm, settings).evaluate(sample_listing(), [sample_image()], purpose="resale")
        out["seconds"] = rnd(time.monotonic() - started, 1)
    finally:
        await llm.aclose()
    if verdict.confidence <= 0:
        reason = verdict.reasoning.removeprefix("Нейросеть не проверила фото: ")
        out["error_ru"] = (ai_problem("openai", settings.base_url, settings.model, reason, server_ok=None,
                                      cloud=settings.cloud) if "разобрать" not in reason
                           else "Модель ответила не в том формате — выбери другую модель для фото")
        return out
    out["product"] = verdict.product
    out["recognised"] = "iphone" in f"{verdict.product} {verdict.search_query}".lower()
    out["ok"] = True
    return out


@router.post("/ai/cloud/test", responses=ERRORS,
             summary="Проверить облако с этого компьютера: ключ, бесплатные модели, лимиты, 5 объявлений-примеров, фото")
async def cloud_test(body: CloudTestIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    p = cloud_ai.PRESETS[body.provider]
    base_url = (body.base_url or "").strip().rstrip("/") or p.base_url
    if not base_url:
        raise validation_error({"base_url": "укажи адрес сервиса, например https://…/v1"})
    if not base_url.lower().startswith(("http://", "https://")):
        raise validation_error({"base_url": "адрес должен начинаться с https://"})
    typed = (body.api_key or "").strip()
    key = typed or cloud_ai.stored_key(body.provider)
    if p.key_required and not key:
        raise validation_error({"api_key": "вставь ключ — кнопка «Получить ключ» откроет страницу, где его взять"})
    result: dict[str, Any] = {
        "ok": False, "provider": p.key, "provider_name": p.name, "base_url": base_url, "key_ok": None,
        "key_masked": mask(key) if key else "", "key_saved": False, "key_info": None, "models": [],
        "picks": {"text": None, "vision": None}, "text_model": None, "vision_model": None,
        "limits": {"rpm": p.rpm or None, "daily_limit": p.daily_limit or None, "is_free_tier": None,
                   "daily_limit_paid": p.daily_limit_paid or None},
        "rate_headers": {}, "triage": None, "photo": None, "budget": None, "limits_ru": "", "summary_ru": "",
        "message_ru": "", "error_ru": "", "warnings": [], "privacy_ru": PRIVACY_RU, "terms_ru": p.terms_ru,
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if p.key == "openrouter":
        headers.update(cloud_ai.OPENROUTER_HEADERS)

    def fail(text: str, details: str = "") -> dict[str, Any]:
        result["error_ru"] = result["message_ru"] = text
        if details:
            result["details"] = cloud_ai.redact_key(details, key)
        return result

    async with httpx.AsyncClient(transport=ctx.http_transport, headers=headers,
                                 timeout=httpx.Timeout(PROBE_TIMEOUT, connect=10.0)) as client:
        # 1. the key (OpenRouter tells the account's limits; the others answer on the first chat call)
        try:
            if p.key == "openrouter":
                status, data, _ = await _get_json(client, f"{base_url}/key")
                if status in (401, 403):
                    result["key_ok"] = False
                    return fail(f"Ключ {p.name} не подходит — скопируй его заново на странице ключей")
                if status < 400 and data is not None:
                    info = cloud_ai.parse_key_info(data)
                    result["key_ok"], result["key_info"] = True, info
                    result["limits"].update(is_free_tier=info["is_free_tier"])
                    if info["daily_limit"]:
                        result["limits"]["daily_limit"] = info["daily_limit"]
            # 2. the models: free ones, vision marked
            status, data, _ = await _get_json(client, f"{base_url}/models")
        except httpx.TimeoutException as exc:
            return fail(f"{p.name} не ответил за {PROBE_TIMEOUT:g} с — попробуй ещё раз через минуту", str(exc))
        except httpx.HTTPError as exc:
            if p.key == "omniroute":
                return fail("OmniRoute не отвечает — запусти его на этом компьютере и нажми «Проверить» ещё раз",
                            f"{type(exc).__name__}: {exc}")
            return fail(f"Нет связи с {p.name} — проверь интернет", f"{type(exc).__name__}: {exc}")
    if status in (401, 403):
        result["key_ok"] = False
        return fail(f"Ключ {p.name} не подходит — скопируй его заново на странице ключей")
    models = cloud_ai.parse_models(data, p.key) if status < 400 else []
    if p.key == "openrouter":
        models = [m for m in models if m["free"]]
    picks = cloud_ai.pick_models(models, p.key)
    result["picks"] = picks
    text_model = (body.text_model or "").strip() or picks["text"]
    vision_model = (body.vision_model or "").strip() or picks["vision"]
    ids = {m["id"] for m in models}
    for m in models:
        m["recommended_for"] = [r for r, mid in (("text", picks["text"]), ("vision", picks["vision"])) if mid == m["id"]]
    result["models"] = models[:120]
    result["text_model"], result["vision_model"] = text_model, vision_model
    if models and text_model and text_model not in ids:
        result["warnings"].append(f"Модели «{text_model}» нет в списке {p.name} — выбери другую")
    if not models:
        result["warnings"].append(f"{p.name} не показал список бесплатных моделей — впиши название вручную")
    if not text_model:
        return fail(f"У {p.name} не нашлось бесплатной модели для текста — выбери другой сервис")

    # 3. a tiny scout batch (5 demo ads): latency, quality, the rate-limit headers
    limiter = cloud_ai.registry().get(base_url, key, kind=p.key, label=text_model)
    timeout = float(body.timeout_seconds)
    if body.triage:
        result["triage"] = await _triage_check(ctx, _settings(p.key, base_url, key, text_model, timeout=timeout))
        if result["triage"]["error_ru"] and any(w in result["triage"]["error_ru"] for w in ("Ключ", "ключ")):
            result["key_ok"] = False
        elif result["triage"]["answered"]:
            result["key_ok"] = True
    if body.photo and vision_model:
        result["photo"] = await _photo_check(ctx, _settings(p.key, base_url, key, vision_model, timeout=timeout,
                                                            max_images=1))
        if result["photo"]["ok"]:
            result["key_ok"] = True
    result["rate_headers"] = dict(limiter.last_headers)
    header_limit = cloud_ai.as_number(limiter.last_headers.get("x-ratelimit-limit"))
    rpm = result["limits"]["rpm"] or (int(header_limit) if header_limit and header_limit < 1000 else 0)
    daily = result["limits"]["daily_limit"] or 0
    result["limits"]["rpm"] = rpm or None
    budget = cloud_ai.plan_budget(rpm, daily)
    result["budget"] = budget
    result["limits_ru"] = cloud_ai.limits_text(p.key, rpm, daily)

    # 4. the summary line
    triage, photo = result["triage"], result["photo"]
    if result["key_ok"] is False:
        return fail((triage or {}).get("error_ru") or f"Ключ {p.name} не подходит")
    if triage is not None and not triage["answered"]:
        return fail(triage["error_ru"] or f"{p.name} не ответил")
    parts = ["Ключ работает" if key else f"{p.name} работает", result["limits_ru"]]
    if triage and triage["seconds"] is not None:
        parts.append(f"ответ за {_sec(triage['seconds'])}")
    ads = budget["ads_per_day"]
    parts.append(f"хватит на ~{cloud_ai.int_ru(ads)} объявлений в день" if ads else "хватит на все объявления")
    result["summary_ru"] = result["message_ru"] = " · ".join(parts)
    result["ok"] = bool((triage is None or triage["answered"]) and (photo is None or photo["ok"]))
    if triage and not triage["ok"] and triage["error_ru"]:
        result["warnings"].append(triage["error_ru"])
    if photo and not photo["ok"] and photo["error_ru"]:
        result["warnings"].append(photo["error_ru"])
    if result["ok"] and body.save_key and typed:
        ctx.write_secrets({CLOUD_SECRET[p.key]: typed})
        result["key_saved"] = True
    return result


# ---------------------------------------------------------------------------- saving
@router.put("/ai/cloud", responses=ERRORS,
            summary="Где работает нейросеть: cloud (облако) · hybrid (облако + компьютер про запас) · local")
async def cloud_put(body: CloudSaveIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    ai, sc = ctx.config.ai, ctx.config.ai.scout
    updates: dict[str, Any] = {}
    if body.send_ebay is not None:
        updates["ai.cloud_send_ebay"] = body.send_ebay
    was_local = not cloud_ai.cloud_kind(ai) and ai.provider in ("openai", "ollama")
    if body.mode == "local":
        fb = ai.fallback
        if cloud_ai.cloud_kind(ai) and fb.base_url.strip() and fb.model.strip():
            # back to the computer: the local model remembered when the cloud was switched on
            updates.update({"ai.provider": fb.provider, "ai.base_url": fb.base_url.strip(), "ai.model": fb.model.strip()})
            if sc.enabled and not sc.base_url.strip():
                updates["ai.scout.model"] = sc.fallback.model.strip()
        updates.update({"ai.cloud": "", "ai.fallback.enabled": False, "ai.scout.fallback.enabled": False})
        ctx.write_values(updates, reason="ai")
        return cloud_view(ctx)
    p = cloud_ai.PRESETS[body.provider]
    base_url = (body.base_url or "").strip().rstrip("/") or p.base_url
    problems: dict[str, str] = {}
    if not base_url or not base_url.lower().startswith(("http://", "https://")):
        problems["base_url"] = "укажи адрес сервиса, например https://…/v1"
    vision_model = (body.vision_model or "").strip()
    text_model = (body.text_model or "").strip()
    if not vision_model and not text_model:
        problems["text_model"] = "выбери модель — нажми «Проверить», и я покажу список"
    typed = (body.api_key or "").strip()
    if p.key_required and not typed and not cloud_ai.stored_key(p.key):
        problems["api_key"] = "вставь ключ — кнопка «Получить ключ» откроет страницу, где его взять"
    fb_in = body.fallback
    if body.mode == "hybrid" and fb_in is not None and fb_in.base_url.strip() and not fb_in.model.strip():
        problems["fallback.model"] = "выбери модель на компьютере"
    if problems:
        raise validation_error(problems)
    if typed:
        from .routes_app import check_secret

        why = check_secret(CLOUD_SECRET[p.key], typed)
        if why:
            raise validation_error({"api_key": why})
        ctx.write_secrets({CLOUD_SECRET[p.key]: typed})
    if was_local and ai.base_url.strip() and ai.model.strip() and not ai.fallback.base_url.strip():
        # remember the computer's model: «Облако + компьютер про запас» / «На моём компьютере» later
        updates.update({"ai.fallback.provider": ai.provider, "ai.fallback.base_url": ai.base_url.strip(),
                        "ai.fallback.model": ai.model.strip()})
        if sc.model.strip() and not sc.base_url.strip():
            updates["ai.scout.fallback.model"] = sc.model.strip()
    updates.update({
        "ai.enabled": bool(vision_model) or ai.enabled,
        "ai.cloud": p.key, "ai.provider": "openai", "ai.base_url": base_url,
        "ai.model": vision_model or text_model, "ai.api_key": "",
    })
    if body.rpm is not None:
        updates["ai.rpm"] = body.rpm
    if body.daily_limit is not None:
        updates["ai.daily_limit"] = body.daily_limit
    if body.scout and text_model:
        updates.update({"ai.scout.enabled": True, "ai.scout.base_url": "", "ai.scout.model": text_model,
                        "ai.scout.api_key": ""})
    hybrid = body.mode == "hybrid"
    updates["ai.fallback.enabled"] = hybrid
    updates["ai.scout.fallback.enabled"] = False  # the scout's stand-in: the ai.fallback server
    if hybrid and fb_in is not None and fb_in.base_url.strip():
        updates.update({"ai.fallback.provider": fb_in.provider, "ai.fallback.base_url": fb_in.base_url.strip(),
                        "ai.fallback.model": fb_in.model.strip()})
        updates["ai.scout.fallback.model"] = fb_in.text_model.strip()
    if hybrid and not ((fb_in and fb_in.base_url.strip()) or ai.fallback.base_url.strip()
                       or updates.get("ai.fallback.base_url")):
        raise validation_error({"fallback": "выбери модель на компьютере, которая заменит облако"})
    ctx.write_values(updates, reason="ai")
    ctx.mark_done("ai")
    return {**cloud_view(ctx), "saved": True}


__all__ = ["PRIVACY_RU", "cloud_status", "cloud_view", "router"]
