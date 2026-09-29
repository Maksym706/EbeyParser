"""Parts plan for a build: goal text → requirements → template slots with alternatives → default
choices → compatibility checks with the maths in plain Russian.

Works without AI (templates + knowledge-base rules). With the local LLM on, a text-only JSON call
may only pick among the knowledge-base options, explain the picks and add free-form items (a
search phrase, never a price); anything else in its answer is ignored.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from . import knowledge as kb
from .knowledge import Part, Template, fmt_gb, fmt_int
from .market import KbMarket
from .models import Plan, PlanOption, PlanSlot, Requirements

log = logging.getLogger(__name__)

STRETCH_REACHABLE = 0.85  # a build whose market total is within budget / 0.85 is reachable with deals
MAX_GPUS = 3
MAX_GPU_OPTIONS = 6
MAX_CUSTOM_ITEMS = 20
MAX_AI_ITEMS = 5
ELECTRICITY_EUR_KWH = 0.35


# ===================================================================== request
class CustomItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=80)
    query: str = Field("", max_length=80)
    target_price: float | None = Field(None, gt=0, le=100000)
    qty: int = Field(1, ge=1, le=20)


class PlanRequest(BaseModel):
    """POST /projects/plan (and POST /projects without a ready plan)."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field("", max_length=2000)
    budget: float | None = Field(None, gt=0, le=100000)
    template: str | None = None
    name: str | None = Field(None, max_length=80)
    use_ai: bool = True
    model_size_b: float | None = Field(None, gt=0, le=2000)
    active_b: float | None = Field(None, gt=0, le=2000)
    quant: str | None = Field(None, max_length=20)
    context: int | None = Field(None, ge=256, le=2_000_000)
    vram_gb: float | None = Field(None, gt=0, le=1024)
    storage_tb: float | None = Field(None, gt=0, le=500)
    drives: int | None = Field(None, ge=1, le=16)
    location: str | None = Field(None, max_length=80)
    radius_km: int | None = Field(None, ge=0, le=500)
    items: list[CustomItem] = Field(default_factory=list, max_length=MAX_CUSTOM_ITEMS)


# ================================================================ goal parsing
@dataclass
class GoalInfo:
    kind: str | None = None
    budget: float | None = None
    model_size_b: float | None = None
    active_b: float | None = None
    quant: str | None = None
    context: int | None = None
    vram_gb: float | None = None
    storage_tb: float | None = None
    drives: int | None = None
    items: list[CustomItem] = field(default_factory=list)
    understood: list[str] = field(default_factory=list)  # «Понял так»: short Russian facts


_NUM = r"\d{1,3}(?:[ .  ]\d{3})+(?!\d)|\d+(?:[.,]\d+)?"
_MULT = r"(k|к|тыс\.?|тысяч\w*)?"
_MONEY_RE = re.compile(rf"({_NUM})\s*{_MULT}\s*(?:€|eur\b|euro\b|евро\b)", re.IGNORECASE)
_BUDGET_RE = re.compile(rf"(?:бюджет\w*|budget|всего|итого|в пределах|не больше|не дороже|максимум)\s*"
                        rf"(?:до|—|-|:|~|≈|около|примерно|в)?\s*({_NUM})\s*{_MULT}\s*(?:€|eur\b|euro\b|евро\b)?",
                        re.IGNORECASE)
_MODEL_RE = re.compile(r"(?<![\w.])(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:b|bn|млрд|миллиард\w*)(?!\w)", re.IGNORECASE)
_MOE_RE = re.compile(r"(\d{1,4}(?:[.,]\d)?)\s*b\s*[-_ ]?\s*a\s*(\d{1,3}(?:[.,]\d)?)\s*b(?!\w)", re.IGNORECASE)
_GPT_OSS_RE = re.compile(r"gpt[- ]?oss[- ]?(120|20)\s*b?", re.IGNORECASE)
_QUANT_RE = re.compile(r"(?<![\w])(i?q[2-8](?:_[a-z0-9]{1,2}){0,2}|fp16|bf16|f16|int[48]|awq|gptq)(?![\w])", re.IGNORECASE)
_BITS_RE = re.compile(r"(\d{1,2})\s*[- ]?(?:бит\w*|bits?)(?![a-z])", re.IGNORECASE)
_CTX_RE = re.compile(r"(?:контекст\w*|context|ctx)\D{0,15}?(\d+(?:[.,]\d+)?)\s*(k|к|тыс\w*)?", re.IGNORECASE)
_CTX2_RE = re.compile(r"(\d+)\s*(k|к)\s*(?:токен\w*|контекст\w*|context|ctx)", re.IGNORECASE)
_VRAM_RE = re.compile(r"(\d{1,3})\s*(?:гб|gb|g)\s*(?:vram|видеопамят\w*|врам)", re.IGNORECASE)
_VRAM2_RE = re.compile(r"(?:vram|видеопамят\w*|врам)\D{0,10}?(\d{1,3})\s*(?:гб|gb)", re.IGNORECASE)
_TB_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:тб|tb|терабайт\w*)(?!\w)", re.IGNORECASE)
_DRIVES_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:x\s*)?(?:диск\w*|hdd|жёстк\w*|жестк\w*|drives?)", re.IGNORECASE)

_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("llm", ("llm", "ллм", "нейросет", "языков", "lm studio", "ollama", "llama", "qwen", "mistral", "deepseek", "gemma",
             "инференс", "ии-сервер", "ai-сервер", "ai сервер", "ии сервер", "для ии", "gpt-oss", "vram", "видеопамят")),
    ("nas", ("nas", "хранилищ", "файлов", "бэкап", "бекап", "plex", "jellyfin", "медиасервер", "truenas", "unraid",
             "raid", "файлопомойк")),
    ("gaming", ("игров", "игр ", "игры", "gaming", "гейм", "1080p", "fps", "full hd", "fullhd", "киберспорт", "cs2",
                "fortnite")),
)
_TEMPLATE_BY_KIND = {"nas": "nas", "gaming": "gaming_1080p", "custom": "custom"}

# a few everyday Russian words -> what German ads say (custom lists without the AI)
_RU_DE = {
    "ноутбук": "notebook", "монитор": "monitor", "видеокарта": "grafikkarte", "процессор": "prozessor",
    "клавиатура": "tastatur", "мышь": "maus", "мышка": "maus", "наушники": "kopfhörer", "колонки": "lautsprecher",
    "док-станция": "dockingstation", "докстанция": "dockingstation", "дюймов": "zoll", "дюйма": "zoll", "дюйм": "zoll",
    "стол": "schreibtisch", "кресло": "bürostuhl", "стул": "stuhl", "велосипед": "fahrrad", "телефон": "handy",
    "смартфон": "handy", "планшет": "tablet", "роутер": "router", "принтер": "drucker", "камера": "kamera",
    "объектив": "objektiv", "блок": "", "питания": "netzteil", "корпус": "gehäuse", "память": "ram",
    "оперативка": "ram", "диск": "festplatte", "жёсткий": "", "жесткий": "", "кулер": "kühler", "вебкамера": "webcam",
    "микрофон": "mikrofon", "телевизор": "fernseher", "приставка": "konsole", "холодильник": "kühlschrank",
    "лампа": "lampe", "шкаф": "schrank", "полка": "regal", "кровать": "bett", "диван": "sofa", "матрас": "matratze",
}
_RU_FILLER = frozenset({"для", "с", "на", "и", "или", "б/у", "бу", "новый", "новая", "новое", "хороший", "хорошая",
                        "нормальный", "нужен", "нужна", "нужно", "хочу", "купить", "штука", "шт", "мне", "в", "под",
                        "до", "около", "примерно", "бюджет", "евро"})


def _num(raw: str, mult: str | None) -> float | None:
    text = raw.replace(" ", " ").replace(" ", " ").strip()
    if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+", text):
        value = float(re.sub(r"[ .]", "", text))
    else:
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            return None
    if mult:
        value *= 1000
    return value


def _euro(value: float | None) -> str:
    from ..notify.render import format_money

    return format_money(value)


def _dedupe_facts(facts: list[str]) -> list[str]:
    return list(dict.fromkeys(f for f in facts if f))


def parse_goal(text: str) -> GoalInfo:
    """What the goal text says, by rules (numbers are never left to the LLM)."""
    info = GoalInfo()
    raw = " ".join((text or "").split())
    if not raw:
        return info
    low = raw.lower()
    # budget: "бюджет 1500", else the largest amount in euros (custom lists: only "бюджет …")
    budgets = [b for b in (_num(m.group(1), m.group(2)) for m in _BUDGET_RE.finditer(raw))
               if b is not None and 10 <= b <= 100000]
    money = [v for v in (_num(m.group(1), m.group(2)) for m in _MONEY_RE.finditer(raw))
             if v is not None and 10 <= v <= 100000]
    for kind, words in _KIND_WORDS:
        if any(w in f" {low} " for w in words):
            info.kind = kind
            break
    moe = _MOE_RE.search(raw)
    oss = _GPT_OSS_RE.search(raw)
    if oss:
        total, active = (117.0, 5.1) if oss.group(1) == "120" else (21.0, 3.6)
        info.model_size_b, info.active_b = total, active
    elif moe:
        info.model_size_b = float(moe.group(1).replace(",", "."))
        info.active_b = float(moe.group(2).replace(",", "."))
    else:
        sizes = [float(m.group(1).replace(",", ".")) for m in _MODEL_RE.finditer(raw)]
        sizes = [s for s in sizes if 0.5 <= s <= 2000]
        if sizes:
            info.model_size_b = max(sizes)
    if info.model_size_b and info.kind is None:
        info.kind = "llm"
    quant = _QUANT_RE.search(raw)
    if quant:
        info.quant = kb.normalize_quant(quant.group(1))
    else:
        bits = _BITS_RE.search(raw)
        if bits:
            info.quant = kb.normalize_quant(f"{bits.group(1)}bit")
    ctx = _CTX_RE.search(raw) or _CTX2_RE.search(raw)
    if ctx:
        value = float(ctx.group(1).replace(",", "."))
        if ctx.group(2) or value <= 1024:
            value *= 1024
        if 256 <= value <= 2_000_000:
            info.context = int(value)
    vram = _VRAM_RE.search(raw) or _VRAM2_RE.search(raw)
    if vram:
        info.vram_gb = float(vram.group(1))
    if info.kind == "nas" or (info.kind is None and _TB_RE.search(raw)):
        tb = _TB_RE.search(raw)
        if tb:
            info.storage_tb = float(tb.group(1).replace(",", "."))
            info.kind = info.kind or "nas"
        drives = _DRIVES_RE.search(raw)
        if drives and 1 <= int(drives.group(1)) <= 16:
            info.drives = int(drives.group(1))
    if info.kind is None:
        info.kind = "custom"
    if info.kind == "custom":
        info.budget = max(budgets) if budgets else None
        info.items = split_items(raw)
    else:
        info.budget = max(budgets) if budgets else (max(money) if money else None)
    facts: list[str] = []
    if info.budget:
        facts.append(f"бюджет {_euro(info.budget)}")
    if info.model_size_b:
        facts.append(f"модель {fmt_gb(info.model_size_b)}B" + (f" (MoE, активных {fmt_gb(info.active_b)}B)"
                                                                  if info.active_b else ""))
    if info.quant:
        facts.append(f"квантизация {kb.quant(info.quant).label}")
    if info.context:
        facts.append(f"контекст {fmt_int(info.context)} токенов")
    if info.vram_gb:
        facts.append(f"видеопамять {fmt_gb(info.vram_gb)} ГБ")
    if info.storage_tb:
        facts.append(f"место {fmt_gb(info.storage_tb)} ТБ")
    if info.drives:
        facts.append(f"дисков: {info.drives}")
    info.understood = _dedupe_facts(facts)
    return info


_ITEM_SPLIT_RE = re.compile(r"[\n;,]+|\s\+\s")
_ITEM_PRICE_RE = re.compile(rf"(?:до|за|≈|~)?\s*({_NUM})\s*(?:€|eur\b|евро\b)", re.IGNORECASE)
_ITEM_QTY_RE = re.compile(r"^(\d{1,2})\s*(?:x|×|шт\.?)\s*|\s(\d{1,2})\s*шт\.?", re.IGNORECASE)


def split_items(text: str) -> list[CustomItem]:
    """'ThinkPad T480 до 200 €, 2 шт монитор 27 дюймов; бюджет 400 €' -> items (without the budget)."""
    items: list[CustomItem] = []
    for chunk in _ITEM_SPLIT_RE.split(text or ""):
        part = chunk.strip(" -•*.:\t")
        part = re.sub(r"^\d{1,2}[.)]\s+", "", part)
        if not part or _BUDGET_RE.search(part) and len(_BUDGET_RE.sub("", part).strip(" .,:-")) < 3:
            continue
        part = _BUDGET_RE.sub("", part).strip(" -—:.")
        target = None
        price = _ITEM_PRICE_RE.search(part)
        if price:
            target = _num(price.group(1), None)
            part = (part[:price.start()] + part[price.end():]).strip(" -—:.")
        qty = 1
        q = _ITEM_QTY_RE.search(part)
        if q:
            qty = int(q.group(1) or q.group(2))
            part = (part[:q.start()] + " " + part[q.end():]).strip()
        label = " ".join(part.split())[:80]
        if len(label) < 2 or not re.search(r"[\w]", label):
            continue
        for prefix in ("нужен ", "нужна ", "нужно ", "хочу ", "купить "):
            if label.lower().startswith(prefix):
                label = label[len(prefix):]
        label = label[:1].upper() + label[1:]
        items.append(CustomItem(label=label, query=search_words(label), target_price=target,
                                qty=max(1, min(qty, 20))))
        if len(items) >= MAX_CUSTOM_ITEMS:
            break
    return items


def search_words(label: str) -> str:
    """Words for a Kleinanzeigen search from a (Russian) item name: common words translated."""
    out: list[str] = []
    for word in re.split(r"\s+", (label or "").strip()):
        low = word.lower().strip(",.;:!?()«»\"'")
        if not low or low in _RU_FILLER:
            continue
        if low in _RU_DE:
            if _RU_DE[low]:
                out.append(_RU_DE[low])
            continue
        if re.search(r"[а-яё]", low):
            continue  # other Russian words would never match a German ad
        out.append(word.strip(",.;:!?()«»\"'"))
    return " ".join(dict.fromkeys(out))[:80] or (label or "")[:80]


# ============================================================ requirement merge
def merged_info(req: PlanRequest) -> GoalInfo:
    """Parsed goal + explicit request fields (explicit values win)."""
    info = parse_goal(req.goal)
    for name in ("budget", "model_size_b", "active_b", "context", "vram_gb", "storage_tb", "drives"):
        value = getattr(req, name)
        if value is not None:
            setattr(info, name, value)
    if req.quant:
        info.quant = kb.normalize_quant(req.quant) or info.quant
    if req.items:
        info.items = list(req.items)
    if req.budget is not None and not any(f.startswith("бюджет") for f in info.understood):
        info.understood.insert(0, f"бюджет {_euro(req.budget)}")
    tmpl = kb.template(req.template)
    if tmpl is not None:
        info.kind = tmpl.kind
    elif req.model_size_b:
        info.kind = "llm"
    return info


def pick_template(requested: str | None, info: GoalInfo) -> str:
    if requested and requested in kb.TEMPLATES:
        return requested
    if info.kind == "llm":
        need24 = _llm_need(info, 1)
        if info.vram_gb:
            return "llm_24" if info.vram_gb <= 24 else "llm_48"
        if need24 is not None and _fits(24.0, need24):
            return "llm_24"
        return "llm_48" if (need24 is not None or info.model_size_b) else "llm_24"
    return _TEMPLATE_BY_KIND.get(info.kind or "custom", "custom")


def _llm_need(info: GoalInfo | Requirements, gpus: int) -> float | None:
    size = info.model_size_b
    if not size:
        return None
    return kb.llm_memory(size, info.quant, info.context, gpus, active_b=info.active_b).total_gib


def _fits(have: float, need: float) -> bool:
    return have - need >= _margin(have)


def _margin(have: float) -> float:
    return max(1.0, 0.04 * have)


def vram_status(have: float, need: float, *, strict_margin: bool = True) -> str:
    headroom = have - need
    if headroom < 0:
        return "fail"
    if strict_margin and headroom < _margin(have):
        return "warn"
    return "ok"


def requirements_from(info: GoalInfo, tmpl: Template) -> Requirements:
    req = Requirements(kind=tmpl.kind)  # type: ignore[arg-type]
    if tmpl.kind == "llm":
        req.model_size_b = info.model_size_b
        req.active_b = info.active_b
        req.quant = info.quant or (kb.DEFAULT_QUANT if info.model_size_b else None)
        req.context = info.context or (kb.DEFAULT_CONTEXT if info.model_size_b else None)
        # a model size decides the memory; otherwise the explicit VRAM or the template's
        req.vram_gb = None if info.model_size_b else (info.vram_gb or tmpl.vram_gb)
    elif tmpl.kind == "nas":
        req.storage_tb = info.storage_tb or 8.0
        req.drives = info.drives
    return req


# ======================================================================= plan
def build_plan(req: PlanRequest, market: Any = None, *, template_key: str | None = None) -> Plan:
    """The rules-only plan (the LLM step refines it in plan_goal)."""
    market = market or KbMarket()
    info = merged_info(req)
    tkey = template_key or pick_template(req.template, info)
    tmpl = kb.TEMPLATES[tkey]
    requirements = requirements_from(info, tmpl)
    plan = Plan(goal=(req.goal or "").strip(), template=tkey, budget=info.budget, requirements=requirements,
                location=(req.location or "").strip(), radius_km=req.radius_km)
    if tmpl.kind == "custom":
        plan.slots = custom_slots(info.items)
        if not plan.slots:
            plan.notes.append("Добавь, что нужно купить: название как в объявлениях (например, «ThinkPad T480») "
                              "и сколько готов заплатить")
    else:
        plan.slots = [_slot_from_spec(spec) for spec in tmpl.slots]
        gpu = plan.slot("gpu")
        if gpu is not None:
            gpu.options, note = gpu_options(tmpl, requirements, market)
            if note:
                plan.notes.append(note)
        if tmpl.kind == "nas":
            _nas_disks(plan)
        choose_defaults(plan, market)
    plan.name = (req.name or "").strip() or default_name(plan)
    if info.understood:
        plan.notes.insert(0, "Понял так: " + ", ".join(info.understood))
    return plan


def default_name(plan: Plan) -> str:
    req = plan.requirements
    tmpl = kb.TEMPLATES.get(plan.template)
    if req.kind == "llm" and req.model_size_b:
        return f"LLM-сервер: {fmt_gb(req.model_size_b)}B {kb.quant(req.quant).label}"
    if req.kind == "nas" and req.storage_tb:
        return f"NAS на {fmt_gb(req.storage_tb)} ТБ"
    if req.kind == "custom" and plan.slots:
        first = plan.slots[0].label
        return first if len(plan.slots) == 1 else f"{first} и ещё {len(plan.slots) - 1}"
    return tmpl.title if tmpl else "Моя сборка"


def _slot_from_spec(spec: kb.SlotSpec) -> PlanSlot:
    options = [PlanOption(key=k, kb_key=k) for k in spec.options if k in kb.PARTS]
    return PlanSlot(key=spec.key, label=spec.label, kind=spec.kind, when=spec.when, per_gpu=spec.per_gpu,
                    hint=spec.hint, options=options, chosen=options[0].key if options else None)


def custom_slots(items: Iterable[CustomItem]) -> list[PlanSlot]:
    slots: list[PlanSlot] = []
    used: set[str] = set()
    for n, item in enumerate(items, 1):
        key = f"item{n}"
        while key in used:
            key += "x"
        used.add(key)
        opt = PlanOption(key="main", label=item.label, query=(item.query or search_words(item.label)), qty=item.qty,
                         source="user", target_price=item.target_price,
                         target_by="user" if item.target_price else "auto")
        slots.append(PlanSlot(key=key, label=item.label, kind="generic", options=[opt], chosen="main"))
    return slots


def gpu_option_key(part_key: str, count: int) -> str:
    return part_key if count == 1 else f"{part_key}x{count}"


def gpu_options(tmpl: Template, req: Requirements, market: Any) -> tuple[list[PlanOption], str]:
    """GPU alternatives that reach the video memory the goal needs (1–3 cards each)."""
    if tmpl.kind == "gaming":
        return [PlanOption(key=k, kb_key=k) for k in kb.GAMING_GPUS], ""
    target = req.vram_gb
    found: list[tuple[PlanOption, float]] = []
    for key in kb.LLM_GPUS:
        part = kb.GPUS[key]
        vram = part.specs["vram_gb"]
        for count in range(1, MAX_GPUS + 1):
            have = vram * count
            need = target if target else _llm_need(req, count)
            if need is None:
                need = 24.0
            status = ("ok" if have >= need else "fail") if target else vram_status(have, need)
            if status != "fail":
                break
        else:
            continue
        price = market.price(part).typical or part.price.typical
        found.append((PlanOption(key=gpu_option_key(key, count), kb_key=key, qty=count), price * count))
    note = ""
    if not found:
        note = ("Столько видеопамяти не набрать даже тремя картами — показываю самые ёмкие варианты; "
                "модель придётся частично держать в оперативной памяти или взять квантизацию поменьше")
        for key in ("mi50_32", "rtx3090", "tesla_p40"):
            part = kb.GPUS[key]
            found.append((PlanOption(key=gpu_option_key(key, MAX_GPUS), kb_key=key, qty=MAX_GPUS),
                          (market.price(part).typical or part.price.typical) * MAX_GPUS))
    always = [f for f in found if f[0].kb_key in kb.LLM_ALWAYS_SHOWN]
    rest = sorted((f for f in found if f[0].kb_key not in kb.LLM_ALWAYS_SHOWN), key=lambda f: (f[0].qty, f[1]))
    chosen = (always + rest)[:MAX_GPU_OPTIONS]
    chosen.sort(key=lambda f: f[1])
    return [f[0] for f in chosen], note


def _nas_disks(plan: Plan) -> None:
    req = plan.requirements
    slot = plan.slot("hdd")
    if slot is None:
        return
    usable = req.storage_tb or 8.0
    for opt in slot.options:
        part = kb.part(opt.kb_key)
        size = float(part.specs.get("size_tb") or 1) if part else 1.0
        drives = max(2, 2 * math.ceil(usable / size))  # mirror pairs
        if req.drives:
            drives = max(req.drives, 2 if req.drives >= 2 else 1)
        opt.qty = min(drives, 8)
    wanted = "hdd_8tb" if usable > 4 else "hdd_4tb"
    if slot.option(wanted) is not None:
        slot.chosen = wanted


# =================================================================== resolution
CONDITIONS_RU = {
    "passive_gpu": "у выбранных видеокарт свои вентиляторы",
    "open_frame": "карты стоят прямо в корпусе",
    "cpu_needs_cooler": "кулер идёт в коробке с процессором",
    "board_needs_cpu": "процессор уже распаян на плате",
    "hba_needed": "SATA-портов на плате хватает",
}


@dataclass
class RSlot:
    slot: PlanSlot
    option: PlanOption | None
    part: Part | None
    qty: int
    active: bool
    inactive_ru: str = ""

    @property
    def open(self) -> bool:
        return self.active and self.slot.status == "open"

    @property
    def bought_qty(self) -> int:
        return sum(p.qty for p in self.slot.purchases)

    @property
    def need_qty(self) -> int:
        if not self.active or self.slot.status in ("have", "skipped", "bought"):
            return 0
        return max(0, self.qty - self.bought_qty)


def resolve(plan: Plan) -> dict[str, RSlot]:
    """Chosen option, part, quantity and whether each slot is needed for the current choices."""
    out: dict[str, RSlot] = {}
    for slot in plan.slots:
        opt = slot.option()
        out[slot.key] = RSlot(slot, opt, kb.part(opt.kb_key) if opt else None, opt.qty if opt else 1, True)
    gpu = out.get("gpu")
    gpu_n = gpu.qty if gpu and gpu.part else 0
    case = out.get("case")
    board = out.get("board")
    facts = {
        "passive_gpu": bool(gpu and gpu.part and gpu.part.specs.get("cooling") == "passive"),
        "open_frame": bool(case and case.part and case.part.specs.get("open_frame")),
        "board_needs_cpu": bool(board and board.part and not board.part.specs.get("cpu_included")),
    }
    cpu = out.get("cpu")
    if cpu is not None and cpu.slot.when:
        cpu.active = facts.get(cpu.slot.when, True)
    facts["cpu_needs_cooler"] = bool(cpu and cpu.active and cpu.part and not cpu.part.specs.get("cooler_included"))
    facts["hba_needed"] = _hba_needed(out)
    for key, rs in out.items():
        if rs.slot.per_gpu:
            rs.qty = max(1, gpu_n)
        when = rs.slot.when
        if when and key != "cpu":
            rs.active = facts.get(when, True)
        if when and not rs.active:
            rs.inactive_ru = CONDITIONS_RU.get(when, "не нужно для выбранного варианта")
    return out


def _hba_needed(out: dict[str, RSlot]) -> bool:
    board, hdd, boot = out.get("board"), out.get("hdd"), out.get("boot")
    if board is None or board.part is None or hdd is None:
        return False
    ports = board.part.specs.get("sata_ports")
    if not ports:
        return False
    need = hdd.qty + (1 if boot and boot.part and boot.part.specs.get("type") == "ssd" else 0)
    return need > ports


def gpu_cards(r: dict[str, RSlot]) -> list[tuple[Part, int]]:
    gpu = r.get("gpu")
    return [(gpu.part, gpu.qty)] if gpu and gpu.part and gpu.active else []


def _part_of(r: dict[str, RSlot], key: str) -> Part | None:
    rs = r.get(key)
    return rs.part if rs is not None else None


def _cpu_tdp(r: dict[str, RSlot]) -> float:
    cpu, board = r.get("cpu"), r.get("board")
    if cpu and cpu.active and cpu.part:
        return float(cpu.part.specs.get("tdp_w") or 0)
    if board and board.part and board.part.specs.get("cpu_included"):
        return float(board.part.specs.get("tdp_w") or 0)
    return 0.0


def _drives(r: dict[str, RSlot]) -> int:
    hdd = r.get("hdd")
    return hdd.qty if hdd and hdd.part else 0


# ============================================================ default choices
def _set(plan: Plan, key: str, option: str | None, keep: frozenset[str] | set[str]) -> None:
    slot = plan.slot(key)
    if slot is None or option is None or key in keep or slot.status != "open":
        return
    if slot.option(option) is not None:
        slot.chosen = option


def _psu_for(plan: Plan, r: dict[str, RSlot]) -> str | None:
    slot = plan.slot("psu")
    if slot is None:
        return None
    rec = kb.psu_recommendation(gpu_cards(r), _cpu_tdp(r), drives=_drives(r))["recommended_w"]
    sized = sorted(((p.specs["watts"], o.key) for o in slot.options if (p := kb.part(o.kb_key)) is not None),
                   key=lambda t: t[0])
    for watts, key in sized:
        if watts >= rec:
            return key
    return sized[-1][1] if sized else None


def _ram_ok(ram: Part | None, board: Part | None) -> bool:
    if ram is None or board is None:
        return True
    s, b = ram.specs, board.specs
    types = b.get("ram_types") or ()
    if types and s["gen"] not in types:
        return False
    if s["registered"] and not b.get("rdimm"):
        return False
    return bool(s["sodimm"]) == bool(b.get("sodimm"))


def _first_ok_ram(plan: Plan, board: Part | None, prefer: tuple[str, ...] = ()) -> str | None:
    slot = plan.slot("ram")
    if slot is None:
        return None
    keys = [k for k in prefer if slot.option(k)] + [o.key for o in slot.options]
    for key in keys:
        if _ram_ok(kb.part(key), board):
            return key
    return None


def apply_dependents(plan: Plan, *, keep: frozenset[str] | set[str] = frozenset(), ideal: bool = True) -> None:
    """Choices of the slots that depend on others (platform, RAM, PSU, case). ideal=True: the
    rule's pick for the current GPU/CPU (planning); ideal=False: only fix what became incompatible
    (the user changed a choice)."""
    kind = plan.requirements.kind
    keep = frozenset(keep)
    r = resolve(plan)
    if kind == "llm":
        cards = gpu_cards(r)
        part, n = cards[0] if cards else (None, 0)
        display = bool(part and part.specs.get("display") is True)
        board = _part_of(r, "board")
        board_ok = board is not None and (board.specs.get("x16_slots") or 0) >= n
        if ideal or not board_ok:
            _set(plan, "board", "x99" if n >= 3 else "b550", keep)
        r = resolve(plan)
        board = r["board"].part if "board" in r else None
        socket = board.specs.get("socket") if board else None
        cpu = _part_of(r, "cpu")
        cpu_ok = cpu is not None and cpu.specs.get("socket") == socket and (display or cpu.specs.get("igpu")
                                                                            or socket != "AM4")
        if ideal or not cpu_ok:
            if socket == "AM4":
                _set(plan, "cpu", "r5_5600" if display else "r5_5600g", keep)
            elif socket == "LGA2011-3":
                _set(plan, "cpu", "xeon_2680v4", keep)
        ram = _part_of(r, "ram")
        if ideal or not _ram_ok(ram, board):
            prefer = ("ddr4_ecc_64",) if board and board.specs.get("rdimm") else ("ddr4_32",)
            _set(plan, "ram", _first_ok_ram(plan, board, prefer), keep)
        case = _part_of(r, "case")
        width = int(part.specs.get("slot_width") or 2) if part else 2
        if n >= 3:
            want_case = "open_frame"
        elif n == 2 and width > 2:
            want_case = "case_big"
        else:
            want_case = "case_atx"
        case_ok = case is not None and (case.specs.get("open_frame") or n <= 1 or
                                        (n == 2 and (width <= 2 or (case.specs.get("expansion_slots") or 0) >= 8)))
        if ideal or not case_ok:
            _set(plan, "case", want_case, keep)
    elif kind == "gaming":
        r = resolve(plan)
        cpu = _part_of(r, "cpu")
        socket = cpu.specs.get("socket") if cpu else "AM4"
        board_key = {"AM4": "b550", "LGA1700": "b660_ddr4", "AM5": "b650"}.get(socket or "AM4", "b550")
        board = _part_of(r, "board")
        if ideal or board is None or board.specs.get("socket") != socket:
            _set(plan, "board", board_key, keep)
        r = resolve(plan)
        board = r["board"].part if "board" in r else None
        ram = _part_of(r, "ram")
        if ideal or not _ram_ok(ram, board):
            prefer = ("ddr4_32",) if plan.budget and plan.budget >= 1000 else ("ddr4_16",)
            _set(plan, "ram", _first_ok_ram(plan, board, prefer), keep)
    elif kind == "nas":
        board = _part_of(r, "board")
        ram = _part_of(r, "ram")
        if ideal or not _ram_ok(ram, board):
            _set(plan, "ram", _first_ok_ram(plan, board), keep)
    r = resolve(plan)
    psu_slot = plan.slot("psu")
    if psu_slot is not None:
        chosen = psu_slot.option()
        current = kb.part(chosen.kb_key if chosen else None)
        rec = kb.psu_recommendation(gpu_cards(r), _cpu_tdp(r), drives=_drives(r))["recommended_w"]
        if ideal or current is None or current.specs["watts"] < rec:
            _set(plan, "psu", _psu_for(plan, r), keep)


def unit_price(market: Any, rs: RSlot) -> float | None:
    if rs.option is None:
        return None
    if rs.option.source == "user" and rs.option.target_price and rs.part is None:
        return rs.option.target_price
    return market.price(rs.part, rs.option).typical


def build_total(plan: Plan, r: dict[str, RSlot], market: Any) -> tuple[float, list[str]]:
    """Typical market total of the active slots (bought slots at what was paid); slots without a
    known price are listed separately."""
    total = 0.0
    unknown: list[str] = []
    for rs in r.values():
        if not rs.active or rs.slot.status in ("have", "skipped"):
            continue
        total += sum(p.price for p in rs.slot.purchases)
        need = rs.need_qty
        if need <= 0:
            continue
        price = unit_price(market, rs)
        if price is None:
            unknown.append(rs.slot.key)
            continue
        total += price * need
    return total, unknown


def _quality(plan: Plan, r: dict[str, RSlot]) -> float:
    cards = gpu_cards(r)
    if not cards:
        return 0.0
    part, n = cards[0]
    if plan.requirements.kind == "gaming":
        return float(part.specs.get("perf_1080p") or 0)
    req = plan.requirements
    if req.model_size_b:
        mem = kb.llm_memory(req.model_size_b, req.quant, req.context, n, active_b=req.active_b)
        weights = mem.active_weights_gib
        status = vram_status(part.specs["vram_gb"] * n, mem.total_gib)
    else:
        weights = 0.8 * (req.vram_gb or 24)
        status = "ok"
    speed = kb.speed_estimate(weights, cards)
    mid = ((speed["low"] + speed["high"]) / 2) if speed else 0.0
    factor = (1 - part.risk) * {1: 1.0, 2: 0.95}.get(n, 0.8) * (0.85 if status == "warn" else 1.0)
    return mid * factor


@dataclass
class Candidate:
    option: PlanOption
    total: float
    quality: float
    choices: dict[str, str | None]


def gpu_candidates(plan: Plan, market: Any) -> list[Candidate]:
    """Each GPU option with its dependent defaults: typical build total and quality."""
    slot = plan.slot("gpu")
    out: list[Candidate] = []
    if slot is None:
        return out
    for opt in slot.options:
        trial = plan.model_copy(deep=True)
        trial.slot("gpu").chosen = opt.key  # type: ignore[union-attr]
        apply_dependents(trial, ideal=True)
        r = resolve(trial)
        total, _ = build_total(trial, r, market)
        out.append(Candidate(opt, total, _quality(trial, r), {s.key: s.chosen for s in trial.slots}))
    return out


def pick_candidate(cands: list[Candidate], budget: float | None) -> Candidate | None:
    if not cands:
        return None
    if budget:
        pool = [c for c in cands if c.total * STRETCH_REACHABLE <= budget]
        if not pool:
            return min(cands, key=lambda c: c.total)
        return max(pool, key=lambda c: (round(c.quality, 3), -c.total))
    return max(cands, key=lambda c: (c.quality / max(c.total, 1.0), -c.total))


def choose_defaults(plan: Plan, market: Any) -> None:
    kind = plan.requirements.kind
    if kind in ("llm", "gaming") and plan.slot("gpu") is not None:
        best = pick_candidate(gpu_candidates(plan, market), plan.budget)
        if best is not None:
            for slot in plan.slots:
                if best.choices.get(slot.key):
                    slot.chosen = best.choices[slot.key]
            return
    apply_dependents(plan, ideal=True)


# ====================================================================== checks
STATUS_LABELS = {"ok": "Подходит", "warn": "Впритык", "fail": "Не подходит", "info": "К сведению"}


def _check(key: str, status: str, title: str, summary: str, lines: list[str] | None = None,
           fix: dict[str, str] | None = None) -> dict[str, Any]:
    return {"key": key, "status": status, "status_label": STATUS_LABELS[status], "title": title, "summary": summary,
            "lines": list(lines or []), "fix": fix}


def _label(key: str | None) -> str:
    part = kb.part(key)
    return part.label if part else ""


def _fix(plan: Plan, slot: str, option: str | None, label: str) -> dict[str, str] | None:
    s = plan.slot(slot)
    if option is None or s is None or s.option(option) is None or s.chosen == option:
        return None
    return {"slot": slot, "option": option, "label_ru": label}


def compat_checks(plan: Plan, r: dict[str, RSlot] | None = None) -> list[dict[str, Any]]:
    """Compatibility of the chosen options, with the maths in plain Russian."""
    r = r or resolve(plan)
    kind = plan.requirements.kind
    out: list[dict[str, Any]] = []
    if kind == "llm":
        out += _vram_checks(plan, r)
    if kind in ("llm", "gaming", "nas"):
        psu = _psu_check(plan, r)
        if psu:
            out.append(psu)
    if kind == "llm":
        out += _pcie_checks(plan, r)
    if kind in ("llm", "gaming", "nas"):
        out += [c for c in (_socket_check(plan, r), _ram_check(plan, r), _display_check(plan, r),
                            _cooling_check(plan, r)) if c]
    if kind == "llm":
        extra = _power_connectors(plan, r)
        if extra:
            out.append(extra)
    if kind == "nas":
        out += [c for c in (_capacity_check(plan, r), _sata_check(plan, r), _electricity_check(plan, r)) if c]
    return out


def _vram_checks(plan: Plan, r: dict[str, RSlot]) -> list[dict[str, Any]]:
    cards = gpu_cards(r)
    if not cards:
        return []
    part, n = cards[0]
    have = float(part.specs["vram_gb"] * n)
    card = f"{n}× {part.label}" if n > 1 else part.label
    req = plan.requirements
    out: list[dict[str, Any]] = []
    if req.model_size_b:
        mem = kb.llm_memory(req.model_size_b, req.quant, req.context, n, active_b=req.active_b)
        need = mem.total_gib
        status = vram_status(have, need)
        lines = mem.lines_ru() + [f"У {card}: {fmt_gb(have)} ГБ"]
        fix = None
        if status == "ok":
            per_token = kb.kv_bytes_per_token(req.model_size_b) / kb.GIB
            spare = have - mem.weights_gib - mem.overhead_gib - _margin(have)
            max_ctx = int(spare / per_token) // 1024 * 1024 if per_token else 0
            summary = f"{fmt_gb(have)} ГБ ≥ нужно ≈{fmt_gb(need)} ГБ — запас {fmt_gb(have - need)} ГБ"
            if max_ctx > mem.context:
                lines.append(f"Влезет целиком; контекст можно поднять примерно до {fmt_int(max_ctx)} токенов")
        elif status == "warn":
            summary = f"{fmt_gb(have)} ГБ при нужных ≈{fmt_gb(need)} ГБ — впритык"
            lower = _lower_quant(mem.quant.key)
            lines.append("Запас меньше пары гигабайт: длинный контекст не влезет. Помогут KV-кэш в q8_0 (вдвое "
                         "меньше)" + (f" или квантизация {kb.QUANTS[lower].label}" if lower else ""))
        else:
            summary = f"Не хватает ≈{fmt_gb(need - have)} ГБ: нужно ≈{fmt_gb(need)} ГБ, а у {card} {fmt_gb(have)} ГБ"
            lines.append("Не влезет целиком: часть слоёв уйдёт в оперативную память — скорость упадёт в разы")
            gpu_slot = plan.slot("gpu")
            better = next((o for o in (gpu_slot.options if gpu_slot else [])
                           if (p := kb.part(o.kb_key)) is not None
                           and vram_status(p.specs["vram_gb"] * o.qty, _llm_need(req, o.qty) or 0) != "fail"), None)
            if better is not None:
                fix = _fix(plan, "gpu", better.key, f"Взять {_option_label(better)}")
        out.append(_check("vram", status, "Видеопамять", summary, lines, fix))
        weights = mem.active_weights_gib
    else:
        target = float(req.vram_gb or 24)
        ok = have >= target
        biggest = kb.max_model_b(have, "q4_k_m", kb.DEFAULT_CONTEXT, n)
        biggest8 = kb.max_model_b(have, "q8_0", kb.DEFAULT_CONTEXT, n)
        lines = [f"У {card}: {fmt_gb(have)} ГБ видеопамяти",
                 f"Влезут модели примерно до {fmt_int(biggest)}B в Q4_K_M или до {fmt_int(biggest8)}B в Q8_0 "
                 f"(контекст 8 192 токенов)"]
        summary = (f"{fmt_gb(have)} ГБ — модели до ~{fmt_int(biggest)}B в Q4" if ok
                   else f"{fmt_gb(have)} ГБ — меньше, чем нужно по шаблону ({fmt_gb(target)} ГБ)")
        out.append(_check("vram", "ok" if ok else "fail", "Видеопамять", summary, lines))
        weights = 0.8 * have
    speed = kb.speed_estimate(weights, cards)
    if speed:
        lines = [speed["text_ru"]]
        if n > 1:
            lines.append("Карты работают по очереди (по слоям), поэтому скорость — как у одной карты с такой же памятью")
        out.append(_check("speed", "info", "Скорость ответа", speed["label_ru"], lines))
    return out


def _lower_quant(key: str) -> str | None:
    order = list(kb.QUANTS)
    i = order.index(key) if key in order else -1
    return order[i - 1] if i > 0 else None


def _option_label(opt: PlanOption) -> str:
    part = kb.part(opt.kb_key)
    if part is None:
        return opt.label or opt.key
    return f"{opt.qty}× {part.label}" if opt.qty > 1 and part.kind == "gpu" else part.label


def option_label(opt: PlanOption | None) -> str:
    return _option_label(opt) if opt is not None else ""


def _psu_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    psu = r.get("psu")
    if psu is None or not psu.active or psu.part is None:
        return None
    rec = kb.psu_recommendation(gpu_cards(r), _cpu_tdp(r), drives=_drives(r))
    watts = psu.part.specs["watts"]
    lines = list(rec["lines_ru"])
    fix = None
    better = _psu_for(plan, r)
    if watts >= rec["recommended_w"]:
        status = "ok"
        summary = f"{fmt_int(watts)} Вт — хватает (с запасом нужно от {fmt_int(rec['recommended_w'])} Вт)"
        smaller = kb.part(better)
        if smaller is not None and watts >= rec["recommended_w"] * 1.5 and smaller.specs["watts"] < watts:
            lines.append(f"Можно взять поменьше: хватит {fmt_int(rec['recommended_w'])} Вт")
            fix = _fix(plan, "psu", better, f"Взять {smaller.label}")
    elif watts >= rec["load_w"] * 1.1 or (rec["limited_w"] and watts >= rec["limited_w"]):
        status = "warn"
        summary = f"{fmt_int(watts)} Вт — впритык: лучше от {fmt_int(rec['recommended_w'])} Вт"
        fix = _fix(plan, "psu", better, f"Взять {_label(better)}")
    else:
        status = "fail"
        summary = f"{fmt_int(watts)} Вт мало: под нагрузкой ~{fmt_int(rec['load_w'])} Вт, нужно от " \
                  f"{fmt_int(rec['recommended_w'])} Вт"
        fix = _fix(plan, "psu", better, f"Взять {_label(better)}")
    return _check("psu", status, "Блок питания", summary, lines, fix)


def _pcie_checks(plan: Plan, r: dict[str, RSlot]) -> list[dict[str, Any]]:
    cards = gpu_cards(r)
    board = r.get("board")
    case = r.get("case")
    if not cards or board is None or board.part is None:
        return []
    part, n = cards[0]
    b = board.part.specs
    open_frame = bool(case and case.active and case.part and case.part.specs.get("open_frame"))
    x16 = b.get("x16_slots")
    lines: list[str] = []
    fix = None
    if x16 is None:
        status, summary = "info", f"Проверь, что на плате не меньше {n} длинных слотов PCIe"
    elif n > x16:
        status = "fail"
        summary = f"На плате {x16} длинных слота, а видеокарт {n}"
        fix = _fix(plan, "board", "x99", "Взять плату X99 с тремя слотами") or \
            _fix(plan, "case", "open_frame", "Открытый стенд с райзерами")
    else:
        status = "ok"
        summary = f"Слотов хватает: {n} из {x16}" if n > 1 else "Карта встаёт в основной слот x16"
    if n > 1:
        lines.append(f"{board.part.label}: {b.get('lanes_ru', '')}")
        lines.append("Для работы нейросети по слоям (LM Studio, llama.cpp) карте хватает x4 — модель только дольше "
                     "загружается. Для обучения и vLLM с разделением тензоров лучше x8/x8 (X570, X99)")
    if part.specs.get("pcie_lanes") == 8:
        lines.append(f"{part.label} сама работает на x8 — в слоте от чипсета получит x4")
    out = [_check("pcie", status, "Слоты PCIe", summary, lines, fix)]
    if n >= 2:
        width = int(part.specs.get("slot_width") or 2)
        if open_frame:
            out.append(_check("spacing", "ok", "Расстояние между картами",
                              "Открытый стенд с райзерами — расстояние между слотами не важно"))
        elif width <= 2:
            out.append(_check("spacing", "ok", "Расстояние между картами",
                              f"Карты по {width} слота — встанут рядом в обычный корпус",
                              ["Между картами желательно оставить слот для воздуха"]))
        else:
            slots = (case.part.specs.get("expansion_slots") if case and case.part else None) or 7
            lines = [f"Каждая {part.label} занимает ~{width} слота: между длинными слотами на плате нужно "
                     f"не меньше {width} позиций (лучше {width + 1} — для воздуха)",
                     "Если нижний длинный слот — последний на плате, корпусу нужно 8+ слотов расширения",
                     "Иначе — вертикальный райзер или открытый стенд"]
            if slots >= 8:
                out.append(_check("spacing", "info", "Расстояние между картами",
                                  f"Две толстые карты: в корпусе {slots} слотов — влезут, проверь расстояние на плате",
                                  lines))
            else:
                out.append(_check("spacing", "warn", "Расстояние между картами",
                                  f"Две карты по {width} слота в корпус на {slots} слотов — скорее всего не влезут",
                                  lines, _fix(plan, "case", "case_big", "Взять большой корпус (8+ слотов)")
                                  or _fix(plan, "case", "open_frame", "Открытый стенд с райзерами")))
    return out


def _socket_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    cpu, board = r.get("cpu"), r.get("board")
    if board is None or board.part is None:
        return None
    if board.part.specs.get("cpu_included"):
        return _check("socket", "ok", "Процессор и плата", "Процессор уже распаян на плате")
    if cpu is None or not cpu.active or cpu.part is None:
        return None
    cs, bs = cpu.part.specs.get("socket"), board.part.specs.get("socket")
    if cs == bs:
        return _check("socket", "ok", "Процессор и плата", f"{cpu.part.label} подходит к плате ({cs})")
    board_slot = plan.slot("board")
    alt_board = next((o.key for o in (board_slot.options if board_slot else [])
                      if (p := kb.part(o.kb_key)) is not None and p.specs.get("socket") == cs), None)
    fix = _fix(plan, "board", alt_board, f"Взять {_label(alt_board)}")
    return _check("socket", "fail", "Процессор и плата", f"{cpu.part.label} ({cs}) не встанет в плату под {bs}",
                  ["Сокет процессора и платы должен совпадать"], fix)


def _ram_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    ram, board = r.get("ram"), r.get("board")
    if ram is None or ram.part is None or board is None or board.part is None:
        return None
    s, b = ram.part.specs, board.part.specs
    gen = s["gen"].upper()
    types = b.get("ram_types") or ()
    lines: list[str] = []
    req = plan.requirements
    if req.kind == "llm" and req.model_size_b:
        cards = gpu_cards(r)
        have = sum(p.specs["vram_gb"] * n for p, n in cards)
        mem = kb.llm_memory(req.model_size_b, req.quant, req.context, max(1, cards[0][1] if cards else 1))
        if mem.total_gib <= have:
            lines.append(f"Модель целиком в видеопамяти — {s['size_gb']} ГБ оперативной памяти хватает")
        else:
            lines.append(f"Часть модели не влезет в видеопамять: оперативной памяти нужно не меньше "
                         f"~{fmt_int(mem.total_gib - have + 8)} ГБ")
    if not types:
        return _check("ram", "info", "Оперативная память", f"Тип памяти зависит от модели платы — проверь, что "
                      f"нужна {gen}{' SO-DIMM' if s['sodimm'] else ''}",
                      [*lines, "У плат с N100 бывает DDR4 или DDR5 SO-DIMM — посмотри в описании платы и выбери нужный "
                               "вариант памяти"] if b.get("cpu_included") else lines)
    alt = _first_ok_ram(plan, board.part)
    fix = _fix(plan, "ram", alt, f"Взять {_label(alt)}")
    if s["gen"] not in types:
        need = "/".join(t.upper() for t in types)
        return _check("ram", "fail", "Оперативная память", f"Плате нужна {need}, а выбрана {gen}", lines, fix)
    if s["registered"] and not b.get("rdimm"):
        return _check("ram", "fail", "Оперативная память",
                      f"Серверная память ECC REG (RDIMM) не заработает на {board.part.label}",
                      [f"Нужна обычная {gen} (UDIMM)", *lines], fix)
    if bool(s["sodimm"]) != bool(b.get("sodimm")):
        want = "ноутбучная SO-DIMM" if b.get("sodimm") else "обычная DIMM"
        return _check("ram", "fail", "Оперативная память", f"Плате нужна {want} память", lines, fix)
    if not s["registered"] and b.get("rdimm"):
        lines.append("Обычная память тоже подойдёт, но серверная ECC REG обычно дешевле")
    return _check("ram", "ok", "Оперативная память",
                  f"{ram.part.label} подходит к плате ({gen}{', ECC REG' if s['registered'] else ''})", lines)


def _display_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    cards = gpu_cards(r)
    if not cards:
        return None
    part, _ = cards[0]
    display = part.specs.get("display")
    cpu, board = r.get("cpu"), r.get("board")
    igpu = bool((cpu and cpu.active and cpu.part and cpu.part.specs.get("igpu"))
                or (board and board.part and board.part.specs.get("igpu")))
    if display is True:
        return None
    lines = ["Экран нужен хотя бы для установки системы и настройки BIOS (Above 4G Decoding)"]
    if igpu:
        who = cpu.part.label if cpu and cpu.part and cpu.part.specs.get("igpu") else "платы"
        return _check("display", "ok", "Видеовыход", f"Монитор подключается к встроенной графике ({who})", lines)
    lines.append("Решения: процессор со встроенной графикой (Ryzen 5 5600G) или любая дешёвая видеокарта на время "
                 "настройки")
    fix = _fix(plan, "cpu", "r5_5600g", "Взять Ryzen 5 5600G (есть встроенная графика)") \
        if board and board.part and board.part.specs.get("socket") == "AM4" else None
    cpu_label = cpu.part.label if cpu and cpu.part else "процессора"
    if display is False:
        return _check("display", "warn", "Видеовыход", f"Некуда подключить монитор: у {part.label} нет видеовыхода, "
                      f"у {cpu_label} — встроенной графики", lines, fix)
    return _check("display", "info", "Видеовыход", f"Видеовыход у {part.label} часто не работает, а у {cpu_label} "
                  "нет встроенной графики", lines, fix)


def _cooling_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    cards = gpu_cards(r)
    if not cards or cards[0][0].specs.get("cooling") != "passive":
        return None
    part, n = cards[0]
    slot = r.get("gpu_cooling")
    lines = [f"Без обдува {part.label} перегреется за минуту: турбину 40–97 мм с переходником крепят к торцу карты",
             "Переходник часто печатают на 3D-принтере или берут на eBay/AliExpress"]
    if slot is None or slot.slot.status == "skipped":
        return _check("cooling", "fail", "Охлаждение серверных карт", "Для пассивных карт нужны вентиляторы", lines)
    return _check("cooling", "ok", "Охлаждение серверных карт",
                  f"Добавлены вентиляторы-турбины: {n} шт." if n > 1 else "Добавлен вентилятор-турбина", lines)


def _power_connectors(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    cards = gpu_cards(r)
    if not cards:
        return None
    part, n = cards[0]
    power = part.specs.get("power")
    lines: list[str] = []
    if power:
        lines.append(f"{part.label}: {power}" + (f" — на каждую из {n} карт" if n > 1 else ""))
    else:
        lines.append(f"{part.label}: разъёмы питания у разных версий разные — посмотри на фото в объявлении")
    if part.specs.get("server"):
        lines.append("В BIOS включи Above 4G Decoding (и Resizable BAR, если есть) — иначе система не увидит карту")
    return _check("power", "info", "Разъёмы питания", "Проверь разъёмы у блока питания", lines)


def _capacity_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    hdd = r.get("hdd")
    if hdd is None or hdd.part is None:
        return None
    size = float(hdd.part.specs["size_tb"])
    usable = size * hdd.qty / 2 if hdd.qty >= 2 else size
    want = plan.requirements.storage_tb or usable
    lines = [f"{hdd.qty} × {fmt_gb(size)} ТБ зеркалом (RAID1 / зеркало ZFS): полезных {fmt_gb(usable)} ТБ — "
             "один диск может умереть без потери данных" if hdd.qty >= 2 else
             f"Один диск на {fmt_gb(size)} ТБ: если он умрёт, данные пропадут — лучше два зеркалом"]
    status = "ok" if usable >= want else "fail"
    summary = (f"Полезных {fmt_gb(usable)} ТБ ≥ нужно {fmt_gb(want)} ТБ" if status == "ok"
               else f"Полезных {fmt_gb(usable)} ТБ — меньше нужных {fmt_gb(want)} ТБ")
    return _check("capacity", status, "Место на дисках", summary, lines)


def _sata_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    board, hdd, boot, hba = r.get("board"), r.get("hdd"), r.get("boot"), r.get("hba")
    if board is None or board.part is None or hdd is None:
        return None
    need = hdd.qty + (1 if boot and boot.part and boot.part.specs.get("type") == "ssd" else 0)
    ports = board.part.specs.get("sata_ports")
    if ports is None:
        return _check("sata", "info", "SATA-порты", f"Нужно {need} SATA-порта — проверь, сколько их у платы",
                      ["У плат с N100 бывает от 2 до 6 портов; не хватит — поможет контроллер LSI 9211-8i"])
    if hba is not None and hba.active:
        return _check("sata", "ok", "SATA-порты", f"На плате {ports}, нужно {need} — добавлен контроллер на 8 портов")
    return _check("sata", "ok", "SATA-порты", f"Нужно {need} из {ports} портов на плате")


def _electricity_check(plan: Plan, r: dict[str, RSlot]) -> dict[str, Any] | None:
    board, hdd = r.get("board"), r.get("hdd")
    if board is None or board.part is None:
        return None
    idle = float(board.part.specs.get("idle_w") or 25)
    drives = hdd.qty if hdd and hdd.part else 0
    total = idle + 5 * drives
    kwh = total * 24 * 365 / 1000
    cost = kwh * ELECTRICITY_EUR_KWH
    return _check("electricity", "info", "Электричество", f"≈ {fmt_int(total)} Вт круглосуточно ≈ {fmt_int(cost)} € в год",
                  [f"Плата и процессор ~{fmt_int(idle)} Вт + {drives} × ~5 Вт диски = {fmt_int(total)} Вт",
                   f"{fmt_int(total)} Вт × 24 ч × 365 = {fmt_int(kwh)} кВт·ч × {str(ELECTRICITY_EUR_KWH).replace('.', ',')} € "
                   f"≈ {fmt_int(cost)} € в год (примерно)"])


# ========================================================================= LLM
PLAN_SYSTEM = (
    "You help a student in Germany plan a computer build from USED parts bought on Kleinanzeigen and eBay.\n"
    "Answer with ONE JSON object only, no other text.\n"
    "Rules:\n"
    "- Pick options ONLY by the keys listed for each slot. Never invent parts, never change numbers.\n"
    "- Never write prices: prices come from real ads, not from you.\n"
    "- If the goal clearly needs something that is in no slot (e.g. a monitor), put it into extra_items with a short "
    "German/English search phrase as sellers write it on Kleinanzeigen.\n"
    "- All explanations in Russian, short (max 20 words each), friendly, informal «ты».\n"
    "JSON shape: {\"template\": one of the template keys or null, \"model_size_b\": number or null, "
    "\"quant\": one of the quant keys or null, \"context_tokens\": integer or null, "
    "\"choices\": {\"<slot key>\": \"<option key>\"}, \"reasons\": {\"<slot key>\": \"<why, Russian>\"}, "
    "\"extra_items\": [{\"label\": \"<Russian name>\", \"query\": \"<search phrase>\", \"why\": \"<Russian>\"}], "
    "\"summary\": \"<2 sentences in Russian: what to build and the main trade-off>\"}"
)


def plan_prompt(plan: Plan, market: Any) -> str:
    lines = [f"Goal (Russian): {plan.goal or '—'}",
             f"Budget: {str(round(plan.budget)) + ' EUR' if plan.budget else 'not given'}",
             "Templates: " + ", ".join(f"{k} = {t.title}" for k, t in kb.TEMPLATES.items()),
             f"Current template: {plan.template}",
             "Quant keys: " + ", ".join(kb.QUANTS)]
    req = plan.requirements
    if req.model_size_b:
        lines.append(f"Parsed: model {fmt_gb(req.model_size_b)}B, quant {kb.quant(req.quant).label}, "
                     f"context {req.context or kb.DEFAULT_CONTEXT}")
    r = resolve(plan)
    lines.append("Slots and option keys (rough used price in EUR from our own data, for budget only):")
    for slot in plan.slots:
        rs = r[slot.key]
        if not rs.active or not slot.options:
            continue
        opts = []
        for opt in slot.options:
            part = kb.part(opt.kb_key)
            if part is None:
                opts.append(f"{opt.key} ({opt.label})")
                continue
            price = market.price(part).typical
            specs = _spec_line(part)
            cost = f", ~{fmt_int(price * opt.qty)} EUR" if price else ""
            risk = ", needs tinkering" if part.risk >= 0.3 else ""
            opts.append(f"{opt.key} ({_option_label(opt)}; {specs}{cost}{risk})")
        lines.append(f"- {slot.key} [{slot.label}], now {slot.chosen}: " + "; ".join(opts))
    return "\n".join(lines)


def _spec_line(part: Part) -> str:
    s = part.specs
    if part.kind == "gpu":
        return f"{s['vram_gb']} GB, {s['bandwidth_gbs']} GB/s, {s['tdp_w']} W, {s['stack']}"
    if part.kind == "cpu":
        return f"{s['socket']}, {s['cores']} cores, iGPU {'yes' if s['igpu'] else 'no'}"
    if part.kind == "board":
        return f"{s['socket']}, {'/'.join(s.get('ram_types') or ()) or 'RAM varies'}"
    if part.kind == "ram":
        return f"{s['gen']} {s['size_gb']} GB{' ECC REG' if s['registered'] else ''}"
    if part.kind == "psu":
        return f"{s['watts']} W"
    if part.kind == "storage":
        return f"{s['type']} {s['size_tb']} TB"
    return part.kind


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a model answer (code fences / <think> blocks tolerated)."""
    body = _THINK_RE.sub("", text or "").strip()
    candidates = [m.strip() for m in _FENCE_RE.findall(body)] + [body]
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = cand.find("{")
        while start >= 0:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    esc = (ch == "\\") and not esc
                    if ch == '"' and not esc:
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", cand[start:i + 1]))
                            if isinstance(obj, dict):
                                return obj
                        except ValueError:
                            pass
                        break
            start = cand.find("{", start + 1)
    return None


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\u0000", "").split())
    text = re.sub(r"https?://\S+", "", text)
    return text[:limit].strip()


_QUERY_OK_RE = re.compile(r"[^\w\s.+\-/]", re.UNICODE)


def apply_ai_answer(plan: Plan, answer: dict[str, Any], market: Any, *, explicit_template: bool,
                    req: PlanRequest) -> Plan:
    """Merge the LLM's answer into the plan: only known template/quant keys and option keys of
    the plan's slots are accepted; free-form items become their own slots (no price). Numbers
    the user wrote win over the model's."""
    applied: dict[str, str] = {}
    ignored: list[str] = []
    tkey = answer.get("template")
    info = merged_info(req)
    changed = False
    if isinstance(answer.get("model_size_b"), (int, float)) and not info.model_size_b \
            and 0.5 <= float(answer["model_size_b"]) <= 2000:
        req = req.model_copy(update={"model_size_b": float(answer["model_size_b"])})
        changed = True
    quant = kb.normalize_quant(answer.get("quant")) if isinstance(answer.get("quant"), str) else None
    if quant and not info.quant:
        req = req.model_copy(update={"quant": quant})
        changed = True
    ctx = answer.get("context_tokens")
    if isinstance(ctx, int) and not info.context and 512 <= ctx <= 1_000_000:
        req = req.model_copy(update={"context": ctx})
        changed = True
    # the model may only pick the template when the rules were unsure (a custom list), or between
    # the LLM templates when the goal named no model size / memory
    rules_kind = kb.TEMPLATES[plan.template].kind
    new = kb.TEMPLATES.get(tkey) if isinstance(tkey, str) else None
    allowed = new is not None and not explicit_template and tkey != plan.template and (
        rules_kind == "custom" or (rules_kind == new.kind == "llm" and not info.model_size_b and not info.vram_gb))
    if allowed:
        changed = True
    else:
        tkey = plan.template
    if changed:
        plan = build_plan(req, market, template_key=tkey)
    raw_choices, raw_reasons = answer.get("choices"), answer.get("reasons")
    choices: dict[str, Any] = raw_choices if isinstance(raw_choices, dict) else {}
    reasons: dict[str, Any] = raw_reasons if isinstance(raw_reasons, dict) else {}
    for slot_key, opt_key in choices.items():
        slot = plan.slot(str(slot_key))
        if slot is None or not isinstance(opt_key, str) or slot.option(opt_key) is None:
            ignored.append(f"{slot_key}={opt_key}")
            continue
        if slot.chosen != opt_key:
            slot.chosen = opt_key
            applied[slot.key] = opt_key
    if applied:
        apply_dependents(plan, keep=set(applied), ideal=True)  # still planning: the rule's best fit for the pick
    for slot_key, why in reasons.items():
        slot = plan.slot(str(slot_key))
        text = _clean_text(why, 200)
        if slot is not None and text and slot.option() is not None:
            slot.option().why = text  # type: ignore[union-attr]
    raw_extra = answer.get("extra_items")
    extra: list[Any] = raw_extra if isinstance(raw_extra, list) else []
    existing = {s.key for s in plan.slots}
    added = 0
    for item in extra:
        if added >= MAX_AI_ITEMS or not isinstance(item, dict):
            continue
        label = _clean_text(item.get("label"), 60)
        query = _QUERY_OK_RE.sub(" ", _clean_text(item.get("query"), 60)).strip()
        if not label or not query:
            continue
        added += 1
        key = f"ai{added}"
        while key in existing:
            key += "x"
        existing.add(key)
        plan.slots.append(PlanSlot(key=key, label=label, kind="generic", chosen="main", options=[
            PlanOption(key="main", label=label, query=query, source="ai", why=_clean_text(item.get("why"), 200))]))
    summary = _clean_text(answer.get("summary"), 400)
    plan.ai = {"used": True, "ok": True, "summary_ru": summary, "applied": applied, "ignored": ignored[:10],
               "extra_items": added, "message_ru": "Нейросеть помогла выбрать варианты — цены взяты из объявлений"}
    return plan


async def plan_goal(req: PlanRequest, market: Any = None, *, llm: Any = None, model_name: str = "",
                    ai_off_reason: str = "") -> Plan:
    """Rules first; then (when `llm` is given and req.use_ai) one text-only JSON call to refine."""
    market = market or KbMarket()
    plan = build_plan(req, market)
    if not req.use_ai:
        plan.ai = {"used": False, "ok": None, "message_ru": "План составлен по правилам, без нейросети"}
        return plan
    if llm is None:
        plan.ai = {"used": False, "ok": None,
                   "message_ru": ai_off_reason or "Нейросеть выключена — план составлен по правилам"}
        return plan
    try:
        raw = await llm.chat_json(PLAN_SYSTEM, plan_prompt(plan, market))
    except Exception as exc:  # noqa: BLE001 - the plan works without the AI
        log.info("Planner LLM failed: %s", exc)
        plan.ai = {"used": True, "ok": False, "model": model_name,
                   "message_ru": "Нейросеть не ответила — план составлен по правилам"}
        return plan
    answer = parse_json_object(raw if isinstance(raw, str) else "")
    if answer is None:
        plan.ai = {"used": True, "ok": False, "model": model_name,
                   "message_ru": "Нейросеть ответила непонятно — план составлен по правилам"}
        return plan
    plan = apply_ai_answer(plan, answer, market, explicit_template=bool(req.template and req.template in kb.TEMPLATES),
                           req=req)
    if plan.ai is not None:
        plan.ai["model"] = model_name
    return plan


__all__ = [
    "CustomItem", "GoalInfo", "PlanRequest", "RSlot", "apply_ai_answer", "apply_dependents", "build_plan",
    "build_total", "choose_defaults", "compat_checks", "gpu_candidates", "gpu_cards", "gpu_option_key",
    "merged_info", "option_label", "parse_goal", "parse_json_object", "pick_candidate", "pick_template",
    "plan_goal", "resolve", "search_words", "split_items", "unit_price", "vram_status",
]
