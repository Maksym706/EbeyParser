"""The user's time zone (general.timezone, default Europe/Berlin) and Russian time labels.

Every server-generated text with a time in it ("пауза до 21:34", "следующая проверка в 21:40",
Telegram / e-mail messages, the log file) goes through here, so it never shows the host's
time zone (UTC in Docker) or a raw ISO timestamp. Machine fields stay ISO/UTC.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, tzinfo
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Berlin"


def _zone(name: str) -> tzinfo | None:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name / no tzdata
        return None


def is_valid_timezone(name: str) -> bool:
    return bool((name or "").strip()) and _zone(name.strip()) is not None


_fallback = _zone(DEFAULT_TIMEZONE) or timezone.utc
_current: tzinfo = _fallback
_current_name = DEFAULT_TIMEZONE


def set_timezone(name: str | None) -> tzinfo:
    """Use `name` (IANA, e.g. "Europe/Berlin") for every local time from now on; an unknown
    name keeps Europe/Berlin (logged)."""
    global _current, _current_name
    wanted = (name or "").strip() or DEFAULT_TIMEZONE
    zone = _zone(wanted)
    if zone is None:
        log.warning("Unknown time zone %r, using %s", wanted, DEFAULT_TIMEZONE)
        zone, wanted = _fallback, DEFAULT_TIMEZONE
    _current, _current_name = zone, wanted
    return zone


def apply_config(config: Any) -> None:
    """set_timezone(config.general.timezone) for any AppConfig-like object (never raises)."""
    try:
        set_timezone(getattr(getattr(config, "general", None), "timezone", None))
    except Exception:  # noqa: BLE001
        log.exception("time zone not applied")


def local_tz() -> tzinfo:
    return _current


def timezone_name() -> str:
    return _current_name


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_local(dt: datetime) -> datetime:
    return aware(dt).astimezone(_current)


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(_current)


def hhmm(dt: datetime | None) -> str:
    """'21:34' in the user's time zone ('' for None)."""
    return "" if dt is None else f"{to_local(dt):%H:%M}"


def when_label(dt: datetime | None, now: datetime | None = None) -> str:
    """'21:34' today, 'завтра в 03:10', '30.09 в 03:10' otherwise ('' for None)."""
    if dt is None:
        return ""
    local = to_local(dt)
    today = (to_local(now) if now is not None else now_local()).date()
    delta = (local.date() - today).days
    if delta == 0:
        return f"{local:%H:%M}"
    if delta == 1:
        return f"завтра в {local:%H:%M}"
    if delta == -1:
        return f"вчера в {local:%H:%M}"
    return f"{local:%d.%m} в {local:%H:%M}"


def at_label(dt: datetime | None, now: datetime | None = None) -> str:
    """'в 21:34' today, 'завтра в 03:10', '30.09 в 03:10' ('' for None) — for "продолжу в 21:34"."""
    label = when_label(dt, now)
    return f"в {label}" if label and label[0].isdigit() and len(label) == 5 else label


def until_label(dt: datetime | None, now: datetime | None = None) -> str:
    """'до 21:34' / 'до завтра 03:10' / 'до 30.09 03:10' ('' for None)."""
    label = when_label(dt, now)
    if not label:
        return ""
    return "до " + label.replace("завтра в ", "завтра ").replace(" в ", " ")


def date_label(dt: datetime | None) -> str:
    """'29.09.2026' in the user's time zone ('' for None)."""
    return "" if dt is None else f"{to_local(dt):%d.%m.%Y}"


def epoch_label(ts: float, now: float | None = None) -> str:
    """when_label for epoch seconds."""
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    ref = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return when_label(when, ref)


class LocalFormatter(logging.Formatter):
    """logging.Formatter whose %(asctime)s is in the user's time zone (not the host's)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        local = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone(_current)
        if datefmt:
            return local.strftime(datefmt)
        return f"{local:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}"


__all__ = [
    "DEFAULT_TIMEZONE",
    "LocalFormatter",
    "apply_config",
    "at_label",
    "aware",
    "date_label",
    "epoch_label",
    "hhmm",
    "is_valid_timezone",
    "local_tz",
    "now_local",
    "set_timezone",
    "timezone_name",
    "to_local",
    "until_label",
    "when_label",
]
