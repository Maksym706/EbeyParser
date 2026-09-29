"""AI scout, stage B/D building blocks: product keys for what the model named, grounding against
the ad text (no inventions, no negated parts), bundle valuation from real prices, the scout
plan, promotion rules, vision normalisation, alert tiers, the learning-loop hints and the
status texts. Pure functions, no network."""

from __future__ import annotations

import pytest

from ebeyparser.ai import scout
from ebeyparser.ai.triage import TriageItem
from ebeyparser.config import SuperDealsConfig
from ebeyparser.models import AIVerdict, Comparable, Evaluation, Listing, PriceEstimate
from ebeyparser.pricing.ai_key import ai_key, ai_keys_compatible, grounded, resolve, split_quantity
from ebeyparser.pricing.bundle import Component, value_bundle
from ebeyparser.pricing.tiers import deal_tier, is_super


def est(price: float, n: int = 10, source: str = "history") -> PriceEstimate:
    return PriceEstimate(market_price=price, low=price * 0.9, high=price * 1.1, sample_size=n, source=source,
                         comparables=[Comparable(title=f"x {i}", price=price) for i in range(3)], age_days=2.0,
                         history_days=60)


# -------------------------------------------------------------------- keys


def test_resolve_prefers_identity_keys():
    ref = resolve("Apple iPhone 13 Pro 256GB")
    assert ref is not None and not ref.is_ai and ref.key == "iphone|13|pro|256gb" and ref.lookup == "iphone|13|pro"
    gpu = resolve("NVIDIA RTX 3080 10GB")
    assert gpu.key == "rtx|3080" and gpu.category == "gpu"


def test_resolve_falls_back_to_normalized_ai_keys():
    assert resolve("Bose SoundLink Flex Schwarz").key == "ai:soundlink flex"
    assert resolve("Anker PowerCore 20000mAh").key == "ai:anker powercore|20000mah"
    # identity's catch-all "word + number" key is the one the script's history uses: shared
    assert resolve("Garmin Fenix 7").key == "fenix|7" and not resolve("Garmin Fenix 7").is_ai
    assert resolve("") is None and resolve("   ") is None and ai_key("Gebraucht") is None


def test_ai_key_attributes_and_compatibility():
    assert ai_key("Lego Millennium Falcon 75192") == "ai:millennium falcon 75192"
    assert ai_keys_compatible("ai:tm6", "ai:tm6") and ai_keys_compatible("ai:ssd x|1tb", "ai:ssd x")
    assert not ai_keys_compatible("ai:ssd x|1tb", "ai:ssd x|2tb") and not ai_keys_compatible("ai:a", "ai:b")


def test_split_quantity():
    assert split_quantity("2x DualSense Controller") == (2, "DualSense Controller")
    assert split_quantity("3 Stück Joy-Con") == (3, "Joy-Con")
    assert split_quantity("Controller x2") == (2, "Controller")
    assert split_quantity("RTX 3070") == (1, "RTX 3070")


# --------------------------------------------------------------- grounding


@pytest.mark.parametrize("product,text,expected", [
    ("NVIDIA RTX 3080 10GB", "Alter Gaming PC, i7, RTX 3080, 16GB RAM", True),
    ("NVIDIA RTX 3080", "Grafikkarte von Nvidia, auf der Packung steht 3080", True),
    ("Apple iPhone 13 128GB", "Iphne 13 128gb Akku 88%", True),  # one typo in the product line
    ("Samsung Galaxy S22", "Samsnug Galxy S22 128GB", True),
    ("Intel Core i7-8700K", "PC mit 8700k und 1070", True),  # the series "i7" is optional
    ("Sony PlayStation 5", "Playstaion 5 mit 2 Controllern", True),
    ("NVIDIA RTX 3080", "RTX 3070 mit Kühler", False),  # model numbers must be exact
    ("Google Pixel 7", "iPhone 7 32GB", False),  # a bare "7" needs its product line
    ("Apple iPhone 13 Pro", "iPhone 13 128GB", False),  # an edition the ad doesn't state
    ("Apple iPhone 13 256GB", "iPhone 13 128GB", False),  # a storage size the ad doesn't state
    ("NVIDIA RTX 3080 10GB", "RTX 3080 12GB", False),  # another size of the same product
    ("NVIDIA RTX 3080 10GB", "PC mit RTX 3080 und 32GB DDR4", True),  # that size is the PC's RAM
    ("Apple iPhone 13 128GB", "iPhone 13, sehr gut", True),  # no size stated at all
    ("NVIDIA RTX 3080", "Gaming PC ohne Grafikkarte (war RTX 3080 drin)", False),
    ("NVIDIA RTX 3080", "RTX 3080 schon verkauft, Rest da", False),
    ("NVIDIA RTX 3080", "Gaming PC, GTX 1060, suche eigentlich was mit RTX 3080", False),
    ("NVIDIA RTX 3080", "Gaming PC ohne Monitor, RTX 3080, i7", True),  # a negation stays in its clause
    ("NVIDIA RTX 3080", "RTX 3080 verbaut, läuft", True),
    ("Bose SoundLink Flex", "JBL Lautsprecher", False),
])
def test_grounded(product, text, expected):
    assert grounded(product, text) is expected


# ------------------------------------------------------------------ bundles


def comp(name: str, price: float | None, qty: int = 1, n: int = 10) -> Component:
    return Component(name=name, qty=qty, ref=resolve(name), estimate=est(price, n) if price else None)


def test_bundle_value_is_the_discounted_sum_of_priced_parts():
    value = value_bundle([comp("RTX 3070", 270.0), comp("AMD Ryzen 5 3600", 60.0), comp("16GB DDR4 RAM", None),
                          comp("2x DualSense Controller", 45.0, qty=2)], discount=0.25)
    e = value.estimate
    assert e is not None and e.market_price == pytest.approx((270 + 60 + 90) * 0.75)
    assert e.source == "history" and e.sample_size == 10 and "RTX 3070" in e.notes and "25 %" in e.notes
    assert [c.name for c in value.unpriced] == ["16GB DDR4 RAM"]  # RAM is a minor part: no veto


def test_bundle_needs_enough_priced_major_parts():
    thin = value_bundle([comp("RTX 3070", 270.0), comp("Intel Core i7-8700K", None), comp("AMD Ryzen 5 3600", None)],
                        min_priced_share=0.5)
    assert thin.estimate is None and "нет цен" in thin.reason
    assert value_bundle([comp("Gehäuse", None)]).estimate is None
    ungrounded = comp("RTX 3090", 700.0)
    ungrounded.grounded = False
    assert value_bundle([ungrounded, comp("RTX 3060", 200.0)], discount=0.0).estimate.market_price == 200.0


def test_minor_parts():
    assert not Component(name="16GB DDR4 RAM", ref=resolve("16GB DDR4 RAM")).major
    assert not Component(name="Netzteil 650W").major and not Component(name="512GB SSD").major
    assert Component(name="RTX 3070", ref=resolve("RTX 3070")).major
    assert Component(name="Intel Core i7-8700K", ref=resolve("Intel Core i7-8700K")).major


# -------------------------------------------------------------------- plans


def listing(title: str, price: float | None, text: str = "") -> Listing:
    return Listing(ad_id="1", url="https://www.kleinanzeigen.de/s-anzeige/x/1", title=title, price=price,
                   description=text)


def lookup_from(prices: dict[str, float]):
    def lookup(ref):
        for key, price in prices.items():
            if ref.key.startswith(key):
                return est(price)
        return None
    return lookup


def test_plan_prices_a_pc_by_its_grounded_parts():
    ad = listing("Alter Rechner", 150.0, "Drin: RTX 3080, Intel Core i7-8700K, 16GB RAM. Ohne Monitor.")
    item = TriageItem(ad_id="1", kind="pc", contents=["RTX 3080", "Intel Core i7-8700K", "16GB DDR4 RAM",
                                                      "RTX 4090"], interest=9, reason="старый ПК с RTX 3080")
    plan = scout.plan(ad, item, lookup_from({"rtx|3080": 400.0, "rtx|4090": 1500.0}), pc_discount=0.25)
    parts = {c.name: c for c in plan.components}
    assert parts["RTX 4090"].grounded is False and parts["RTX 4090"].estimate is None  # invented: ignored
    assert plan.estimate.market_price == pytest.approx(300.0) and plan.usable and plan.grounded
    assert plan.priority(ad) == pytest.approx(0.5) and plan.reason_ru() == "старый ПК с RTX 3080"
    assert scout.unpriced_parts(plan)[0].name == "Intel Core i7-8700K"
    assert scout.worth_a_look(plan, deal_math=lambda e: e.market_price > 200, min_interest=6)


def test_plan_single_with_typo_and_quantity():
    ad = listing("Iphne 13 128gb", 250.0, "2 Stück, beide mit Hülle")
    item = TriageItem(ad_id="1", kind="single", product="Apple iPhone 13 128GB", qty=2, interest=7,
                      query="iphone 13 128gb")
    plan = scout.plan(ad, item, lookup_from({"iphone|13": 340.0}))
    assert plan.identified and plan.ref.key == "iphone|13||128gb" and plan.estimate.market_price == 680.0
    assert scout.record_rows(ad, plan) == []  # two phones: not a single price point
    one = scout.plan(ad, item.model_copy(update={"qty": 1}), lookup_from({"iphone|13": 340.0}))
    assert scout.record_rows(ad, one) == [("iphone|13||128gb", ad)]


def test_plan_ignores_inventions_and_blocks_risks():
    ad = listing("Handy", 100.0, "Samsung Galaxy S21 128GB")
    invented = scout.plan(ad, TriageItem(kind="single", product="Apple iPhone 15 Pro", interest=9), lookup_from({}))
    assert not invented.grounded and not invented.usable
    assert not scout.worth_a_look(invented, deal_math=lambda e: True, min_interest=6)
    scam = scout.plan(ad, TriageItem(kind="single", product="Samsung Galaxy S21 128GB", risks=["scam"], interest=9),
                      lookup_from({"galaxy s|s21": 200.0}))
    assert scam.blocked and not scam.usable and scam.priority(ad) == scout.DEMOTED_PRIORITY
    wanted = scout.plan(ad, TriageItem(kind="wanted", product="Samsung Galaxy S21", interest=0), lookup_from({}))
    assert wanted.blocked.startswith("вид объявления")
    fallback = scout.plan(ad, TriageItem(kind="single", product="Samsung Galaxy S21", source="script"), lookup_from({}))
    assert not fallback.usable and fallback.priority(ad) == scout.UNKNOWN_PRIORITY


def test_worth_a_look_rules():
    ad = listing("Handy", 100.0, "Google Pixel 7 128GB")
    item = TriageItem(kind="single", product="Google Pixel 7 128GB", query="pixel 7 128gb", interest=6)
    no_price = scout.plan(ad, item, lookup_from({}))
    assert scout.worth_a_look(no_price, deal_math=lambda e: False, min_interest=6)  # comparables will tell
    assert not scout.worth_a_look(no_price, deal_math=lambda e: False, min_interest=7)
    priced = scout.plan(ad, item, lookup_from({"pixel|7": 220.0}))
    assert scout.worth_a_look(priced, deal_math=lambda e: True, min_interest=6)
    assert not scout.worth_a_look(priced, deal_math=lambda e: False, min_interest=6)  # history says no deal


def test_interest_is_only_a_tie_breaker():
    ad = listing("Handy", 100.0, "Google Pixel 7 128GB")
    low = scout.plan(ad, TriageItem(kind="single", product="Google Pixel 7 128GB", query="pixel 7 128gb", interest=0),
                     lookup_from({}))
    high = scout.plan(ad, low.item.model_copy(update={"interest": 10}), lookup_from({}))
    # a weak model's score filters nothing by default: the comparables lookup decides
    assert scout.worth_a_look(low, deal_math=lambda e: False) and scout.worth_a_look(high, deal_math=lambda e: False)
    assert high.priority(ad) < low.priority(ad) and low.priority(ad) - high.priority(ad) <= 0.03
    priced = scout.plan(ad, low.item, lookup_from({"pixel|7": 220.0}))
    assert priced.priority(ad) < high.priority(ad)  # a real price beats any interest
    # a lot with nothing to price: no data, no promotion — whatever the interest says
    lot = scout.plan(listing("Kiste vom Dachboden", 40.0, "Alte Kabel, eine Lampe und Spielzeug"),
                     TriageItem(kind="lot", contents=["Kabel", "Lampe"], interest=10), lookup_from({}))
    assert lot.usable and not scout.worth_a_look(lot, deal_math=lambda e: True)


@pytest.mark.parametrize("text", [
    "RTX 3080 10GB, Kontakt nur per WhatsApp",
    "RTX 3080 10GB, schreib mir auf Telegram",
    "RTX 3080 10GB, bitte an max.muster@gmail.com schreiben",
    "RTX 3080 10GB. Sicher bezahlen: ich schicke dir den Link",
    "RTX 3080 10GB, Zahlung per Vorkasse",
    "RTX 3080 10GB, nur PayPal Freunde",
    "RTX 3080 10GB, Western Union",
    "RTX 3080 10GB, nur Versand",
])
def test_code_red_flags_back_up_the_model(text):
    """A 2B missed the WhatsApp / e-mail / «Sicher bezahlen» scams: the code's own rules block them."""
    clean = TriageItem(kind="single", product="NVIDIA RTX 3080", query="rtx 3080", interest=9)  # no risk tag
    ad = listing("Grafikkarte", 200.0, text)
    plan = scout.plan(ad, clean, lookup_from({"rtx|3080": 600.0}))
    assert plan.blocked.startswith("признаки") and not plan.usable
    assert not scout.worth_a_look(plan, deal_math=lambda e: True) and plan.priority(ad) == scout.DEMOTED_PRIORITY


def test_code_red_flags_leave_honest_ads_and_ebay_shipping_alone():
    clean = TriageItem(kind="single", product="NVIDIA RTX 3080", query="rtx 3080", interest=9)
    honest = scout.plan(listing("Grafikkarte", 200.0, "RTX 3080 10GB, Abholung in Berlin, Sicher bezahlen möglich"),
                        clean, lookup_from({"rtx|3080": 600.0}))
    assert not honest.blocked and scout.worth_a_look(honest, deal_math=lambda e: True)
    ebay = Listing(ad_id="2", url="https://www.ebay.de/itm/2", title="Grafikkarte", price=200.0, source="ebay",
                   description="RTX 3080 10GB, nur Versand")
    assert not scout.plan(ebay, clean, lookup_from({"rtx|3080": 600.0})).blocked  # eBay ships, buyer protection


def test_vision_normalisation_and_disagreement():
    ad = listing("Alter PC", 150.0, "RTX 3080 drin")
    pc = scout.plan(ad, TriageItem(kind="pc", contents=["RTX 3080"], interest=9), lookup_from({"rtx|3080": 400.0}))
    verdict = AIVerdict(item_type="complete_pc", confidence=0.8, product="Gaming PC")
    assert scout.vision_for_plan(verdict, pc).item_type == "bundle"
    assert scout.vision_for_plan(verdict.model_copy(update={"item_type": "box_only"}), pc).item_type == "box_only"
    assert scout.vision_for_plan(verdict, None).item_type == "complete_pc"
    single_ad = listing("Grafikkarte", 200.0, "RTX 3080 10GB")
    single = scout.plan(single_ad, TriageItem(kind="single", product="NVIDIA RTX 3080", interest=8),
                        lookup_from({"rtx|3080": 400.0}))
    assert "3060" in scout.vision_disagrees(AIVerdict(product="RTX 3060", confidence=0.8), single)
    assert scout.vision_disagrees(AIVerdict(product="RTX 3080 Gaming OC", confidence=0.8), single) == ""
    assert scout.vision_disagrees(AIVerdict(product="RTX 3060", confidence=0.0), single) == ""


# -------------------------------------------------------------------- tiers


def ev(**kw) -> Evaluation:
    base = dict(ad_id="1", verdict="buy", action="buy", ai_checked=True, expected_profit=200.0, roi=1.2, score=92.0,
                estimate=est(500.0))
    return Evaluation(**{**base, **kw})


def test_super_tier_needs_everything():
    cfg = SuperDealsConfig()
    assert is_super(ev(), cfg) and deal_tier(ev(), cfg) == "super"
    for bad in (dict(ai_checked=False), dict(red_flags=["Обратить внимание: x"]), dict(expected_profit=90.0),
                dict(roi=0.5), dict(score=80.0), dict(no_alert=True), dict(estimate=est(500.0, source="ai"))):
        assert deal_tier(ev(**bad), cfg) == "deal", bad
    assert deal_tier(ev(), SuperDealsConfig(enabled=False)) == "deal" and deal_tier(ev(), None) == "deal"
    assert deal_tier(ev(verdict="maybe", would_buy=True, ai_checked=False), cfg) == "unchecked"
    assert deal_tier(ev(verdict="maybe"), cfg) == "maybe" and deal_tier(ev(verdict="skip"), cfg) == "skip"
    assert deal_tier(None) == ""


# ------------------------------------------------------- learning loop, status


def test_feedback_hints_are_short_and_learnable():
    text = scout.feedback_hints({
        "good": [{"title": "Alter PC mit RTX 3070", "bought": 150.0, "sold": 390.0}],
        "hidden": [{"title": "Kinderwagen", "reason": "не интересно"}, {"title": "x" * 300, "reason": None}],
    })
    assert "good buy: Alter PC mit RTX 3070 (bought 150 €, sold 390 €)" in text
    assert "not interesting: Kinderwagen (не интересно)" in text and len(text) <= scout.HINTS_LIMIT
    assert scout.feedback_hints({"good": [], "hidden": []}) == ""


def test_status_view_texts():
    common = dict(mode_setting="auto", provider="openai", base_url="http://nas:8080/v1", model="qwen3.5-2b",
                  own_endpoint=True, vision_waiting=0, vision_wait_minutes=45)
    off = scout.status_view(enabled=False, snap={}, **common)
    assert off["state"] == "off" and "выключен" in off["text_ru"]
    snap = {"seen_last_hour": 120, "triaged_last_hour": 90, "sec_per_ad": 5.0, "capacity_per_hour": 360,
            "mode": "candidates", "batch_size": 8}
    on = scout.status_view(enabled=True, snap=snap, **{**common, "vision_waiting": 2})
    assert on["state"] == "behind" and on["text_ru"] == "Успевает смотреть 90 из 120 новых объявлений в час"
    assert on["speed_ru"] == "≈ 5.0 с на объявление, до 360 объявлений в час" and on["mode"] == "candidates"
    assert on["vision_queue"]["waiting"] == 2 and "2" in on["vision_queue"]["text_ru"]
    assert not on["speed_expected"] and not on["too_small"]
    # not measured yet: the model research's speed for this model
    fresh = scout.status_view(enabled=True, snap={}, expected_sec_per_ad=6.0, pass_share=0.5, max_per_hour=600,
                              **common)
    assert fresh["speed_expected"] and fresh["speed_ru"] == (
        "Ожидается ≈ 6.0 с на объявление, до 300 объявлений в час (пока не измерено)")
    small = scout.status_view(enabled=True, snap={}, too_small=True, **{**common, "model": "qwen3.5:0.8b"})
    assert small["state"] == "too_small" and "слишком маленькая" in small["text_ru"] and "qwen3.5:0.8b" in small["text_ru"]
    assert small["speed_ru"] == ""
    down = scout.status_view(enabled=True, snap={**snap, "last_error": "x", "last_error_at": 10.0, "last_ok_at": 5.0},
                             **common)
    assert down["state"] == "down" and "не отвечает" in down["text_ru"]
