"""End-to-end pipeline tests with fake sources, AI and notifiers (no network)."""

from __future__ import annotations

import asyncio

import pytest

from ebeyparser.config import AppConfig, SearchConfig, parse_config
from ebeyparser.db import Database
from ebeyparser.models import AIVerdict, Comparable, Listing
from ebeyparser.monitor import AlreadyRunningError, Monitor, ad_id_from_url
from ebeyparser.scraper.http import BlockedError


def make_listing(ad_id: str, title: str, price: float | None, **kw) -> Listing:
    return Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-225-3331",
                   title=title, price=price, image_urls=["https://img.kleinanzeigen.de/x.jpg"], **kw)


class FakeSource:
    def __init__(self, listings: list[Listing], comps: list[Comparable] | None = None):
        self.listings = listings
        self.comps = comps or []
        self.detail_calls: list[str] = []
        self.search_calls = 0
        self.block_search = False

    async def search(self, search: SearchConfig, max_pages: int = 1) -> list[Listing]:
        self.search_calls += 1
        if self.block_search:
            raise BlockedError("captcha")
        return [l.model_copy(update={"search_name": search.name}) for l in self.listings]

    async def fetch_detail(self, listing: Listing) -> Listing:
        self.detail_calls.append(listing.ad_id)
        return listing.model_copy(update={
            "detail_loaded": True,
            "description": listing.description or "Funktioniert einwandfrei, kaum benutzt.",
        })

    async def comparables(self, query, *, exclude_ad_id=None, limit=30, min_price=None, max_price=None):
        return list(self.comps)

    async def download_images(self, listing: Listing, max_images: int = 3) -> list[bytes]:
        return [b"\xff\xd8\xffJPEG"]

    async def aclose(self):
        pass


class FakeSold:
    def __init__(self, comps: list[Comparable]):
        self.comps = comps
        self.calls = 0

    async def sold_comparables(self, query: str, limit: int = 30) -> list[Comparable]:
        self.calls += 1
        return list(self.comps)


class FakeEvaluator:
    def __init__(self, verdict: AIVerdict):
        self.verdict = verdict
        self.calls: list[str] = []

    async def evaluate(self, listing, images, *, purpose="resale", estimate=None, target_price=None):
        self.calls.append(listing.ad_id)
        return self.verdict


class FakeNotifier:
    name = "fake"

    def __init__(self, fail: bool = False):
        self.sent: list[list[str]] = []
        self.fail = fail

    async def send(self, deals, *, title=None):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append([d.listing.ad_id for d in deals])


SOLD = [Comparable(title=f"RTX 3080 #{i}", price=p, source="ebay_sold", sold=True)
        for i, p in enumerate([520, 540, 550, 560, 575, 590, 600, 610, 530, 545])]


def config(**overrides) -> AppConfig:
    data = {
        "general": {"max_new_per_search": 10},
        "searches": [{"name": "GPU", "query": "rtx 3080", "exclude_keywords": ["defekt"]}],
        "pricing": {"min_profit": 40, "min_roi": 0.25},
        "notifications": {"min_score": 50, "verdicts": ["buy"]},
    }
    for key, value in overrides.items():
        data[key] = value
    return parse_config(data)


def build(cfg: AppConfig, listings, *, verdict=None, notifier=None, sold=SOLD, **kw):
    db = Database()
    source = FakeSource(listings)
    notifier = notifier or FakeNotifier()
    monitor = Monitor(
        cfg, db, scraper=source, ebay=FakeSold(sold),
        evaluator=FakeEvaluator(verdict) if verdict else None,
        notifiers=[notifier], **kw,
    )
    return monitor, db, source, notifier


GOOD_AI = AIVerdict(product="NVIDIA RTX 3080 10GB", search_query="rtx 3080", verdict="buy",
                    confidence=0.8, photo_matches_description=True, condition="good",
                    reasoning="Реальные фото, состояние хорошее.", model="fake")


async def test_full_run_finds_deal_and_notifies_once():
    listings = [
        make_listing("1001", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0),
        make_listing("1002", "RTX 3080 FE", 560.0),              # market price -> no profit
        make_listing("1003", "Suche RTX 3080", 300.0),           # wanted ad -> prefiltered
        make_listing("1004", "RTX 3080 defekt", 80.0),           # excluded keyword
    ]
    monitor, db, source, notifier = build(config(), listings, verdict=GOOD_AI)
    summary = await monitor.run_once()

    assert summary.searches == 1 and summary.listings_seen == 4 and summary.new_listings == 4
    assert summary.evaluated == 4 and summary.deals_found == 1 and summary.notified == 1
    assert not summary.errors
    assert notifier.sent == [["1001"]]
    deal = db.get_deal("1001")
    assert deal.evaluation.verdict == "buy" and deal.evaluation.expected_profit > 100
    assert deal.evaluation.ai.model == "fake"
    assert deal.notified
    assert db.get_evaluation("1003").verdict == "skip"
    assert db.get_evaluation("1004").verdict == "skip"
    assert "1003" not in source.detail_calls  # prefilter happens before the detail request
    assert db.list_runs()[0].deals_found == 1

    # second pass: nothing new -> nothing evaluated or sent again
    again = await monitor.run_once()
    assert again.new_listings == 0 and again.evaluated == 0 and again.notified == 0
    assert notifier.sent == [["1001"]]


async def test_price_drop_triggers_reevaluation():
    listing = make_listing("2001", "RTX 3080 Ventus", 520.0)
    monitor, db, source, notifier = build(config(), [listing], verdict=GOOD_AI)
    await monitor.run_once()
    assert db.get_evaluation("2001").verdict != "buy"
    source.listings = [listing.model_copy(update={"price": 260.0})]
    summary = await monitor.run_once()
    assert summary.evaluated == 1
    assert db.get_evaluation("2001").verdict == "buy"
    assert db.get_listing("2001").price == 260.0
    assert notifier.sent == [["2001"]]


async def test_blocked_stops_run_and_records_error():
    cfg = config(searches=[{"name": "A", "query": "a"}, {"name": "B", "query": "b"}])
    monitor, db, source, _ = build(cfg, [])
    source.block_search = True
    summary = await monitor.run_once()
    assert source.search_calls == 1  # second search not attempted
    assert any("ограничил" in e for e in summary.errors)
    assert db.list_runs()[0].errors


async def test_notifier_failure_is_reported_not_marked():
    notifier = FakeNotifier(fail=True)
    monitor, db, _, _ = build(config(), [make_listing("3001", "RTX 3080", 250.0)],
                              verdict=GOOD_AI, notifier=notifier)
    summary = await monitor.run_once()
    assert summary.notified == 0
    assert any("smtp down" in e for e in summary.errors)
    assert not db.was_notified("3001")


async def test_digest_mode_sends_one_message_per_run():
    cfg = config(notifications={"min_score": 50, "mode": "digest"},
                 searches=[{"name": "A", "query": "rtx 3080"}, {"name": "B", "query": "rtx 3080"}])
    monitor, db, source, notifier = build(cfg, [make_listing("4001", "RTX 3080", 250.0)], verdict=GOOD_AI)
    await monitor.run_once()
    assert notifier.sent == [["4001"]]  # one digest for the whole run


async def test_reference_price_skips_comps_lookup():
    cfg = config(searches=[{"name": "GPU", "query": "rtx 3080", "reference_price": 600}])
    monitor, db, _, _ = build(cfg, [make_listing("5001", "RTX 3080", 250.0)], verdict=GOOD_AI)
    await monitor.run_once()
    assert monitor._ebay.calls == 0
    assert db.get_evaluation("5001").estimate.source == "reference"


async def test_ai_query_used_when_title_gives_no_estimate():
    cfg = config(pricing={"use_ebay_sold_comps": True, "use_kleinanzeigen_comps": False})
    monitor, db, _, _ = build(cfg, [make_listing("6001", "Grafikkarte zu verkaufen", 250.0)],
                              verdict=GOOD_AI, sold=[])
    # no comps for any query -> AI-only market price; evaluation must still complete
    await monitor.run_once()
    ev = db.get_evaluation("6001")
    assert ev is not None and ev.ai is not None


async def test_second_opinion_only_for_top_deals():
    second = FakeEvaluator(AIVerdict(verdict="skip", confidence=0.9, reasoning="Фото из интернета",
                                     photo_matches_description=False, model="claude-opus-5"))
    cfg = config(ai={"second_opinion": {"enabled": True, "min_score": 50, "max_per_run": 5}})
    listings = [make_listing("7001", "RTX 3080", 250.0), make_listing("7002", "RTX 3080", 560.0)]
    monitor, db, _, notifier = build(cfg, listings, verdict=GOOD_AI, second_evaluator=second)
    await monitor.run_once()
    assert second.calls == ["7001"]  # the overpriced one never reaches the paid model
    ev = db.get_evaluation("7001")
    assert ev.ai.model == "fake" and ev.ai_second.model == "claude-opus-5"
    assert ev.verdict == "skip"  # stronger model vetoed it
    assert notifier.sent == []


async def test_ebay_search_requires_credentials():
    cfg = config(searches=[{"name": "eBay", "source": "ebay", "query": "rtx 3080"}])
    monitor, db, _, _ = build(cfg, [])
    summary = await monitor.run_once()
    assert any("client_id" in e for e in summary.errors)


async def test_ebay_source_routed_to_api_client():
    ebay_listing = make_listing("ebay-123456789012", "RTX 3080 Auktion", 150.0).model_copy(
        update={"source": "ebay", "buying_options": ["FIXED_PRICE"], "shipping_cost": 5.0})
    api = FakeSource([ebay_listing])
    cfg = config(searches=[{"name": "eBay", "source": "ebay", "query": "rtx 3080"}])
    monitor, db, ka, _ = build(cfg, [], verdict=GOOD_AI, ebay_api=api)
    summary = await monitor.run_once()
    assert api.search_calls == 1 and ka.search_calls == 0
    assert api.detail_calls == ["ebay-123456789012"]
    assert summary.evaluated == 1
    assert db.get_listing("ebay-123456789012").source == "ebay"


async def test_no_searches_and_already_running():
    monitor, db, _, _ = build(config(searches=[]), [])
    summary = await monitor.run_once()
    assert summary.errors and not db.list_runs()
    monitor._running = True
    with pytest.raises(AlreadyRunningError):
        await monitor.run_once()


async def test_run_forever_stops_on_event():
    monitor, _, source, _ = build(config(general={"interval_minutes": 60}), [])
    stop = asyncio.Event()
    task = asyncio.create_task(monitor.run_forever(stop))
    await asyncio.sleep(0.05)
    assert source.search_calls == 1 and monitor.next_run_at is not None
    stop.set()
    await asyncio.wait_for(task, 1)
    assert monitor.next_run_at is None


async def test_evaluate_url():
    monitor, db, source, _ = build(config(), [], verdict=GOOD_AI)
    deal = await monitor.evaluate_url("https://www.kleinanzeigen.de/s-anzeige/rtx-3080/2891234567-225-3331")
    assert deal.listing.ad_id == "2891234567" and deal.evaluation is not None
    assert source.detail_calls == ["2891234567"]
    with pytest.raises(ValueError):
        await monitor.evaluate_url("https://example.com/nope")


def test_ad_id_from_url():
    assert ad_id_from_url("https://www.kleinanzeigen.de/s-anzeige/rtx/2891234567-225-3331") == "2891234567"
    assert ad_id_from_url("https://www.ebay.de/itm/123456789012?hash=x") == "ebay-123456789012"
    assert ad_id_from_url("https://www.ebay.de/itm/rtx-3080-gaming/123456789012") == "ebay-123456789012"
    assert ad_id_from_url("https://example.com") is None


def test_set_ai_model_in_config_only_touches_ai_block(tmp_path):
    from ebeyparser.cli import set_ai_model_in_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "general:\n  interval_minutes: 15\n"
        "ai:\n  enabled: true\n  provider: openai\n  model: qwen2.5-vl-7b-instruct   # comment\n"
        "  second_opinion:\n    model: claude-opus-5\n"
        "web:\n  port: 8000\n", encoding="utf-8")
    assert set_ai_model_in_config(cfg, "qwen/qwen2.5-vl-7b")
    text = cfg.read_text(encoding="utf-8")
    assert "  model: qwen/qwen2.5-vl-7b   # comment\n" in text
    assert "    model: claude-opus-5\n" in text
    assert not set_ai_model_in_config(tmp_path / "missing.yaml", "x")


def test_ai_check_lists_models_and_fixes_config(tmp_path, monkeypatch, capsys):
    from ebeyparser import cli

    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  enabled: true\n  provider: openai\n  model: qwen2.5-vl-7b-instruct\n", encoding="utf-8")

    async def fake_health(self):
        return {"ok": False, "server_ok": True, "provider": "openai", "base_url": "http://localhost:1234/v1",
                "model": "qwen2.5-vl-7b-instruct", "models": ["text-embedding-x", "google/gemma-3-12b"],
                "model_available": False, "error": "not loaded"}

    monkeypatch.setattr(Monitor, "ai_health", fake_health)
    assert cli.main(["-c", str(cfg), "ai-check"]) == 1
    out = capsys.readouterr().out
    assert "google/gemma-3-12b   ← умеет смотреть фото" in out and "ai-check --fix" in out
    assert cli.main(["-c", str(cfg), "ai-check", "--fix"]) == 0
    assert "  model: google/gemma-3-12b\n" in cfg.read_text(encoding="utf-8")


async def test_listings_stored_but_not_evaluated_are_retried():
    """A run interrupted (Ctrl+C) after storing listings must not lose them."""
    listing = make_listing("8001", "Gigabyte RTX 3080 Gaming OC", 250.0)
    monitor, db, source, notifier = build(config(), [listing], verdict=GOOD_AI)
    db.upsert_listing(listing.model_copy(update={"search_name": "GPU"}))  # seen, never evaluated
    summary = await monitor.run_once()
    assert summary.new_listings == 0 and summary.evaluated == 1
    assert db.get_evaluation("8001").verdict == "buy"
    assert notifier.sent == [["8001"]]
    again = await monitor.run_once()
    assert again.evaluated == 0  # evaluated ones are not repeated


def test_debug_ad_rejects_placeholder_url(capsys):
    from ebeyparser import cli

    assert cli.main(["-c", "/nonexistent.yaml", "debug-ad", "https://www.kleinanzeigen.de/s-anzeige/..."]) == 2
    assert "полная ссылка" in capsys.readouterr().out


def test_once_reevaluate_clears_old_verdicts(tmp_path, monkeypatch, capsys):
    from ebeyparser import cli
    from ebeyparser.models import Evaluation

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"general:\n  data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    db = Database(tmp_path / "data" / "ebeyparser.sqlite3")
    db.upsert_listing(make_listing("9001", "RTX 3080", 300.0))
    db.save_evaluation(Evaluation(ad_id="9001", verdict="skip"))
    db.close()

    async def fake_run_once(self):
        from ebeyparser.models import RunSummary
        return RunSummary()

    monkeypatch.setattr(Monitor, "run_once", fake_run_once)
    cli.main(["-c", str(cfg), "once", "--reevaluate"])
    assert "Сбросил старые оценки (1 шт.)" in capsys.readouterr().out
    assert Database(tmp_path / "data" / "ebeyparser.sqlite3").get_evaluation("9001") is None
