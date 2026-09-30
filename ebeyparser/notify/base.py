"""Notifier protocol, error type and the factory that builds enabled channels."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .render import is_local_url

if TYPE_CHECKING:
    from ..config import EmailConfig, NotificationsConfig, TelegramConfig
    from ..models import DealView

log = logging.getLogger(__name__)


class NotifyError(Exception):
    """A notification could not be delivered. str(exc) is the technical Russian text (log,
    CLI); `message_ru` (when given) is the plain one for the web UI — without it the UI
    derives one from the cause (ebeyparser.errors_ru.humanize)."""

    def __init__(self, message: str, *, message_ru: str = "", code: str = "delivery_failed",
                 service: str = "") -> None:
        super().__init__(message)
        if message_ru:
            self.message_ru = message_ru
        self.code = code
        self.service = service


@runtime_checkable
class Notifier(Protocol):
    name: str  # "email" | "telegram"

    async def send(self, deals: list[DealView], *, title: str | None = None) -> None:
        """Deliver `deals`; raise NotifyError on failure. Empty list -> no-op."""
        ...

    async def send_text(self, text: str) -> None:
        """Deliver a short service message (health alert, heartbeat); raise NotifyError on failure."""
        ...


def _host_is_local(host: str) -> bool:
    return is_local_url(f"smtp://{host.strip()}") if host.strip() else False


def _email_missing(c: EmailConfig) -> list[str]:
    missing: list[str] = []
    host = c.smtp_host.strip()
    if not host:
        missing.append("smtp_host")
    if not c.smtp_port or c.smtp_port <= 0:
        missing.append("smtp_port")
    # A local relay (localhost:25) may work without login; real providers need it.
    needs_auth = not _host_is_local(host) or bool(c.username.strip())
    if needs_auth and not c.username.strip():
        missing.append("username")
    if needs_auth and not c.password:
        missing.append("password")
    if not c.from_addr.strip() and not c.username.strip() and "username" not in missing:
        missing.append("from_addr")
    if not [a for a in c.to_addrs if "@" in a]:
        missing.append("to_addrs")
    return missing


def _telegram_missing(c: TelegramConfig) -> list[str]:
    missing: list[str] = []
    if not c.bot_token.strip():
        missing.append("bot_token")
    if not str(c.chat_id).strip():
        missing.append("chat_id")
    return missing


def missing_settings(cfg: NotificationsConfig) -> dict[str, list[str]]:
    """Missing/empty settings per *enabled* channel, e.g.
    {"email": ["password", "to_addrs"], "telegram": []}. Disabled channels are absent."""
    result: dict[str, list[str]] = {}
    if cfg.email.enabled:
        result["email"] = _email_missing(cfg.email)
    if cfg.telegram.enabled:
        result["telegram"] = _telegram_missing(cfg.telegram)
    return result


def build_notifiers(cfg: NotificationsConfig, *, web_base_url: str | None = None) -> list[Notifier]:
    """Notifiers for channels that are enabled and fully configured."""
    # Imported here: emailer/telegram import NotifyError from this module.
    from .emailer import EmailNotifier
    from .telegram import TelegramNotifier

    notifiers: list[Notifier] = []
    for channel, fields in missing_settings(cfg).items():
        if fields:
            log.warning(
                "Уведомления %s включены, но не настроены (не заполнено: %s) — канал пропущен",
                channel,
                ", ".join(fields),
            )
            continue
        if channel == "email":
            notifiers.append(EmailNotifier(cfg.email, web_base_url=web_base_url))
        elif channel == "telegram":
            notifiers.append(TelegramNotifier(cfg.telegram, web_base_url=web_base_url))
    return notifiers
