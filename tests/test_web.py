"""Web dashboard tests: pages, filters, JSON API, searches CRUD, demo data."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ebeyparser.config import AppConfig, SearchConfig, load_config, save_searches
from ebeyparser.db import Database
from ebeyparser.demo import demo_deals, seed_demo
from ebeyparser.models import DealView, Evaluation, Listing, RunSummary, utcnow
from ebeyparser.scraper.categories import merge_categories, parse_category_links
from ebeyparser.web.app import (
    DealFilters,
    create_app,
    fmt_money,
    fmt_number,
    fmt_percent,
    mask_email,
    mask_secret,
    page_links,
    plural,
)

RTX3090 = "2894398213"
IPHONE = "2893876502"  # skip, iCloud locked
BOSCH = "2893402211"  # maybe, ignored by the user
THINKPAD = "2893987145"  # bought
MACMINI = "2894311780"  # starred
EBAY_AUCTION = "ebay-306512349871"
LOCAL = "http://localhost"  # the app only accepts loopback Host headers (DNS-rebinding protection)


# --------------------------------------------------------------------- fakes
class FakeMonitor:
    def __init__(self) -> None:
        self.is_running = False
        self.last_summary: RunSummary | None = None
        self.next_run_at = utcnow() + timedelta(minutes=12)
        self.calls = 0
        self.release = threading.Event()
        self.release.set()
        self.loop_started = False
        self.loop_stopped = False
        self.updated: AppConfig | None = None
        self.health: dict[str, Any] = {
            "ok": True, "provider": "ollama", "base_url": "http://localhost:11434",
            "model": "qwen2.5vl:7b", "model_available": True, "error": None,
        }

    async def run_once(self) -> RunSummary:
        self.calls += 1
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        self.last_summary = RunSummary(finished_at=utcnow(), new_listings=2, deals_found=1)
        return self.last_summary

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.loop_started = True
        await stop.wait()
        self.loop_stopped = True

    async def ai_health(self) -> dict[str, Any]:
        return dict(self.health)

    def update_config(self, config: AppConfig) -> None:
        self.updated = config


class FakeNotifier:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[tuple[list[DealView], str | None]] = []

    async def send(self, deals: list[DealView], *, title: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("SMTP: authentication failed")
        self.sent.append((deals, title))


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def db() -> Database:
    database = Database()
    seed_demo(database)
    return database


@pytest.fixture()
def monitor() -> FakeMonitor:
    return FakeMonitor()


@pytest.fixture()
def client(db: Database, monitor: FakeMonitor):
    app = create_app(AppConfig(), db, monitor=monitor)
    with TestClient(app, base_url=LOCAL) as c:
        yield c


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "general:\n  interval_minutes: 5\n"
        "notifications:\n  email:\n    password: ${SMTP_PASSWORD}\n",
        encoding="utf-8",
    )
    save_searches(path, [SearchConfig(name="Видеокарты Берлин", query="rtx 3090", location="Berlin", radius_km=20)])
    return path


@pytest.fixture()
def editable(db: Database, monitor: FakeMonitor, config_file: Path):
    config = load_config(config_file)
    app = create_app(config, db, config_path=config_file, monitor=monitor)
    with TestClient(app, base_url=LOCAL) as c:
        yield c, config, config_file


def _ids(response) -> list[str]:
    return [item["listing"]["ad_id"] for item in response.json()["items"]]


# ----------------------------------------------------------------- demo data
def test_demo_deals_are_realistic_and_offline() -> None:
    deals = demo_deals()
    assert len(deals) >= 12
    ids = [listing.ad_id for listing, _ in deals]
    assert len(ids) == len(set(ids))
    verdicts = {ev.verdict for _, ev in deals}
    assert verdicts == {"buy", "maybe", "skip"}
    assert {ev.purpose for _, ev in deals} == {"resale", "personal"}
    assert {listing.source for listing, _ in deals} == {"kleinanzeigen", "ebay"}
    for listing, ev in deals:
        assert listing.ad_id == ev.ad_id
        assert listing.image_urls and all(u.startswith("data:image/svg+xml") for u in listing.image_urls)
        assert ev.reasons, listing.title
    by_id = {listing.ad_id: (listing, ev) for listing, ev in deals}
    auction, auction_ev = by_id[EBAY_AUCTION]
    assert "AUCTION" in auction.buying_options and auction.ends_at and auction.bid_count
    assert auction_ev.max_buy_price and auction_ev.ai_second is not None
    assert any(ev.ai_second is not None and ev.ai_second.model.startswith("claude") for _, ev in deals)
    assert any(listing.is_free for listing, _ in deals)
    iphone_ev = by_id[IPHONE][1]
    assert iphone_ev.verdict == "skip" and iphone_ev.ai and iphone_ev.ai.red_flags


def test_seed_demo_idempotent() -> None:
    database = Database()
    first = seed_demo(database)
    assert first == len(demo_deals())
    count = database.count_deals(include_ignored=True)
    runs = len(database.list_runs())
    assert runs == 3
    database.set_status(RTX3090, "contacted", "моя заметка")
    assert seed_demo(database) == 0
    assert database.count_deals(include_ignored=True) == count
    assert len(database.list_runs()) == runs
    deal = database.get_deal(RTX3090)
    assert deal is not None and deal.status == "contacted" and deal.note == "моя заметка"
    assert database.get_deal(THINKPAD).status == "bought"
    assert database.get_deal(MACMINI).status == "starred"
    assert database.get_deal(BOSCH).status == "ignored"
    first_seen = sorted(listing.first_seen for listing in database.iter_listings())
    assert first_seen[-1] - first_seen[0] > timedelta(hours=24)


# --------------------------------------------------------------------- pages
def test_dashboard_renders(client: TestClient) -> None:
    r = client.get("/classic")
    assert r.status_code == 200
    html = r.text
    for text in ("EbeyParser", "Сделки", "Поиски", "Статус", "Проверить сейчас", "Лучшие", "Покупать",
                 "Подумать", "Все", "Новых за 24 ч", "Потенциальная прибыль", "Куплено",
                 "Gigabyte GeForce RTX 3090", "рынок ~780", "предложи 420", "экономия", "Выгодно до",
                 "Макс. ставка", "Kleinanzeigen", "eBay", "Следующая проверка через", "/static/placeholder.svg"):
        assert text in html, text
    # default filter = buy + maybe, no ignored
    assert "iCloud gesperrt" not in html
    assert "Bosch Professional" not in html
    assert "Canyon Endurace" not in html
    assert 'onerror="this.onerror=null' in html


def test_dashboard_filters(client: TestClient) -> None:
    r = client.get("/classic", params={"verdict": "skip"})
    assert "iPhone 13 128GB" in r.text and "Canyon" in r.text and "RTX 3090" not in r.text
    r = client.get("/classic", params={"verdict": "all", "status": "ignored"})
    assert "Bosch Professional" in r.text and "Gigabyte GeForce RTX 3090" not in r.text
    r = client.get("/classic", params={"q": "thinkpad"})
    assert "ThinkPad T480" in r.text and "Mac mini" not in r.text
    r = client.get("/classic", params={"purpose": "personal"})
    assert "DDR4 ECC" in r.text and "HP Z440" in r.text and "Dyson V11 Absolute Akkusauger" not in r.text
    r = client.get("/classic", params={"source": "ebay"})
    assert "RTX 4070" in r.text and "iPad Air" in r.text and "Dyson V11 Absolute Akkusauger" not in r.text
    r = client.get("/classic", params={"search": "Apple техника", "verdict": "all"})
    assert "Mac mini" in r.text and "iPhone 13" in r.text and "Dyson V11 Absolute Akkusauger" not in r.text
    # garbage parameters never break the page
    r = client.get("/classic", params={"min_score": "abc", "sort": "evil", "verdict": "x", "page": "-3", "source": "zz"})
    assert r.status_code == 200 and "RTX 3090" in r.text


def test_dashboard_empty_state() -> None:
    app = create_app(AppConfig(), Database())
    with TestClient(app, base_url=LOCAL) as c:
        r = c.get("/classic")
    assert r.status_code == 200
    assert "Пока нет ни одного объявления" in r.text
    assert "Добавить поиск" in r.text
    assert "Монитор выключен" in r.text


def test_dashboard_pagination(db: Database) -> None:
    for i in range(30):
        ad = f"9{i:09d}"
        db.upsert_listing(Listing(ad_id=ad, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad}", title=f"Filler item {i}",
                                  price=10 + i, search_name="Filler"))
        db.save_evaluation(Evaluation(ad_id=ad, verdict="maybe", score=40, buy_price=10 + i))
    app = create_app(AppConfig(), db)
    with TestClient(app, base_url=LOCAL) as c:
        first = c.get("/classic")
        assert 'aria-label="Страницы"' in first.text
        second = c.get("/classic", params={"page": 2})
        assert second.status_code == 200 and "страница 2 из 2" in second.text
        api = c.get("/api/deals", params={"limit": 5, "offset": 5}).json()
        assert len(api["items"]) == 5 and api["total"] == db.count_deals(verdict=["buy", "maybe"])


def test_deal_page(client: TestClient) -> None:
    r = client.get(f"/classic/deal/{RTX3090}")
    assert r.status_code == 200
    html = r.text
    for text in ("Открыть на Kleinanzeigen", 'target="_blank"', "noopener", "Расчёт прибыли", "Рыночная цена",
                 "Запас на торг и риск", "Комиссии при продаже", "Цена покупки", "Чистая прибыль", "ROI",
                 "Локальная модель", "Второе мнение: claude-opus-5", "qwen2.5vl:7b", "Фото совпадают с описанием",
                 "Уверенность", "Похожие предложения", "eBay · продано", "Почему такая оценка", "Описание",
                 "Stützhalterung dabei", "Мой статус", "Сохранить заметку", "Выгодно до", "data-thumb"):
        assert text in html, text
    assert "+252" in html  # 780 * 0.9 - 450


def test_deal_page_ebay_auction_and_personal(client: TestClient) -> None:
    html = client.get(f"/classic/deal/{EBAY_AUCTION}").text
    for text in ("Открыть на eBay", "Максимальная ставка", "7 ставок", "Текущая ставка", "вкл. доставку 6,99",
                 "99,6", "заканчивается через"):
        assert text in html, text
    personal = client.get("/classic/deal/2893790331").text
    assert "Сколько сэкономлю" in personal and "Экономия" in personal and "Для себя" in personal


def test_deal_404(client: TestClient) -> None:
    r = client.get("/classic/deal/does-not-exist")
    assert r.status_code == 404
    assert "Страница не найдена" in r.text and "Такого объявления нет" in r.text
    r = client.get("/api/deals/does-not-exist")
    assert r.status_code == 404 and r.json()["detail"]
    assert client.get("/classic/nowhere").status_code == 404


def test_status_page(db: Database, monitor: FakeMonitor) -> None:
    config = AppConfig()
    config.notifications.email.enabled = True
    config.notifications.email.to_addrs = ["student.berlin@gmail.com"]
    config.notifications.telegram.chat_id = "518204417"
    app = create_app(config, db, monitor=monitor)
    with TestClient(app, base_url=LOCAL) as c:
        html = c.get("/classic/status").text
    for text in ("Последние проверки", "ИИ выключен в конфиге", "Отправить тестовое уведомление", "st***@gmail.com",
                 "51***17", "Второе мнение", "eBay API", "Не настроен", "Главные настройки", "HTTP 429",
                 "qwen2.5vl:7b", "Длительность"):
        assert text in html, text
    assert "student.berlin@gmail.com" not in html
    assert "518204417" not in html


def test_status_page_ai_health(db: Database, monitor: FakeMonitor) -> None:
    config = AppConfig()
    config.ai.enabled = True
    config.ebay.client_id = "MaxMuste-EbeyPars-PRD-a1b2c3d4e-12345678"
    config.ebay.client_secret = "secret"
    app = create_app(config, db, monitor=monitor)
    with TestClient(app, base_url=LOCAL) as c:
        html = c.get("/classic/status").text
        assert "Работает, модель на месте" in html
        assert "Подключён" in html and "12345678" not in html
        monitor.health = {"ok": False, "error": "Connection refused"}
        html = c.get("/classic/status").text
        assert "Недоступна" in html and "Connection refused" in html


def test_searches_page_renders(editable) -> None:
    c, _, _ = editable
    html = c.get("/classic/searches").text
    for text in ("мастер настройки", "настрой его на kleinanzeigen.de", "Видеокарты Берлин",
                 "Новый поиск", "Добавить поиск", "Источник", "Категории eBay", "Формат продажи"):
        assert text in html, text
    edit = c.get("/classic/searches", params={"edit": "Видеокарты Берлин"}).text
    assert "Изменить поиск «Видеокарты Берлин»" in edit and 'value="rtx 3090"' in edit


def test_static_assets(client: TestClient) -> None:
    for name in ("style.css", "app.js", "placeholder.svg", "logo.svg", "favicon.svg"):
        assert client.get(f"/static/{name}").status_code == 200, name


# ----------------------------------------------------------------------- API
def test_api_deals_filters(client: TestClient, db: Database) -> None:
    data = client.get("/api/deals").json()
    assert data["total"] == db.count_deals(verdict=["buy", "maybe"]) == len(data["items"])
    buy = client.get("/api/deals", params={"verdict": "buy"}).json()
    assert buy["items"] and all(i["evaluation"]["verdict"] == "buy" for i in buy["items"])
    assert _ids(client.get("/api/deals", params={"q": "ThinkPad"})) == [THINKPAD]
    ebay = client.get("/api/deals", params={"source": "ebay", "verdict": "all"}).json()
    assert ebay["total"] == 2 and all(i["listing"]["source"] == "ebay" for i in ebay["items"])
    high = client.get("/api/deals", params={"min_score": 85}).json()["items"]
    assert high and all(i["evaluation"]["score"] >= 85 for i in high)
    prices = [i["listing"]["price"] for i in client.get("/api/deals", params={"sort": "price", "verdict": "all"}).json()["items"]]
    assert prices == sorted(prices)
    personal = client.get("/api/deals", params={"purpose": "personal"}).json()["items"]
    assert {i["evaluation"]["purpose"] for i in personal} == {"personal"}
    assert IPHONE in _ids(client.get("/api/deals", params={"verdict": "skip"}))
    assert BOSCH in _ids(client.get("/api/deals", params={"verdict": "all", "status": "any"}))
    assert BOSCH not in _ids(client.get("/api/deals", params={"verdict": "all"}))
    one = client.get(f"/api/deals/{EBAY_AUCTION}").json()
    assert one["listing"]["bid_count"] == 7 and one["evaluation"]["ai_second"]["model"] == "claude-opus-5"


def test_api_status_updates_db(client: TestClient, db: Database) -> None:
    r = client.post(f"/api/deals/{RTX3090}/status", json={"status": "starred", "note": "Написать Марко"})
    assert r.status_code == 200
    assert r.json()["status"] == "starred" and r.json()["note"] == "Написать Марко"
    deal = db.get_deal(RTX3090)
    assert deal.status == "starred" and deal.note == "Написать Марко"
    # note is kept when omitted
    r = client.post(f"/api/deals/{RTX3090}/status", json={"status": "bought"})
    assert r.json()["note"] == "Написать Марко" and db.get_deal(RTX3090).status == "bought"
    r = client.post(f"/api/deals/{RTX3090}/status", json={"status": "sold-out"})
    assert r.status_code == 400 and "Неизвестный статус" in r.json()["detail"]
    assert db.get_deal(RTX3090).status == "bought"
    assert client.post("/api/deals/nope/status", json={"status": "starred"}).status_code == 404
    assert client.post(f"/api/deals/{RTX3090}/status", json={}).status_code == 422


def test_api_rejects_foreign_origin(client: TestClient, db: Database) -> None:
    r = client.post(f"/api/deals/{RTX3090}/status", json={"status": "ignored"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert db.get_deal(RTX3090).status == "new"
    r = client.post(f"/api/deals/{RTX3090}/status", json={"status": "starred"}, headers={"Origin": "http://localhost"})
    assert r.status_code == 200


def test_api_stats_and_runs(client: TestClient, db: Database) -> None:
    stats = client.get("/api/stats").json()
    assert stats == db.stats()
    assert stats["bought_total"] == 1 and stats["potential_profit"] > 0
    runs = client.get("/api/runs").json()
    assert len(runs) == 3 and runs[0]["searches"] == 8


def test_api_run(db: Database, monitor: FakeMonitor) -> None:
    with TestClient(create_app(AppConfig(), db), base_url=LOCAL) as c:
        r = c.post("/api/run")
        assert r.status_code == 503 and "Перезапусти программу" in r.json()["detail"]
        assert "ebeyparser run" not in r.json()["detail"]

    app = create_app(AppConfig(), db, monitor=monitor)
    with TestClient(app, base_url=LOCAL) as c:
        monitor.is_running = True
        assert c.post("/api/run").status_code == 409
        monitor.is_running = False

        monitor.release.clear()  # keep the run "in progress"
        r = c.post("/api/run")
        assert r.status_code == 202 and r.json() == {"started": True}
        deadline = time.time() + 5
        while monitor.calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert monitor.calls == 1
        assert c.post("/api/run").status_code == 409  # our own task is still running
        assert c.get("/api/health", params={"ai": 0}).json()["monitor"]["running"] is True
        monitor.release.set()
        deadline = time.time() + 5
        while c.get("/api/health", params={"ai": 0}).json()["monitor"]["running"] and time.time() < deadline:
            time.sleep(0.01)
        assert c.post("/api/run").status_code == 202


def test_start_monitor_lifespan(db: Database, monitor: FakeMonitor) -> None:
    app = create_app(AppConfig(), db, monitor=monitor, start_monitor=True)
    with TestClient(app, base_url=LOCAL) as c:
        deadline = time.time() + 5
        while not monitor.loop_started and time.time() < deadline:
            time.sleep(0.01)
        assert monitor.loop_started
        assert c.get("/api/health", params={"ai": 0}).json()["monitor"]["loop"] is True
    assert monitor.loop_stopped


def test_api_health(db: Database, monitor: FakeMonitor) -> None:
    app = create_app(AppConfig(), db, monitor=monitor)
    with TestClient(app, base_url=LOCAL) as c:
        data = c.get("/api/health").json()
        assert data["ok"] is True
        assert data["monitor"]["available"] is True and data["monitor"]["next_run_at"]
        assert data["ai"]["ok"] is False and "выключен" in data["ai"]["error"]
        app.state.config.ai.enabled = True
        data = c.get("/api/health").json()
        assert data["ai"]["ok"] is True and data["ai"]["model_available"] is True
        assert c.get("/api/health", params={"ai": "0"}).json()["ai"] is None
    with TestClient(create_app(AppConfig(), db), base_url=LOCAL) as c:
        data = c.get("/api/health").json()
        assert data["monitor"]["available"] is False


def test_api_notify_test(db: Database) -> None:
    with TestClient(create_app(AppConfig(), db), base_url=LOCAL) as c:
        r = c.post("/api/notify/test")
        assert r.status_code == 400 and "канал" in r.json()["detail"]
    with TestClient(create_app(AppConfig(), db, notifiers_factory=lambda: []), base_url=LOCAL) as c:
        assert c.post("/api/notify/test").status_code == 400

    good, bad = FakeNotifier("telegram"), FakeNotifier("email", fail=True)
    with TestClient(create_app(AppConfig(), db, notifiers_factory=lambda: [good, bad]), base_url=LOCAL) as c:
        r = c.post("/api/notify/test")
    assert r.status_code == 200
    results = r.json()["results"]
    assert results["telegram"] == "ok"
    assert "authentication failed" in results["email"]
    deals, title = good.sent[0]
    assert len(deals) == 2 and title
    scores = [d.evaluation.score for d in deals]
    assert scores == sorted(scores, reverse=True) and scores[0] == max(ev.score for _, ev in demo_deals())
    assert all(not u.startswith("data:") for d in deals for u in d.listing.image_urls)


# ------------------------------------------------------------------ searches
def test_api_searches_crud(editable, monitor: FakeMonitor) -> None:
    c, config, path = editable
    listing = c.get("/api/searches").json()
    assert listing["editable"] is True and [s["name"] for s in listing["items"]] == ["Видеокарты Берлин"]

    new = {"name": "eBay аукционы", "source": "ebay", "query": "rtx 4070", "buying_options": ["AUCTION"],
           "ending_within_hours": 6, "max_price": 450}
    r = c.post("/api/searches", json=new)
    assert r.status_code == 201 and r.json()["source"] == "ebay"
    assert [s.name for s in load_config(path).searches] == ["Видеокарты Берлин", "eBay аукционы"]
    assert monitor.updated is config and len(config.searches) == 2
    assert c.post("/api/searches", json=new).status_code == 409
    assert c.post("/api/searches", json={"name": "Пустой"}).status_code == 400  # no url / query / category
    assert c.post("/api/searches", json={"name": "Плохой URL", "url": "https://example.com/x"}).status_code == 400
    assert c.post("/api/searches", json={"query": "x"}).status_code == 422

    r = c.put("/api/searches/eBay аукционы", json={**new, "name": "eBay 4070", "max_price": 400})
    assert r.status_code == 200
    reloaded = load_config(path)
    assert reloaded.search_by_name("eBay 4070").max_price == 400
    assert reloaded.search_by_name("eBay аукционы") is None
    assert c.put("/api/searches/eBay 4070", json={**new, "name": "Видеокарты Берлин"}).status_code == 409
    assert c.put("/api/searches/нет такого", json=new).status_code == 404

    assert c.delete("/api/searches/eBay 4070").json() == {"deleted": "eBay 4070"}
    assert c.delete("/api/searches/eBay 4070").status_code == 404
    assert [s.name for s in load_config(path).searches] == ["Видеокарты Берлин"]
    # the rest of the YAML is preserved, including ${ENV} placeholders
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["general"]["interval_minutes"] == 5
    assert raw["notifications"]["email"]["password"] == "${SMTP_PASSWORD}"


def test_searches_form_flow(editable) -> None:
    c, config, path = editable
    form = {
        "name": "AI-сервер (для себя)", "source": "kleinanzeigen", "purpose": "personal", "enabled": "1",
        "url": "", "query": "ddr4 ecc", "location": "10115", "radius_km": "50", "category_id": "225",
        "min_price": "", "max_price": "120,5", "include_keywords": "ecc, reg", "exclude_keywords": "ddr3;defekt",
        "target_price": "90", "min_roi": "25", "max_pages": "2",
    }
    r = c.post("/classic/searches/save", data=form, follow_redirects=False)
    assert r.status_code == 303 and "saved=" in r.headers["location"]
    saved = load_config(path).search_by_name("AI-сервер (для себя)")
    assert saved is not None
    assert saved.purpose == "personal" and saved.radius_km == 50 and saved.max_price == 120.5
    assert saved.include_keywords == ["ecc", "reg"] and saved.exclude_keywords == ["ddr3", "defekt"]
    assert saved.min_roi == pytest.approx(0.25) and saved.target_price == 90 and saved.enabled
    assert "сохранён" in c.get(r.headers["location"]).text

    # edit + rename
    r = c.post("/classic/searches/save", data={**form, "name": "RAM для сервера", "original_name": "AI-сервер (для себя)"},
               follow_redirects=False)
    assert r.status_code == 303
    names = [s.name for s in load_config(path).searches]
    assert names == ["Видеокарты Берлин", "RAM для сервера"]

    # validation errors re-render the form with messages
    r = c.post("/classic/searches/save", data={"name": "", "radius_km": "abc"})
    assert r.status_code == 400 and "нужно целое число" in r.text
    r = c.post("/classic/searches/save", data={**form, "name": "Видеокарты Берлин"})
    assert r.status_code == 400 and "уже есть" in r.text
    r = c.post("/classic/searches/save", data={"name": "Ничего", "source": "kleinanzeigen"})
    assert r.status_code == 400 and "Укажи URL" in r.text

    # toggle + delete
    r = c.post("/classic/searches/toggle", data={"name": "RAM для сервера"}, follow_redirects=False)
    assert r.status_code == 303 and load_config(path).search_by_name("RAM для сервера").enabled is False
    r = c.post("/classic/searches/delete", data={"name": "RAM для сервера"}, follow_redirects=False)
    assert r.status_code == 303 and [s.name for s in load_config(path).searches] == ["Видеокарты Берлин"]
    assert [s.name for s in config.searches] == ["Видеокарты Берлин"]


def test_searches_read_only(db: Database) -> None:
    config = AppConfig(searches=[SearchConfig(name="Видеокарты Берлин", query="rtx 3090")])
    app = create_app(config, db)
    with TestClient(app, base_url=LOCAL) as c:
        html = c.get("/classic/searches").text
        assert "Только просмотр" in html and "Видеокарты Берлин" in html
        assert 'action="/classic/searches/save"' not in html
        assert c.get("/api/searches").json()["editable"] is False
        assert c.post("/api/searches", json={"name": "x", "query": "y"}).status_code == 403
        assert c.put("/api/searches/Видеокарты Берлин", json={"name": "x", "query": "y"}).status_code == 403
        assert c.delete("/api/searches/Видеокарты Берлин").status_code == 403
        assert c.post("/classic/searches/save", data={"name": "x", "query": "y"}).status_code == 403
    assert [s.name for s in config.searches] == ["Видеокарты Берлин"]


# ----------------------------------------------------------------- helpers
def test_formatters() -> None:
    assert fmt_money(1234.5) == "1 234,50 €"
    assert fmt_money(450) == "450 €"
    assert fmt_money(-117, sign=True) == "−117 €"
    assert fmt_money(252, sign=True) == "+252 €"
    assert fmt_money(None) == "—"
    assert fmt_percent(1.16) == "116 %"
    assert [plural(n, "ставка", "ставки", "ставок") for n in (1, 3, 5, 11, 21, 22)] == [
        "ставка", "ставки", "ставок", "ставок", "ставка", "ставки"]
    assert mask_email("maksem706@gmail.com") == "ma***@gmail.com"
    assert mask_secret("123456789") == "12***89"
    assert mask_secret("123") == "***"
    assert page_links(5, 10) == [1, None, 4, 5, 6, None, 10]
    f = DealFilters.from_params({"verdict": "buy", "q": "rtx", "min_score": "70"})
    assert f.url(page=2) == "/classic?verdict=buy&q=rtx&min_score=70&page=2"
    assert DealFilters.from_params({}).url() == "/classic"


# ------------------------------------------------------------- setup wizard
SETUP_CONFIG = """# мой конфиг
general:
  interval_minutes: 15   # как часто проверять
  data_dir: {data}
notifications:
  email:
    password: ${{SMTP_PASSWORD}}   # секрет из .env
searches:
  - name: Мой поиск
    query: dyson
"""


@pytest.fixture()
def restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def setup_env(tmp_path: Path, db: Database, monitor: FakeMonitor, restore_environ):
    """Editable app on a commented config with data_dir in tmp; category discovery is faked."""
    path = tmp_path / "config.yaml"
    path.write_text(SETUP_CONFIG.format(data=tmp_path / "data"), encoding="utf-8")
    calls: list[tuple[str, int]] = []
    state: dict[str, Any] = {"fail": False}

    async def discover(location: str, radius: int):
        calls.append((location, radius))
        if state["fail"]:
            raise RuntimeError("proxy says no")
        html = ('<a href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a> (12.345)'
                '<a href="/s-berlin/sammeln/c234l3331r30">Sammeln</a> (321)')
        return merge_categories(parse_category_links(html))

    config = load_config(path)
    app = create_app(config, db, config_path=path, monitor=monitor, category_discovery=discover,
                     ai_probe=lambda url: {"data": [{"id": "qwen/qwen2.5-vl-7b"}]} if "1234" in url else None)
    with TestClient(app, base_url=LOCAL) as c:
        yield c, config, path, calls, state


def test_dashboard_setup_banner_when_no_searches(db: Database) -> None:
    with TestClient(create_app(AppConfig(), db), base_url=LOCAL) as c:
        html = c.get("/classic").text
    assert "Настрой поиски за 2 минуты" in html and 'href="/classic/setup"' in html and "Настройка" in html
    with TestClient(create_app(AppConfig(), Database()), base_url=LOCAL) as c:
        empty = c.get("/classic").text
    assert "Настроить за 2 минуты" in empty and "Добавить поиск" in empty
    config = AppConfig(searches=[SearchConfig(name="x", query="y")])
    with TestClient(create_app(config, db), base_url=LOCAL) as c:
        assert "Настрой поиски за 2 минуты" not in c.get("/classic").text


def test_setup_page_uses_builtin_list_without_requests(setup_env) -> None:
    c, config, _, calls, _ = setup_env
    r = c.get("/classic/setup")
    assert r.status_code == 200 and calls == []  # no request to Kleinanzeigen on a normal visit
    html = r.text
    for text in ("Настройка за 2 минуты", "Встроенный справочник категорий", "Handy &amp; Telefon",
                 "смартфоны и телефоны", "рекомендуем", "Сверить с сайтом", "Для себя: список желаний",
                 "Как часто проверять", "стр. выдачи в час", "Заменить текущие поиски (1)", "Сохранить поиски",
                 'value="Berlin"', "Нейросеть, уведомления, выгода"):
        assert text in html, text
    assert 'name="category" value="173" checked' in html and 'name="category" value="228" checked' not in html
    assert 'class="active" aria-current="page"' in html  # subnav


def test_setup_refresh_discovers_once_and_caches(setup_env, tmp_path: Path) -> None:
    c, _, _, calls, state = setup_env
    r = c.get("/classic/setup", params={"location": "Berlin", "radius": "30", "category": ["173", "234"],
                                "wish_item": ["RTX 3090"], "wish_price": ["550"], "refresh": "1"})
    assert r.status_code == 200 and calls == [("Berlin", 30)]
    assert "с kleinanzeigen.de" in r.text and f"{fmt_number(12345)} объявлений" in r.text and "Sammeln" in r.text
    assert 'name="category" value="234" checked' in r.text and 'value="RTX 3090"' in r.text  # form state kept
    assert (tmp_path / "data" / "categories.json").is_file()
    again = c.get("/classic/setup", params={"location": "berlin", "radius": "30"})
    assert calls == [("Berlin", 30)] and "Sammeln" in again.text  # served from the cache
    state["fail"] = True
    failed = c.get("/classic/setup", params={"location": "Hamburg", "radius": "30", "refresh": "1"})
    assert failed.status_code == 200 and "получить не удалось" in failed.text
    assert "Нет связи с Kleinanzeigen" in failed.text and "proxy says no" not in failed.text  # no raw exception
    assert "Handy &amp; Telefon" in failed.text  # built-in fallback


def test_setup_save_replaces_searches_and_keeps_comments(setup_env, monitor: FakeMonitor) -> None:
    c, config, path, _, _ = setup_env
    form = {"location": "berlin", "radius": "25", "purpose": "resale", "max_price": "300", "min_profit": "50",
            "category": ["173", "278"], "wish_item": ["RTX 3090", ""], "wish_price": ["550", ""],
            "interval_minutes": "20", "replace": "1"}
    r = c.post("/classic/setup", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/classic/searches?setup=3"
    saved = load_config(path)
    assert [s.name for s in saved.searches] == ["Handy & Telefon · berlin 30 км", "Notebooks · berlin 30 км",
                                                "Для себя: RTX 3090"]
    assert saved.searches[0].category_id == 173 and saved.searches[0].min_profit == 50
    assert saved.searches[2].target_price == 550 and saved.general.interval_minutes == 20
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# мой конфиг") and "# как часто проверять" in text and "${SMTP_PASSWORD}   # секрет" in text
    assert "dyson" in (path.parent / "config.yaml.bak").read_text(encoding="utf-8")
    assert monitor.updated is config and [s.name for s in config.searches] == [s.name for s in saved.searches]
    assert config.general.interval_minutes == 20
    assert "Готово: сохранено 3 поиска" in c.get(r.headers["location"]).text


def test_setup_save_can_keep_existing_and_validates(setup_env) -> None:
    c, _, path, _, _ = setup_env
    r = c.post("/classic/setup", data={"location": "Berlin", "radius": "30", "category": ["279"]}, follow_redirects=False)
    assert r.status_code == 303
    assert [s.name for s in load_config(path).searches] == ["Мой поиск", "Konsolen · Berlin 30 км"]
    r = c.post("/classic/setup", data={"location": "Berlin", "radius": "30"})
    assert r.status_code == 400 and "Выбери хотя бы одну категорию" in r.text
    r = c.post("/classic/setup", data={"location": "", "category": ["173"], "max_price": "abc",
                               "wish_item": ["RTX"], "wish_price": ["дёшево"], "interval_minutes": "1"})
    assert r.status_code == 400
    for text in ("Укажи город", "Максимальная цена за вещь", "«RTX»: цена", "Как часто проверять"):
        assert text in r.text, text


def test_setup_read_only_and_foreign_origin(db: Database) -> None:
    config = AppConfig(searches=[SearchConfig(name="Видеокарты", query="rtx")])
    with TestClient(create_app(config, db), base_url=LOCAL) as c:
        html = c.get("/classic/setup").text
        assert "Только просмотр" in html and "disabled" in html
        assert c.post("/classic/setup", data={"location": "Berlin", "category": ["173"]}).status_code == 403
        assert c.post("/classic/settings/ai", data={"provider": "openai", "model": "x"}).status_code == 403
        assert "Только просмотр" in c.get("/classic/settings").text
    with TestClient(create_app(AppConfig(), db, config_path=Path("/nonexistent/config.yaml")), base_url=LOCAL) as c:
        r = c.post("/classic/setup", data={"location": "Berlin", "category": ["173"]}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
    assert [s.name for s in config.searches] == ["Видеокарты"]


# ----------------------------------------------------------------- settings
def test_settings_ai_telegram_pricing(setup_env, monitor: FakeMonitor) -> None:
    c, config, path, _, _ = setup_env
    html = c.get("/classic/settings").text
    for text in ("Локальная нейросеть", "Найти LM Studio / Ollama", "Проверить", "Telegram", "@BotFather",
                 "Что считать выгодным", "Минимальный ROI"):
        assert text in html, text

    r = c.post("/classic/settings/ai", data={"enabled": "1", "provider": "openai", "base_url": "http://localhost:1234/v1",
                                     "model": "qwen/qwen2.5-vl-7b"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=ai" in r.headers["location"]
    assert (config.ai.provider, config.ai.model, config.ai.enabled) == ("openai", "qwen/qwen2.5-vl-7b", True)
    assert monitor.updated is config and "Настройки нейросети сохранены" in c.get("/classic/settings?saved=ai").text
    assert c.post("/classic/settings/ai", data={"provider": "openai", "base_url": "ftp://x", "model": ""}).status_code == 400

    token = "123456789:AAHfakeTokenFakeTokenFake_12345"
    r = c.post("/classic/settings/telegram", data={"enabled": "1", "bot_token": token, "chat_id": "987654"},
               follow_redirects=False)
    assert r.status_code == 303
    assert token in (path.parent / ".env").read_text(encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert token not in text and "${TELEGRAM_BOT_TOKEN}" in text and "# мой конфиг" in text
    assert config.notifications.telegram.enabled and config.notifications.telegram.bot_token == token
    assert c.post("/classic/settings/telegram", data={"enabled": "1", "bot_token": "nope", "chat_id": "x"}).status_code == 400

    r = c.post("/classic/settings/pricing", data={"min_profit": "60", "min_roi": "30", "max_capital": "500",
                                          "vb_discount": "12"}, follow_redirects=False)
    assert r.status_code == 303
    saved = load_config(path)
    assert saved.pricing.min_profit == 60 and saved.pricing.min_roi == pytest.approx(0.3)
    assert config.pricing.min_roi == pytest.approx(0.3)
    if hasattr(saved.pricing, "max_capital"):
        assert saved.pricing.max_capital == 500
    if hasattr(saved.pricing, "vb_expected_discount"):
        assert saved.pricing.vb_expected_discount == pytest.approx(0.12)
    assert c.post("/classic/settings/pricing", data={"min_roi": "много"}).status_code == 400
    assert [s.name for s in load_config(path).searches] == ["Мой поиск"]  # searches untouched


def test_api_ai_detect_and_check(setup_env, monitor: FakeMonitor) -> None:
    c, config, _, _, _ = setup_env
    servers = c.get("/api/ai/detect").json()["servers"]
    assert servers == [{"name": "LM Studio", "provider": "openai", "base_url": "http://localhost:1234/v1",
                        "models": ["qwen/qwen2.5-vl-7b"], "vision_models": ["qwen/qwen2.5-vl-7b"],
                        "default_model": "qwen/qwen2.5-vl-7b"}]
    config.ai.enabled = True
    check = c.get("/api/ai/check").json()
    assert check["ok"] is True and check["model_available"] is True


# ----------------------------------------------------------------- security
def test_trusted_hosts_block_dns_rebinding(db: Database) -> None:
    app = create_app(AppConfig(), db)
    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/").status_code == 400
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/docs").status_code == 200  # docs only on loopback


def test_network_mode_requires_token(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "s3cret-token-for-tests-123"
    monkeypatch.setenv("EBEYPARSER_ALLOWED_HOSTS", "pc.tail1234.ts.net")
    app = create_app(AppConfig(), db, bind_host="0.0.0.0", access_token=token)
    with TestClient(app, base_url=LOCAL) as c:
        gate = c.get("/")
        assert gate.status_code == 401 and "Нужен ключ доступа" in gate.text
        assert c.get("/api/stats").status_code == 401
        assert c.get("/static/style.css").status_code == 200
        assert c.get("/api/docs").status_code in (401, 404)
        r = c.get("/searches?token=wrong")
        assert r.status_code == 401
        r = c.get(f"/searches?token={token}&x=1", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/searches?x=1"
        assert "ebp_token" in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()
        assert c.get("/").status_code == 200  # cookie remembered
        assert c.get("/api/docs").status_code == 404  # no API docs in network mode
    with TestClient(app, base_url="http://pc.tail1234.ts.net") as c:
        assert c.get("/api/stats", headers={"X-EbeyParser-Token": token}).status_code == 200


def _blocked_state(path: Path) -> None:
    """A real PoliteClient that got HTTP 403 once: cooldown + strike persisted to `path`."""
    import httpx

    from ebeyparser.scraper.http import BlockedError, PoliteClient

    async def go() -> None:
        client = PoliteClient(delay_range=(0, 0), max_retries=0, state_path=path, max_requests_per_hour=150,
                              transport=httpx.MockTransport(lambda request: httpx.Response(403, text="nope")))
        with pytest.raises(BlockedError):
            await client.get_text("https://www.kleinanzeigen.de/s-handy-telefon/c173")
        await client.aclose()

    asyncio.run(go())


def test_status_page_shows_site_limits_and_cooldown(tmp_path: Path, db: Database) -> None:
    config = AppConfig()
    config.general.data_dir = str(tmp_path)
    with TestClient(create_app(config, db), base_url=LOCAL) as c:
        html = c.get("/classic/status").text
    assert "Запросы к сайтам и защита от блокировки" in html and "Запросов к сайтам пока не было" in html
    state = tmp_path / "http_state.json"
    _blocked_state(state)
    before = state.read_text(encoding="utf-8")
    with TestClient(create_app(config, db), base_url=LOCAL) as c:
        html = c.get("/classic/status").text
    assert "www.kleinanzeigen.de" in html and "пауза до" in html and "пауза после блокировки" in html
    assert state.read_text(encoding="utf-8") == before  # the page only reads the shared state

    class LimitsMonitor(FakeMonitor):
        def http_status(self) -> dict[str, Any]:
            return {"www.kleinanzeigen.de": {"requests_last_hour": 150, "images_last_hour": 3, "limit_per_hour": 150,
                                             "remaining": 0, "blocked": False, "cooldown_until": None, "strikes": 0,
                                             "last_block_at": None, "last_block_reason": "",
                                             "note": "лимит 150 страниц в час исчерпан"}}

    class IdleMonitor(FakeMonitor):
        def http_status(self) -> dict[str, Any]:
            return {}  # the monitor creates its HTTP client lazily

    with TestClient(create_app(config, db, monitor=IdleMonitor()), base_url=LOCAL) as c:
        assert "пауза до" in c.get("/classic/status").text  # falls back to the shared state file
    with TestClient(create_app(config, db, monitor=LimitsMonitor()), base_url=LOCAL) as c:
        html = c.get("/classic/status").text
    assert "150 / 150" in html and "лимит в этот час исчерпан" in html and "всё спокойно" in html


def test_status_page_shows_evaluation_backlog(db: Database) -> None:
    class BacklogMonitor(FakeMonitor):
        def backlog_status(self) -> dict[str, Any]:
            return {"pending": 17, "expired_24h": 4}

    with TestClient(create_app(AppConfig(), db, monitor=BacklogMonitor()), base_url=LOCAL) as c:
        html = c.get("/classic/status").text
    assert "Очередь на оценку" in html and "17" in html and "просрочено за сутки" in html and "устарела" in html

    class BrokenMonitor(FakeMonitor):
        def backlog_status(self) -> Any:
            raise RuntimeError("boom")

    with TestClient(create_app(AppConfig(), db, monitor=BrokenMonitor()), base_url=LOCAL) as c:
        r = c.get("/classic/status")  # falls back to the database counts (or hides the line)
    assert r.status_code == 200


def test_dashboard_hint_for_keyword_only_searches(db: Database) -> None:
    keywords = AppConfig(searches=[SearchConfig(name="RTX", query="rtx 3090")])
    with TestClient(create_app(keywords, db), base_url=LOCAL) as c:
        html = c.get("/classic").text
    assert "Сейчас только поиски по словам" in html and "data-dismiss-hint" in html and "ebp-hide-cat-hint" in html
    assert "Настрой поиски за 2 минуты" not in html
    scans = AppConfig(searches=[SearchConfig(name="Handy", category_id=173, location="Berlin"),
                                SearchConfig(name="RTX", query="rtx 3090")])
    by_url = AppConfig(searches=[SearchConfig(name="URL", url="https://www.kleinanzeigen.de/s-berlin/c225l3331r20")])
    for config in (scans, by_url):
        with TestClient(create_app(config, db), base_url=LOCAL) as c:
            assert "Сейчас только поиски по словам" not in c.get("/classic").text
