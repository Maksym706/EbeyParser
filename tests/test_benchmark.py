"""Benchmark on the synthetic Berlin market (ebeyparser/benchmark.py).

Plain tests = invariants that must hold now and forever (no crash, plainly stated severe traps
never "buy", report renders, the synthetic world is what it claims to be). Tests marked
xfail(strict=False) document quality targets; the lead flips them once the engine meets them.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

import pytest

from ebeyparser import benchmark as bm

N = 200  # small but covers every planted kind


@pytest.fixture(scope="module")
def oracle() -> bm.BenchmarkResult:
    return bm.run_benchmark(seed=1, n_listings=N, ai_mode="oracle")


@pytest.fixture(scope="module")
def no_ai() -> bm.BenchmarkResult:
    # worst case for the text rules: no vision model and eBay sold blocked (live 403)
    return bm.run_benchmark(seed=1, n_listings=N, ai_mode="off", ebay_sold=False, probe=False)


@pytest.fixture(scope="module")
def market() -> bm.Market:
    return bm.build_market(seed=1, n_listings=600)


# --------------------------------------------------------------- synthetic world


def test_catalog_is_broad_and_variants_are_priced_apart():
    assert len(bm.CATEGORIES) >= 12
    assert len(bm.PRODUCTS) >= 40
    assert {p.category for p in bm.PRODUCTS} == {c.key for c in bm.CATEGORIES}
    assert len({p.pid for p in bm.PRODUCTS}) == len(bm.PRODUCTS)
    for pricier, cheaper in bm.VARIANT_PAIRS:
        assert bm.PRODUCT_BY_ID[pricier].price > bm.PRODUCT_BY_ID[cheaper].price, (pricier, cheaper)
    by = bm.PRODUCT_BY_ID
    assert by["iphone13_256"].price > by["iphone13_128"].price  # storage matters
    assert by["rtx3080ti"].price > by["rtx3080"].price  # Ti matters
    assert by["iphone13pro_128"].price > by["iphone13_128"].price > by["iphone13mini_128"].price


def test_market_plants_every_kind_with_ground_truth(market: bm.Market):
    kinds = Counter((a.truth.kind, a.truth.flavour) for a in market.stream)
    for kind, flavour, _ in bm.STREAM_MIX:
        expected = "auction" if flavour.startswith("auction") else flavour
        assert kinds[(kind, expected)] > 0, (kind, flavour)
    assert kinds[("trap", "auction")] > 0 and kinds[("deal", "ebay")] > 0
    traps = {a.truth.trap for a in market.stream if a.truth.kind == "trap"}
    assert {"defect", "box_only", "locked", "wanted", "swap", "scam", "scam_cheap", "accessory", "bundle",
            "too_good", "variant", "auction"} <= traps
    assert {a.truth.clarity for a in market.stream if a.truth.kind == "trap"} == set(bm.CLARITIES)
    for ad in market.stream:
        assert ad.arrival >= 1 and ad.truth.pid in bm.PRODUCT_BY_ID
        if ad.source == "kleinanzeigen":
            assert 10000 <= int(ad.postal_code) <= 14999  # Berlin / Potsdam
    for ad in market.warmup:
        assert ad.arrival == 0  # online before monitoring starts (learning pass)
    deals = [a for a in market.stream if a.truth.kind == "deal" and a.truth.flavour in ("clean", "urgent", "glued")]
    assert deals and all(0.38 <= a.final_price / a.truth.value <= 0.7 for a in deals)
    normal = [a for a in market.stream if a.truth.flavour == "plain" and a.price]
    ratios = sorted(a.price / a.truth.value for a in normal)
    assert 0.9 < ratios[len(ratios) // 2] < 1.15  # asking prices mostly 85–125 % of market
    pricing = bm.benchmark_config().pricing
    assert sum(bm.is_true_deal(a, pricing) for a in market.stream) >= 40
    assert not any(bm.is_true_deal(a, pricing) for a in market.stream if a.truth.kind == "trap")


def test_market_is_deterministic():
    a, b = bm.build_market(seed=3, n_listings=150), bm.build_market(seed=3, n_listings=150)
    assert [(x.ad_id, x.title, x.price) for x in a.stream] == [(x.ad_id, x.title, x.price) for x in b.stream]
    c = bm.build_market(seed=4, n_listings=150)
    assert [x.title for x in a.stream] != [x.title for x in c.stream]


def test_fake_site_returns_look_alikes_so_relevance_filtering_is_tested(market: bm.Market):
    p = market.passes
    classes = Counter(bm.comp_class("rtx3080", a.truth.pid, a.truth.klass) for a in market.search_site("rtx 3080", p))
    assert {"ok", "variant", "pc", "laptop", "box", "wanted"} <= set(classes)  # 3080 Ti, PCs, empty boxes …
    classes = Counter(bm.comp_class("iphone13_128", a.truth.pid, a.truth.klass)
                      for a in market.search_site("iphone 13", p))
    assert {"ok", "variant", "storage", "accessory"} <= set(classes)  # Pro / mini / 256 GB / cases
    ka = bm.FakeKleinanzeigen(market)
    ka.current_pass = p
    comps = asyncio.run(ka.comparables("ps5", limit=50))
    assert comps and ka.calls["comparables"] == 1
    assert not any(c.title.lower().startswith("suche") for c in comps)  # like the real scraper


def test_fake_ebay_sold_can_be_blocked(market: bm.Market):
    from ebeyparser.scraper.http import BlockedError

    sold = bm.FakeEbaySold(market, enabled=False)
    with pytest.raises(BlockedError):
        asyncio.run(sold.sold_comparables("iphone 13"))
    assert sold.calls == 1
    ok = asyncio.run(bm.FakeEbaySold(market).sold_comparables("iphone 13 128gb"))
    assert ok and all(c.sold for c in ok)


def test_oracle_is_deterministic_and_flags_traps(market: bm.Market):
    ai = bm.OracleEvaluator(market, "oracle", seed=1, recall=1.0, noise=0.0)
    trap = next(a for a in market.stream if a.truth.trap == "defect")
    listing = trap.card(trap.arrival)
    v1 = asyncio.run(ai.evaluate(listing, [], purpose="resale", estimate=None, target_price=None))
    v2 = asyncio.run(ai.evaluate(listing, [], purpose="resale", estimate=None, target_price=None))
    assert v1 == v2 and v1.verdict == "skip" and v1.condition == "defective" and ai.calls == 2
    deal = next(a for a in market.stream if a.truth.kind == "deal" and a.truth.flavour == "clean")
    v = asyncio.run(ai.evaluate(deal.card(deal.arrival), [], purpose="resale"))
    assert v.verdict == "buy" and v.estimated_market_price == pytest.approx(deal.truth.value)


# ------------------------------------------------------------------ invariants


@pytest.mark.parametrize("which", ["oracle", "no_ai"])
def test_benchmark_runs_and_plain_severe_traps_are_never_buy(which: str, request: pytest.FixtureRequest):
    r: bm.BenchmarkResult = request.getfixturevalue(which)
    assert r.n_scored == N and r.passes_run >= r.passes
    assert sum(r.verdicts.values()) == N
    assert r.verdicts["none"] <= N * 0.05  # budgets may defer, drain passes must catch up
    assert r.true_deals > 0 and r.traps > 0
    assert r.calls["comparables"] > 0 and r.calls["details"] > 0
    assert (r.calls["ai"] > 0) == (r.ai_mode != "off")
    # plainly stated defect / box only / locked / wanted / swap / scam / fake / rent / 1 € must never be "buy"
    offenders = [m.title for m in r.mistakes if m.kind == "trap_buy" and "(" not in m.planted
                 and m.planted.split("/")[1] in bm.SEVERE_TRAPS]
    assert r.severe_plain_trap_buys == 0, offenders
    assert r.invariants_ok


def test_report_renders(oracle: bm.BenchmarkResult):
    text = bm.format_report(oracle)
    for part in ("Бенчмарк EbeyParser", "Точность «buy»", "Ловушки", "Оценка рынка", "Особые случаи",
                 "Коллизии товаров", "Воронка", "Худшие ошибки", "Инвариант"):
        assert part in text, part
    assert oracle.collisions_probe.get("ok", {}).get("returned", 0) > 0
    assert oracle.as_dict()["n_scored"] == N  # JSON-able summary


def test_ai_down_never_produces_buy():
    """Vision model unreachable (confidence 0): photos unchecked -> never "buy"."""
    r = bm.run_benchmark(seed=2, n_listings=120, ai_mode="down", probe=False)
    assert r.checks["ai_down"]["evaluated"] > 0
    assert r.checks["ai_down"].get("buy", 0) == 0 and r.buys == 0
    assert r.severe_plain_trap_buys == 0


def test_same_seed_same_result():
    a = bm.run_benchmark(seed=5, n_listings=80, ai_mode="oracle", probe=False)
    b = bm.run_benchmark(seed=5, n_listings=80, ai_mode="oracle", probe=False)
    assert (a.verdicts, a.buys, a.correct_buys, a.est_median_error) == (b.verdicts, b.buys, b.correct_buys,
                                                                        b.est_median_error)


def test_run_cli_prints_report(capsys: pytest.CaptureFixture[str]):
    args = argparse.Namespace(seed=2, n=60, ai="off", no_ebay=True, no_categories=False, passes=None, top=5,
                              json=None)
    assert bm.run_cli(args) == 0
    out = capsys.readouterr().out
    assert "Бенчмарк EbeyParser" in out and "eBay sold: выкл" in out


# ------------------------------------------------------- quality targets (xfail)


def test_quality_targets(oracle: bm.BenchmarkResult):
    # met since the v0.2 fix round (buy/notification precision 100 %, recall ~90 %): now a regression test
    assert oracle.precision is not None and oracle.precision >= bm.TARGET_PRECISION
    assert oracle.recall is not None and oracle.recall >= bm.TARGET_RECALL
    assert oracle.est_median_error is not None and oracle.est_median_error <= bm.TARGET_MEDIAN_ERROR
    assert oracle.notify_precision is not None and oracle.notify_precision >= bm.TARGET_PRECISION


@pytest.mark.xfail(strict=False, reason="quality target: no trap of any kind/wording is ever 'buy'")
@pytest.mark.parametrize("which", ["oracle", "no_ai"])
def test_no_trap_is_ever_buy(which: str, request: pytest.FixtureRequest):
    r: bm.BenchmarkResult = request.getfixturevalue(which)
    assert r.trap_buys == 0, [(m.title, m.planted) for m in r.mistakes if m.kind == "trap_buy"]


@pytest.mark.xfail(strict=False, reason="quality target: special cases")
def test_special_cases(oracle: bm.BenchmarkResult):
    c = oracle.checks
    assert c["auction"].get("total", 0) > 0 and c["auction"].get("buy", 0) == 0  # a low bid is not a price
    assert c["scam_cheap"].get("skip", 0) == c["scam_cheap"].get("total", 0) > 0  # too cheap + ship only
    assert c["disclaimer"].get("defect_flag", 1) == 0  # "keine Rücknahme bei Defekten" is boilerplate
    assert c["haggle"].get("action_haggle", 0) == c["haggle"].get("total", -1)  # VB: suggest an offer


@pytest.mark.xfail(strict=False, reason="quality target: collisions")
def test_relevance_filter_drops_other_products(oracle: bm.BenchmarkResult):
    probe = oracle.collisions_probe
    for klass in ("variant", "pc", "laptop", "part", "bundle", "controller", "games", "wanted", "defect"):
        row = probe.get(klass)
        assert row is None or row["kept_share"] <= 0.05, (klass, row and row["examples"])
