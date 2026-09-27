"""Web dashboard tests: pages, filters, JSON API, searches CRUD, demo data."""

from __future__ import annotations

import asyncio
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
from ebeyparser.web.app import (
    DealFilters,
    create_app,
    fmt_money,
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
    with TestClient(app) as c:
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
    with TestClient(app) as c:
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
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for text in ("EbeyParser", "Сделки", "Поиски", "Статус", "Проверить сейчас", "Лучшие", "Покупать",
                 "Подумать", "Все", "Новых за 24 ч", "Потенциальная прибыль", "Куплено",
                 "Gigabyte GeForce RTX 3090", "рынок ~780", "ROI 56", "экономия", "Выгодно до",
                 "Макс. ставка", "Kleinanzeigen", "eBay", "Следующая проверка через", "/static/placeholder.svg"):
        assert text in html, text
    # default filter = buy + maybe, no ignored
    assert "iCloud gesperrt" not in html
    assert "Bosch Professional" not in html
    assert "Canyon Endurace" not in html
    assert 'onerror="this.onerror=null' in html


def test_dashboard_filters(client: TestClient) -> None:
    r = client.get("/", params={"verdict": "skip"})
    assert "iPhone 13 128GB" in r.text and "Canyon" in r.text and "RTX 3090" not in r.text
    r = client.get("/", params={"verdict": "all", "status": "ignored"})
    assert "Bosch Professional" in r.text and "Gigabyte GeForce RTX 3090" not in r.text
    r = client.get("/", params={"q": "thinkpad"})
    assert "ThinkPad T480" in r.text and "Mac mini" not in r.text
    r = client.get("/", params={"purpose": "personal"})
    assert "DDR4 ECC" in r.text and "HP Z440" in r.text and "Dyson V11 Absolute Akkusauger" not in r.text
    r = client.get("/", params={"source": "ebay"})
    assert "RTX 4070" in r.text and "iPad Air" in r.text and "Dyson V11 Absolute Akkusauger" not in r.text
    r = client.get("/", params={"search": "Apple техника", "verdict": "all"})
    assert "Mac mini" in r.text and "iPhone 13" in r.text and "Dyson V11 Absolute Akkusauger" not in r.text
    # garbage parameters never break the page
    r = client.get("/", params={"min_score": "abc", "sort": "evil", "verdict": "x", "page": "-3", "source": "zz"})
    assert r.status_code == 200 and "RTX 3090" in r.text


def test_dashboard_empty_state() -> None:
    app = create_app(AppConfig(), Database())
    with TestClient(app) as c:
        r = c.get("/")
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
    with TestClient(app) as c:
        first = c.get("/")
        assert 'aria-label="Страницы"' in first.text
        second = c.get("/", params={"page": 2})
        assert second.status_code == 200 and "страница 2 из 2" in second.text
        api = c.get("/api/deals", params={"limit": 5, "offset": 5}).json()
        assert len(api["items"]) == 5 and api["total"] == db.count_deals(verdict=["buy", "maybe"])


def test_deal_page(client: TestClient) -> None:
    r = client.get(f"/deal/{RTX3090}")
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
    html = client.get(f"/deal/{EBAY_AUCTION}").text
    for text in ("Открыть на eBay", "Максимальная ставка", "7 ставок", "Текущая ставка", "вкл. доставку 6,99",
                 "99,6", "заканчивается через"):
        assert text in html, text
    personal = client.get("/deal/2893790331").text
    assert "Сколько сэкономлю" in personal and "Экономия" in personal and "Для себя" in personal


def test_deal_404(client: TestClient) -> None:
    r = client.get("/deal/does-not-exist")
    assert r.status_code == 404
    assert "Страница не найдена" in r.text and "Такого объявления нет" in r.text
    r = client.get("/api/deals/does-not-exist")
    assert r.status_code == 404 and r.json()["detail"]
    assert client.get("/nowhere").status_code == 404


def test_status_page(db: Database, monitor: FakeMonitor) -> None:
    config = AppConfig()
    config.notifications.email.enabled = True
    config.notifications.email.to_addrs = ["student.berlin@gmail.com"]
    config.notifications.telegram.chat_id = "518204417"
    app = create_app(config, db, monitor=monitor)
    with TestClient(app) as c:
        html = c.get("/status").text
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
    with TestClient(app) as c:
        html = c.get("/status").text
        assert "Работает, модель на месте" in html
        assert "Подключён" in html and "12345678" not in html
        monitor.health = {"ok": False, "error": "Connection refused"}
        html = c.get("/status").text
        assert "Недоступна" in html and "Connection refused" in html


def test_searches_page_renders(editable) -> None:
    c, _, _ = editable
    html = c.get("/searches").text
    for text in ("Самый надёжный способ — настроить поиск на kleinanzeigen.de", "Видеокарты Берлин",
                 "Новый поиск", "Добавить поиск", "Источник", "Категории eBay", "Формат продажи"):
        assert text in html, text
    edit = c.get("/searches", params={"edit": "Видеокарты Берлин"}).text
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
    r = client.post(f"/api/deals/{RTX3090}/status", json={"status": "starred"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_api_stats_and_runs(client: TestClient, db: Database) -> None:
    stats = client.get("/api/stats").json()
    assert stats == db.stats()
    assert stats["bought_total"] == 1 and stats["potential_profit"] > 0
    runs = client.get("/api/runs").json()
    assert len(runs) == 3 and runs[0]["searches"] == 8


def test_api_run(db: Database, monitor: FakeMonitor) -> None:
    with TestClient(create_app(AppConfig(), db)) as c:
        r = c.post("/api/run")
        assert r.status_code == 503 and "Монитор" in r.json()["detail"]

    app = create_app(AppConfig(), db, monitor=monitor)
    with TestClient(app) as c:
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
    with TestClient(app) as c:
        deadline = time.time() + 5
        while not monitor.loop_started and time.time() < deadline:
            time.sleep(0.01)
        assert monitor.loop_started
        assert c.get("/api/health", params={"ai": 0}).json()["monitor"]["loop"] is True
    assert monitor.loop_stopped


def test_api_health(db: Database, monitor: FakeMonitor) -> None:
    app = create_app(AppConfig(), db, monitor=monitor)
    with TestClient(app) as c:
        data = c.get("/api/health").json()
        assert data["ok"] is True
        assert data["monitor"]["available"] is True and data["monitor"]["next_run_at"]
        assert data["ai"]["ok"] is False and "выключен" in data["ai"]["error"]
        app.state.config.ai.enabled = True
        data = c.get("/api/health").json()
        assert data["ai"]["ok"] is True and data["ai"]["model_available"] is True
        assert c.get("/api/health", params={"ai": "0"}).json()["ai"] is None
    with TestClient(create_app(AppConfig(), db)) as c:
        data = c.get("/api/health").json()
        assert data["monitor"]["available"] is False


def test_api_notify_test(db: Database) -> None:
    with TestClient(create_app(AppConfig(), db)) as c:
        r = c.post("/api/notify/test")
        assert r.status_code == 400 and "канал" in r.json()["detail"]
    with TestClient(create_app(AppConfig(), db, notifiers_factory=lambda: [])) as c:
        assert c.post("/api/notify/test").status_code == 400

    good, bad = FakeNotifier("telegram"), FakeNotifier("email", fail=True)
    with TestClient(create_app(AppConfig(), db, notifiers_factory=lambda: [good, bad])) as c:
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
    r = c.post("/searches/save", data=form, follow_redirects=False)
    assert r.status_code == 303 and "saved=" in r.headers["location"]
    saved = load_config(path).search_by_name("AI-сервер (для себя)")
    assert saved is not None
    assert saved.purpose == "personal" and saved.radius_km == 50 and saved.max_price == 120.5
    assert saved.include_keywords == ["ecc", "reg"] and saved.exclude_keywords == ["ddr3", "defekt"]
    assert saved.min_roi == pytest.approx(0.25) and saved.target_price == 90 and saved.enabled
    assert "сохранён" in c.get(r.headers["location"]).text

    # edit + rename
    r = c.post("/searches/save", data={**form, "name": "RAM для сервера", "original_name": "AI-сервер (для себя)"},
               follow_redirects=False)
    assert r.status_code == 303
    names = [s.name for s in load_config(path).searches]
    assert names == ["Видеокарты Берлин", "RAM для сервера"]

    # validation errors re-render the form with messages
    r = c.post("/searches/save", data={"name": "", "radius_km": "abc"})
    assert r.status_code == 400 and "нужно целое число" in r.text
    r = c.post("/searches/save", data={**form, "name": "Видеокарты Берлин"})
    assert r.status_code == 400 and "уже есть" in r.text
    r = c.post("/searches/save", data={"name": "Ничего", "source": "kleinanzeigen"})
    assert r.status_code == 400 and "Укажи URL" in r.text

    # toggle + delete
    r = c.post("/searches/toggle", data={"name": "RAM для сервера"}, follow_redirects=False)
    assert r.status_code == 303 and load_config(path).search_by_name("RAM для сервера").enabled is False
    r = c.post("/searches/delete", data={"name": "RAM для сервера"}, follow_redirects=False)
    assert r.status_code == 303 and [s.name for s in load_config(path).searches] == ["Видеокарты Берлин"]
    assert [s.name for s in config.searches] == ["Видеокарты Берлин"]


def test_searches_read_only(db: Database) -> None:
    config = AppConfig(searches=[SearchConfig(name="Видеокарты Берлин", query="rtx 3090")])
    app = create_app(config, db)
    with TestClient(app) as c:
        html = c.get("/searches").text
        assert "Только просмотр" in html and "Видеокарты Берлин" in html
        assert 'action="/searches/save"' not in html
        assert c.get("/api/searches").json()["editable"] is False
        assert c.post("/api/searches", json={"name": "x", "query": "y"}).status_code == 403
        assert c.put("/api/searches/Видеокарты Берлин", json={"name": "x", "query": "y"}).status_code == 403
        assert c.delete("/api/searches/Видеокарты Берлин").status_code == 403
        assert c.post("/searches/save", data={"name": "x", "query": "y"}).status_code == 403
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
    assert f.url(page=2) == "/?verdict=buy&q=rtx&min_score=70&page=2"
    assert DealFilters.from_params({}).url() == "/"
