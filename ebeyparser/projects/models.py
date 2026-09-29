"""Data of «Сборки» (build projects): what the user wants, the parts plan (slots with
alternatives) and what was bought. Prices, checks and offers are NOT stored here — they are
computed on every read from the knowledge base, the price history and the current ads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import utcnow

ProjectStatus = Literal["draft", "tracking", "paused", "done"]
SlotStatus = Literal["open", "bought", "have", "skipped"]
OptionSource = Literal["kb", "ai", "user"]

PROJECT_STATUS_LABELS = {"draft": "Черновик", "tracking": "Отслеживаю", "paused": "На паузе", "done": "Собрано"}
SLOT_STATUS_LABELS = {"open": "Нужно купить", "bought": "Куплено", "have": "Уже есть", "skipped": "Не нужно"}
OPTION_SOURCE_LABELS = {"kb": "Из базы знаний", "ai": "Предложила нейросеть", "user": "Добавлено тобой"}


class Requirements(BaseModel):
    """What the build must do (parsed from the goal, the template or the LLM)."""

    kind: Literal["llm", "nas", "gaming", "custom"] = "custom"
    model_size_b: float | None = None  # LLM: billions of parameters
    active_b: float | None = None  # MoE: active parameters per token
    quant: str | None = None  # knowledge.QUANTS key
    context: int | None = None  # tokens
    vram_gb: float | None = None  # explicit / template video memory target
    storage_tb: float | None = None  # NAS: usable space
    drives: int | None = None  # NAS: number of disks


class PlanOption(BaseModel):
    """One alternative of a slot: a knowledge-base part × qty, or a free-form item."""

    key: str
    kb_key: str | None = None
    qty: int = 1
    label: str = ""  # free-form items (the knowledge base labels its own parts)
    query: str = ""  # free-form search phrase (Kleinanzeigen)
    why: str = ""  # short reason (rules or the LLM)
    source: OptionSource = "kb"
    target_price: float | None = None  # per unit; set by the user (override) or frozen when tracking starts
    max_price: float | None = None  # per unit, the search's upper limit
    target_by: Literal["auto", "user"] = "auto"  # "user": never recomputed from the budget split


class Purchase(BaseModel):
    price: float  # total paid for `qty` units (incl. shipping)
    qty: int = 1
    ad_id: str | None = None
    option: str | None = None
    note: str = ""
    at: datetime = Field(default_factory=utcnow)


class PlanSlot(BaseModel):
    key: str
    label: str
    kind: str = "generic"
    when: str | None = None  # condition (planner.CONDITIONS); inactive slots are not needed
    per_gpu: bool = False
    hint: str = ""
    chosen: str | None = None
    options: list[PlanOption] = Field(default_factory=list)
    status: SlotStatus = "open"
    note: str = ""
    purchases: list[Purchase] = Field(default_factory=list)

    def option(self, key: str | None = None) -> PlanOption | None:
        wanted = key if key is not None else self.chosen
        return next((o for o in self.options if o.key == wanted), None)


class Plan(BaseModel):
    """A parts plan (what POST /projects/plan returns, what a project stores)."""

    name: str = ""
    goal: str = ""
    template: str = "custom"
    budget: float | None = None
    requirements: Requirements = Field(default_factory=Requirements)
    slots: list[PlanSlot] = Field(default_factory=list)
    location: str = ""
    radius_km: int | None = None
    ai: dict[str, Any] | None = None  # {"used", "ok", "model", "summary_ru", "message_ru"}
    notes: list[str] = Field(default_factory=list)  # planner remarks (Russian)

    def slot(self, key: str) -> PlanSlot | None:
        return next((s for s in self.slots if s.key == key), None)


class Project(Plan):
    id: int = 0
    status: ProjectStatus = "draft"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    tracking_since: datetime | None = None
    fits_alerted: bool = False  # the «whole build fits the budget» alert was sent (reset when it no longer fits)


class SearchLink(BaseModel):
    project_id: int
    slot: str
    option: str
    search_name: str
    created_at: datetime = Field(default_factory=utcnow)


class AlertRecord(BaseModel):
    project_id: int
    ad_id: str
    kind: Literal["slot_target", "budget_fit"]
    slot: str = ""
    price: float | None = None
    total: float | None = None
    text: str = ""
    delivered: bool = False
    sent_at: datetime = Field(default_factory=utcnow)


__all__ = [
    "AlertRecord", "OPTION_SOURCE_LABELS", "PROJECT_STATUS_LABELS", "Plan", "PlanOption", "PlanSlot", "Project",
    "ProjectStatus", "Purchase", "Requirements", "SLOT_STATUS_LABELS", "SearchLink", "SlotStatus",
]
