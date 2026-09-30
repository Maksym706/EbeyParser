"""Request bodies (strict: unknown fields are an error) and the main response shapes of
/api/v1. Response models are documentation for the UI (see /api/docs); nested objects that
mirror config sections are plain dicts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="allow")


# ------------------------------------------------------------------ common
class OkOut(Out):
    ok: bool = True
    message_ru: str = ""


class ErrorInfo(BaseModel):
    code: str
    message_ru: str
    fields: dict[str, str] | None = None
    details: str | None = None  # technical text for «Подробнее» (collapsed)
    action: dict[str, str] | None = None  # {"label_ru", "href"}: the screen that fixes it


class ErrorOut(BaseModel):
    error: ErrorInfo


ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorOut, "description": "Неверный запрос"},
    403: {"model": ErrorOut, "description": "Только чтение / чужой сайт"},
    404: {"model": ErrorOut, "description": "Не найдено"},
    422: {"model": ErrorOut, "description": "Ошибки в полях: error.fields = {поле: сообщение}"},
}

StepKey = Literal["location", "categories", "money", "wishlist", "ai", "telegram", "ebay"]
ApiStatus = Literal["new", "starred", "contacted", "bought", "sold", "ignored"]


# -------------------------------------------------------------- app / onboarding
class OnboardingStep(Out):
    key: str
    title_ru: str
    required: bool
    done: bool
    skipped: bool


class OnboardingOut(Out):
    steps: list[OnboardingStep]
    completed_at: str | None
    next_step: str | None
    required_done: bool


class AppOut(Out):
    version: str
    onboarded: bool
    config_exists: bool
    config_writable: bool
    config_path: str | None
    onboarding: OnboardingOut
    first_run: dict[str, Any]
    features: dict[str, bool]
    demo: dict[str, Any]
    counts: dict[str, int]
    monitor: dict[str, Any]
    access: dict[str, Any]
    server_time: str


class OnboardingSkipIn(ApiModel):
    step: StepKey
    skipped: bool = True


class OnboardingCompleteIn(ApiModel):
    completed: bool = True


# ---------------------------------------------------------------- setup
class WishItem(ApiModel):
    item: str = Field(min_length=1, max_length=80)
    max_price: float | None = Field(default=None, ge=0)
    haggle: bool = True  # also show ads up to +20 % (for haggling)


class PricingPreset(ApiModel):
    min_profit: float | None = Field(default=None, ge=0)
    min_roi: float | None = Field(default=None, ge=0, le=10)
    safety_margin_percent: float | None = Field(default=None, ge=0, le=50)
    min_comparables: int | None = Field(default=None, ge=1, le=100)


class SetupIn(ApiModel):
    location: str = Field(default="", max_length=80)
    radius_km: int = Field(default=30, ge=0, le=500)
    category_ids: list[int] = Field(default_factory=list)
    category_names: dict[str, str] = Field(default_factory=dict)  # {"234": "Sammeln"} for live-only categories
    purpose: Literal["resale", "personal", "both"] = "resale"
    max_price: float | None = Field(default=400.0, ge=0)
    min_profit: float | None = Field(default=None, ge=0)
    strategy: Literal["careful", "balanced", "aggressive", "custom"] | None = None
    pricing: PricingPreset | None = None
    notify_min_score: float | None = Field(default=None, ge=0, le=100)
    wishlist: list[WishItem] = Field(default_factory=list, max_length=30)
    interval_minutes: float | None = Field(default=None, ge=5, le=1440)
    # False (default): new searches are added / same-named ones updated, nothing is removed.
    # True: the answers replace every current search (the response lists them in `replaced`).
    replace: bool = False
    set_max_capital: bool = True
    start_run: bool = False
    complete_onboarding: bool = False


class EstimateOut(Out):
    interval_minutes: float
    suggested_interval: int
    category_scans: int
    keyword_searches: int
    pages_per_hour: int
    requests_per_hour: int
    requests_per_day: int
    cap_per_hour: int
    load_percent: int
    evaluation_per_hour: int | None
    tight: bool
    level: Literal["ok", "warn", "danger"]
    text_ru: str  # the full explanation (for «Как это работает?»)
    short_ru: str = ""  # «Безопасно — блокировки маловероятны»
    pages_label_ru: str = ""  # «~20 из 150 страниц в час»
    searches: int = 0


# ------------------------------------------------------------- settings
class SecretsIn(ApiModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    notify_email: str | None = None
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    ebay_oauth_token: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    nvidia_api_key: str | None = None
    omniroute_api_key: str | None = None
    cloud_api_key: str | None = None


class AccessIn(ApiModel):
    mode: Literal["local", "tailscale", "lan"]
    host: str | None = Field(default=None, max_length=100)  # tailscale: the 100.x address (auto-detected if empty)
    allowed_hosts: list[str] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)


class ConfirmIn(ApiModel):
    confirm: str = ""


# ------------------------------------------------------------ connections
class AiTestIn(ApiModel):
    provider: Literal["openai", "ollama", "anthropic"] | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    sample: bool = True  # run the model on the bundled sample ad (photo + text), not just "is it there"
    timeout_seconds: float = Field(default=180.0, ge=5, le=900)
    save: bool = False  # on success write provider/base_url/model (and enable the AI)
    # "fallback": the local model is the stand-in of a cloud endpoint (ai.fallback), the cloud stays
    save_as: Literal["main", "fallback"] = "main"


CloudProviderIn = Literal["openrouter", "nvidia", "omniroute", "custom"]


class CloudTestIn(ApiModel):
    provider: CloudProviderIn = "openrouter"
    api_key: str | None = Field(default=None, max_length=400)  # empty: the saved one
    base_url: str | None = Field(default=None, max_length=300)  # empty: the provider's address
    text_model: str | None = Field(default=None, max_length=200)  # empty: the recommended one
    vision_model: str | None = Field(default=None, max_length=200)
    triage: bool = True  # 5 demo ads through the scout (latency, quality)
    photo: bool = False  # + one demo photo through the vision model
    save_key: bool = False  # the key works -> store it in .env
    timeout_seconds: float = Field(default=90.0, ge=5, le=600)


class CloudFallbackIn(ApiModel):
    provider: Literal["openai", "ollama"] = "openai"
    base_url: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=200)
    text_model: str = Field(default="", max_length=200)  # the scout's local model ("" = model)


class CloudSaveIn(ApiModel):
    mode: Literal["cloud", "hybrid", "local"]
    provider: CloudProviderIn = "openrouter"
    api_key: str | None = Field(default=None, max_length=400)
    base_url: str | None = Field(default=None, max_length=300)
    text_model: str | None = Field(default=None, max_length=200)
    vision_model: str | None = Field(default=None, max_length=200)
    rpm: int | None = Field(default=None, ge=0, le=100000)
    daily_limit: int | None = Field(default=None, ge=0, le=10000000)
    fallback: CloudFallbackIn | None = None
    send_ebay: bool | None = None
    scout: bool = True  # the scout reads ads with text_model


class TelegramTokenIn(ApiModel):
    token: str | None = None  # default: the saved one
    save: bool = False  # valid -> store in .env


class TelegramTestIn(ApiModel):
    token: str | None = None
    chat_id: str | None = None
    with_deal: bool = True  # a sample deal with photo (else a plain text message)


class EmailTestIn(ApiModel):
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    use_ssl: bool | None = None
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    to_addrs: list[str] | str | None = None
    save: bool = False  # delivered -> store (password in .env) and enable e-mail


class EbayTestIn(ApiModel):
    client_id: str | None = None
    client_secret: str | None = None
    oauth_token: str | None = None
    sandbox: bool | None = None
    marketplace_id: str | None = None
    save: bool = False  # valid -> store the keys in .env


class NotifyTestIn(ApiModel):
    channel: Literal["telegram", "email"] | None = None


# ------------------------------------------------------------------ deals
class DealPatchIn(ApiModel):
    status: ApiStatus | None = None
    note: str | None = Field(default=None, max_length=5000)
    bought_price: float | None = None  # checked in the route: clear messages, never clamped
    bought_at: datetime | None = None
    sold_price: float | None = None
    sold_at: datetime | None = None
    extra_costs: float | None = None
    hidden_reason: str | None = Field(default=None, max_length=200)


class SeenIn(ApiModel):
    ids: list[str] = Field(default_factory=list, max_length=1000)


class CheckIn(ApiModel):
    url: str = Field(min_length=8, max_length=2000)
    purpose: Literal["resale", "personal"] = "resale"
    target_price: float | None = Field(default=None, ge=0)


class ReevaluateIn(ApiModel):
    refetch: bool = False  # open the ad page again (current price, photos)


class DealsPage(Out):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    page: int
    pages: int
    next_cursor: str | None
    facets: dict[str, Any] | None = None


class JobOut(Out):
    id: str
    kind: str
    status: Literal["queued", "running", "done", "error"]
    stage: str
    stage_ru: str
    stages: list[dict[str, Any]]
    ad_id: str | None
    error: dict[str, str] | None
    result: dict[str, Any] | None


class JobAccepted(Out):
    job: JobOut
    poll_url: str


__all__ = [name for name in dir() if name[0].isupper()]
