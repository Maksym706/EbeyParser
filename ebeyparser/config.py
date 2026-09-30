"""App configuration: a YAML file (config.yaml) with ${ENV_VAR} substitution.

Secrets (SMTP password, Telegram token) can live in environment variables or a
.env file next to the config; reference them in YAML as "${SMTP_PASSWORD}".
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .models import Purpose, Source

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.yaml")
# "reference_prices:[]" / "model:qwen" — a missing space after the key's colon
_MISSING_SPACE_RE = re.compile(r"^(\s*(?:-\s+)?[A-Za-z_][\w-]*):(?=[^\s/])", re.MULTILINE)


class ConfigError(Exception):
    """config.yaml can't be read; the message says where and why, in plain Russian."""
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class SearchConfig(BaseModel):
    """One thing to monitor.

    Kleinanzeigen: either paste a ready search `url` (set region/category/price
    filters on the website and copy the address), or describe it with
    `query` + `location` + `radius_km` + `category_id`.
    eBay (official Browse API): `query` + optional `ebay_category_ids`,
    price range, `buying_options`, and `location`/`radius_km` for local pickup."""

    name: str
    source: Source = "kleinanzeigen"
    enabled: bool = True
    url: str | None = None
    query: str = ""
    location: str = ""  # city or postal code, e.g. "Berlin" or "10115"
    location_id: int | None = None  # Kleinanzeigen internal id (optional)
    radius_km: int | None = None
    category_id: int | None = None  # e.g. 225 = "PC-Zubehör & Software"
    category_name: str = ""  # display only
    min_price: float | None = None
    max_price: float | None = None
    purpose: Purpose = "resale"
    include_keywords: list[str] = Field(default_factory=list)  # at least one must match
    exclude_keywords: list[str] = Field(default_factory=list)  # none may match
    reference_price: float | None = None  # known typical resale price for this search
    target_price: float | None = None  # personal: max price you are happy to pay
    min_profit: float | None = None  # override pricing.min_profit
    min_roi: float | None = None  # override pricing.min_roi
    max_pages: int | None = None  # override general.max_pages
    # eBay only
    ebay_category_ids: list[str] = Field(default_factory=list)  # e.g. ["27386"] = Grafikkarten
    buying_options: list[Literal["FIXED_PRICE", "AUCTION", "BEST_OFFER"]] = Field(default_factory=list)
    ebay_conditions: list[Literal["NEW", "USED", "UNSPECIFIED"]] = Field(default_factory=list)
    local_pickup_only: bool = False  # only items you can pick up within radius_km of `location` (postal code)
    ending_within_hours: float | None = None  # auctions ending soon (the classic cheap-auction trick)


class ReferencePrice(BaseModel):
    """Known market price for items whose title matches all `keywords`."""

    keywords: list[str]
    price: float
    exclude: list[str] = Field(default_factory=list)


class GeneralConfig(BaseModel):
    interval_minutes: float = 15
    max_pages: int = 1
    fetch_details: bool = True  # open every new ad to get full text + all photos
    request_delay_seconds: tuple[float, float] = (3.0, 7.0)
    request_timeout_seconds: float = 25.0
    user_agent: str | None = None
    data_dir: str = "data"
    max_new_per_search: int = 25  # cap work per search per run
    # Funnel budgets per monitoring pass (category scans see many ads; only candidates get the
    # expensive steps). Comparables lookups and ad pages hit the site; AI calls cost time.
    max_comps_lookups_per_run: int = 40
    max_details_per_run: int = 40
    max_ai_per_run: int = 30
    min_listing_price: float = 10.0  # ignore cheaper paid ads (junk); free ads are still considered
    # Ban protection: hard cap of HTML pages per hour per site, and a growing, persisted cooldown
    # after 403/429/captcha (hours for the 1st, 2nd, 3rd, ... block in a row).
    max_requests_per_hour: int = 150
    block_cooldown_hours: list[float] = Field(default_factory=lambda: [1.0, 2.0, 4.0, 12.0])
    baseline_first_run: bool = True  # first pass of a NEW search only learns prices, no alerts
    # the user's time zone: every time in messages / the web UI / the log ("пауза до 21:34")
    timezone: str = "Europe/Berlin"

    @field_validator("request_delay_seconds", mode="before")
    @classmethod
    def _delay(cls, v: Any) -> Any:
        if isinstance(v, (int, float)):
            return (float(v), float(v))
        return v


class PricingConfig(BaseModel):
    min_profit: float = 40.0  # EUR net profit for a "buy"
    min_roi: float = 0.25  # 25% return on the buy price
    selling_fee_percent: float = 0.0  # private sellers on eBay.de / Kleinanzeigen pay 0 %
    payment_fee_percent: float = 0.0  # e.g. 2.49 for PayPal goods & services
    default_shipping_cost: float = 0.0  # your cost to ship when reselling
    safety_margin_percent: float = 10.0  # discount on market price (haggling, risk, time)
    asking_price_discount: float = 0.85  # asking prices on Kleinanzeigen > real sale prices
    use_kleinanzeigen_comps: bool = True
    use_ebay_sold_comps: bool = True
    comps_limit: int = 30
    use_price_history: bool = True  # learn prices from every ad seen; estimate without extra requests
    vb_expected_discount: float = 0.10  # "VB" ads usually go ~10 % below the asking price
    max_capital: float | None = None  # never recommend buying above this (your budget)
    min_comparables: int = 6  # fewer data points -> at most "maybe"
    max_price_spread: float = 0.4  # IQR/median above this -> market too unclear -> at most "maybe"
    paypal_fixed_fee: float = 0.0  # e.g. 0.35 for PayPal goods & services
    history_days: int = 60
    history_min_points: int = 6
    reference_prices: list[ReferencePrice] = Field(default_factory=list)


AIProvider = Literal["ollama", "openai", "anthropic"]
# A free cloud endpoint (docs/design/CLOUD_AI.md): "" = a local server (LM Studio / Ollama / llama.cpp),
# the default. The cloud ones are OpenAI-compatible (provider "openai"); their key lives in .env
# (OPENROUTER_API_KEY / NVIDIA_API_KEY / OMNIROUTE_API_KEY / CLOUD_API_KEY), never in config.yaml.
CloudKind = Literal["", "openrouter", "nvidia", "omniroute", "custom"]
# The model's reasoning ("thinking"): "auto" = off for the photo check and the scout (answers in
# seconds instead of minutes, all max_tokens go to the JSON), the model's default elsewhere.
ThinkingMode = Literal["auto", "off", "on"]


class LLMSettings(BaseModel):
    """Connection settings shared by the main (local) model and the optional second opinion.
    provider: "ollama" (local), "openai" = any OpenAI-compatible local server
    (LM Studio, llama.cpp, vLLM), "anthropic" = Claude API (cloud, paid, optional)."""

    provider: AIProvider = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5vl:7b"
    api_key: str = ""
    max_images: int = 3
    timeout_seconds: float = 240.0
    temperature: float = 0.2  # ignored for Claude
    max_tokens: int = 700  # cap on the model's answer (a looping 7B model otherwise runs to the timeout)
    image_max_side: int = 1024  # photos are downscaled to this many px (needs Pillow; otherwise sent as is)
    thinking: ThinkingMode = "auto"
    # free cloud AI (docs/design/CLOUD_AI.md); "" keeps the local behaviour
    cloud: CloudKind = ""
    rpm: int = 0  # requests per minute; 0 = auto (the provider preset or the key's own limits)
    daily_limit: int = 0  # requests per UTC day; 0 = auto (OpenRouter: 50, 1000 once $10 of credits were bought)


class FallbackConfig(BaseModel):
    """A local model that takes over while the cloud is limited (free quota used up, 429s) or
    down. Empty / disabled = the usual "AI down" paths (the scout falls back to the script, photo
    checks wait in the vision queue)."""

    enabled: bool = False
    provider: Literal["ollama", "openai"] = "openai"
    base_url: str = ""
    model: str = ""

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.base_url.strip()) and bool(self.model.strip())


class SecondOpinionConfig(LLMSettings):
    """Optional: ask a stronger (cloud) model to double-check the best deals only."""

    enabled: bool = False
    provider: AIProvider = "anthropic"
    base_url: str = ""
    model: str = "claude-opus-5"
    min_score: float = 60.0  # only deals the local pipeline already rates this high
    verdicts: list[Literal["buy", "maybe", "skip"]] = Field(default_factory=lambda: ["buy"])
    max_per_run: int = 10  # hard cap on paid calls per monitoring pass


ScoutMode = Literal["auto", "all", "candidates"]


class ScoutConfig(LLMSettings):
    """The AI scout (docs/design/AI_SCOUT.md): a small TEXT model reads EVERY new ad in batches
    (title, price, snippet) and says what it really is — product, bundle contents, hidden value,
    risks, interest 0..10. It never guesses prices: those come from our own price history.

    Its own endpoint: an always-on 2-4B model on the home server's CPU (llama.cpp / Ollama /
    LM Studio), while `ai` (the vision model) may live on a gaming PC that is only sometimes on.
    Empty base_url + model = use the `ai` endpoint and model for the scout too."""

    enabled: bool = False
    provider: AIProvider = "openai"
    base_url: str = ""  # "" = the ai endpoint; e.g. http://127.0.0.1:8080/v1 (llama.cpp server)
    model: str = ""  # "" = ai.model; e.g. "qwen3.5:2b-q4_K_M" (Ollama) or "qwen/qwen3.5-2b" (LM Studio)
    timeout_seconds: float = 180.0
    temperature: float = 0.1
    max_tokens: int = 1800  # one batch answer; the engine also caps it per batch size
    # "auto": every new ad while the model keeps up, else only the ads where the script is blind
    # (no product found, PCs, bundles, lots); "all": always every ad; "candidates": only those
    mode: ScoutMode = "auto"
    # ads per model call, adapted between min_batch and max_batch; 0 = by the model's size
    # (docs/design/AI_MODELS.md §5): 5 for ~2B models (they lose track in longer batches), 10 for 4B+
    batch_size: int = 0
    min_batch: int = 2
    max_batch: int = 0  # 0 = by the model's size: 5 for ~2B, 16 for 4B+
    max_per_hour: int = 600  # hard cap of ads per hour (a 4-core CPU must stay usable)
    pass_share: float = 0.5  # at most this share of general.interval_minutes per pass goes to the scout
    # The model's interest 0..10 is only a weak signal (a 2B barely tells deals from junk): the scout
    # promotes an ad by a grounded product and real prices. An ad it rates below this is not
    # promoted; 0 = interest never filters (only orders equal candidates).
    min_interest: int = 0
    backlog_hours: float = 6.0  # ads not reached in their pass are still read later (rescue) this long
    bundle_discount: float = 0.15  # a bundle sells for the sum of its parts minus this
    pc_discount: float = 0.25  # parting out a PC: more work, lower price
    min_priced_share: float = 0.5  # share of a bundle's model-numbered parts that must have a market price
    learn_from_feedback: bool = True  # hidden / bought / sold deals become hints in the prompt
    # the scout's local stand-in while its cloud endpoint is limited; empty = ai.fallback's server
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)


class AIConfig(LLMSettings):
    enabled: bool = False
    run_for: Literal["promising", "all"] = "promising"  # "promising" = skip obvious junk to save time
    min_prefilter_score: float = 20.0
    # The vision model may be offline (the PC is off): a would-be deal waits this long for the photo
    # check, then goes out marked "фото не проверены" (notifications.unchecked_deals). 0 = at once.
    vision_wait_minutes: float = 45.0
    second_opinion: SecondOpinionConfig = Field(default_factory=SecondOpinionConfig)
    scout: ScoutConfig = Field(default_factory=ScoutConfig)
    # «Облако + компьютер про запас»: the local model for photos while the cloud is limited or down
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    # eBay's API license forbids passing its content to third parties / AI training: eBay ads never
    # go to a cloud endpoint unless this is switched on (they use the fallback or the script path)
    cloud_send_ebay: bool = False


class EbayConfig(BaseModel):
    """Official eBay Browse API (https://developer.ebay.com → My Account → Application Keys).
    Put App ID (Client ID) + Cert ID (Client Secret) here — tokens are then created and
    refreshed automatically. A pasted `oauth_token` works too, but expires after ~2 hours."""

    client_id: str = ""
    client_secret: str = ""
    oauth_token: str = ""
    marketplace_id: str = "EBAY_DE"
    item_location_country: str = "DE"
    sandbox: bool = False

    @property
    def configured(self) -> bool:
        return bool((self.client_id and self.client_secret) or self.oauth_token)


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_ssl: bool = False  # True for port 465, otherwise STARTTLS
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)

    @field_validator("to_addrs", mode="before")
    @classmethod
    def _split(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [a.strip() for a in v.split(",") if a.strip()]
        return v


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class SuperDealsConfig(BaseModel):
    """«🔥 Супер-находка»: an exceptional deal (big profit AND ROI, sure market, photos checked,
    no warnings) is sent at once, past the hourly cap and the digest mode, with its own headline."""

    enabled: bool = True
    min_profit: float = 120.0  # EUR net profit at the asking price
    min_roi: float = 0.8  # 80 % return
    min_score: float = 85.0


class DailyTopConfig(BaseModel):
    """«Топ за день»: once a day at `hour` (general.timezone) the best `per_search` deals of the
    last 24 h per search, as one message (also the ones already sent)."""

    enabled: bool = False
    hour: int = 20
    per_search: int = 3
    verdicts: list[Literal["buy", "maybe"]] = Field(default_factory=lambda: ["buy", "maybe"])


class NotificationsConfig(BaseModel):
    super_deals: SuperDealsConfig = Field(default_factory=SuperDealsConfig)
    daily_top: DailyTopConfig = Field(default_factory=DailyTopConfig)
    min_score: float = 70.0
    verdicts: list[Literal["buy", "maybe", "skip"]] = Field(default_factory=lambda: ["buy"])
    mode: Literal["instant", "digest"] = "instant"  # digest = one message per run
    max_alerts_per_hour: int = 10  # more deals than this in an hour -> the rest go into one digest
    health_alerts: bool = True  # tell me when the AI is down, the site blocks us or parsing breaks
    heartbeat_hour: int | None = 9  # daily "I'm alive: checked N ads, M deals" at this local hour; None = off
    unchecked_deals: bool = True  # AI down: still send would-be deals, marked "фото НЕ проверены"
    email: EmailConfig = Field(default_factory=EmailConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    run_monitor: bool = True  # run the monitor loop inside the web server
    allowed_hosts: list[str] = Field(default_factory=list)  # extra host names allowed to open the web UI


class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    searches: list[SearchConfig] = Field(default_factory=list)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    ebay: EbayConfig = Field(default_factory=EbayConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    @property
    def data_path(self) -> Path:
        return Path(self.general.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "ebeyparser.sqlite3"

    def search_by_name(self, name: str) -> SearchConfig | None:
        return next((s for s in self.searches if s.name == name), None)


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def parse_config(data: dict[str, Any] | None) -> AppConfig:
    return AppConfig.model_validate(_expand_env(data or {}))


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml (or the given path). Missing file -> defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    load_dotenv(cfg_path.parent / ".env")
    if not cfg_path.is_file():
        return AppConfig()
    data = _read_yaml(cfg_path)
    try:
        return parse_config(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
        )
        raise ConfigError(f"В {cfg_path} неверные значения — {problems}") from exc


def _read_yaml(cfg_path: Path) -> dict[str, Any]:
    text = cfg_path.read_text(encoding="utf-8-sig")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        fixed = _MISSING_SPACE_RE.sub(r"\1: ", text)
        if fixed != text:
            try:
                data = yaml.safe_load(fixed) or {}
            except yaml.YAMLError:
                pass
            else:
                lines = [n + 1 for n, (a, b) in enumerate(zip(text.splitlines(), fixed.splitlines())) if a != b]
                log.warning("В %s после двоеточия не хватает пробела (строки %s) — прочитал как `ключ: значение`,"
                            " но лучше поправь файл", cfg_path, ", ".join(map(str, lines)))
                return data
        mark = getattr(exc, "problem_mark", None)
        where = f", строка {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(
            f"Не могу прочитать {cfg_path}{where}: {problem}. Частые причины: нет пробела после двоеточия"
            " (надо `ключ: значение`), сбиты отступы (только пробелы, по 2), табы вместо пробелов."
        ) from exc


def save_searches(path: str | Path, searches: list[SearchConfig]) -> None:
    """Rewrite only the `searches` section of the YAML file, keeping the rest
    as written by the user (including ${ENV} placeholders, which are not expanded)."""
    cfg_path = Path(path)
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw["searches"] = [s.model_dump(exclude_none=True, exclude_defaults=False) for s in searches]
    cfg_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
