"""One place that turns any failure into a friendly Russian message with a concrete next step.

Everything the web UI shows (API errors, connection tests, run errors, health problems,
Telegram / e-mail service messages) goes through `humanize` (an exception) or `humanize_text`
(a stored string). The result never names CLI commands, config files, dotted config keys or
exception classes; the technical text goes to `details` (the UI shows it collapsed under
«Подробнее») and to the log. CLI output keeps its own, more technical wording.
"""

from __future__ import annotations

import asyncio
import errno
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any

# service -> (nominative, "with ..." form, "to ..." form)
SERVICES: dict[str, tuple[str, str, str]] = {
    "telegram": ("Telegram", "Telegram", "Telegram"),
    "email": ("Почтовый сервер", "почтовым сервером", "почтового сервера"),
    "ebay": ("eBay", "eBay", "eBay"),
    "ai": ("Сервер нейросети", "сервером нейросети", "сервера нейросети"),
    "kleinanzeigen": ("Kleinanzeigen", "Kleinanzeigen", "Kleinanzeigen"),
    "": ("Сервис", "сервисом", "сервиса"),
}
ACTIONS: dict[str, dict[str, str]] = {
    "telegram": {"label_ru": "Настроить Telegram", "href": "/settings/notifications"},
    "email": {"label_ru": "Настроить почту", "href": "/settings/notifications"},
    "ebay": {"label_ru": "Подключить eBay", "href": "/settings/ebay"},
    "ai": {"label_ru": "Настройки нейросети", "href": "/settings/ai"},
    "kleinanzeigen": {"label_ru": "Снизить нагрузку", "href": "/settings/region"},
}
LMSTUDIO_DOWN = "LM Studio не отвечает. Открой LM Studio → Developer → Start Server"
AI_DOWN_RU = "Нейросеть не отвечает — фото объявлений сейчас не проверяются"
OLLAMA_DOWN = "Ollama не отвечает. Запусти приложение Ollama и попробуй снова"
GENERIC = "Что-то пошло не так — попробуй ещё раз. Если повторится, загляни в «Состояние» → «Журнал»"

_NETWORK_ERRNOS = {
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
    errno.EAFNOSUPPORT, errno.ECONNABORTED, errno.ENETDOWN, getattr(errno, "EHOSTDOWN", -1),
}
_NETWORK_WORDS = (
    "connecterror", "connecttimeout", "connection refused", "connection reset", "network is unreachable",
    "name or service not known", "nodename nor servname", "getaddrinfo failed", "temporary failure in name",
    "no address associated", "address family not supported", "errno", "remoteprotocolerror",
    "server disconnected", "failed to establish", "no route to host", "ssl: ", "certificate verify failed",
)
_PROXY_WORDS = ("host not in allowlist", "egress", "proxy", "tunnel connection failed", "407")
_TIMEOUT_WORDS = ("timed out", "timeout", "readtimeout", "не ответил за", "не ответила за", "не ответил вовремя",
                  "таймаут")


@dataclass
class Human:
    """A failure as the UI shows it."""

    message_ru: str
    details: str = ""
    code: str = "error"
    action: dict[str, str] | None = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"message_ru": self.message_ru, "code": self.code}
        if self.details:
            out["details"] = self.details
        if self.action:
            out["action"] = dict(self.action)
        return out


# ------------------------------------------------------------------ helpers
def service_name(service: str) -> str:
    return SERVICES.get(service, SERVICES[""])[0]


def _with(service: str) -> str:
    return SERVICES.get(service, SERVICES[""])[1]


def _of(service: str) -> str:
    return SERVICES.get(service, SERVICES[""])[2]


def email_provider(host: str) -> str:
    """'smtp.gmail.com' -> 'Gmail' (for "Не достучался до Gmail")."""
    h = (host or "").lower()
    for words, label in ((("gmail", "googlemail"), "Gmail"), (("gmx",), "GMX"), (("web.de",), "Web.de"),
                         (("outlook", "office365", "hotmail", "live.com"), "Outlook"), (("yandex",), "Яндекс Почты"),
                         (("mail.ru",), "Mail.ru"), (("t-online",), "T-Online"), (("icloud", "me.com"), "iCloud")):
        if any(w in h for w in words):
            return label
    return "почтового сервера"


def _technical(exc: BaseException) -> str:
    text = str(exc).strip()
    name = type(exc).__name__
    if not text:
        return name
    return text if name in text else f"{name}: {text}"


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    node: BaseException | None = exc
    while node is not None and node not in out and len(out) < 6:
        out.append(node)
        node = node.__cause__ or node.__context__
    return out


def _is_type(exc: BaseException, module: str, *names: str) -> bool:
    for cls in type(exc).__mro__:
        if cls.__module__.startswith(module) and cls.__name__ in names:
            return True
    return False


def no_connection(service: str, host: str = "") -> str:
    if service == "ai":
        return OLLAMA_DOWN if "11434" in host or "ollama" in host.lower() else LMSTUDIO_DOWN
    if service == "email":
        return f"Не достучался до {email_provider(host)} — проверь интернет и попробуй ещё раз"
    if service == "kleinanzeigen":
        return "Нет связи с Kleinanzeigen — проверь интернет. Попробую снова на следующей проверке"
    if not service:
        return "Нет связи с сайтом — проверь интернет. Попробую снова на следующей проверке"
    return f"Нет связи с {_with(service)} — проверь интернет и попробуй ещё раз"


def network_blocked(service: str) -> str:
    """A proxy / firewall / VPN answered instead of the service (e.g. "Host not in allowlist")."""
    name = service_name(service) if service else "Сайт"
    return f"{name} недоступен из этой сети — проверь интернет или VPN и попробуй ещё раз"


def timed_out(service: str, host: str = "") -> str:
    if service == "ai":
        return ("Нейросеть не ответила вовремя — возможно, модель слишком большая для этого компьютера. "
                "Выбери модель поменьше в настройках нейросети")
    if service == "email":
        return f"Не дождался ответа от {email_provider(host)} — проверь интернет и попробуй ещё раз"
    return f"{service_name(service)} не ответил вовремя — проверь интернет и попробуй ещё раз"


def status_message(status: int, service: str = "", *, method: str = "", description: str = "",
                   json_answer: bool = True) -> tuple[str, str]:
    """(code, Russian message) for an HTTP status from `service`."""
    name = service_name(service)
    low = (description or "").lower()
    if any(w in low for w in _PROXY_WORDS) or (status in (403, 407) and not json_answer):
        return "network", network_blocked(service)
    if service == "telegram":
        if status in (401, 404):
            return "telegram_token_invalid", "Telegram не узнал ключ бота — скопируй его у @BotFather ещё раз"
        if status == 409:
            return "telegram_webhook", ("У бота включён webhook, поэтому я не вижу сообщения. "
                                        "Нажми «Сбросить webhook бота» и попробуй снова")
        if status == 400 and "chat not found" in low:
            return "telegram_chat_not_found", "Чат не найден — открой своего бота в Telegram и нажми Start"
        if status == 403:
            if "blocked by the user" in low:
                return "telegram_blocked", "Ты заблокировал бота в Telegram — открой бота и нажми «Перезапустить»"
            if "not a member" in low or "not enough rights" in low or "kicked" in low:
                return "telegram_blocked", "Бота нет в этой группе или у него нет прав — добавь бота в группу снова"
            if method in ("sendMessage", "sendPhoto", "") or "initiate" in low:
                return "telegram_blocked", "Бот пока не может тебе писать — открой бота в Telegram и нажми Start"
            return "telegram_forbidden", "Telegram отказал боту — скопируй ключ у @BotFather ещё раз"
        if status == 400:
            return "telegram_rejected", "Telegram не принял сообщение — попробуй ещё раз чуть позже"
    if service == "ebay" and status in (400, 401) and ("invalid_client" in low or status == 401):
        return "ebay_keys_invalid", ("eBay не принял ключи — проверь, что это ключи Production (не Sandbox) "
                                     "и что они активированы")
    if service == "ai":
        if status in (401, 403):
            return "ai_forbidden", "Сервер нейросети просит ключ доступа — проверь ключ в настройках нейросети"
        if status == 404:
            return "ai_model_missing", "На сервере нейросети нет этой модели — скачай её в LM Studio или выбери другую"
    if status == 401:
        return "unauthorized", f"{name} не принял ключ или пароль — проверь их и попробуй ещё раз"
    if status == 403:
        if service == "kleinanzeigen":
            return "blocked", "Kleinanzeigen временно не пускает (защита от ботов) — попробую позже сам"
        return "forbidden", f"{name} отказал в доступе — попробуй позже"
    if status == 404:
        return "not_found", f"{name}: адрес не найден — попробуй ещё раз позже"
    if status == 429:
        return "rate_limited", f"{name} просит подождать: слишком много запросов. Попробуй через пару минут"
    if status >= 500:
        return "upstream", f"У {_of(service)} сейчас сбой на их стороне — попробуй позже"
    return "upstream", f"{name} ответил ошибкой — попробуй ещё раз позже"


_STATUS_RE = re.compile(r"(?:HTTP|ошибк[аи]|status|код)\s*:?\s*(\d{3})\b|\b(\d{3})\s+(?:Client|Server) Error",
                        re.IGNORECASE)


def _status_in(text: str) -> int | None:
    m = _STATUS_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


# ----------------------------------------------------------------- humanize
def humanize(exc: BaseException, service: str = "", *, host: str = "") -> Human:
    """Any exception -> Human (friendly message + technical details)."""
    technical = _technical(exc)
    own = getattr(exc, "message_ru", None)
    if isinstance(own, str) and own:  # ApiError, NotifyError, PageLayoutError, ...: already friendly
        details = str(getattr(exc, "details", "") or "") or (technical if str(exc) != own else "")
        action = getattr(exc, "action", None) or ACTIONS.get(service or str(getattr(exc, "service", "") or ""))
        return Human(own, details, str(getattr(exc, "code", "") or "error"), action)
    service = service or str(getattr(exc, "service", "") or "")
    host = host or str(getattr(exc, "host", "") or "")
    for node in _chain(exc):
        human = _known(node, service, host)
        if human is not None:
            human.details = human.details or technical
            if human.action is None:
                human.action = ACTIONS.get(service)
            return human
    text = humanize_text(str(exc), service, host=host)
    return Human(text.message_ru, technical, text.code, ACTIONS.get(service))


def _known(exc: BaseException, service: str, host: str) -> Human | None:  # noqa: C901, PLR0911
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout)) or _is_type(
            exc, "httpx", "TimeoutException"):
        return Human(timed_out(service, host), code="timeout")
    if _is_type(exc, "httpx", "ProxyError") or any(w in str(exc).lower() for w in ("host not in allowlist",)):
        return Human(network_blocked(service), code="network")
    if _is_type(exc, "httpx", "HTTPStatusError"):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", 0) or 0
        json_answer = "json" in str(getattr(response, "headers", {}).get("content-type", "")) if response else True
        code, text = status_message(status, service, description=_body(response), json_answer=json_answer)
        return Human(text, code=code)
    if _is_type(exc, "httpx", "ConnectError", "NetworkError", "RemoteProtocolError", "TransportError"):
        return Human(no_connection(service, host), code="network")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return Human(_email_auth(host), code="email_auth")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return Human("Почтовый сервер не принял адрес получателя — проверь, куда слать письма", code="email_to")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return Human("Почтовый сервер не принял адрес отправителя — он должен совпадать с адресом, "
                     "под которым ты входишь в почту", code="email_from")
    if isinstance(exc, (smtplib.SMTPNotSupportedError, smtplib.SMTPServerDisconnected, ssl.SSLError)):
        return Human("Не получилось установить защищённое соединение с почтой — выбери порт 587 "
                     "(или 465 с SSL) и попробуй снова", code="email_tls")
    if isinstance(exc, socket.gaierror):
        return Human(no_connection(service or "email", host), code="network")
    if isinstance(exc, ConnectionRefusedError):
        if service == "email":
            return Human("Почтовый сервер не принимает подключение на этом порту — выбери порт 587 "
                         "(или 465 с SSL)", code="email_port")
        return Human(no_connection(service, host), code="network")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _NETWORK_ERRNOS:
        return Human(no_connection(service, host), code="network")
    if isinstance(exc, smtplib.SMTPResponseException):
        return Human("Почтовый сервер отклонил письмо — проверь адреса и попробуй ещё раз", code="email_rejected")
    kind = type(exc).__name__
    if kind == "BlockedError":
        return Human(blocked_message(exc), code="blocked", action=ACTIONS["kleinanzeigen"])
    if kind == "RateBudgetExceeded":
        return Human(budget_message(exc), code="rate_limited")
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status >= 400 and service:
        code, text = status_message(status, service, description=str(exc))
        return Human(text, code=code)
    return None


def _body(response: Any) -> str:
    try:
        return str(response.text)[:300] if response is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _email_auth(host: str) -> str:
    provider = email_provider(host)
    if provider == "Gmail":
        return ("Gmail не принял логин или пароль. Нужен «пароль приложения», а не обычный пароль: "
                "создай его на myaccount.google.com/apppasswords")
    if provider in ("GMX", "Web.de"):
        return f"{provider} не принял логин или пароль. Включи в настройках почты доступ по SMTP и попробуй снова"
    return "Почта не приняла логин или пароль — проверь их. Если включена двухэтапная защита, нужен пароль приложения"


def blocked_message(exc: BaseException) -> str:
    from .timefmt import at_label

    until = getattr(exc, "cooldown_until", None)
    when = at_label(until) if until is not None else ""
    if when:
        return f"Kleinanzeigen попросил паузу — продолжу сам {when}. Это защита от блокировки"
    if getattr(exc, "cooling_down", False):
        return "Kleinanzeigen попросил паузу — продолжу сам позже. Это защита от блокировки"
    return "Kleinanzeigen временно не пускает (защита от ботов) — попробую позже сам"


def budget_message(exc: BaseException) -> str:
    from .timefmt import when_label

    retry = getattr(exc, "retry_at", None)
    when = f" около {when_label(retry)}" if retry is not None else " позже"
    return f"Лимит запросов к сайту на этот час исчерпан — это защита от блокировки. Продолжу{when}"


# ---------------------------------------------------------------------- AI
_MODEL_MISSING = ("не найдена", "not found", "нет модели", "не установлена", "не загружена")


def ai_problem(provider: str, base_url: str, model: str, error: str, *, server_ok: bool | None,
               cloud: str = "") -> str:
    """The one Russian explanation of a failed AI check (/health, the AI test, the tile)."""
    from .ai.cloud import detect_cloud, preset

    kind = "" if cloud == "-" else (cloud or detect_cloud(base_url))  # "-": a local server, whatever the address
    if kind:
        return _cloud_problem(kind, preset(kind).name if preset(kind) else "облако", model, error, server_ok)
    ollama = provider == "ollama" or "11434" in (base_url or "")
    low = (error or "").lower()
    if any(w in low for w in _MODEL_MISSING):
        if ollama:
            return f"В Ollama нет модели «{model}» — скачай её в Ollama или выбери другую модель в настройках нейросети"
        return (f"В LM Studio не загружена модель «{model}» — загрузи её в LM Studio или выбери другую "
                "в настройках нейросети")
    if any(w in low for w in _TIMEOUT_WORDS):
        return timed_out("ai")
    if server_ok is False or not error or any(w in low for w in _NETWORK_WORDS) or "недоступна по адресу" in low:
        return OLLAMA_DOWN if ollama else LMSTUDIO_DOWN
    return humanize_text(error, "ai", host=base_url).message_ru


def _cloud_problem(kind: str, name: str, model: str, error: str, server_ok: bool | None) -> str:
    """A free cloud endpoint (docs/design/CLOUD_AI.md): the key, the model, the quota, the network."""
    low = (error or "").lower()
    text = " ".join(str(error or "").split())
    if any(w in low for w in ("лимит", "подожд", "перегружен", "ebay", "берегу", "распределяет")):
        return text  # CloudLimited: already plain Russian
    if any(w in low for w in ("http 401", "http 403", "не подходит", "unauthorized", "invalid api key")):
        return f"Ключ {name} не подходит — вставь его заново в настройках нейросети"
    if "http 402" in low:
        return f"{name} просит пополнить баланс — выбери бесплатную модель (с «:free») в настройках нейросети"
    if any(w in low for w in _MODEL_MISSING) or "нет у" in low:
        return f"Модели «{model}» нет у {name} — выбери другую в настройках нейросети"
    if any(w in low for w in _TIMEOUT_WORDS) or "не ответило" in low:
        return f"{name} не ответил вовремя — бесплатные модели иногда перегружены, попробую позже"
    if kind == "omniroute" and (server_ok is False or any(w in low for w in _NETWORK_WORDS) or "не отвечает" in low):
        return "OmniRoute не отвечает — запусти его на этом компьютере"
    if server_ok is False or any(w in low for w in _NETWORK_WORDS) or "недоступно" in low:
        return f"Нет связи с {name} — проверь интернет. Если он есть, сервис временно недоступен"
    return humanize_text(error, "ai").message_ru if error else f"{name} не ответил"


# ------------------------------------------------------------ stored texts
_CLI_SENTENCE_RE = re.compile(
    r"[^.;!?\n]*(?:python -m ebeyparser|ebeyparser (?:run|init|debug-search|categories|setup)|debug-search"
    r"|ollama (?:serve|pull)|pip install|`[^`]+`)[^.;!?\n]*[.;!?]?", re.IGNORECASE)
_CONFIG_FILE_RE = re.compile(r"\s*(?:,\s*)?(?:или\s+|и\s+)?(?:в\s+)?(?:файле?\s+)?(?:config\.yaml(?:\s*/\s*\.env)?|\.env\b)",
                             re.IGNORECASE)
_KEY_RE = re.compile(r"\b(?:general|pricing|ai|notifications|ebay|web|searches)\.[a-z_]+(?:\.[a-z_]+)*\b")
_CLASS_RE = re.compile(r"\s*\((?:[A-Z][A-Za-z]+(?:Error|Exception|Timeout|Refused))\)")
_RAW_KEYS = re.compile(r"\b(?:to_addrs|from_addr|smtp_host|smtp_port|use_ssl|bot_token|chat_id|api_key|"
                       r"client_id|client_secret|oauth_token|interval_minutes|request_delay_seconds)\b")
KEY_LABELS_RU = {
    "ai.timeout_seconds": "время ожидания нейросети",
    "general.interval_minutes": "интервал проверок",
    "interval_minutes": "интервал проверок",
    "general.request_delay_seconds": "паузу между запросами",
    "request_delay_seconds": "паузу между запросами",
    "general.max_requests_per_hour": "лимит запросов в час",
    "to_addrs": "адрес получателя",
    "from_addr": "адрес отправителя",
    "smtp_host": "адрес почтового сервера",
    "smtp_port": "порт",
    "use_ssl": "SSL",
    "bot_token": "ключ бота",
    "chat_id": "чат",
    "api_key": "ключ",
    "client_id": "ключи eBay",
    "ebay.client_id": "ключи eBay",
    "ebay.client_secret": "ключи eBay",
    "client_secret": "ключи eBay",
}


def humanize_text(text: str, service: str = "", *, host: str = "") -> Human:
    """A stored / foreign error string -> Human. Known technical patterns become a friendly
    message; otherwise the text is kept minus CLI / config instructions."""
    raw = " ".join(str(text or "").split())
    low = raw.lower()
    if not raw:
        return Human(GENERIC, code="error")
    if any(w in low for w in ("host not in allowlist", "egress settings", "tunnel connection failed")):
        return Human(network_blocked(service), raw, "network")
    status = _status_in(raw)
    if status and service:
        code, message = status_message(status, service, description=raw)
        return Human(message, raw, code)
    if any(w in low for w in _TIMEOUT_WORDS) and service:
        return Human(timed_out(service, host), raw, "timeout")
    if any(w in low for w in _NETWORK_WORDS):
        return Human(no_connection(service, host), raw, "network")
    clean = sanitize(raw)
    if not clean or _looks_technical(clean):  # raw English / an exception: never shown as is
        return Human(GENERIC_BY_SERVICE.get(service, GENERIC), raw, "error")
    return Human(clean, raw if clean != raw else "", "error")


_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_EXC_NAME_RE = re.compile(r"\b[A-Z][A-Za-z]+(?:Error|Exception|Exceeded|Refused)\b")
GENERIC_BY_SERVICE = {
    "ai": "Нейросеть ответила с ошибкой — попробуй ещё раз. Если повторяется, перезапусти LM Studio",
    "telegram": "Telegram ответил с ошибкой — попробуй ещё раз чуть позже",
    "email": "Почта ответила с ошибкой — проверь настройки почты и попробуй ещё раз",
    "ebay": "eBay ответил с ошибкой — попробуй ещё раз чуть позже",
    "kleinanzeigen": "Kleinanzeigen ответил с ошибкой — попробую снова на следующей проверке",
}


def _looks_technical(text: str) -> bool:
    return not _CYRILLIC_RE.search(text) or bool(_EXC_NAME_RE.search(text))


def sanitize(text: str) -> str:
    """Drop CLI commands (the whole sentence), config file names, dotted config keys and
    exception class names from a Russian message."""
    raw = " ".join(str(text or "").split())
    out = _CLI_SENTENCE_RE.sub("", raw)
    out = _CONFIG_FILE_RE.sub("", out)
    out = _CLASS_RE.sub("", out)
    out = _KEY_RE.sub(lambda m: KEY_LABELS_RU.get(m.group(0), "нужную настройку"), out)
    out = _RAW_KEYS.sub(lambda m: KEY_LABELS_RU.get(m.group(0), "настройки"), out)
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out.rstrip(" ,;:—-").strip()


def friendly(text: str, service: str = "") -> str:
    """humanize_text(...).message_ru — for lists of stored strings."""
    return humanize_text(text, service).message_ru


def friendly_run_error(text: str) -> str:
    """A stored run error ("<search name>: <problem>") for the UI: the search name stays,
    the problem becomes friendly (old runs may hold technical text)."""
    raw = " ".join(str(text or "").split())
    head, sep, tail = raw.partition(": ")
    if sep and 0 < len(head) <= 80 and tail:
        service = "ebay" if head.lower().startswith("ebay") else ""
        return f"{head}: {humanize_text(tail, service).message_ru}"
    return humanize_text(raw).message_ru


__all__ = [
    "ACTIONS",
    "AI_DOWN_RU",
    "ai_problem",
    "GENERIC",
    "Human",
    "LMSTUDIO_DOWN",
    "OLLAMA_DOWN",
    "blocked_message",
    "budget_message",
    "email_provider",
    "friendly",
    "friendly_run_error",
    "humanize",
    "humanize_text",
    "network_blocked",
    "no_connection",
    "sanitize",
    "service_name",
    "status_message",
    "timed_out",
]
