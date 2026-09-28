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
    history_days: int = 60
    history_min_points: int = 6
    reference_prices: list[ReferencePrice] = Field(default_factory=list)


AIProvider = Literal["ollama", "openai", "anthropic"]


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


class SecondOpinionConfig(LLMSettings):
    """Optional: ask a stronger (cloud) model to double-check the best deals only."""

    enabled: bool = False
    provider: AIProvider = "anthropic"
    base_url: str = ""
    model: str = "claude-opus-5"
    min_score: float = 60.0  # only deals the local pipeline already rates this high
    verdicts: list[Literal["buy", "maybe", "skip"]] = Field(default_factory=lambda: ["buy"])
    max_per_run: int = 10  # hard cap on paid calls per monitoring pass


class AIConfig(LLMSettings):
    enabled: bool = False
    run_for: Literal["promising", "all"] = "promising"  # "promising" = skip obvious junk to save time
    min_prefilter_score: float = 20.0
    second_opinion: SecondOpinionConfig = Field(default_factory=SecondOpinionConfig)


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


class NotificationsConfig(BaseModel):
    min_score: float = 70.0
    verdicts: list[Literal["buy", "maybe", "skip"]] = Field(default_factory=lambda: ["buy"])
    mode: Literal["instant", "digest"] = "instant"  # digest = one message per run
    email: EmailConfig = Field(default_factory=EmailConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    run_monitor: bool = True  # run the monitor loop inside the web server


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
