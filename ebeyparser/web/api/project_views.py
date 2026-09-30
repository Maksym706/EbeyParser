"""JSON of «Сборки» (build projects) for /api/v1/projects: plain numbers (EUR), ISO times plus
ready Russian *_label / *_ru texts, like the rest of the API."""

from __future__ import annotations

import re
from typing import Any

from ...projects import knowledge as kb
from ...projects.knowledge import Part, fmt_gb, fmt_int
from ...projects.market import PriceInfo, listing_point
from ...projects.models import (
    OPTION_SOURCE_LABELS,
    PROJECT_STATUS_LABELS,
    SLOT_STATUS_LABELS,
    AlertRecord,
    Plan,
    PlanOption,
    Project,
    SearchLink,
)
from ...projects.planner import (
    RSlot,
    compat_checks,
    gpu_candidates,
    gpu_cards,
    option_label,
    vram_status,
)
from ...projects.planner import STATUS_LABELS as CHECK_STATUS_LABELS
from ...projects.scoring import gpu_value_score, value_label, value_metric
from ...projects.tracker import (
    STALE_AFTER_DAYS,
    Offer,
    ProjectState,
    SlotState,
    option_target,
    price_trend,
    project_state,
    rank_gpu_offers,
    stretch_label,
)
from ...timefmt import when_label
from .presenters import (
    ACTION_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    VERDICT_LABELS,
    derive_action,
    iso,
    market_price,
    rnd,
)

ALERT_KIND_LABELS = {"slot_target": "Ниже цели", "budget_fit": "Сборка укладывается в бюджет"}
TREND_LIMIT_DAYS = 60


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def euro(value: float | None) -> str:
    from ...notify.render import format_money

    return format_money(value)


# ================================================================== templates
def template_view(t: kb.Template) -> dict[str, Any]:
    return {"key": t.key, "title": t.title, "subtitle": t.subtitle, "icon": t.icon, "kind": t.kind,
            "default_budget": t.budget, "vram_gb": t.vram_gb, "example_goal": t.example_goal,
            "slots": [{"key": s.key, "label": s.label, "optional": bool(s.when)} for s in t.slots]}


def templates_payload() -> list[dict[str, Any]]:
    return [template_view(kb.TEMPLATES[k]) for k in kb.TEMPLATE_ORDER]


def template_label(plan: Plan) -> str:
    t = kb.TEMPLATES.get(plan.template)
    if t is None:
        return "Своя сборка"
    req = plan.requirements
    if t.kind == "llm" and req.model_size_b:
        return f"LLM-сервер для {fmt_gb(req.model_size_b)}B {kb.quant(req.quant).label}"
    if t.kind == "llm" and req.vram_gb and req.vram_gb != t.vram_gb:
        return f"LLM-сервер {fmt_gb(req.vram_gb)} ГБ VRAM"
    return t.title


# ===================================================================== parts
def specs_ru(part: Part | None) -> list[str]:
    if part is None:
        return []
    s = part.specs
    if part.kind == "gpu":
        out = [f"{s['vram_gb']} ГБ {s.get('memory') or ''}".strip(), f"{fmt_int(s['bandwidth_gbs'])} ГБ/с",
               f"{s['tdp_w']} Вт", f"{s['slot_width']} {plural(s['slot_width'], 'слот', 'слота', 'слотов')}",
               s.get("pcie") or ""]
        if s.get("cooling") == "passive":
            out.append("пассивное охлаждение")
        if s.get("display") is False:
            out.append("без видеовыхода")
        elif s.get("display") is None:
            out.append("видеовыход не гарантирован")
        return [x for x in out if x]
    if part.kind == "cpu":
        out = [f"{s['cores']} {plural(s['cores'], 'ядро', 'ядра', 'ядер')} / {s['threads']} потоков", s["socket"],
               f"{s['tdp_w']} Вт"]
        out.append("встроенная графика" if s.get("igpu") else "без встроенной графики")
        return out
    if part.kind == "board":
        out = [s["socket"] if not s.get("cpu_included") else "процессор на плате"]
        if s.get("ram_types"):
            out.append("/".join(t.upper() for t in s["ram_types"]) + (" (+ ECC REG)" if s.get("rdimm") else ""))
        if s.get("x16_slots"):
            out.append(f"{s['x16_slots']} {plural(s['x16_slots'], 'длинный слот', 'длинных слота', 'длинных слотов')}")
        if s.get("sata_ports"):
            out.append(f"{s['sata_ports']} SATA")
        return out
    if part.kind == "ram":
        return [s["gen"].upper(), f"{s['size_gb']} ГБ", *(["ECC REG"] if s["registered"] else []),
                *(["SO-DIMM"] if s["sodimm"] else [])]
    if part.kind == "psu":
        return [f"{s['watts']} Вт"]
    if part.kind == "storage":
        kind = {"nvme": "NVMe", "ssd": "SATA SSD", "hdd": "HDD"}.get(s["type"], s["type"])
        size = f"{fmt_gb(s['size_tb'] * 1000)} ГБ" if s["size_tb"] < 1 else f"{fmt_gb(s['size_tb'])} ТБ"
        return [kind, size]
    if part.kind == "case" and s.get("expansion_slots"):
        return [f"{s['expansion_slots']} слотов расширения"]
    return []


def _json_specs(part: Part | None) -> dict[str, Any]:
    if part is None:
        return {}
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in part.specs.items()}


def price_view(info: PriceInfo, qty: int = 1) -> dict[str, Any]:
    data = info.as_dict()
    data["qty"] = qty
    data["total"] = rnd(info.typical * qty) if info.typical else None
    if info.typical and info.low and info.high:
        data["range_label"] = f"{euro(info.low)}–{euro(info.high)}"
    return data


def option_view(plan: Plan, slot_key: str, opt: PlanOption, market: Any, ratio: float | None, *,
                candidates: dict[str, Any] | None = None) -> dict[str, Any]:
    part = kb.part(opt.kb_key)
    slot = plan.slot(slot_key)
    info = market.price(part, opt)
    cand = (candidates or {}).get(opt.key)
    if cand is not None and plan.budget and cand.total and not (slot and slot.chosen == opt.key):
        ratio = plan.budget / cand.total  # an alternative is judged as if it were chosen
    target, top = option_target(opt, part, market, ratio)
    typical = info.typical
    view: dict[str, Any] = {
        "key": opt.key, "kb_key": opt.kb_key, "label": option_label(opt) if part else (opt.label or opt.query),
        "unit_label": part.label if part else (opt.label or opt.query), "qty": opt.qty, "kind": part.kind if part else "generic",
        "chosen": bool(slot and slot.chosen == opt.key), "source": opt.source,
        "source_label": OPTION_SOURCE_LABELS.get(opt.source, opt.source), "query": part.query if part else opt.query,
        "specs": _json_specs(part), "specs_ru": specs_ru(part), "pros": list(part.pros) if part else [],
        "caveats": list(part.caveats) if part else [], "where_ru": part.where_ru if part else "", "why": opt.why,
        "price": price_view(info, opt.qty), "target_unit": rnd(target), "max_unit": rnd(top),
        "target_by": opt.target_by if opt.target_price else "auto",
        "new_price": rnd(part.price.new) if part else None, "new_label": part.price.new_label if part else "",
        "value": value_metric(part, typical), "risk": part.risk if part else None,
    }
    if part is not None and part.kind == "gpu":
        score = gpu_value_score(part, typical)
        view["value_score"] = score
        view["value_label"] = value_label(score)
        vram = part.specs["vram_gb"] * opt.qty
        view["vram_gb"] = vram
        req = plan.requirements
        if req.kind == "llm":
            if req.model_size_b:
                mem = kb.llm_memory(req.model_size_b, req.quant, req.context, opt.qty, active_b=req.active_b)
                status = vram_status(vram, mem.total_gib)
                speed = kb.speed_estimate(mem.active_weights_gib, [(part, opt.qty)])
            else:
                status = "ok" if vram >= (req.vram_gb or 24) else "fail"
                speed = kb.speed_estimate(0.8 * vram, [(part, opt.qty)])
            view["vram_status"] = status
            view["vram_status_label"] = CHECK_STATUS_LABELS.get(status, status)
            view["speed"] = speed
        if cand is not None:
            view["build_total"] = rnd(cand.total)
            view["fits_budget"] = (cand.total <= plan.budget) if plan.budget else None
            view["reachable"] = (cand.total * 0.85 <= plan.budget) if plan.budget else None
    return view


# ==================================================================== offers
def offer_view(offer: Offer, ss: SlotState | None = None) -> dict[str, Any]:
    listing, ev = offer.listing, offer.evaluation
    verdict = ev.verdict if ev else "none"
    action = derive_action(listing, ev)
    target = ss.target_unit if ss else None
    market = market_price(ev) if ev else None
    if market is None and ss is not None:
        market = ss.price.typical
    images = [u for u in listing.image_urls if u]
    vs = rnd(offer.unit_cost - target) if target else None
    if vs is None:
        vs_label = ""
    elif vs <= 0:
        vs_label = f"на {euro(-vs)} ниже цели" if vs < 0 else "ровно по цели"
    else:
        vs_label = f"на {euro(vs)} дороже цели"
    return {
        "ad_id": offer.ad_id, "title": listing.title, "url": listing.url, "image": images[0] if images else None,
        "source": listing.source, "source_label": SOURCE_LABELS.get(listing.source, listing.source),
        "price": rnd(listing.price), "price_text": listing.price_text, "shipping_cost": rnd(listing.shipping_cost),
        "unit_cost": rnd(offer.unit_cost), "negotiable": listing.negotiable,
        "distance_km": rnd(listing.distance_km, 1), "location": listing.location,
        "first_seen": iso(offer.first_seen), "first_seen_label": when_label(offer.first_seen),
        "last_seen": iso(offer.last_seen), "last_seen_label": when_label(offer.last_seen),
        "stale": offer.days_since_seen > STALE_AFTER_DAYS,
        "stale_ru": "давно не видел на сайте — может быть уже продано" if offer.days_since_seen > STALE_AFTER_DAYS else "",
        "verdict": verdict, "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        "action": action, "action_label": ACTION_LABELS.get(action, action),
        "score": round(ev.score) if ev else None, "market_price": rnd(market),
        "savings": rnd(market - offer.unit_cost) if market else None,
        "status": offer.status, "status_label": STATUS_LABELS.get(offer.status, offer.status),
        "under_target": bool(target and offer.unit_cost <= target), "vs_target": vs, "vs_target_label": vs_label,
        "flags": offer.flags, "value": offer.value, "value_score": offer.value_score,
        "value_label": value_label(offer.value_score), "option": offer.option, "slot": offer.slot,
        "deal_path": f"/deal/{offer.ad_id}", "search_name": offer.search_name,
    }


# ===================================================================== checks
def budget_check(plan: Plan, totals: dict[str, Any], ratio: float | None, *, candidates: Any = None) -> dict[str, Any] | None:
    budget = plan.budget
    if not budget:
        return None
    estimate = totals.get("estimated_total") or totals.get("typical_total") or 0.0
    over = estimate - budget
    lines = [f"Сейчас по рынку и найденным предложениям: ~{euro(estimate)} при бюджете {euro(budget)}"]
    label = stretch_label(ratio, over)
    if label:
        lines.append(label)
    if totals.get("unknown_price_slots"):
        lines.append("Для части вещей цены пока нет — узнаю из объявлений")
    fix = None
    if over <= 0:
        status, summary = "ok", f"Укладываешься: ~{euro(estimate)} из {euro(budget)} (запас {euro(-over)})"
    elif ratio is not None and ratio >= 0.85:
        status, summary = "warn", f"По рынку на {euro(over)} больше бюджета — реально, если брать ниже рынка"
    elif ratio is not None and ratio >= 0.65:
        status, summary = "warn", f"По рынку на {euro(over)} больше бюджета — очень жёстко"
    else:
        status, summary = "fail", f"Не укладываешься: не хватает ~{euro(over)}"
    if status != "ok" and candidates:
        cheaper = [c for c in candidates.values() if c.total * 0.85 <= budget]
        gpu = plan.slot("gpu")
        if cheaper and gpu is not None:
            best = max(cheaper, key=lambda c: c.quality)
            if best.option.key != gpu.chosen:
                fix = {"slot": "gpu", "option": best.option.key,
                       "label_ru": f"Взять {option_label(best.option)} — ~{euro(best.total)} за всю сборку"}
    return {"key": "budget", "status": status, "status_label": CHECK_STATUS_LABELS[status], "title": "Бюджет",
            "summary": summary, "lines": lines, "fix": fix}


def summary_ru(plan: Plan, r: dict[str, RSlot], totals: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    cards = gpu_cards(r)
    if cards:
        part, n = cards[0]
        parts.append(f"{n}× {part.label}" if n > 1 else part.label)
    estimate = totals.get("estimated_total") or totals.get("typical_total")
    money = f"~{euro(estimate)} по рынку" + (f" при бюджете {euro(plan.budget)}" if plan.budget else "")
    text = (" + остальное: " if parts else "") + money
    head = (parts[0] + text) if parts else money[:1].upper() + money[1:]
    extra = [c["summary"] for c in checks if c["key"] in ("vram", "speed")]
    fails = [c["title"] for c in checks if c["status"] == "fail"]
    out = head + "."
    if extra:
        out += " " + "; ".join(extra) + "."
    if fails:
        out += " Нужно поправить: " + ", ".join(fails).lower() + "."
    return out


# ======================================================================= plan
def _candidates(plan: Plan, market: Any) -> dict[str, Any]:
    if plan.slot("gpu") is None or plan.requirements.kind not in ("llm", "gaming"):
        return {}
    return {c.option.key: c for c in gpu_candidates(plan, market)}


def slot_view(plan: Plan, rs: RSlot, market: Any, ratio: float | None, *, ss: SlotState | None = None,
              tracking: bool = False, links: list[SearchLink] | None = None, candidates: dict[str, Any] | None = None,
              runner_ups: int = 4, trend: bool = False) -> dict[str, Any]:
    slot = rs.slot
    status_label = SLOT_STATUS_LABELS.get(slot.status, slot.status)
    if slot.status == "open" and tracking and rs.active:
        status_label = "Ищу"
    view: dict[str, Any] = {
        "key": slot.key, "label": slot.label, "kind": slot.kind, "hint": slot.hint, "active": rs.active,
        "inactive_reason": rs.inactive_ru, "status": slot.status, "status_label": status_label, "qty": rs.qty,
        "bought_qty": rs.bought_qty, "need_qty": rs.need_qty, "chosen": slot.chosen,
        "chosen_label": option_label(rs.option) if rs.option and rs.part else (rs.option.label if rs.option else ""),
        "options": [option_view(plan, slot.key, o, market, ratio, candidates=candidates) for o in slot.options],
        "note": slot.note,
        "purchases": [{"price": rnd(p.price), "qty": p.qty, "ad_id": p.ad_id, "option": p.option, "note": p.note,
                       "at": iso(p.at), "at_label": when_label(p.at)} for p in slot.purchases],
        "spent": rnd(sum(p.price for p in slot.purchases)),
    }
    if ss is not None:
        view.update({
            "price": price_view(ss.price, rs.qty), "target_unit": rnd(ss.target_unit), "max_unit": rnd(ss.max_unit),
            "target_total": rnd(ss.target_unit * rs.need_qty) if ss.target_unit else None,
            "best": offer_view(ss.offers[0], ss) if ss.offers else None,
            "picked": [offer_view(o, ss) for o in ss.picked],
            "runner_ups": [offer_view(o, ss) for o in ss.offers[1:1 + runner_ups]],
            "offers_count": len(ss.offers), "best_cost": rnd(ss.best_cost), "best_complete": ss.best_complete,
            "alternatives": [{"option": key, "label": option_label(slot.option(key)),
                              "best": offer_view(offers[0], ss) if offers else None, "count": len(offers)}
                             for key, offers in ss.alternatives.items() if slot.option(key) is not None],
        })
        if trend:
            seen = [listing_point(o.listing, o.first_seen, o.last_seen) for o in ss.offers]
            points = market.points(rs.part, rs.option, seen) if rs.option else seen
            view["trend"] = price_trend(points)
    view["searches"] = [link.search_name for link in (links or []) if link.slot == slot.key]
    return view


def plan_view(plan: Plan, market: Any) -> dict[str, Any]:
    """POST /projects/plan: the preview (nothing saved)."""
    state = project_state(plan, None, market)  # type: ignore[arg-type]
    return _plan_payload(plan, state, market)


def _plan_payload(plan: Plan, state: ProjectState, market: Any, *, tracking: bool = False,
                  links: list[SearchLink] | None = None, trend: bool = False) -> dict[str, Any]:
    r = state.r
    candidates = _candidates(plan, market)
    checks = compat_checks(plan, r)
    budget = budget_check(plan, state.totals, state.ratio, candidates=candidates)
    if budget:
        checks.append(budget)
    t = dict(state.totals)
    t["stretch_label_ru"] = stretch_label(state.ratio, t.get("over_budget"))
    new_note = ""
    if t.get("new_total"):
        new_note = f"Новым в магазине: ~{euro(t['new_total'])}"
        if t.get("new_missing"):
            new_note += " (без учёта: " + ", ".join(s.lower() for s in t["new_missing"]) + " — новыми не продаются)"
        elif t.get("savings_vs_new") and t["savings_vs_new"] > 0:
            new_note += f" — б/у выходит дешевле на ~{euro(t['savings_vs_new'])}"
    t["new_label_ru"] = new_note
    rough = any(s.price.rough and s.price.typical for s in state.slots.values() if s.rs.active)
    tmpl = kb.TEMPLATES.get(plan.template)
    return {
        "name": plan.name, "goal": plan.goal, "template": plan.template, "template_label": template_label(plan),
        "icon": tmpl.icon if tmpl else "list-checks", "kind": plan.requirements.kind, "budget": rnd(plan.budget),
        "requirements": requirements_view(plan),
        "slots": [slot_view(plan, r[s.key], market, state.ratio, ss=state.slots.get(s.key), tracking=tracking,
                            links=links, candidates=candidates if s.key == "gpu" else None, trend=trend)
                  for s in plan.slots],
        "checks": checks,
        "checks_summary": _checks_summary(checks),
        "totals": t,
        "summary_ru": summary_ru(plan, r, t, checks),
        "notes": list(plan.notes),
        "ai": plan.ai,
        "location": plan.location, "radius_km": plan.radius_km,
        "prices_rough": rough, "prices_note_ru": kb.ROUGH_NOTE_RU if rough else "",
        "plan": plan.model_dump(mode="json", include=set(Plan.model_fields)),
    }


def _checks_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    if fails:
        text = f"Есть проблемы: {fails}"
    elif warns:
        text = f"Всё совместимо, но есть моменты: {warns}"
    else:
        text = "Всё совместимо"
    return {"fail": fails, "warn": warns, "status": "fail" if fails else ("warn" if warns else "ok"), "text_ru": text}


def requirements_view(plan: Plan) -> dict[str, Any]:
    req = plan.requirements
    data = req.model_dump(mode="json")
    lines: list[str] = []
    if req.kind == "llm":
        if req.model_size_b:
            q = kb.quant(req.quant)
            lines.append(f"Модель {fmt_gb(req.model_size_b)}B в {q.label}, контекст {fmt_int(req.context or kb.DEFAULT_CONTEXT)}")
            mem = kb.llm_memory(req.model_size_b, req.quant, req.context, 1, active_b=req.active_b)
            data["weights_gb"] = round(mem.weights_gib, 1)
            data["kv_cache_gb"] = round(mem.kv_gib, 1)
        elif req.vram_gb:
            lines.append(f"Видеопамять {fmt_gb(req.vram_gb)} ГБ")
        data["quant_label"] = kb.quant(req.quant).label if req.quant else None
    if req.kind == "nas" and req.storage_tb:
        lines.append(f"Полезное место {fmt_gb(req.storage_tb)} ТБ, диски зеркалом")
    data["lines_ru"] = lines
    return data


# ==================================================================== project
def next_step(project: Project, state: ProjectState) -> dict[str, str]:
    if project.status == "draft":
        return {"key": "track", "label_ru": "Начать отслеживание"}
    if project.status == "paused":
        return {"key": "resume", "label_ru": "Продолжить отслеживание"}
    if project.status == "done":
        return {"key": "done", "label_ru": "Всё собрано"}
    best = [(key, ss) for key, ss in state.slots.items() if ss.picked and ss.target_unit
            and ss.picked[0].unit_cost <= ss.target_unit]
    if best:
        key, ss = best[0]
        return {"key": "buy", "label_ru": f"Посмотреть: {ss.rs.slot.label.lower()} ниже цели", "slot": key}
    return {"key": "wait", "label_ru": "Жду предложений"}


def project_card(project: Project, state: ProjectState, *, searches: int = 0,
                 alerts: tuple[int, Any] | None = None) -> dict[str, Any]:
    t = state.totals
    done, total = t["slots_done"], t["slots_total"]
    estimate = t.get("best_total") if t.get("best_complete") else t.get("estimated_total")
    if project.budget:
        prefix = "" if t.get("best_complete") else "~"
        total_label = f"{prefix}{euro(estimate)} из {euro(project.budget)}"
    else:
        total_label = f"~{euro(estimate)}"
    count, last = alerts or (0, None)
    tmpl = kb.TEMPLATES.get(project.template)
    headline = ""
    for ss in state.slots.values():
        if ss.picked and ss.target_unit and ss.picked[0].unit_cost <= ss.target_unit:
            o = ss.picked[0]
            name = o.part.label if o.part else o.listing.title[:40]
            headline = f"Ниже цели: {name} за {euro(o.unit_cost)}"
            break
    return {
        "id": project.id, "name": project.name, "goal": project.goal, "template": project.template,
        "template_label": template_label(project), "icon": tmpl.icon if tmpl else "list-checks",
        "status": project.status, "status_label": PROJECT_STATUS_LABELS.get(project.status, project.status),
        "budget": rnd(project.budget), "spent": t["spent"], "remaining_budget": t["remaining_budget"],
        "best_total": t["best_total"], "best_complete": t["best_complete"], "estimated_total": t["estimated_total"],
        "fits_budget": t["fits_budget"], "fits_estimate": t["fits_estimate"], "over_budget": t["over_budget"],
        "total_label": total_label, "slots_total": total, "slots_done": done,
        "progress": round(done / total, 3) if total else 0.0,
        "progress_label": f"Собрано {done} из {total}",
        "searches": searches, "alerts": count, "last_alert_at": iso(last), "last_alert_at_label": when_label(last),
        "created_at": iso(project.created_at), "updated_at": iso(project.updated_at),
        "updated_at_label": when_label(project.updated_at),
        "tracking_since": iso(project.tracking_since), "tracking_since_label": when_label(project.tracking_since),
        "next_step": next_step(project, state), "headline_ru": headline,
    }


_URL_RE = re.compile(r"https?://\S+")
_LEAD_SYMBOLS_RE = re.compile(r"^[^\w«(~]+")  # emoji, ⚠, • before the text
_DE_THOUSANDS_RE = re.compile(r"(\d)\.(?=\d{3}(?!\d))")


def legacy_alert_fields(text: str) -> dict[str, str]:
    """title_ru / detail_ru / url of an alert stored before they existed, from its Telegram text:
    the first line is the news, the rest is plain detail; links, emoji and German numbers go."""
    lines: list[str] = []
    url = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Сборка: "):  # the build's own link
            continue
        url = url or next(iter(_URL_RE.findall(line)), "")
        line = _LEAD_SYMBOLS_RE.sub("", _URL_RE.sub("", line)).strip().rstrip(" —-·").strip()
        line = _DE_THOUSANDS_RE.sub("\\1\u00a0", line).replace(" €", "\u00a0€")
        if line:
            lines.append(line)
    return {"title_ru": lines[0] if lines else "", "detail_ru": " · ".join(lines[1:]), "url": url}


def alert_view(a: AlertRecord) -> dict[str, Any]:
    """`text` is the Telegram message (kept as it was sent); title_ru / detail_ru / url are for the app."""
    fields = {"title_ru": a.title_ru, "detail_ru": a.detail_ru, "url": a.url}
    if not a.title_ru:
        fields = legacy_alert_fields(a.text)
    return {"kind": a.kind, "kind_label": ALERT_KIND_LABELS.get(a.kind, a.kind), "ad_id": a.ad_id, "slot": a.slot,
            "price": rnd(a.price), "total": rnd(a.total), "text": a.text, **fields, "delivered": a.delivered,
            "delivered_label": "Отправлено" if a.delivered else "Только в приложении",
            "sent_at": iso(a.sent_at), "sent_at_label": when_label(a.sent_at), "deal_path": f"/deal/{a.ad_id}"}


def project_view(project: Project, state: ProjectState, market: Any, *, links: list[SearchLink],
                 alerts: list[AlertRecord], search_info: dict[str, dict[str, Any]], alert_stats: tuple[int, Any]) -> dict[str, Any]:
    card = project_card(project, state, searches=len(links), alerts=alert_stats)
    payload = _plan_payload(project, state, market, tracking=project.status == "tracking", links=links, trend=True)
    payload.pop("name", None)
    gpu_offers = [o for ss in state.slots.values() if ss.rs.slot.kind == "gpu"
                  for o in [*ss.offers, *(x for offers in ss.alternatives.values() for x in offers)]]
    gpu_slot = state.slots.get("gpu")
    return {
        **card, **payload,
        "searches_list": [{"name": link.search_name, "slot": link.slot, "option": link.option,
                           **search_info.get(link.search_name, {"exists": False, "enabled": False, "id": None})}
                          for link in links],
        "alerts_list": [alert_view(a) for a in alerts],
        "gpu_ranking": [offer_view(o, gpu_slot) for o in rank_gpu_offers(gpu_offers)[:5]],
    }


__all__ = [
    "offer_view", "option_view", "plan_view", "project_card", "project_view", "slot_view", "template_label",
    "templates_payload",
]
