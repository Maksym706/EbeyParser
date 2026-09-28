"""The monitor's request-budgeted funnel: free triage, early skips, budgets and deferral,
category scans, baseline pass, instant alerts, re-checks. Fake sources, no network."""

from __future__ import annotations

from datetime import timedelta

from ebeyparser.ai.evaluator import SAME_VARIANT_KEY
from ebeyparser.config import parse_config
from ebeyparser.db import Database
from ebeyparser.models import AIVerdict, Comparable, Evaluation, PriceEstimate, utcnow
from ebeyparser.monitor import Monitor, _comparables_for_ai
from ebeyparser.pricing.estimator import product_query
from ebeyparser.scraper.http import BlockedError, RateBudgetExceeded

from test_monitor import GOOD_AI, SOLD, FakeEvaluator, FakeNotifier, FakeSource, make_listing

KA = "https://www.kleinanzeigen.de/s-anzeige/x/{}-225-3331"


class CompsSource(FakeSource):
    """FakeSource that remembers which comparables queries were asked."""

    def __init__(self, listings, comps=None):
        super().__init__(listings, comps)
        self.comps_queries: list[str] = []

    async def comparables(self, query, *, exclude_ad_id=None, limit=30, min_price=None, max_price=None):
        self.comps_queries.append(query)
        return list(self.comps)


class RecordingSold:
    def __init__(self, comps):
        self.comps = comps
        self.queries: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.queries)

    async def sold_comparables(self, query: str, limit: int = 30):
        self.queries.append(query)
        return list(self.comps)


def build(listings, *, verdict=GOOD_AI, sold=SOLD, general=None, search=None, **cfg):
    data = {
        "general": {"baseline_first_run": False, "max_new_per_search": 50, **(general or {})},
        "searches": [search or {"name": "GPU", "query": "rtx 3080"}],
        "pricing": {"min_profit": 40, "min_roi": 0.25},
        "notifications": {"min_score": 50, "verdicts": ["buy"]},
    }
    data.update(cfg)
    db = Database()
    source = CompsSource(listings)
    ebay = RecordingSold(sold)
    evaluator = FakeEvaluator(verdict) if verdict else None
    notifier = FakeNotifier()
    monitor = Monitor(parse_config(data), db, scraper=source, ebay=ebay, evaluator=evaluator, notifiers=[notifier])
    return monitor, db, source, ebay, evaluator, notifier


def teach(db: Database, price: float = 560.0, n: int = 10, key: str = "rtx 3080", title: str = "RTX 3080",
          start: int = 0) -> None:
    """Price history: n asking prices around `price`."""
    db.add_price_points(key, [
        Comparable(title=f"{title} {i}", price=price + (i % 5) * 5, url=KA.format(700000 + start + i),
                   source="kleinanzeigen")
        for i in range(n)
    ])


# ---------------------------------------------------------------------------- early skip


async def test_early_skip_avoids_comparables_page_and_ai():
    monitor, db, source, ebay, evaluator, _ = build([make_listing("1", "MSI RTX 3080 Gaming", 600.0)])
    teach(db)
    summary = await monitor.run_once()
    assert ebay.calls == 0 and source.comps_queries == []
    assert source.detail_calls == [] and evaluator.calls == []
    ev = db.get_evaluation("1")
    assert ev.verdict == "skip" and ev.stage == "market" and ev.action == "skip"
    assert ev.reasons[0].startswith("По рынку: ~") and "выгоды нет" in ev.reasons[0]
    assert ev.estimate.source == "history"
    assert summary.early_skips == 1 and summary.history_hits == 1 and summary.evaluated == 1
    assert summary.comps_lookups == summary.details_fetched == summary.ai_calls == 0
    again = await monitor.run_once()  # seen again soon after: not re-checked yet
    assert again.evaluated == 0 and again.early_skips == 0


async def test_reference_price_allows_early_skip():
    monitor, db, source, ebay, evaluator, _ = build(
        [make_listing("1", "RTX 3080", 480.0)], search={"name": "GPU", "query": "rtx 3080", "reference_price": 500})
    summary = await monitor.run_once()
    assert summary.early_skips == 1 and ebay.calls == 0 and source.detail_calls == [] and evaluator.calls == []


async def test_thin_history_no_deal_is_verified_with_comparables():
    monitor, db, source, ebay, _, _ = build([make_listing("1", "MSI RTX 3080 Gaming", 600.0)], verdict=None)
    teach(db, n=6)  # enough for an estimate, too few to drop an ad unseen
    summary = await monitor.run_once()
    assert summary.early_skips == 0 and ebay.calls == 1
    assert source.detail_calls == ["1"]  # ad page before comparables
    assert db.get_evaluation("1").stage == "full"


async def test_no_early_skip_for_keys_without_model_number():
    listing = make_listing("1", "Bugaboo Kinderwagen", 600.0)
    monitor, db, _, ebay, _, _ = build([listing], verdict=None, sold=[])
    teach(db, key="bugaboo kinderwagen", title="Bugaboo Kinderwagen", n=10)
    summary = await monitor.run_once()
    assert summary.early_skips == 0 and ebay.calls == 1


async def test_early_skip_is_rechecked_when_history_changes():
    monitor, db, _, ebay, evaluator, _ = build([make_listing("1", "MSI RTX 3080 Gaming", 600.0)])
    teach(db)
    await monitor.run_once()
    old = db.get_evaluation("1")
    db.save_evaluation(old.model_copy(update={"evaluated_at": utcnow() - timedelta(hours=7)}))
    quiet = await monitor.run_once()  # same history: still no deal, not counted again
    assert quiet.evaluated == 0 and quiet.early_skips == 0
    assert db.get_evaluation("1").evaluated_at > old.evaluated_at
    # prices went up since: the ad is worth a real look now
    db._execute("DELETE FROM price_points")
    teach(db, price=900, start=100)
    db.save_evaluation(db.get_evaluation("1").model_copy(update={"evaluated_at": utcnow() - timedelta(hours=7)}))
    summary = await monitor.run_once()
    assert summary.evaluated == 1 and evaluator.calls == ["1"]
    assert db.get_evaluation("1").stage == "full"


# ---------------------------------------------------------------------------- budgets


async def test_ai_budget_defers_and_next_runs_pick_up_the_rest():
    listings = [make_listing(str(10 + i), f"RTX 3080 #{i}", 300.0) for i in range(3)]
    monitor, db, source, ebay, evaluator, _ = build(listings, general={"max_ai_per_run": 1})
    first = await monitor.run_once()
    assert first.ai_calls == 1 and first.deferred == 2 and first.evaluated == 1
    assert [db.get_evaluation(l.ad_id) is None for l in listings].count(True) == 2  # pending
    source.listings = []  # the deferred ads dropped off the first result page meanwhile
    second = await monitor.run_once()
    assert second.ai_calls == 1 and second.deferred == 1 and second.evaluated == 1
    third = await monitor.run_once()
    assert third.evaluated == 1 and third.deferred == 0
    assert sorted(evaluator.calls) == ["10", "11", "12"]
    assert all(db.get_evaluation(l.ad_id).verdict == "buy" for l in listings)


async def test_detail_budget_defers():
    listings = [make_listing("1", "RTX 3080 A", 300.0), make_listing("2", "RTX 3080 B", 310.0)]
    monitor, db, source, _, _, _ = build(listings, verdict=None, general={"max_details_per_run": 1})
    summary = await monitor.run_once()
    assert source.detail_calls == ["1"] and summary.details_fetched == 1
    assert summary.deferred == 1 and db.get_evaluation("2") is None


async def test_comps_budget_defers_but_cache_hits_are_free():
    listings = [make_listing("1", "RTX 3080 A", 300.0), make_listing("2", "RTX 3080 B", 310.0),
                make_listing("3", "Apple iPhone 13 128GB", 300.0)]
    monitor, db, _, ebay, _, _ = build(listings, verdict=None, general={"max_comps_lookups_per_run": 1})
    summary = await monitor.run_once()
    assert ebay.queries == ["rtx 3080"] and summary.comps_lookups == 1
    assert summary.evaluated == 2 and summary.deferred == 1 and db.get_evaluation("3") is None


async def test_zero_budget_switches_the_step_off_instead_of_deferring():
    monitor, db, source, _, evaluator, _ = build([make_listing("1", "RTX 3080", 300.0)],
                                                 general={"max_ai_per_run": 0, "max_details_per_run": 0})
    summary = await monitor.run_once()
    assert evaluator.calls == [] and source.detail_calls == [] and summary.deferred == 0
    ev = db.get_evaluation("1")
    assert ev.verdict == "buy" and ev.ai is None and ev.ai_checked is None


async def test_max_new_per_search_caps_paid_work_not_free_triage():
    listings = [make_listing("1", "RTX 3080 A", 300.0), make_listing("2", "RTX 3080 B", 310.0),
                make_listing("3", "Suche RTX 3080", 200.0)]
    monitor, db, _, _, _, _ = build(listings, general={"max_new_per_search": 1})
    summary = await monitor.run_once()
    assert summary.prefiltered == 1 and summary.evaluated == 2 and summary.deferred == 1


async def test_counters_are_stored_with_the_run():
    monitor, db, _, _, _, _ = build([make_listing("1", "RTX 3080", 300.0), make_listing("2", "Suche RTX 3080", 1.0)])
    summary = await monitor.run_once()
    stored = db.list_runs()[0]
    for name in ("prefiltered", "early_skips", "history_hits", "comps_lookups", "details_fetched",
                 "ai_calls", "deferred"):
        assert getattr(stored, name) == getattr(summary, name), name
    assert (stored.prefiltered, stored.comps_lookups, stored.details_fetched, stored.ai_calls) == (1, 1, 1, 1)


# ---------------------------------------------------------------------------- stage 0


async def test_min_listing_price_skips_junk_but_not_free_ads_or_auctions():
    listings = [
        make_listing("1", "RTX 3080 Aufkleber", 5.0),
        make_listing("2", "RTX 3080", None, is_free=True, price_text="Zu verschenken"),
        make_listing("3", "RTX 3080", 1.0, buying_options=["AUCTION"]),
    ]
    monitor, db, _, _, _, _ = build(listings, verdict=None)
    summary = await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.stage == "prefilter" and "ниже порога 10 €" in ev.reasons[0]
    assert db.get_evaluation("2").stage == "full" and db.get_evaluation("2").verdict == "buy"
    assert db.get_evaluation("3").stage == "full" and db.get_evaluation("3").action == "bid"
    assert summary.prefiltered == 1


async def test_search_min_price_overrides_min_listing_price():
    monitor, db, _, _, _, _ = build([make_listing("1", "Lego 75192", 6.0)], verdict=None, sold=[],
                                    search={"name": "Lego", "query": "lego", "min_price": 5})
    await monitor.run_once()
    assert db.get_evaluation("1").stage == "full"


# ---------------------------------------------------------------------------- category scans


async def test_category_scan_with_empty_query():
    listings = [make_listing("1", "Apple iPhone 13 128GB Blau", 300.0),
                make_listing("2", "Samsung Galaxy S21 Ultra 256GB", 250.0)]
    monitor, db, source, ebay, _, _ = build(
        listings, verdict=None, sold=[],
        search={"name": "Handys", "category_id": 173, "category_name": "Handy & Telefon",
                "location": "Berlin", "radius_km": 30})
    assert monitor.config.searches[0].query == ""
    summary = await monitor.run_once()
    assert summary.evaluated == 2 and not summary.errors
    # every ad gets its own product key / comparables query
    expected = [product_query(l.title) for l in listings]
    assert source.comps_queries == ebay.queries == expected
    assert "iphone 13 128gb" in expected[0] and "s21 ultra 256gb" in expected[1]
    assert db.get_listing("1").search_name == "Handys"


# ---------------------------------------------------------------------------- baseline


async def test_first_pass_of_a_new_search_only_learns():
    listings = [make_listing("1", "RTX 3080 A", 300.0), make_listing("2", "RTX 3080 B", 400.0)]
    monitor, db, source, ebay, evaluator, notifier = build(listings, general={"baseline_first_run": True})
    first = await monitor.run_once()
    assert first.new_listings == 2 and first.evaluated == 0 and notifier.sent == []
    assert db.get_evaluation("1") is None and db.count_price_points() == 2
    assert ebay.calls == 0 and source.detail_calls == [] and evaluator.calls == []
    source.listings = listings + [make_listing("3", "RTX 3080 C", 300.0)]
    second = await monitor.run_once()
    assert second.evaluated == 1 and evaluator.calls == ["3"]  # only the truly new ad
    source.listings = [listings[1].model_copy(update={"price": 300.0})]  # a learned ad got cheaper
    third = await monitor.run_once()
    assert third.evaluated == 1 and evaluator.calls == ["3", "2"]


async def test_searches_from_older_versions_are_not_relearned():
    monitor, db, source, _, evaluator, _ = build([make_listing("1", "RTX 3080", 300.0)],
                                                 general={"baseline_first_run": True})
    db.upsert_listing(make_listing("0", "RTX 3080 alt", 500.0).model_copy(update={"search_name": "GPU"}))
    db.save_evaluation(Evaluation(ad_id="0", verdict="skip"))
    summary = await monitor.run_once()
    assert summary.evaluated == 1 and evaluator.calls == ["1"]


# ---------------------------------------------------------------------------- alerts / order


async def test_best_deals_first_and_alert_right_away():
    listings = [make_listing("A", "RTX 3080 A", 330.0), make_listing("B", "RTX 3080 B", 260.0)]
    monitor, db, _, _, evaluator, notifier = build(listings)
    teach(db)
    summary = await monitor.run_once()
    assert evaluator.calls == ["B", "A"]  # cheapest vs. the market first
    assert notifier.sent == [["B"], ["A"]]  # one alert per deal, as soon as it is found
    assert summary.notified == 2 and summary.history_hits == 2


async def test_ai_unavailable_means_no_buy_and_no_alert():
    down = AIVerdict(verdict="maybe", confidence=0.0, reasoning="LM Studio не отвечает")
    monitor, db, _, _, _, notifier = build([make_listing("1", "RTX 3080", 300.0)], verdict=down)
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.ai_checked is False and ev.verdict == "maybe"
    assert "⚠ Фото НЕ проверены ИИ (нейросеть не ответила)" in ev.reasons
    assert notifier.sent == []


async def test_reserved_ads_never_alert():
    listing = make_listing("1", "RTX 3080", 300.0, tags=["Reserviert"])
    monitor, db, _, _, _, notifier = build([listing], notifications={"min_score": 0, "verdicts": ["buy", "maybe"]})
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.verdict == "maybe" and ev.no_alert and ev.action == "watch"
    assert notifier.sent == []


# ---------------------------------------------------------------------------- price drops


async def test_price_drop_is_measured_from_the_evaluated_price():
    listing = make_listing("1", "RTX 3080 Ventus", 520.0)
    monitor, db, source, _, _, _ = build([listing])
    await monitor.run_once()
    assert db.get_evaluation("1").buy_price == 520
    source.listings = [listing.model_copy(update={"price": 490.0})]  # -6 %: not yet
    assert (await monitor.run_once()).evaluated == 0
    source.listings = [listing.model_copy(update={"price": 465.0})]  # -5 % vs. last seen, -11 % vs. evaluated
    assert (await monitor.run_once()).evaluated == 1
    assert db.get_evaluation("1").buy_price == 465


async def test_deferred_price_drop_is_retried_next_pass():
    listing = make_listing("1", "RTX 3080 Ventus", 520.0)
    monitor, db, source, _, evaluator, _ = build([listing], general={"max_ai_per_run": 1})
    await monitor.run_once()
    assert db.get_evaluation("1").verdict == "skip"
    gift = make_listing("2", "RTX 3080 zu verschenken", None, is_free=True)
    source.listings = [gift, listing.model_copy(update={"price": 250.0})]
    summary = await monitor.run_once()
    assert evaluator.calls == ["2"] and summary.deferred == 1
    assert db.get_evaluation("1") is None  # stale verdict dropped: pending now
    await monitor.run_once()
    assert evaluator.calls == ["2", "1"] and db.get_evaluation("1").verdict == "buy"


# ---------------------------------------------------------------------------- single-ad check


async def test_evaluate_url_runs_every_step_without_budgets():
    monitor, db, source, ebay, evaluator, _ = build(
        [], general={"max_ai_per_run": 0, "max_details_per_run": 0, "max_comps_lookups_per_run": 0,
                     "min_listing_price": 1000})
    deal = await monitor.evaluate_url("https://www.kleinanzeigen.de/s-anzeige/rtx-3080/2891234567-225-3331")
    assert deal.evaluation is not None and deal.evaluation.stage == "full"
    assert source.detail_calls == ["2891234567"] and ebay.calls == 1 and evaluator.calls == ["2891234567"]


# ---------------------------------------------------------------------------- stats


def test_potential_profit_counts_open_deals_of_the_last_week():
    db = Database()
    for ad_id, days_ago, profit in (("new", 1, 100.0), ("bought", 1, 200.0), ("old", 10, 300.0)):
        listing = make_listing(ad_id, "RTX 3080", 300.0).model_copy(
            update={"first_seen": utcnow() - timedelta(days=days_ago)})
        db.upsert_listing(listing)
        db.save_evaluation(Evaluation(ad_id=ad_id, verdict="buy", expected_profit=profit))
    db.set_status("bought", "bought")
    assert db.stats()["potential_profit"] == 100


# ---------------------------------------------------------------------------- network limits


def rate_limit(host: str = "www.kleinanzeigen.de") -> RateBudgetExceeded:
    return RateBudgetExceeded(f"{host}: лимит 150 страниц в час исчерпан", host=host, limit=150,
                              retry_at=utcnow() + timedelta(minutes=20))


async def test_search_pages_on_until_known_ads_and_category_scans_get_more_pages():
    monitor, _, source, _, _, _ = build([], search={"name": "Handys", "category_id": 173})
    await monitor.run_once()
    assert source.seen_given == [True] and source.search_pages == [3]
    monitor, _, source, _, _, _ = build([])
    await monitor.run_once()
    assert source.search_pages == [1]  # keyword searches keep general.max_pages


async def test_hourly_budget_on_ad_pages_defers_without_counting_a_request():
    listings = [make_listing("1", "RTX 3080 A", 300.0), make_listing("2", "RTX 3080 B", 310.0)]
    monitor, db, source, _, _, _ = build(listings)
    real_fetch = source.fetch_detail

    async def limited(listing):
        raise rate_limit()

    source.fetch_detail = limited
    summary = await monitor.run_once()
    assert summary.deferred == 2 and summary.evaluated == 0 and summary.details_fetched == 0
    assert db.get_evaluation("1") is None and db.get_evaluation("2") is None
    assert sum("лимит 150 страниц" in e for e in summary.errors) == 1  # said once, not a block
    assert not any("ограничил" in e for e in summary.errors)
    source.fetch_detail = real_fetch  # next pass: the hour is over
    again = await monitor.run_once()
    assert again.evaluated == 2 and again.deferred == 0


async def test_hourly_budget_on_the_search_page_skips_only_that_search():
    monitor, db, source, _, _, _ = build(
        [make_listing("1", "RTX 3080", 300.0)],
        searches=[{"name": "A", "query": "rtx 3080"}, {"name": "B", "query": "rtx 3080"}])
    calls = {"n": 0}
    real_search = source.search

    async def first_limited(search, max_pages=1, seen=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise rate_limit()
        return await real_search(search, max_pages=max_pages, seen=seen)

    source.search = first_limited
    summary = await monitor.run_once()
    assert calls["n"] == 2 and summary.evaluated == 1  # search B still ran
    assert any("лимит 150 страниц" in e for e in summary.errors)


async def test_comparables_blocked_by_hourly_budget_defer_the_ad():
    monitor, db, source, ebay, _, _ = build([make_listing("1", "RTX 3080", 300.0)], verdict=None, sold=[])

    async def limited(query, limit=30):
        raise rate_limit("www.ebay.de")

    ebay.sold_comparables = limited
    summary = await monitor.run_once()  # eBay sold limited, Kleinanzeigen comps empty: nothing to go on
    assert summary.deferred == 1 and summary.comps_lookups == 0 and db.get_evaluation("1") is None
    source.comps = [Comparable(title=f"RTX 3080 {i}", price=560.0 + i, url=KA.format(800000 + i),
                               source="kleinanzeigen") for i in range(8)]
    summary = await monitor.run_once()  # partial lookup: used, but not cached
    ev = db.get_evaluation("1")
    assert ev is not None and ev.estimate.market_price is not None
    assert monitor._cached_comps("rtx 3080") is None


async def test_cooldown_after_a_block_is_a_short_note():
    monitor, _, source, _, _, _ = build([])

    async def cooling(search, max_pages=1, seen=None):
        raise BlockedError("Kleinanzeigen: пауза", host="www.kleinanzeigen.de", cooling_down=True,
                           cooldown_until=utcnow() + timedelta(minutes=42))

    source.search = cooling
    summary = await monitor.run_once()
    assert len(summary.errors) == 1 and summary.errors[0].startswith("Kleinanzeigen: пауза после блокировки ещё")
    assert "увеличь" not in summary.errors[0]


def test_http_client_state_is_persisted_and_status_exposed(tmp_path):
    cfg = parse_config({"general": {"data_dir": str(tmp_path)}})
    monitor = Monitor(cfg, Database(), notifiers=[])
    assert monitor.http_status() == {}
    monitor._ensure_components()
    assert monitor._client.state_path == tmp_path / "http_state.json"
    assert isinstance(monitor.http_status(), dict)


# ---------------------------------------------------------------------------- AI and comparables


class VariantEvaluator(FakeEvaluator):
    """Fake AI that picks the same-variant comparables by index."""

    def __init__(self, verdict, picks: str | None):
        super().__init__(verdict)
        self.picks = picks
        self.shown: list[list[Comparable]] = []

    async def evaluate(self, listing, images, *, purpose="resale", estimate=None, target_price=None,
                       comparables=None):
        self.calls.append(listing.ad_id)
        self.shown.append(list(comparables or []))
        variant = {} if self.picks is None else {SAME_VARIANT_KEY: self.picks}
        return self.verdict.model_copy(update={"variant": variant})


def history_monitor(picks):
    monitor, db, _, _, _, _ = build([make_listing("1", "RTX 3080 Gaming", 300.0)])
    monitor._evaluator = evaluator = VariantEvaluator(GOOD_AI, picks)
    teach(db)
    return monitor, db, evaluator


async def test_ai_sees_typical_comparables_and_confirms_the_variant():
    monitor, db, evaluator = history_monitor("0,1,2,3,4,5,6")
    await monitor.run_once()
    assert len(evaluator.shown[0]) == 8
    ev = db.get_evaluation("1")
    assert ev.estimate.ai_variant_matches == 7 and ev.estimate.sample_size == 7
    assert ev.estimate.notes.startswith("ИИ подтвердил 7 точных аналогов")
    assert ev.verdict == "buy"


async def test_ai_finding_no_same_variant_caps_at_maybe():
    monitor, db, _ = history_monitor("")
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.estimate.ai_variant_matches == 0 and ev.verdict == "maybe"
    assert any("тот же вариант" in r for r in ev.reasons)


async def test_ai_without_variant_answer_changes_nothing():
    monitor, db, _ = history_monitor(None)
    await monitor.run_once()
    ev = db.get_evaluation("1")
    assert ev.estimate.ai_variant_matches is None and ev.verdict == "buy"


def test_comparables_for_ai_are_the_typical_ones():
    comps = [Comparable(title=f"RTX 3080 {p}", price=float(p)) for p in (50, 90, 380, 390, 400, 410, 420,
                                                                           430, 440, 450, 900)]
    chosen = _comparables_for_ai(PriceEstimate(market_price=415, comparables=comps))
    assert len(chosen) == 8 and min(c.price for c in chosen) == 380 and max(c.price for c in chosen) == 450
    spread = _comparables_for_ai(PriceEstimate(comparables=comps))
    assert len(spread) == 8 and spread[0].price == 50  # no market yet: a spread of prices
    assert _comparables_for_ai(PriceEstimate()) == []
