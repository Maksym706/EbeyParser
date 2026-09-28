"""v0.2 hard rules in evaluate(): money-losing alerts must not happen."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ebeyparser.config import PricingConfig, SearchConfig
from ebeyparser.models import AIVerdict, Evaluation, Listing, PriceEstimate, utcnow
from ebeyparser.pricing.estimator import (
    evaluate,
    market_says_no_deal,
    no_deal_reasons,
    prefilter_score,
)
from ebeyparser.pricing.text import detect_red_flags, is_remote_only

RESALE = SearchConfig(name="gpu")
PRICING = PricingConfig()
SURE_AI = AIVerdict(verdict="buy", confidence=0.8, photo_matches_description=True, condition="good",
                    item_type="single")


def listing(**kw) -> Listing:
    data = {"ad_id": "1", "url": "https://example.org/1", "title": "MSI RTX 3080 Ventus 10GB"}
    data.update(kw)
    return Listing(**data)


def market(price: float, n: int = 15, source: str = "ebay_sold", spread: float = 0.2) -> PriceEstimate:
    return PriceEstimate(market_price=price, low=price * (1 - spread / 2), high=price * (1 + spread / 2),
                         sample_size=n, source=source)


# 1 — auctions --------------------------------------------------------------------------


def test_auction_is_a_bid_never_a_buy():
    now = utcnow()
    ad = listing(price=50, buying_options=["AUCTION"], bid_count=2, ends_at=now + timedelta(hours=1))
    ev = evaluate(ad, market(400), SURE_AI, RESALE, PRICING, now=now)
    assert ev.verdict == "maybe" and ev.action == "bid"
    assert ev.expected_profit is None and ev.roi is None
    assert ev.max_buy_price == 288  # 400 - 10 % margin = 360 -> min(320, 288)
    assert "Аукцион: ставь максимум 288 € (сейчас 50 €)" in ev.reasons
    assert ev.score <= 60


def test_auction_score_follows_headroom_and_hopeless_bid_is_skip():
    now = utcnow()
    ends = now + timedelta(hours=1)
    low = evaluate(listing(price=50, buying_options=["AUCTION"], ends_at=ends), market(400), None, RESALE, PRICING, now=now)
    high = evaluate(listing(price=250, buying_options=["AUCTION"], ends_at=ends), market(400), None, RESALE, PRICING,
                    now=now)
    assert low.score > high.score > 0
    over = evaluate(listing(price=300, buying_options=["AUCTION"], ends_at=ends), market(400), None, RESALE, PRICING,
                    now=now)
    assert over.verdict == "skip" and over.action == "skip"
    assert market_says_no_deal(over) and no_deal_reasons(over)[0].startswith("Ставка уже выше")


def test_prefilter_score_of_auctions_uses_the_room_to_the_max_bid():
    assert prefilter_score(listing(price=50, buying_options=["AUCTION"]), market(400), RESALE, PRICING) > 50
    assert prefilter_score(listing(price=300, buying_options=["AUCTION"]), market(400), RESALE, PRICING) == 0


# 2 — too good to be true ------------------------------------------------------------------


def test_too_cheap_is_at_most_maybe_and_watch():
    ev = evaluate(listing(price=110), market(450), SURE_AI, RESALE, PRICING)
    assert ev.verdict == "maybe" and ev.action == "watch"
    assert "⚠ Подозрительно дёшево — проверь, не развод" in ev.reasons


@pytest.mark.parametrize("text", ["Nur Versand, keine Abholung", "Zahlung nur PayPal", "Nur Vorkasse"])
def test_too_cheap_and_remote_only_is_a_scam(text):
    ev = evaluate(listing(price=110, description=text), market(450), SURE_AI, RESALE, PRICING)
    assert ev.verdict == "skip"
    assert any("похоже на развод" in r for r in ev.reasons)


def test_remote_only_alone_is_fine():
    ev = evaluate(listing(price=250, description="Nur Versand"), market(600), None, RESALE, PRICING)
    assert ev.verdict == "buy"
    assert is_remote_only("Nur Versand") and not is_remote_only("Versand oder Abholung in Berlin")
    assert not is_remote_only("Keine Vorkasse, nur Abholung")


# 3 — AI-only market price -------------------------------------------------------------------


def test_ai_only_market_is_never_notify_worthy():
    ai = SURE_AI.model_copy(update={"estimated_market_price": 400.0})
    ev = evaluate(listing(price=150), PriceEstimate(), ai, RESALE, PRICING)
    assert ev.verdict == "maybe" and ev.score <= 55 and ev.no_alert
    assert ev.expected_profit is not None


# 4 — weak evidence / 10 — calibration ---------------------------------------------------------


def test_few_comparables_cap_at_maybe_and_cannot_beat_many():
    few = evaluate(listing(price=200), market(400, n=3, source="kleinanzeigen"), None, RESALE, PRICING)
    many = evaluate(listing(price=200), market(400, n=12, source="kleinanzeigen"), None, RESALE, PRICING)
    assert few.verdict == "maybe" and many.verdict == "buy"
    assert few.score < many.score
    assert any(r.startswith("⚠ Мало данных о рынке: 3 цены") for r in few.reasons)


def test_scattered_market_caps_at_maybe():
    ev = evaluate(listing(price=250), market(600, spread=0.6), None, RESALE, PRICING)
    assert ev.verdict == "maybe" and any("сильно разбросаны" in r for r in ev.reasons)


def test_meeting_the_targets_with_good_confidence_scores_at_least_70():
    # net = 0.9 * 222.23 = 200 -> buying at 160 gives exactly 40 € and 25 %
    ev = evaluate(listing(price=160), market(222.23, n=10), None, RESALE, PRICING)
    assert ev.verdict == "buy" and ev.score >= 70
    bigger = evaluate(listing(price=100), market(222.23, n=10), None, RESALE, PRICING)
    assert bigger.score > ev.score


# 5 — AI unavailable ----------------------------------------------------------------------------


def test_ai_unavailable_caps_at_maybe():
    down = AIVerdict(verdict="maybe", confidence=0.0)
    ev = evaluate(listing(price=250), market(600), down, RESALE, PRICING, ai_expected=True)
    assert ev.verdict == "maybe" and ev.ai_checked is False
    assert "⚠ Фото НЕ проверены ИИ (нейросеть не ответила)" in ev.reasons
    ok = evaluate(listing(price=250), market(600), SURE_AI, RESALE, PRICING, ai_expected=True)
    assert ok.verdict == "buy" and ok.ai_checked is True
    off = evaluate(listing(price=250), market(600), None, RESALE, PRICING)
    assert off.verdict == "buy" and off.ai_checked is None


# 6 — reserved / deleted, tags + condition ----------------------------------------------------------


def test_reserved_is_maybe_without_alert_and_deleted_is_skip():
    ev = evaluate(listing(price=250, tags=["Reserviert"]), market(600), None, RESALE, PRICING)
    assert ev.verdict == "maybe" and ev.no_alert and ev.action == "watch"
    assert evaluate(listing(price=250, tags=["Gelöscht"]), market(600), None, RESALE, PRICING).verdict == "skip"
    # "Daten gelöscht" in the text is a wiped phone, not a deleted ad
    wiped = evaluate(listing(price=250, description="Alle Daten gelöscht"), market(600), None, RESALE, PRICING)
    assert wiped.verdict == "buy"


def test_condition_is_part_of_the_red_flag_text():
    ev = evaluate(listing(price=250, condition="Defekt"), market(600), None, RESALE, PRICING)
    assert ev.verdict == "skip"


# 7 — AI structured vetoes -----------------------------------------------------------------------


@pytest.mark.parametrize("item_type", ["bundle", "complete_pc", "part", "accessory", "box_only", "wanted"])
def test_ai_item_type_vetoes(item_type):
    ai = SURE_AI.model_copy(update={"item_type": item_type})
    assert evaluate(listing(price=250), market(600), ai, RESALE, PRICING).verdict == "skip"
    unsure = ai.model_copy(update={"confidence": 0.4})
    assert evaluate(listing(price=250), market(600), unsure, RESALE, PRICING).verdict != "skip"


def test_laptop_is_only_wrong_when_pricing_a_component():
    ai = SURE_AI.model_copy(update={"item_type": "laptop"})
    assert evaluate(listing(price=250), market(600), ai, RESALE, PRICING).verdict == "skip"
    thinkpad = listing(title="Lenovo ThinkPad T480 i5 16GB", price=150)
    assert evaluate(thinkpad, market(300), ai, SearchConfig(name="laptops"), PRICING).verdict == "buy"
    macbook = listing(title="Apple MacBook Pro 13 M1 2020", price=500)
    assert evaluate(macbook, market(900), ai, SearchConfig(name="macs"), PRICING).verdict == "buy"


def test_ai_locked_defects_and_stock_photos():
    locked = SURE_AI.model_copy(update={"locked": True})
    assert evaluate(listing(price=250), market(600), locked, RESALE, PRICING).verdict == "skip"
    broken = SURE_AI.model_copy(update={"defects": ["Riss im Display"]})
    ev = evaluate(listing(price=250), market(600), broken, RESALE, PRICING)
    assert ev.verdict == "skip" and "ИИ видит дефекты: Riss im Display" in ev.reasons
    stock = SURE_AI.model_copy(update={"stock_photos": True})
    assert evaluate(listing(price=250), market(600), stock, RESALE, PRICING).verdict == "maybe"


# 8 — haggling / budget ------------------------------------------------------------------------------


def test_vb_within_the_usual_discount_is_a_haggle_buy():
    ev = evaluate(listing(price=470, negotiable=True, price_text="470 € VB"), market(600), None, RESALE, PRICING)
    assert ev.verdict == "buy" and ev.action == "haggle" and ev.offer_price == 430  # max 432 -> 430
    assert any(r.startswith("Торгуйся: предложи 430 €") for r in ev.reasons)
    assert ev.score >= 70
    fixed = evaluate(listing(price=470), market(600), None, RESALE, PRICING)
    assert fixed.verdict != "buy" and fixed.offer_price is None
    too_far = evaluate(listing(price=520, negotiable=True), market(600), None, RESALE, PRICING)
    assert too_far.verdict != "buy"
    plain = evaluate(listing(price=250), market(600), None, RESALE, PRICING)
    assert plain.action == "buy" and plain.offer_price is None


def test_max_capital():
    ev = evaluate(listing(price=250), market(600), None, RESALE, PricingConfig(max_capital=200))
    assert ev.verdict == "skip" and "Дороже твоего бюджета (200 €)" in ev.reasons


# 9 — economics --------------------------------------------------------------------------------------


def test_fees_on_the_resale_price_plus_fixed_fee():
    pricing = PricingConfig(payment_fee_percent=2.49, paypal_fixed_fee=0.35)
    ev = evaluate(listing(price=250), market(600), None, RESALE, pricing)
    assert ev.fees == pytest.approx(600 * 0.0249 + 0.35, abs=0.01)
    assert ev.expected_profit == pytest.approx(600 - 60 - 15.29 - 250, abs=0.01)


# early "no deal" helpers ----------------------------------------------------------------------------


def test_market_says_no_deal():
    over = evaluate(listing(price=600), market(600), None, RESALE, PRICING)
    assert market_says_no_deal(over) and no_deal_reasons(over)[0] == "По рынку: ~600 €, выгоды нет"
    assert not market_says_no_deal(evaluate(listing(price=250), market(600), None, RESALE, PRICING))
    assert not market_says_no_deal(evaluate(listing(price=None), market(600), None, RESALE, PRICING))
    assert not market_says_no_deal(evaluate(listing(price=600), PriceEstimate(), None, RESALE, PRICING))
    thin = evaluate(listing(price=530), market(600), None, RESALE, PRICING)  # 10 € profit: skip
    assert market_says_no_deal(thin) and "выгода слишком мала" in no_deal_reasons(thin)[0]
    broken = evaluate(listing(price=250, description="Display gebrochen"), market(600), None, RESALE, PRICING)
    assert no_deal_reasons(broken)[0].startswith("Серьёзные проблемы")
    assert not market_says_no_deal(Evaluation(ad_id="x"))


# text ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Privatverkauf, keine Garantie oder Rücknahme bei Defekten",
    "Keine Haftung für Defekte",
    "Gewährleistung für Defekte ausgeschlossen",
])
def test_private_sale_disclaimer_is_not_a_defect(text):
    assert detect_red_flags(text) == []


def test_real_defects_still_detected_next_to_a_disclaimer():
    assert "Дефект / для мастера" in detect_red_flags("Display defekt, keine Rücknahme")
