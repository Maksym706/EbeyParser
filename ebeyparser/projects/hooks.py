"""Wire build-project alerts to a running Monitor — in every mode that runs one: the web app
(`run`), the headless loop (`monitor`) and a single pass (`once`).

attach() is idempotent per monitor: the web app attaching to a monitor the CLI already wired only
updates the wiring (event publishing, notifiers), it never registers a second hook. It also
registers the notify.extras provider, so a normal deal alert about a project part carries the
project context («📦 Для сборки …») instead of a second message.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..notify import extras
from .market import Market
from .tracker import ProjectAlerter

log = logging.getLogger(__name__)

EXTRAS_NAME = "projects"


def _monitor_notifiers(monitor: Any) -> Callable[[], list[Any]]:
    """The monitor's own channels (built when a pass starts), else fresh ones from its config."""

    def get() -> list[Any]:
        own = getattr(monitor, "_notifiers", None)
        if own is not None:
            return list(own)
        config = getattr(monitor, "config", None)
        if config is None:
            return []
        from ..notify.base import build_notifiers

        return build_notifiers(config.notifications, web_base_url=getattr(monitor, "web_base_url", None))

    return get


def _monitor_project_url(monitor: Any) -> Callable[[int], str | None]:
    def url(project_id: int) -> str | None:
        base = (getattr(monitor, "web_base_url", None) or "").strip().rstrip("/")
        return f"{base}/projects/{project_id}" if base else None

    return url


def attach(monitor: Any, db: Any, *, config: Callable[[], Any] | None = None,
           notifiers: Callable[[], list[Any]] | None = None, publish: Callable[[dict[str, Any]], Any] | None = None,
           project_url: Callable[[int], str | None] | None = None, market: Market | None = None) -> ProjectAlerter:
    """The monitor's ProjectAlerter: created and hooked into monitor.on_event once, later calls
    only update what they pass."""
    alerter = getattr(monitor, "project_alerter", None)
    if isinstance(alerter, ProjectAlerter):
        alerter.configure(config=config, notifiers=notifiers, publish=publish, project_url=project_url, market=market)
    else:
        cfg = config or (lambda: getattr(monitor, "config", None))
        alerter = ProjectAlerter(db, config=cfg, notifiers=notifiers or _monitor_notifiers(monitor), publish=publish,
                                 project_url=project_url or _monitor_project_url(monitor),
                                 market=market or Market(db, cfg()))
        hooks = getattr(monitor, "on_event", None)
        if isinstance(hooks, list):
            hooks.append(alerter.on_event)
        else:
            log.warning("monitor has no on_event hooks: build-project alerts are off")
        monitor.project_alerter = alerter
    extras.register(EXTRAS_NAME, alerter.context_lines)
    return alerter


def attached(monitor: Any) -> ProjectAlerter | None:
    alerter = getattr(monitor, "project_alerter", None)
    return alerter if isinstance(alerter, ProjectAlerter) else None


async def drain(monitor: Any) -> None:
    """Let the checks started by the last events finish (before a `once` pass exits)."""
    alerter = attached(monitor)
    if alerter is not None:
        await alerter.drain()


__all__ = ["EXTRAS_NAME", "attach", "attached", "drain"]
