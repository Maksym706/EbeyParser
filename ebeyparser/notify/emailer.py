"""E-mail notifications over SMTP (STARTTLS on 587 or SSL on 465)."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from ..config import EmailConfig
from ..models import DealView
from .base import NotifyError
from .render import email_subject, render_email_html, render_text

log = logging.getLogger(__name__)

SMTPFactory = Callable[..., smtplib.SMTP]  # (host, port, *, use_ssl, timeout) -> SMTP-like


def default_smtp_factory(host: str, port: int, *, use_ssl: bool, timeout: float) -> smtplib.SMTP:
    """Open a connection: implicit TLS for use_ssl, plain SMTP (STARTTLS follows) otherwise."""
    if use_ssl:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def _auth_hint(host: str) -> str:
    h = host.lower()
    if "gmail" in h or "googlemail" in h:
        return (
            "Для Gmail нужен «Пароль приложения» (App Password), а не обычный пароль: включите "
            "двухэтапную аутентификацию и создайте пароль на https://myaccount.google.com/apppasswords"
        )
    if "gmx" in h or "web.de" in h:
        return "Для GMX/Web.de включите в настройках почты доступ по POP3/IMAP/SMTP."
    if "outlook" in h or "office365" in h or "hotmail" in h or "live.com" in h:
        return "Outlook/Hotmail часто блокирует вход по паролю для SMTP — удобнее использовать Gmail с паролем приложения."
    return "Проверьте логин и пароль; если включена двухэтапная защита, нужен пароль приложения."


class EmailNotifier:
    """Sends one HTML + plain-text e-mail per `send()` call.

    `smtp_factory(host, port, *, use_ssl, timeout)` returns an smtplib.SMTP-like
    object; inject a fake one in tests."""

    name = "email"

    def __init__(
        self,
        cfg: EmailConfig,
        *,
        web_base_url: str | None = None,
        smtp_factory: SMTPFactory | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.web_base_url = web_base_url
        self._factory = smtp_factory or default_smtp_factory
        self.timeout = timeout

    def build_message(self, deals: list[DealView], *, title: str | None = None) -> EmailMessage:
        cfg = self.cfg
        sender = (cfg.from_addr or cfg.username).strip()
        name, addr = parseaddr(sender)
        if not addr or "@" not in addr:
            raise NotifyError("E-mail: не указан адрес отправителя (from_addr или username).")
        recipients = [a.strip() for a in cfg.to_addrs if a.strip()]
        if not recipients:
            raise NotifyError("E-mail: не указаны получатели (to_addrs).")

        subject = " ".join((title or email_subject(deals)).split())  # no CR/LF in headers
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((name or "EbeyParser", addr))
        msg["To"] = ", ".join(recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=addr.rsplit("@", 1)[-1] or "ebeyparser.local")
        msg.set_content(render_text(deals, title=title, web_base_url=self.web_base_url))
        msg.add_alternative(
            render_email_html(deals, title=title, web_base_url=self.web_base_url), subtype="html"
        )
        return msg

    async def send(self, deals: list[DealView], *, title: str | None = None) -> None:
        if not deals:
            return
        msg = self.build_message(deals, title=title)
        await asyncio.to_thread(self._send_sync, msg)
        log.info("E-mail отправлен: %s (%d шт.)", msg["To"], len(deals))

    async def send_text(self, text: str) -> None:
        """A plain service message (health alert, heartbeat); the first line is the subject."""
        text = text.strip()
        if not text:
            return
        cfg = self.cfg
        sender = (cfg.from_addr or cfg.username).strip()
        name, addr = parseaddr(sender)
        recipients = [a.strip() for a in cfg.to_addrs if a.strip()]
        if not addr or "@" not in addr or not recipients:
            raise NotifyError("E-mail: не указан отправитель или получатели.")
        first = " ".join(text.splitlines()[0].split())
        msg = EmailMessage()
        msg["Subject"] = "EbeyParser: " + (first[:90] + "…" if len(first) > 90 else first)
        msg["From"] = formataddr((name or "EbeyParser", addr))
        msg["To"] = ", ".join(recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=addr.rsplit("@", 1)[-1] or "ebeyparser.local")
        msg.set_content(text)
        await asyncio.to_thread(self._send_sync, msg)
        log.info("E-mail (служебное) отправлен: %s", msg["Subject"])

    def _send_sync(self, msg: EmailMessage) -> None:
        cfg = self.cfg
        host, port = cfg.smtp_host.strip(), cfg.smtp_port
        smtp = None
        try:
            smtp = self._factory(host, port, use_ssl=cfg.use_ssl, timeout=self.timeout)
            if not cfg.use_ssl:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if cfg.username:
                smtp.login(cfg.username.strip(), cfg.password)
            smtp.send_message(msg)
        except NotifyError:
            raise
        except Exception as exc:  # noqa: BLE001 - everything becomes a readable NotifyError
            raise NotifyError(self._explain(exc)) from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001 - connection may already be gone
                    pass

    def _explain(self, exc: BaseException) -> str:
        cfg = self.cfg
        where = f"{cfg.smtp_host}:{cfg.smtp_port}"
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return f"E-mail: сервер {where} не принял логин/пароль ({_smtp_reply(exc)}). {_auth_hint(cfg.smtp_host)}"
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            bad = ", ".join(exc.recipients) or ", ".join(cfg.to_addrs)
            return f"E-mail: сервер отклонил адреса получателей: {bad}. Проверьте to_addrs."
        if isinstance(exc, smtplib.SMTPSenderRefused):
            return (
                f"E-mail: сервер отклонил адрес отправителя {exc.sender} ({_smtp_reply(exc)}). "
                "У Gmail и большинства почтовых сервисов from_addr должен совпадать с username."
            )
        if isinstance(exc, smtplib.SMTPNotSupportedError):
            return (
                f"E-mail: сервер {where} не поддерживает STARTTLS/AUTH. "
                "Попробуйте порт 465 с use_ssl: true или порт 587 с use_ssl: false."
            )
        if isinstance(exc, smtplib.SMTPServerDisconnected):
            return (
                f"E-mail: сервер {where} разорвал соединение. Частая причина — неверная пара порт/use_ssl "
                "(465 → use_ssl: true, 587 → use_ssl: false)."
            )
        if isinstance(exc, ssl.SSLError):
            return (
                f"E-mail: ошибка TLS при подключении к {where} ({exc.__class__.__name__}). "
                "Проверьте порт и use_ssl (465 → use_ssl: true, 587 → use_ssl: false)."
            )
        if isinstance(exc, socket.gaierror):
            return f"E-mail: не удалось найти SMTP-сервер «{cfg.smtp_host}». Проверьте smtp_host и интернет."
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return f"E-mail: таймаут подключения к {where}. Проверьте smtp_host/smtp_port и файрвол."
        if isinstance(exc, ConnectionRefusedError):
            return f"E-mail: сервер {where} отказал в подключении. Проверьте smtp_port."
        if isinstance(exc, smtplib.SMTPResponseException):
            return f"E-mail: ошибка SMTP на {where}: {_smtp_reply(exc)}"
        if isinstance(exc, (smtplib.SMTPException, OSError)):
            return f"E-mail: не удалось отправить письмо через {where}: {exc}"
        return f"E-mail: неожиданная ошибка при отправке ({exc.__class__.__name__}: {exc})"


def _smtp_reply(exc: BaseException) -> str:
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = " ".join(text.split())[:200]
    return f"{code} {text}".strip() if code else text or exc.__class__.__name__
