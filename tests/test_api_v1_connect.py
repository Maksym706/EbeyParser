"""/api/v1 connection checks: local AI, guided Telegram linking, e-mail, eBay keys, test
notifications. Every external service is an httpx.MockTransport / fake SMTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api_v1_helpers import BOT_TOKEN, LOCAL, clean_environ, events_of, make_app, restore_environ
from ebeyparser.config import load_config


@pytest.fixture(autouse=True)
def isolated_env():
    saved = clean_environ()
    yield
    restore_environ(saved)


class Services:
    """Fake Telegram Bot API, LM Studio and eBay behind one MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.updates: list[dict[str, Any]] = []
        self.webhook = False
        self.lmstudio_up = True
        self.models = ["qwen/qwen2.5-vl-7b", "text-embedding-nomic"]
        self.answer: dict[str, Any] = {
            "product": "Apple iPhone 13 128GB", "search_query": "iphone 13 128gb", "photo_matches_description": True,
            "condition": "like_new", "red_flags": [], "estimated_market_price": 420, "verdict": "buy",
            "confidence": 0.8, "reasoning": "На фото iPhone 13 без повреждений.", "item_type": "single",
        }
        self.ebay_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://api.telegram.org/bot"):
            token, method = request.url.path.removeprefix("/bot").split("/", 1)
            if token != BOT_TOKEN:
                return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"id": 42, "is_bot": True, "first_name": "Deals",
                                                                        "username": "maks_deals_bot"}})
            if method == "getUpdates":
                if self.webhook:
                    return httpx.Response(409, json={"ok": False, "error_code": 409,
                                                     "description": "Conflict: can't use getUpdates while webhook is active"})
                return httpx.Response(200, json={"ok": True, "result": self.updates})
            if method == "deleteWebhook":
                self.webhook = False
                return httpx.Response(200, json={"ok": True, "result": True})
            if method in ("sendMessage", "sendPhoto"):
                body = json.loads(request.content)
                if body.get("chat_id") == "404":
                    return httpx.Response(400, json={"ok": False, "error_code": 400,
                                                     "description": "Bad Request: chat not found"})
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if url.startswith("http://localhost:1234"):
            if not self.lmstudio_up:
                raise httpx.ConnectError("connection refused", request=request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
            if request.url.path == "/v1/chat/completions":
                body = json.loads(request.content)
                content = body["messages"][1]["content"]
                assert isinstance(content, list) and content[1]["type"] == "image_url"  # the sample photo is sent
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.answer)}}]})
        if "api.ebay.com" in url:
            if request.url.path.endswith("/oauth2/token"):
                if not self.ebay_ok:
                    return httpx.Response(401, json={"error": "invalid_client"})
                return httpx.Response(200, json={"access_token": "v^1.1#app", "expires_in": 7200})
            if request.url.path.endswith("/rate_limit/"):
                return httpx.Response(200, json={"rateLimits": [{
                    "apiContext": "buy", "apiName": "Browse", "apiVersion": "v1",
                    "resources": [{"name": "buy.browse", "rates": [{
                        "count": 12, "limit": 5000, "remaining": 4988, "reset": "2026-09-30T07:00:00.000Z",
                        "timeWindow": 86400}]}]}]})
        return httpx.Response(404, json={"error": f"unexpected {request.method} {url}"})


class FakeSMTP:
    sent: list[Any] = []
    fail: Exception | None = None

    def __init__(self, host: str, port: int, *, use_ssl: bool, timeout: float) -> None:
        self.host, self.port, self.use_ssl = host, port, use_ssl

    def ehlo(self) -> None: ...

    def starttls(self, context: Any = None) -> None: ...

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail is not None:
            raise FakeSMTP.fail

    def send_message(self, msg: Any) -> None:
        FakeSMTP.sent.append(msg)

    def quit(self) -> None: ...


@pytest.fixture()
def conn(tmp_path: Path):
    services = Services()
    probe_calls: list[str] = []

    def probe(url: str) -> Any:
        probe_calls.append(url)
        if "1234" in url and services.lmstudio_up:
            return {"data": [{"id": m} for m in services.models]}
        return None

    app, config, db, path, monitor = make_app(tmp_path, ai_probe=probe)
    app.state.api.http_transport = httpx.MockTransport(services)
    FakeSMTP.sent, FakeSMTP.fail = [], None
    app.state.api.smtp_factory = FakeSMTP
    with TestClient(app, base_url=LOCAL) as client:
        yield client, app, config, path, services


# ------------------------------------------------------------------------ AI
def test_ai_detect(conn) -> None:
    c, app, config, path, services = conn
    data = c.post("/api/v1/ai/detect").json()
    assert data["variant"] == "found" and data["message_ru"].startswith("Нашёл LM Studio")
    server = data["servers"][0]
    assert server["name"] == "LM Studio" and server["default_model"] == "qwen/qwen2.5-vl-7b"
    assert server["models"] == [{"id": "qwen/qwen2.5-vl-7b", "vision": True, "recommended": True},
                                {"id": "text-embedding-nomic", "vision": False, "recommended": False}]
    assert data["suggested"] == {"provider": "openai", "base_url": "http://localhost:1234/v1",
                                 "model": "qwen/qwen2.5-vl-7b"}
    assert len(data["gpu_presets"]) == 4 and "installed" in data["pillow"]
    assert c.get("/api/v1/ai/detect").json()["variant"] == "found"
    services.models = ["llama-3-8b"]
    assert c.post("/api/v1/ai/detect").json()["variant"] == "no_vision"
    services.lmstudio_up = False
    none = c.post("/api/v1/ai/detect").json()
    assert none["variant"] == "none" and none["servers"] == [] and none["suggested"]["model"]


def test_ai_test_on_sample_ad_and_save(conn) -> None:
    c, app, config, path, services = conn
    r = c.post("/api/v1/ai/test", json={"provider": "openai", "base_url": "http://localhost:1234/v1",
                                        "model": "qwen2.5-vl-7b-instruct", "save": True})
    data = r.json()
    assert r.status_code == 200 and data["ok"], data
    assert data["server_ok"] and data["model_available"] and data["resolved_model"] == "qwen/qwen2.5-vl-7b"
    assert data["verdict"]["product"] == "Apple iPhone 13 128GB" and data["verdict"]["condition_label"] == "Как новое"
    assert data["vision_ok"] and data["seconds"] is not None and "Модель увидела: Apple iPhone 13" in data["message_ru"]
    assert data["saved"] and config.ai.enabled and config.ai.model == "qwen/qwen2.5-vl-7b"
    assert load_config(path).ai.enabled is True
    assert any(s["key"] == "ai" and s["done"] for s in c.get("/api/v1/onboarding").json()["steps"])

    services.answer = {"product": "Kaffeemaschine", "condition": "good", "verdict": "maybe", "confidence": 0.4}
    odd = c.post("/api/v1/ai/test", json={}).json()
    assert odd["ok"] and odd["vision_ok"] is False and "странно" in odd["warning_ru"]
    quick = c.post("/api/v1/ai/test", json={"sample": False}).json()
    assert quick["ok"] and quick["verdict"] is None and "найдена" in quick["message_ru"]
    missing = c.post("/api/v1/ai/test", json={"model": "llava-13b"}).json()
    assert not missing["ok"] and missing["server_ok"] and not missing["model_available"] and missing["error_ru"]
    services.lmstudio_up = False
    down = c.post("/api/v1/ai/test", json={"save": True}).json()
    assert not down["ok"] and not down["server_ok"] and not down["saved"]
    assert down["message_ru"] == "LM Studio не отвечает. Открой LM Studio → Developer → Start Server"
    assert "ConnectError" not in down["message_ru"] and "http://" not in down["message_ru"]
    assert c.post("/api/v1/ai/test", json={"provider": "gpt"}).status_code == 422


def test_sample_image_is_an_image() -> None:
    from ebeyparser.web.api.routes_connect import _png, sample_image, sample_listing

    data = sample_image()
    assert data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
    assert _png(4, 4, (1, 2, 3)).startswith(b"\x89PNG") and "iPhone 13" in sample_listing().title


# ------------------------------------------------------------------ Telegram
def test_telegram_guided_linking(conn) -> None:
    c, app, config, path, services = conn
    bad = c.post("/api/v1/telegram/validate", json={"token": "123:short"})
    assert bad.status_code == 422 and "скопировалось не всё" in bad.json()["error"]["fields"]["token"]
    wrong = c.post("/api/v1/telegram/validate", json={"token": "987654321:AAHwrongTokenWrongTokenWrong_1"})
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "telegram_token_invalid"
    assert "987654321:AAH" not in wrong.text
    ok = c.post("/api/v1/telegram/validate", json={"token": BOT_TOKEN}).json()
    assert ok["bot"] == {"id": 42, "username": "maks_deals_bot", "name": "Deals", "link": "https://t.me/maks_deals_bot"}
    assert ok["message_ru"] == "✓ Бот @maks_deals_bot найден" and not ok["saved"]
    assert c.post("/api/v1/telegram/verify-token", json={"token": BOT_TOKEN}).status_code == 200

    link = c.post("/api/v1/telegram/link", json={"token": BOT_TOKEN}).json()
    code = link["code"]
    assert len(code) == 16 and link["deep_link"] == f"https://t.me/maks_deals_bot?start={code}"
    assert link["app_link"].startswith("tg://resolve?domain=maks_deals_bot")
    assert BOT_TOKEN in (path.parent / ".env").read_text(encoding="utf-8")  # stored once validated
    waiting = c.get("/api/v1/telegram/detect", params={"code": code}).json()
    assert waiting == {"found": False, "saved": False, "chat": None, "message_ru": "Жду твоё сообщение…"}
    services.webhook = True
    blocked = c.get("/api/v1/telegram/detect", params={"code": code})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "telegram_webhook"
    assert c.post("/api/v1/telegram/reset-webhook", json={}).json()["ok"] and not services.webhook
    services.updates = [
        {"update_id": 1, "message": {"text": "/start", "chat": {"id": 111, "type": "private", "first_name": "Other"}}},
        {"update_id": 2, "message": {"text": f"/start {code}",
                                     "chat": {"id": 555666777, "type": "private", "first_name": "Maksim"}}},
    ]
    found = c.get("/api/v1/telegram/detect", params={"code": code}).json()
    assert found["found"] and found["saved"] and found["chat"]["name"] == "Maksim"
    assert found["chat"]["chat_id_masked"] == "…6777" and "555666777" not in json.dumps(found)
    assert found["message_ru"] == "✓ Нашёл тебя: Maksim (личный чат)"
    assert config.notifications.telegram.chat_id == "555666777" and config.notifications.telegram.enabled
    saved = load_config(path)
    assert saved.notifications.telegram.enabled and "TELEGRAM_CHAT_ID=555666777" in (path.parent / ".env").read_text()
    assert "555666777" not in path.read_text(encoding="utf-8")
    assert c.get("/api/v1/telegram/detect", params={"code": code}).status_code == 404  # used up
    steps = {s["key"]: s["done"] for s in c.get("/api/v1/onboarding").json()["steps"]}
    assert steps["telegram"]

    chats = c.post("/api/v1/telegram/find-chat", json={}).json()
    assert [ch["chat_id"] for ch in chats["chats"]] == ["555666777", "111"]  # newest first

    test = c.post("/api/v1/telegram/test", json={}).json()
    assert test == {"ok": True, "message_ru": "Отправлено — проверь Telegram"}
    sent = [r for r in services.requests if r.url.path.endswith(("/sendPhoto", "/sendMessage"))]
    assert sent and json.loads(sent[-1].content)["chat_id"] == "555666777"
    lost = c.post("/api/v1/telegram/test", json={"chat_id": "404", "with_deal": False})
    assert lost.status_code == 502 and lost.json()["error"]["code"] == "delivery_failed"
    assert "нажми Start" in lost.json()["error"]["message_ru"] and "chat_id" not in lost.json()["error"]["message_ru"]
    assert lost.json()["error"]["details"]  # the technical text for «Подробнее»


def test_telegram_needs_token_and_chat(conn) -> None:
    c = conn[0]
    assert c.post("/api/v1/telegram/validate", json={}).json()["error"]["fields"]["token"]
    assert c.post("/api/v1/telegram/link").status_code == 422
    r = c.post("/api/v1/telegram/test", json={"token": BOT_TOKEN})
    assert r.status_code == 422 and "chat_id" in r.json()["error"]["fields"]
    assert c.get("/api/v1/telegram/detect", params={"code": "nothing-here"}).status_code == 404
    saved = c.post("/api/v1/telegram/validate", json={"token": BOT_TOKEN, "save": True}).json()
    assert saved["saved"] and c.get("/api/v1/settings").json()["secrets"]["telegram_bot_token"]["set"]


# -------------------------------------------------------------------- e-mail
def test_email_test_and_save(conn) -> None:
    c, app, config, path, services = conn
    missing = c.post("/api/v1/email/test", json={})
    assert missing.status_code == 422 and {"username", "password", "to_addrs"} <= set(missing.json()["error"]["fields"])
    body = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "username": "maksem706@gmail.com",
            "password": "abcd efgh ijkl mnop", "to_addrs": "maksem706@gmail.com", "save": True}
    r = c.post("/api/v1/email/test", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "saved": True, "message_ru": "Письмо отправлено на ma***@gmail.com"}
    assert FakeSMTP.sent and FakeSMTP.sent[-1]["To"] == "maksem706@gmail.com"
    env = (path.parent / ".env").read_text(encoding="utf-8")
    assert "SMTP_PASSWORD=abcd efgh ijkl mnop" in env and "SMTP_USER=maksem706@gmail.com" in env
    assert config.notifications.email.enabled and config.notifications.email.password == "abcd efgh ijkl mnop"
    assert "abcd efgh" not in path.read_text(encoding="utf-8")
    import smtplib

    FakeSMTP.fail = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    failed = c.post("/api/v1/email/validate", json={})
    assert failed.status_code == 502 and "пароль приложения" in failed.json()["error"]["message_ru"]
    assert "535" in failed.json()["error"]["details"] and "535" not in failed.json()["error"]["message_ru"]


# ---------------------------------------------------------------------- eBay
def test_ebay_test_keys_and_limits(conn) -> None:
    c, app, config, path, services = conn
    assert c.post("/api/v1/ebay/test", json={}).status_code == 422
    r = c.post("/api/v1/ebay/test", json={"client_id": "MyApp-PRD-123", "client_secret": "PRD-secret-456",
                                          "save": True})
    data = r.json()
    assert r.status_code == 200 and data["ok"] and data["token_ok"], data
    assert data["message_ru"] == "✓ Ключи работают · лимит 5 000 запросов в день"
    assert data["limits"][0]["remaining"] == 4988 and data["limits"][0]["reset"].startswith("2026-09-30")
    assert data["saved"] and config.ebay.configured and "EBAY_CLIENT_SECRET=PRD-secret-456" in (
        path.parent / ".env").read_text(encoding="utf-8")
    assert any(s["key"] == "ebay" and s["done"] for s in c.get("/api/v1/onboarding").json()["steps"])
    services.ebay_ok = False
    bad = c.post("/api/v1/ebay/validate", json={"client_id": "x", "client_secret": "y"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "ebay_keys_invalid"
    assert "Production" in bad.json()["error"]["message_ru"]


# ------------------------------------------------------------ notifications
def test_notify_test_per_channel_and_all(conn) -> None:
    c, app, config, path, services = conn
    r = c.post("/api/v1/notify/test", params={"channel": "telegram"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_configured"
    assert c.post("/api/v1/notify/test").json()["error"]["code"] == "no_channels"
    c.put("/api/v1/secrets", json={"telegram_bot_token": BOT_TOKEN, "telegram_chat_id": "555666777"})
    ok = c.post("/api/v1/notify/test", params={"channel": "telegram"}).json()
    assert ok == {"ok": True, "message_ru": "Отправлено: Telegram",
                  "results": {"telegram": {"ok": True, "label_ru": "Telegram", "message_ru": "Отправлено"}}}
    photo = [r for r in services.requests if r.url.path.endswith("/sendPhoto")]
    assert photo and "RTX 3080" in json.loads(photo[-1].content)["caption"]  # a sample deal with a photo
    assert c.post("/api/v1/notify/test", json={"channel": "email"}).json()["error"]["code"] == "not_configured"
    assert c.post("/api/v1/notify/test", params={"channel": "sms"}).status_code == 422
    c.patch("/api/v1/settings", json={"notifications": {"telegram": {"enabled": True}}})
    all_ok = c.post("/api/v1/notify/test").json()
    assert all_ok["ok"] and set(all_ok["results"]) == {"telegram"}
    config.notifications.telegram.chat_id = "404"
    broken = c.post("/api/v1/notify/test")
    assert broken.status_code == 200 and broken.json()["ok"] is False
    assert c.post("/api/v1/notify/test", params={"channel": "telegram"}).status_code == 502


def test_notify_test_uses_injected_factory(tmp_path: Path) -> None:
    sent: list[Any] = []

    class Fake:
        name = "fake"

        async def send(self, deals: list[Any], *, title: str | None = None) -> None:
            sent.append((deals, title))

    app, *_ = make_app(tmp_path, notifiers_factory=lambda: [Fake()])
    with TestClient(app, base_url=LOCAL) as c:
        assert c.post("/api/v1/notify/test").json() == {
            "ok": True, "message_ru": "Отправлено: fake",
            "results": {"fake": {"ok": True, "label_ru": "fake", "message_ru": "Отправлено"}}}
    assert sent and sent[0][1] == "EbeyParser: тестовое уведомление"
    assert not events_of(app, "health_alert")
