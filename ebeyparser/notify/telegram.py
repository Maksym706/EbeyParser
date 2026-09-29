"""Telegram notifications via the Bot API (one message per deal, photo + caption)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..config import TelegramConfig
from ..models import DealView
from ..errors_ru import humanize, status_message
from .base import NotifyError
from .render import (
    SUPER_PREFIX,
    more_deals_phrase,
    deal_web_url,
    deals_count_phrase,
    is_local_url,
    open_link_text,
    render_telegram,
    safe_url,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_DEALS_PER_SEND = 20
MAX_RETRY_AFTER = 30.0  # seconds; longer 429 waits are not worth blocking the monitor
PAUSE_BETWEEN_MESSAGES = 0.5  # Telegram allows ~1 msg/s per chat; short bursts are fine
NOT_CONNECTED_RU = "Telegram не подключён — привяжи бота в настройках уведомлений"

Sleep = Callable[[float], Awaitable[Any]]


class _BadRequest(NotifyError):
    """HTTP 400 from Telegram (bad photo URL, bad markup, ...): worth a fallback."""


class TelegramNotifier:
    """Sends each deal as its own message (photo with caption when possible)."""

    name = "telegram"

    def __init__(
        self,
        cfg: TelegramConfig,
        *,
        web_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.web_base_url = web_base_url
        self._transport = transport
        self._sleep: Sleep = sleep or asyncio.sleep
        self.timeout = timeout
        self._token = cfg.bot_token.strip()
        self._api = f"{API_BASE}/bot{self._token}/"

    # -- public --------------------------------------------------------------

    async def send(self, deals: list[DealView], *, title: str | None = None) -> None:
        if not deals:
            return
        if not self._token or not str(self.cfg.chat_id).strip():
            raise NotifyError("Telegram: не указан bot_token или chat_id.", message_ru=NOT_CONNECTED_RU,
                              code="not_configured", service="telegram")
        shown = deals[:MAX_DEALS_PER_SEND]
        rest = len(deals) - len(shown)
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            first = True

            async def pause() -> None:
                nonlocal first
                if not first and PAUSE_BETWEEN_MESSAGES > 0:
                    await self._sleep(PAUSE_BETWEEN_MESSAGES)
                first = False

            if title and (len(deals) > 1 or title.startswith(SUPER_PREFIX)):
                await pause()
                header = f"<b>{_esc(title)}</b>\n{deals_count_phrase(len(deals))}"
                if rest:
                    header += f" (показываю первые {len(shown)})"
                await self._call(client, "sendMessage", self._text_payload(header, preview=False))
            for deal in shown:
                await pause()
                await self._send_deal(client, deal)
            if rest:
                await pause()
                await self._call(client, "sendMessage", self._text_payload(self._more_text(rest), preview=False))
        log.info("Telegram: отправлено %d сообщ. в чат %s", len(shown), self.cfg.chat_id)

    async def send_text(self, text: str) -> None:
        """A plain service message (health alert, heartbeat)."""
        text = text.strip()
        if not text:
            return
        if not self._token or not str(self.cfg.chat_id).strip():
            raise NotifyError("Telegram: не указан bot_token или chat_id.", message_ru=NOT_CONNECTED_RU,
                              code="not_configured", service="telegram")
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            await self._call(client, "sendMessage", self._text_payload(_esc(text[:4000]), preview=False))
        log.info("Telegram: служебное сообщение отправлено в чат %s", self.cfg.chat_id)

    # -- internals -----------------------------------------------------------

    def _text_payload(self, text: str, *, preview: bool, markup: dict | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": str(self.cfg.chat_id).strip(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if markup:
            payload["reply_markup"] = markup
        return payload

    def _keyboard(self, deal: DealView) -> dict | None:
        rows: list[list[dict[str, str]]] = []
        url = safe_url(deal.listing.url)
        if url:
            rows.append([{"text": open_link_text(deal), "url": url}])
        web = deal_web_url(deal, self.web_base_url)
        if web and not is_local_url(web):  # Telegram rejects localhost URLs in buttons
            rows.append([{"text": "Подробнее в EbeyParser", "url": web}])
        return {"inline_keyboard": rows} if rows else None

    def _more_text(self, rest: int) -> str:
        text = _esc(more_deals_phrase(rest))
        base = safe_url((self.web_base_url or "").strip().rstrip("/"))
        if base and not is_local_url(base):
            text += f' — <a href="{_esc(base)}">смотрите в EbeyParser</a>'
        else:
            text += " — смотрите в EbeyParser"
        return text

    async def _send_deal(self, client: httpx.AsyncClient, deal: DealView) -> None:
        text = render_telegram(deal, web_base_url=self.web_base_url)
        markup = self._keyboard(deal)
        photo = next((u for u in (safe_url(x) for x in deal.listing.image_urls) if u), None)
        chat_id = str(self.cfg.chat_id).strip()

        # Fallback chain for 400 errors: photo -> text -> text without buttons.
        attempts: list[tuple[str, dict[str, Any]]] = []
        if photo:
            payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo, "caption": text, "parse_mode": "HTML"}
            if markup:
                payload["reply_markup"] = markup
            attempts.append(("sendPhoto", payload))
        attempts.append(("sendMessage", self._text_payload(text, preview=True, markup=markup)))
        if markup:
            attempts.append(("sendMessage", self._text_payload(text, preview=True)))

        last: _BadRequest | None = None
        for method, payload in attempts:
            try:
                await self._call(client, method, payload)
                return
            except _BadRequest as exc:
                if "chat not found" in str(exc).lower():
                    raise
                log.warning("Telegram %s отклонён (%s), пробую запасной вариант", method, exc)
                last = exc
        assert last is not None
        raise NotifyError(str(last), message_ru=getattr(last, "message_ru", ""), code=last.code,
                          service="telegram") from last

    async def _call(self, client: httpx.AsyncClient, method: str, payload: dict[str, Any]) -> Any:
        for attempt in range(2):
            try:
                resp = await client.post(self._api + method, json=payload)
            except httpx.HTTPError as exc:
                raise NotifyError(
                    f"Telegram: сетевая ошибка ({exc.__class__.__name__}): {self._redact(str(exc))}",
                    message_ru=humanize(exc, "telegram").message_ru, code="network", service="telegram",
                ) from None
            try:
                data = resp.json()
                json_answer = True
            except ValueError:
                data, json_answer = {}, False
            if not isinstance(data, dict):
                data, json_answer = {}, False
            if resp.status_code == 200 and data.get("ok"):
                return data.get("result")
            code = int(data.get("error_code") or resp.status_code)
            desc = self._redact(str(data.get("description") or resp.reason_phrase or "нет описания"))
            if code == 429 and attempt == 0:
                params = data.get("parameters") or {}
                try:
                    wait = float(params.get("retry_after", 1))
                except (TypeError, ValueError):
                    wait = 1.0
                wait = max(0.0, min(wait, MAX_RETRY_AFTER))
                log.warning("Telegram: слишком много запросов, жду %.0f с", wait)
                await self._sleep(wait)
                continue
            raise self._error(code, desc, json_answer=json_answer, method=method)
        raise AssertionError("unreachable")  # pragma: no cover

    def _error(self, code: int, desc: str, *, json_answer: bool = True, method: str = "") -> NotifyError:
        """Technical text for the log / CLI + a plain `message_ru` for the web UI (a 403 that is
        not Telegram's own JSON answer comes from a proxy / firewall, not from a blocked bot)."""
        kind, message_ru = status_message(code, "telegram", method=method, description=desc, json_answer=json_answer)
        low = desc.lower()
        extra = {"message_ru": message_ru, "code": kind, "service": "telegram"}
        if code == 400 and "chat not found" in low:
            return _BadRequest(
                f"Telegram: чат не найден ({desc}). Проверьте chat_id и сначала напишите своему боту /start.", **extra
            )
        if code == 400:
            return _BadRequest(f"Telegram: запрос отклонён (400): {desc}", **extra)
        if code == 401:
            return NotifyError(f"Telegram: неверный bot_token ({desc}). Скопируйте токен из @BotFather заново.", **extra)
        if code == 403 and not json_answer:
            return NotifyError(f"Telegram: доступ закрыт сетью или прокси (HTTP 403, не ответ Telegram): {desc}", **extra)
        if code == 403:
            return NotifyError(
                f"Telegram: бот не может писать в этот чат ({desc}). "
                "Напишите боту /start (или добавьте его в группу/канал) и проверьте chat_id.", **extra
            )
        if code == 404:
            return NotifyError("Telegram: API вернул 404 — скорее всего, bot_token указан неверно.", **extra)
        if code == 429:
            return NotifyError(f"Telegram: слишком много сообщений, попробуйте позже ({desc}).", **extra)
        return NotifyError(f"Telegram: ошибка {code}: {desc}", **extra)

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
