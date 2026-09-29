"""Connections the onboarding / settings screens check live: local AI, Telegram (guided bot
linking), e-mail, eBay keys, test notifications and the notification-threshold preview."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import struct
import time
import zlib
from datetime import timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query

from ...config import EbayConfig, EmailConfig, NotificationsConfig, TelegramConfig
from ...models import DealView, Listing, utcnow
from ..localai import LMSTUDIO_URL, OLLAMA_URL, detect_local_ai, probe_json
from .context import ApiContext, get_ctx, mask
from .errors import ApiError, validation_error
from .presenters import CONDITION_LABELS, rnd
from .schemas import ERRORS, AiTestIn, EbayTestIn, EmailTestIn, NotifyTestIn, TelegramTestIn, TelegramTokenIn

log = logging.getLogger(__name__)
router = APIRouter()

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT = 15.0
LINK_KEY = "telegram:link"
LINK_TTL = timedelta(minutes=15)
SLOW_AI_SECONDS = 90.0
GPU_PRESETS = [
    {"key": "10-12", "label_ru": "10–12 ГБ", "model_ru": "Qwen2.5-VL-7B", "lmstudio": "Qwen2.5-VL-7B-Instruct",
     "ollama": "qwen2.5vl:7b", "note_ru": "лучшее качество, понимает немецкий; на 8 ГБ — 2 фото", "recommended": True},
    {"key": "6-8", "label_ru": "6–8 ГБ", "model_ru": "Gemma 3 4B или MiniCPM-V", "lmstudio": "gemma-3-4b-it",
     "ollama": "gemma3:4b", "note_ru": "занимает 5–8 ГБ", "recommended": False},
    {"key": "weak", "label_ru": "Слабый ПК", "model_ru": "Qwen2.5-VL-3B", "lmstudio": "Qwen2.5-VL-3B-Instruct",
     "ollama": "qwen2.5vl:3b", "note_ru": "на процессоре 1–3 минуты на объявление", "recommended": False},
    {"key": "16-24", "label_ru": "16–24 ГБ", "model_ru": "Gemma 3 12B или Qwen2.5-VL-32B", "lmstudio": "gemma-3-12b-it",
     "ollama": "gemma3:12b", "note_ru": "для RTX 3090 и мощнее", "recommended": False},
]


# ====================================================================== AI
def _pillow() -> dict[str, Any]:
    from ...runtime import PILLOW_WARNING, pillow_missing

    missing = pillow_missing()
    return {"installed": not missing, "warning_ru": PILLOW_WARNING if missing else ""}


@router.post("/ai/detect", summary="Найти LM Studio (:1234) и Ollama (:11434) на этом компьютере и их vision-модели")
@router.get("/ai/detect", include_in_schema=False)
async def ai_detect(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...ai.client import looks_like_vision_model

    servers = await asyncio.to_thread(detect_local_ai, ctx.ai_probe or probe_json)
    out = []
    for server in servers:
        default = server.default_model
        out.append({
            **server.as_dict(),
            "models": [{"id": m, "vision": looks_like_vision_model(m), "recommended": m == default and bool(
                server.vision_models)} for m in server.models],
            "model_ids": list(server.models),
        })
    vision = [s for s in out if s["vision_models"]]
    if vision:
        variant, text = "found", f"Нашёл {vision[0]['name']} · модель видит фото"
    elif out:
        variant = "no_vision"
        text = f"{out[0]['name']} работает, но нет модели, которая понимает фото — скачай Qwen2.5-VL-7B-Instruct"
    else:
        variant, text = "none", "LM Studio и Ollama не найдены — установи LM Studio и запусти сервер"
    best = vision[0] if vision else (out[0] if out else None)
    return {
        "servers": out,
        "variant": variant,
        "message_ru": text,
        "suggested": {"provider": best["provider"], "base_url": best["base_url"], "model": best["default_model"]}
        if best else {"provider": "openai", "base_url": LMSTUDIO_URL, "model": "qwen/qwen2.5-vl-7b"},
        "gpu_presets": GPU_PRESETS,
        "pillow": _pillow(),
        "current": {"enabled": ctx.config.ai.enabled, "provider": ctx.config.ai.provider,
                    "base_url": ctx.config.ai.base_url, "model": ctx.config.ai.model},
    }


def sample_listing() -> Listing:
    """The bundled sample ad for «Проверить на примере»."""
    return Listing(
        ad_id="sample-ai-test", url="https://www.kleinanzeigen.de/s-anzeige/sample/0",
        title="Apple iPhone 13 128GB Mitternacht – Top Zustand mit OVP",
        price=350.0, price_text="350 € VB", negotiable=True, location="10247 Friedrichshain",
        description=("Verkaufe mein iPhone 13 mit 128 GB in Mitternacht. Akkukapazität 89 %, immer mit Hülle und "
                     "Panzerglas benutzt, keine Kratzer. Originalverpackung und Ladekabel dabei. Nicht in iCloud "
                     "gesperrt, kein Netlock. Abholung in Berlin oder Versand möglich."),
        image_urls=["sample://iphone"], seller_type="private", shipping_possible=True, detail_loaded=True,
    )


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A plain PNG without Pillow."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def sample_image() -> bytes:
    """A drawn phone photo: dark iPhone-like body with a camera block and the words "iPhone 13"."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return _png(64, 64, (30, 30, 40))
    img = Image.new("RGB", (480, 640), (233, 228, 220))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((110, 60, 370, 590), radius=42, fill=(28, 32, 42), outline=(90, 94, 104), width=4)
    draw.rounded_rectangle((135, 85, 255, 205), radius=26, fill=(44, 48, 58))
    for cx, cy in ((168, 118), (222, 172)):
        draw.ellipse((cx - 24, cy - 24, cx + 24, cy + 24), fill=(12, 12, 16), outline=(120, 124, 132), width=3)
    draw.ellipse((218, 108, 234, 124), fill=(200, 200, 190))
    try:
        font = ImageFont.load_default(size=34)
        small = ImageFont.load_default(size=24)
    except TypeError:  # old Pillow: fixed-size bitmap font
        font = small = ImageFont.load_default()
    draw.text((240, 380), "iPhone 13", fill=(235, 235, 235), font=font, anchor="mm")
    draw.text((240, 425), "128 GB", fill=(200, 200, 200), font=small, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _llm(ctx: ApiContext, settings: Any) -> Any:
    if settings.provider in ("openai", "ollama"):
        from ...ai.client import VisionLLM

        return VisionLLM(settings, transport=ctx.http_transport)
    from ...ai.claude import make_llm

    return make_llm(settings)


@router.post("/ai/test", summary="Проверить нейросеть: сервер, модель и ответ на объявлении-примере (фото + текст)")
async def ai_test(body: AiTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    body = body or AiTestIn()
    ai = ctx.config.ai
    provider = body.provider or ai.provider
    base_url = (body.base_url if body.base_url is not None else ai.base_url).strip()
    if provider == "ollama" and not base_url:
        base_url = OLLAMA_URL
    elif provider == "openai" and not base_url:
        base_url = LMSTUDIO_URL
    model = (body.model or ai.model).strip()
    if not model:
        raise validation_error({"model": "укажи модель, например qwen/qwen2.5-vl-7b"})
    settings = ai.model_copy(update={
        "provider": provider, "base_url": base_url, "model": model,
        "api_key": body.api_key if body.api_key is not None else ai.api_key,
        "timeout_seconds": body.timeout_seconds,
    })
    result: dict[str, Any] = {
        "ok": False, "provider": provider, "base_url": base_url, "model": model, "server_ok": False,
        "model_available": False, "resolved_model": None, "models": [], "health_ms": None, "seconds": None,
        "verdict": None, "vision_ok": None, "message_ru": "", "warning_ru": "", "error_ru": "", "saved": False,
        "pillow": _pillow(),
    }
    try:
        llm = _llm(ctx, settings)
    except Exception as exc:  # noqa: BLE001 - e.g. the anthropic package is missing
        result["error_ru"] = result["message_ru"] = f"Не удалось подключиться: {exc}"
        return result
    try:
        started = time.monotonic()
        try:
            health = await asyncio.wait_for(llm.health(), timeout=20)
        except asyncio.TimeoutError:
            health = {"ok": False, "server_ok": False, "error": "сервер не ответил за 20 с"}
        result["health_ms"] = int((time.monotonic() - started) * 1000)
        result.update({
            "server_ok": bool(health.get("server_ok", health.get("ok"))),
            "model_available": bool(health.get("model_available", health.get("ok"))),
            "resolved_model": health.get("resolved_model"),
            "models": list(health.get("models") or []),
        })
        if not result["server_ok"]:
            result["error_ru"] = result["message_ru"] = str(health.get("error") or "Сервер нейросети не отвечает")
            return result
        if not result["model_available"]:
            result["error_ru"] = result["message_ru"] = str(health.get("error") or f"Модели «{model}» нет на сервере")
            return result
        if not body.sample:
            result["ok"] = True
            result["message_ru"] = f"Сервер работает, модель {result['resolved_model'] or model} найдена"
        else:
            await _sample_check(llm, settings, result)
    finally:
        try:
            await llm.aclose()
        except Exception:  # noqa: BLE001
            pass
    if result["ok"] and body.save:
        ctx.write_values({"ai.enabled": True, "ai.provider": provider, "ai.base_url": base_url,
                          "ai.model": result["resolved_model"] or model}, reason="ai")
        ctx.mark_done("ai")
        result["saved"] = True
    return result


async def _sample_check(llm: Any, settings: Any, result: dict[str, Any]) -> None:
    from ...ai.evaluator import AIEvaluator

    evaluator = AIEvaluator(llm, settings)
    started = time.monotonic()
    verdict = await evaluator.evaluate(sample_listing(), [sample_image()], purpose="resale")
    seconds = time.monotonic() - started
    result["seconds"] = rnd(seconds, 1)
    if verdict.confidence <= 0:
        result["error_ru"] = result["message_ru"] = verdict.reasoning or "Модель не ответила"
        return
    product = verdict.product or ""
    result["verdict"] = {
        "product": product, "condition": verdict.condition,
        "condition_label": CONDITION_LABELS.get(verdict.condition, verdict.condition),
        "photo_matches_description": verdict.photo_matches_description, "verdict": verdict.verdict,
        "confidence": rnd(verdict.confidence, 2), "red_flags": list(verdict.red_flags),
        "reasoning": verdict.reasoning, "item_type": verdict.item_type,
    }
    recognised = "iphone" in product.lower() or "iphone" in (verdict.search_query or "").lower()
    result["vision_ok"] = recognised
    result["ok"] = True
    match = {True: "фото совпадают с описанием ✓", False: "фото не совпадают ✗", None: "по фото неясно"}[
        verdict.photo_matches_description]
    result["message_ru"] = (f"Модель увидела: {product or 'неясно'} · состояние "
                            f"{CONDITION_LABELS.get(verdict.condition, verdict.condition).lower()} · {match}"
                            f" · ответ за {seconds:.0f} с")
    if not recognised:
        result["warning_ru"] = ("Модель ответила странно. В LM Studio поставь Context Length 8192 и проверь, что это "
                                "vision-модель (Qwen2.5-VL)")
    elif seconds > SLOW_AI_SECONDS:
        result["warning_ru"] = "Медленно. На слабом ПК выбери модель поменьше (Qwen2.5-VL-3B)"


# ================================================================ Telegram
_TG_TOKEN_RE = re.compile(r"^\d{5,12}:[\w-]{20,}$")


async def tg_call(ctx: ApiContext, token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """Bot API call; errors become Russian ApiErrors (the token never appears in messages)."""
    try:
        async with httpx.AsyncClient(transport=ctx.http_transport, timeout=TELEGRAM_TIMEOUT) as client:
            resp = await client.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params or {})
    except httpx.HTTPError as exc:
        raise ApiError(502, "network", f"Нет связи с Telegram ({exc.__class__.__name__}) — проверь интернет") from None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code == 200 and data.get("ok"):
        return data.get("result")
    code = int(data.get("error_code") or resp.status_code)
    desc = str(data.get("description") or resp.reason_phrase or "").replace(token, "***")
    if code in (401, 404):
        raise ApiError(400, "telegram_token_invalid", "Telegram не узнал этот ключ — скопируй его у BotFather ещё раз")
    if code == 409:
        raise ApiError(409, "telegram_webhook", "У бота включён webhook, поэтому я не вижу сообщения. "
                                                "Нажми «Сбросить webhook бота» и попробуй снова")
    if code == 403:
        raise ApiError(400, "telegram_blocked", "Бот не может писать в этот чат — открой бота и нажми Start")
    if code == 400 and "chat not found" in desc.lower():
        raise ApiError(400, "telegram_chat_not_found", "Чат не найден — открой бота и нажми Start")
    raise ApiError(502, "upstream", f"Telegram: ошибка {code}: {desc}")


def _token(ctx: ApiContext, supplied: str | None) -> str:
    token = (supplied or ctx.config.notifications.telegram.bot_token or "").strip()
    if not token:
        raise validation_error({"token": "вставь ключ бота от @BotFather"})
    if not _TG_TOKEN_RE.match(token):
        raise validation_error({"token": "похоже, скопировалось не всё — ключ выглядит так: 123456789:AA…"})
    return token


def _bot(me: dict[str, Any]) -> dict[str, Any]:
    username = me.get("username") or ""
    return {"id": me.get("id"), "username": username, "name": me.get("first_name") or username,
            "link": f"https://t.me/{username}" if username else None}


@router.post("/telegram/validate", responses=ERRORS, summary="Проверить ключ бота (getMe); save=true — записать в .env")
async def telegram_validate(body: TelegramTokenIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    body = body or TelegramTokenIn()
    token = _token(ctx, body.token)
    me = await tg_call(ctx, token, "getMe")
    bot = _bot(me or {})
    saved = False
    if body.save and body.token:
        ctx.write_secrets({"telegram_bot_token": token})
        saved = True
    return {"ok": True, "bot": bot, "saved": saved, "message_ru": f"✓ Бот @{bot['username']} найден"}


@router.post("/telegram/verify-token", responses=ERRORS, include_in_schema=False)
async def telegram_verify_token(body: TelegramTokenIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await telegram_validate(body, ctx)


@router.post("/telegram/link", responses=ERRORS,
             summary="Код привязки + ссылка t.me/<бот>?start=<код>; ключ из тела сохраняется")
async def telegram_link(body: TelegramTokenIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    body = body or TelegramTokenIn()
    token = _token(ctx, body.token)
    bot = _bot(await tg_call(ctx, token, "getMe") or {})
    if body.token and body.token.strip() != ctx.config.notifications.telegram.bot_token:
        ctx.write_secrets({"telegram_bot_token": token})
    code = secrets.token_hex(8)  # 16 chars, allowed in /start payloads
    ctx.set_json(LINK_KEY, {"code": code, "created_at": utcnow().isoformat(), "bot": bot})
    username = bot["username"]
    return {
        "code": code,
        "bot": bot,
        "deep_link": f"https://t.me/{username}?start={code}",
        "app_link": f"tg://resolve?domain={username}&start={code}",
        "expires_in": int(LINK_TTL.total_seconds()),
        "message_ru": "Открой бота и нажми Start — я сам увижу сообщение",
    }


def _chat_view(chat: dict[str, Any]) -> dict[str, Any]:
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("title") or ""
    kind = chat.get("type") or ""
    return {"type": kind, "type_ru": {"private": "личный чат", "group": "группа", "supergroup": "группа",
                                      "channel": "канал"}.get(kind, kind),
            "name": name, "username": chat.get("username") or ""}


@router.get("/telegram/detect", responses=ERRORS,
            summary="Ждать /start <код> от пользователя (getUpdates); найдено — chat_id сохраняется, Telegram включается")
async def telegram_detect(code: str = Query(..., min_length=6, max_length=64),
                          ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    link = ctx.get_json(LINK_KEY, {}) or {}
    if link.get("code") != code:
        raise ApiError(404, "telegram_code_unknown", "Код устарел — нажми «Открыть бота» ещё раз")
    try:
        from datetime import datetime

        created = datetime.fromisoformat(link["created_at"])
        if utcnow() - created > LINK_TTL:
            raise ApiError(410, "telegram_code_expired", "Код устарел — нажми «Открыть бота» ещё раз")
    except (KeyError, ValueError):
        pass
    token = _token(ctx, None)
    updates = await tg_call(ctx, token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}) or []
    for update in reversed(updates):
        message = update.get("message") or {}
        text = str(message.get("text") or "")
        if code not in text:
            continue
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            continue
        ctx.write_secrets({"telegram_chat_id": chat_id},
                          extra_updates={"notifications.telegram.enabled": True})
        ctx.mark_done("telegram")
        ctx.set_json(LINK_KEY, None)
        view = _chat_view(chat)
        return {"found": True, "saved": True, "chat": {**view, "chat_id_masked": mask(chat_id, "id")},
                "message_ru": f"✓ Нашёл тебя: {view['name'] or view['username'] or 'чат'} ({view['type_ru']})"}
    return {"found": False, "saved": False, "chat": None, "message_ru": "Жду твоё сообщение…"}


@router.post("/telegram/find-chat", responses=ERRORS,
             summary="Чаты, которые писали боту (новые сверху) — для ручного выбора chat_id")
async def telegram_find_chat(body: TelegramTokenIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    body = body or TelegramTokenIn()
    token = _token(ctx, body.token)
    updates = await tg_call(ctx, token, "getUpdates", {"timeout": 0}) or []
    chats: dict[str, dict[str, Any]] = {}
    for update in reversed(updates):
        message = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if chat_id and chat_id not in chats:
            chats[chat_id] = {"chat_id": chat_id, **_chat_view(chat), "last_text": str(message.get("text") or "")[:100],
                              "date": message.get("date")}
    items = list(chats.values())
    message = ("Напиши своему боту /start и нажми «Найти» ещё раз" if not items
               else f"Найдено чатов: {len(items)}")
    return {"chats": items, "message_ru": message}


@router.post("/telegram/reset-webhook", responses=ERRORS, summary="Удалить webhook бота (мешает getUpdates)")
async def telegram_reset_webhook(body: TelegramTokenIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    token = _token(ctx, (body or TelegramTokenIn()).token)
    await tg_call(ctx, token, "deleteWebhook", {"drop_pending_updates": False})
    return {"ok": True, "message_ru": "Webhook сброшен — теперь снова открой бота и нажми Start"}


def _sample_deal(ctx: ApiContext) -> list[DealView]:
    from ...notify.render import sample_deals

    return sample_deals()[:1]


async def _deliver(notifier: Any, *, with_deal: bool, ctx: ApiContext) -> None:
    from ...notify.base import NotifyError

    try:
        if with_deal:
            await asyncio.wait_for(notifier.send(_sample_deal(ctx), title="EbeyParser: тестовое уведомление"), 60)
        else:
            await asyncio.wait_for(notifier.send_text("✅ EbeyParser: тестовое сообщение. Всё настроено!"), 60)
    except asyncio.TimeoutError:
        raise ApiError(504, "timeout", "Нет ответа за 60 с — проверь интернет") from None
    except NotifyError as exc:
        raise ApiError(502, "delivery_failed", str(exc)) from None


@router.post("/telegram/test", responses=ERRORS, summary="Тестовое сообщение в Telegram (пример сделки с фото)")
async def telegram_test(body: TelegramTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...notify.telegram import TelegramNotifier

    body = body or TelegramTestIn()
    token = _token(ctx, body.token)
    chat_id = (body.chat_id or ctx.config.notifications.telegram.chat_id or "").strip()
    if not chat_id:
        raise validation_error({"chat_id": "сначала привяжи чат: открой бота и нажми Start"})
    notifier = TelegramNotifier(TelegramConfig(enabled=True, bot_token=token, chat_id=chat_id),
                                web_base_url=ctx.web_base_url(), transport=ctx.http_transport)
    await _deliver(notifier, with_deal=body.with_deal, ctx=ctx)
    return {"ok": True, "message_ru": "Отправлено — проверь Telegram"}


# ================================================================== e-mail
def _email_config(ctx: ApiContext, body: EmailTestIn) -> EmailConfig:
    base = ctx.config.notifications.email
    data = base.model_dump()
    for key, value in body.model_dump(exclude={"save"}).items():
        if value is not None:
            data[key] = value
    data["enabled"] = True
    if not data.get("from_addr"):
        data["from_addr"] = data.get("username") or ""
    if data.get("smtp_port") == 465 and body.use_ssl is None:
        data["use_ssl"] = True
    return EmailConfig.model_validate(data)


@router.post("/email/test", responses=ERRORS,
             summary="Тестовое письмо (поля — поверх сохранённых); save=true — сохранить и включить почту")
async def email_test(body: EmailTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...notify.base import missing_settings
    from ...notify.emailer import EmailNotifier

    body = body or EmailTestIn()
    cfg = _email_config(ctx, body)
    missing = missing_settings(NotificationsConfig(email=cfg)).get("email") or []
    if missing:
        labels = {"smtp_host": "SMTP-сервер", "smtp_port": "порт", "username": "адрес почты",
                  "password": "пароль приложения", "from_addr": "отправитель", "to_addrs": "куда слать"}
        raise validation_error({m: f"не заполнено: {labels.get(m, m)}" for m in missing}, "Заполни почту")
    kwargs = {"smtp_factory": ctx.smtp_factory} if ctx.smtp_factory is not None else {}
    notifier = EmailNotifier(cfg, web_base_url=ctx.web_base_url(), **kwargs)
    await _deliver(notifier, with_deal=False, ctx=ctx)
    saved = False
    if body.save:
        secrets_in: dict[str, str | None] = {}
        if body.username is not None:
            secrets_in["smtp_user"] = body.username
        if body.password is not None:
            secrets_in["smtp_password"] = body.password
        if body.to_addrs is not None:
            secrets_in["notify_email"] = ", ".join(cfg.to_addrs)
        extra = {"notifications.email.enabled": True, "notifications.email.smtp_host": cfg.smtp_host,
                 "notifications.email.smtp_port": cfg.smtp_port, "notifications.email.use_ssl": cfg.use_ssl}
        if body.from_addr and body.from_addr != (body.username or cfg.username):
            extra["notifications.email.from_addr"] = body.from_addr
        ctx.write_secrets(secrets_in, extra_updates=extra)
        saved = True
    return {"ok": True, "saved": saved, "message_ru": f"Письмо отправлено на {mask(', '.join(cfg.to_addrs), 'email')}"}


@router.post("/email/validate", responses=ERRORS, include_in_schema=False)
async def email_validate(body: EmailTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await email_test(body, ctx)


# ==================================================================== eBay
@router.post("/ebay/test", responses=ERRORS,
             summary="Проверить ключи eBay: токен + лимиты запросов; save=true — записать ключи в .env")
async def ebay_test(body: EbayTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...scraper.ebay_api import EbayAPIError, EbayBrowseClient

    body = body or EbayTestIn()
    data = ctx.config.ebay.model_dump()
    for key, value in body.model_dump(exclude={"save"}).items():
        if value is not None:
            data[key] = value.strip() if isinstance(value, str) else value
    cfg = EbayConfig.model_validate(data)
    if not cfg.configured:
        raise validation_error({"client_id": "вставь App ID (Client ID)", "client_secret": "и Cert ID (Client Secret)"},
                               "Нужны ключи eBay")
    client = EbayBrowseClient(cfg, transport=ctx.http_transport, timeout=20.0)
    limits: list[dict[str, Any]] = []
    warning = ""
    try:
        try:
            await client._get_token(force=bool(cfg.client_id))
        except EbayAPIError as exc:
            text = str(exc)
            if "invalid_client" in text or "401" in text:
                message = ("eBay не принял ключи — проверь, что это Production, а не Sandbox, и что ключи активированы"
                           " (вопрос про Marketplace Account Deletion)")
            else:
                message = f"eBay: {text[:200]}"
            raise ApiError(400, "ebay_keys_invalid", message) from None
        except httpx.HTTPError as exc:
            raise ApiError(502, "network", f"Нет связи с eBay ({exc.__class__.__name__})") from None
        try:
            rows = await client.rate_limits("buy")
            limits = [{**r, "reset": r["reset"].isoformat() if r.get("reset") else None} for r in rows]
        except (EbayAPIError, httpx.HTTPError) as exc:
            warning = f"Ключи работают, но лимиты прочитать не удалось ({str(exc)[:120]})"
    finally:
        await client.aclose()
    browse = next((r for r in limits if "browse" in r["api"].lower() and r.get("limit")), None) or \
        next((r for r in limits if r.get("limit")), None)
    message = "✓ Ключи работают"
    if browse:
        message += f" · лимит {browse['limit']:,} запросов в день".replace(",", " ")
    saved = False
    if body.save:
        ctx.write_secrets({"ebay_client_id": body.client_id, "ebay_client_secret": body.client_secret,
                           "ebay_oauth_token": body.oauth_token})
        ctx.mark_done("ebay")
        saved = True
    return {"ok": True, "token_ok": True, "limits": limits, "browse_limit": browse, "warning_ru": warning,
            "saved": saved, "message_ru": message}


@router.post("/ebay/validate", responses=ERRORS, include_in_schema=False)
async def ebay_validate(body: EbayTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await ebay_test(body, ctx)


# ========================================================== notifications
@router.post("/notify/test", responses=ERRORS,
             summary="Тестовое уведомление: ?channel=telegram|email (пример сделки) или во все включённые каналы")
async def notify_test(channel: str | None = Query(None, pattern="^(telegram|email)$"),
                      body: NotifyTestIn | None = None, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...notify.base import missing_settings
    from ...notify.emailer import EmailNotifier
    from ...notify.telegram import TelegramNotifier

    channel = channel or (body.channel if body else None)
    cfg = ctx.config.notifications
    notifiers: list[Any] = []
    if channel is None and ctx.app.state.notifiers_factory is not None:
        try:
            notifiers = list(ctx.app.state.notifiers_factory() or [])
        except Exception as exc:  # noqa: BLE001
            raise ApiError(500, "internal", f"Не удалось создать каналы уведомлений: {exc}") from exc
    else:
        wanted = [channel] if channel else ["telegram", "email"]
        probe = NotificationsConfig(email=cfg.email.model_copy(update={"enabled": True}),
                                    telegram=cfg.telegram.model_copy(update={"enabled": True}))
        missing = missing_settings(probe)
        for name in wanted:
            enabled = getattr(cfg, name).enabled
            if channel is None and not enabled:
                continue
            if missing.get(name):
                if channel:
                    raise ApiError(400, "not_configured", f"{'Telegram' if name == 'telegram' else 'Почта'} не настроен(а):"
                                                          f" не хватает {', '.join(missing[name])}")
                continue
            if name == "telegram":
                notifiers.append(TelegramNotifier(cfg.telegram, web_base_url=ctx.web_base_url(),
                                                  transport=ctx.http_transport))
            else:
                kwargs = {"smtp_factory": ctx.smtp_factory} if ctx.smtp_factory is not None else {}
                notifiers.append(EmailNotifier(cfg.email, web_base_url=ctx.web_base_url(), **kwargs))
    if not notifiers:
        raise ApiError(400, "no_channels", "Не настроен ни один канал уведомлений — подключи Telegram или почту")
    results: dict[str, dict[str, Any]] = {}
    for notifier in notifiers:
        name = str(getattr(notifier, "name", "") or type(notifier).__name__)
        try:
            await _deliver(notifier, with_deal=True, ctx=ctx)
            results[name] = {"ok": True, "message_ru": "Отправлено"}
        except ApiError as exc:
            results[name] = {"ok": False, "message_ru": exc.message_ru}
        except Exception as exc:  # noqa: BLE001
            results[name] = {"ok": False, "message_ru": str(exc) or type(exc).__name__}
    ok = all(r["ok"] for r in results.values())
    if channel and not ok:
        raise ApiError(502, "delivery_failed", results[next(iter(results))]["message_ru"])
    return {"ok": ok, "results": results}


@router.get("/notify/preview", summary="Сколько уведомлений пришло бы за N дней при таком пороге (?min_score=70&verdicts=buy,maybe)")
async def notify_preview(
    min_score: float | None = Query(None, ge=0, le=100),
    verdicts: str | None = Query(None, pattern=r"^[a-z,]*$"),
    days: int = Query(7, ge=1, le=90),
    ctx: ApiContext = Depends(get_ctx),
) -> dict[str, Any]:
    from datetime import datetime

    from .presenters import LOCAL_TZ, aware

    cfg = ctx.config.notifications
    score = cfg.min_score if min_score is None else min_score
    wanted = [v for v in (verdicts.split(",") if verdicts else cfg.verdicts) if v in ("buy", "maybe", "skip")]
    if verdicts is not None and not wanted:
        raise validation_error({"verdicts": "buy, maybe или skip через запятую"})
    since = utcnow() - timedelta(days=days)
    stamps = ctx.db.notify_candidates(since, min_score=score, verdicts=wanted)
    current = ctx.db.notify_candidates(since, min_score=cfg.min_score, verdicts=list(cfg.verdicts))
    per_day: dict[str, int] = {}
    for raw in stamps:
        day = aware(datetime.fromisoformat(raw)).astimezone(LOCAL_TZ).date().isoformat()
        per_day[day] = per_day.get(day, 0) + 1
    total = len(stamps)
    return {
        "min_score": score, "verdicts": wanted, "days": days, "would_send": total,
        "per_day_avg": round(total / days, 1), "per_day": [{"date": d, "count": c} for d, c in sorted(per_day.items())],
        "current": {"min_score": cfg.min_score, "verdicts": list(cfg.verdicts), "would_send": len(current)},
        "message_ru": f"За последние {days} дн. пришло бы {total} уведомлений",
    }


__all__ = ["router", "sample_image", "sample_listing", "tg_call"]
