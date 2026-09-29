"""/api/v1: app state, onboarding, settings, secrets, setup, searches, deals, pipeline, stats,
health, logs, demo, backup, SSE, errors and security. No network."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api_v1_helpers import (
    BOT_TOKEN,
    EBAY,
    IPHONE_SKIP,
    LOCAL,
    MACMINI,
    RTX,
    clean_environ,
    events_of,
    make_app,
    restore_environ,
    wait_job,
)
from ebeyparser.config import AppConfig, SearchConfig, load_config
from ebeyparser.db import Database
from ebeyparser.models import Comparable, Evaluation, Listing, utcnow
from ebeyparser.web.app import create_app
from ebeyparser.scraper.categories import merge_categories, parse_category_links


@pytest.fixture(autouse=True)
def isolated_env():
    saved = clean_environ()
    yield
    restore_environ(saved)


@pytest.fixture()
def api(tmp_path: Path):
    calls: list[tuple[str, int]] = []

    async def discover(location: str, radius: int):
        calls.append((location, radius))
        html = ('<a href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a> (12.345)'
                '<a href="/s-berlin/sammeln/c234l3331r30">Sammeln</a> (321)')
        return merge_categories(parse_category_links(html))

    app, config, db, path, monitor = make_app(tmp_path, category_discovery=discover)
    with TestClient(app, base_url=LOCAL) as client:
        client.calls = calls  # type: ignore[attr-defined]
        yield client, app, config, db, path, monitor


# ------------------------------------------------------------------ app / onboarding
def test_app_state_and_onboarding_flow(api) -> None:
    c, app, config, db, path, _ = api
    db.set_state("onboarding:bootstrapped", "1")  # config.yaml created by `run` for the web onboarding
    info = c.get("/api/v1/app").json()
    assert info["onboarded"] is False and info["config_exists"] and info["config_writable"]
    steps = {s["key"]: s for s in info["onboarding"]["steps"]}
    assert set(steps) == {"location", "categories", "money", "wishlist", "ai", "telegram", "ebay"}
    assert not steps["categories"]["done"] and steps["location"]["required"] and not steps["ebay"]["required"]
    assert info["onboarding"]["next_step"] == "location"
    assert info["features"]["monitor"] and info["features"]["pause"] and info["features"]["check_url"]
    assert info["demo"]["loaded"] and info["demo"]["count"] == 14 and info["counts"]["searches"] == 0

    r = c.post("/api/v1/setup", json={"location": "Berlin", "radius_km": 25, "category_ids": [173, 278],
                                      "max_price": 300, "strategy": "careful",
                                      "wishlist": [{"item": "RTX 3090", "max_price": 550, "haggle": False}]})
    assert r.status_code == 200, r.text
    view = c.get("/api/v1/onboarding").json()
    done = {s["key"]: s["done"] for s in view["steps"]}
    assert done["location"] and done["categories"] and done["money"] and done["wishlist"] and not done["ai"]
    assert c.get("/api/v1/app").json()["onboarded"] is False  # AI neither on nor skipped
    skipped = c.post("/api/v1/onboarding/skip", json={"step": "ai"}).json()
    assert next(s for s in skipped["steps"] if s["key"] == "ai")["skipped"]
    assert c.get("/api/v1/app").json()["onboarded"] is True
    c.post("/api/v1/onboarding/skip", json={"step": "ai", "skipped": False})
    assert c.get("/api/v1/app").json()["onboarded"] is False
    completed = c.post("/api/v1/onboarding/complete", json={}).json()
    assert completed["completed_at"] and c.get("/api/v1/app").json()["onboarded"] is True
    assert c.post("/api/v1/onboarding/skip", json={"step": "nope"}).status_code == 422


def test_config_from_before_the_web_onboarding_counts_as_onboarded(api) -> None:
    c, app, config, db, path, _ = api
    assert c.get("/api/v1/app").json()["onboarded"] is False  # no searches yet
    config.searches = [SearchConfig(name="Мой поиск", query="dyson")]
    info = c.get("/api/v1/app").json()
    assert info["onboarded"] is True and not config.ai.enabled  # CLI-era config: no forced wizard


def test_onboarding_draft_roundtrip(api) -> None:
    c = api[0]
    assert c.get("/api/v1/onboarding/draft").json() == {"draft": None}
    saved = c.put("/api/v1/onboarding/draft", json={"step": 3, "location": "Köln", "categories": [173]}).json()
    assert saved["draft"]["location"] == "Köln"
    assert c.get("/api/v1/onboarding/draft").json()["draft"] == {"step": 3, "location": "Köln", "categories": [173]}
    assert c.put("/api/v1/onboarding/draft", content=b"x" * 70_000,
                 headers={"content-type": "application/json"}).status_code == 413
    assert c.delete("/api/v1/onboarding/draft").json() == {"draft": None}


# ------------------------------------------------------------------ locations / categories
def test_locations(api) -> None:
    c = api[0]
    items = c.get("/api/v1/locations", params={"q": "munchen"}).json()["items"]
    assert items[0]["name"] == "München" and items[0]["value"] == "München" and items[0]["state"] == "Bayern"
    assert c.get("/api/v1/locations", params={"q": "Кёльн"}).json()["items"][0]["name"] == "Köln"
    kreuzberg = c.get("/api/v1/locations", params={"q": "kreuzb"}).json()["items"][0]
    assert kreuzberg["kind"] == "district" and kreuzberg["value"] == "10997" and kreuzberg["parent"] == "Berlin"
    assert c.get("/api/v1/locations", params={"q": "Hmaburg"}).json()["items"][0]["name"] == "Hamburg"  # typo
    plz = c.get("/api/v1/locations", params={"q": "12345"}).json()["items"][0]
    assert plz["kind"] == "plz" and plz["value"] == "12345"
    halle = c.get("/api/v1/locations", params={"q": "halle"}).json()["items"][0]
    assert halle["name"] == "Halle (Saale)" and halle["value"] == "06108"  # ambiguous name -> postal code
    assert len(c.get("/api/v1/locations", params={"q": "", "limit": 5}).json()["items"]) == 5


def test_categories_builtin_then_live_and_cached(api, tmp_path: Path) -> None:
    c = api[0]
    data = c.get("/api/v1/categories", params={"location": "Berlin", "radius_km": 25}).json()
    assert data["source"] == "builtin" and not data["live"] and c.calls == []
    first = data["items"][0]
    assert first["id"] == 173 and first["recommended"] and first["icon"] == "smartphone"
    assert 173 in data["recommended_ids"] and data["radius_km"] == 30  # snapped to the site's steps
    live = c.get("/api/v1/categories", params={"location": "Berlin", "radius_km": 30, "live": 1}).json()
    assert live["source"] == "live" and live["live"] and c.calls == [("Berlin", 30)]
    assert any(i["id"] == 234 for i in live["items"]) and (tmp_path / "data" / "categories.json").is_file()
    again = c.get("/api/v1/categories", params={"location": "berlin", "radius_km": 30}).json()
    assert again["source"] == "cache" and c.calls == [("Berlin", 30)]


# ------------------------------------------------------------------ setup
def test_setup_options_estimate_and_preview_saves_nothing(api) -> None:
    c, app, config, db, path, _ = api
    options = c.get("/api/v1/setup/options").json()
    assert set(options["presets"]) == {"careful", "balanced", "aggressive"}
    assert options["presets"]["balanced"]["min_profit"] == 40 and options["current"]["existing_searches"] == 0
    assert options["current"]["category_ids"] == options["recommended_category_ids"]
    est = c.get("/api/v1/setup/estimate", params={"categories": 7, "keywords": 1}).json()
    assert est["interval_minutes"] >= est["suggested_interval"] and est["level"] in ("ok", "warn", "danger")
    busy = c.get("/api/v1/setup/estimate", params={"categories": 12, "interval": 10}).json()
    assert busy["tight"] and busy["level"] == "danger" and busy["load_percent"] > 50
    before = path.read_text(encoding="utf-8")
    preview = c.post("/api/v1/setup/preview", json={"location": "Berlin", "category_ids": [173, 279, 999],
                                                    "category_names": {"999": "Sammeln"},
                                                    "wishlist": [{"item": "DDR4 ECC 64GB", "max_price": 90}]}).json()
    assert preview["valid"] and preview["count"] == 4 and preview["category_scans"] == 3
    names = [s["name"] for s in preview["searches"]]
    assert "Sammeln · Berlin 30 км" in names and "Для себя: DDR4 ECC 64GB" in names
    assert preview["searches"][0]["id"] == "handy-telefon-berlin-30-km"
    assert preview["estimate"]["category_scans"] == 3 and preview["suggested_interval"] >= 10
    assert path.read_text(encoding="utf-8") == before and config.searches == []
    alias = c.post("/api/v1/searches/preview", json={"location": "Berlin", "category_ids": [173]}).json()
    assert alias["count"] == 1
    bad = c.post("/api/v1/setup/preview", json={"location": "", "category_ids": []}).json()
    assert not bad["valid"] and "location" in bad["problems"]
    r = c.post("/api/v1/setup", json={"location": "  ", "category_ids": [173]})
    assert r.status_code == 422 and r.json()["error"]["fields"]["location"]
    r = c.post("/api/v1/setup", json={"location": "Berlin", "category_ids": []})
    assert r.status_code == 422 and "category_ids" in r.json()["error"]["fields"]
    assert c.post("/api/v1/setup", json={"location": "Berlin", "radius_km": -1}).status_code == 422


def test_setup_apply_writes_everything(api) -> None:
    c, app, config, db, path, monitor = api
    body = {"location": "Hamburg", "radius_km": 50, "category_ids": [173, 278], "purpose": "resale",
            "max_price": 350, "strategy": "aggressive", "interval_minutes": 45,
            "wishlist": [{"item": "RTX 3090", "max_price": 550, "haggle": False},
                         {"item": "Ryzen 9 5950X", "max_price": 250}],
            "start_run": True, "complete_onboarding": True}
    r = c.post("/api/v1/setup", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["saved"] == 4 and data["run_started"] and data["interval_minutes"] == 45
    saved = load_config(path)
    assert [s.name for s in saved.searches] == ["Handy & Telefon · Hamburg 50 км", "Notebooks · Hamburg 50 км",
                                                "Для себя: RTX 3090", "Для себя: Ryzen 9 5950X"]
    rtx, ryzen = saved.searches[2], saved.searches[3]
    assert rtx.target_price == 550 and rtx.max_price == 550  # no +20 % when haggle is off
    assert ryzen.target_price == 250 and ryzen.max_price == 300
    assert saved.pricing.min_profit == 25 and saved.pricing.min_roi == pytest.approx(0.15)
    assert saved.pricing.safety_margin_percent == 7 and saved.pricing.min_comparables == 4
    assert saved.pricing.max_capital == 350 and saved.notifications.min_score == 60
    assert saved.general.interval_minutes == 45 and config.general.interval_minutes == 45
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# мой конфиг") and "# как часто проверять" in text and "${TELEGRAM_BOT_TOKEN}" in text
    assert (path.parent / "config.yaml.bak").is_file()
    assert monitor.updated is config and monitor.calls == 1
    assert c.get("/api/v1/app").json()["onboarding"]["completed_at"]
    assert events_of(app, "searches_changed") and events_of(app, "settings_changed")
    # replace=false keeps existing searches, same names are updated in place
    r = c.post("/api/v1/searches/apply", json={"location": "Hamburg", "radius_km": 50, "category_ids": [279],
                                               "replace": False})
    assert r.status_code == 200
    assert [s.name for s in load_config(path).searches][-1] == "Konsolen · Hamburg 50 км"
    assert len(load_config(path).searches) == 5


# ------------------------------------------------------------------ settings / secrets
def test_settings_get_masks_secrets(api) -> None:
    c, app, config, *_ = api
    config.notifications.telegram.bot_token = BOT_TOKEN
    config.notifications.email.username = "maksem706@gmail.com"
    data = c.get("/api/v1/settings").json()
    assert data["editable"] and data["general"]["interval_minutes"] == 30 and data["region"]["radius_km"] == 30
    assert "bot_token" not in data["notifications"]["telegram"] and "password" not in data["notifications"]["email"]
    assert "api_key" not in data["ai"] and "api_key" not in data["ai"]["second_opinion"]
    assert "client_secret" not in data["ebay"] and data["ebay"]["configured"] is False
    tg = data["secrets"]["telegram_bot_token"]
    assert tg == {"set": True, "masked": "…2345", "label": "Токен Telegram-бота", "env": "TELEGRAM_BOT_TOKEN",
                  "in_env_file": False, "in_config": False}
    assert data["secrets"]["smtp_user"]["masked"] == "ma***@gmail.com"
    assert not data["secrets"]["smtp_password"]["set"]
    assert BOT_TOKEN not in json.dumps(data) and "maksem706" not in json.dumps(data)


def test_settings_patch_validates_and_keeps_comments(api) -> None:
    c, app, config, db, path, monitor = api
    r = c.patch("/api/v1/settings", json={"general": {"interval_minutes": 20, "max_ai_per_run": 10},
                                          "pricing": {"min_roi": 0.3, "max_capital": 500},
                                          "ai": {"enabled": True, "model": "qwen2.5vl:7b", "provider": "ollama",
                                                 "base_url": "http://localhost:11434",
                                                 "second_opinion": {"min_score": 80}},
                                          "notifications": {"verdicts": ["buy", "maybe"], "heartbeat_hour": None}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "general.interval_minutes" in body["applied"] and body["restart_required"] == []
    saved = load_config(path)
    assert saved.general.interval_minutes == 20 and saved.pricing.min_roi == pytest.approx(0.3)
    assert saved.ai.provider == "ollama" and saved.ai.second_opinion.min_score == 80
    assert saved.notifications.verdicts == ["buy", "maybe"] and saved.notifications.heartbeat_hour is None
    assert config.general.max_ai_per_run == 10 and monitor.updated is config  # applied live
    text = path.read_text(encoding="utf-8")
    assert "# как часто проверять" in text and "${SMTP_PASSWORD}" in text
    ev = events_of(app, "settings_changed")[-1]
    assert "general" in ev.data["sections"] and "general.interval_minutes" in ev.data["keys"]

    bad = c.patch("/api/v1/settings", json={"general": {"interval_minutes": 1, "max_pages": "много"}})
    assert bad.status_code == 422
    err = bad.json()["error"]
    assert err["code"] == "validation" and "general.max_pages" in err["fields"]
    bad = c.patch("/api/v1/settings", json={"general": {"interval_minutes": 2}})
    assert bad.json()["error"]["fields"] == {
        "general.interval_minutes": "Проверять можно не чаще раза в 5 минут и не реже раза в сутки"}
    bad = c.patch("/api/v1/settings", json={"notifications": {"telegram": {"bot_token": "x"}},
                                            "general": {"nope": 1}, "cloud": {}})
    fields = bad.json()["error"]["fields"]
    assert set(fields) == {"notifications.telegram.bot_token", "general.nope", "cloud"}
    bad = c.patch("/api/v1/settings", json={"notifications": {"telegram": {"enabled": True}}})
    assert "Сначала подключи бота" in bad.json()["error"]["fields"]["notifications.telegram.enabled"]
    bad = c.patch("/api/v1/settings", json={"ai": {"base_url": "ftp://x"}})
    assert "ai.base_url" in bad.json()["error"]["fields"]
    assert load_config(path).general.interval_minutes == 20  # nothing written by failed patches

    one = c.patch("/api/v1/settings/pricing", json={"min_profit": 55})
    assert one.status_code == 200 and one.json()["pricing"]["min_profit"] == 55
    assert c.patch("/api/v1/settings/nope", json={"x": 1}).status_code == 404
    web = c.patch("/api/v1/settings/web", json={"port": 8080})
    assert web.json()["restart_required"] == ["web.port"]
    assert c.patch("/api/v1/settings", json={}).status_code == 400


def test_settings_region_moves_searches(api) -> None:
    c, app, config, db, path, _ = api
    c.post("/api/v1/setup", json={"location": "Berlin", "radius_km": 30, "category_ids": [173],
                                  "wishlist": [{"item": "RTX 3090", "max_price": 500}]})
    r = c.patch("/api/v1/settings", json={"region": {"location": "Leipzig", "radius_km": 45}})
    assert r.status_code == 200, r.text
    saved = load_config(path).searches
    assert saved[0].name == "Handy & Telefon · Leipzig 50 км" and saved[0].location == "Leipzig"
    assert saved[1].location == "Leipzig" and saved[1].radius_km == 50 and saved[1].name == "Для себя: RTX 3090"
    assert r.json()["region"] == {"location": "Leipzig", "location_label": "Leipzig", "radius_km": 50,
                                  "radius_choices": [0, 5, 10, 20, 30, 50, 100, 150, 200]}


def test_secrets_go_to_env_file_only(api) -> None:
    c, app, config, db, path, _ = api
    r = c.put("/api/v1/secrets", json={"telegram_bot_token": BOT_TOKEN, "telegram_chat_id": "987654321",
                                       "smtp_password": "app pass word", "anthropic_api_key": "sk-ant-123456789"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["updated"]) == {"telegram_bot_token", "telegram_chat_id", "smtp_password", "anthropic_api_key"}
    assert BOT_TOKEN not in r.text and "987654321" not in r.text and body["secrets"]["telegram_chat_id"]["set"]
    env = (path.parent / ".env").read_text(encoding="utf-8")
    assert f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}" in env and "SMTP_PASSWORD=app pass word" in env
    text = path.read_text(encoding="utf-8")
    assert BOT_TOKEN not in text and "sk-ant" not in text and "api_key: ${ANTHROPIC_API_KEY}" in text
    assert config.notifications.telegram.bot_token == BOT_TOKEN and config.ai.second_opinion.api_key == "sk-ant-123456789"
    assert body["secrets"]["telegram_bot_token"]["in_env_file"]
    cleared = c.put("/api/v1/secrets", json={"smtp_password": ""}).json()
    assert not cleared["secrets"]["smtp_password"]["set"] and config.notifications.email.password == ""
    bad = c.put("/api/v1/secrets", json={"telegram_bot_token": "nope", "notify_email": "no-at-sign"})
    assert bad.status_code == 422 and set(bad.json()["error"]["fields"]) == {"telegram_bot_token", "notify_email"}
    assert c.put("/api/v1/secrets", json={"unknown": "x"}).status_code == 422


def test_read_only_without_config_file(tmp_path: Path) -> None:
    db = Database()
    app = create_app(AppConfig(), db)
    with TestClient(app, base_url=LOCAL) as c:
        info = c.get("/api/v1/app").json()
        assert not info["config_writable"] and not info["config_exists"] and not info["features"]["monitor"]
        for method, url, body in (("PATCH", "/api/v1/settings", {"general": {"interval_minutes": 20}}),
                                  ("PUT", "/api/v1/secrets", {"telegram_chat_id": "12345"}),
                                  ("POST", "/api/v1/searches", {"name": "x", "query": "y"}),
                                  ("POST", "/api/v1/setup", {"location": "Berlin", "category_ids": [173]})):
            r = c.request(method, url, json=body)
            assert r.status_code == 403 and r.json()["error"]["code"] == "read_only", (url, r.text)
        assert c.post("/api/v1/monitor/run").json()["error"]["code"] == "unavailable"
        assert c.post("/api/v1/monitor/pause").status_code == 503
        assert c.post("/api/v1/check", json={"url": "https://www.kleinanzeigen.de/s-anzeige/x/2891234567-1-2"}
                      ).status_code == 503


# ------------------------------------------------------------------ searches
def test_searches_crud(api) -> None:
    c, app, config, db, path, monitor = api
    r = c.post("/api/v1/searches", json={"name": "Видеокарты Берлин", "query": "rtx 3080", "location": "Berlin",
                                         "radius_km": 20, "max_price": 500})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["id"] == "videokarty-berlin" and created["kind"] == "keyword" and created["enabled"]
    assert created["summary"] == "Berlin + 20 км · «rtx 3080» · до 500 €"
    stats = created["stats"]
    assert stats["ads"] == 2 and stats["deals"] == 1 and stats["maybe"] == 1  # demo ads of that search name
    assert c.post("/api/v1/searches", json={"name": "Видеокарты Берлин", "query": "x"}).status_code == 409
    bad = c.post("/api/v1/searches", json={"name": "Пусто"})
    assert bad.status_code == 422 and "query" in bad.json()["error"]["fields"]
    bad = c.post("/api/v1/searches", json={"name": "x", "query": "y", "min_price": 50, "max_price": 10})
    assert bad.json()["error"]["fields"] == {"min_price": "«Цена от» больше, чем «Цена до»"}
    bad = c.post("/api/v1/searches", json={"name": "x", "query": "y", "purpose": "flip"})
    assert "purpose" in bad.json()["error"]["fields"]

    listed = c.get("/api/v1/searches").json()
    assert [s["id"] for s in listed["items"]] == ["videokarty-berlin"] and listed["editable"]
    assert listed["estimate"]["keyword_searches"] == 1
    assert c.get("/api/v1/searches/videokarty-berlin").json()["name"] == "Видеокарты Берлин"
    assert c.get("/api/v1/searches/Видеокарты Берлин").json()["id"] == "videokarty-berlin"  # the name works too
    assert c.get("/api/v1/searches/nope").status_code == 404

    patched = c.patch("/api/v1/searches/videokarty-berlin", json={"max_price": 450, "name": "GPU Berlin"}).json()
    assert patched["id"] == "gpu-berlin" and patched["config"]["max_price"] == 450
    assert patched["config"]["query"] == "rtx 3080"  # untouched fields kept
    toggled = c.post("/api/v1/searches/gpu-berlin/toggle").json()
    assert toggled["enabled"] is False
    assert c.post("/api/v1/searches/gpu-berlin/toggle", json={"enabled": True}).json()["enabled"] is True
    dup = c.post("/api/v1/searches/gpu-berlin/duplicate")
    assert dup.status_code == 201 and dup.json()["name"] == "GPU Berlin (копия)"
    assert c.patch("/api/v1/searches/gpu-berlin-kopiya", json={"name": "GPU Berlin"}).status_code == 409
    assert c.delete("/api/v1/searches/gpu-berlin-kopiya").json() == {"deleted": "gpu-berlin-kopiya",
                                                                   "name": "GPU Berlin (копия)"}
    assert [s.name for s in load_config(path).searches] == ["GPU Berlin"]
    assert monitor.updated is config and events_of(app, "searches_changed")

    parsed = c.post("/api/v1/searches/parse-url",
                    json={"url": "https://www.kleinanzeigen.de/s-berlin/preis:100:500/c278l3331r20"}).json()
    assert parsed["category_id"] == 278 and parsed["radius_km"] == 20 and parsed["min_price"] == 100
    assert parsed["summary_ru"] == "Распознал: категория Notebooks, Berlin, 20 км, 100–500 €"
    assert parsed["search"]["url"].endswith("c278l3331r20")
    wrong = c.post("/api/v1/searches/parse-url", json={"url": "https://www.kleinanzeigen.de/s-anzeige/x/123"})
    assert wrong.status_code == 422 and "одно объявление" in wrong.json()["error"]["fields"]["url"]


# ------------------------------------------------------------------ deals
def _add_deal(db: Database, ad_id: str, **ev: object) -> None:
    listing = Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-1-2",
                      title="Sony PlayStation 5 Disc", price=380.0, negotiable=True, distance_km=12.0,
                      search_name="Konsolen", image_urls=["https://img.example/ps5.jpg"])
    db.upsert_listing(listing)
    db.save_evaluation(Evaluation(ad_id=ad_id, verdict="maybe", action="haggle", offer_price=330.0,
                                  max_buy_price=345.0, expected_profit=20.0, score=64.0, stage="full", **ev))


def test_deals_feed_filters_sort_and_pages(api) -> None:
    c, app, config, db, *_ = api
    _add_deal(db, "3000000001")
    feed = c.get("/api/v1/deals").json()
    assert feed["total"] == 12 and feed["limit"] == 30 and feed["page"] == 1 and feed["next_cursor"] is None
    ids = [d["id"] for d in feed["items"]]
    assert IPHONE_SKIP not in ids and "2893402211" not in ids  # skip verdict / hidden (ignored) by default
    card = next(d for d in feed["items"] if d["id"] == RTX)
    for key in ("title", "image", "price", "market_price", "profit", "roi", "offer_price", "max_buy_price", "action",
                "verdict", "score", "reasons", "red_flags", "ai_flags", "ai_checked", "status", "note",
                "distance_km", "posted_at_text", "first_seen", "source", "search_name", "seen"):
        assert key in card, key
    assert card["verdict_label"] == "Покупай" and card["status"] == "new" and card["seen"] is False

    def ids_of(**params: object) -> list[str]:
        r = c.get("/api/v1/deals", params=params)
        assert r.status_code == 200, r.text
        return [d["id"] for d in r.json()["items"]]

    # demo VB ads carry offers (RTX 3090, ThinkPad, HP Z440, eBay best offer), the auction bids
    assert set(ids_of(action="haggle")) == {"3000000001", RTX, "2893987145", "2893655120", "ebay-205873410266"}
    assert ids_of(action="bid") == [EBAY]
    assert set(ids_of(actionable=1)) >= {RTX, "3000000001"}
    assert set(ids_of(verdict="skip", status="any")) == {IPHONE_SKIP, "2893109876"}
    assert ids_of(status="ignored", verdict="all") == ["2893402211"]
    assert ids_of(status="bought", verdict="all") == ["2893987145"]
    assert set(ids_of(source="ebay")) == {EBAY, "ebay-205873410266"}
    assert set(ids_of(purpose="personal")) == {"2893790331", "2893655120"}
    assert ids_of(q="rtx 3090") == [RTX]
    assert all(d["distance_km"] <= 5 for d in c.get("/api/v1/deals", params={"max_km": 5}).json()["items"])
    no_ship = ids_of(shipping=0)
    assert "2894410057" in no_ship and RTX not in no_ship
    clean = c.get("/api/v1/deals", params={"no_flags": 1}).json()["items"]
    assert clean and "2894102266" not in [d["id"] for d in clean] and all(not d["red_flags"] for d in clean)
    assert ids_of(search="Консоли") == ["2893280764"] and ids_of(search="Konsolen") == ["3000000001"]
    config.searches = [SearchConfig(name="Консоли", query="ps5")]
    assert set(ids_of(search="konsoli,Konsolen")) == {"2893280764", "3000000001"}  # ids and names
    assert ids_of(min_score=93) == [RTX]
    assert ids_of(sort="distance")[0] == "2894410057"  # 2.1 km
    assert ids_of(sort="ending")[0] == EBAY  # the only auction
    assert ids_of(since="1h", verdict="all", status="any") == ids_of(since="1h", verdict="all", status="any")
    newest = ids_of(sort="fresh")
    assert newest[0] == "3000000001"

    page1 = c.get("/api/v1/deals", params={"limit": 5}).json()
    assert len(page1["items"]) == 5 and page1["pages"] == 3 and page1["next_cursor"]
    page2 = c.get("/api/v1/deals", params={"limit": 5, "cursor": page1["next_cursor"]}).json()
    by_page = c.get("/api/v1/deals", params={"limit": 5, "page": 2}).json()
    assert [d["id"] for d in page2["items"]] == [d["id"] for d in by_page["items"]]
    assert not {d["id"] for d in page1["items"]} & {d["id"] for d in page2["items"]}

    facets = c.get("/api/v1/deals", params={"facets": 1}).json()["facets"]
    assert facets["verdict"]["good"] == 12 and facets["action"]["haggle"] == 5 and facets["hidden"] >= 1
    assert facets["action"]["bid"] == 1 and facets["action"]["buy"] == 4
    assert facets["purpose"]["personal"] == 2 and facets["unseen"] == 12

    bad = c.get("/api/v1/deals", params={"verdict": "great", "sort": "random", "max_km": "far", "since": "yesterday"})
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"verdict", "sort", "max_km", "since"}
    assert c.get("/api/v1/deals", params={"cursor": "!!"}).status_code == 422


def test_deal_detail(api) -> None:
    c, app, config, db, *_ = api
    _add_deal(db, "3000000001")
    d = c.get(f"/api/v1/deals/{RTX}").json()
    assert d["id"] == RTX and d["listing"]["images"] and d["seller"]["type_label"]
    assert d["evaluation"]["estimate"]["comparables"] and d["evaluation"]["reasons"]
    assert d["ai"]["verdict_label"] and 0 <= d["ai"]["confidence_percent"] <= 100
    rows = {r["key"]: r["value"] for r in d["breakdown"]["rows"]}
    assert rows["market"] > 0 and rows["buy"] == -450 and d["breakdown"]["total"] == d["profit"]
    assert d["seller_message"]["language"] == "de" and d["seller_message"]["text"].startswith("Hallo,")
    assert d["pipeline"]["steps"][0] == {"key": "starred", "label": "Избранное", "done": False}
    assert d["links"]["market"] == f"/api/v1/deals/{RTX}/market" and d["product_type"] == "gpu"
    haggle = c.get("/api/v1/deals/3000000001").json()
    assert haggle["offer"]["offer_price"] == 330 and haggle["seller_message"]["kind"] == "haggle"
    assert "330 €" in haggle["seller_message"]["text"] and "Wäre" in haggle["seller_message"]["text"]
    assert haggle["product_type"] == "console"
    auction = c.get(f"/api/v1/deals/{EBAY}").json()
    assert auction["auction"]["ends_at"] and auction["seller_message"]["kind"] == "bid"
    assert c.get("/api/v1/deals/nope").json()["error"]["code"] == "not_found"
    assert not c.get(f"/api/v1/deals/{RTX}").json()["seen"]
    assert c.get(f"/api/v1/deals/{RTX}", params={"seen": 1}).json()["seen"] is True


def test_deal_pipeline_statuses_and_profit(api) -> None:
    c, app, config, db, *_ = api
    r = c.patch(f"/api/v1/deals/{RTX}", json={"status": "starred"})
    assert r.status_code == 200 and r.json()["status"] == "starred"
    contacted = c.patch(f"/api/v1/deals/{RTX}", json={"status": "contacted", "note": "Написал, жду"}).json()
    assert contacted["contacted_at"] and contacted["note"] == "Написал, жду"
    bought = c.patch(f"/api/v1/deals/{RTX}", json={"status": "bought"}).json()
    assert bought["bought_price"] == 420 and bought["bought_at"]  # the suggested offer (450 € VB) and bought["pipeline"]["steps"][2]["done"]
    fail = c.patch(f"/api/v1/deals/{RTX}", json={"status": "sold"})
    assert fail.status_code == 422 and "sold_price" in fail.json()["error"]["fields"]
    sold = c.patch(f"/api/v1/deals/{RTX}", json={"status": "sold", "sold_price": 600, "extra_costs": 12.5,
                                                 "bought_price": 430}).json()
    assert sold["status"] == "sold" and sold["realized_profit"] == 157.5 and sold["sold_at"]
    assert sold["pipeline"]["realized_profit"] == 157.5 and all(s["done"] for s in sold["pipeline"]["steps"])
    assert db.get_deal(RTX).status == "sold"
    back = c.patch(f"/api/v1/deals/{RTX}", json={"status": "bought"}).json()
    assert back["sold_at"] is None and back["realized_profit"] is None and back["bought_price"] == 430
    hidden = c.patch(f"/api/v1/deals/{MACMINI}", json={"status": "ignored", "hidden_reason": "далеко"}).json()
    assert hidden["status"] == "ignored" and hidden["hidden_reason"] == "далеко"
    assert c.patch(f"/api/v1/deals/{MACMINI}", json={"status": "starred"}).json()["hidden_reason"] is None
    assert c.patch(f"/api/v1/deals/{RTX}", json={}).status_code == 400
    assert c.patch(f"/api/v1/deals/{RTX}", json={"status": "lost"}).status_code == 422
    assert c.patch(f"/api/v1/deals/{RTX}", json={"bought_price": -1}).status_code == 422
    assert c.patch("/api/v1/deals/nope", json={"status": "starred"}).status_code == 404
    updates = events_of(app, "deal_updated")
    assert updates and updates[-1].data["card"]["id"] == MACMINI


def test_pipeline_board_and_summary(api) -> None:
    c, app, config, db, *_ = api
    now = utcnow()
    db.update_deal_state(RTX, status="sold", bought_price=400.0, sold_price=520.0, extra_costs=10.0,
                         bought_at=now - timedelta(days=5), sold_at=now)
    db.update_deal_state(EBAY, status="sold", bought_price=300.0, sold_price=380.0, bought_at=now, sold_at=now)
    db.update_deal_state("ebay-205873410266", status="sold", bought_price=280.0, sold_price=300.0,
                         bought_at=now, sold_at=now)
    db.update_deal_state("2894102266", status="contacted", contacted_at=now - timedelta(hours=30))
    db.update_deal_state("2893987145", status="bought", bought_price=140.0, bought_at=now - timedelta(days=30))
    board = c.get("/api/v1/pipeline").json()
    cols = {col["key"]: col for col in board["columns"]}
    assert [col["key"] for col in board["columns"]] == ["starred", "contacted", "bought", "sold"]
    assert cols["starred"]["count"] == 1 and cols["sold"]["count"] == 3 and cols["bought"]["amount"] == 140
    assert cols["sold"]["amount"] == 110 + 80 + 20 and board["hidden"] == 1
    summary = c.get("/api/v1/pipeline/summary", params={"months": 3}).json()
    assert summary["earned_month"] == 210 and len(summary["earned_by_month"]) == 3
    assert summary["invested"] == 140 and summary["in_stock"] == 1 and summary["expected_in_stock"] == 84
    assert summary["followups"] == 1 and summary["stale_stock"] == 1 and summary["sold_count"] == 3
    assert summary["accuracy"]["n"] == 3 and "от прогноза" in summary["accuracy"]["message_ru"]


def test_seen_marks(api) -> None:
    c, app, config, db, *_ = api
    assert c.post(f"/api/v1/deals/{RTX}/seen").json() == {"ad_id": RTX, "marked": 1}
    assert c.post(f"/api/v1/deals/{RTX}/seen").json()["marked"] == 0
    assert c.post("/api/v1/deals/seen", json={"ids": [MACMINI, EBAY, "unknown"]}).json()["marked"] == 2
    unseen = [d["id"] for d in c.get("/api/v1/deals", params={"unseen": 1}).json()["items"]]
    assert RTX not in unseen and MACMINI not in unseen and len(unseen) == 8
    assert c.post("/api/v1/deals/nope/seen").status_code == 404


def test_market_history(api) -> None:
    c, app, config, db, *_ = api
    now = utcnow()
    comps = [Comparable(title=f"RTX 3090 24GB Founders #{i}", price=p, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{i}",
                        source="kleinanzeigen") for i, p in enumerate([600, 620, 640, 650, 700, 720, 580], 1)]
    for i, comp in enumerate(comps):
        db.record_price_points([("rtx|3090", comp)], seen_at=now - timedelta(days=i * 3))
    db.record_price_points([("rtx|3090", Comparable(title="RTX 3090 Lüfter defekt", price=90, source="kleinanzeigen",
                                                   url="https://www.kleinanzeigen.de/s-anzeige/x/99"))])
    old = Comparable(title="RTX 3090 alt", price=500, url="https://www.kleinanzeigen.de/s-anzeige/x/77",
                     source="kleinanzeigen")
    db.record_price_points([("rtx|3090", old)], seen_at=now - timedelta(days=90))
    m = c.get(f"/api/v1/deals/{RTX}/market").json()
    assert m["available"] and m["product_key"] == "rtx|3090" and m["max_buy_price"] == 561
    assert m["stats"]["count"] == 7 and m["stats"]["median"] == 640 and m["stats"]["low"] == 580
    assert m["stats"]["p25"] == 610 and m["stats"]["p75"] == 675
    assert all(p["price"] != 90 for p in m["points"])  # "defekt" is another kind of offer
    assert m["median_line"] and m["this_ad"]["price"] == 450 and "Рынок ~640 €" in m["caption_ru"]
    everything = c.get(f"/api/v1/deals/{RTX}/market", params={"days": 0}).json()
    assert everything["stats"]["count"] == 8
    assert c.get(f"/api/v1/deals/{RTX}/price-history").json()["stats"]["count"] == 7
    unknown = c.get("/api/v1/deals/2893540987/market").json()  # Dyson: no known model key in history
    assert unknown["points"] == [] and not unknown["available"]


def test_check_and_reevaluate_jobs(api) -> None:
    c, app, config, db, path, monitor = api
    r = c.post("/api/v1/check", json={"url": "https://www.kleinanzeigen.de/s-anzeige/sony/2899999999-172-3331",
                                      "purpose": "personal", "target_price": 150})
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert r.json()["poll_url"] == f"/api/v1/jobs/{job['id']}" and job["kind"] == "check"
    done = wait_job(c, job["id"])
    assert done["status"] == "done", done
    assert done["result"]["id"] == "2899999999" and done["result"]["offer_price"] == 100
    assert [s["stage"] for s in done["stages"]] == ["fetch", "market", "ai", "done"]
    assert monitor.evaluated[-1][1] == {"purpose": "personal", "target_price": 150}
    kinds = [e.type for e in events_of(app)]
    assert "job_progress" in kinds and "job_finished" in kinds and "deal_updated" in kinds
    failed = wait_job(c, c.post("/api/v1/check", json={"url": "https://www.kleinanzeigen.de/s-anzeige/fail/2899999998-1-2"}
                                ).json()["job"]["id"])
    assert failed["status"] == "error" and failed["error"] == {"code": "bad_request", "message_ru": "Объявление удалено"}
    bad = c.post("/api/v1/check", json={"url": "https://example.com/whatever"})
    assert bad.status_code == 422 and bad.json()["error"]["fields"]["url"]
    assert c.get("/api/v1/jobs/nope").status_code == 404
    assert len(c.get("/api/v1/jobs").json()["items"]) == 2

    config.searches = [SearchConfig(name="Видеокарты Берлин", query="rtx")]
    again = wait_job(c, c.post(f"/api/v1/deals/{RTX}/reevaluate").json()["job"]["id"])
    assert again["status"] == "done" and again["result"]["verdict"] == "maybe"
    assert monitor.evaluated[-1] == (RTX, {"search": "Видеокарты Берлин"})
    refetch = wait_job(c, c.post(f"/api/v1/deals/{RTX}/reevaluate", json={"refetch": True}).json()["job"]["id"])
    assert refetch["status"] == "done" and monitor.evaluated[-1][0].startswith("https://")
    assert c.post("/api/v1/deals/nope/reevaluate").status_code == 404


# ------------------------------------------------------------------ monitor
def test_monitor_state_run_pause_resume(api) -> None:
    c, app, config, db, path, monitor = api
    state = c.get("/api/v1/monitor").json()
    assert state["available"] and state["state"] == "idle" and state["next_run_at"]
    assert state["backlog"] == {"pending": 3, "expired_24h": 1}
    assert state["http"][0]["host"] == "www.kleinanzeigen.de" and state["http"][0]["load_percent"] == 20
    assert state["last_summary"]["id"] == 3  # from the database (demo runs)
    monitor.release.clear()
    r = c.post("/api/v1/monitor/run")
    assert r.status_code == 202 and r.json()["started"]
    busy = c.post("/api/v1/monitor/run")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    monitor.release.set()
    for _ in range(100):
        if not c.get("/api/v1/monitor").json()["running"]:
            break
        import time

        time.sleep(0.01)
    paused = c.post("/api/v1/monitor/pause").json()
    assert paused["paused"] and paused["state"] == "paused" and monitor.paused
    resumed = c.post("/api/v1/monitor/resume").json()
    assert not resumed["paused"]
    kinds = [e.type for e in events_of(app)]
    assert {"run_started", "run_finished", "monitor_paused", "monitor_resumed"} <= set(kinds)
    runs = c.get("/api/v1/runs", params={"limit": 2}).json()["items"]
    assert len(runs) == 2 and runs[0]["duration_seconds"] == 131 and "error_count" in runs[0]


# ------------------------------------------------------------------ stats / summary
def test_summary_today_and_stats(api, monkeypatch) -> None:
    c, app, config, db, *_ = api
    config.searches = [SearchConfig(name="x", query="y")]
    # "today" starts at local midnight: shortly after it the seeded deals would belong to yesterday
    from ebeyparser.web.api import routes_deals
    monkeypatch.setattr(routes_deals, "local_midnight", lambda now=None: utcnow() - timedelta(hours=20))
    today = c.get("/api/v1/summary/today").json()
    assert today["count"] >= 1 and today["best"]["id"] and today["headline_ru"].startswith("Найдено")
    assert today["potential_profit"] > 0 and today["unseen_good"] >= today["count"]
    assert today["monitor"]["state"] == "idle" and today["setup_checklist"]["total"] == 4
    overview = c.get("/api/v1/stats/overview").json()
    assert overview["week"]["ads_seen"] == 14 and overview["week"]["deals"] == 8  # the auction is a "maybe"
    assert overview["totals"]["listings_total"] == 14
    db.update_deal_state(RTX, status="sold", bought_price=400.0, sold_price=520.0, bought_at=utcnow(), sold_at=utcnow())
    week = c.get("/api/v1/stats/overview").json()["week"]
    assert week["sold"] == 1 and week["realized_profit"] == 120 and week["bought"] >= 1
    series = c.get("/api/v1/stats/timeseries", params={"days": 7}).json()["items"]
    assert len(series) == 7 and sum(d["ads_seen"] for d in series) >= 12 and series[-1]["realized_profit"] == 120
    assert c.get("/api/v1/stats/timeseries", params={"days": 0}).status_code == 422


def test_notify_preview(api) -> None:
    c, app, config, *_ = api
    p = c.get("/api/v1/notify/preview", params={"min_score": 80, "verdicts": "buy"}).json()
    assert p["would_send"] == 6 and p["verdicts"] == ["buy"] and p["current"]["min_score"] == 70
    wide = c.get("/api/v1/notify/preview", params={"min_score": 50, "verdicts": "buy,maybe"}).json()
    assert wide["would_send"] == 11 and sum(d["count"] for d in wide["per_day"]) == 11  # the hidden one excluded
    assert c.get("/api/v1/notify/preview", params={"verdicts": "great"}).status_code == 422


def test_export_csv(api) -> None:
    c, app, config, db, *_ = api
    db.update_deal_state(RTX, status="bought", bought_price=431.5)
    r = c.get("/api/v1/export/deals.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0].startswith("ID;Название;") and len(lines) == 1 + 4  # starred, contacted, bought x2
    assert any("431,50" in line and "Купил" in line for line in lines)
    plain = c.get("/api/v1/export/deals.csv", params={"excel": 0, "verdict": "all", "status": "any"}).text
    assert plain.startswith("ID,") and len(plain.strip().splitlines()) == 15


# ------------------------------------------------------------------ health / logs / data
def test_health(api, tmp_path: Path) -> None:
    c, app, config, db, path, monitor = api
    h = c.get("/api/v1/health").json()
    assert h["level"] == "warn" and "ошибок" in h["banner_ru"] or h["level"] in ("ok", "warn")
    assert h["ai"]["enabled"] is False and h["sites"][0]["requests_last_hour"] == 30
    assert h["storage"]["counts"]["listings"] == 14 and h["notifications"]["channels"][0]["name"] == "telegram"
    assert len(h["runs"]) == 3 and h["last_error"]["errors"]
    config.ai.enabled = True
    monitor.health = {"ok": False, "server_ok": False, "error": "LM Studio не запущен"}
    bad = c.get("/api/v1/health").json()
    assert bad["level"] == "error" and bad["ai"]["ok"] is False and "Нейросеть не отвечает" in bad["banner_ru"]
    assert c.get("/api/v1/health", params={"ai": 0}).json()["ai"] is None
    data = c.get("/api/v1/data").json()
    assert data["counts"]["runs"] == 3 and data["listing_retention_days"] == 60 and data["demo"]["count"] == 14


LOG = """2026-09-29 10:00:00,001 INFO    ebeyparser.monitor: Run finished: 3 new
2026-09-29 10:05:00,002 WARNING ebeyparser.monitor: Blocked by Kleinanzeigen: 403
2026-09-29 10:06:00,003 ERROR   ebeyparser.web: boom
Traceback (most recent call last):
  File "x.py", line 1
2026-09-29 10:07:00,004 INFO    ebeyparser.notify: Telegram: отправлено 1 сообщ.
"""


def test_logs_tail_filter_download_and_stream(api, tmp_path: Path) -> None:
    c = api[0]
    assert c.get("/api/v1/logs").json() == {"path": str(tmp_path / "data" / "logs" / "ebeyparser.log"),
                                            "exists": False, "size": 0, "items": [], "truncated": False}
    assert c.get("/api/v1/logs/download").status_code == 404
    log_file = tmp_path / "data" / "logs" / "ebeyparser.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text(LOG, encoding="utf-8")
    items = c.get("/api/v1/logs").json()["items"]
    assert [i["level"] for i in items] == ["info", "warning", "error", "info"]
    assert items[2]["message"].startswith("boom\nTraceback") and items[0]["time_text"] == "10:00:00"
    assert [i["level"] for i in c.get("/api/v1/logs", params={"level": "warning"}).json()["items"]] == ["warning",
                                                                                                        "error"]
    assert len(c.get("/api/v1/logs", params={"q": "telegram"}).json()["items"]) == 1
    tail = c.get("/api/v1/logs", params={"lines": 2}).json()
    assert len(tail["items"]) == 2 and tail["truncated"]
    assert c.get("/api/v1/logs", params={"level": "loud"}).status_code == 422
    download = c.get("/api/v1/logs/download")
    assert download.status_code == 200 and "Blocked by Kleinanzeigen" in download.text
    assert "attachment" in download.headers["content-disposition"]

    async def append_later() -> None:
        await asyncio.sleep(0.2)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write("2026-09-29 11:00:00,000 ERROR   ebeyparser.x: new problem\n")

    import threading

    threading.Thread(target=lambda: asyncio.run(append_later()), daemon=True).start()
    stream = c.get("/api/v1/logs/stream", params={"level": "error", "max_events": 1, "timeout": 5, "poll": 0.05})
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: log" in stream.text and "new problem" in stream.text and "boom" not in stream.text


def test_demo_load_and_clear(tmp_path: Path) -> None:
    app, config, db, path, monitor = make_app(tmp_path, seed=False)
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/api/v1/app").json()["demo"] == {"loaded": False, "count": 0}
        loaded = c.post("/api/v1/demo").json()
        assert loaded["inserted"] == 14 and loaded["count"] == 14 and len(db.list_runs()) == 3
        assert c.post("/api/v1/demo/load").json()["inserted"] == 0  # idempotent
        assert c.get("/api/v1/app").json()["demo"]["loaded"]
        cleared = c.delete("/api/v1/demo").json()
        assert cleared == {"removed": 14, "runs_removed": 3, "message_ru": "Демо-данные удалены"}
        assert c.get("/api/v1/deals", params={"verdict": "all", "status": "any"}).json()["total"] == 0
        assert [e.data["reason"] for e in events_of(app, "data_changed")] == ["demo_loaded", "demo_loaded",
                                                                             "demo_cleared"]


def test_backup_and_reset(tmp_path: Path) -> None:
    app, config, db, path, monitor = make_app(tmp_path)
    (path.parent / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    with TestClient(app, base_url=LOCAL) as c:
        r = c.get("/api/v1/backup")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert set(zf.namelist()) == {"data/ebeyparser.sqlite3", "config.yaml", "README.txt"}
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(zf.read("data/ebeyparser.sqlite3"))
        assert Database(restored).count_deals_v1(include_ignored=True) == 14
        with_secrets = zipfile.ZipFile(io.BytesIO(c.get("/api/v1/backup", params={"include_secrets": 1}).content))
        assert with_secrets.read(".env") == b"TELEGRAM_BOT_TOKEN=secret\n"
        db.record_price_points([("rtx|3090", Comparable(title="RTX 3090", price=600, source="kleinanzeigen"))])
        wrong = c.post("/api/v1/data/reset-history", json={"confirm": "да"})
        assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "confirm_required"
        assert c.post("/api/v1/data/reset-history", json={"confirm": "Сбросить"}).json()["removed"] == 1
        assert db.count_price_points() == 0
        c.post("/api/v1/onboarding/complete")
        assert c.post("/api/v1/data/reset-all", json={"confirm": "удалить"}).json()["removed"]["listings"] == 14
        assert db.table_counts()["listings"] == 0 and db.table_counts()["runs"] == 0
        assert c.get("/api/v1/onboarding").json()["completed_at"]  # onboarding survives a data reset


# ------------------------------------------------------------------ access / restart
def test_access_modes_token_rotation_and_restart(tmp_path: Path) -> None:
    app, config, db, path, monitor = make_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        local = c.get("/api/v1/access").json()
        assert local["mode"] == "local" and not local["token_required"] and local["token"] is None
        lan = c.put("/api/v1/access", json={"mode": "lan", "allowed_hosts": ["My-PC.tail1234.ts.net"]}).json()
        assert lan["mode"] == "lan" and lan["restart_required"] and lan["token"]
        assert lan["allowed_hosts"] == ["my-pc.tail1234.ts.net"]
        assert any(u["url"].endswith(f"?token={lan['token']}") for u in lan["urls"]) and lan["qr_payload"]
        assert load_config(path).web.host == "0.0.0.0" and (tmp_path / "data" / "web_token.txt").is_file()
        ts = c.put("/api/v1/access", json={"mode": "tailscale", "host": "100.101.102.103"}).json()
        assert ts["mode"] == "tailscale" and ts["urls"][0]["url"].startswith("http://100.101.102.103:8000/?token=")
        assert c.put("/api/v1/access", json={"mode": "tailscale", "host": "bad host!"}).status_code == 422
        assert c.put("/api/v1/access", json={"mode": "lan", "allowed_hosts": ["evil/x"]}).status_code == 422
        assert c.put("/api/v1/access", json={"mode": "local"}).json()["mode"] == "local"
        assert c.post("/api/v1/system/restart").status_code == 503
        called: list[bool] = []
        app.state.restart_callback = lambda: called.append(True)
        r = c.post("/api/v1/system/restart")
        assert r.status_code == 202 and r.json()["restarting"]
        import time

        time.sleep(0.7)
        assert called == [True]


def test_rotate_token_keeps_this_browser_signed_in(tmp_path: Path) -> None:
    token = "first-token-for-tests-1234567"
    path = tmp_path / "config.yaml"
    app, config, db, path, monitor = make_app(tmp_path, bind_host="0.0.0.0", access_token=token)
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/api/v1/app").status_code == 401
        c.cookies.set("ebp_token", token)
        r = c.post("/api/v1/access/rotate-token")
        assert r.status_code == 200 and r.json()["rotated"]
        new = app.state.access_token
        assert new != token and r.json()["token"] == new
        assert c.get("/api/v1/app").status_code == 200  # the response set the new cookie
        c.cookies.clear()
        assert c.get("/api/v1/app", headers={"X-EbeyParser-Token": token}).status_code == 401
        assert c.get("/api/v1/app", headers={"X-EbeyParser-Token": new}).status_code == 200


# ------------------------------------------------------------------ errors / security / SSE
def test_error_shapes(api) -> None:
    c = api[0]
    r = c.get("/api/v1/nope")
    assert r.status_code == 404 and r.json() == {"error": {"code": "not_found", "message_ru": "Нет такого адреса API"}}
    r = c.delete("/api/v1/app")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
    r = c.post("/api/v1/check", json={"url": 5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation" and "url" in r.json()["error"]["fields"]
    r = c.post("/api/v1/check", content=b"{broken", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation"
    # the old API keeps its own shape
    assert "detail" in c.get("/api/deals/nope").json()


def test_crash_becomes_error_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, config, db, path, monitor = make_app(tmp_path)
    monkeypatch.setattr(db, "stats", lambda: 1 / 0)
    with TestClient(app, base_url=LOCAL, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/stats/overview")
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
        assert "ZeroDivision" not in r.text
        reset = c.post("/api/v1/onboarding/complete", json={"completed": False}).json()
        assert reset["completed_at"] is None


def test_security_token_origin_and_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "s3cret-token-for-tests-123"
    monkeypatch.setenv("EBEYPARSER_ALLOWED_HOSTS", "pc.tail1234.ts.net")
    app, config, db, path, monitor = make_app(tmp_path, bind_host="0.0.0.0", access_token=token)
    with TestClient(app, base_url=LOCAL) as c:
        r = c.get("/api/v1/app")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        assert c.get("/api/v1/events", params={"max_events": 1, "timeout": 0.1}).status_code == 401
        assert c.get("/api/v1/app", params={"token": token}).status_code == 200  # sets the cookie
        assert c.get("/api/v1/settings").status_code == 200  # cookie remembered
        app.state.api.hub.publish("health_alert", {"kind": "test", "text": "hi"})
        sse = c.get("/api/v1/events", params={"last_event_id": 0, "max_events": 1, "timeout": 2})
        assert sse.status_code == 200 and "event: health_alert" in sse.text  # SSE works with the cookie
        r = c.post("/api/v1/monitor/pause", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "foreign_origin" and not monitor.paused
        assert c.post("/api/v1/monitor/pause", headers={"Origin": "http://localhost"}).status_code == 200
    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/api/v1/app", headers={"X-EbeyParser-Token": token}).status_code == 400
    with TestClient(app, base_url="http://pc.tail1234.ts.net") as c:
        assert c.get("/api/v1/app", headers={"X-EbeyParser-Token": token}).status_code == 200


def test_sse_stream_replay_live_and_deal_card(api) -> None:
    c, app, config, db, path, monitor = api
    hub = app.state.api.hub
    first = hub.publish("run_started", {"run_id": 7})
    monitor._emit("deal_found", {"ad_id": RTX, "verdict": "buy", "score": 94})  # through the monitor bridge
    r = c.get("/api/v1/events", params={"last_event_id": first.id - 1, "max_events": 2, "timeout": 2})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"].startswith("no-cache")
    text = r.text
    assert text.startswith("retry: 3000") and "event: ready" in text
    assert f"id: {first.id}\nevent: run_started" in text and "event: deal_found" in text
    payload = json.loads(text.split("event: deal_found\ndata: ")[1].split("\n")[0])
    assert payload["card"]["id"] == RTX and payload["card"]["title"].startswith("Gigabyte")
    # Last-Event-ID header: only what came after it
    r = c.get("/api/v1/events", params={"max_events": 1, "timeout": 2}, headers={"Last-Event-ID": str(first.id)})
    assert "event: deal_found" in r.text and "event: run_started" not in r.text
    # live: an event published while the stream is open
    import threading

    threading.Timer(0.2, lambda: hub.publish("settings_changed", {"sections": ["ai"]})).start()
    live = c.get("/api/v1/events", params={"max_events": 1, "timeout": 3})
    assert "event: settings_changed" in live.text
    quiet = c.get("/api/v1/events", params={"timeout": 0.2})
    assert "event: ready" in quiet.text and "event: settings_changed" not in quiet.text
    recent = c.get("/api/v1/events/recent").json()
    assert recent["last_event_id"] >= 3 and recent["items"][-1]["type"] == "settings_changed"


async def test_sse_stream_ends_on_close_and_disconnect() -> None:
    from ebeyparser.web.api.events import EventHub, sse_stream

    hub = EventHub()
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in sse_stream(hub, ping=0.05):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.12)
    hub.publish("run_finished", {"deals_found": 2})
    await asyncio.sleep(0.02)
    hub.close()
    await asyncio.wait_for(task, 1)
    text = "".join(chunks)
    assert ": ping" in text and "event: run_finished" in text and hub.subscribers == 0

    hub2 = EventHub()

    async def gone() -> bool:
        return True

    parts = [chunk async for chunk in sse_stream(hub2, is_disconnected=gone, ping=0.01)]
    assert parts[-1].startswith("event: ready") and hub2.subscribers == 0
