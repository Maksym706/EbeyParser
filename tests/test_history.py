"""Price history: storage, lookup, pruning, the history estimate and how the monitor feeds it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ebeyparser.config import PricingConfig, SearchConfig, parse_config
from ebeyparser.db import Database, price_point_id
from ebeyparser.models import Comparable, PriceEstimate, utcnow
from ebeyparser.monitor import Monitor
from ebeyparser.pricing.estimator import (
    combine_estimates,
    estimate_from_history,
    evaluate,
    history_key_for,
    history_worthy,
    is_single_item,
)
from ebeyparser.pricing.text import history_anchor_words, is_model_key, price_point_words

from test_monitor import FakeNotifier, FakeSold, FakeSource, make_listing

KA = "https://www.kleinanzeigen.de/s-anzeige/x/{}-225-3331"


def ka(ad_id: str, title: str, price: float) -> Comparable:
    return Comparable(title=title, price=price, url=KA.format(ad_id), source="kleinanzeigen")


def sold(n: int, title: str, price: float) -> Comparable:
    return Comparable(title=title, price=price, url=f"https://www.ebay.de/itm/{400000000000 + n}",
                      source="ebay_sold", sold=True)


# ---------------------------------------------------------------------------- keys


def test_anchor_and_index_words_match_spelling_variants():
    assert history_anchor_words("apple iphone 13 128gb") == ["13", "iphone"]
    assert history_anchor_words("iphone13 128gb") == ["13", "iphone"]
    assert history_anchor_words("rtx 3080") == ["3080", "rtx"]
    assert history_anchor_words("apple macbook 13 m1 2020") == ["m1", "macbook"]  # m1 beats bare 13/2020
    assert history_anchor_words("kinderwagen bugaboo") == ["kinderwagen"]
    assert history_anchor_words("") == []
    words = price_point_words("Apple MacBook Pro 13 M1 2020 16GB", "apple macbook 13 m1 2020")
    assert {"macbook", "pro", "m1", "13", "2020"} <= words
    assert {"iphone", "13"} <= price_point_words("iPhone13 128GB", "iphone13 128gb")


def test_model_key():
    assert is_model_key("iphone 13") and is_model_key("rtx 3080") and is_model_key("sony ps5 disc")
    assert not is_model_key("kinderwagen bugaboo") and not is_model_key("ssd 512gb")


def test_price_point_id():
    assert price_point_id(ka("2891234567", "RTX 3080", 400)) == "2891234567"
    assert price_point_id(sold(1, "RTX 3080", 400)) == "ebay-400000000001"
    assert price_point_id(Comparable(title="RTX 3080", price=400.0, source="ebay_sold")).startswith("ebay_sold:")
    assert price_point_id(make_listing("77", "RTX 3080", 400.0)) == "77"


# ---------------------------------------------------------------------------- storage


def test_record_lookup_and_one_row_per_ad():
    db = Database()
    db.add_price_points("rtx 3080", [ka("1", "MSI RTX 3080 Ventus", 400), ka("2", "RTX 3080 FE", 420),
                                      sold(1, "Zotac RTX 3080 Trinity", 450)])
    db.add_price_points("rtx 3080", [ka("1", "MSI RTX 3080 Ventus", 380)])  # seen again, cheaper
    assert db.count_price_points() == 3
    points = {c.title: c for c in db.price_history("rtx 3080", utcnow() - timedelta(days=1))}
    assert points["MSI RTX 3080 Ventus"].price == 380
    assert points["Zotac RTX 3080 Trinity"].sold and points["Zotac RTX 3080 Trinity"].source == "ebay_sold"
    # the same ad as a listing (search results) and as a comparable is one row
    db.record_price_points([("rtx 3080", make_listing("2", "RTX 3080 FE", 410.0))])
    assert db.count_price_points() == 3
    assert [c.title for c in db.price_history("rtx 3080", exclude_ad_id="2")].count("RTX 3080 FE") == 0
    # no price, zero price, empty key: ignored
    assert db.add_price_points("rtx 3080", [Comparable(title="x", price=0.0)]) == 0
    assert db.add_price_points("", [ka("9", "RTX 3080", 300)]) == 0


def test_lookup_finds_other_spellings_but_not_other_products():
    db = Database()
    db.add_price_points("apple iphone 13 128gb", [ka("1", "Apple iPhone 13 128GB Blau", 450)])
    db.add_price_points("iphone13 128gb", [ka("2", "iPhone13 128GB", 440)])
    db.add_price_points("ipad 13", [ka("3", "iPad Air 13 Zoll", 500)])
    db.add_price_points("rtx 3080", [ka("4", "RTX 3080", 400)])
    titles = {c.title for c in db.price_history("iphone 13 128gb")}
    assert titles == {"Apple iPhone 13 128GB Blau", "iPhone13 128GB"}


def test_prune_removes_old_points_and_their_words():
    db = Database()
    db.add_price_points("rtx 3080", [ka("1", "RTX 3080", 400)], seen_at=utcnow() - timedelta(days=90))
    db.add_price_points("rtx 3080", [ka("2", "RTX 3080", 410)])
    assert db.prune_price_points(60) == 1
    assert [c.price for c in db.price_history("rtx 3080")] == [410]
    assert db._query("SELECT COUNT(*) FROM price_point_words WHERE point_id NOT IN"
                     " (SELECT id FROM price_points)")[0][0] == 0
    with pytest.raises(ValueError):
        db.prune_price_points()


def test_history_window_filters_by_seen_at():
    db = Database()
    db.add_price_points("rtx 3080", [ka("1", "RTX 3080", 400)], seen_at=utcnow() - timedelta(days=30))
    assert db.price_history("rtx 3080", utcnow() - timedelta(days=7)) == []
    assert len(db.price_history("rtx 3080", utcnow() - timedelta(days=60))) == 1


# ---------------------------------------------------------------------------- estimate


def test_estimate_from_history_needs_enough_relevant_points():
    points = [ka(str(i), f"RTX 3080 #{i}", 400 + i * 5) for i in range(5)]
    assert estimate_from_history("rtx 3080", points, min_points=6) is None
    points += [ka("90", "Gaming PC mit RTX 3080", 1400), ka("91", "RTX 3080 Ti", 600),
               ka("92", "Handyhülle RTX 3080 Motiv", 15)]
    assert estimate_from_history("rtx 3080", points, min_points=6) is None  # other products don't count
    points.append(ka("5", "RTX 3080 #5", 425))
    est = estimate_from_history("rtx 3080", points, min_points=6, days=60, asking_price_discount=0.85)
    assert est is not None and est.source == "history" and est.sample_size == 6
    assert est.market_price == pytest.approx(0.85 * 412.5, abs=0.01)
    assert est.notes.startswith("История: медиана 6 цен за 60 дней")
    assert est.history_days == 60 and est.query == "rtx 3080"


def test_history_keeps_sold_weighting_and_mentions_sales():
    points = [ka(str(i), "RTX 3080", 500) for i in range(3)] + [sold(i, "RTX 3080", 430) for i in range(3)]
    est = estimate_from_history("rtx 3080", points, min_points=6, asking_price_discount=0.8)
    # asking 500 * 0.8 = 400, sold 430 at face value -> median 415
    assert est.market_price == pytest.approx(415.0)
    assert "из них 3 продаж на eBay" in est.notes


def test_history_prefers_fresh_prices():
    now = utcnow()
    old = [(ka(str(i), "RTX 3080", 600), now - timedelta(days=50)) for i in range(5)]
    fresh = [(ka(str(10 + i), "RTX 3080", 400), now - timedelta(hours=2)) for i in range(4)]
    est = estimate_from_history("rtx 3080", old + fresh, min_points=6, asking_price_discount=1.0, now=now)
    assert est.market_price < 450  # 4 fresh prices outweigh 5 seven-week-old ones
    assert est.age_days is not None and est.age_days < 20
    assert "свежие цены весят больше" in est.notes
    plain = estimate_from_history("rtx 3080", [c for c, _ in old + fresh], min_points=6, asking_price_discount=1.0)
    assert plain.market_price == 600  # without dates: plain median


def test_history_wired_into_scoring():
    est = estimate_from_history("rtx 3080", [ka(str(i), "RTX 3080", 600 + i) for i in range(12)],
                                min_points=6, days=60, asking_price_discount=1.0)
    ev = evaluate(make_listing("1", "RTX 3080", 300.0), est, None, SearchConfig(name="s"), PricingConfig())
    assert ev.verdict == "buy" and "История: 12 цен за 60 дней" in ev.reasons
    ka_est = PriceEstimate(market_price=500, sample_size=10, source="kleinanzeigen")
    sold_est = PriceEstimate(market_price=480, sample_size=5, source="ebay_sold")
    assert combine_estimates(est, sold_est).source == "ebay_sold"
    assert combine_estimates(est, PriceEstimate(market_price=900, source="ai")).source == "history"
    assert combine_estimates(ka_est, est).market_price in (500, est.market_price)  # same rank


def test_only_real_offers_go_into_history_and_other_kinds_never_under_the_product():
    # v0.2 fix round: identity keys; bundles/accessories get a kind prefix instead of being dropped
    assert history_key_for(make_listing("1", "RTX 3080 Gaming", 400.0)) == "rtx|3080"
    assert history_key_for(make_listing("2", "RTX 3080", None)) is None
    assert history_key_for(make_listing("3", "RTX 3080", 1.0)) is None  # "1 € VB" placeholder
    assert history_key_for(make_listing("4", "RTX 3080", 0.0, is_free=True)) is None
    assert history_key_for(make_listing("5", "Suche RTX 3080", 300.0)) is None
    assert history_key_for(make_listing("6", "RTX 3080 defekt", 90.0)) is None
    assert history_key_for(make_listing("7", "Tablet zu verkaufen", 90.0)) is None  # names no product
    auction = make_listing("9", "RTX 3080", 50.0, buying_options=["AUCTION"])
    assert history_key_for(auction) is None  # current bid, not a price
    for title in ("PS5 + 2 Controller + 5 Spiele", "Hülle für iPhone 13", "Gaming PC Ryzen 7 + RTX 3080"):
        key = history_key_for(make_listing("8", title, 300.0))
        assert key is None or ":" in key, (title, key)  # never the bare product's key
    # parts missing that the price includes: not stored at all (they dragged "dewalt dcd796" down)
    kit_less = make_listing("10", "DeWalt DCD796", 90.0,
                            description="Verkaufe nur das Grundgerät, Akkus und Lader behalte ich.")
    assert not history_worthy(kit_less) and history_key_for(kit_less) is None
    assert history_key_for(make_listing("11", "DeWalt DCD796 solo", 90.0)) == "dewalt|dcd796|solo"
    phone = make_listing("12", "Apple iPhone 13 128GB", 400.0, description="Mit Ladekabel, ohne Netzteil.")
    assert history_key_for(phone) == "iphone|13||128gb"  # phones never come with a brick
    assert is_single_item("Lenovo ThinkPad T480 Laptop") and not is_single_item("Panzerglas iPhone 13")


# ---------------------------------------------------------------------------- monitor


def monitor_for(listings, *, search=None, sold_comps=None, **cfg):
    data = {
        "general": {"baseline_first_run": False, "max_new_per_search": 50},
        "searches": [search or {"name": "GPU", "query": "rtx 3080"}],
        "pricing": {"min_profit": 40, "min_roi": 0.25},
    }
    data.update(cfg)
    db = Database()
    source = FakeSource(listings)
    ebay = FakeSold(sold_comps or [])
    monitor = Monitor(parse_config(data), db, scraper=source, ebay=ebay, notifiers=[FakeNotifier()])
    return monitor, db, source, ebay


async def test_search_results_and_comparables_feed_the_history():
    comps = [sold(i, f"RTX 3080 Gaming {i}", 520 + 10 * i) for i in range(6)]
    comps.append(sold(99, "Gaming PC RTX 3080 Ryzen", 1500))  # another kind: never under the card
    listings = [make_listing("1", "MSI RTX 3080 Ventus", 480.0), make_listing("2", "Suche RTX 3080", 300.0)]
    monitor, db, _, ebay = monitor_for(listings, sold_comps=comps)
    await monitor.run_once()
    assert ebay.calls == 1
    titles = {c.title for c, _ in db.price_history_prefix("rtx|3080")}
    assert "MSI RTX 3080 Ventus" in titles and "RTX 3080 Gaming 0" in titles
    assert "Suche RTX 3080" not in titles and "Gaming PC RTX 3080 Ryzen" not in titles
    assert db.count_price_points("rtx|3080") == 7
    assert all(key.startswith("complete_pc:") for key in
               {r["product_key"] for r in db._query("SELECT product_key FROM price_points")} - {"rtx|3080"})


async def test_price_filtered_search_does_not_teach_the_history():
    listings = [make_listing(str(i), "RTX 3080", 300.0 + i) for i in range(3)]
    monitor, db, _, _ = monitor_for(listings, search={"name": "GPU", "query": "rtx 3080", "max_price": 350})
    await monitor.run_once()
    assert db.count_price_points() == 0
    url_search = {"name": "U", "url": "https://www.kleinanzeigen.de/s-berlin/preis::350/rtx-3080/k0l3331r20"}
    monitor, db, _, _ = monitor_for(listings, search=url_search)
    await monitor.run_once()
    assert db.count_price_points() == 0


async def test_history_estimate_replaces_the_comparables_lookup():
    monitor, db, _, ebay = monitor_for([make_listing("100", "Zotac RTX 3080 Trinity", 300.0)],
                                       sold_comps=[sold(1, "RTX 3080", 500)] * 3)
    db.add_price_points("rtx|3080", [ka(str(i), f"RTX 3080 #{i}", 560 + 5 * i) for i in range(8)])
    summary = await monitor.run_once()
    assert ebay.calls == 0  # no network lookup
    ev = db.get_evaluation("100")
    assert ev.estimate.source == "history" and ev.verdict == "buy"
    assert summary.history_hits == 1 and summary.comps_lookups == 0


async def test_history_can_be_switched_off():
    monitor, db, _, ebay = monitor_for([make_listing("100", "Zotac RTX 3080 Trinity", 300.0)],
                                       pricing={"use_price_history": False})
    db.add_price_points("rtx|3080", [ka(str(i), f"RTX 3080 #{i}", 560) for i in range(8)])
    await monitor.run_once()
    assert ebay.calls == 1 and db.get_evaluation("100").estimate.source != "history"


async def test_run_prunes_old_points():
    monitor, db, _, _ = monitor_for([], pricing={"history_days": 30})
    db.add_price_points("rtx 3080", [ka("1", "RTX 3080", 400)], seen_at=utcnow() - timedelta(days=45))
    db.add_price_points("rtx 3080", [ka("2", "RTX 3080", 400)], seen_at=utcnow() - timedelta(days=10))
    await monitor.run_once()
    assert db.count_price_points() == 1
    assert db.stats()["price_points_total"] == 1


# ---------------------------------------------------------------------------- pricing.identity


def _has_identity() -> bool:
    try:
        from ebeyparser.pricing import identity
    except ImportError:
        return False
    return hasattr(identity, "product_key") and hasattr(identity, "same_product")


needs_identity = pytest.mark.skipif(not _has_identity(), reason="pricing.identity not installed")


@needs_identity
def test_identity_unifies_history_keys_and_checks_variants():
    from ebeyparser.monitor import _is_model_key
    from ebeyparser.pricing.estimator import comparable_is_relevant, product_query

    assert product_query("Apple iPhone13 128 GB Blau") == product_query("iPhone 13 128GB") == "iphone 13 128gb"
    assert product_query("Bugaboo Kinderwagen") == "bugaboo kinderwagen"  # unknown product: title words
    assert not comparable_is_relevant("galaxy s21", "Samsung Galaxy S21 FE 128GB")  # Fan Edition != S21
    assert comparable_is_relevant("rtx 3080", "Gigabyte GeForce RTX 3080 Gaming OC 10G")
    assert _is_model_key("iphone 13 128gb") and not _is_model_key("bugaboo kinderwagen")


def test_single_item_uses_identity_kinds():
    assert is_single_item("iPhone 13") and is_single_item("Lenovo ThinkPad T480")  # a laptop is the item
    assert is_single_item("Apple Mac mini M1 8GB")  # so is a Mac mini
    assert not is_single_item("iPhone 13 Hülle") and not is_single_item("Gaming PC RTX 3080 Ryzen 5")
    assert not is_single_item("PS5 + 2 Controller + 5 Spiele")
