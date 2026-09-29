"""Health, logs, demo data, backup and data reset."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from ... import __version__
from ...models import utcnow
from .context import DEMO_RUNS_KEY, ApiContext, get_ctx
from .errors import ApiError
from ...errors_ru import AI_DOWN_RU, ai_problem, humanize_text
from ...timefmt import local_tz, when_label
from .presenters import iso
from .routes_app import demo_count, demo_ids
from .schemas import ERRORS, ConfirmIn

router = APIRouter()

AI_HEALTH_TIMEOUT = 8.0
LOG_TAIL_BYTES = 4 * 1024 * 1024
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
_LOG_RE = re.compile(r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(?P<ms>\d{3}))?\s+"
                     r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<logger>[\w.\-]+):\s?(?P<message>.*)$")


# ------------------------------------------------------------------- health
AI_STATE_OK = "Работает"
AI_STATE_DOWN = "Не отвечает"
AI_STATE_OFF = "Нейросеть выключена — фото никто не проверяет"


async def ai_health(ctx: ApiContext) -> dict[str, Any]:
    """AI status for /health (and the Состояние tile): the same texts as the AI test."""
    ai = ctx.config.ai
    base = {"enabled": ai.enabled, "provider": ai.provider, "base_url": ai.base_url, "model": ai.model}
    if not ai.enabled:
        return {**base, "ok": None, "state_ru": AI_STATE_OFF, "error_ru": "", "latency_ms": None}
    fn = getattr(ctx.monitor, "ai_health", None)
    if fn is None:
        return {**base, "ok": None, "state_ru": "Проверить нейросеть сейчас нельзя — фоновые проверки выключены",
                "error_ru": "", "latency_ms": None}
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(fn(), timeout=AI_HEALTH_TIMEOUT)
    except asyncio.TimeoutError:
        return {**base, "ok": False, "server_ok": False, "state_ru": AI_STATE_DOWN, "latency_ms": None,
                "error_ru": ai_problem(ai.provider, ai.base_url, ai.model, "не ответил за 8 с", server_ok=False),
                "details": f"Сервер нейросети не ответил за {AI_HEALTH_TIMEOUT:g} с"}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "server_ok": False, "state_ru": AI_STATE_DOWN, "latency_ms": None,
                "error_ru": ai_problem(ai.provider, ai.base_url, ai.model, str(exc), server_ok=False),
                "details": f"{type(exc).__name__}: {exc}"}
    result = result if isinstance(result, dict) else {}
    ok = bool(result.get("ok"))
    server_ok = result.get("server_ok", ok)
    error = str(result.get("error") or "")
    out = {
        **base, "ok": ok, "server_ok": server_ok, "model_available": result.get("model_available"),
        "resolved_model": result.get("resolved_model"),
        "error_ru": "" if ok else ai_problem(ai.provider, ai.base_url, ai.model, error, server_ok=server_ok),
        "latency_ms": int((time.monotonic() - started) * 1000) if ok else None,  # no "1 мс" next to "не отвечает"
        "state_ru": AI_STATE_OK if ok else (AI_STATE_DOWN if server_ok is False else "Модель не найдена"),
    }
    if error and not ok:
        out["details"] = error
    return out


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def storage_view(ctx: ApiContext) -> dict[str, Any]:
    db_path = Path(getattr(ctx.db, "path", "") or "")
    data_dir = ctx.config.data_path
    free = None
    try:
        free = shutil.disk_usage(data_dir if data_dir.exists() else Path.cwd()).free
    except OSError:
        pass
    in_memory = str(db_path) in ("", ":memory:")
    return {
        "db_path": None if in_memory else str(db_path),
        "db_bytes": 0 if in_memory else _size(db_path),
        "wal_bytes": 0 if in_memory else _size(Path(str(db_path) + "-wal")),
        "data_dir": str(data_dir),
        "free_bytes": free,
        "log_path": str(log_path(ctx)),
        "log_bytes": _size(log_path(ctx)),
        "counts": ctx.db.table_counts(),
        "config_path": str(ctx.config_path) if ctx.config_path else None,
        "env_path": str(ctx.env_path) if ctx.env_path else None,
        "version": __version__,
    }


def channels_view(ctx: ApiContext) -> dict[str, Any]:
    from ...notify.base import missing_settings

    cfg = ctx.config.notifications
    probe = cfg.model_copy(update={"email": cfg.email.model_copy(update={"enabled": True}),
                                   "telegram": cfg.telegram.model_copy(update={"enabled": True})})
    missing = missing_settings(probe)
    last = ctx.db.last_deliveries()
    problems = {p["channel"]: p for p in ctx.db.delivery_problems(utcnow() - timedelta(days=1))}
    channels = []
    for name, label in (("telegram", "Telegram"), ("email", "Почта")):
        problem = problems.get(name)
        raw_error = problem["last_error"] if problem else ""
        channels.append({
            "name": name, "label": label, "enabled": getattr(cfg, name).enabled,
            "configured": not missing.get(name), "missing": missing.get(name, []),
            "last_delivery_at": iso(last.get(name)), "last_delivery_at_label": when_label(last.get(name)),
            "failed_24h": problem["failed"] if problem else 0,
            "last_error": humanize_text(raw_error, name).message_ru if raw_error else "",
            "last_error_details": raw_error,
        })
    try:
        queued = len(ctx.db.queued_alerts())
    except Exception:  # noqa: BLE001
        queued = 0
    ready = any(c["enabled"] and c["configured"] for c in channels)
    return {"channels": channels, "queued": queued, "health_alerts": cfg.health_alerts,
            "heartbeat_hour": cfg.heartbeat_hour,
            # the daily "жив" report only goes out through a working channel: don't promise it otherwise
            "heartbeat_active": cfg.heartbeat_hour is not None and ready, "any_channel": ready}


NOTIFY_ACTION = {"label_ru": "Настроить уведомления", "href": "/settings/notifications"}
LEVEL_ORDER = {"error": 0, "warn": 1}


@router.get("/health", summary="Состояние: итоговый баннер (+ action), монитор, нейросеть, сайты, уведомления, диск")
async def health(ai: bool = Query(True, description="проверять нейросеть (до 8 с)"),
                 ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    """level: ok | warn | error. Never "ok" while automatic checks are stopped, paused or a
    site asked for a pause, or while a notification channel is on but not set up.
    banner_ru: the most important problem in plain Russian; action: {label_ru, href} (an
    SPA path) or null. problems: [{level, text_ru, action?, details?}]."""
    from .routes_monitor import monitor_view, real_runs, run_view, site_label

    mon = await monitor_view(ctx, with_hosts=True)
    sites = mon["http"]
    ai_info = await ai_health(ctx) if ai else None
    notif = channels_view(ctx)
    runs = real_runs(ctx, 48)
    last_error = next(({"run_id": r.id, "at": iso(r.started_at), "at_label": when_label(r.started_at),
                        **{k: run_view(r)[k] for k in ("errors", "error_details")}} for r in runs if r.errors), None)
    problems: list[dict[str, Any]] = []

    def add(level: str, text: str, action: dict[str, str] | None = None, details: str = "") -> None:
        item: dict[str, Any] = {"level": level, "text_ru": text, "action": action}
        if details:
            item["details"] = details
        problems.append(item)

    if ai_info and ai_info.get("ok") is False:
        add("error", f"{AI_DOWN_RU}. {ai_info.get('error_ru') or ''}".strip(),
            {"label_ru": "Настройки нейросети", "href": "/settings/ai"}, str(ai_info.get("details") or ""))
    scout = mon.get("scout") or {}
    if scout.get("enabled") and scout.get("state") == "down":
        add("warn", scout.get("text_ru") or "Разведчик не отвечает", {"label_ru": "Настройки нейросети",
                                                                       "href": "/settings/ai"})
    if (scout.get("vision_queue") or {}).get("waiting"):  # the photo model is offline: deals wait for it
        add("warn", f"{scout['vision_queue']['text_ru']} — нейросеть для фото сейчас не отвечает",
            {"label_ru": "Настройки нейросети", "href": "/settings/ai"})
    if not mon["available"]:
        restart = callable(getattr(ctx.app.state, "restart_callback", None))
        add("warn", "Фоновые проверки выключены — перезапусти программу, и они включатся сами",
            {"label_ru": "Перезапустить", "href": "/settings/data"} if restart else None)
    elif mon["state"] == "stopped":
        add("warn", "Автопроверка выключена — новые объявления смотрю только по кнопке «Проверить сейчас»",
            {"label_ru": "Проверить сейчас", "href": "/health"})
    if mon["paused"]:
        add("warn", "Проверки на паузе — новые объявления сейчас не смотрю", {"label_ru": "Продолжить", "href": "/health"})
    if not any(s.enabled for s in ctx.config.searches):
        add("warn", "Нет ни одного включённого поиска — мне нечего проверять. Добавь, что искать",
            {"label_ru": "Поиски", "href": "/searches"})
    cooldown = mon.get("cooldown")
    if cooldown:
        add("warn", f"{cooldown['text_ru']}. Ничего делать не нужно — это защита от блокировки",
            {"label_ru": "Снизить нагрузку", "href": "/settings/region"})
    for site in sites:
        if site["exhausted"] and not site["blocked"]:
            add("warn", f"{site_label(site['host'])}: лимит запросов на этот час исчерпан — продолжу сам позже",
                {"label_ru": "Снизить нагрузку", "href": "/settings/region"})
    for channel in notif["channels"]:
        if channel["enabled"] and not channel["configured"]:
            add("warn", f"{channel['label']} включен(а), но не настроен(а) — уведомления туда не уходят",
                NOTIFY_ACTION)
        elif channel["enabled"] and channel["failed_24h"]:
            add("warn", f"{channel['label']}: не доставлено {channel['failed_24h']} — {channel['last_error']}",
                NOTIFY_ACTION, channel["last_error_details"])
    if runs and runs[0].errors:
        add("warn", f"В последней проверке ошибок: {len(runs[0].errors)} — подробности ниже, в «Проверки»",
            {"label_ru": "Подробнее", "href": "/health"})
    problems.sort(key=lambda p: LEVEL_ORDER.get(p["level"], 2))
    level = "error" if any(p["level"] == "error" for p in problems) else ("warn" if problems else "ok")
    action = problems[0]["action"] if problems else None
    if problems:
        banner = problems[0]["text_ru"]
    elif mon["last_summary"]:
        last = mon["last_summary"]
        banner = (f"Всё работает. Последняя проверка: {last.get('new_listings', 0)} новых, "
                  f"{last.get('deals_found', 0)} выгодных.")
    else:
        banner = "Проверок ещё не было — первая начнётся скоро"
    return {
        "ok": level == "ok",
        "level": level,
        "banner_ru": banner,
        "action": action,
        "problems": problems,
        "version": __version__,
        "uptime_seconds": int((utcnow() - ctx.started_at).total_seconds()),
        "monitor": {k: v for k, v in mon.items() if k != "http"},
        "ai": ai_info,
        "scout": mon.get("scout"),
        "sites": sites,
        "cooldown": cooldown,
        "ebay": {"configured": ctx.config.ebay.configured, "marketplace_id": ctx.config.ebay.marketplace_id},
        "notifications": notif,
        "backlog": mon["backlog"],
        "storage": storage_view(ctx),
        "last_error": last_error,
        "runs": [run_view(r) for r in runs],
    }


# --------------------------------------------------------------------- logs
def log_path(ctx: ApiContext) -> Path:
    from ...runtime import LOG_FILE

    return ctx.config.data_path / "logs" / LOG_FILE


def parse_log_lines(lines: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        m = _LOG_RE.match(line)
        if m:
            entries.append({"time": m["time"], "time_text": m["time"][11:], "level": m["level"].lower(),
                            "logger": m["logger"], "message": m["message"]})
        elif entries and line.strip():
            entries[-1]["message"] += "\n" + line
        elif line.strip():
            entries.append({"time": None, "time_text": "", "level": "info", "logger": "", "message": line})
    return entries


def _keep(entry: dict[str, Any], level: str, q: str) -> bool:
    if level and LEVELS.get(entry["level"], 20) < LEVELS.get(level, 0):
        return False
    return not q or q in entry["message"].lower() or q in entry["logger"].lower()


def _tail(path: Path) -> list[str]:
    size = _size(path)
    with path.open("rb") as fh:
        if size > LOG_TAIL_BYTES:
            fh.seek(size - LOG_TAIL_BYTES)
            fh.readline()  # drop the partial first line
        return fh.read().decode("utf-8", "replace").splitlines()


def _level(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in ("", "all", "всё", "все"):
        return ""
    if value in ("warn", "warnings"):
        return "warning"
    if value in ("errors",):
        return "error"
    if value not in LEVELS:
        raise ApiError(422, "validation", "level: error, warning, info, debug или all", fields={"level": "неверный уровень"})
    return value


@router.get("/logs", summary="Хвост лога data/logs/ebeyparser.log: ?lines=200&level=warning&q=текст (новые в конце)")
async def logs(
    lines: int = Query(200, ge=1, le=5000),
    limit: int | None = Query(None, ge=1, le=5000),
    level: str | None = Query(None),
    q: str | None = Query(None, max_length=200),
    ctx: ApiContext = Depends(get_ctx),
) -> dict[str, Any]:
    path = log_path(ctx)
    wanted = limit or lines
    minimum = _level(level)
    needle = (q or "").strip().lower()
    if not path.is_file():
        return {"path": str(path), "exists": False, "size": 0, "items": [], "truncated": False}
    entries = [e for e in parse_log_lines(await asyncio.to_thread(_tail, path)) if _keep(e, minimum, needle)]
    return {"path": str(path), "exists": True, "size": _size(path), "items": entries[-wanted:],
            "truncated": len(entries) > wanted}


@router.get("/logs/download", responses=ERRORS, summary="Скачать лог целиком")
async def logs_download(ctx: ApiContext = Depends(get_ctx)) -> FileResponse:
    path = log_path(ctx)
    if not path.is_file():
        raise ApiError(404, "not_found", "Лога ещё нет — он появляется после запуска проверок")
    stamp = utcnow().astimezone(local_tz()).strftime("%Y-%m-%d_%H-%M")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"ebeyparser-{stamp}.log")


@router.get("/logs/stream", summary="Новые строки лога как SSE (event: log); ?level=&q=")
async def logs_stream(
    request: Request,
    level: str | None = Query(None),
    q: str | None = Query(None, max_length=200),
    max_events: int | None = Query(None, ge=1, le=100000),
    timeout: float | None = Query(None, gt=0, le=86400),
    poll: float = Query(1.0, ge=0.05, le=10),
    ctx: ApiContext = Depends(get_ctx),
) -> StreamingResponse:
    import json

    path = log_path(ctx)
    minimum = _level(level)
    needle = (q or "").strip().lower()

    async def stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout else None
        position = _size(path)
        sent = 0
        idle = 0.0
        buffer = ""
        yield "retry: 3000\n\n"
        yield f"event: ready\ndata: {json.dumps({'path': str(path)})}\n\n"
        while not ctx.hub.closed:
            if deadline is not None and loop.time() >= deadline:
                return
            size = _size(path)
            if size < position:  # rotated
                position = 0
            if size > position:
                with path.open("rb") as fh:
                    fh.seek(position)
                    chunk = fh.read(size - position).decode("utf-8", "replace")
                position = size
                buffer += chunk
                complete, _, buffer = buffer.rpartition("\n")
                for entry in parse_log_lines(complete.splitlines()):
                    if _keep(entry, minimum, needle):
                        yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
                        sent += 1
                        if max_events and sent >= max_events:
                            return
                idle = 0.0
            else:
                idle += poll
                if idle >= 15:
                    idle = 0.0
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------- demo
@router.post("/demo", summary="Загрузить демо-данные (14 примеров сделок), чтобы посмотреть интерфейс")
@router.post("/demo/load", include_in_schema=False)
async def demo_load(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...demo import seed_demo

    before = {r.id for r in ctx.db.list_runs(limit=10_000)}
    inserted = seed_demo(ctx.db)
    new_runs = [r.id for r in ctx.db.list_runs(limit=10_000) if r.id not in before]
    if new_runs:
        known = ctx.get_json(DEMO_RUNS_KEY, []) or []
        ctx.set_json(DEMO_RUNS_KEY, sorted(set(known) | set(new_runs)))
    ctx.hub.publish("data_changed", {"reason": "demo_loaded", "inserted": inserted})
    return {"inserted": inserted, "count": demo_count(ctx), "message_ru": "Демо-данные загружены"}


@router.delete("/demo", summary="Убрать демо-данные")
@router.post("/demo/clear", include_in_schema=False)
async def demo_clear(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    removed = ctx.db.delete_listings(demo_ids())
    runs = ctx.db.delete_runs(ctx.get_json(DEMO_RUNS_KEY, []) or [])
    ctx.set_json(DEMO_RUNS_KEY, [])
    ctx.hub.publish("data_changed", {"reason": "demo_cleared", "removed": removed})
    return {"removed": removed, "runs_removed": runs, "message_ru": "Демо-данные удалены"}


# ------------------------------------------------------------------- backup
@router.get("/data", summary="Данные: размер базы, счётчики, пути, версия")
async def data_info(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from ...monitor import LISTING_RETENTION_DAYS, RUN_RETENTION_DAYS

    return {**storage_view(ctx), "listing_retention_days": LISTING_RETENTION_DAYS,
            "run_retention_days": RUN_RETENTION_DAYS, "demo": {"count": demo_count(ctx)}}


@router.get("/backup", summary="Резервная копия (zip): база + config.yaml (+ .env с ключами, если include_secrets=1)")
async def backup(include_secrets: bool = Query(False), ctx: ApiContext = Depends(get_ctx)) -> FileResponse:
    workdir = Path(tempfile.mkdtemp(prefix="ebeyparser-backup-"))
    stamp = utcnow().astimezone(local_tz()).strftime("%Y-%m-%d_%H-%M")
    archive = workdir / f"ebeyparser-backup-{stamp}.zip"

    def build() -> None:
        db_copy = workdir / "ebeyparser.sqlite3"
        ctx.db.backup_to(db_copy)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_copy, "data/ebeyparser.sqlite3")
            if ctx.config_path and ctx.config_path.is_file():
                zf.write(ctx.config_path, "config.yaml")
            if include_secrets and ctx.env_path and ctx.env_path.is_file():
                zf.write(ctx.env_path, ".env")
            categories = ctx.config.data_path / "categories.json"
            if categories.is_file():
                zf.write(categories, "data/categories.json")
            zf.writestr("README.txt", (
                f"EbeyParser {__version__}, копия от {stamp}.\n"
                "Восстановить: остановить программу, положить config.yaml рядом с программой, а файлы из data/ — "
                "в папку данных (general.data_dir), и запустить снова.\n"
                + ("Внимание: внутри .env с ключами и паролями — храни копию в надёжном месте.\n" if include_secrets
                   else "Ключи и пароли (.env) в копию не входят.\n")))
        db_copy.unlink(missing_ok=True)

    try:
        await asyncio.to_thread(build)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise ApiError(500, "backup_failed", "Не получилось создать резервную копию — проверь, что на диске есть "
                       "место, и попробуй ещё раз", details=f"{type(exc).__name__}: {exc}") from exc
    return FileResponse(archive, media_type="application/zip", filename=archive.name,
                        background=BackgroundTask(shutil.rmtree, workdir, True))


@router.post("/data/reset-history", responses=ERRORS, summary="Сбросить историю цен (подтверждение: «сбросить»)")
async def reset_history(body: ConfirmIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    if body.confirm.strip().lower() != "сбросить":
        raise ApiError(422, "confirm_required", "Для подтверждения напиши «сбросить»", fields={"confirm": "напиши «сбросить»"})
    removed = ctx.db.reset_price_history()
    ctx.hub.publish("data_changed", {"reason": "history_reset", "removed": removed})
    return {"removed": removed, "message_ru": f"История цен удалена ({removed} цен)"}


@router.post("/data/reset-all", responses=ERRORS, summary="Удалить все данные (подтверждение: «удалить»); настройки остаются")
async def reset_all(body: ConfirmIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    if body.confirm.strip().lower() != "удалить":
        raise ApiError(422, "confirm_required", "Для подтверждения напиши «удалить»", fields={"confirm": "напиши «удалить»"})
    if getattr(ctx.monitor, "is_running", False):
        raise ApiError(409, "busy", "Идёт проверка — дождись её окончания")
    counts = ctx.db.reset_all()
    ctx.hub.publish("data_changed", {"reason": "reset_all"})
    return {"removed": counts, "message_ru": "Все данные удалены — следующая проверка начнёт с изучения рынка"}
