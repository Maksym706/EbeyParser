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
from ...errors_ru import friendly_run_error
from ...models import RunSummary, utcnow
from ...timefmt import at_label, when_label
from .context import DEMO_RUNS_KEY, ApiContext, get_ctx
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


NO_MONITOR_RU = ("Проверки сейчас недоступны: программа запущена без фоновых проверок. "
                 "Перезапусти программу — проверки включатся сами")


def start_run(ctx: ApiContext, *, strict: bool = True) -> bool:
    """Start a pass in the background. strict: raise if impossible, else return False."""
    mon = ctx.monitor
    if mon is None or not hasattr(mon, "run_once"):
        if strict:
            restart = callable(getattr(ctx.app.state, "restart_callback", None))
            raise ApiError(503, "unavailable", NO_MONITOR_RU,
                           action={"label_ru": "Перезапустить", "href": "/settings/data"} if restart else None)
        return False
    if monitor_running(ctx):
        if strict:
            raise ApiError(409, "busy", "Проверка уже идёт — дождись её окончания")
        return False
    ctx.app.state.run_task = asyncio.create_task(_guarded(mon.run_once(), "manual run"), name="ebeyparser-run-once")
    return True


def run_view(run: RunSummary) -> dict[str, Any]:
    """A pass for the UI: errors in plain Russian (old runs may hold technical text; the
    technical text of new runs is in `error_details`), times also as local labels."""
    data = run.model_dump(mode="json")
    duration = None
    if run.finished_at is not None:
        duration = round((aware(run.finished_at) - aware(run.started_at)).total_seconds(), 1)
    data["duration_seconds"] = duration
    data["error_count"] = len(run.errors)
    data["errors"] = [friendly_run_error(e) for e in run.errors]
    details = list(getattr(run, "error_details", []) or [])
    if not details:
        details = [e for e, shown in zip(run.errors, data["errors"]) if e != shown]
    data["error_details"] = details
    data["started_at_label"] = when_label(run.started_at)
    data["finished_at_label"] = when_label(run.finished_at) if run.finished_at else ""
    return data


def demo_run_ids(ctx: ApiContext) -> set[int]:
    try:
        return {int(i) for i in (ctx.get_json(DEMO_RUNS_KEY, []) or [])}
    except (TypeError, ValueError):
        return set()


def real_runs(ctx: ApiContext, limit: int) -> list[RunSummary]:
    """Recent passes without the ones the demo data added."""
    demo = demo_run_ids(ctx)
    runs = ctx.db.list_runs(limit=limit + len(demo))
    return [r for r in runs if r.id not in demo][:limit]


SITE_LABELS = {"kleinanzeigen": "Kleinanzeigen", "ebay": "eBay"}


def site_label(host: str) -> str:
    """'www.kleinanzeigen.de' -> 'Kleinanzeigen' (the user never sees host names)."""
    low = (host or "").lower()
    for key, label in SITE_LABELS.items():
        if key in low:
            return label + (" (фото)" if low.startswith("img.") or "ebayimg" in low else "")
    return host


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
        if isinstance(until, str):
            until = _parse_dt(until)
        if isinstance(last, str):
            last = _parse_dt(last)
        blocked = bool(info.get("blocked"))
        rows.append({
            "host": host,
            "site": site_label(host),
            "requests_last_hour": used,
            "images_last_hour": info.get("images_last_hour") or 0,
            "limit_per_hour": limit,
            "load_percent": int(round(100 * used / limit)) if limit else None,
            "exhausted": limit is not None and limit > 0 and used >= limit,
            "blocked": blocked,
            "cooldown_until": iso(until) if isinstance(until, datetime) else None,
            "cooldown_until_label": when_label(until) if isinstance(until, datetime) else "",
            "strikes": info.get("strikes") or 0,
            "last_block_reason": str(info.get("last_block_reason") or ""),
            "last_block_at": iso(last) if isinstance(last, datetime) else None,
            "last_block_at_label": when_label(last) if isinstance(last, datetime) else "",
            "note": str(info.get("note") or ""),
        })
    return rows


def _parse_dt(raw: str) -> datetime | None:
    try:
        return aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def cooldown_view(hosts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A site asked us to pause (block cooldown): {until, until_label, hosts, sites, text_ru}
    or None. The latest end wins when several sites cool down."""
    now = utcnow()
    blocked: list[tuple[datetime, dict[str, Any]]] = []
    for row in hosts:
        until = _parse_dt(row["cooldown_until"]) if row.get("cooldown_until") else None
        if row.get("blocked") and until is not None and until > now:
            blocked.append((until, row))
    if not blocked:
        return None
    until = max(u for u, _ in blocked)
    sites = list(dict.fromkeys(row.get("site") or site_label(row["host"]) for _, row in blocked))
    who = sites[0] if len(sites) == 1 else "Сайт"
    return {
        "until": iso(until),
        "until_label": when_label(until),
        "hosts": [row["host"] for _, row in blocked],
        "sites": sites,
        "text_ru": f"{who} попросил паузу — продолжу {at_label(until)}",
    }


def only_kleinanzeigen(ctx: ApiContext) -> bool:
    enabled = [s for s in ctx.config.searches if s.enabled]
    return bool(enabled) and all(s.source == "kleinanzeigen" for s in enabled)


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


def scout_view(ctx: ApiContext) -> dict[str, Any]:
    """The AI scout (docs/design/AI_SCOUT.md): on/off, "успевает смотреть N из M новых объявлений
    в час", speed, mode, the vision queue; from the running monitor or the last stored snapshot."""
    from ...ai.scout import status_view
    from ...ai.triage import expected_sec_per_ad, too_small_for_triage

    fn = getattr(ctx.monitor, "scout_status", None)
    if callable(fn):
        try:
            view = fn()
            if isinstance(view, dict):
                return view
        except Exception:  # noqa: BLE001 - a status tile never breaks the page
            log.exception("scout status failed")
    sc = ctx.config.ai.scout
    model = sc.model or ctx.config.ai.model
    return status_view(enabled=sc.enabled, mode_setting=sc.mode, provider=sc.provider,
                       base_url=sc.base_url or ctx.config.ai.base_url, model=model,
                       own_endpoint=bool(sc.base_url.strip()), snap={}, vision_waiting=0,
                       vision_wait_minutes=ctx.config.ai.vision_wait_minutes,
                       expected_sec_per_ad=expected_sec_per_ad(model), pass_share=sc.pass_share,
                       max_per_hour=sc.max_per_hour, too_small=sc.enabled and too_small_for_triage(model))


async def monitor_view(ctx: ApiContext, *, with_hosts: bool = True) -> dict[str, Any]:
    from .routes_app import _learning

    mon = ctx.monitor
    cfg = ctx.config
    next_run = getattr(mon, "next_run_at", None) if mon is not None else None
    last = getattr(mon, "last_summary", None) if mon is not None else None
    running = monitor_running(ctx)
    paused = bool(getattr(mon, "paused", False))
    progress = getattr(mon, "progress", None) if running else None
    hosts = await http_hosts(ctx)
    cooldown = cooldown_view(hosts)
    looping = loop_active(ctx)
    if mon is None:
        state, text = "off", "Проверки выключены"
    elif running:
        state = "running"
        text = "Идёт проверка…"
        if isinstance(progress, dict) and progress.get("total"):
            text = f"Идёт проверка… {progress.get('index')} из {progress.get('total')} поисков"
    elif paused:
        state, text = "paused", "На паузе"
    elif cooldown is not None and (looping or isinstance(next_run, datetime)):
        state, text = "cooldown", cooldown["text_ru"]
    elif isinstance(next_run, datetime):
        state = "idle"
        seconds = (aware(next_run) - utcnow()).total_seconds()
        text = "Проверка вот-вот начнётся" if seconds <= 60 else f"Следующая проверка через {int(seconds // 60)} мин"
    elif looping:
        state, text = "idle", "Проверки включены"
    else:
        state, text = "stopped", "Автопроверка выключена — проверяю только по кнопке «Проверить сейчас»"
    if last is None or (isinstance(last, RunSummary) and last.id in demo_run_ids(ctx)):
        runs = real_runs(ctx, 1)
        last = runs[0] if runs else None
    view: dict[str, Any] = {
        "available": mon is not None,
        "running": running,
        "paused": paused,
        "loop": looping,
        "state": state,
        "state_ru": text,
        "next_run_at": iso(next_run) if isinstance(next_run, datetime) else None,
        "next_run_label": when_label(next_run) if isinstance(next_run, datetime) else "",
        "cooldown": cooldown,
        "interval_minutes": cfg.general.interval_minutes,
        "progress": dict(progress) if isinstance(progress, dict) else None,
        "last_summary": run_view(last) if isinstance(last, RunSummary) else None,
        "backlog": await backlog(ctx),
        "http": hosts if with_hosts else [],
        "searches_enabled": sum(1 for s in cfg.searches if s.enabled),
        "scout": scout_view(ctx),
    }
    view["learning"] = _learning(ctx, view)
    return view


# ------------------------------------------------------------------- routes
@router.get("/monitor", summary="Монитор: идёт/пауза, следующая проверка, прогресс, очередь, лимиты сайтов, обучение")
async def monitor_get(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return await monitor_view(ctx)


@router.post("/monitor/run", status_code=202, responses=ERRORS, summary="Проверить сейчас (в фоне)")
@router.post("/run", status_code=202, include_in_schema=False)
async def monitor_run(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    cooldown = cooldown_view(await http_hosts(ctx))
    if cooldown is not None and only_kleinanzeigen(ctx) and ctx.monitor is not None:
        # every search would only meet the pause: say so instead of "Проверка запущена"
        raise ApiError(409, "cooldown", f"{cooldown['text_ru']}. Сейчас проверять нельзя: это защита от блокировки")
    start_run(ctx)
    message = "Проверка запущена"
    if cooldown is not None:
        message = f"Проверка запущена. {cooldown['text_ru']}: поиски на этом сайте подождут"
    return {"started": True, "message_ru": message, "cooldown": cooldown,
            "monitor": await monitor_view(ctx, with_hosts=False)}


@router.post("/monitor/pause", responses=ERRORS, summary="Пауза автопроверок (сохраняется после перезапуска)")
async def monitor_pause(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    mon = ctx.monitor
    pause = getattr(mon, "pause", None)
    if not callable(pause):
        raise ApiError(503, "unavailable", NO_MONITOR_RU)
    pause()
    if not isinstance(getattr(mon, "on_event", None), list):
        ctx.hub.publish("monitor_paused", {"paused": True})
    return await monitor_view(ctx, with_hosts=False)


@router.post("/monitor/resume", responses=ERRORS, summary="Продолжить автопроверки")
async def monitor_resume(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    mon = ctx.monitor
    resume = getattr(mon, "resume", None)
    if not callable(resume):
        raise ApiError(503, "unavailable", NO_MONITOR_RU)
    resume()
    if not isinstance(getattr(mon, "on_event", None), list):
        ctx.hub.publish("monitor_resumed", {"paused": False})
    return await monitor_view(ctx, with_hosts=False)


@router.get("/runs", summary="Последние проверки (новые сверху; без проверок из демо-данных)")
async def runs(limit: int = Query(30, ge=1, le=500), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"items": [run_view(r) for r in real_runs(ctx, limit)]}


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
        raise ApiError(503, "unavailable", "Проверка ссылки сейчас недоступна: программа запущена без фоновых "
                                           "проверок. Перезапусти программу")
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
