"""JSON API v1 for the single-page UI: everything the user needs without the CLI or editing
config.yaml / .env by hand.

Mounted by web.app.create_app under /api/v1 (behind the same Host allow-list, access token
and same-origin checks as the rest of the app). Errors: {"error": {"code", "message_ru",
"fields"?}}. Live updates: GET /api/v1/events (Server-Sent Events).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import APIRouter

from .context import ApiContext
from .errors import API_PREFIX, install_error_handlers

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

TAGS = [
    {"name": "app", "description": "Состояние приложения и онбординг"},
    {"name": "settings", "description": "Настройки, секреты (.env), доступ с телефона"},
    {"name": "setup", "description": "Мастер: города, категории, пресеты, оценка нагрузки, сохранение"},
    {"name": "connections", "description": "Нейросеть, Telegram, почта, eBay, тестовые уведомления"},
    {"name": "searches", "description": "Поиски"},
    {"name": "monitor", "description": "Проверки, пауза, события (SSE), задачи, проверка ссылки"},
    {"name": "deals", "description": "Лента, сделка, «Мои сделки», статистика, экспорт"},
    {"name": "system", "description": "Состояние, логи, демо, резервная копия, сброс данных"},
    {"name": "projects", "description": "Сборки: план из б/у деталей, отслеживание, лучшие предложения, покупки"},
]


def build_router() -> APIRouter:
    from . import routes_app, routes_connect, routes_deals, routes_monitor, routes_searches, routes_setup, routes_system

    router = APIRouter(prefix=API_PREFIX)
    router.include_router(routes_app.router, tags=["app"])
    router.include_router(routes_setup.router, tags=["setup"])
    router.include_router(routes_connect.router, tags=["connections"])
    router.include_router(routes_searches.router, tags=["searches"])
    router.include_router(routes_monitor.router, tags=["monitor"])
    router.include_router(routes_deals.router, tags=["deals"])
    router.include_router(routes_system.router, tags=["system"])
    from . import routes_projects

    router.include_router(routes_projects.router, tags=["projects"])
    return router


def monitor_bridge(ctx: ApiContext) -> Callable[[str, dict[str, Any]], None]:
    """Monitor.on_event hook -> event hub (deal_found gets the full deal card)."""

    def hook(kind: str, data: dict[str, Any]) -> None:
        payload = dict(data or {})
        if kind == "deal_found" and payload.get("ad_id"):
            try:
                from .presenters import deal_card

                found = ctx.db.get_deal_extras(str(payload["ad_id"]))
                if found is not None:
                    payload["card"] = deal_card(found[0], found[1], config=ctx.config)
            except Exception:  # noqa: BLE001 - an event must never break monitoring
                log.exception("deal_found card failed")
        ctx.hub.publish(kind, payload)

    return hook


def mount_api_v1(
    app: FastAPI,
    *,
    category_discovery: Callable[[str, int], Awaitable[Any]] | None = None,
    ai_probe: Callable[[str], Any] | None = None,
    bind_host: str | None = None,
) -> ApiContext:
    """Add /api/v1 to `app` (created by web.app.create_app); returns the context (app.state.api)."""
    ctx = ApiContext(app, category_discovery=category_discovery, ai_probe=ai_probe, bind_host=bind_host)
    app.state.api = ctx
    app.state.api_events = ctx.hub
    install_error_handlers(app)
    app.include_router(build_router())
    hooks = getattr(app.state.monitor, "on_event", None)
    if isinstance(hooks, list):
        hooks.append(monitor_bridge(ctx))
    from .routes_projects import install_project_alerts

    install_project_alerts(ctx)  # «Сборки»: alerts from deal_found / deal_updated / run_finished on the hub
    if app.openapi_tags is None:
        app.openapi_tags = list(TAGS)
    return ctx


__all__ = ["ApiContext", "build_router", "mount_api_v1", "monitor_bridge"]
