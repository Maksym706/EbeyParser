"""One error shape for the whole /api/v1: {"error": {"code": "...", "message_ru": "..."}}.

`code` is a stable machine word (the UI switches on it), `message_ru` can be shown to the
user as is; validation errors also carry `fields`: {"general.interval_minutes": "message"}
(dotted field path -> Russian message, for inline errors next to the inputs).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# HTTP status -> default error code
STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "validation",
    429: "rate_limited",
    500: "internal",
    502: "upstream",
    503: "unavailable",
    504: "timeout",
}
STATUS_MESSAGES_RU: dict[int, str] = {
    401: "Нужен ключ доступа: открой ссылку с ?token=… из окна программы",
    403: "Доступ запрещён",
    404: "Нет такого адреса API",
    405: "Этот метод здесь не поддерживается",
    500: "Внутренняя ошибка сервера — подробности в логе",
}

FIELD_LABELS_RU: dict[str, str] = {
    "name": "Название",
    "url": "Ссылка",
    "query": "Запрос",
    "location": "Город",
    "radius_km": "Радиус",
    "min_price": "Цена от",
    "max_price": "Цена до",
    "min_profit": "Мин. прибыль",
    "min_roi": "Мин. ROI",
    "interval_minutes": "Как часто проверять",
    "status": "Статус",
    "note": "Заметка",
    "bought_price": "Цена покупки",
    "sold_price": "Цена продажи",
    "model": "Модель",
    "base_url": "Адрес сервера",
    "provider": "Сервер нейросети",
}


class ApiError(Exception):
    """Raise anywhere in /api/v1 handlers: becomes {"error": {...}} with `status`."""

    def __init__(self, status: int, code: str, message_ru: str, *, fields: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message_ru)
        self.status = status
        self.code = code
        self.message_ru = message_ru
        self.fields = fields
        self.headers = headers


def error_body(code: str, message_ru: str, fields: dict[str, str] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message_ru": message_ru}
    if fields:
        error["fields"] = fields
    return {"error": error}


def error_response(status: int, code: str, message_ru: str, *, fields: dict[str, str] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error_body(code, message_ru, fields), status_code=status, headers=headers)


def not_found(message_ru: str) -> ApiError:
    return ApiError(404, "not_found", message_ru)


def bad_request(message_ru: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message_ru)


def is_api_v1(request: Request) -> bool:
    path = request.url.path
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def _field_name(loc: tuple[Any, ...] | list[Any]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return ".".join(parts)


def field_errors(errors: list[Any], prefix: str = "") -> dict[str, str]:
    """pydantic / FastAPI error dicts -> {"dotted.field": "Russian message"}."""
    out: dict[str, str] = {}
    for err in errors:
        loc = err.get("loc", ()) if isinstance(err, dict) else ()
        name = _field_name(loc)
        if prefix:
            name = f"{prefix}.{name}" if name else prefix
        msg = _message_ru(err) if isinstance(err, dict) else str(err)
        out.setdefault(name or "_", msg)
    return out


_PYDANTIC_RU = {
    "missing": "обязательное поле",
    "float_parsing": "нужно число",
    "int_parsing": "нужно целое число",
    "int_from_float": "нужно целое число",
    "bool_parsing": "нужно да/нет",
    "string_type": "нужен текст",
    "list_type": "нужен список",
    "literal_error": "недопустимое значение",
    "greater_than_equal": "слишком маленькое значение",
    "less_than_equal": "слишком большое значение",
    "greater_than": "слишком маленькое значение",
    "less_than": "слишком большое значение",
    "extra_forbidden": "неизвестное поле",
    "string_too_long": "слишком длинный текст",
    "url_parsing": "неверная ссылка",
    "json_invalid": "неверный JSON",
}


def _message_ru(err: dict[str, Any]) -> str:
    kind = str(err.get("type", ""))
    text = _PYDANTIC_RU.get(kind)
    if kind == "literal_error":
        expected = (err.get("ctx") or {}).get("expected")
        return f"допустимо: {expected}" if expected else "недопустимое значение"
    if text and kind in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
        ctx = err.get("ctx") or {}
        bound = next(iter(ctx.values()), None)
        sign = {"greater_than_equal": "не меньше", "less_than_equal": "не больше",
                "greater_than": "больше", "less_than": "меньше"}[kind]
        return f"{sign} {bound}" if bound is not None else text
    if text:
        return text
    msg = str(err.get("msg", "неверное значение"))
    return msg.removeprefix("Value error, ")


def validation_error(exc: ValidationError | list[Any] | dict[str, str], message_ru: str = "Проверь введённые данные",
                     *, prefix: str = "") -> ApiError:
    """422 {"error": {"code": "validation", "message_ru", "fields": {field: message}}}."""
    if isinstance(exc, dict):
        fields = dict(exc)
    else:
        errors = exc.errors() if isinstance(exc, ValidationError) else list(exc)
        fields = field_errors(errors, prefix)
    text = message_ru
    if fields:
        text = f"{message_ru}: " + "; ".join(_labelled(k, v) for k, v in list(fields.items())[:3])
    return ApiError(422, "validation", text, fields=fields)


def _labelled(field: str, message: str) -> str:
    label = FIELD_LABELS_RU.get(field.rsplit(".", 1)[-1], "")
    return f"«{label}» — {message}" if label else (f"{field}: {message}" if field and field != "_" else message)


def install_error_handlers(app: FastAPI) -> None:
    """ApiError everywhere; HTTP / validation errors in the /api/v1 shape for /api/v1 paths only
    (other paths keep the handlers the app already had)."""

    async def api_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ApiError)
        return error_response(exc.status, exc.code, exc.message_ru, fields=exc.fields, headers=exc.headers)

    previous_http: Callable[[Request, Exception], Awaitable[Response]] | None = \
        app.exception_handlers.get(StarletteHTTPException)  # type: ignore[assignment]
    previous_validation = app.exception_handlers.get(RequestValidationError)

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        if is_api_v1(request):
            status = exc.status_code
            detail = exc.detail if isinstance(exc.detail, str) else ""
            if detail in ("Not Found", "Method Not Allowed", "Forbidden", "Unauthorized", "Internal Server Error"):
                detail = ""
            message = detail or STATUS_MESSAGES_RU.get(status, f"Ошибка {status}")
            return error_response(status, STATUS_CODES.get(status, "error"), message,
                                  headers=getattr(exc, "headers", None))
        if previous_http is not None:
            return await previous_http(request, exc)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    async def validation(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        if is_api_v1(request):
            err = validation_error(list(exc.errors()))
            return error_response(err.status, err.code, err.message_ru, fields=err.fields)
        if previous_validation is not None:
            return await previous_validation(request, exc)  # type: ignore[misc]
        return await request_validation_exception_handler(request, exc)

    previous_crash = app.exception_handlers.get(Exception) or app.exception_handlers.get(500)

    async def crash(request: Request, exc: Exception) -> Response:
        if is_api_v1(request):
            log.error("API %s %s failed: %s", request.method, request.url.path, exc)
            return error_response(500, "internal", STATUS_MESSAGES_RU[500])
        if previous_crash is not None:
            return await previous_crash(request, exc)  # type: ignore[misc]
        return PlainTextResponse("Internal Server Error", status_code=500)

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation)
    app.add_exception_handler(Exception, crash)


__all__ = [
    "API_PREFIX",
    "ApiError",
    "bad_request",
    "error_body",
    "error_response",
    "field_errors",
    "install_error_handlers",
    "is_api_v1",
    "not_found",
    "validation_error",
]
