"""SPA free cloud AI (docs/design/CLOUD_AI.md): «Где работает нейросеть» in Settings → Нейросеть and
the onboarding AI step, the cloud flow (provider → «Получить ключ» → key → «Проверить» → models →
limits → «Сохранить»), the «Облако» tile on Состояние, the stylesheet (tokens only), the API
routes the screens call, the key links, and `node --check` when Node is around."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ebeyparser.config import AppConfig
from ebeyparser.db import Database
from ebeyparser.web.app import create_app
from ebeyparser.web.spa import SPA_DIR

LOCAL = "http://127.0.0.1:8000"
JS = SPA_DIR / "js"
CLOUD = JS / "setup" / "cloud.js"
CSS = SPA_DIR / "css" / "cloud.css"
CHANGED = [CLOUD, JS / "setup" / "ai.js", JS / "screens" / "settings" / "sections-a.js", JS / "screens" / "health.js",
           JS / "screens" / "onboarding" / "steps-connect.js", JS / "screens" / "onboarding" / "index.js",
           JS / "features" / "scout.js", JS / "lib" / "links.js"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture
def client() -> TestClient:
    app = create_app(AppConfig(), Database())
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def test_where_choice_in_settings_and_onboarding() -> None:
    cloud = _read(CLOUD)
    for label in ("Бесплатное облако", "На моём компьютере", "Облако + компьютер про запас"):
        assert f'label: "{label}"' in cloud
    assert re.search(r'value: "cloud".*value: "local".*value: "hybrid"', cloud, re.S)
    settings = _read(JS / "screens" / "settings" / "sections-a.js")
    assert "<${WherePicker}" in settings and "<${CloudConnect}" in settings and 'title="Где работает нейросеть"' in settings
    assert 'id="cloud"' in settings  # /settings/ai#cloud (Состояние, /health actions)
    assert 'api.put("/ai/cloud", { mode: "local" })' in settings  # «Перейти на компьютер»
    onb = _read(JS / "screens" / "onboarding" / "steps-connect.js")
    assert "<${WherePicker}" in onb and "<${CloudConnect}" in onb and "Где работает нейросеть" in onb
    # the onboarding's «Дальше» only auto-accepts a found LOCAL model
    assert 'aiWhere === "local"' in _read(JS / "screens" / "onboarding" / "index.js")


def test_cloud_flow_calls_the_api_and_links_the_key_pages() -> None:
    cloud = _read(CLOUD)
    assert 'api.get("/ai/cloud")' in cloud and 'api.post("/ai/cloud/test"' in cloud and 'api.put("/ai/cloud"' in cloud
    assert "save_key: true" in cloud and "photo: true" in cloud
    for text in ("Получить ключ", "Проверить", "Сохранить", "Читает объявления", "Проверяет фото",
                 "Отправлять объявления с eBay в облако", "Компьютер про запас", "Купи $10 кредитов один раз"):
        assert text in cloud, text
    assert "<${SecretInput}" in cloud  # the key is masked while typed
    links = _read(JS / "lib" / "links.js")
    for name in ("openRouterKeys", "nvidiaKeys", "omniRoute", "openRouterCredits", "openRouterPrivacy"):
        assert f"{name}:" in links and f"LINKS.{name}" in cloud
    assert "https://" not in cloud  # every outside page through LINKS
    # the hybrid's local model is saved as the stand-in, not over the cloud
    ai = _read(JS / "setup" / "ai.js")
    assert 'saveAs = "main"' in ai and "save_as: saveAs" in ai
    assert 'saveAs="fallback"' in cloud


def test_api_routes_the_screens_call_exist(client: TestClient) -> None:
    test = client.post("/api/v1/ai/cloud/test", json={"provider": "nvidia"})
    assert test.status_code == 422 and "api_key" in test.json()["error"]["fields"]  # routed, validated
    assert client.put("/api/v1/ai/cloud", json={"mode": "local"}).status_code not in (404, 405)
    data = client.get("/api/v1/ai/cloud").json()
    assert data["mode"] == "local" and {p["key"] for p in data["presets"]} >= {"openrouter", "nvidia", "omniroute"}
    assert set(data["keys"]) >= {"openrouter", "nvidia", "omniroute"} and not data["keys"]["openrouter"]["set"]
    assert data["privacy_ru"] and "eBay" in data["ebay_ru"]
    assert client.get("/api/v1/monitor").json()["cloud"]["enabled"] is False


def test_health_cloud_tile_and_scout_quota_state() -> None:
    health = _read(JS / "screens" / "health.js")
    assert "function CloudTile" in health and "d.cloud && d.cloud.enabled && html`<${CloudTile}" in health
    for key in ("used_today", "daily_limit", "count_429_today", "fallback_active", "fallback_configured", "next_reset"):
        assert key in health, key
    assert 'href="/settings/ai#cloud"' in health and "cloud_name" in health
    scout = _read(JS / "features" / "scout.js")
    assert re.search(r'quota: \{ tone: "haggle", label: "[^"]+" \}', scout)


def test_stylesheet_linked_served_and_tokens_only(client: TestClient) -> None:
    index = _read(SPA_DIR / "index.html")
    assert '<link rel="stylesheet" href="/app/css/cloud.css" />' in index
    assert index.index("scout.css") < index.index("cloud.css")
    r = client.get("/app/css/cloud.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    css = _read(CSS)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css)  # colours only from tokens.css
    for cls in (".where-cards", ".cloud-flow", ".cloud-key", ".cloud-models", ".cloud-limits", ".cloud-privacy",
                ".cloud-usage"):
        assert cls in css and cls.lstrip(".") in _read(CLOUD) + _read(JS / "screens" / "onboarding" / "steps-connect.js")


def test_copy_is_russian_without_tech_words() -> None:
    strings = " ".join(re.findall(r'"[^"\n]*"|`[^`]*`', re.sub(r"^\s*(//|\*|/\*\*).*$", "", _read(CLOUD), flags=re.M)))
    for bad in ("config.yaml", ".env", "api_key", "python -m", "localhost", "Error", "undefined"):
        assert bad not in strings, bad


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_changed_modules_parse() -> None:
    for path in CHANGED:
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)
