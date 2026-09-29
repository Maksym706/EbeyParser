"""Extra lines other features add to a deal notification — so one ad is one message.

A feature registers a provider `(listing, evaluation) -> list[str]` under a name (a second
registration under the same name replaces the first). notify.render asks every provider when it
builds a deal (Telegram, e-mail, plain text) and shows the lines under the money block, e.g.
«📦 Для сборки «LLM-сервер»: RTX 3090 24 ГБ за 580 € → итог 1 240 € из 1 500 €».

A provider must be quick and must not raise (errors are logged and ignored); at most
MAX_LINES lines per deal, each at most MAX_LINE_LEN characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..models import Evaluation, Listing

log = logging.getLogger(__name__)

MAX_LINES = 3
MAX_LINE_LEN = 200

Provider = Callable[["Listing", "Evaluation | None"], list[str]]
_providers: dict[str, Provider] = {}


def register(name: str, provider: Provider) -> None:
    _providers[name] = provider


def unregister(name: str) -> None:
    _providers.pop(name, None)


def providers() -> dict[str, Provider]:
    return dict(_providers)


def lines_for(listing: Listing, evaluation: Evaluation | None = None) -> list[str]:
    """Every provider's lines for this deal (deduplicated, capped); never raises."""
    out: list[str] = []
    for name, provider in list(_providers.items()):
        try:
            lines = provider(listing, evaluation) or []
        except Exception:  # noqa: BLE001 - an add-on must never break a notification
            log.exception("notification extras %r failed", name)
            continue
        for line in lines:
            text = " ".join(str(line or "").split())[:MAX_LINE_LEN]
            if text and text not in out:
                out.append(text)
    return out[:MAX_LINES]


__all__ = ["MAX_LINES", "Provider", "lines_for", "providers", "register", "unregister"]
