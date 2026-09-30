"""«Сборки» (build projects): a goal and a budget → a parts plan with alternatives and
compatibility checks → personal searches for every part → the best current offer per slot,
alerts when an offer beats its target or the whole build fits the budget, purchases.

Mounted by web/api/__init__.py; alerts: install_project_alerts() hooks projects.hooks.attach() into
Monitor.on_event (the same wiring the headless CLI modes use) plus the API's own deal_updated."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field, ValidationError

from ...config import LLMSettings, SearchConfig
from ...models import utcnow
from ...projects import knowledge as kb
from ...projects.market import Market
from ...projects.models import Plan, PlanOption, PlanSlot, Project, Purchase
from ...projects.planner import (
    CustomItem,
    PlanRequest,
    apply_dependents,
    custom_slots,
    gpu_candidates,
    option_label,
    plan_goal,
    resolve,
)
from ...projects.store import ProjectStore
from ...projects.tracker import HAGGLE_ROOM, ProjectAlerter, ProjectState, option_target, project_state, round_price
from ...scraper.categories import WISHLIST_EXCLUDE_KEYWORDS
from .context import ApiContext, get_ctx
from .errors import ApiError, validation_error
from .presenters import API_STATUSES, STATUS_LABELS, search_ids
from .project_views import offer_view, plan_view, project_card, project_view, templates_payload
from .schemas import ERRORS, ApiModel

log = logging.getLogger(__name__)
router = APIRouter()

NOT_FOUND_RU = "Сборка не найдена — возможно, её удалили"
PLANNER_TIMEOUT = 90.0  # per model; the UI shows a waiting state and offers «без нейросети»
MAX_ALTERNATIVES = 2


# ===================================================================== bodies
class ProjectCreateIn(PlanRequest):
    """A plan from POST /projects/plan (`plan`, possibly with other `choices`), or the same fields
    as /projects/plan (the server plans it)."""

    plan: dict[str, Any] | None = None
    choices: dict[str, str] = Field(default_factory=dict)


class SlotPatchIn(ApiModel):
    status: Literal["open", "have", "skipped"] | None = None
    target_price: float | None = Field(None, gt=0, le=100000)
    auto_target: bool = False  # forget the own target: back to the budget split
    note: str | None = Field(None, max_length=300)
    qty: int | None = Field(None, ge=1, le=8)


class ProjectPatchIn(ApiModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    goal: str | None = Field(None, max_length=2000)
    budget: float | None = Field(None, gt=0, le=100000)
    clear_budget: bool = False
    status: Literal["tracking", "paused"] | None = None
    choices: dict[str, str] = Field(default_factory=dict)
    adjust: bool = True  # fix the dependent parts (PSU, board, RAM, case) that became incompatible
    slots: dict[str, SlotPatchIn] = Field(default_factory=dict)
    add_items: list[CustomItem] = Field(default_factory=list, max_length=10)
    remove_slots: list[str] = Field(default_factory=list, max_length=20)
    location: str | None = Field(None, max_length=80)
    radius_km: int | None = Field(None, ge=0, le=500)


class TrackIn(ApiModel):
    alternatives: bool = True  # also watch alternatives (up to max_alternatives per slot) ...
    alternatives_scope: Literal["key", "all"] = "key"  # ... of the key part only (GPU, disks), or of every part
    max_alternatives: int = Field(MAX_ALTERNATIVES, ge=0, le=5)
    slots: list[str] | None = None  # only these slots (default: every open slot)
    location: str | None = Field(None, max_length=80)
    radius_km: int | None = Field(None, ge=0, le=500)
    ebay: bool = False  # also eBay searches (when eBay is connected)
    price_filter: bool = False  # also a price range on the site (fewer ads, but the price history learns nothing)
    dry_run: bool = False  # only say what would be created (and the load)


class BoughtIn(ApiModel):
    price: float = Field(..., gt=0, le=100000)  # what was paid in total (incl. shipping) for `qty` units
    qty: int | None = Field(None, ge=1, le=8)
    ad_id: str | None = Field(None, max_length=64)
    option: str | None = Field(None, max_length=40)
    note: str = Field("", max_length=300)


# ==================================================================== helpers
def store_of(ctx: ApiContext) -> ProjectStore:
    return ProjectStore(ctx.db)


def market_of(ctx: ApiContext) -> Market:
    cached = getattr(ctx, "project_market", None)
    if cached is None or cached.db is not ctx.db:
        cached = Market(ctx.db, ctx.config)
        ctx.project_market = cached  # type: ignore[attr-defined]
    cached.config = ctx.config
    return cached


def _get(ctx: ApiContext, project_id: int) -> Project:
    project = store_of(ctx).get(project_id)
    if project is None:
        raise ApiError(404, "not_found", NOT_FOUND_RU)
    return project


def _search_info(ctx: ApiContext) -> dict[str, dict[str, Any]]:
    searches = ctx.config.searches
    return {s.name: {"exists": True, "enabled": s.enabled, "id": sid}
            for s, sid in zip(searches, search_ids(searches))}


def _view(ctx: ApiContext, project: Project, state: ProjectState | None = None) -> dict[str, Any]:
    store = store_of(ctx)
    market = market_of(ctx)
    state = state or project_state(project, store, market)
    stats = store.alert_counts().get(project.id, (0, None))
    return project_view(project, state, market, links=store.searches(project.id), alerts=store.alerts(project.id),
                        search_info=_search_info(ctx), alert_stats=stats)


def _card(ctx: ApiContext, project: Project, state: ProjectState | None = None) -> dict[str, Any]:
    store = store_of(ctx)
    state = state or project_state(project, store, market_of(ctx))
    return project_card(project, state, searches=len(store.searches(project.id)),
                        alerts=store.alert_counts().get(project.id, (0, None)))


def _deleting(ctx: ApiContext) -> set[int]:
    """Ids of builds whose DELETE is running right now (their late updates are dropped)."""
    ids = getattr(ctx, "projects_deleting", None)
    if ids is None:
        ids = ctx.projects_deleting = set()  # type: ignore[attr-defined]
    return ids


def publish(ctx: ApiContext, project: Project | None, reason: str, **extra: Any) -> None:
    """project_updated for a build that still exists. Never for one being deleted or already gone
    (an alert check that was running when it was deleted): its DELETE sends project_deleted, and a
    later project_updated would bring the card back in the UI."""
    pid = project.id if project else extra.pop("id", None)
    if pid is not None and (pid in _deleting(ctx) or not store_of(ctx).exists(pid)):
        log.debug("project_updated (%s) of deleted build %s dropped", reason, pid)
        return
    data: dict[str, Any] = {"id": pid, "reason": reason, **extra}
    state = data.pop("state", None)
    if project is not None:
        try:
            data["card"] = _card(ctx, project, state)
        except Exception:  # noqa: BLE001 - an event must never fail a request
            log.exception("project card for event failed")
            data["card"] = None
    ctx.hub.publish("project_updated", data)


AI_OFF_RU = ("Нейросеть выключена — план составлен по правилам. Включи её в «Настройки» → «Нейросеть», "
             "чтобы она помогала с целями своими словами")


def planner_llm_settings(ctx: ApiContext) -> list[LLMSettings]:
    """Models that may help with a plan, best first: the main local model (bigger, better at
    reasoning), then the AI scout's always-on text model (ai.scout, docs/design/AI_SCOUT.md) — the
    main one may live on a PC that is switched off. Text-only JSON calls, never photos."""
    ai = ctx.config.ai
    out: list[LLMSettings] = []

    def add(provider: str, base_url: str, model: str, api_key: str, timeout: float) -> None:
        if any(s.base_url == base_url and s.model == model for s in out):
            return
        out.append(LLMSettings(provider=provider, base_url=base_url, model=model, api_key=api_key,  # type: ignore[arg-type]
                               timeout_seconds=min(float(timeout), PLANNER_TIMEOUT), temperature=0.2,
                               max_tokens=1200))

    if ai.enabled:
        add(ai.provider, ai.base_url, ai.model, ai.api_key, ai.timeout_seconds)
    scout = getattr(ai, "scout", None)
    if scout is not None and getattr(scout, "enabled", False):
        own = bool(getattr(scout, "base_url", ""))
        add(scout.provider if own else ai.provider, scout.base_url or ai.base_url, scout.model or ai.model,
            scout.api_key or ai.api_key, scout.timeout_seconds)
    return out


def _client(ctx: ApiContext, settings: LLMSettings) -> Any:
    if settings.provider in ("openai", "ollama"):
        from ...ai.client import VisionLLM

        return VisionLLM(settings, transport=ctx.http_transport)
    from ...ai.claude import make_llm

    return make_llm(settings)


async def _plan(ctx: ApiContext, req: PlanRequest) -> Plan:
    market = market_of(ctx)
    models = planner_llm_settings(ctx) if req.use_ai else []
    if not models:
        return await plan_goal(req, market, llm=None, ai_off_reason=AI_OFF_RU if req.use_ai else "")
    plan: Plan | None = None
    for settings in models:  # the next model only when the previous one did not answer usefully
        try:
            llm = _client(ctx, settings)
        except Exception as exc:  # noqa: BLE001
            log.info("planner LLM %s unavailable: %s", settings.model, exc)
            continue
        try:
            plan = await plan_goal(req, market, llm=llm, model_name=settings.model)
        finally:
            try:
                await llm.aclose()
            except Exception:  # noqa: BLE001
                pass
        if plan.ai and plan.ai.get("ok"):
            return plan
    return plan or await plan_goal(req, market, llm=None,
                                   ai_off_reason="Нейросеть недоступна — план составлен по правилам")


def _check_template(template: str | None) -> None:
    if template and template not in kb.TEMPLATES:
        raise validation_error({"template": "Нет такого шаблона — выбери из списка"})


def _sanitized_plan(raw: dict[str, Any]) -> Plan:
    """A plan sent back by the client: known parts only, free-form items need a search phrase."""
    try:
        plan = Plan.model_validate(raw)
    except ValidationError as exc:
        raise validation_error(exc, "План не читается — составь его заново", prefix="plan") from exc
    _check_template(plan.template)
    slots: list[PlanSlot] = []
    seen: set[str] = set()
    for slot in plan.slots[:30]:
        if not slot.key or slot.key in seen:
            continue
        seen.add(slot.key)
        options = []
        for opt in slot.options[:12]:
            if opt.kb_key and kb.part(opt.kb_key) is None:
                continue
            if not opt.kb_key and not (opt.query or opt.label).strip():
                continue
            opt.qty = max(1, min(opt.qty, 20))
            options.append(opt)
        if not options:
            continue
        slot.options = options
        if slot.option() is None:
            slot.chosen = options[0].key
        slot.status = "open"
        slot.purchases = []
        slots.append(slot)
    plan.slots = slots
    return plan


def _apply_choices(plan: Plan, choices: dict[str, str], *, adjust: bool) -> None:
    problems: dict[str, str] = {}
    for key, option in choices.items():
        slot = plan.slot(key)
        if slot is None:
            problems[f"choices.{key}"] = "Нет такой части в сборке"
        elif slot.option(option) is None:
            problems[f"choices.{key}"] = "Нет такого варианта"
    if problems:
        raise validation_error(problems, "Не получилось выбрать вариант")
    for key, option in choices.items():
        plan.slot(key).chosen = option  # type: ignore[union-attr]
    if choices and adjust:
        fixed = {s.key for s in plan.slots if s.status != "open"}
        apply_dependents(plan, keep=set(choices) | fixed, ideal=False)


# ============================================================== search sync
def _default_location(ctx: ApiContext, project: Project) -> tuple[str, int | None]:
    if project.location:
        return project.location, project.radius_km
    places = Counter((s.location, s.radius_km) for s in ctx.config.searches if s.location)
    if places:
        (loc, radius), _ = places.most_common(1)[0]
        return loc, radius
    return "", None


def _unique_name(base: str, taken: set[str]) -> str:
    name, n = base, 2
    while name in taken:
        name = f"{base} ({n})"
        n += 1
    return name


def _search_for(project: Project, opt: PlanOption, target: float | None, top: float | None, typical: float | None,
                *, name: str, location: str, radius: int | None, source: str = "kleinanzeigen",
                price_filter: bool = False) -> SearchConfig:
    """A personal search for one part. By default WITHOUT a price range on the site: a price-filtered
    search teaches the price history nothing (the monitor skips it — a list cut at max_price drags
    medians down), and the market price is what the targets and trends stand on. The monitor still
    drops ads above 1.3× the target for free (estimator.prefilter), which is the option's max_price."""
    part = kb.part(opt.kb_key)
    query = part.query if part else (opt.query or opt.label)
    excludes = list(dict.fromkeys([*WISHLIST_EXCLUDE_KEYWORDS, *(part.exclude if part else ())]))
    data: dict[str, Any] = {
        "name": name, "source": source, "query": query[:80], "purpose": "personal", "target_price": target,
        "exclude_keywords": excludes, "enabled": True,
    }
    if price_filter:
        data["min_price"] = round_price(typical * 0.3) if typical and typical >= 50 else None
        data["max_price"] = top if top and (target is None or top >= target) else None
    if source == "kleinanzeigen":
        data.update({"location": location, "radius_km": radius if location else None})
    return SearchConfig.model_validate(data)


def _gpu_candidates(project: Project, market: Market) -> dict[str, Any]:
    if project.slot("gpu") is None or project.requirements.kind not in ("llm", "gaming"):
        return {}
    return {c.option.key: c for c in gpu_candidates(project, market)}


def _freeze_targets(project: Project, state: ProjectState, market: Market, candidates: dict[str, Any]) -> None:
    """Targets and max prices the searches use (auto ones follow the budget split at this moment;
    a GPU alternative is judged as if it were chosen, with its own platform)."""
    for slot in project.slots:
        for opt in slot.options:
            if opt.target_by == "user" and opt.target_price:
                continue
            opt.target_price = opt.max_price = None
    for slot in project.slots:
        for opt in slot.options:
            if opt.target_by == "user" and opt.target_price:
                continue
            ratio = state.ratio
            cand = candidates.get(opt.key) if slot.key == "gpu" and opt.key != slot.chosen else None
            if cand is not None and cand.total and project.budget:
                ratio = (project.budget - state.totals.get("spent", 0.0)) / cand.total
            target, top = option_target(opt, kb.part(opt.kb_key), market, ratio)
            opt.target_price, opt.max_price = target, top


KEY_SLOTS = ("gpu", "hdd")  # the expensive parts whose alternatives are worth their own searches


def _options_to_track(slot: PlanSlot, body: TrackIn, budget: float | None = None,
                      candidates: dict[str, Any] | None = None) -> list[PlanOption]:
    """The chosen option, plus alternatives: by default only for the key part (every extra keyword
    search costs requests per pass; a smaller PSU or case is no alternative anyway). GPU
    alternatives: the ones the budget can reach first, then the best of the rest (a steal alert)."""
    chosen = slot.option()
    if chosen is None:
        return []
    out = [chosen]
    wanted = body.alternatives_scope == "all" or slot.key in KEY_SLOTS or slot.kind == "gpu"
    if slot.purchases:
        wanted = False  # one card of two is bought: the second one must match it
    if body.alternatives and body.max_alternatives and wanted:
        alts = [o for o in slot.options if o.key != chosen.key]
        if candidates:
            def rank(o: PlanOption) -> tuple[int, float]:
                cand = candidates.get(o.key)
                if cand is None:
                    return (2, 0.0)
                if budget is None or cand.total * 0.85 <= budget:
                    return (0, -cand.quality)  # reachable: the fastest first
                return (1, -cand.quality / max(cand.total, 1.0))  # a steal would be needed: best value first
            alts.sort(key=rank)
        out += alts[: body.max_alternatives]
    return out


def _short(label: str) -> str:
    """'Большой корпус: 8+ слотов (Define 7 XL …)' -> 'Большой корпус' (search names stay short)."""
    return label.split(" (")[0].split(":")[0].strip()


def sync_searches(ctx: ApiContext, project: Project, *, body: TrackIn | None = None, save: bool = True) -> dict[str, Any]:
    """Create / update the personal searches of every open slot (chosen part + alternatives), pause
    the ones of finished slots. Returns what changed and the load estimate."""
    body = body or TrackIn.model_validate({})
    store = store_of(ctx)
    market = market_of(ctx)
    state = project_state(project, store, market)
    candidates = _gpu_candidates(project, market)
    _freeze_targets(project, state, market, candidates)
    links = {(link.slot, link.option): link for link in store.searches(project.id)}
    by_name = {s.name: s for s in ctx.config.searches}
    taken = set(by_name)
    location, radius = (body.location, body.radius_km) if body.location is not None else _default_location(ctx, project)
    if body.location is not None or not project.location:
        project.location, project.radius_km = location or "", radius
    wanted: dict[str, SearchConfig] = {}
    new_links: list[tuple[str, str, str]] = []
    created: list[str] = []
    updated: list[str] = []
    paused: list[str] = []
    only = set(body.slots or [])
    ebay_ok = body.ebay and ctx.config.ebay.configured
    tracking = project.status != "paused"
    for slot in project.slots:
        rs = state.r[slot.key]
        slot_open = rs.need_qty > 0 and tracking
        if only and slot.key not in only and slot_open:
            continue
        keep = {o.key for o in _options_to_track(slot, body, project.budget, candidates)} if slot_open else set()
        for opt in slot.options:
            sources = ["kleinanzeigen"] + (["ebay"] if ebay_ok else [])
            for source in sources:
                link_key = (slot.key, opt.key if source == "kleinanzeigen" else f"{opt.key}@ebay")
                link = links.get(link_key)
                existing = by_name.get(link.search_name) if link else None
                if opt.key not in keep:
                    if existing is not None and existing.enabled:
                        wanted[existing.name] = existing.model_copy(update={"enabled": False})
                        paused.append(existing.name)
                    continue
                typical = market.price(kb.part(opt.kb_key), opt).typical
                part = kb.part(opt.kb_key)
                base = f"{project.name[:40]} · {_short(part.label) if part else (opt.label or opt.query)[:40]}"
                if source == "ebay":
                    base += " · eBay"
                name = existing.name if existing is not None else _unique_name(base[:90], taken)
                taken.add(name)
                search = _search_for(project, opt, opt.target_price, opt.max_price, typical, name=name,
                                     location=location, radius=radius, source=source, price_filter=body.price_filter)
                if existing is not None:
                    # the user's own tweaks of the search (words, place) stay; prices and state follow the project
                    update = {"target_price": search.target_price, "max_price": search.max_price, "enabled": True,
                              "purpose": "personal"}
                    if any(getattr(existing, k) != v for k, v in update.items()):
                        wanted[name] = existing.model_copy(update=update)
                        updated.append(name)
                    continue
                created.append(name)
                wanted[name] = search
                new_links.append((slot.key, link_key[1], name))
    active = sum(1 for link in links.values() if link.search_name in by_name
                 and wanted.get(link.search_name, by_name[link.search_name]).enabled)
    searches = [wanted.pop(s.name, s) for s in ctx.config.searches] + list(wanted.values())
    from .routes_setup import config_estimate

    estimate = config_estimate(ctx, searches=searches)
    result = {"created": created, "updated": updated, "paused": paused, "estimate": estimate,
              "tracked": len(created) + active,
              "searches": [s.model_dump(mode="json") for s in searches if s.name in set(created) | set(updated)]}
    if not save:
        return result
    if created or updated or paused:
        ctx.save_searches(searches, reason="project")
    for slot_key, opt_key, name in new_links:
        store.link_search(project.id, slot_key, opt_key, name)
    store.save(project)
    return result


def set_searches_enabled(ctx: ApiContext, names: set[str], enabled: bool) -> list[str]:
    changed = [s.name for s in ctx.config.searches if s.name in names and s.enabled != enabled]
    if not changed:
        return []
    searches = [s.model_copy(update={"enabled": enabled}) if s.name in names else s for s in ctx.config.searches]
    ctx.save_searches(searches, reason="project")
    return changed


# ===================================================================== routes
@router.get("/projects/templates", summary="Шаблоны сборок (LLM-сервер 24/48 ГБ, NAS, игровой ПК, свой список)")
async def projects_templates(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    models = planner_llm_settings(ctx)
    return {
        "items": templates_payload(),
        "ai": {"available": bool(models), "model": models[0].model if models else "",
               "label_ru": "Нейросеть поможет понять цель своими словами" if models else AI_OFF_RU},
        "quants": [{"key": q.key, "label": q.label, "bits": q.bits} for q in kb.QUANTS.values()],
        "default_quant": kb.DEFAULT_QUANT, "default_context": kb.DEFAULT_CONTEXT,
        "prices_note_ru": kb.ROUGH_NOTE_RU,
    }


@router.post("/projects/plan", responses=ERRORS,
             summary="План сборки по цели и бюджету (детали с альтернативами, проверки, итоги) — ничего не сохраняет")
async def projects_plan(body: PlanRequest, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    _check_template(body.template)
    if not body.goal.strip() and not body.template and not body.items:
        raise validation_error({"goal": "Опиши цель или выбери шаблон"}, "Нужна цель")
    plan = await _plan(ctx, body)
    return plan_view(plan, market_of(ctx))


@router.get("/projects", summary="Сборки: карточки (прогресс, лучшая сумма против бюджета, статус)")
async def projects_list(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    store = store_of(ctx)
    market = market_of(ctx)
    counts = store.search_counts()
    alerts = store.alert_counts()
    items = []
    for project in store.list_projects():
        state = project_state(project, store, market)
        items.append(project_card(project, state, searches=counts.get(project.id, 0),
                                  alerts=alerts.get(project.id, (0, None))))
    return {"items": items, "count": len(items)}


@router.post("/projects", status_code=201, responses=ERRORS,
             summary="Сохранить сборку: план из /projects/plan (поле plan, можно с другими choices) или цель")
async def projects_create(body: ProjectCreateIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    _check_template(body.template)
    if body.plan is not None:
        plan = _sanitized_plan(body.plan)
        if body.budget is not None:
            plan.budget = body.budget
        if body.goal.strip():
            plan.goal = body.goal.strip()
    else:
        if not body.goal.strip() and not body.template and not body.items:
            raise validation_error({"goal": "Опиши цель или выбери шаблон"}, "Нужна цель")
        plan = await _plan(ctx, PlanRequest.model_validate(body.model_dump(exclude={"plan", "choices"})))
    if body.name and body.name.strip():
        plan.name = body.name.strip()
    if not plan.name.strip():
        plan.name = "Моя сборка"
    if body.location is not None:
        plan.location, plan.radius_km = body.location.strip(), body.radius_km
    _apply_choices(plan, body.choices, adjust=True)
    project = store_of(ctx).create(plan)
    publish(ctx, project, "created")
    return _view(ctx, project)


@router.get("/projects/{project_id}", responses=ERRORS,
            summary="Сборка: части, варианты, проверки, лучшее предложение и тренд цены по каждой части, итоги")
async def projects_get(project_id: int, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return _view(ctx, _get(ctx, project_id))


@router.patch("/projects/{project_id}", responses=ERRORS,
              summary="Изменить сборку: название, бюджет, выбор вариантов, цели частей, пауза/продолжить")
async def projects_patch(project_id: int, body: ProjectPatchIn, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    project = _get(ctx, project_id)
    was = project.status
    if body.name is not None:
        project.name = " ".join(body.name.split())
    if body.goal is not None:
        project.goal = body.goal.strip()
    if body.clear_budget:
        project.budget = None
    elif body.budget is not None:
        project.budget = body.budget
    if body.location is not None:
        project.location, project.radius_km = body.location.strip(), body.radius_km
    removed = set(body.remove_slots)
    unknown = [k for k in removed if project.slot(k) is None]
    if unknown:
        raise validation_error({f"remove_slots.{k}": "Нет такой части" for k in unknown})
    project.slots = [s for s in project.slots if s.key not in removed]
    if body.add_items:
        extra = custom_slots(body.add_items)
        used = {s.key for s in project.slots}
        for new_slot in extra:
            key, n = new_slot.key, 1
            while key in used:
                n += 1
                key = f"item{len(used) + n}"
            new_slot.key = key
            used.add(key)
        project.slots += extra
    problems: dict[str, str] = {}
    for key, patch in body.slots.items():
        slot = project.slot(key)
        if slot is None:
            problems[f"slots.{key}"] = "Нет такой части"
            continue
        if patch.status is not None:
            slot.status = patch.status
            if patch.status == "open" and slot.purchases:
                slot.purchases = []
        if patch.note is not None:
            slot.note = patch.note.strip()
        opt = slot.option()
        if patch.qty is not None and opt is not None and not slot.per_gpu:
            opt.qty = patch.qty
        if opt is not None and patch.auto_target:
            opt.target_price, opt.max_price, opt.target_by = None, None, "auto"
        elif opt is not None and patch.target_price is not None:
            opt.target_price, opt.target_by = patch.target_price, "user"
            opt.max_price = round_price(patch.target_price * HAGGLE_ROOM + 5)
    if problems:
        raise validation_error(problems)
    _apply_choices(project, body.choices, adjust=body.adjust)
    if body.status == "tracking" and was == "paused":
        project.status = "tracking"
    elif body.status == "paused" and was == "tracking":
        project.status = "paused"
    elif body.status == "tracking" and was == "draft":
        raise ApiError(409, "conflict", "Сначала нажми «Начать отслеживание» — я создам поиски для каждой части")
    store = store_of(ctx)
    affects_searches = bool(body.choices or body.budget is not None or body.clear_budget or body.status
                            or body.slots or body.add_items or body.remove_slots or body.location is not None)
    changed_search = affects_searches and (was in ("tracking", "paused") or project.status == "tracking")
    if changed_search and store.searches(project.id):
        ctx.require_editable()
        if project.status == "paused":
            store.save(project)
            set_searches_enabled(ctx, {link.search_name for link in store.searches(project.id)}, False)
        else:
            sync_searches(ctx, project)
    store.save(project)
    publish(ctx, project, "updated")
    return _view(ctx, project)


@router.delete("/projects/{project_id}", responses=ERRORS,
               summary="Удалить сборку (её поиски тоже удаляются, если не передать keep_searches=true)")
async def projects_delete(project_id: int, keep_searches: bool = Query(False),
                          ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    project = _get(ctx, project_id)
    store = store_of(ctx)
    names = {link.search_name for link in store.searches(project.id)}
    removed: list[str] = []
    warning = ""
    deleting = _deleting(ctx)
    deleting.add(project.id)  # no project_updated for it from here on (see publish)
    try:
        if names and not keep_searches:
            if ctx.config_path is not None and ctx.config_writable():
                removed = [s.name for s in ctx.config.searches if s.name in names]
                if removed:
                    ctx.save_searches([s for s in ctx.config.searches if s.name not in names], reason="project")
            else:
                warning = "Поиски сборки остались: настройки сейчас нельзя менять — удали их в «Поисках»"
        store.delete(project.id)
    finally:
        deleting.discard(project.id)
    ctx.hub.publish("project_deleted", {"id": project.id, "name": project.name, "searches_removed": len(removed)})
    message = f"Сборка «{project.name}» удалена" + (f", поисков убрано: {len(removed)}" if removed else "")
    return {"deleted": project.id, "searches_removed": removed, "message_ru": message, "warning_ru": warning}


@router.post("/projects/{project_id}/track", responses=ERRORS,
             summary="Начать отслеживание: поиски «для себя» на каждую часть и её альтернативы (dry_run — только посчитать)")
async def projects_track(project_id: int, body: TrackIn | None = None,
                         ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    body = body or TrackIn.model_validate({})
    project = _get(ctx, project_id)
    unknown = [k for k in body.slots or [] if project.slot(k) is None]
    if unknown:
        raise validation_error({f"slots.{k}": "Нет такой части" for k in unknown})
    if project.status == "done":
        raise ApiError(409, "conflict", "Всё уже куплено — отслеживать нечего")
    if not any(rs.need_qty > 0 for rs in resolve(project).values()):
        raise ApiError(409, "conflict", "Нечего отслеживать: все части куплены, есть или не нужны")
    if not body.dry_run:
        ctx.require_editable()
    before = project.status
    if not body.dry_run and project.status in ("draft", "paused"):
        project.status = "tracking"
        project.tracking_since = project.tracking_since or utcnow()
    result = sync_searches(ctx, project, body=body, save=not body.dry_run)
    n = len(result["created"]) + len(result["updated"])
    if body.dry_run:
        project.status = before
        created = len(result["created"])
        return {**result, "dry_run": True,
                "message_ru": f"Создам {created} {_plural(created, 'поиск', 'поиска', 'поисков')}"
                              f"{', обновлю ' + str(len(result['updated'])) if result['updated'] else ''}"}
    if not n and not result["tracked"]:
        raise ApiError(409, "conflict", "Нечего отслеживать: все части куплены, есть или не нужны")
    n = result["tracked"] or n
    message = f"Слежу за {n} {_searches_word(n)}"
    if result["created"] and ctx.config.general.baseline_first_run:
        message += ". Первая проверка покажет, что уже продаётся, и запомнит цены"
    publish(ctx, project, "tracking")
    return {**result, "dry_run": False, "message_ru": message, "project": _view(ctx, project)}


def _searches_word(n: int) -> str:
    return _plural(n, "поиском", "поисками", "поисками")


def _plural(n: int, one: str, few: str, many: str) -> str:
    from .project_views import plural

    return plural(n, one, few, many)


@router.post("/projects/{project_id}/slots/{slot_key}/bought", responses=ERRORS,
             summary="Купил часть: цена (и объявление) — слот заполняется, поиски части на паузу, бюджет пересчитан")
async def projects_bought(project_id: int, slot_key: str, body: BoughtIn,
                          ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    project = _get(ctx, project_id)
    slot = project.slot(slot_key)
    if slot is None:
        raise ApiError(404, "not_found", "Такой части в сборке нет")
    if body.option and slot.option(body.option) is None:
        raise validation_error({"option": "Нет такого варианта"})
    rs = resolve(project)[slot_key]
    need = rs.need_qty if slot.status == "open" else 0
    qty = body.qty or max(1, need)
    if body.option and body.option != slot.chosen:
        slot.chosen = body.option
        apply_dependents(project, keep={slot_key}, ideal=False)
        rs = resolve(project)[slot_key]
    found = ctx.db.get_deal_extras(body.ad_id) if body.ad_id else None
    slot.purchases.append(Purchase(price=body.price, qty=qty, ad_id=body.ad_id or None, option=slot.chosen,
                                   note=body.note.strip(), deal_before=_deal_snapshot(found)))
    if sum(p.qty for p in slot.purchases) >= rs.qty:
        slot.status = "bought"
    deal_note = ""
    if body.ad_id and found is not None:
        ctx.db.update_deal_state(body.ad_id, status="bought", bought_price=body.price, bought_at=utcnow())
        _deal_changed(ctx, body.ad_id)
        deal_note = " Сделка отмечена как «Купил» в «Моих сделках»."
    store = store_of(ctx)
    r = resolve(project)
    if all(rr.need_qty == 0 for rr in r.values()):
        project.status = "done"
    paused: list[str] = []
    links = store.searches(project.id)
    warning = ""
    if links:
        if ctx.config_path is not None and ctx.config_writable():
            if project.status == "done":
                store.save(project)
                paused = set_searches_enabled(ctx, {link.search_name for link in links}, False)
            elif project.status == "tracking":
                result = sync_searches(ctx, project)
                paused = result["paused"]
        else:
            warning = "Поиски этой части ещё работают: настройки сейчас нельзя менять"
    store.save(project)
    state = project_state(project, store, market_of(ctx))
    label = option_label(slot.option()) if slot.option() else slot.label
    left = state.totals.get("remaining_budget")
    open_left = sum(1 for rr in state.r.values() if rr.need_qty > 0)
    message = f"Записал: {label} за {_money(body.price)}."
    if project.status == "done":
        message += " Всё собрано!"
    elif left is not None:
        message += f" Осталось {_money(left)} на {open_left} {_parts_word(open_left)}."
    message += deal_note
    publish(ctx, project, "bought", slot=slot_key, state=state)
    return {"project": _view(ctx, project, state), "message_ru": message, "paused_searches": paused,
            "warning_ru": warning}


@router.delete("/projects/{project_id}/slots/{slot_key}/bought", responses=ERRORS,
               summary="Отменить последнюю покупку части (слот снова открыт, поиски снова работают)")
async def projects_unbought(project_id: int, slot_key: str, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    project = _get(ctx, project_id)
    slot = project.slot(slot_key)
    if slot is None:
        raise ApiError(404, "not_found", "Такой части в сборке нет")
    if not slot.purchases:
        raise ApiError(409, "conflict", "Эта часть не отмечена купленной")
    purchase = slot.purchases.pop()
    slot.status = "open"
    if project.status == "done":
        project.status = "tracking" if store_of(ctx).searches(project.id) else "draft"
    deal_status = _restore_deal(ctx, project, purchase)
    store = store_of(ctx)
    if project.status == "tracking" and store.searches(project.id) and ctx.config_path is not None \
            and ctx.config_writable():
        sync_searches(ctx, project)
    store.save(project)
    publish(ctx, project, "updated", slot=slot_key)
    message = "Покупка отменена — снова ищу эту часть."
    if deal_status == "new":
        message += " Сделка больше не в «Купил»."
    elif deal_status and deal_status != "bought":
        message += f" Сделка вернулась в «{STATUS_LABELS.get(deal_status, deal_status)}»."
    return {"project": _view(ctx, project), "message_ru": message, "deal_status": deal_status}


def _deal_snapshot(found: tuple[Any, dict[str, Any]] | None) -> dict[str, Any] | None:
    """The deal's pipeline state before a purchase moves it to «Купил» (undo restores it)."""
    if found is None:
        return None
    deal, extras = found
    bought_at = extras.get("bought_at")
    return {"status": deal.status or "new", "bought_price": extras.get("bought_price"),
            "bought_at": bought_at.isoformat() if bought_at else None}


def _restore_deal(ctx: ApiContext, project: Project, purchase: Purchase) -> str | None:
    """Undo of a purchase made through an ad: the deal leaves «Купил» for the status it had before
    (or «Новое» when that is unknown), so the offer is back in the build. Left alone when the user
    moved the deal since (e.g. to «Продал») or another purchase of the build still uses the ad.
    Returns the deal's status afterwards (None: no deal was touched)."""
    ad_id = purchase.ad_id
    if not ad_id or any(p.ad_id == ad_id for s in project.slots for p in s.purchases):
        return None
    found = ctx.db.get_deal_extras(ad_id)
    if found is None or found[0].status != "bought":
        return None
    before = purchase.deal_before or {}
    status, bought_price, bought_at = "new", None, None
    if before.get("status") in API_STATUSES:
        status, bought_price = before["status"], before.get("bought_price")
        try:
            bought_at = datetime.fromisoformat(before["bought_at"]) if before.get("bought_at") else None
        except (TypeError, ValueError):
            bought_at = None
    ctx.db.update_deal_state(ad_id, status=status, bought_price=bought_price, bought_at=bought_at)
    _deal_changed(ctx, ad_id)
    return status


def _deal_changed(ctx: ApiContext, ad_id: str) -> None:
    from .routes_deals import publish_update

    try:
        publish_update(ctx, ad_id)
    except Exception:  # noqa: BLE001 - the purchase itself is done; the live update is a courtesy
        log.exception("deal_updated after a purchase change failed")


@router.get("/projects/{project_id}/offers", responses=ERRORS,
            summary="Текущие предложения по частям сборки (лучшие сверху) и рейтинг видеокарт по ценности")
async def projects_offers(project_id: int, slot: str | None = Query(None), limit: int = Query(10, ge=1, le=50),
                          ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    project = _get(ctx, project_id)
    if slot is not None and project.slot(slot) is None:
        raise ApiError(404, "not_found", "Такой части в сборке нет")
    state = project_state(project, store_of(ctx), market_of(ctx))
    out = []
    total = 0
    gpu_all = []
    for key, ss in state.slots.items():
        if slot is not None and key != slot:
            continue
        if not ss.rs.active:
            continue
        total += len(ss.offers) + sum(len(v) for v in ss.alternatives.values())
        if ss.rs.slot.kind == "gpu":
            gpu_all += ss.offers + [o for v in ss.alternatives.values() for o in v]
        out.append({
            "slot": key, "label": ss.rs.slot.label, "option": ss.rs.slot.chosen,
            "option_label": option_label(ss.rs.option) if ss.rs.option else "",
            "target_unit": ss.target_unit, "need_qty": ss.need, "status": ss.rs.slot.status,
            "offers": [offer_view(o, ss) for o in ss.offers[:limit]],
            "alternatives": [{"option": k, "label": option_label(ss.rs.slot.option(k)),
                              "offers": [offer_view(o, ss) for o in v[:limit]]}
                             for k, v in ss.alternatives.items() if v and ss.rs.slot.option(k) is not None],
        })
    from ...projects.tracker import rank_gpu_offers

    gpu_slot = state.slots.get("gpu")
    return {"id": project.id, "slots": out, "count": total,
            "gpu_ranking": [offer_view(o, gpu_slot) for o in rank_gpu_offers(gpu_all)[:limit]]}


def _money(value: float | None) -> str:
    from ...notify.render import format_money

    return format_money(value)


def _parts_word(n: int) -> str:
    from .project_views import plural

    return plural(n, "часть", "части", "частей")


# ===================================================================== alerts
def _notifiers(ctx: ApiContext) -> list[Any]:
    override = getattr(ctx, "project_notifiers", None)
    if override is not None:
        return list(override() if callable(override) else override)
    factory = getattr(ctx.app.state, "notifiers_factory", None)
    if factory is not None:
        return list(factory() or [])
    from ...notify.base import build_notifiers

    return build_notifiers(ctx.config.notifications, web_base_url=ctx.web_base_url())


def install_project_alerts(ctx: ApiContext) -> ProjectAlerter:
    """Alerts of build projects. With a monitor: projects.hooks.attach() hooks them straight into
    Monitor.on_event (the same helper the headless CLI modes use; attaching twice only updates the
    wiring) and the hub adds just the API's own deal_updated (e.g. a manual re-check). Without a
    monitor: the hub carries everything."""
    from ...projects import hooks

    def announce(data: dict[str, Any]) -> None:
        project = data.pop("project", None)
        state = data.pop("state", None)
        publish(ctx, project, str(data.pop("reason", "offer")), state=state, **data)

    wiring: dict[str, Any] = {
        "config": lambda: ctx.config, "notifiers": lambda: _notifiers(ctx), "publish": announce,
        "project_url": lambda pid: f"{ctx.web_base_url().rstrip('/')}/projects/{pid}", "market": market_of(ctx),
    }
    monitor = ctx.monitor
    if isinstance(getattr(monitor, "on_event", None), list):
        alerter = hooks.attach(monitor, ctx.db, **wiring)

        def api_events(event: Any) -> None:
            # the monitor's own events reach the alerter directly; its deal_updated carries search_name
            if event.type == "deal_updated" and "search_name" not in (event.data or {}):
                alerter.on_event(event.type, event.data)

        ctx.hub.add_listener(api_events)
    else:
        from ...notify import extras

        alerter = ProjectAlerter(ctx.db, **wiring)
        extras.register(hooks.EXTRAS_NAME, alerter.context_lines)
        ctx.hub.add_listener(lambda event: alerter.on_event(event.type, event.data))
    ctx.project_alerts = alerter  # type: ignore[attr-defined]
    return alerter


__all__ = ["install_project_alerts", "planner_llm_settings", "router", "sync_searches"]
