"""The new single-page UI (ebeyparser/web/app): served at "/" and deep links, assets with the right
content types, everything vendored (no CDN), licences present, module imports resolvable."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ebeyparser.config import AppConfig
from ebeyparser.db import Database
from ebeyparser.web.app import create_app
from ebeyparser.web.spa import SPA_DIR

LOCAL = "http://127.0.0.1:8000"
# Outside pages the app may *link to* (opened in a new tab, never loaded by the page itself),
# plus XML namespace URIs inside vendored code.
ALLOWED_LINK_HOSTS = {
    "www.w3.org",  # SVG / XHTML / MathML namespaces
    "t.me",
    "lmstudio.ai",
    "ollama.com",
    "developer.ebay.com",
    "www.kleinanzeigen.de",
    "myaccount.google.com",
    "tailscale.com",
}
URL_RE = re.compile(r"""(?:https?:)?//([a-z0-9.-]+\.[a-z]{2,})(?=[/"'`\s)?#:]|$)""", re.I)


@pytest.fixture
def client() -> TestClient:
    app = create_app(AppConfig(), Database())
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def _files(*suffixes: str) -> list[Path]:
    return sorted(p for p in SPA_DIR.rglob("*") if p.is_file() and p.suffix in suffixes)


# ----------------------------------------------------------------------------- serving
def test_index_served_at_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert '<div id="app">' in r.text and '<script type="importmap">' in r.text
    assert 'src="/app/js/main.js"' in r.text


@pytest.mark.parametrize("path", [
    "/deal/2893790331",
    "/deals",
    "/deals/bought",
    "/searches",
    "/searches/new/category",
    "/health/logs",
    "/settings",
    "/settings/notifications",
    "/welcome",
    "/welcome/telegram",
    "/setup",  # old address: the SPA redirects it
    "/some/unknown/page",
])
def test_deep_links_return_the_spa(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, path
    assert '<div id="app">' in r.text


def test_non_page_paths_are_not_swallowed(client: TestClient) -> None:
    api = client.get("/api/v1/definitely-not-there")
    assert api.status_code == 404 and api.headers["content-type"].startswith("application/json")
    old_api = client.get("/api/nope")
    assert old_api.status_code == 404 and old_api.headers["content-type"].startswith("application/json")
    assert client.get("/app/js/missing.js").status_code == 404
    assert client.get("/favicon.ico").status_code == 404  # file-like paths are not pages
    assert client.post("/deal/1").status_code in (404, 405)  # only GET/HEAD fall back to the SPA
    assert '<div id="app">' not in client.get("/classic/nowhere").text


def test_classic_pages_live_under_classic(client: TestClient) -> None:
    r = client.get("/classic")
    assert r.status_code == 200 and "EbeyParser" in r.text and 'href="/classic/searches"' in r.text
    for path in ("/classic/searches", "/classic/status", "/classic/setup", "/classic/settings"):
        assert client.get(path).status_code == 200, path


# ----------------------------------------------------------------------------- assets
CONTENT_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain",
}


def test_static_assets_have_correct_content_types(client: TestClient) -> None:
    files = _files(*CONTENT_TYPES)
    assert len(files) > 20
    for path in files:
        url = "/app/" + path.relative_to(SPA_DIR).as_posix()
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.headers["content-type"].startswith(CONTENT_TYPES[path.suffix]), (url, r.headers["content-type"])
        assert r.headers["cache-control"] == "no-cache"


def test_index_references_existing_files(client: TestClient) -> None:
    html = (SPA_DIR / "index.html").read_text(encoding="utf-8")
    refs = set(re.findall(r'(?:href|src)="(/app/[^"?#]+)"', html))
    imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"]
    refs |= set(imports.values())
    assert {"/app/js/main.js", "/app/vendor/preact.module.js", "/app/manifest.webmanifest"} <= refs
    for ref in refs:
        assert client.get(ref).status_code == 200, ref


def test_manifest_icons_exist(client: TestClient) -> None:
    manifest = json.loads((SPA_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "/" and manifest["display"] == "standalone"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


IMPORT_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["']([^"']+)["']""")


def test_js_imports_resolve() -> None:
    """Every relative import points at an existing file; bare imports are in the import map."""
    html = (SPA_DIR / "index.html").read_text(encoding="utf-8")
    mapped = set(json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"])
    for path in _files(".js"):
        text = path.read_text(encoding="utf-8")
        for spec in IMPORT_RE.findall(text):
            if spec.startswith("."):
                assert (path.parent / spec).resolve().is_file(), f"{path.relative_to(SPA_DIR)} imports missing {spec}"
            elif spec.startswith("/app/"):
                assert (SPA_DIR / spec[len("/app/"):]).is_file(), spec
            else:
                assert spec in mapped, f"{path.relative_to(SPA_DIR)}: bare import {spec!r} is not in the import map"


# ----------------------------------------------------------------------------- vendoring
@pytest.mark.parametrize("name", [
    "vendor/preact.module.js",
    "vendor/preact-hooks.module.js",
    "vendor/htm.module.js",
    "vendor/uqr.module.js",
    "vendor/LICENSE-preact.txt",
    "vendor/LICENSE-htm.txt",
    "vendor/LICENSE-uqr.txt",
    "vendor/LICENSE-lucide.txt",
    "fonts/LICENSE-inter.txt",
    "fonts/inter-latin.woff2",
    "fonts/inter-cyrillic.woff2",
])
def test_vendored_files_present(name: str) -> None:
    assert (SPA_DIR / name).is_file(), name


def test_licences_mention_their_projects() -> None:
    assert "MIT" in (SPA_DIR / "vendor/LICENSE-preact.txt").read_text(encoding="utf-8")
    assert "Apache" in (SPA_DIR / "vendor/LICENSE-htm.txt").read_text(encoding="utf-8")
    assert "ISC" in (SPA_DIR / "vendor/LICENSE-lucide.txt").read_text(encoding="utf-8")
    assert "Open Font License" in (SPA_DIR / "fonts/LICENSE-inter.txt").read_text(encoding="utf-8")


def test_no_external_urls_in_markup_and_styles() -> None:
    for path in _files(".html", ".css", ".webmanifest"):
        text = path.read_text(encoding="utf-8")
        for host in URL_RE.findall(text):
            assert host.lower() == "www.w3.org", f"{path.relative_to(SPA_DIR)} references {host}"
    for path in _files(".css"):
        text = path.read_text(encoding="utf-8")
        assert "@import" not in text or "http" not in text, path
        for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", text):
            assert not re.match(r"(https?:)?//", url), f"{path.name}: {url}"


def test_js_loads_nothing_from_the_internet() -> None:
    for path in _files(".js"):
        text = path.read_text(encoding="utf-8")
        for spec in IMPORT_RE.findall(text):
            assert not re.match(r"(https?:)?//", spec), f"{path.relative_to(SPA_DIR)} imports {spec}"
        for host in URL_RE.findall(text):
            assert host.lower() in ALLOWED_LINK_HOSTS, f"{path.relative_to(SPA_DIR)} mentions {host}"


def test_js_stays_small() -> None:
    vendor = sum(p.stat().st_size for p in (SPA_DIR / "vendor").glob("*.js"))
    assert vendor < 80_000  # Preact + hooks + htm + QR
    core = sum(p.stat().st_size for d in ("lib", "ui", "shell") for p in (SPA_DIR / "js" / d).glob("*.js"))
    assert core < 250_000


# ----------------------------------------------------------------------------- network mode
def test_network_mode_assets_public_pages_need_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "spa-token-for-tests-123456"
    app = create_app(AppConfig(), Database(), bind_host="0.0.0.0", access_token=token)
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/app/js/main.js").status_code == 200  # no secrets in the bundle
        assert c.get("/app/manifest.webmanifest").status_code == 200  # phones fetch it without cookies
        assert c.get("/").status_code == 401
        assert c.get("/deal/1").status_code == 401
        r = c.get(f"/deal/1?token={token}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/deal/1"
        assert c.get("/deal/1").status_code == 200  # cookie remembered
