"""Final round: health alerts, notification robustness, upkeep and request savings."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta


from ebeyparser.config import parse_config
from ebeyparser.db import Database
from ebeyparser.models import AIVerdict, Comparable, Evaluation, PriceEstimate, utcnow
from ebeyparser.monitor import Monitor
from ebeyparser.notify.emailer import EmailNotifier
from ebeyparser.pricing.estimator import estimate_from_history
from ebeyparser.scraper.http import BlockedError
from ebeyparser.scraper.kleinanzeigen import KleinanzeigenScraper, PageLayoutError

from test_funnel import CompsSource, RecordingSold, teach
from test_http_budget import KA, Clock, Handler, make
from test_monitor import GOOD_AI, SOLD, FakeEvaluator, make_listing
from test_notify import FakeFactory, FakeTelegram, email_cfg

DOWN = AIVerdict(verdict="maybe", confidence=0.0, reasoning="LM Studio не отвечает")


class TextNotifier:
    """Deal + service-message channel for monitor tests."""

    def __init__(self, name: str = "email", *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[tuple[list[str], str | None]] = []
        self.texts: list[str] = []

    async def send(self, deals, *, title=None) -> None:
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append(([d.listing.ad_id for d in deals], title))

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("smtp down")
        self.texts.append(text)


def build(listings, *, notifiers=None, verdict=GOOD_AI, sold=SOLD, general=None, notifications=None, search=None,
          **cfg):
    data = {
        "general": {"baseline_first_run": False, "max_new_per_search": 50, **(general or {})},
        "searches": [search or {"name": "GPU", "query": "rtx 3080"}],
        "pricing": {"min_profit": 40, "min_roi": 0.25},
        "notifications": {"min_score": 50, "verdicts": ["buy"], "heartbeat_hour": None, **(notifications or {})},
    }
    data.update(cfg)
    db = Database()
    source = CompsSource(listings)
    notifiers = notifiers if notifiers is not None else [TextNotifier()]
    monitor = Monitor(parse_config(data), db, scraper=source, ebay=RecordingSold(sold),
                      evaluator=FakeEvaluator(verdict) if verdict else None, notifiers=notifiers)
    return monitor, db, source, notifiers


# ---------------------------------------------------------------------------- notifier send_text


async def test_telegram_send_text() -> None:
    tg = FakeTelegram()
    await tg.notifier().send_text("⚠ Нейросеть <недоступна> & всё")
    method, body = tg.requests[0]
    assert method == "sendMessage" and body["text"] == "⚠ Нейросеть &lt;недоступна&gt; &amp; всё"


async def test_email_send_text_uses_the_first_line_as_subject() -> None:
    factory = FakeFactory()
    await EmailNotifier(email_cfg(), smtp_factory=factory).send_text("Жив: за сутки проверено 12\nподробности")
    msg = factory.instances[0].sent[0]
    assert msg["Subject"] == "EbeyParser: Жив: за сутки проверено 12"
    assert "подробности" in msg.get_content()


# ---------------------------------------------------------------------------- health alerts


async def test_ai_down_is_reported_once_per_six_hours_and_deals_still_arrive_marked() -> None:
    monitor, db, source, (email,) = build([make_listing("1", "RTX 3080", 300.0)], verdict=DOWN)
    first = await monitor.run_once()
    # the default provider is Ollama: its own hint (LM Studio users get "Developer → Start Server")
    assert any("Нейросеть не отвечает" in t and "Ollama" in t for t in email.texts) and first.health_alerts == 1
    assert email.sent == [(["1"], None)]  # the would-be buy went out (renderer marks it unchecked)
    ev = db.get_evaluation("1")
    assert ev.verdict == "maybe" and ev.would_buy and ev.ai_checked is False
    source.listings = [make_listing("2", "RTX 3080 B", 310.0)]
    second = await monitor.run_once()
    assert second.health_alerts == 0 and len(email.texts) == 1  # deduplicated


async def test_no_health_alert_when_switched_off() -> None:
    monitor, _, _, (email,) = build([make_listing("1", "RTX 3080", 300.0)], verdict=DOWN,
                                    notifications={"health_alerts": False})
    await monitor.run_once()
    assert email.texts == []


async def test_site_block_is_reported_with_the_pause_end() -> None:
    monitor, _, source, (email,) = build([])

    async def blocked(search, max_pages=1, seen=None):
        raise BlockedError("HTTP 403", host="www.kleinanzeigen.de", cooldown_until=utcnow() + timedelta(hours=2))

    source.search = blocked
    await monitor.run_once()
    assert len(email.texts) == 1 and email.texts[0].startswith("⚠ Kleinanzeigen попросил паузу — продолжу сам ")
    # the time is the user's (Europe/Berlin), not the host's
    from ebeyparser.timefmt import hhmm

    assert hhmm(utcnow() + timedelta(hours=2)) in email.texts[0]


async def test_empty_search_two_passes_in_a_row_is_reported() -> None:
    monitor, db, source, (email,) = build([])
    await monitor.run_once()
    assert email.texts == []
    await monitor.run_once()
    assert len(email.texts) == 1 and "не находит ни одного объявления" in email.texts[0]
    source.listings = [make_listing("1", "RTX 3080", 700.0)]
    await monitor.run_once()
    assert db.get_state("streak:GPU")[0] == "0"


async def test_unreadable_search_page_two_passes_in_a_row_is_reported() -> None:
    monitor, _, source, (email,) = build([])

    async def layout(search, max_pages=1, seen=None):
        raise PageLayoutError("объявления не распознаны")

    source.search = layout
    await monitor.run_once()
    await monitor.run_once()
    assert len(email.texts) == 1 and "не получается разобрать" in email.texts[0]
    assert "python -m" not in email.texts[0] and "debug-search" not in email.texts[0]


async def test_heartbeat_once_a_day() -> None:
    monitor, _, _, (email,) = build([make_listing("1", "RTX 3080", 300.0)], notifications={"heartbeat_hour": 0})
    await monitor.run_once()
    beats = [t for t in email.texts if t.startswith("Жив:")]
    assert beats == ["Жив: за сутки проверено 1 объявлений, 1 выгодных, ошибок 0"]
    await monitor.run_once()
    assert len([t for t in email.texts if t.startswith("Жив:")]) == 1


# ---------------------------------------------------------------------------- delivery


async def test_a_failed_channel_is_reported_elsewhere_and_retried() -> None:
    email, telegram = TextNotifier("email", fail=True), TextNotifier("telegram")
    monitor, db, source, _ = build([make_listing("1", "RTX 3080", 300.0)], notifiers=[email, telegram])
    await monitor.run_once()
    assert telegram.sent == [(["1"], None)] and db.was_notified("1", "telegram")
    assert not db.was_notified("1", "email")
    assert any("через почту" in t for t in telegram.texts)
    email.fail = False
    source.listings = []
    summary = await monitor.run_once()
    assert email.sent == [(["1"], None)] and db.was_notified("1", "email") and summary.notified == 1
    assert telegram.sent == [(["1"], None)]  # not sent twice on the channel that worked


async def test_retries_stop_after_five_attempts() -> None:
    email = TextNotifier("email", fail=True)
    monitor, db, source, _ = build([make_listing("1", "RTX 3080", 300.0)], notifiers=[email])
    await monitor.run_once()
    source.listings = []
    for _ in range(6):
        await monitor.run_once()
    assert db.delivery_failures("1", "email")[0] == 5


async def test_hourly_cap_sends_the_overflow_as_one_digest_later() -> None:
    listings = [make_listing(str(i), f"RTX 3080 #{i}", 300.0 + i) for i in range(4)]
    monitor, db, source, (email,) = build(listings, notifications={"max_alerts_per_hour": 2})
    summary = await monitor.run_once()
    assert [ids for ids, _ in email.sent] == [["0"], ["1"]] and summary.queued_alerts == 2
    assert len(db.queued_alerts()) == 2
    source.listings = []
    await monitor.run_once()
    assert len(email.sent) == 2  # still within the hour
    db.set_state("alerts:window", "")  # an hour later
    await monitor.run_once()
    ids, title = email.sent[-1]
    assert sorted(ids) == ["2", "3"] and "сверх лимита 2 в час" in title
    assert db.queued_alerts() == []


async def test_a_repost_by_the_same_seller_is_not_alerted_again() -> None:
    first = make_listing("1", "Apple iPhone 13 128GB", 300.0, seller_name="Max")
    monitor, db, source, (email,) = build([first], sold=[], search={"name": "Handy", "query": "iphone 13"},
                                          pricing={"min_profit": 40, "min_roi": 0.25},
                                          general={"baseline_first_run": False, "fetch_details": False})
    db.add_price_points("iphone|13||128gb", [Comparable(title="iPhone 13 128GB", price=520.0 + i,
                                                          url=f"https://www.kleinanzeigen.de/s-anzeige/x/{9000 + i}-1",
                                                          source="kleinanzeigen") for i in range(10)])
    await monitor.run_once()
    assert email.sent == [(["1"], None)]
    source.listings = [make_listing("2", "Apple iPhone 13 128GB", 310.0, seller_name="Max")]
    await monitor.run_once()
    assert email.sent == [(["1"], None)] and db.was_notified("2", "_repost")
    source.listings = [make_listing("3", "Apple iPhone 13 128GB", 305.0, seller_name="Anna")]
    await monitor.run_once()
    assert email.sent[-1] == (["3"], None)  # another seller: a new offer


# ---------------------------------------------------------------------------- category scans, backlog


async def test_category_scan_asks_the_site_for_all_prices_and_filters_locally() -> None:
    received = []
    listings = [make_listing("1", "Apple iPhone 13 128GB", 50.0), make_listing("2", "Apple iPhone 13 128GB", 300.0),
                make_listing("3", "Apple iPhone 13 128GB", 900.0)]
    monitor, db, source, _ = build(listings, verdict=None, sold=[],
                                   search={"name": "Handys", "category_id": 173, "min_price": 100, "max_price": 500})
    real = source.search

    async def spy(search, max_pages=1, seen=None):
        received.append(search)
        return await real(search, max_pages=max_pages, seen=seen)

    source.search = spy
    await monitor.run_once()
    assert received[0].min_price is None and received[0].max_price is None
    assert db.count_price_points("iphone|13||128gb") == 3  # every price feeds the history
    assert db.get_evaluation("1").stage == db.get_evaluation("3").stage == "prefilter"
    assert db.get_evaluation("2").stage != "prefilter"


async def test_backlog_gives_up_visibly_after_two_days() -> None:
    monitor, db, _, _ = build([])
    old = make_listing("7", "RTX 3080", 300.0).model_copy(
        update={"search_name": "GPU", "first_seen": utcnow() - timedelta(days=3)})
    db.upsert_listing(old)
    fresh = make_listing("8", "RTX 3080", 300.0).model_copy(update={"search_name": "GPU"})
    db.upsert_listing(fresh)
    assert monitor.backlog_status()["pending"] == 1  # the fresh one; the old one is past the window
    summary = await monitor.run_once()
    ev = db.get_evaluation("7")
    assert ev.stage == "expired" and ev.verdict == "skip" and summary.expired == 1
    assert monitor.backlog_status() == {"pending": 0, "expired_24h": 1}


# ---------------------------------------------------------------------------- upkeep


async def test_v01_verdicts_are_dropped_once() -> None:
    monitor, db, _, _ = build([])
    db.upsert_listing(make_listing("1", "RTX 3080", 300.0))
    db.save_evaluation(Evaluation(ad_id="1", verdict="buy", stage="full"))
    db.upsert_listing(make_listing("2", "RTX 3080", 300.0))
    db._execute("INSERT INTO evaluations (ad_id, score, verdict, purpose, profit, evaluated_at, data)"
                " VALUES ('2', 90, 'buy', 'resale', 100, ?, ?)",
                (utcnow().isoformat(), json.dumps({"ad_id": "2", "verdict": "buy"})))  # written by v0.1
    await monitor.run_once()
    assert db.get_evaluation("2") is None and db.get_listing("2") is not None
    assert db.get_evaluation("1") is not None and db.get_state("migration:v01_evaluations")[0] == "1"


def test_retention_forgets_old_skips_but_keeps_starred_and_price_points() -> None:
    db = Database()
    for ad_id, status in (("old", None), ("starred", "starred")):
        db.upsert_listing(make_listing(ad_id, "RTX 3080", 300.0))
        db.save_evaluation(Evaluation(ad_id=ad_id, verdict="skip"))
        if status:
            db.set_status(ad_id, status)
    db._execute("UPDATE listings SET last_seen = ?", ((utcnow() - timedelta(days=70)).isoformat(),))
    db.add_price_points("rtx|3080", [make_listing("old", "RTX 3080", 300.0)])
    run = db.start_run()
    db._execute("UPDATE runs SET started_at = ? WHERE id = ?", ((utcnow() - timedelta(days=100)).isoformat(), run.id))
    assert db.apply_retention() == {"listings": 1, "runs": 1}
    assert db.get_listing("old") is None and db.get_listing("starred") is not None
    assert db.count_price_points() == 1


def test_comparables_cache_is_capped(monkeypatch) -> None:
    import ebeyparser.monitor as mon

    monitor, _, _, _ = build([])
    monkeypatch.setattr(mon, "COMPS_CACHE_MAX", 3)
    for i in range(5):
        monitor._cache_comps(f"q{i}", PriceEstimate())
    assert list(monitor._comps_cache) == ["q2", "q3", "q4"]


async def test_changed_request_settings_rebuild_the_http_client(tmp_path) -> None:
    cfg = parse_config({"general": {"data_dir": str(tmp_path)}})
    monitor = Monitor(cfg, Database(), notifiers=[])
    monitor._ensure_components()
    old_client = monitor._client
    monitor.update_config(parse_config({"general": {"data_dir": str(tmp_path), "request_delay_seconds": [9, 12]}}))
    assert monitor._client is None and monitor._scraper is None
    monitor._ensure_components()
    assert monitor._client is not old_client and monitor._client.delay_range == (9.0, 12.0)
    await monitor.aclose()


# ---------------------------------------------------------------------------- pricing bits


async def test_reference_price_far_from_the_market_is_flagged() -> None:
    monitor, db, _, _ = build([make_listing("1", "RTX 3080", 150.0)], verdict=None,
                              search={"name": "GPU", "query": "rtx 3080", "reference_price": 300})
    teach(db)  # history: ~480 € after the asking-price discount
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert any(r.startswith("⚠ reference_price (300 €) расходится с рынком (~4") for r in ev.reasons)


def test_price_points_get_first_and_last_seen_with_migration(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE price_points (id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id TEXT NOT NULL UNIQUE,"
        " source TEXT NOT NULL, product_key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', price REAL NOT NULL,"
        " sold INTEGER NOT NULL DEFAULT 0, url TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL);"
        "INSERT INTO price_points (ad_id, source, product_key, title, price, seen_at)"
        " VALUES ('1', 'kleinanzeigen', 'rtx|3080', 'RTX 3080', 400, '2026-09-01T10:00:00+00:00');")
    conn.commit()
    conn.close()
    db = Database(path)
    (comp, first, last), = db.price_history_spans("rtx|3080")
    assert first == last and first.isoformat() == "2026-09-01T10:00:00+00:00"
    db.add_price_points("rtx|3080", [Comparable(title="RTX 3080", price=380, url="u")])
    listing = make_listing("1", "RTX 3080", 390.0)
    db.record_price_points([("rtx|3080", listing)])  # seen again: first_seen stays
    spans = {c.price: (first, last) for c, first, last in db.price_history_spans("rtx|3080")}
    assert spans[390.0][0].isoformat() == "2026-09-01T10:00:00+00:00" and spans[390.0][1] > spans[390.0][0]


def test_asking_prices_listed_for_weeks_weigh_less() -> None:
    now = utcnow()
    stale = [(Comparable(title="RTX 3080", price=600), now - timedelta(days=30), now) for _ in range(5)]
    fresh = [(Comparable(title="RTX 3080", price=450), now - timedelta(days=1), now) for _ in range(5)]
    est = estimate_from_history("rtx 3080", stale + fresh, min_points=6, asking_price_discount=1.0, now=now)
    assert est.market_price < 500


# ---------------------------------------------------------------------------- HTTP state, redirects


async def test_state_writes_merge_with_another_process(tmp_path) -> None:
    state = tmp_path / "http_state.json"
    clock = Clock()
    until = clock() + 3 * 3600
    state.write_text(json.dumps({"version": 1, "saved_at": clock(), "hosts": {"www.kleinanzeigen.de": {
        "strikes": 2, "cooldown_until": until, "last_block_at": clock() - 60, "last_reason": "403",
        "requests": [clock() - 120]}}}), encoding="utf-8")
    client = make(Handler(), clock, state_path=state)
    state.write_text(json.dumps({"version": 1, "saved_at": clock(), "hosts": {"www.kleinanzeigen.de": {
        "strikes": 3, "cooldown_until": until + 600, "last_block_at": clock(), "last_reason": "captcha",
        "requests": [clock() - 120, clock() - 30]}}}), encoding="utf-8")  # the monitor wrote meanwhile
    client._dirty = True
    client._save_state(force=True)
    host = json.loads(state.read_text(encoding="utf-8"))["hosts"]["www.kleinanzeigen.de"]
    assert host["strikes"] == 3 and host["cooldown_until"] == until + 600 and host["last_reason"] == "captcha"
    assert len(host["requests"]) == 2
    await client.aclose()


async def test_search_form_redirect_is_remembered(tmp_path) -> None:
    from ebeyparser.config import SearchConfig
    from test_scraper import fixture

    pretty = f"{KA}/s-berlin/rtx-3080/k0l3331r20"
    html = fixture("ka_search_page1.html")
    fetched: list[str] = []

    class Client:
        last_url: str | None = None

        async def get_text(self, url, *, referer=None):
            fetched.append(url)
            self.last_url = pretty if "s-suchanfrage" in url else url
            return html

    scraper = KleinanzeigenScraper(Client())  # type: ignore[arg-type]
    search = SearchConfig(name="GPU", query="rtx 3080", location="Berlin", radius_km=20)
    await scraper.search(search)
    await scraper.search(search)
    assert "s-suchanfrage" in fetched[0] and fetched[1] == pretty
