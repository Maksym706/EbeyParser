"""AI scout in the web API: /ai/scout/test on a mocked always-on server (text only, the catalog
suggests the model, save switches the scout on), the scout block in /monitor and /health, and
the scout / super-deal / daily-top settings (ranges, secret key, URL check). No network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api_v1_helpers import LOCAL, clean_environ, make_app, restore_environ
from ebeyparser.config import load_config

SCOUT_URL = "http://nas:8080/v1"
ITEMS = [
    {"i": 0, "k": "pc", "p": "", "n": 1, "c": ["RTX 3070", "Intel Core i5-9600K"], "q": "gaming pc rtx 3070",
     "z": "used", "h": ["pc_parts"], "x": [], "s": 9, "r": "старый ПК, внутри RTX 3070"},
    {"i": 1, "k": "single", "p": "Apple iPhone 13 128GB", "n": 1, "c": [], "q": "iphone 13 128gb", "z": "used",
     "h": ["typo"], "x": [], "s": 6, "r": "iPhone 13 с опечаткой"},
    {"i": 2, "k": "wanted", "p": "Sony DualSense Controller", "n": 2, "c": [], "q": "dualsense", "z": "unknown",
     "h": [], "x": [], "s": 0, "r": "ищет, а не продаёт"},
    {"i": 3, "k": "other", "p": "", "n": 1, "c": [], "q": "", "z": "used", "h": [], "x": [], "s": 2, "r": "не техника"},
]


@pytest.fixture(autouse=True)
def isolated_env():
    saved = clean_environ()
    yield
    restore_environ(saved)


class ScoutServer:
    def __init__(self) -> None:
        self.up = True
        self.bodies: list[dict] = []
        self.models = ["qwen3.5-2b-instruct", "qwen/qwen2.5-vl-7b"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not str(request.url).startswith(SCOUT_URL.removesuffix("/v1")):
            return httpx.Response(404, json={"error": "unexpected"})
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        body = json.loads(request.content)
        self.bodies.append(body)
        content = body["messages"][1]["content"]
        assert isinstance(content, str)  # text only
        n = sum(1 for line in content.splitlines() if line.startswith("["))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"items": ITEMS[:n]})}}]})


@pytest.fixture()
def client(tmp_path: Path):
    app, config, db, path, monitor = make_app(tmp_path)
    server = ScoutServer()
    app.state.api.http_transport = httpx.MockTransport(server)
    with TestClient(app, base_url=LOCAL) as c:
        yield c, path, server


def test_scout_test_suggests_a_model_reads_the_samples_and_saves(client) -> None:
    c, path, server = client
    data = c.post("/api/v1/ai/scout/test", json={"provider": "openai", "base_url": SCOUT_URL, "save": True}).json()
    assert data["ok"] and data["answered"] == 4 and data["hidden_gpu"] and data["typo_fixed"] and data["wanted_seen"]
    assert data["suggested_model"] == "qwen3.5-2b-instruct" and data["model"] == "qwen3.5-2b-instruct"
    assert data["sec_per_ad"] is not None and "Разведчик работает" in data["message_ru"]
    assert server.bodies[0]["model"] == "qwen3.5-2b-instruct" and "items" in json.dumps(server.bodies[0])
    saved = load_config(path).ai.scout
    assert data["saved"] and saved.enabled and saved.base_url == SCOUT_URL and saved.model == "qwen3.5-2b-instruct"
    scout = c.get("/api/v1/monitor").json()["scout"]
    assert scout["enabled"] and scout["own_endpoint"] and scout["state"] in ("idle", "ok") and scout["text_ru"]


def test_scout_test_refuses_a_model_under_2b(client) -> None:
    c, path, server = client
    data = c.post("/api/v1/ai/scout/test", json={"provider": "openai", "base_url": SCOUT_URL, "model": "qwen3.5-0.8b",
                                                  "save": True}).json()
    assert not data["ok"] and data["too_small"] and not data["saved"] and data["answered"] == 0
    assert "слишком маленькая" in data["message_ru"] and data["error_ru"] == data["message_ru"]
    assert data["suggested_model"] == "qwen3.5-2b-instruct" and "qwen3.5-2b-instruct" in data["message_ru"]
    assert server.bodies == []  # no ad was sent to the tiny model
    assert load_config(path).ai.scout.enabled is False
    server.models = ["qwen3.5-0.8b"]  # only the tiny one: the catalog's pick to download, in plain words
    data = c.post("/api/v1/ai/scout/test", json={"provider": "openai", "base_url": SCOUT_URL,
                                                  "model": "qwen3.5-0.8b"}).json()
    assert data["too_small"] and data["suggested_model"] is None and "qwen/qwen3.5-2b" in data["message_ru"]
    assert data["recommended"]["ollama"] == "qwen3.5:2b-q4_K_M" and data["recommended"]["lmstudio"] == "qwen/qwen3.5-2b"


def test_scout_test_when_the_server_is_off(client) -> None:
    c, path, server = client
    server.up = False
    data = c.post("/api/v1/ai/scout/test", json={"provider": "openai", "base_url": SCOUT_URL,
                                                  "model": "qwen3.5-2b", "save": True}).json()
    assert not data["ok"] and not data["saved"] and data["error_ru"] and "ConnectError" not in data["error_ru"]
    assert load_config(path).ai.scout.enabled is False


def test_scout_in_health_and_monitor(client) -> None:
    c, _, _ = client
    scout = c.get("/api/v1/health", params={"ai": False}).json()["scout"]
    assert scout["enabled"] is False and scout["state"] == "off" and "выключен" in scout["text_ru"]
    assert c.get("/api/v1/monitor").json()["scout"]["vision_queue"]["waiting"] == 0


def test_scout_settings(client) -> None:
    c, path, _ = client
    r = c.patch("/api/v1/settings", json={"ai": {"scout": {"enabled": True, "mode": "candidates", "batch_size": 6},
                                                  "vision_wait_minutes": 30},
                                           "notifications": {"super_deals": {"min_profit": 150},
                                                             "daily_top": {"enabled": True, "hour": 21}}})
    assert r.status_code == 200, r.text
    cfg = load_config(path)
    assert cfg.ai.scout.mode == "candidates" and cfg.ai.scout.batch_size == 6 and cfg.ai.vision_wait_minutes == 30
    assert cfg.notifications.super_deals.min_profit == 150 and cfg.notifications.daily_top.hour == 21
    view = c.get("/api/v1/settings").json()
    assert view["ai"]["scout"]["mode"] == "candidates" and "api_key" not in view["ai"]["scout"]
    bad = c.patch("/api/v1/settings", json={"ai": {"scout": {"base_url": "nas:8080", "batch_size": 99}}})
    fields = bad.json()["error"]["fields"]
    assert bad.status_code == 422 and "ai.scout.base_url" in fields and "ai.scout.batch_size" in fields
    secret = c.patch("/api/v1/settings", json={"ai": {"scout": {"api_key": "sk-x"}}})
    assert secret.status_code == 422 and "ai.scout.api_key" in secret.json()["error"]["fields"]


HOST = {"cores": 4, "ram_gb": 7.8, "arch": "x86_64", "arm": False, "os": "Linux", "gpu": None}


def test_ai_recommend_for_this_machine_and_for_a_gaming_pc(tmp_path: Path) -> None:
    def probe(url: str):
        return {"data": [{"id": "qwen/qwen3.5-2b"}, {"id": "qwen/qwen3.5-0.8b"}]} if "1234" in url else None

    app, *_ = make_app(tmp_path, ai_probe=probe)
    app.state.api.host_probe = lambda: dict(HOST)
    with TestClient(app, base_url=LOCAL) as c:
        data = c.get("/api/v1/ai/recommend").json()
        assert data["host"]["ram_gb"] == 7.8 and data["host"]["tier"] == "T0" and data["input"]["from_host"]
        assert data["tier"] == "T0" and data["role"] == "server" and data["tier_label_ru"]
        triage = data["picks"]["triage"]
        assert triage["key"] == "qwen3.5-2b" and triage["ids"] == {
            "ollama": "qwen3.5:2b-q4_K_M", "lmstudio": "qwen/qwen3.5-2b", "llamacpp": "unsloth/Qwen3.5-2B-GGUF:Q4_K_M"}
        assert triage["sec_per_ad"] == 6.0 and triage["ads_per_hour"] == 600
        assert triage["speed_ru"] == "≈ 6 с на объявление, ~600 объявлений в час"
        assert triage["label_ru"] and triage["desc_ru"] and triage["ram_gb"] == 2.0 and triage["quant"] == "Q4_K_M"
        assert "Discover" in triage["get_ru"]["lmstudio"] and "qwen3.5:2b-q4_K_M" in triage["get_ru"]["ollama"]
        assert "ollama pull" not in json.dumps(data, ensure_ascii=False)  # no shell commands in the UI
        assert data["picks"]["embeddings"]["key"] == "qwen3-embedding-0.6b" and data["picks"]["second_opinion"] is None
        assert data["picks"]["vision"]["name"] == "Qwen3.5 2B" and data["picks"]["vision"]["speed_ru"]
        match = data["installed_match"]
        assert match["triage"] == {"server": "LM Studio", "provider": "openai", "base_url": "http://localhost:1234/v1",
                                   "model": "qwen/qwen3.5-2b"}
        assert match["servers"][0]["triage"] == "qwen/qwen3.5-2b"  # the 0.8B is never a triage match
        assert set(data["setup_ru"]) == {"lmstudio", "ollama", "llamacpp"} and data["summary_ru"].startswith("Qwen3.5 2B")

        pc = c.get("/api/v1/ai/recommend", params={"ram_gb": 32, "vram_gb": 12, "role": "pc", "detect": False}).json()
        assert pc["tier"] == "T2" and pc["picks"]["triage"] is None and pc["picks"]["embeddings"] is None
        assert pc["picks"]["vision"]["key"] == "qwen3.5-9b-vision" and pc["picks"]["vision"]["speed_ru"]
        assert pc["picks"]["second_opinion"]["key"] == "qwen3.5-9b-think" and pc["installed_match"] is None
        assert not pc["input"]["from_host"] and "сервер" in pc["notes_ru"][0]
        assert c.get("/api/v1/ai/recommend", params={"ram_gb": 2, "detect": False}).json()["picks"]["triage"] is None
        assert c.get("/api/v1/ai/recommend", params={"role": "nas"}).status_code == 422
