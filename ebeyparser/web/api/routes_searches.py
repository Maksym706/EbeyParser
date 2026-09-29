"""Searches CRUD. A search's id is the slug of its name ("handy-telefon-berlin-30-km")."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ValidationError

from ...config import SearchConfig
from .context import ApiContext, get_ctx
from .errors import ApiError, validation_error
from .presenters import search_errors, search_ids, search_view
from .routes_setup import config_estimate
from .schemas import ERRORS
from .validation import parse_search_url, search_field_problems

router = APIRouter()


class ToggleIn(BaseModel):
    enabled: bool | None = None  # None = flip


class ParseUrlIn(BaseModel):
    url: str


def _views(ctx: ApiContext) -> list[dict[str, Any]]:
    cfg = ctx.config
    stats = ctx.db.search_stats()
    runs = ctx.db.list_runs(limit=5)
    return [search_view(s, sid, stats.get(s.name), errors=search_errors(runs, s.name),
                        baseline_first_run=cfg.general.baseline_first_run)
            for s, sid in zip(cfg.searches, search_ids(cfg.searches))]


def _find(ctx: ApiContext, search_id: str) -> tuple[int, SearchConfig, str]:
    searches = ctx.config.searches
    ids = search_ids(searches)
    for i, (search, sid) in enumerate(zip(searches, ids)):
        if sid == search_id:
            return i, search, sid
    for i, (search, sid) in enumerate(zip(searches, ids)):  # the exact name works too
        if search.name == search_id:
            return i, search, sid
    raise ApiError(404, "not_found", "Такого поиска нет — возможно, его удалили")


def _view(ctx: ApiContext, name: str) -> dict[str, Any]:
    for view in _views(ctx):
        if view["name"] == name:
            return view
    raise ApiError(404, "not_found", "Поиск не найден")


def _checked(data: dict[str, Any]) -> SearchConfig:
    data = dict(data)
    if isinstance(data.get("name"), str):
        data["name"] = " ".join(data["name"].split())
    try:
        search = SearchConfig.model_validate(data)
    except ValidationError as exc:
        raise validation_error(exc) from exc
    problems = search_field_problems(search)
    if problems:
        raise validation_error(problems)
    return search


@router.get("/searches", summary="Поиски со статистикой (объявлений, выгодных, последняя проверка, обучение, ошибки)")
async def searches_list(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return {
        "items": _views(ctx),
        "editable": ctx.config_path is not None and ctx.config_writable(),
        "estimate": config_estimate(ctx),
        "ebay_configured": ctx.config.ebay.configured,
    }


@router.get("/searches/{search_id}", responses=ERRORS, summary="Один поиск")
async def search_get(search_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    _, search, _ = _find(ctx, search_id)
    return _view(ctx, search.name)


@router.post("/searches", status_code=201, responses=ERRORS, summary="Новый поиск (поля SearchConfig)")
async def search_create(data: dict[str, Any] = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    search = _checked(data)
    if ctx.config.search_by_name(search.name) is not None:
        raise ApiError(409, "conflict", f"Поиск с названием «{search.name}» уже есть")
    ctx.save_searches([*ctx.config.searches, search], reason="create")
    ctx.mark_done("categories")
    return _view(ctx, search.name)


@router.patch("/searches/{search_id}", responses=ERRORS, summary="Изменить поиск (только переданные поля)")
async def search_update(search_id: str, data: dict[str, Any] = Body(...),
                        ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    index, current, _ = _find(ctx, search_id)
    merged = {**current.model_dump(), **data}
    search = _checked(merged)
    if search.name != current.name and ctx.config.search_by_name(search.name) is not None:
        raise ApiError(409, "conflict", f"Поиск с названием «{search.name}» уже есть")
    searches = list(ctx.config.searches)
    searches[index] = search
    ctx.save_searches(searches, reason="update")
    return _view(ctx, search.name)


@router.delete("/searches/{search_id}", responses=ERRORS, summary="Удалить поиск (найденные объявления остаются)")
async def search_delete(search_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    index, search, sid = _find(ctx, search_id)
    searches = list(ctx.config.searches)
    searches.pop(index)
    ctx.save_searches(searches, reason="delete")
    return {"deleted": sid, "name": search.name}


@router.post("/searches/{search_id}/toggle", responses=ERRORS, summary="Включить/выключить (тело {enabled} необязательно)")
async def search_toggle(search_id: str, body: ToggleIn | None = None,
                        ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    index, search, _ = _find(ctx, search_id)
    enabled = (not search.enabled) if body is None or body.enabled is None else body.enabled
    searches = list(ctx.config.searches)
    searches[index] = search.model_copy(update={"enabled": enabled})
    ctx.save_searches(searches, reason="toggle")
    return _view(ctx, search.name)


@router.post("/searches/{search_id}/duplicate", status_code=201, responses=ERRORS, summary="Копия поиска")
async def search_duplicate(search_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_editable()
    _, search, _ = _find(ctx, search_id)
    names = {s.name for s in ctx.config.searches}
    name, n = f"{search.name} (копия)", 2
    while name in names:
        name = f"{search.name} (копия {n})"
        n += 1
    copy = search.model_copy(update={"name": name})
    ctx.save_searches([*ctx.config.searches, copy], reason="duplicate")
    return _view(ctx, name)


@router.post("/searches/parse-url", responses=ERRORS,
             summary="Разобрать ссылку поиска Kleinanzeigen: категория, место, радиус, цены, слова")
async def search_parse_url(body: ParseUrlIn) -> dict[str, Any]:
    try:
        parsed = parse_search_url(body.url)
    except ValueError as exc:
        raise validation_error({"url": str(exc)}) from None
    return {**parsed, "search": {"name": parsed["suggested_name"], "source": "kleinanzeigen", "url": body.url.strip(),
                                 "category_id": parsed["category_id"], "category_name": parsed["category_name"]}}
