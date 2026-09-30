"""AI scout inside the real Monitor (fake sources, a fake scout model, no network): a PC the
script drops is promoted and bought with its GPU's real market price («Нашла нейросеть»), a
typo'd title gets its market price, junk is ranked last, the scout being down changes
nothing, the vision queue holds would-be deals while the photo model is offline, the
«Супер-находка» skips the hourly cap, «Топ за день», the events, the DB and the API card."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ebeyparser.ai.client import LLMError
from ebeyparser.ai.triage import TriageEngine
from ebeyparser.config import parse_config
from ebeyparser.db import Database
from ebeyparser.models import AIVerdict, Comparable, DealView, Evaluation, Listing, PriceEstimate, utcnow
from ebeyparser.notify.render import SUPER_PREFIX

from test_monitor import GOOD_AI, FakeNotifier, build, make_listing

PC_AI = AIVerdict(product="Gaming PC", item_type="complete_pc", verdict="buy", confidence=0.8,
                  photo_matches_description=True, condition="good", reasoning="Настоящие фото ПК.", model="fake")
PHONE_AI = AIVerdict(product="Apple iPhone 13 128GB", item_type="single", verdict="buy", confidence=0.8,
                     photo_matches_description=True, condition="good", reasoning="Реальные фото.", model="fake")
DOWN_AI = AIVerdict(verdict="maybe", confidence=0.0, reasoning="Нейросеть не проверила фото: LM Studio не отвечает")
KA = "https://www.kleinanzeigen.de/s-anzeige/x/{}-225-3331"


class ScoutLLM:
    """The scout's model: answers by ad title from `answers` (title -> item dict without "i")."""

    def __init__(self, answers: dict[str, dict], *, down: bool = False):
        self.answers = answers
        self.down = down
        self.prompts: list[str] = []

    async def chat_json(self, system, user, images=None, schema=None):
        self.prompts.append(user)
        if self.down:
            raise LLMError("Локальная модель недоступна по адресу http://nas:8080 (ConnectError)")
        items = []
        for line in user.splitlines():
            if not line.startswith("["):
                continue
            idx = int(line[1:line.index("]")])
            title = line.split("Titel: ", 1)[1].split(" | ", 1)[0]
            answer = self.answers.get(title, {"k": "other", "s": 2, "r": "ничего интересного"})
            items.append({"i": idx, "k": "single", "p": "", "n": 1, "c": [], "q": "", "z": "used", "h": [], "x": [],
                          "s": 5, "r": "", **answer})
        return json.dumps({"items": items}, ensure_ascii=False)

    async def health(self):
        return {"ok": True}

    async def aclose(self):
        return None


def scout_engine(answers: dict[str, dict], **kw) -> tuple[TriageEngine, ScoutLLM]:
    llm = ScoutLLM(answers, **kw)
    return TriageEngine(llm, model="fake-scout", batch_size=8, max_per_hour=0), llm


def teach(db: Database, key: str, title: str, price: float, n: int = 10, start: int = 0) -> None:
    db.add_price_points(key, [Comparable(title=f"{title} {i}", price=price + (i % 5) * 5,
                                         url=KA.format(800000 + start + i), source="kleinanzeigen")
                              for i in range(n)])


def cfg(**overrides):
    data = {
        "general": {"max_new_per_search": 20, "baseline_first_run": False},
        "searches": [{"name": "Technik", "category_id": 225}],
        "pricing": {"min_profit": 40, "min_roi": 0.25},
        "notifications": {"min_score": 50, "verdicts": ["buy"], "heartbeat_hour": None},
        "ai": {"scout": {"min_interest": 6}},
    }
    for key, value in overrides.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) else value
    return parse_config(data)


PC_TEXT = "Drin ist eine RTX 3080 10GB, Intel Core i7-8700K und 16GB RAM. Läuft."
PC_ITEM = {"k": "pc", "c": ["RTX 3080 10GB", "Intel Core i7-8700K", "16GB DDR4 RAM"], "s": 9, "h": ["pc_parts"],
           "r": "старый ПК, внутри RTX 3080"}


async def test_scout_promotes_a_pc_the_script_drops():
    pc = make_listing("1", "Alter Rechner vom Dachboden", 150.0, description=PC_TEXT)
    # script only: the kind of offer (a whole PC) ends it for free
    monitor, db, _, notifier = build(cfg(), [pc.model_copy()], verdict=PC_AI)
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.verdict == "skip" and ev.stage == "prefilter" and ev.found_by == "script"

    engine, llm = scout_engine({"Alter Rechner vom Dachboden": PC_ITEM})
    monitor, db, _, notifier = build(cfg(), [pc.model_copy()], verdict=PC_AI, scout=engine)
    teach(db, "rtx|3080", "RTX 3080", 560.0)
    events: list[tuple[str, dict]] = []
    monitor.on_event.append(lambda kind, data: events.append((kind, data)))
    summary = await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.verdict == "buy" and ev.found_by == "ai_scout" and ev.stage == "full", ev.reasons
    assert ev.estimate.market_price < 480 and "по частям" in ev.estimate.notes  # the GPU minus 25 %, CPU not guessed
    assert "🔎 Нашла нейросеть: старый ПК, внутри RTX 3080" in ev.reasons
    assert summary.scout_read == 1 and summary.scout_promoted == 1 and summary.scout_deals == 1
    assert notifier.sent == [["1"]]
    found = next(data for kind, data in events if kind == "deal_found")
    assert found["found_by"] == "ai_scout" and found["tier"] in ("deal", "super") and found["listing"]["ad_id"] == "1"
    assert "Titel: Alter Rechner vom Dachboden" in llm.prompts[0]
    assert db.get_triage(["1"])["1"]["kind"] == "pc"


async def test_scout_prices_a_typo_the_regexes_cannot_read():
    phone = make_listing("7", "Iphne 13 128gb", 180.0, description="Akku 88 %, kleine Kratzer.")
    monitor, db, _, _ = build(cfg(), [phone.model_copy()], verdict=PHONE_AI)
    teach(db, "iphone|13||128gb", "iPhone 13 128GB", 400.0)
    await monitor.run_once()
    assert db.get_evaluation("7").verdict != "buy"  # no market for "iphne 13"

    engine, _ = scout_engine({"Iphne 13 128gb": {"p": "Apple iPhone 13 128GB", "q": "iphone 13 128gb", "s": 7,
                                                 "h": ["typo"], "r": "iPhone 13 с опечаткой"}})
    monitor, db, _, _ = build(cfg(), [phone.model_copy()], verdict=PHONE_AI, scout=engine)
    teach(db, "iphone|13||128gb", "iPhone 13 128GB", 400.0)
    await monitor.run_once()
    ev = db.get_evaluation("7")
    assert ev.verdict == "buy" and ev.found_by == "ai_scout" and ev.estimate.source == "history", ev.reasons
    # its price now counts for the iPhone 13 128GB, not for "iphne"
    assert db.count_price_points("iphone|13||128gb") == 11 and db.count_price_points("iphne|13||128gb") == 0


async def test_scout_ranks_junk_last():
    junk = make_listing("A", "Sachen vom Dachboden", 60.0, description="Alles mögliche, nur zusammen.")
    tablet = make_listing("B", "Tablet zu verkaufen", 150.0, description="iPad Air 5 64GB WiFi, blau.")
    answers = {"Sachen vom Dachboden": {"k": "other", "s": 1, "r": "хлам"},
               "Tablet zu verkaufen": {"p": "Apple iPad Air 5 64GB", "q": "ipad air 5 64gb", "s": 8}}
    limits = {"general": {"max_ai_per_run": 1}}
    monitor, _, _, _ = build(cfg(**limits), [junk.model_copy(), tablet.model_copy()], verdict=GOOD_AI)
    await monitor.run_once()
    assert monitor._evaluator.calls == ["A"]  # script only: unknown markets, oldest first
    engine, _ = scout_engine(answers)
    monitor, _, _, _ = build(cfg(**limits), [junk.model_copy(), tablet.model_copy()], verdict=GOOD_AI, scout=engine)
    summary = await monitor.run_once()
    assert monitor._evaluator.calls == ["B"] and summary.deferred == 1  # the interesting one gets the AI call


async def test_scout_never_promotes_what_the_code_flags():
    """A small model missed the WhatsApp-only scam; the code's own red flags stop the promotion."""
    pc = make_listing("1", "Alter Rechner vom Dachboden", 150.0, description=PC_TEXT + " Kontakt nur per WhatsApp.")
    engine, _ = scout_engine({"Alter Rechner vom Dachboden": PC_ITEM})  # no risk tag from the model
    monitor, db, _, notifier = build(cfg(), [pc], verdict=PC_AI, scout=engine)
    teach(db, "rtx|3080", "RTX 3080", 560.0)
    summary = await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.verdict == "skip" and ev.found_by != "ai_scout" and summary.scout_promoted == 0
    assert summary.scout_read == 1 and notifier.sent == []


async def test_scout_refuses_a_model_under_2b_and_shows_the_expected_speed():
    pc = make_listing("1", "Alter Rechner vom Dachboden", 150.0, description=PC_TEXT)
    tiny = cfg(ai={"scout": {"enabled": True, "base_url": "http://nas:8080/v1", "model": "qwen3.5:0.8b"}})
    monitor, db, _, _ = build(tiny, [pc.model_copy()], verdict=PC_AI)
    summary = await monitor.run_once()
    assert monitor._scout is None and not monitor.scout_enabled and summary.scout_read == 0
    assert db.get_evaluation("1").stage == "prefilter"  # the old pipeline, untouched
    status = monitor.scout_status()
    assert status["state"] == "too_small" and "слишком маленькая" in status["text_ru"] and status["speed_ru"] == ""
    assert monitor.config.ai.scout.model == "qwen3.5:0.8b"  # the user's model stays as configured
    ok = cfg(ai={"scout": {"enabled": True, "base_url": "http://nas:8080/v1", "model": "qwen3.5:2b-q4_K_M"}})
    monitor, _, _, _ = build(ok, [], verdict=PC_AI)
    status = monitor.scout_status()  # not measured yet: the model research's speed (a remote box: T0)
    assert status["speed_expected"] and status["speed_ru"].startswith("≈ 6,0 с на объявление")


async def test_scout_down_is_the_old_pipeline():
    pc = make_listing("1", "Alter Rechner vom Dachboden", 150.0, description=PC_TEXT)
    engine, _ = scout_engine({}, down=True)
    monitor, db, _, _ = build(cfg(), [pc], verdict=PC_AI, scout=engine)
    events: list[str] = []
    monitor.on_event.append(lambda kind, data: events.append(data.get("kind", "")) if kind == "health_alert" else None)
    summary = await monitor.run_once()
    assert db.get_evaluation("1").stage == "prefilter" and summary.scout_read == 0 and summary.scout_overflow == 1
    assert "scout_down" in events
    status = monitor.scout_status()
    assert status["enabled"] and status["state"] == "down" and "не отвечает" in status["text_ru"]


async def test_vision_queue_holds_until_the_model_is_back():
    listing = make_listing("1", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0)
    monitor, db, _, notifier = build(cfg(searches=[{"name": "GPU", "query": "rtx 3080"}]), [listing], verdict=DOWN_AI)
    first = await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.would_buy and ev.ai_checked is False and notifier.sent == []  # waits for the photo check
    assert [a for a, _, _ in db.vision_queue()] == ["1"] and monitor.scout_status()["vision_queue"]["waiting"] == 1
    assert first.notified == 0
    monitor._evaluator.verdict = GOOD_AI  # the gaming PC is on again
    events: list[str] = []
    monitor.on_event.append(lambda kind, data: events.append(kind))
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.verdict == "buy" and ev.ai_checked is True and notifier.sent == [["1"]]
    assert db.vision_queue() == [] and "deal_updated" in events


async def test_vision_queue_gives_up_after_the_wait():
    listing = make_listing("1", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0)
    monitor, db, _, notifier = build(cfg(searches=[{"name": "GPU", "query": "rtx 3080"}], ai={"vision_wait_minutes": 30}),
                                     [listing], verdict=DOWN_AI)
    await monitor.run_once()
    assert notifier.sent == []
    with db._lock:
        db._conn.execute("UPDATE vision_queue SET queued_at = ?", ((utcnow() - timedelta(minutes=31)).isoformat(),))
        db._conn.commit()
    await monitor.run_once()
    assert notifier.sent == [["1"]] and db.vision_queue() == []  # sent, marked "фото не проверены"
    assert db.get_evaluation("1").ai_checked is False


class TitleNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.titles: list[str | None] = []

    async def send(self, deals, *, title=None):
        self.titles.append(title)
        await super().send(deals, title=title)


async def test_super_find_skips_the_hourly_cap():
    listings = [make_listing("S", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0), make_listing("N", "RTX 3080 FE", 380.0)]
    notifier = TitleNotifier()
    monitor, db, _, _ = build(cfg(searches=[{"name": "GPU", "query": "rtx 3080"}],
                                  notifications={"max_alerts_per_hour": 1}), listings, verdict=GOOD_AI,
                              notifier=notifier)
    monitor._count_alert_message()  # the hour's one alert is already used
    summary = await monitor.run_once()
    assert db.get_evaluation("S").verdict == "buy" and db.get_evaluation("N").verdict == "buy"
    assert notifier.sent == [["S"]] and notifier.titles[0].startswith(SUPER_PREFIX)
    assert summary.super_deals == 1 and summary.queued_alerts == 1


async def test_daily_top_once_a_day():
    notifier = TitleNotifier()
    monitor, _, _, _ = build(cfg(searches=[{"name": "GPU", "query": "rtx 3080"}],
                                 notifications={"daily_top": {"enabled": True, "hour": 0, "per_search": 2}}),
                             [make_listing("1", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0)], verdict=GOOD_AI,
                             notifier=notifier)
    await monitor.run_once()
    await monitor.run_once()
    tops = [t for t in notifier.titles if t and t.startswith("Топ за день")]
    assert len(tops) == 1 and notifier.sent.count(["1"]) == 2  # the alert, and once in the daily top


def test_card_has_found_by_and_tier():
    from ebeyparser.web.api.presenters import deal_card

    listing = Listing(ad_id="1", url="https://www.kleinanzeigen.de/s-anzeige/x/1", title="Alter PC", price=150.0)
    ev = Evaluation(ad_id="1", verdict="buy", action="buy", ai_checked=True, expected_profit=200.0, roi=1.3,
                    score=95.0, found_by="ai_scout",
                    estimate=PriceEstimate(market_price=400.0, sample_size=10, source="history"),
                    reasons=["Цена 150 €", "🔎 Нашла нейросеть: старый ПК, внутри RTX 3080"])
    card = deal_card(DealView(listing=listing, evaluation=ev), config=parse_config({}))
    assert card["found_by"] == "ai_scout" and card["found_by_label"] == "Нашла нейросеть"
    assert card["scout_reason"] == "старый ПК, внутри RTX 3080"
    assert card["tier"] == "super" and card["tier_label"].startswith("🔥")
    plain = deal_card(DealView(listing=listing, evaluation=ev.model_copy(update={"found_by": "", "reasons": []})))
    assert plain["found_by"] == "script" and plain["found_by_label"] == "" and plain["tier"] == "deal"


def test_db_migration_is_additive(tmp_path: Path):
    path = tmp_path / "old.sqlite3"
    db = Database(path)
    db.upsert_listing(Listing(ad_id="1", url="u", title="t", price=5.0))
    with db._lock:  # an old database: no scout tables yet
        db._conn.execute("DROP TABLE scout_triage")
        db._conn.execute("DROP TABLE vision_queue")
        db._conn.commit()
    db.close()
    db = Database(path)
    assert db.get_listing("1") is not None
    assert db.save_triage([{"ad_id": "1", "kind": "pc", "interest": 8}]) == 1 and db.get_triage(["1"])["1"]["kind"] == "pc"
    db.queue_vision("1", "s")
    db.delete_listings(["1"])
    assert db.get_triage(["1"]) == {} and db.vision_queue() == []
