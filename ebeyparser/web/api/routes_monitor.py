"""Monitor control and live data: state, run now, pause/resume, runs, SSE events, jobs,
checking one pasted link."""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ... import __version__
from ...models import RunSummary, utcnow
from .context import ApiContext, get_ctx
from .errors import ApiError
from .events import sse_stream
from .presenters import aware, deal_card, deal_detail, iso
from .schemas import ERRORS, CheckIn

log = logging.getLogger(__name__)
router = APIRouter()


# ------------------------------------------------------------------ helpers
def monitor_running(ctx: ApiContext) -> bool:
    mon = ctx.monitor
    if mon is None:
        return False
    task: asyncio.Task | None = getattr(ctx.app.state, "run_task", None)
    return bool(getattr(mon, "is_running", False)) or (task is not None and not task.done())


def loop_active(ctx: ApiContext) -> bool:
    task: asyncio.Task | None = getattr(ctx.app.state, "monitor_task", None)
    return task is not None and not task.done()


async def _guarded(coro: Any, label: str) -> Any:
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("%s failed", label)
        return None


def start_run(ctx: ApiContext, *, strict: bool = True) -> bool:
    """Start a pass in the background. strict: raise if impossible, else return False."""
    mon = ctx.monitor
    if mon is None or not hasattr(mon, "run_once"):
        if strict:
            raise ApiError(503, "unavailable", "Монитор не подключён — запусти программу командой `python -m ebeyparser`")
        return False
    if monitor_running(ctx):
        if strict:
            raise ApiError(409, "busy", "Проверка уже идёт — дождись её окончания")
        return False
    ctx.app.state.run_task = asyncio.create_task(_guarded(mon.run_once(), "manual run"), name="ebeyparser-run-once")
    return True


def run_view(run: RunSummary) -> dict[str, Any]:
    data = run.model_dump(mode="json")
    duration = None
    if run.finished_at is not None:
        duration = round((aware(run.finished_at) - aware(run.started_at)).total_seconds(), 1)
    data["duration_seconds"] = duration
    data["error_count"] = len(run.errors)
    return data


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def http_hosts(ctx: ApiContext) -> list[dict[str, Any]]:
    """Per-site request budget and block cooldowns: monitor.http_status() or data/http_state.json."""
    from ...runtime import http_state_path

    data: Any = None
    fn = getattr(ctx.monitor, "http_status", None)
    if callable(fn):
        try:
            data = await _maybe_await(fn())
        except Exception:  # noqa: BLE001
            log.exception("monitor.http_status failed")
    path = http_state_path(ctx.config.data_path)
    if not data and path.is_file():
        from ...scraper.http import PoliteClient

        client = PoliteClient.from_config(ctx.config.general, state_path=path)
        try:
            data = client.host_status()
        except Exception:  # noqa: BLE001
            log.exception("reading %s failed", path)
        finally:
            client.state_path = None  # read-only
            await client.aclose()
    rows: list[dict[str, Any]] = []
    for host, info in sorted(data.items()) if isinstance(data, dict) else []:
        if not isinstance(info, dict):
            continue
        limit = info.get("limit_per_hour")
        used = info.get("requests_last_hour") or 0
        until = info.get("cooldown_until")
        last = info.get("last_block_at")
        rows.append({
            "host": host,
            "requests_last_hour": used,
            "images_last_hour": info.get("images_last_hour") or 0,
            "limit_per_hour": limit,
            "load_percent": int(round(100 * used / limit)) if limit else None,
            "exhausted": limit is not None and limit > 0 and used >= limit,
            "blocked": bool(info.get("blocked")),
            "cooldown_until": iso(until) if isinstance(until, datetime) else None,
            "strikes": info.get("strikes") or 0,
            "last_block_reason": str(info.get("last_block_reason") or ""),
            "last_block_at": iso(last) if isinstance(last, datetime) else None,
            "note": str(info.get("note") or ""),
        })
    return rows


async def backlog(ctx: ApiContext) -> dict[str, int | None]:
    data: Any = None
    fn = getattr(ctx.monitor, "backlog_status", None)
    if callable(fn):
        try:
            data = await _maybe_await(fn())
        except Exception:  # noqa: BLE001
            log.exception("monitor.backlog_status failed")
    queue = expired = None
    if isinstance(data, dict):
        queue = data.get("pending", data.get("queue"))
        expired = data.get("expired_24h")
    try:
        if queue is None:
            queue = ctx.db.pending_count()
        if expired is None:
            expired = ctx.db.expired_since(utcnow() - timedelta(days=1))
    except Exception:  # noqa: BLE001
        log.exception("backlog counts failed")
    return {"pending": queue, "expired_24h": expired}


async def monitor_view(ctx: ApiContext, *, with_hosts: bool = True) -> dict[str, Any]:
    from .routes_app import _learning

    mon = ctx.monitor
    cfg = ctx.config
    next_run = getattr(mon, "next_run_at", None) if mon is not None else None
    last = getattr(mon, "last_summary", None) if mon is not None else None
    if last is None:
        runs = ctx.db.list_runs(limit=1)
        last = runs[0] if runs else None
    running = monitor_running(ctx)
    paused = bool(getattr(mon, "paused", False))
    progress = getattr(mon, "progress", None) if running else None
    if mon is None:
        state, text = "off", "Монитор выключен"
    elif running:
        state = "running"
        text = "Идёт проверка…"
        if isinstance(progress, dict) and progress.get("total"):
            text = f"Идёт проверка… {progress.get('index')} из {progress.get('total')} поисков"
    elif paused:
        state, text = "paused", "На паузе"
    elif isinstance(next_run, datetime):
        state = "idle"
        seconds = (aware(next_run) - utcnow()).total_seconds()
        text = "Проверка вот-вот начнётся" if seconds <= 60 else f"Следующая проверка через {int(seconds // 60)} мин"
    elif loop_active(ctx):
        state, text = "idle", "Монитор запущен"
    else:
        state, text = "stopped", "Автопроверка выключена"
    return {
        "available": mon is not None,
        "running": running,
        "paused": paused,
        "loop": loop_active(ctx),
        "state": state,
        "state_ru": text,
        "next_run_at": iso(next_run) if isinstance(next_run, datetime) else None,
        "interval_minutes": cfg.general.interval_minutes,
        "progress": dict(progress) if isinstance(progress, dict) else None,
        "last_summary": run_view(last) if isinstance(last, RunSummary) else None,
        "backlog": await backlog(ctx),
        "http": await http_hosts(ctx) if with_hosts else [],
        "learning": _learning(ctx),
        "searches_enabled": sum(1 for s in cfg.searches if s.enabled),
    }


# ------------------------------------------------------------------- routes
@router.get("/monitor", summary="Монитор: идёт/пауза, следующая проверка, прогресс, очередь, лимиты сайтов, обучение")
async def monitor_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await monitor_view(ctx)


@router.post("/monitor/run", status_code=202, responses=ERRORS, summary="Проверить сейчас (в фоне)")
@router.post("/run", status_code=202, include_in_schema=False)
async def monitor_run(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    start_run(ctx)
    return {"started": True, "monitor": await monitor_view(ctx, with_hosts=False)}


@router.post("/monitor/pause", responses=ERRORS, summary="Пауза автопроверок (сохраняется после перезапуска)")
async def monitor_pause(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    mon = ctx.monitor
    pause = getattr(mon, "pause", None)
    if not callable(pause):
        raise ApiError(503, "unavailable", "Монитор не подключён — пауза недоступна")
    pause()
    if not isinstance(getattr(mon, "on_event", None), list):
        ctx.hub.publish("monitor_paused", {"paused": True})
    return await monitor_view(ctx, with_hosts=False)


@router.post("/monitor/resume", responses=ERRORS, summary="Продолжить автопроверки")
async def monitor_resume(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    mon = ctx.monitor
    resume = getattr(mon, "resume", None)
    if not callable(resume):
        raise ApiError(503, "unavailable", "Монитор не подключён")
    resume()
    if not isinstance(getattr(mon, "on_event", None), list):
        ctx.hub.publish("monitor_resumed", {"paused": False})
    return await monitor_view(ctx, with_hosts=False)


@router.get("/runs", summary="Последние проверки (новые сверху)")
async def runs(limit: int = Query(30, ge=1, le=500), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"items": [run_view(r) for r in ctx.db.list_runs(limit=limit)]}


@router.get("/events", summary="Server-Sent Events (text/event-stream): run_started, run_progress, run_finished, "
                               "deal_found, health_alert, settings_changed, searches_changed, deal_updated, "
                               "monitor_paused, monitor_resumed, job_progress, job_finished")
async def events(
    request: Request,
    last_event_id: int | None = Query(None, ge=0),
    max_events: int | None = Query(None, ge=1, le=10000),
    timeout: float | None = Query(None, gt=0, le=86400),
    ctx: ApiContext = Depends(get_ctx),
) -> StreamingResponse:
    header = request.headers.get("last-event-id")
    if last_event_id is None and header and header.strip().isdigit():
        last_event_id = int(header.strip())
    stream = sse_stream(ctx.hub, last_id=last_event_id, is_disconnected=request.is_disconnected,
                        ready={"version": __version__}, max_events=max_events, timeout=timeout)
    return StreamingResponse(stream, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@router.get("/events/recent", summary="Последние события из памяти (для отладки / без SSE)")
async def events_recent(limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"items": [e.as_dict() for e in ctx.hub.recent(limit)], "last_event_id": ctx.hub.last_id}


# -------------------------------------------------------------------- jobs
@router.get("/jobs", summary="Фоновые задачи (проверка ссылки, переоценка)")
async def jobs_list(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"items": [j.as_dict(with_result=False) for j in ctx.jobs.all()]}


@router.get("/jobs/{job_id}", responses=ERRORS, summary="Задача: статус, шаги, результат (карточка сделки)")
async def job_get(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise ApiError(404, "not_found", "Задача не найдена (они хранятся только до перезапуска)")
    return job.as_dict()


def accepted(job: Any) -> JSONResponse:
    return JSONResponse({"job": job.as_dict(), "poll_url": f"/api/v1/jobs/{job.id}"}, status_code=202)


@router.post("/check", status_code=202, responses=ERRORS,
             summary="Оценить объявление по ссылке (Kleinanzeigen / eBay) — 202 + задача; шаги в job_progress")
async def check_url(body: CheckIn, ctx: ApiContext = Depends(get_ctx)) -> JSONResponse:
    from ...monitor import ad_id_from_url

    mon = ctx.monitor
    if mon is None or not callable(getattr(mon, "evaluate_url", None)):
        raise ApiError(503, "unavailable", "Монитор не подключён — проверка ссылки недоступна")
    url = body.url.strip()
    ad_id = ad_id_from_url(url)
    if not ad_id or not url.lower().startswith(("http://", "https://")):
        raise ApiError(422, "validation", "Не похоже на ссылку на объявление Kleinanzeigen или eBay",
                       fields={"url": "вставь ссылку вида https://www.kleinanzeigen.de/s-anzeige/…"})

    async def work(job: Any) -> dict[str, Any]:
        async with ctx.check_lock:
            job.progress("fetch", "Открываю объявление…")
            kwargs: dict[str, Any] = {"purpose": body.purpose, "target_price": body.target_price}
            if "progress" in inspect.signature(mon.evaluate_url).parameters:
                kwargs["progress"] = job.progress
            deal = await mon.evaluate_url(url, **kwargs)
            job.progress("done", "Готово")
        return _deal_result(ctx, deal.listing.ad_id)

    return accepted(ctx.jobs.start("check", work, params=body.model_dump(), ad_id=ad_id))


def _deal_result(ctx: ApiContext, ad_id: str) -> dict[str, Any]:
    """The finished job's result: the deal detail; also announced as deal_updated."""
    found = ctx.db.get_deal_extras(ad_id)
    if found is None:
        raise ApiError(404, "not_found", "Объявление не сохранилось в базе")
    deal, extras = found
    ctx.hub.publish("deal_updated", {"ad_id": ad_id, "card": deal_card(deal, extras, config=ctx.config)})
    return deal_detail(deal, extras, ctx.config)
