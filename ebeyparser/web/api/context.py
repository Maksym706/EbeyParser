"""Shared state of the /api/v1 routers: the running config, database, monitor, event hub and
jobs, plus the writers that change config.yaml / .env and apply the result live."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import yaml
from fastapi import Request

from ...config import AppConfig, ConfigError, SearchConfig, load_config
from ...db import Database
from ...models import utcnow
from ..configfile import read_env_file, save_searches_block, update_yaml_values, write_env_values
from .errors import ApiError
from .events import EventHub
from .jobs import JobRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

LIVE_SECTIONS = ("general", "pricing", "ai", "notifications", "ebay", "web")
ONBOARDING_KEY = "onboarding"
ONBOARDING_DRAFT_KEY = "onboarding:draft"
BOOTSTRAPPED_KEY = "onboarding:bootstrapped"  # config.yaml was created by `run` for the web onboarding
DEMO_RUNS_KEY = "demo:run_ids"
WRITE_FAILED_RU = ("Не получилось сохранить настройки — файл занят другой программой или нет места на диске. "
                   "Попробуй ещё раз через минуту")
ONBOARDING_STEPS: tuple[tuple[str, str, bool], ...] = (  # key, title, required
    ("location", "Где искать", True),
    ("categories", "Что искать", True),
    ("money", "Деньги", True),
    ("wishlist", "Для себя", False),
    ("ai", "Нейросеть", False),
    ("telegram", "Telegram", False),
    ("ebay", "eBay", False),
)


@dataclass(frozen=True)
class SecretSpec:
    env: str  # variable in .env
    paths: tuple[str, ...]  # config keys that reference it as ${ENV} (none: read from the environment)
    label: str
    kind: str = "token"  # token | email | id


SECRETS: dict[str, SecretSpec] = {
    "telegram_bot_token": SecretSpec("TELEGRAM_BOT_TOKEN", ("notifications.telegram.bot_token",), "Токен Telegram-бота"),
    "telegram_chat_id": SecretSpec("TELEGRAM_CHAT_ID", ("notifications.telegram.chat_id",), "Telegram chat_id", "id"),
    "smtp_user": SecretSpec("SMTP_USER", ("notifications.email.username", "notifications.email.from_addr"),
                            "Почта (логин SMTP)", "email"),
    "smtp_password": SecretSpec("SMTP_PASSWORD", ("notifications.email.password",), "Пароль приложения почты"),
    "notify_email": SecretSpec("NOTIFY_EMAIL", ("notifications.email.to_addrs",), "Куда слать письма", "email"),
    "ebay_client_id": SecretSpec("EBAY_CLIENT_ID", ("ebay.client_id",), "eBay App ID (Client ID)", "id"),
    "ebay_client_secret": SecretSpec("EBAY_CLIENT_SECRET", ("ebay.client_secret",), "eBay Cert ID (Client Secret)"),
    "ebay_oauth_token": SecretSpec("EBAY_OAUTH_TOKEN", ("ebay.oauth_token",), "Разовый OAuth-токен eBay"),
    "anthropic_api_key": SecretSpec("ANTHROPIC_API_KEY", ("ai.second_opinion.api_key",), "Ключ Claude (Anthropic)"),
    # free cloud AI (docs/design/CLOUD_AI.md): only in .env; the client picks the provider's key itself
    "openrouter_api_key": SecretSpec("OPENROUTER_API_KEY", (), "Ключ OpenRouter"),
    "nvidia_api_key": SecretSpec("NVIDIA_API_KEY", (), "Ключ NVIDIA"),
    "omniroute_api_key": SecretSpec("OMNIROUTE_API_KEY", (), "Ключ OmniRoute"),
    "cloud_api_key": SecretSpec("CLOUD_API_KEY", (), "Ключ облачной нейросети"),
}
CLOUD_SECRET = {"openrouter": "openrouter_api_key", "nvidia": "nvidia_api_key", "omniroute": "omniroute_api_key",
                "custom": "cloud_api_key"}


def mask(value: str, kind: str = "token") -> str:
    """'123456789:AAHxyz…' -> '…xyz1', 'maksem706@gmail.com' -> 'ma***@gmail.com'."""
    text = str(value or "").strip()
    if not text:
        return ""
    if kind == "email" or ("@" in text and kind != "token"):
        parts = [p.strip() for p in text.split(",") if p.strip()]
        out = []
        for part in parts:
            local, _, domain = part.partition("@")
            out.append(f"{local[:2]}***@{domain}" if domain else "***")
        return ", ".join(out)
    if len(text) <= 6:
        return "…" + text[-2:] if len(text) > 3 else "***"
    return "…" + text[-4:]


def dig(data: Any, dotted: str) -> Any:
    node = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


class ApiContext:
    """One per app (app.state.api). Test hooks: http_transport (httpx transport for Telegram,
    eBay and the AI server), smtp_factory (EmailNotifier), ai_probe (LM Studio / Ollama probe),
    host_probe (this machine's hardware for /ai/recommend)."""

    def __init__(
        self,
        app: FastAPI,
        *,
        category_discovery: Callable[[str, int], Awaitable[Any]] | None = None,
        ai_probe: Callable[[str], Any] | None = None,
        bind_host: str | None = None,
    ) -> None:
        self.app = app
        self.hub = EventHub()
        self.jobs = JobRegistry(self.hub, error_mapper=describe_exception)
        self.category_discovery = category_discovery
        self.ai_probe = ai_probe
        self.bind_host = bind_host
        self.http_transport: Any = None
        self.smtp_factory: Any = None
        self.host_probe: Any = None  # () -> this machine's hardware (ai.hardware.detect_host); tests set a fake
        self.check_lock = asyncio.Lock()
        self.started_at = utcnow()

    # ------------------------------------------------------------------ state
    @property
    def config(self) -> AppConfig:
        return self.app.state.config

    @property
    def db(self) -> Database:
        return self.app.state.db

    @property
    def monitor(self) -> Any:
        return self.app.state.monitor

    @property
    def config_path(self) -> Path | None:
        return self.app.state.config_path

    @property
    def env_path(self) -> Path | None:
        return self.config_path.parent / ".env" if self.config_path else None

    def config_writable(self) -> bool:
        path = self.config_path
        if path is None:
            return False
        target = path if path.exists() else path.parent
        return target.exists() and os.access(target, os.W_OK)

    def require_editable(self) -> Path:
        path = self.config_path
        if path is None:
            raise ApiError(403, "read_only", "Настройки сейчас только для чтения — перезапусти программу обычным "
                                             "способом, и их снова можно будет менять")
        if not self.config_writable():
            raise ApiError(403, "read_only", "Не могу сохранить настройки: нет доступа к папке программы. Если она "
                                             "лежит в OneDrive или защищённой папке, перенеси её, например, в Документы",
                           details=f"нет прав на запись: {path}")
        return path

    def raw_config(self) -> dict[str, Any]:
        """config.yaml as written (${ENV} placeholders not expanded)."""
        path = self.config_path
        if path is None or not path.is_file():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- writing
    def apply_file(self, *, reason: str, keys: list[str] | None = None) -> AppConfig:
        """Re-read config.yaml (+ .env) and apply the non-search sections to the running config."""
        path = self.require_editable()
        try:
            fresh = load_config(path)
        except ConfigError as exc:
            raise ApiError(500, "config_invalid", "Файл настроек повреждён — изменения не применились. Восстанови "
                                                  "резервную копию в «Настройки» → «Данные»", details=str(exc)) from exc
        cfg = self.config
        for section in LIVE_SECTIONS:
            if hasattr(fresh, section):
                setattr(cfg, section, getattr(fresh, section))
        from ...timefmt import apply_config as apply_timezone

        apply_timezone(cfg)
        self.notify_monitor()
        sections = sorted({k.split(".", 1)[0] for k in keys or []})
        self.hub.publish("settings_changed", {"reason": reason, "sections": sections, "keys": list(keys or [])})
        return cfg

    def notify_monitor(self) -> None:
        update = getattr(self.monitor, "update_config", None)
        if update is not None:
            try:
                update(self.config)
            except Exception:  # noqa: BLE001
                log.exception("monitor.update_config failed")

    def write_values(self, updates: dict[str, Any], *, env: dict[str, str] | None = None, reason: str = "settings") -> None:
        """Comment-preserving write of dotted config keys (and .env values), then apply live."""
        path = self.require_editable()
        try:
            if env:
                write_env_values(path.parent / ".env", env)
            if updates:
                if not path.is_file():
                    path.write_text("", encoding="utf-8")
                update_yaml_values(path, updates)
        except OSError as exc:
            raise ApiError(500, "config_write_failed", WRITE_FAILED_RU, details=f"{path}: {exc}") from exc
        self.apply_file(reason=reason, keys=list(updates) + [f"env.{k}" for k in (env or {})])

    def save_searches(self, searches: list[SearchConfig], *, backup: bool = False, reason: str = "searches") -> None:
        path = self.require_editable()
        if backup and path.is_file():
            try:
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
            except OSError as exc:
                log.warning("backup of %s failed: %s", path, exc)
        try:
            save_searches_block(path, searches)
        except OSError as exc:
            raise ApiError(500, "config_write_failed", WRITE_FAILED_RU, details=f"{path}: {exc}") from exc
        self.config.searches = list(searches)
        self.notify_monitor()
        from .presenters import search_ids

        self.hub.publish("searches_changed", {"reason": reason, "count": len(searches),
                                              "ids": search_ids(searches)})

    # ---------------------------------------------------------------- secrets
    def secret_value(self, name: str) -> str:
        spec = SECRETS[name]
        if not spec.paths:
            return os.environ.get(spec.env, "").strip()
        value = dig(self.config.model_dump(), spec.paths[0])
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        return str(value or "")

    def secrets_status(self) -> dict[str, dict[str, Any]]:
        env = read_env_file(self.env_path) if self.env_path else {}
        raw = self.raw_config()
        out: dict[str, dict[str, Any]] = {}
        for name, spec in SECRETS.items():
            value = self.secret_value(name)
            referenced = str(dig(raw, spec.paths[0]) or "").strip() if spec.paths else ""
            out[name] = {
                "set": bool(value),
                "masked": mask(value, spec.kind),
                "label": spec.label,
                "env": spec.env,
                "in_env_file": bool(env.get(spec.env)),
                "in_config": bool(referenced) and not referenced.startswith("${"),
            }
        return out

    def write_secrets(self, values: dict[str, str | None], *, extra_updates: dict[str, Any] | None = None) -> list[str]:
        """Secrets go to .env; config.yaml only references them as ${ENV}. None = unchanged,
        "" = clear. Returns the names written."""
        env: dict[str, str] = {}
        updates: dict[str, Any] = dict(extra_updates or {})
        raw = self.raw_config()
        written: list[str] = []
        for name, value in values.items():
            if value is None or name not in SECRETS:
                continue
            spec = SECRETS[name]
            env[spec.env] = " ".join(str(value).split())
            placeholder = "${" + spec.env + "}"
            for i, key in enumerate(spec.paths):
                current = dig(raw, key)
                if current == placeholder:
                    continue
                if i > 0 and current not in (None, "", placeholder):
                    continue  # e.g. a from_addr of its own: keep it
                updates[key] = placeholder
            written.append(name)
        if env or updates:
            self.write_values(updates, env=env, reason="secrets")
        return written

    # -------------------------------------------------------------- kv state
    def get_json(self, key: str, default: Any = None) -> Any:
        try:
            state = self.db.get_state(key)
        except Exception:  # noqa: BLE001
            return default
        if state is None or not state[0]:
            return default
        try:
            return json.loads(state[0])
        except ValueError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.db.set_state(key, json.dumps(value, ensure_ascii=False, default=str))

    def onboarding_state(self) -> dict[str, Any]:
        state = self.get_json(ONBOARDING_KEY, {}) or {}
        return {"skipped": list(state.get("skipped") or []), "done": list(state.get("done") or []),
                "completed_at": state.get("completed_at")}

    def update_onboarding(self, *, skip: str | None = None, unskip: str | None = None, done: list[str] | None = None,
                          completed: bool | None = None) -> dict[str, Any]:
        state = self.onboarding_state()
        if skip and skip not in state["skipped"]:
            state["skipped"].append(skip)
        if unskip and unskip in state["skipped"]:
            state["skipped"].remove(unskip)
        for step in done or []:
            if step not in state["done"]:
                state["done"].append(step)
            if step in state["skipped"]:
                state["skipped"].remove(step)
        if completed is True:
            state["completed_at"] = utcnow().isoformat()
        elif completed is False:
            state["completed_at"] = None
        self.set_json(ONBOARDING_KEY, state)
        return state

    def mark_done(self, *steps: str) -> None:
        try:
            self.update_onboarding(done=list(steps))
        except Exception:  # noqa: BLE001 - bookkeeping only
            log.exception("onboarding state not saved")

    # ------------------------------------------------------------------ misc
    def web_base_url(self, request: Request | None = None) -> str:
        cfg = self.config.web
        host = "localhost" if cfg.host in ("0.0.0.0", "127.0.0.1", "::", "") else cfg.host
        return f"http://{host}:{cfg.port}"


def get_ctx(request: Request) -> ApiContext:
    return request.app.state.api


def describe_exception(exc: BaseException) -> dict[str, Any]:
    """Any failure of a job / check -> {"code", "message_ru", "details"?, "action"?} (plain
    Russian with a next step; the technical text only in `details`)."""
    from ...errors_ru import GENERIC, humanize, sanitize

    if isinstance(exc, ApiError):
        return _error_dict(exc.code, exc.message_ru, exc.details or "", exc.action)
    if isinstance(exc, ValueError) and type(exc).__name__ in ("ValueError",) and str(exc):
        text = sanitize(str(exc))  # our own "Объявление удалено" / "Не похоже на ссылку…"
        return _error_dict("bad_request", text or GENERIC, str(exc) if text != str(exc) else "")
    service = "kleinanzeigen"
    if type(exc).__name__ == "EbayAPIError" or getattr(exc, "service", "") == "ebay":
        service = "ebay"
    human = humanize(exc, service)
    code = human.code if human.code != "error" else "internal"
    return _error_dict(code, human.message_ru, human.details, human.action)


def _error_dict(code: str, message_ru: str, details: str = "", action: dict[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message_ru": message_ru}
    if details:
        out["details"] = details
    if action:
        out["action"] = action
    return out


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ".").replace("€", "").strip())
    except ValueError:
        return None


def parse_since(value: str | None) -> datetime | None:
    """'24h' / '7d' / '90m' / ISO timestamp -> aware datetime (None when empty/invalid)."""
    from datetime import timedelta, timezone

    raw = (value or "").strip()
    text = raw.lower()
    if not text or text in ("all", "всё", "все"):
        return None
    units = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    if text[-1] in units and text[:-1].replace(".", "", 1).isdigit():
        return utcnow() - timedelta(seconds=float(text[:-1]) * units[text[-1]])
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


__all__ = ["CLOUD_SECRET", "ApiContext", "SECRETS", "SecretSpec", "get_ctx", "mask"]
