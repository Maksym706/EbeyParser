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
from .presenters import LOCAL_TZ, iso
from .routes_app import demo_count, demo_ids
from .schemas import ERRORS, ConfirmIn

router = APIRouter()

AI_HEALTH_TIMEOUT = 8.0
LOG_TAIL_BYTES = 4 * 1024 * 1024
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
_LOG_RE = re.compile(r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(?P<ms>\d{3}))?\s+"
                     r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<logger>[\w.\-]+):\s?(?P<message>.*)$")


# ------------------------------------------------------------------- health
async def ai_health(ctx: ApiContext) -> dict[str, Any]:
    ai = ctx.config.ai
    base = {"enabled": ai.enabled, "provider": ai.provider, "base_url": ai.base_url, "model": ai.model}
    if not ai.enabled:
        return {**base, "ok": None, "state_ru": "Нейросеть выключена — фото никто не проверяет"}
    fn = getattr(ctx.monitor, "ai_health", None)
    if fn is None:
        return {**base, "ok": None, "state_ru": "Монитор не подключён — проверить нейросеть нельзя"}
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(fn(), timeout=AI_HEALTH_TIMEOUT)
    except asyncio.TimeoutError:
        return {**base, "ok": False, "error_ru": f"Сервер нейросети не ответил за {AI_HEALTH_TIMEOUT:g} с",
                "state_ru": "Нейросеть не отвечает"}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error_ru": str(exc) or type(exc).__name__, "state_ru": "Нейросеть не отвечает"}
    result = result if isinstance(result, dict) else {}
    ok = bool(result.get("ok"))
    return {
        **base, "ok": ok, "server_ok": result.get("server_ok", ok), "model_available": result.get("model_available"),
        "resolved_model": result.get("resolved_model"), "error_ru": result.get("error") or "",
        "latency_ms": int((time.monotonic() - started) * 1000),
        "state_ru": "Работает" if ok else (result.get("error") or "Нейросеть не отвечает"),
    }


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
        channels.append({
            "name": name, "label": label, "enabled": getattr(cfg, name).enabled,
            "configured": not missing.get(name), "missing": missing.get(name, []),
            "last_delivery_at": iso(last.get(name)), "failed_24h": problem["failed"] if problem else 0,
            "last_error": problem["last_error"] if problem else "",
        })
    try:
        queued = len(ctx.db.queued_alerts())
    except Exception:  # noqa: BLE001
        queued = 0
    return {"channels": channels, "queued": queued, "health_alerts": cfg.health_alerts,
            "heartbeat_hour": cfg.heartbeat_hour}


@router.get("/health", summary="Состояние: итоговый баннер, монитор, нейросеть, сайты, уведомления, диск, последняя ошибка")
async def health(ai: bool = Query(True, description="проверять нейросеть (до 8 с)"),
                 ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    from .routes_monitor import http_hosts, monitor_view, run_view

    mon = await monitor_view(ctx, with_hosts=False)
    sites = await http_hosts(ctx)
    ai_info = await ai_health(ctx) if ai else None
    notif = channels_view(ctx)
    runs = ctx.db.list_runs(limit=48)
    last_error = next(({"run_id": r.id, "at": iso(r.started_at), "errors": list(r.errors)} for r in runs if r.errors), None)
    problems: list[tuple[str, str]] = []  # (level, text)
    if ai_info and ai_info.get("ok") is False:
        problems.append(("error", f"Нейросеть не отвечает — уведомления идут с пометкой «фото не проверены». "
                                  f"{ai_info.get('error_ru') or ''}".strip()))
    for site in sites:
        if site["blocked"]:
            problems.append(("error", f"{site['host']} ограничил запросы — пауза до {site['cooldown_until'] or '…'}"))
        elif site["exhausted"]:
            problems.append(("warn", f"{site['host']}: лимит запросов в час исчерпан — продолжу позже"))
    for channel in notif["channels"]:
        if channel["enabled"] and channel["failed_24h"]:
            problems.append(("warn", f"{channel['label']}: не доставлено {channel['failed_24h']} — {channel['last_error']}"))
    if mon["paused"]:
        problems.append(("warn", "Проверки на паузе"))
    if runs and runs[0].errors:
        problems.append(("warn", f"В последней проверке ошибок: {len(runs[0].errors)}"))
    level = "error" if any(p[0] == "error" for p in problems) else ("warn" if problems else "ok")
    if problems:
        banner = problems[0][1]
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
        "problems": [{"level": lvl, "text_ru": text} for lvl, text in problems],
        "version": __version__,
        "uptime_seconds": int((utcnow() - ctx.started_at).total_seconds()),
        "monitor": mon,
        "ai": ai_info,
        "sites": sites,
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
    stamp = utcnow().astimezone(LOCAL_TZ).strftime("%Y-%m-%d_%H-%M")
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
    stamp = utcnow().astimezone(LOCAL_TZ).strftime("%Y-%m-%d_%H-%M")
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
        raise ApiError(500, "backup_failed", f"Не удалось создать копию: {exc}") from exc
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
