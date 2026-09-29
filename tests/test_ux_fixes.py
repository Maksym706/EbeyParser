"""The fixes from the UX walk-through (scratchpad UX_REPORT): derived deal actions, the site
pause (cooldown) state, honest health levels, local times, friendly errors, server-side
validation, wizard replace semantics, pasted search links, place labels, optional checks
that never pause the whole app. No network anywhere (MockTransport / fakes)."""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api_v1_helpers import BOT_TOKEN, EBAY, LOCAL, RTX, clean_environ, make_app, restore_environ, wait_job
from ebeyparser import timefmt
from ebeyparser.config import AppConfig, SearchConfig, load_config
from ebeyparser.db import Database
from ebeyparser.errors_ru import LMSTUDIO_DOWN, ai_problem, friendly_run_error, humanize, humanize_text, sanitize
from ebeyparser.models import Evaluation, Listing, RunSummary, utcnow
from ebeyparser.web.api.presenters import derive_action

FORBIDDEN = ("python -m", "config.yaml", ".env", "debug-search", "ebay.client_id", "ConnectError", "Errno",
             "Traceback", "Exception")


def clean_text(text: str) -> bool:
    return not any(word in text for word in FORBIDDEN)


@pytest.fixture(autouse=True)
def isolated_env():
    saved = clean_environ()
    timefmt.set_timezone("Europe/Berlin")
    yield
    timefmt.set_timezone("Europe/Berlin")
    restore_environ(saved)


@pytest.fixture()
def api(tmp_path: Path):
    app, config, db, path, monitor = make_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as client:
        yield client, app, config, db, path, monitor


def blocked_status(minutes: int = 90) -> dict[str, Any]:
    until = utcnow() + timedelta(minutes=minutes)
    return {"www.kleinanzeigen.de": {"requests_last_hour": 40, "limit_per_hour": 150, "images_last_hour": 0,
                                     "blocked": True, "cooldown_until": until, "strikes": 1,
                                     "last_block_reason": "HTTP 403", "last_block_at": utcnow()}}


# ------------------------------------------------------------ 1. deal action
def _listing(ad_id: str, **kw: Any) -> Listing:
    return Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-1-2", title="Sony WH-1000XM4",
                   price=kw.pop("price", 100.0), search_name="Test", **kw)


def test_derive_action_rules() -> None:
    auction = _listing("1", buying_options=["AUCTION"], source="ebay")
    vb = _listing("2", negotiable=True)
    plain = _listing("3")
    assert derive_action(auction, Evaluation(ad_id="1", verdict="buy", max_buy_price=200)) == "bid"
    assert derive_action(auction, Evaluation(ad_id="1", verdict="buy", action="buy")) == "bid"  # never "Покупай"
    assert derive_action(auction, Evaluation(ad_id="1", verdict="maybe")) == "watch"  # no max bid known
    assert derive_action(auction, Evaluation(ad_id="1", verdict="skip")) == "skip"
    assert derive_action(vb, Evaluation(ad_id="2", verdict="buy", offer_price=90)) == "haggle"
    assert derive_action(vb, Evaluation(ad_id="2", verdict="buy")) == "buy"
    assert derive_action(plain, Evaluation(ad_id="3", verdict="buy", offer_price=90)) == "buy"  # not negotiable
    assert derive_action(plain, Evaluation(ad_id="3", verdict="maybe")) == "watch"
    assert derive_action(plain, Evaluation(ad_id="3", verdict="skip")) == "skip"
    assert derive_action(plain, None) == "watch"
    assert derive_action(plain, Evaluation(ad_id="3", verdict="maybe", action="haggle")) == "haggle"  # stored wins


def test_empty_action_is_derived_in_cards_filter_and_facets(tmp_path: Path) -> None:
    app, config, db, *_ = make_app(tmp_path, seed=False)
    rows = {
        "a1": (_listing("a1", source="ebay", buying_options=["AUCTION"], bid_count=3,
                        ends_at=utcnow() + timedelta(hours=2)), {"verdict": "buy", "max_buy_price": 150.0}),
        "h1": (_listing("h1", negotiable=True), {"verdict": "buy", "offer_price": 80.0}),
        "b1": (_listing("b1"), {"verdict": "buy"}),
        "m1": (_listing("m1"), {"verdict": "maybe"}),
    }
    for listing, ev in rows.values():
        db.upsert_listing(listing)
        db.save_evaluation(Evaluation(ad_id=listing.ad_id, score=70.0, expected_profit=40.0, **ev))  # action ""
    with TestClient(app, base_url=LOCAL) as c:
        feed = c.get("/api/v1/deals", params={"facets": 1}).json()
        actions = {d["id"]: d["action"] for d in feed["items"]}
        assert actions == {"a1": "bid", "h1": "haggle", "b1": "buy", "m1": "watch"}
        assert all(d["action_label"] != "—" for d in feed["items"])
        haggle = next(d for d in feed["items"] if d["id"] == "h1")
        assert haggle["offer_price"] == 80 and haggle["profit_at_offer"] == 60  # 40 + (100 - 80)
        for action, ids in (("bid", ["a1"]), ("haggle", ["h1"]), ("buy", ["b1"]), ("watch", ["m1"])):
            got = [d["id"] for d in c.get("/api/v1/deals", params={"action": action}).json()["items"]]
            assert got == ids, action
            assert feed["facets"]["action"][action] == 1
        assert {d["id"] for d in c.get("/api/v1/deals", params={"actionable": 1}).json()["items"]} == {"a1", "h1", "b1"}


def test_demo_has_every_kind_of_deal(api) -> None:
    c, *_ = api
    items = c.get("/api/v1/deals", params={"verdict": "all", "status": "any", "limit": 50}).json()["items"]
    assert all(d["action"] for d in items)
    auction = next(d for d in items if d["id"] == EBAY)
    assert auction["action"] == "bid" and "AUCTION" in auction["buying_options"] and auction["max_buy_price"]
    assert auction["auction"]["bid_count"] and auction["auction"]["ends_at_label"]
    assert datetime.fromisoformat(auction["auction"]["ends_at"]) > utcnow()
    haggles = [d for d in items if d["action"] == "haggle"]
    assert any(d["negotiable"] and d["offer_price"] and d["profit_at_offer"] for d in haggles)
    assert any(d["action"] == "buy" and d["purpose"] == "resale" and not d["is_free"] for d in items)
    assert any(d["verdict"] == "maybe" and d["action"] == "watch" for d in items)
    assert any(d["purpose"] == "personal" for d in items) and any(d["is_free"] for d in items)
    assert c.get(f"/api/v1/deals/{EBAY}").json()["action"] == "bid"


# ---------------------------------------------------------------- 2. cooldown
def test_monitor_cooldown_state_app_and_health(api) -> None:
    c, app, config, db, path, monitor = api
    status = blocked_status(90)
    monitor.http_status = lambda: status
    until = status["www.kleinanzeigen.de"]["cooldown_until"]
    mon = c.get("/api/v1/monitor").json()
    assert mon["state"] == "cooldown"
    cooldown = mon["cooldown"]
    assert cooldown["until"] == until.isoformat() and cooldown["hosts"] == ["www.kleinanzeigen.de"]
    assert cooldown["until_label"] == timefmt.when_label(until) and timefmt.hhmm(until) in cooldown["until_label"]
    assert cooldown["text_ru"].startswith("Kleinanzeigen попросил паузу — продолжу ")
    assert mon["state_ru"] == cooldown["text_ru"] and "www." not in mon["state_ru"] and "T" not in mon["state_ru"][-6:]
    assert mon["http"][0]["site"] == "Kleinanzeigen" and mon["http"][0]["cooldown_until_label"]
    app_mon = c.get("/api/v1/app").json()["monitor"]
    assert app_mon["state"] == "cooldown" and app_mon["cooldown"]["until"] == cooldown["until"]
    health = c.get("/api/v1/health", params={"ai": 0}).json()
    assert health["level"] == "warn" and health["cooldown"]["until_label"] == cooldown["until_label"]
    texts = [p["text_ru"] for p in health["problems"]]
    assert any(t.startswith(cooldown["text_ru"]) for t in texts)
    assert all("+00:00" not in t and "www.kleinanzeigen.de" not in t for t in texts)
    pause = next(p for p in health["problems"] if p["text_ru"].startswith("Kleinanzeigen"))
    assert pause["action"] == {"label_ru": "Снизить нагрузку", "href": "/settings/region"}
    # learning note: the first alerts come after the pause, not "after the next check" at an earlier time
    config.searches = [SearchConfig(name="Neu", category_id=173, location="Berlin")]
    learning = c.get("/api/v1/monitor").json()["learning"]
    assert "после паузы" in learning["message_ru"] and learning["first_alerts_label"] == cooldown["until_label"]


def test_run_now_during_a_pause(api) -> None:
    c, app, config, db, path, monitor = api
    monitor.http_status = lambda: blocked_status(30)
    config.searches = [SearchConfig(name="KA", query="rtx 3080")]
    r = c.post("/api/v1/monitor/run")
    assert r.status_code == 409 and r.json()["error"]["code"] == "cooldown" and monitor.calls == 0
    assert "Kleinanzeigen попросил паузу" in r.json()["error"]["message_ru"]
    config.searches.append(SearchConfig(name="eBay", source="ebay", query="rtx 3080"))
    ok = c.post("/api/v1/monitor/run")
    assert ok.status_code == 202 and ok.json()["cooldown"] and "подождут" in ok.json()["message_ru"]


async def test_monitor_schedules_the_next_pass_after_the_pause(tmp_path: Path) -> None:
    from ebeyparser.monitor import Monitor

    class Client:
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

        def cooldown_remaining(self, url: str) -> float:
            return self.seconds

    config = AppConfig(searches=[SearchConfig(name="KA", query="x")])
    monitor = Monitor(config, Database())
    due = utcnow() + timedelta(minutes=10)
    assert monitor._after_cooldown(due) == due  # no client yet: nothing known
    monitor._client = Client(3600)  # type: ignore[assignment]
    later = monitor._after_cooldown(due)
    assert later > utcnow() + timedelta(minutes=59)
    config.searches.append(SearchConfig(name="eBay", source="ebay", query="x"))
    assert monitor._after_cooldown(due) == due  # eBay searches don't wait for Kleinanzeigen


# ------------------------------------------------------------- 3. health level
def test_health_is_never_ok_while_checks_are_stopped_or_paused(tmp_path: Path) -> None:
    app, config, db, path, monitor = make_app(tmp_path, seed=False)
    with TestClient(app, base_url=LOCAL) as c:
        nothing = c.get("/api/v1/health", params={"ai": 0}).json()
        assert nothing["level"] == "warn" and nothing["action"] == {"label_ru": "Поиски", "href": "/searches"}
        config.searches = [SearchConfig(name="KA", query="rtx 3080")]
        ok = c.get("/api/v1/health", params={"ai": 0}).json()
        assert ok["level"] == "ok" and ok["action"] is None  # the fake has a next run: automatic checks work
        monitor.next_run_at = None  # started with --no-monitor: no loop, no next run
        stopped = c.get("/api/v1/health", params={"ai": 0}).json()
        assert stopped["level"] == "warn" and stopped["monitor"]["state"] == "stopped"
        assert stopped["banner_ru"].startswith("Автопроверка выключена") and "Всё работает" not in stopped["banner_ru"]
        assert stopped["action"]["href"].startswith("/")
        monitor.next_run_at = utcnow() + timedelta(minutes=5)
        monitor.paused = True
        paused = c.get("/api/v1/health", params={"ai": 0}).json()
        assert paused["level"] == "warn" and "паузе" in paused["banner_ru"] and paused["action"]["label_ru"]
        monitor.paused = False
        config.notifications.telegram.enabled = True  # on, but no token / chat
        half = c.get("/api/v1/health", params={"ai": 0}).json()
        assert half["level"] == "warn" and "не настроен" in half["banner_ru"]
        assert half["action"] == {"label_ru": "Настроить уведомления", "href": "/settings/notifications"}


def test_health_without_monitor_and_run_now_503(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path, monitor=None, seed=False)
    with TestClient(app, base_url=LOCAL) as c:
        health = c.get("/api/v1/health", params={"ai": 0}).json()
        assert health["level"] == "warn" and "Фоновые проверки выключены" in health["banner_ru"]
        r = c.post("/api/v1/monitor/run")
        assert r.status_code == 503 and clean_text(r.text) and "Перезапусти" in r.json()["error"]["message_ru"]
        assert "`" not in r.json()["error"]["message_ru"]


def test_demo_runs_are_not_shown_as_real_checks(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path, seed=False)
    with TestClient(app, base_url=LOCAL) as c:
        assert c.post("/api/v1/demo").status_code == 200
        assert c.get("/api/v1/runs").json()["items"] == []
        health = c.get("/api/v1/health", params={"ai": 0}).json()
        assert health["runs"] == [] and health["last_error"] is None and "Всё работает" not in health["banner_ru"]


# ------------------------------------------------------------------- 4. times
def test_timefmt_labels_follow_the_configured_zone() -> None:
    when = datetime(2026, 9, 29, 19, 34, tzinfo=timezone.utc)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    assert timefmt.hhmm(when) == "21:34" and timefmt.when_label(when, now) == "21:34"
    assert timefmt.at_label(when, now) == "в 21:34" and timefmt.until_label(when, now) == "до 21:34"
    assert timefmt.when_label(when + timedelta(hours=5), now) == "завтра в 02:34"
    timefmt.set_timezone("America/New_York")
    assert timefmt.hhmm(when) == "15:34"
    timefmt.set_timezone("Mars/Olympus")  # unknown: Europe/Berlin
    assert timefmt.timezone_name() == "Europe/Berlin" and timefmt.hhmm(when) == "21:34"
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    record.created = when.timestamp()
    assert timefmt.LocalFormatter("%(asctime)s", "%H:%M").format(record) == "21:34"


def test_server_texts_use_the_user_zone_not_the_host() -> None:
    from ebeyparser.notify.render import _format_end_clock
    from ebeyparser.scraper.http import _local_time

    when = datetime(2026, 9, 29, 19, 34, tzinfo=timezone.utc)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    assert _format_end_clock(when, now) == "сегодня в 21:34"
    assert _local_time(when.timestamp(), now.timestamp()) == "21:34"
    from ebeyparser.monitor import _until_text
    from ebeyparser.scraper.http import BlockedError

    assert timefmt.hhmm(when) in _until_text(BlockedError("x", cooldown_until=when))


def test_timezone_setting(api) -> None:
    c, app, config, *_ = api
    bad = c.patch("/api/v1/settings", json={"general": {"timezone": "Europe/Nowhere"}})
    assert bad.status_code == 422 and "general.timezone" in bad.json()["error"]["fields"]
    ok = c.patch("/api/v1/settings", json={"general": {"timezone": "Europe/Moscow"}})
    assert ok.status_code == 200 and config.general.timezone == "Europe/Moscow"
    assert timefmt.timezone_name() == "Europe/Moscow" and c.get("/api/v1/app").json()["timezone"] == "Europe/Moscow"


# ------------------------------------------------------------------ 5. errors
def test_humanize_maps_the_usual_failures() -> None:
    request = httpx.Request("GET", "https://api.telegram.org/x")
    tg = humanize(httpx.ConnectError("[Errno -3] Temporary failure in name resolution", request=request), "telegram")
    assert tg.message_ru == "Нет связи с Telegram — проверь интернет и попробуй ещё раз" and "Errno" in tg.details
    mail = humanize(OSError(97, "Address family not supported by protocol"), "email", host="smtp.gmail.com")
    assert mail.message_ru.startswith("Не достучался до Gmail") and clean_text(mail.message_ru)
    auth = humanize(smtplib.SMTPAuthenticationError(535, b"not accepted"), "email", host="smtp.gmail.com")
    assert "пароль приложения" in auth.message_ru and "535" in auth.details
    tls = humanize(smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "email")
    assert "587" in tls.message_ru
    ai = humanize(httpx.ConnectError("refused", request=request), "ai", host="http://localhost:1234/v1")
    assert ai.message_ru == LMSTUDIO_DOWN and ai.action["href"] == "/settings/ai"
    proxy = humanize_text("eBay OAuth: 403 Host not in allowlist: api.ebay.com. Add this host to your network "
                          "egress settings", "ebay")
    assert proxy.message_ru.startswith("eBay недоступен из этой сети") and "allowlist" in proxy.details
    assert humanize(TimeoutError(), "ai").code == "timeout"
    assert ai_problem("openai", "http://localhost:1234/v1", "qwen", "Модель «qwen» не загружена на сервере",
                      server_ok=True).startswith("В LM Studio не загружена модель «qwen»")
    assert ai_problem("ollama", "http://localhost:11434", "q", "", server_ok=False).startswith("Ollama не отвечает")


def test_sanitize_drops_cli_and_config_mentions() -> None:
    for raw in ("страница не распознана. Запусти `python -m ebeyparser debug-search` и пришли вывод.",
                "Для поиска по eBay заполни ebay.client_id и ebay.client_secret в config.yaml/.env",
                "Нет активных поисков — добавь их на странице «Поиски» или в config.yaml",
                "Модель не ответила за 240 с — возьми модель поменьше или увеличь ai.timeout_seconds"):
        assert clean_text(sanitize(raw)), sanitize(raw)
    assert sanitize("Нет активных поисков — добавь их на странице «Поиски» или в config.yaml") == \
        "Нет активных поисков — добавь их на странице «Поиски»"
    assert friendly_run_error("GPU: ошибка сети — [Errno 111] Connection refused").startswith("GPU: Нет связи")


def test_old_technical_run_errors_are_shown_plain(api) -> None:
    c, app, config, db, *_ = api
    run = db.start_run()
    run.errors = ["GPU: страница поиска получена, но объявления на ней не распознаны. Запусти "
                  "`python -m ebeyparser debug-search` и пришли вывод.", "eBay: eBay OAuth: 403 Host not in allowlist"]
    db.finish_run(run)
    item = next(r for r in c.get("/api/v1/runs").json()["items"] if r["id"] == run.id)
    assert all(clean_text(e) for e in item["errors"]) and len(item["error_details"]) == 2
    assert item["started_at_label"] and "недоступен из этой сети" in item["errors"][1]


class TelegramDouble:
    """Telegram, a proxy that answers 403 with a plain page, or no network at all."""

    def __init__(self) -> None:
        self.mode = "ok"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if self.mode == "offline":
            raise httpx.ConnectError("[Errno 101] Network is unreachable", request=request)
        if self.mode == "proxy":
            return httpx.Response(403, text="Host not in allowlist: api.telegram.org")
        if self.mode == "blocked" and method == "sendMessage":
            return httpx.Response(403, json={"ok": False, "error_code": 403,
                                             "description": "Forbidden: bot was blocked by the user"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "b", "first_name": "B"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_telegram_403_kinds_are_told_apart(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path, seed=False)
    double = TelegramDouble()
    app.state.api.http_transport = httpx.MockTransport(double)
    with TestClient(app, base_url=LOCAL) as c:
        double.mode = "proxy"
        r = c.post("/api/v1/telegram/validate", json={"token": BOT_TOKEN})
        err = r.json()["error"]
        assert r.status_code == 502 and err["code"] == "network" and "недоступен из этой сети" in err["message_ru"]
        assert "Start" not in err["message_ru"] and BOT_TOKEN not in r.text
        double.mode = "offline"
        err = c.post("/api/v1/telegram/validate", json={"token": BOT_TOKEN}).json()["error"]
        assert err["message_ru"] == "Нет связи с Telegram — проверь интернет и попробуй ещё раз"
        assert "ConnectError" in err["details"] and BOT_TOKEN not in err["details"]
        double.mode = "blocked"
        lost = c.post("/api/v1/telegram/test", json={"token": BOT_TOKEN, "chat_id": "123456", "with_deal": False})
        assert lost.status_code == 502 and "заблокировал бота" in lost.json()["error"]["message_ru"]
        double.mode = "proxy"
        lost = c.post("/api/v1/telegram/test", json={"token": BOT_TOKEN, "chat_id": "123456", "with_deal": False})
        assert "недоступен из этой сети" in lost.json()["error"]["message_ru"]


def test_ebay_proxy_403_and_email_network_errors(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path, seed=False)

    def ebay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Host not in allowlist: api.ebay.com. Add this host to your network egress")

    app.state.api.http_transport = httpx.MockTransport(ebay)

    class DeadSMTP:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise OSError(97, "Address family not supported by protocol")

    app.state.api.smtp_factory = DeadSMTP
    with TestClient(app, base_url=LOCAL) as c:
        r = c.post("/api/v1/ebay/test", json={"client_id": "App-PRD-1", "client_secret": "PRD-2"})
        err = r.json()["error"]
        assert r.status_code == 502 and err["message_ru"].startswith("eBay недоступен из этой сети")
        assert "allowlist" in err["details"] and clean_text(err["message_ru"]) and "eBay: eBay" not in err["message_ru"]
        body = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "username": "me@gmail.com", "password": "x",
                "to_addrs": "me@gmail.com"}
        mail = c.post("/api/v1/email/test", json=body).json()["error"]
        assert mail["message_ru"].startswith("Не достучался до Gmail") and "Errno 97" in mail["details"]
        bad = c.post("/api/v1/email/test", json={**body, "to_addrs": "not-an-address"})
        assert bad.status_code == 422 and bad.json()["error"]["fields"]["to_addrs"].startswith("Это не похоже")
        assert "to_addrs" not in bad.json()["error"]["message_ru"]


def test_ai_texts_agree_between_health_and_the_test(tmp_path: Path) -> None:
    app, config, db, path, monitor = make_app(tmp_path, seed=False, ai_probe=lambda url: None)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app.state.api.http_transport = httpx.MockTransport(offline)
    config.ai.enabled, config.ai.provider, config.ai.base_url = True, "openai", "http://localhost:1234/v1"
    monitor.health = {"ok": False, "server_ok": False, "error": "Локальная модель недоступна (ConnectError)"}
    with TestClient(app, base_url=LOCAL) as c:
        tile = c.get("/api/v1/health").json()["ai"]
        test = c.post("/api/v1/ai/test", json={"sample": False}).json()
        assert tile["error_ru"] == test["error_ru"] == LMSTUDIO_DOWN
        assert tile["state_ru"] == "Не отвечает" and tile["latency_ms"] is None  # no "1 мс" next to "не отвечает"
        detect = c.post("/api/v1/ai/detect").json()
        assert detect["variant"] == "not_running" and detect["message_ru"] == LMSTUDIO_DOWN


def test_check_link_errors_are_plain(api) -> None:
    from ebeyparser.scraper.ebay_api import EBAY_NOT_CONNECTED_RU, EbayAPIError

    c, app, config, db, path, monitor = api

    async def not_connected(url: str, **kwargs: Any) -> Any:
        raise EbayAPIError("Для поиска по eBay заполни ebay.client_id в config.yaml/.env",
                           message_ru=EBAY_NOT_CONNECTED_RU, code="ebay_not_connected")

    monitor.evaluate_url = not_connected
    job = c.post("/api/v1/check", json={"url": "https://www.ebay.de/itm/123456789012"}).json()["job"]
    done = wait_job(c, job["id"])
    assert done["status"] == "error" and done["error"]["message_ru"] == EBAY_NOT_CONNECTED_RU
    assert done["error"]["action"]["href"] == "/settings/ebay" and "config.yaml" in done["error"]["details"]


def test_tailscale_missing_is_its_own_error(api, monkeypatch: pytest.MonkeyPatch) -> None:
    c, *_ = api
    from ebeyparser.web.api import routes_app

    monkeypatch.setattr(routes_app, "_tailscale_ips", lambda: [])
    r = c.put("/api/v1/access", json={"mode": "tailscale"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "tailscale_not_found"
    assert "host:" not in r.json()["error"]["message_ru"]


async def test_monitor_run_errors_are_plain(tmp_path: Path) -> None:
    from ebeyparser.monitor import Monitor

    general = {"data_dir": str(tmp_path)}
    monitor = Monitor(AppConfig(general=general, searches=[SearchConfig(name="eBay", source="ebay", query="rtx")]),
                      Database(), notifiers=[])
    try:
        summary = await monitor.run_once()
    finally:
        await monitor.aclose()
    assert summary.errors and all(clean_text(e) for e in summary.errors)
    assert summary.error_details and "client_id" in summary.error_details[0]
    empty = await Monitor(AppConfig(general=general), Database(), notifiers=[]).run_once()
    assert empty.errors == ["Нет активных поисков — добавь их на странице «Поиски»"]


# -------------------------------------------------------------- 6. validation
def test_deal_money_is_validated_not_clamped(api) -> None:
    c, app, config, db, *_ = api
    for body, field in (({"status": "bought", "bought_price": -20}, "bought_price"),
                        ({"bought_price": 0}, "bought_price"),
                        ({"status": "sold", "sold_price": 0}, "sold_price"),
                        ({"extra_costs": -1}, "extra_costs"),
                        ({"sold_price": 5_000_000}, "sold_price")):
        r = c.patch(f"/api/v1/deals/{RTX}", json=body)
        assert r.status_code == 422 and field in r.json()["error"]["fields"], body
    assert c.get(f"/api/v1/deals/{RTX}").json()["bought_price"] is None  # nothing saved
    free = "2894410057"  # the free monitor: 0 € is right
    ok = c.patch(f"/api/v1/deals/{free}", json={"status": "bought", "bought_price": 0})
    assert ok.status_code == 200 and ok.json()["bought_price"] == 0
    # demo ThinkPad: bought without a saved price -> its ad price (150 €); the free one counts 0 €, not its ad price
    assert c.get("/api/v1/pipeline/summary").json()["invested"] == 150


def test_settings_and_searches_reject_absurd_values(api) -> None:
    c, app, config, *_ = api
    r = c.patch("/api/v1/settings", json={"pricing": {"min_profit": -50, "safety_margin_percent": 150}})
    fields = r.json()["error"]["fields"]
    assert r.status_code == 422 and set(fields) == {"pricing.min_profit", "pricing.safety_margin_percent"}
    assert fields["pricing.safety_margin_percent"] == "Запас на торг и риск — от 0 до 50 %"
    assert c.patch("/api/v1/settings", json={"pricing": {"safety_margin_percent": 60}}).status_code == 422
    assert config.pricing.min_profit == 40 and config.pricing.safety_margin_percent == 10  # untouched
    wish = c.post("/api/v1/searches", json={"name": "Для себя: RTX", "query": "rtx 3090", "purpose": "personal",
                                            "target_price": 600, "max_price": 500})
    assert wish.status_code == 422 and "max_price" in wish.json()["error"]["fields"]
    swapped = c.post("/api/v1/searches", json={"name": "X", "query": "x", "min_price": 300, "max_price": 100})
    err = swapped.json()["error"]
    assert swapped.status_code == 422 and err["message_ru"].count("«Цена от»") == 1  # not "«Цена от» — «Цена от» …"
    assert c.post("/api/v1/searches", json={"name": "Y", "query": "y", "max_price": -5}).status_code == 422
    setup = c.post("/api/v1/setup/preview", json={"location": "Berlin", "category_ids": [173],
                                                  "wishlist": [{"item": "RTX", "max_price": -5}]})
    assert setup.status_code == 422
    margin = c.post("/api/v1/setup/preview", json={"location": "Berlin", "category_ids": [173],
                                                   "pricing": {"safety_margin_percent": 95}})
    assert margin.status_code == 422
    assert c.get("/api/v1/deals", params={"min_price": 500, "max_price": 100}).status_code == 422


# -------------------------------------------------------------------- 7. setup
def test_setup_adds_by_default_and_replace_lists_what_goes(api) -> None:
    c, app, config, db, path, monitor = api
    config.searches = [SearchConfig(name="steam deck", query="steam deck"),
                       SearchConfig(name="Для себя: RTX 3090", query="RTX 3090", purpose="personal", target_price=500)]
    assert c.get("/api/v1/onboarding").json()["existing_searches"] == 2
    body = {"location": "Berlin", "radius_km": 30, "category_ids": [278],
            "wishlist": [{"item": "rtx 3090", "max_price": 550}]}
    preview = c.post("/api/v1/setup/preview", json=body).json()
    assert preview["replace"] is False and preview["created"] == ["Notebooks · Berlin 30 км"]
    assert preview["updated"] == ["Для себя: rtx 3090"] and preview["kept"] == ["steam deck"]
    assert preview["replaced"] == [] and preview["result_count"] == 3 and preview["wishlist_searches"] == 1
    assert preview["summary_ru"].startswith("Добавлю 1 поиск")
    applied = c.post("/api/v1/setup", json=body).json()
    names = [s.name for s in load_config(path).searches]
    assert names == ["steam deck", "Для себя: rtx 3090", "Notebooks · Berlin 30 км"] and applied["replaced"] == []
    # the preview's load == the «Поиски» card after saving == /setup/estimate for the same counts
    card = c.get("/api/v1/searches").json()["estimate"]
    assert preview["estimate"]["pages_per_hour"] == card["pages_per_hour"] == applied["estimate"]["pages_per_hour"]
    same = c.get("/api/v1/setup/estimate", params={"categories": 1, "keywords": 2,
                                                   "interval": preview["interval_minutes"]}).json()
    assert same["pages_per_hour"] == card["pages_per_hour"] and same["pages_label_ru"] == card["pages_label_ru"]
    assert card["short_ru"] and "из 150 страниц в час" in card["pages_label_ru"]
    replaced = c.post("/api/v1/setup", json={**body, "category_ids": [173], "wishlist": [], "replace": True}).json()
    assert set(replaced["replaced"]) == {"steam deck", "Для себя: rtx 3090", "Notebooks · Berlin 30 км"}
    assert [s.name for s in load_config(path).searches] == ["Handy & Telefon · Berlin 30 км"]
    assert replaced["summary_ru"].startswith("Заменю твои 3 поиска")


def test_setup_during_a_pause_says_so(api) -> None:
    c, app, config, db, path, monitor = api
    monitor.http_status = lambda: blocked_status(45)
    r = c.post("/api/v1/setup", json={"location": "Berlin", "category_ids": [173], "start_run": True}).json()
    assert r["cooldown"] and not r["run_started"] and monitor.calls == 0
    assert "попросил паузу" in r["message_ru"] and "после паузы" in r["message_ru"]


# ---------------------------------------------------------------- 8. parse-url
def test_parse_url_reads_the_place_from_the_right_element(api) -> None:
    c, *_ = api

    def parse(url: str) -> Any:
        return c.post("/api/v1/searches/parse-url", json={"url": url})

    notebooks = parse("https://www.kleinanzeigen.de/s-notebooks/berlin/preis:100:500/c278l3331r20").json()
    assert notebooks["category_name"] == "Notebooks" and notebooks["location"] == "Berlin"
    assert notebooks["radius_km"] == 20 and notebooks["suggested_name"] == "Notebooks · Berlin 20 км"
    assert notebooks["search"]["location"] == "Berlin" and notebooks["search"]["radius_km"] == 20
    words = parse("https://www.kleinanzeigen.de/s-notebooks/berlin/thinkpad/k0c278l3331r20").json()
    assert words["query"] == "thinkpad" and words["location"] == "Berlin" and words["category_id"] == 278
    district = parse("https://www.kleinanzeigen.de/s-neukoelln/rtx-3090/k0l3375r10").json()
    assert district["location"] == "Neukölln" and district["query"] == "rtx 3090"
    google = parse("https://www.google.com/search?q=rtx")
    err = google.json()["error"]
    assert google.status_code == 422 and err["code"] == "not_a_search_url"
    assert err["message_ru"].startswith("Это не ссылка Kleinanzeigen") and err["fields"]["url"]
    ebay = parse("https://www.ebay.de/sch/i.html?_nkw=rtx+3090&_udlo=100&_udhi=500&LH_Auction=1").json()
    assert ebay["source"] == "ebay" and ebay["search"]["source"] == "ebay" and ebay["search"]["query"] == "rtx 3090"
    assert ebay["search"]["buying_options"] == ["AUCTION"] and ebay["search"]["max_price"] == 500
    assert ebay["warning_ru"]  # eBay is not connected in this app


# ------------------------------------------------------------ 9. place labels
def test_search_cards_show_the_picked_place(api) -> None:
    c, *_ = api
    r = c.post("/api/v1/searches", json={"name": "Handy · Neukölln", "category_id": 173, "location": "12043",
                                         "radius_km": 50})
    view = r.json()
    assert r.status_code == 201 and view["location_label"] == "Neukölln" and view["where_ru"] == "Neukölln · 50 км"
    assert view["summary"].startswith("Neukölln + 50 км") and "12043" not in view["summary"]
    from ebeyparser.scraper.categories import scan_name

    assert scan_name("Notebooks", "12043", 50) == "Notebooks · Neukölln 50 км"
    assert [p["kind"] for p in c.get("/api/v1/locations", params={"q": "00000"}).json()["items"]] == []
    assert c.get("/api/v1/locations", params={"q": "13599"}).json()["items"][0]["kind"] in ("plz", "district")


# -------------------------------------------------- 10. optional checks never pause the app
def _polite(handler: Any, tmp_path: Path, *, soft: bool = False) -> Any:
    from ebeyparser.scraper.http import PoliteClient

    client = PoliteClient(delay_range=(0, 0), max_retries=0, transport=httpx.MockTransport(handler),
                          state_path=tmp_path / "http_state.json", max_requests_per_hour=150)
    client.soft_403 = soft
    return client


async def test_live_categories_403_without_block_page_is_not_a_block(tmp_path: Path) -> None:
    from ebeyparser.scraper.categories import discover_categories, discovery_client

    client = discovery_client(state_path=tmp_path / "http_state.json")
    assert client.soft_403  # the optional check never pauses the whole app on an ambiguous 403
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="")))
    try:
        result = await discover_categories(client, "Berlin", 30)
        assert not result.live and result.error and clean_text(result.error)
        assert client.cooldown_remaining("https://www.kleinanzeigen.de") == 0  # no one-hour pause for everything
    finally:
        await client.aclose()


async def test_real_block_pages_and_plain_403_still_pause(tmp_path: Path) -> None:
    from ebeyparser.scraper.http import BlockedError

    captcha = "<html><iframe src='https://geo.captcha-delivery.com/captcha'></iframe></html>"
    soft = _polite(lambda r: httpx.Response(403, text=captcha), tmp_path, soft=True)
    with pytest.raises(BlockedError):
        await soft.get_text("https://www.kleinanzeigen.de/s-berlin/k0")
    assert soft.cooldown_remaining("https://www.kleinanzeigen.de") > 3000  # a real block page: protected
    await soft.aclose()
    strict = _polite(lambda r: httpx.Response(403, text=""), tmp_path / "b")
    with pytest.raises(BlockedError):
        await strict.get_text("https://www.kleinanzeigen.de/s-berlin/k0")
    assert strict.cooldown_remaining("https://www.kleinanzeigen.de") > 3000  # the monitor keeps its protection
    await strict.aclose()


async def test_a_proxy_403_is_a_network_error_not_a_block(tmp_path: Path) -> None:
    client = _polite(lambda r: httpx.Response(403, text="Host not in allowlist: www.kleinanzeigen.de"), tmp_path)
    with pytest.raises(httpx.ProxyError) as caught:
        await client.get_text("https://www.kleinanzeigen.de/s-berlin/k0")
    assert client.cooldown_remaining("https://www.kleinanzeigen.de") == 0
    assert humanize(caught.value, "kleinanzeigen").message_ru.startswith("Kleinanzeigen недоступен из этой сети")
    await client.aclose()


# ------------------------------------------------------------------ misc (P2)
def test_csv_is_localised_and_wipe_vacuums(api) -> None:
    c, app, config, db, *_ = api
    text = c.get("/api/v1/export/deals.csv", params={"verdict": "all", "status": "any"}).content.decode("utf-8-sig")
    assert "Kleinanzeigen" in text and ";buy;" not in text and "+00:00" not in text
    assert c.post("/api/v1/data/reset-all", json={"confirm": "удалить"}).status_code == 200


def test_german_site_words_are_translated(api) -> None:
    c, *_ = api
    deal = c.get(f"/api/v1/deals/{RTX}").json()
    assert deal["posted_at_ru"].startswith("сегодня") and deal["condition_ru"] == "Хорошее"
    keys = {row["key_ru"] for row in deal["listing"]["attributes_ru"]}
    assert {"Тип", "Состояние", "Доставка"} <= keys
    dates = [comp["date_ru"] for comp in deal["evaluation"]["estimate"]["comparables"]]
    assert not any("Verkauft" in d or "Heute" in d or "Gestern" in d for d in dates)
    ebay = c.get("/api/v1/deals/ebay-205873410266").json()
    note = next(r["note"] for r in ebay["breakdown"]["rows"] if r["key"] == "buy")
    assert "4,99" in note and "4.99" not in note


def test_health_alert_events_have_a_toast_text(tmp_path: Path) -> None:
    from ebeyparser.monitor import Monitor

    monitor = Monitor(AppConfig(), Database())
    seen: list[tuple[str, dict[str, Any]]] = []
    monitor.on_event.append(lambda kind, data: seen.append((kind, data)))
    monitor._emit_health("blocked", "⚠ Kleinanzeigen попросил паузу", timedelta(hours=1))
    kind, data = seen[-1]
    assert kind == "health_alert" and data["text_ru"] == "Kleinanzeigen попросил паузу" and data["at_label"]


def test_notify_error_carries_a_plain_message() -> None:
    import asyncio

    from ebeyparser.config import TelegramConfig
    from ebeyparser.notify.telegram import TelegramNotifier

    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "error_code": 403,
                                         "description": "Forbidden: bot was blocked by the user"})

    notifier = TelegramNotifier(TelegramConfig(enabled=True, bot_token=BOT_TOKEN, chat_id="1"),
                                transport=httpx.MockTransport(blocked))
    with pytest.raises(Exception) as caught:
        asyncio.run(notifier.send_text("hi"))
    exc = caught.value
    assert "заблокировал бота" in exc.message_ru and "bot_token" not in exc.message_ru  # type: ignore[attr-defined]
    assert "chat_id" in str(exc)  # the technical text stays for the log / CLI


def test_learning_note_has_no_zero_phrases(tmp_path: Path) -> None:
    app, config, *_ = make_app(tmp_path, seed=False)
    config.searches = [SearchConfig(name="Neu", category_id=173, location="Berlin")]
    with TestClient(app, base_url=LOCAL) as c:
        note = c.get("/api/v1/monitor").json()["learning"]["message_ru"]
    assert "0 из" not in note and "0 цен" not in note and "около" in note


def test_dataset_is_json_serialisable(api) -> None:
    c, *_ = api
    for url in ("/api/v1/app", "/api/v1/monitor", "/api/v1/health?ai=0", "/api/v1/summary/today", "/api/v1/searches"):
        r = c.get(url)
        assert r.status_code == 200, url
        json.dumps(r.json())


def test_run_summary_keeps_error_details() -> None:
    run = RunSummary(errors=["a"], error_details=["b"])
    assert RunSummary.model_validate(run.model_dump(mode="json")).error_details == ["b"]
    assert RunSummary.model_validate({"errors": ["old"]}).error_details == []

