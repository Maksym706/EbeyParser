"""Tracking a build: the best current offer per slot (from the ads of the project's searches),
totals against the budget and against buying new, price trends, budget-split targets and the
alerts «ниже цели» / «сборка укладывается в бюджет»."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Iterable

from ..models import Comparable, Evaluation, Listing, utcnow
from ..timefmt import local_tz
from . import knowledge as kb
from .knowledge import Part, fmt_int
from .market import KbMarket, Market, PriceInfo, listing_point, option_matches, severe
from .models import AlertRecord, PlanOption, Project
from .planner import RSlot, build_total, resolve
from .scoring import gpu_value_score, offer_flags, value_metric
from .store import ProjectStore

log = logging.getLogger(__name__)

SEEN_WITHIN_DAYS = 7  # offers not seen on the result pages for longer are probably sold
STALE_AFTER_DAYS = 2
AIM_BELOW_MARKET = 0.9  # a target is at least 10 % under the market (the app hunts bargains)
# ads up to 30 % above the target are still evaluated (VB: haggle) — the same cut the monitor's
# prefilter applies to personal searches ("Слишком дорого" above 1.3 × target_price)
HAGGLE_ROOM = 1.3
SEND_TIMEOUT = 60.0
STRETCH_OK = 0.8  # from here on (budget / market total) the build is "реально, если торговаться"
_BAD_AI_ITEMS = frozenset({"wanted", "box_only", "accessory", "complete_pc", "laptop"})
_PREFILTER_BAD = ("Стоп-слова", "Нет ключевых слов", "Это объявление о поиске")


def round_price(value: float) -> float:
    """Targets people would say: 5 € steps under 100 €, 10 € above."""
    if value <= 0:
        return 0.0
    step = 5 if value < 100 else 10
    return float(max(step, step * math.floor(value / step)))


# ====================================================================== offers
@dataclass
class Offer:
    ad_id: str
    slot: str
    option: str
    listing: Listing
    evaluation: Evaluation | None
    status: str
    search_name: str
    first_seen: datetime
    last_seen: datetime
    unit_cost: float
    part: Part | None
    flags: list[dict[str, str]] = field(default_factory=list)
    value: dict[str, Any] | None = None
    value_score: float | None = None

    @property
    def price(self) -> float:
        return float(self.listing.price or 0)

    @property
    def days_since_seen(self) -> float:
        return max(0.0, (utcnow() - _aware(self.last_seen)).total_seconds() / 86400)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _usable(listing: Listing, ev: Evaluation | None) -> bool:
    if ev is not None:
        if ev.stage == "prefilter" and any(r.startswith(_PREFILTER_BAD) for r in ev.reasons):
            return False
        if severe(listing.title, listing.description, ev.red_flags):
            return False
        for ai in (ev.ai, ev.ai_second):
            if ai is not None and ai.item_type in _BAD_AI_ITEMS:
                return False
        return True
    return not severe(listing.title, listing.description)


def collect_offers(project: Project, store: ProjectStore, *, days: int = SEEN_WITHIN_DAYS,
                   exclude_ad: str | None = None) -> dict[tuple[str, str], list[Offer]]:
    """Current offers per (slot, option), cheapest first, from the listings of the linked searches."""
    links = store.searches(project.id)
    by_name = {link.search_name: link for link in links}
    purchased = {p.ad_id for s in project.slots for p in s.purchases if p.ad_id}
    out: dict[tuple[str, str], list[Offer]] = {}
    rows = store.offer_rows(by_name, utcnow() - timedelta(days=days))
    for row in rows:
        ad_id = row["ad_id"]
        if ad_id == exclude_ad or ad_id in purchased:
            continue
        status = row["status"] or "new"
        if status in ("ignored", "bought", "sold"):
            continue
        link = by_name.get(row["search_name"])
        slot = project.slot(link.slot) if link else None
        opt = slot.option(link.option.split("@", 1)[0]) if slot and link else None  # "rtx3090@ebay": eBay search
        if slot is None or opt is None:
            continue
        try:
            listing = Listing.model_validate_json(row["l_data"])
            ev = Evaluation.model_validate_json(row["e_data"]) if row["e_data"] else None
        except ValueError:
            continue
        part = kb.part(opt.kb_key)
        if not option_matches(opt, part, listing.title, listing.description) or not _usable(listing, ev):
            continue
        unit = float(listing.price or 0) + float(listing.shipping_cost or 0)
        if unit <= 0:
            continue
        flags = offer_flags(part, listing.title, listing.description)
        offer = Offer(ad_id=ad_id, slot=slot.key, option=opt.key, listing=listing, evaluation=ev, status=status,
                      search_name=row["search_name"], first_seen=datetime.fromisoformat(row["first_seen"]),
                      last_seen=datetime.fromisoformat(row["last_seen"]), unit_cost=unit, part=part, flags=flags,
                      value=value_metric(part, unit), value_score=gpu_value_score(part, unit, flags))
        out.setdefault((slot.key, opt.key), []).append(offer)
    for offers in out.values():
        offers.sort(key=lambda o: (o.unit_cost, -_aware(o.last_seen).timestamp()))
    return out


def rank_gpu_offers(offers: Iterable[Offer]) -> list[Offer]:
    """GPU offers of every tracked alternative, best value for an LLM server first (€/GB VRAM,
    bandwidth per €, minus risk flags)."""
    return sorted((o for o in offers if o.value_score is not None), key=lambda o: (-(o.value_score or 0), o.unit_cost))


# ====================================================================== state
@dataclass
class SlotState:
    rs: RSlot
    price: PriceInfo
    target_unit: float | None
    max_unit: float | None
    offers: list[Offer]
    alternatives: dict[str, list[Offer]]
    spent: float
    best_cost: float | None
    best_complete: bool

    @property
    def need(self) -> int:
        return self.rs.need_qty

    @property
    def picked(self) -> list[Offer]:
        return self.offers[: self.need] if self.need else []


@dataclass
class ProjectState:
    project: Project
    r: dict[str, RSlot]
    slots: dict[str, SlotState]
    ratio: float | None
    totals: dict[str, Any]


def spent_total(project: Project, r: dict[str, RSlot]) -> float:
    return sum(p.price for s in project.slots for p in s.purchases if r.get(s.key) and r[s.key].active)


def budget_ratio(project: Project, r: dict[str, RSlot], market: Any) -> float | None:
    """How far under the market every open slot must be bought to meet the budget (1.0 = at market)."""
    if not project.budget:
        return None
    remaining = project.budget - spent_total(project, r)
    auto = 0.0
    for rs in r.values():
        need = rs.need_qty
        if need <= 0 or rs.option is None:
            continue
        if rs.option.target_by == "user" and rs.option.target_price:
            remaining -= rs.option.target_price * need
            continue
        typical = market.price(rs.part, rs.option).typical
        if typical:
            auto += typical * need
    if auto <= 0:
        return None
    return remaining / auto


def option_target(opt: PlanOption, part: Part | None, market: Any, ratio: float | None) -> tuple[float | None, float | None]:
    """(target, max) per unit: the user's / frozen values, else market × min(0.9, budget ratio)."""
    if opt.target_price:
        return opt.target_price, opt.max_price or round_price(opt.target_price * HAGGLE_ROOM + 5)
    typical = market.price(part, opt).typical
    if not typical:
        return None, None
    factor = AIM_BELOW_MARKET if ratio is None else min(AIM_BELOW_MARKET, ratio)
    target = round_price(max(typical * factor, 1.0))
    return target, round_price(target * HAGGLE_ROOM + 5)


def market_points(opt: PlanOption, part: Part | None, offers: list[Offer]) -> list[tuple[Comparable, datetime, datetime]]:
    """What the project's own ads say about the market: the offers themselves and the comparables
    their evaluations looked up (eBay sold prices, similar ads) that are the same part."""
    out = [listing_point(o.listing, o.first_seen, o.last_seen) for o in offers]
    seen = {o.listing.url for o in offers}
    for o in offers:
        if o.evaluation is None:
            continue
        when = o.evaluation.evaluated_at
        for comp in o.evaluation.estimate.comparables:
            ident = comp.url or f"{comp.title}|{comp.price}"
            if ident in seen or not comp.price or comp.price <= 0:
                continue
            if option_matches(opt, part, comp.title):
                seen.add(ident)
                out.append((comp, when, when))
    return out


def project_state(project: Project, store: ProjectStore | None, market: Any = None, *,
                  exclude_ad: str | None = None, offers: dict[tuple[str, str], list[Offer]] | None = None) -> ProjectState:
    market = market or KbMarket()
    r = resolve(project)
    if offers is None:
        offers = collect_offers(project, store, exclude_ad=exclude_ad) if store is not None and project.id else {}
    ratio = budget_ratio(project, r, market)
    slots: dict[str, SlotState] = {}
    for key, rs in r.items():
        opt = rs.option
        chosen = offers.get((key, opt.key), []) if opt else []
        extra = market_points(opt, rs.part, chosen) if opt else []
        price = market.price(rs.part, opt, extra) if opt else PriceInfo(None, None, None, "none", "", True)
        target, top = option_target(opt, rs.part, market, ratio) if opt else (None, None)
        alts = {o.key: offers.get((key, o.key), []) for o in rs.slot.options if o.key != (opt.key if opt else None)}
        need = rs.need_qty
        best_cost: float | None = None
        complete = need == 0
        if need > 0 and chosen:
            picked = chosen[:need]
            best_cost = sum(o.unit_cost for o in picked)
            complete = len(picked) >= need
        slots[key] = SlotState(rs, price, target, top, chosen, alts, sum(p.price for p in rs.slot.purchases),
                               best_cost, complete)
    state = ProjectState(project, r, slots, ratio, {})
    state.totals = totals(state, market)
    return state


def totals(state: ProjectState, market: Any) -> dict[str, Any]:
    project, r = state.project, state.r
    budget = project.budget
    spent = spent_total(project, r)
    typical_total, unknown = build_total(project, r, market)
    best = spent
    estimated = spent
    complete = True
    missing: list[str] = []
    target_total = spent
    new_total = 0.0
    new_missing: list[str] = []
    done = total_slots = 0
    for ss in state.slots.values():
        rs = ss.rs
        if not rs.active or rs.slot.status == "skipped":
            continue
        total_slots += 1
        if rs.slot.status in ("bought", "have") or (rs.slot.purchases and ss.need == 0):
            done += 1
        need = ss.need
        typical = ss.price.typical
        if need > 0:
            if ss.best_complete and ss.best_cost is not None:
                best += ss.best_cost
                estimated += ss.best_cost
            else:
                complete = False
                missing.append(rs.slot.label)
                have = ss.best_cost or 0.0
                rest = need - len(ss.picked)
                estimated += have + (typical or 0.0) * rest
                best += have
            if ss.target_unit:
                target_total += ss.target_unit * need
            elif typical:
                target_total += typical * need
        if rs.slot.status != "have" and rs.part is not None:
            if rs.part.price.new is not None:
                new_total += rs.part.price.new * rs.qty
            else:
                new_missing.append(rs.slot.label)
    fits = None if not budget or not complete else best <= budget  # None: not every part has an offer yet
    fits_estimate = None if not budget else estimated <= budget
    return {
        "budget": budget, "spent": round(spent, 2), "remaining_budget": round(budget - spent, 2) if budget else None,
        "typical_total": round(typical_total, 2), "unknown_price_slots": unknown,
        "target_total": round(target_total, 2),
        "best_total": round(best, 2) if complete else None, "best_complete": complete,
        "estimated_total": round(estimated, 2), "missing_offers": missing,
        "fits_budget": fits, "fits_estimate": fits_estimate,
        "over_budget": round(estimated - budget, 2) if budget else None,
        "new_total": round(new_total, 2) if new_total else None, "new_missing": new_missing,
        "savings_vs_new": round(new_total - estimated, 2) if new_total and not new_missing else None,
        "slots_total": total_slots, "slots_done": done, "ratio": round(state.ratio, 3) if state.ratio else None,
    }


def stretch_label(ratio: float | None, over: float | None = None) -> str:
    """What the budget means for every purchase, in plain Russian."""
    if ratio is None:
        return ""
    if ratio >= 1.0:
        return "Бюджета хватает: цель — брать каждую часть примерно на 10 % дешевле рынка"
    pct = round((1 - ratio) * 100)
    if ratio >= 0.9:
        return f"Бюджет по рынку: каждую часть нужно брать примерно на {pct} % дешевле рынка — обычная находка"
    if ratio >= STRETCH_OK:
        return f"Реально, если торговаться: каждую часть нужно брать примерно на {pct} % дешевле рынка"
    if ratio >= 0.65:
        return f"Очень жёстко: нужно брать на {pct} % дешевле рынка — такие находки редки"
    short = f" — не хватает ~{fmt_int(over)} €" if over and over > 0 else ""
    return f"Нереально с этим вариантом{short}: выбери вариант дешевле или увеличь бюджет"



# ======================================================================= trend
def price_trend(points: Iterable[tuple[Any, datetime, datetime]], *, now: datetime | None = None) -> dict[str, Any]:
    """Weekly medians of the prices seen (asking prices) and the change of the last 2 weeks vs
    the 4 weeks before."""
    now = now or utcnow()
    tz = local_tz()
    by_week: dict[str, list[float]] = {}
    recent: list[float] = []
    before: list[float] = []
    for comp, first, _last in points:
        price = float(getattr(comp, "price", 0) or 0)
        if price <= 0:
            continue
        seen = _aware(first)
        day = seen.astimezone(tz).date()
        week = (day - timedelta(days=day.weekday())).isoformat()
        by_week.setdefault(week, []).append(price)
        age = (now - seen).days
        if age <= 14:
            recent.append(price)
        elif age <= 42:
            before.append(price)
    series = [{"date": w, "median": round(median(v), 2), "n": len(v)} for w, v in sorted(by_week.items())]
    change = None
    direction = "unknown"
    label = "Мало данных для тренда"
    if len(recent) >= 3 and len(before) >= 3:
        change = (median(recent) - median(before)) / median(before) * 100
        if change <= -3:
            direction, label = "down", f"Дешевеет: {round(change)} % за 2 недели"
        elif change >= 3:
            direction, label = "up", f"Дорожает: +{round(change)} % за 2 недели"
        else:
            direction, label = "flat", "Цена стабильна"
    return {"points": series, "change_pct": round(change, 1) if change is not None else None,
            "direction": direction, "label_ru": label}


# ====================================================================== alerts
@dataclass
class AlertDraft:
    kind: str  # slot_target | budget_fit
    slot: str
    offer: Offer
    target: float | None = None
    alternative: bool = False  # an offer for a tracked alternative (e.g. a steal on an RTX 3090)


def alert_candidates(state: ProjectState, alerted: set[tuple[str, str]], *, only_ad: str | None = None) -> list[AlertDraft]:
    """New offers that beat their slot's target (also for a tracked alternative with its own target),
    and the offer that made the whole build fit the budget."""
    project = state.project
    if project.status != "tracking":
        return []
    drafts: list[AlertDraft] = []

    def wanted(offer: Offer, target: float | None) -> bool:
        return (target is not None and offer.unit_cost <= target and (only_ad is None or offer.ad_id == only_ad)
                and (offer.ad_id, "slot_target") not in alerted)

    for key, ss in state.slots.items():
        if ss.need <= 0:
            continue
        for offer in ss.picked:
            if ss.target_unit and wanted(offer, ss.target_unit):
                drafts.append(AlertDraft("slot_target", key, offer, ss.target_unit))
        for opt_key, offers in ss.alternatives.items():
            opt = ss.rs.slot.option(opt_key)
            target = opt.target_price if opt is not None else None  # frozen when tracking started
            if offers and wanted(offers[0], target):
                drafts.append(AlertDraft("slot_target", key, offers[0], target, alternative=True))
    t = state.totals
    if project.budget and t["fits_budget"] and not project.fits_alerted:
        picked = [o for ss in state.slots.values() for o in ss.picked]
        trigger = next((o for o in picked if o.ad_id == only_ad), None) if only_ad else None
        if trigger is None and picked and only_ad is None:
            trigger = max(picked, key=lambda o: _aware(o.first_seen))
        if trigger is not None and (trigger.ad_id, "budget_fit") not in alerted:
            drafts.append(AlertDraft("budget_fit", trigger.slot, trigger))
    return drafts


def _money(value: float | None) -> str:
    from ..notify.render import format_money

    return format_money(value)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def offer_title(offer: Offer) -> str:
    if offer.part is not None:
        return offer.part.label
    return " ".join(offer.listing.title.split())[:60]


def alert_text(state: ProjectState, drafts: list[AlertDraft], *, project_url: str | None = None) -> str:
    project = state.project
    t = state.totals
    lines: list[str] = []
    slot_drafts = [d for d in drafts if d.kind == "slot_target"]
    fit = next((d for d in drafts if d.kind == "budget_fit"), None)
    if len(slot_drafts) > 1:
        n = len(slot_drafts)
        lines.append(f"🧩 Сборка «{project.name}»: {n} {_plural(n, 'предложение', 'предложения', 'предложений')} "
                     "ниже цели")
        for d in slot_drafts[:6]:
            alt = " (вариант)" if d.alternative else ""
            lines.append(f"• {offer_title(d.offer)}{alt} за {_money(d.offer.unit_cost)} (цель {_money(d.target)}) — "
                         f"{d.offer.listing.url}")
    elif slot_drafts:
        d = slot_drafts[0]
        ss = state.slots[d.slot]
        o = d.offer
        if d.alternative:
            chosen = ss.rs.part.label if ss.rs.part else ss.rs.slot.label
            lines.append(f"🧩 Сборка «{project.name}»: вместо {chosen} — {offer_title(o)} за {_money(o.unit_cost)}")
        else:
            lines.append(f"🧩 Сборка «{project.name}»: {offer_title(o)} за {_money(o.unit_cost)}")
        why = f"Ниже твоей цели {_money(d.target)}"
        market = ss.price.typical if not d.alternative else None
        if market and market > o.unit_cost:
            why += f" · рынок ~{_money(market)} (экономия ~{_money(market - o.unit_cost)})"
        lines.append(why)
        if o.value:
            lines.append(o.value["text_ru"])
        warns = [f["text_ru"] for f in o.flags if f["level"] == "warn"][:2]
        lines += [f"⚠ {w}" for w in warns]
        if o.listing.distance_km is not None:
            lines.append(f"{round(o.listing.distance_km)} км от тебя" + (f" · {o.listing.location}" if o.listing.location else ""))
        lines.append(o.listing.url)
    if fit is not None:
        if not slot_drafts:
            lines.append(f"🧩 Сборка «{project.name}» укладывается в бюджет")
            lines.append(f"Новое предложение: {offer_title(fit.offer)} за {_money(fit.offer.unit_cost)} — "
                         f"{fit.offer.listing.url}")
        lines.append(f"Вся сборка сейчас: {_money(t['best_total'])} из {_money(project.budget)} — укладываешься в бюджет")
    elif project.budget:
        lines.append(f"Вся сборка сейчас: ~{_money(t['estimated_total'])} из {_money(project.budget)}"
                     + ("" if t["best_complete"] else " (для части деталей — по рынку)"))
    lines.append(f"Собрано {t['slots_done']} из {t['slots_total']}")
    if project_url:
        lines.append(f"Сборка: {project_url}")
    return "\n".join(lines)


def toast_text(state: ProjectState, drafts: list[AlertDraft]) -> str:
    project = state.project
    first = drafts[0]
    if first.kind == "budget_fit" and len(drafts) == 1:
        return f"Сборка «{project.name}» укладывается в бюджет: {_money(state.totals['best_total'])}"
    return f"Сборка «{project.name}»: {offer_title(first.offer)} за {_money(first.offer.unit_cost)} — ниже цели"


Publish = Callable[[dict[str, Any]], Any]


class ProjectAlerter:
    """Watches deals of the project searches (deal_found / deal_updated) and every finished pass
    (run_finished): sends «ниже цели» / «укладывается в бюджет» once per ad through the normal
    notification channels and announces project_updated."""

    def __init__(self, db: Any, *, config: Callable[[], Any], notifiers: Callable[[], list[Any]],
                 publish: Publish | None = None, project_url: Callable[[int], str | None] | None = None,
                 market: Market | None = None) -> None:
        self.store = ProjectStore(db)
        self.db = db
        self._config = config
        self._notifiers = notifiers
        self._publish = publish
        self._project_url = project_url
        self.market = market or Market(db, config())
        self._tasks: set[asyncio.Task[Any]] = set()
        self._locks: dict[int, asyncio.Lock] = {}  # one per event loop (an asyncio.Lock binds to its loop)
        self._signatures: dict[int, str] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sent: list[str] = []  # texts sent (tests / diagnostics)

    # ------------------------------------------------------------- events
    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        """Sync hook (EventHub listener / Monitor.on_event): schedules the async work."""
        if kind not in ("deal_found", "deal_updated", "run_finished"):
            return
        coro = self.handle(kind, dict(data or {}))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self.loop = loop
            task = loop.create_task(coro)
            self._tasks.add(task)  # keep a reference until it is done
            task.add_done_callback(self._tasks.discard)
            return
        if self.loop is not None and not self.loop.is_closed() and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.loop)
            return
        try:  # no event loop known (a script / a thread of its own): do it right here
            asyncio.run(coro)
        except Exception:  # noqa: BLE001
            log.exception("project alerts failed (%s)", kind)

    async def handle(self, kind: str, data: dict[str, Any]) -> None:
        try:
            if kind == "run_finished":
                await self.sweep()
            elif data.get("ad_id"):
                await self.check_ad(str(data["ad_id"]))
        except Exception:  # noqa: BLE001 - alerts must never break monitoring
            log.exception("project alerts failed (%s)", kind)

    # ------------------------------------------------------------- checks
    def _projects_for_ad(self, ad_id: str) -> list[Project]:
        search = self.store.listing_search(ad_id)
        if not search:
            return []
        out = []
        for pid in self.store.projects_for_search(search):
            project = self.store.get(pid)
            if project is not None and project.status == "tracking":
                out.append(project)
        return out

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(id(loop))
        if lock is None:
            lock = self._locks[id(loop)] = asyncio.Lock()
        return lock

    async def check_ad(self, ad_id: str) -> int:
        """A new / changed offer: alert at once when it beats its target or makes the build fit."""
        sent = 0
        async with self._lock():
            for project in self._projects_for_ad(ad_id):
                state = project_state(project, self.store, self.market)
                sent += await self._alert(state, only_ad=ad_id)
                self._announce(project, "offer", state, ad_id=ad_id)
        return sent

    async def sweep(self) -> int:
        """After a pass: every tracking project — alerts not sent yet, live update when offers changed."""
        sent = 0
        async with self._lock():
            for project in self.store.list_projects(status="tracking"):
                state = project_state(project, self.store, self.market)
                sent += await self._alert(state)
                sig = "|".join(f"{k}:{','.join(o.ad_id for o in s.picked)}" for k, s in sorted(state.slots.items()))
                if self._signatures.get(project.id) != sig:
                    self._signatures[project.id] = sig
                    self._announce(project, "run", state)
        return sent

    async def _alert(self, state: ProjectState, *, only_ad: str | None = None) -> int:
        project = state.project
        if project.budget and state.totals["fits_budget"] is False and project.fits_alerted and only_ad is None:
            project.fits_alerted = False  # it no longer fits: the next fit is news again
            self.store.save(project)
        drafts = alert_candidates(state, self.store.alerted(project.id), only_ad=only_ad)
        if not drafts:
            return 0
        url = self._project_url(project.id) if self._project_url else None
        text = alert_text(state, drafts, project_url=url)
        claimed = []
        for d in drafts:
            record = AlertRecord(project_id=project.id, ad_id=d.offer.ad_id, kind=d.kind, slot=d.slot,  # type: ignore[arg-type]
                                 price=d.offer.unit_cost, total=state.totals.get("best_total"), text=text)
            if self.store.claim_alert(record):
                claimed.append(record)
        if not claimed:
            return 0
        delivered, channels = await self._send(text)
        for record in claimed:
            if channels and not delivered:
                self.store.drop_alert(project.id, record.ad_id, record.kind)  # retried after the next pass
            else:
                record.delivered = delivered > 0
                self.store.set_alert(record)
        if any(d.kind == "budget_fit" for d in drafts) and (delivered or not channels):
            project.fits_alerted = True
            self.store.save(project)
        self.sent.append(text)
        self._announce(project, "alert", state, alert={"kinds": sorted({d.kind for d in drafts}),
                                                       "text_ru": toast_text(state, drafts),
                                                       "delivered": delivered > 0,
                                                       "ad_id": drafts[0].offer.ad_id})
        return len(claimed)

    async def _send(self, text: str) -> tuple[int, int]:
        try:
            notifiers = list(self._notifiers() or [])
        except Exception:  # noqa: BLE001
            log.exception("notifiers for project alerts")
            notifiers = []
        delivered = 0
        for notifier in notifiers:
            send = getattr(notifier, "send_text", None)
            if send is None:
                continue
            try:
                result = send(text)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, SEND_TIMEOUT)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("project alert via %s failed: %s", getattr(notifier, "name", "?"), exc)
        return delivered, len(notifiers)

    def _announce(self, project: Project, reason: str, state: ProjectState | None = None, **extra: Any) -> None:
        if self._publish is None:
            return
        try:
            self._publish({"id": project.id, "reason": reason, "project": project, "state": state, **extra})
        except Exception:  # noqa: BLE001
            log.exception("project_updated publish failed")


__all__ = [
    "AlertDraft", "Offer", "ProjectAlerter", "ProjectState", "SlotState", "alert_candidates", "alert_text",
    "budget_ratio", "collect_offers", "option_target", "price_trend", "project_state", "rank_gpu_offers",
    "round_price", "spent_total", "stretch_label", "toast_text", "totals",
]
