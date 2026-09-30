"""Serve the single-page app (ebeyparser/web/app/): static files under /app, index.html for "/"
and for every other browser path that is not an API, static or classic-page address (deep links
such as /deal/123 or /settings/ai work after a reload). Everything is local: no build step, no CDN.

Content types are fixed here instead of trusting `mimetypes`: on Windows the registry can map
.js to text/plain, and browsers refuse to run ES modules served like that."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

SPA_DIR = Path(__file__).resolve().parent / "app"
SPA_PREFIX = "/app"
CLASSIC_PREFIX = "/classic"
# paths the SPA fallback never answers (they 404 as JSON / the classic error page instead)
NON_SPA_PREFIXES = ("/api", "/app", "/static", CLASSIC_PREFIX)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


class SPAStaticFiles(StaticFiles):
    """StaticFiles with fixed content types and `Cache-Control: no-cache` (the browser revalidates
    with ETag, so an updated app is picked up at once while unchanged files answer 304)."""

    def file_response(self, full_path: Any, stat_result: os.stat_result, scope: Any, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        content_type = CONTENT_TYPES.get(Path(str(full_path)).suffix.lower())
        if content_type and response.status_code != 304:
            response.headers["content-type"] = content_type
        response.headers["cache-control"] = "no-cache"
        response.headers["x-content-type-options"] = "nosniff"
        return response


def spa_available() -> bool:
    return (SPA_DIR / "index.html").is_file()


def index_response(status_code: int = 200) -> HTMLResponse:
    html = (SPA_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, status_code=status_code,
                        headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"})


def wants_spa(request: Request) -> bool:
    """True for a browser page address the SPA router handles (GET, not API/static/classic, no file extension)."""
    if request.method not in ("GET", "HEAD") or not spa_available():
        return False
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in NON_SPA_PREFIXES):
        return False
    last = path.rsplit("/", 1)[-1]
    return "." not in last  # /favicon.ico, /robots.txt, … stay 404


def install_spa(app: FastAPI) -> None:
    """Mount /app and answer "/" with the SPA. Unknown page paths are turned into the SPA by the
    app's 404 handler (see `wants_spa`), so route order never matters."""
    if not SPA_DIR.is_dir():
        return
    app.mount(SPA_PREFIX, SPAStaticFiles(directory=str(SPA_DIR)), name="spa")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def spa_index() -> Response:
        return index_response()


__all__ = ["CLASSIC_PREFIX", "SPA_DIR", "SPAStaticFiles", "index_response", "install_spa", "spa_available", "wants_spa"]
