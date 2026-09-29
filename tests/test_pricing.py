from __future__ import annotations

from datetime import timedelta

import pytest

from ebeyparser.config import PricingConfig, ReferencePrice, SearchConfig
from ebeyparser.models import AIVerdict, Comparable, Listing, PriceEstimate, utcnow
from ebeyparser.pricing.estimator import (
    combine_estimates,
    estimate_from_comparables,
    evaluate,
    find_reference_price,
    fmt_money,
    prefilter,
    prefilter_score,
    robust_prices,
)
from ebeyparser.pricing.text import (
    SEVERE_FLAGS,
    detect_red_flags,
    is_wanted_ad,
    make_search_query,
    matches_keywords,
    normalize,
)

DEFECT = "Дефект / для мастера"
BOX = "Только коробка"
LOCKED = "Заблокировано (iCloud/аккаунт)"
FAKE = "Реплика / подделка"
SCAM = "Признаки мошенничества"
SWAP = "Только обмен"
WANTED = "Это не продажа, а поиск"
MISSING = "Нет комплектующих"
RENT = "Рассрочка/аренда"


def make_listing(**kw) -> Listing:
    data = {"ad_id": "1", "url": "https://example.org/1", "title": "Gigabyte RTX 3090 Gaming OC"}
    data.update(kw)
    return Listing(**data)


def sold_estimate(market: float, n: int = 15, source: str = "ebay_sold") -> PriceEstimate:
    return PriceEstimate(market_price=market, low=market * 0.9, high=market * 1.1, sample_size=n, source=source)


RESALE = SearchConfig(name="gpu")
PRICING = PricingConfig()
AI_BUY = AIVerdict(
    product="RTX 3090",
    verdict="buy",
    confidence=0.8,
    photo_matches_description=True,
    condition="good",
    reasoning="Реальные фото, цена сильно ниже рынка.",
)


# --------------------------------------------------------------------------- text


def test_normalize():
    assert normalize("Grüße aus KÖLN – Straße!! 🔥🔥") == "gruesse aus koeln strasse"
    assert normalize("  Café   crème ") == "cafe creme"
    assert normalize("2,5 Zoll SSD, 16 GB RAM") == "2.5zoll ssd 16gb ram"
    assert normalize("") == ""


def test_make_search_query_gpu():
    q = make_search_query("Verkaufe Gigabyte RTX 3090 Gaming OC 24GB – Top Zustand!!")
    words = q.split()
    assert "rtx" in words and "3090" in words
    assert len(words) <= 5
    assert "verkaufe" not in words and "top" not in words and "zustand" not in words


def test_make_search_query_mac_mini():
    q = make_search_query("🔥 Apple Mac mini M1 8GB 256GB wie neu OVP 🔥")
    assert q == "apple mac mini m1 8gb"


@pytest.mark.parametrize(
    ("title", "must_have", "must_not"),
    [
        ("Intel Core i7-8700K CPU 250€ VB", ["i7", "8700k"], ["250", "vb"]),
        ("PS5 Disc Edition + 2 Controller NP 549€", ["ps5"], ["549", "controller"]),
        ("Sony PS5 neu originalverpackt Rechnung Garantie", ["sony", "ps5"], ["neu", "rechnung"]),
    ],
)
def test_make_search_query_examples(title, must_have, must_not):
    words = make_search_query(title).split()
    assert len(words) <= 5
    for w in must_have:
        assert w in words
    for w in must_not:
        assert w not in words


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Suche iPhone 13", True),
        ("Suche: RTX 3080", True),
        ("Kaufe defekte Handys", True),
        ("Ankauf von Konsolen aller Art", True),
        ("RTX 3080 gesucht", True),
        ("Verkaufe iPhone 13", False),
        ("Sucher für Canon Kamera", False),
        ("Verkaufe PS5, suche Xbox", False),
        ("", False),
    ],
)
def test_is_wanted_ad(title, expected):
    assert is_wanted_ad(title) is expected


@pytest.mark.parametrize(
    ("text", "flag"),
    [
        ("iPhone 13 defekt, für Bastler", DEFECT),
        ("Laptop Bastlerware ohne Funktion", DEFECT),
        ("Handy geht nicht mehr an", DEFECT),
        ("TV kein Bild", DEFECT),
        ("Wasserschaden", DEFECT),
        ("Display gebrochen, sonst ok", DEFECT),
        ("Display ist leider kaputt", DEFECT),
        ("Sold for parts", DEFECT),
        ("Nur OVP, kein Gerät", BOX),
        ("Leere OVP iPhone 14 Pro", BOX),
        ("Verkauft wird nur die Verpackung", BOX),
        ("iCloud gesperrt", LOCKED),
        ("Aktivierungssperre drin", LOCKED),
        ("Replica Uhr", FAKE),
        ("Nachbau, sieht aber gut aus", FAKE),
        ("1:1 Qualität", FAKE),
        ("Zahlung nur per Vorkasse", SCAM),
        ("Western Union bevorzugt", SCAM),
        ("Nur PayPal Freunde", SCAM),
        ("Kontakt nur über WhatsApp", SCAM),
        ("Versand nur ins Ausland", SCAM),
        ("Nur Tausch", SWAP),
        ("Tausche gegen PS5", SWAP),
        ("Mietkauf", RENT),
        ("Ratenzahlung", RENT),
        ("Konsole ohne Controller", MISSING),
        ("Laptop ohne Netzteil und ohne Akku", MISSING),
    ],
)
def test_red_flags_detected(text, flag):
    assert flag in detect_red_flags(text)


@pytest.mark.parametrize(
    "text",
    [
        "Keine Defekte, nicht defekt, ohne Mängel, ohne Kratzer",
        "Keine Kratzer oder Defekte",
        "Defekte: keine",
        "War nie defekt",
        "iPhone 12 ohne Simlock, iCloud frei, nicht gesperrt",
        "Kein Fake, 100% original",
        "Rechnung als Kopie vorhanden",
        "Nur Abholung, Barzahlung",
        "Keine Vorkasse, nur Abholung",
        "Kein Tausch",
        "Verkaufe oder tausche gegen PS5",
        "Nur die OVP fehlt",
        "Karton leicht kaputt, Gerät top",
        "Display nicht gebrochen",
        "Leasingrückläufer ThinkPad T480",
        "Maßstab 1:18",
        "funktioniert einwandfrei",
    ],
)
def test_red_flags_negations_are_clean(text):
    assert detect_red_flags(text) == []


def test_red_flags_wanted_only_on_first_line_and_dedup():
    flags = detect_red_flags("Suche RTX 3080\ndefekt egal, auch defekte Karten")
    assert flags == [WANTED, DEFECT]
    assert WANTED not in detect_red_flags("RTX 3080\nSuche nichts anderes")


def test_severe_flags_subset():
    assert {DEFECT, BOX, LOCKED, FAKE, SCAM, SWAP, WANTED, RENT} <= SEVERE_FLAGS
    assert MISSING not in SEVERE_FLAGS


def test_matches_keywords():
    text = "Gebrauchte GRAFIKKARTE Nvidia RTX-3090 für Gamer"
    assert matches_keywords(text, [], [])
    assert matches_keywords(text, ["rtx 3090", "rtx 4090"], [])
    assert matches_keywords(text, ["Grafikkarte"], [])
    assert matches_keywords(text, ["fuer gamer"], [])  # ü == ue
    assert not matches_keywords(text, ["4090"], [])
    assert not matches_keywords(text, [], ["Gebrauchte"])
    assert not matches_keywords(text, ["3090"], ["nvidia"])


# --------------------------------------------------------------------------- estimates


def test_robust_prices_removes_outliers():
    assert robust_prices([300, 310, 320, 330, 5000]) == [300, 310, 320, 330]
    assert robust_prices([20, 300, 310]) == [300, 310]  # box-only price < 25% of median
    assert robust_prices([0, -5, 100]) == [100]
    assert robust_prices([]) == []
    assert robust_prices([100, 100, 100, 100, 105]) == [100, 100, 100, 100, 105]


def test_estimate_mixed_sold_and_asking():
    comps = [Comparable(title=f"sold {i}", price=p, source="ebay_sold", sold=True) for i, p in enumerate([500, 520, 540])]
    comps += [Comparable(title=f"ask {i}", price=p, source="kleinanzeigen") for i, p in enumerate([600, 620, 640])]
    comps.append(Comparable(title="nur OVP", price=15, source="kleinanzeigen"))
    est = estimate_from_comparables(comps, asking_price_discount=0.85, query="rtx 3090")
    assert est.source == "mixed"
    assert est.sample_size == 6
    # adjusted: 500 520 540 510 527 544 -> median 523.5
    assert est.market_price == pytest.approx(523.5)
    assert est.low < est.market_price < est.high
    assert est.query == "rtx 3090"
    assert [c.price for c in est.comparables] == sorted(c.price for c in est.comparables)
    assert all(c.price != 15 for c in est.comparables)
    assert "3 проданных на eBay" in est.notes and "15%" in est.notes


def test_estimate_sources_and_too_few():
    sold = [Comparable(title="x", price=p, sold=True, source="ebay_sold") for p in (100, 110, 120)]
    assert estimate_from_comparables(sold).source == "ebay_sold"
    asking = [Comparable(title="x", price=p, source="kleinanzeigen") for p in (100, 110, 120)]
    est = estimate_from_comparables(asking, asking_price_discount=0.8)
    assert est.source == "kleinanzeigen"
    assert est.market_price == pytest.approx(88.0)
    few = estimate_from_comparables(asking[:2])
    assert few.source == "none" and few.market_price is None
    assert len(few.comparables) == 2


def test_estimate_caps_comparables():
    comps = [Comparable(title=str(i), price=100 + i, sold=True) for i in range(50)]
    est = estimate_from_comparables(comps)
    assert est.sample_size == 50
    assert len(est.comparables) == 30


def test_find_reference_price():
    refs = [
        ReferencePrice(keywords=["rtx", "3080"], price=400, exclude=["ti"]),
        ReferencePrice(keywords=["rtx", "3080", "ti"], price=480),
        ReferencePrice(keywords=["Mac Mini", "M1"], price=380),
    ]
    assert find_reference_price("Gigabyte RTX 3080 Gaming", refs) == 400
    assert find_reference_price("MSI RTX 3080 Ti Suprim", refs) == 480
    assert find_reference_price("RTX 3080Ti", refs) is None  # different model number
    assert find_reference_price("Apple Mac mini M1 8GB", refs) == 380
    assert find_reference_price("Apple Mac mini M10", refs) is None
    assert find_reference_price("Something else", refs) is None


def test_combine_estimates_priority():
    ka = PriceEstimate(market_price=300, sample_size=10, source="kleinanzeigen", comparables=[Comparable(title="a", price=300)])
    eb = PriceEstimate(market_price=280, sample_size=5, source="ebay_sold", comparables=[Comparable(title="b", price=280, sold=True)])
    ref = PriceEstimate(market_price=320, source="reference")
    ai = PriceEstimate(market_price=350, source="ai")
    assert combine_estimates(ka, eb, ai).market_price == 280
    assert combine_estimates(ka, eb, ref).source == "reference"
    assert combine_estimates(ai, ka).source == "kleinanzeigen"
    assert combine_estimates(None, ai).source == "ai"
    merged = combine_estimates(ka, eb)
    assert {c.title for c in merged.comparables} == {"a", "b"}
    empty = combine_estimates(None, PriceEstimate(comparables=[Comparable(title="c", price=1)]))
    assert empty.source == "none" and empty.market_price is None
    assert len(empty.comparables) == 1


# --------------------------------------------------------------------------- prefilter


def test_prefilter_reasons():
    search = SearchConfig(
        name="s", min_price=50, max_price=500, include_keywords=["3090"], exclude_keywords=["wasserkühler"]
    )
    assert prefilter(make_listing(price=300), search) == (True, [])
    keep, reasons = prefilter(make_listing(title="Suche RTX 3090", price=300), search)
    assert not keep and any("поиск" in r for r in reasons)
    keep, reasons = prefilter(make_listing(title="Wasserkühler für RTX 3090", price=80), search)
    assert not keep and any("Стоп-слова" in r for r in reasons)
    keep, reasons = prefilter(make_listing(title="RTX 3080", price=300), search)
    assert not keep and any("ключевых слов" in r for r in reasons)
    keep, reasons = prefilter(make_listing(price=20), search)
    assert not keep and any("ниже минимума" in r for r in reasons)
    keep, reasons = prefilter(make_listing(price=900), search)
    assert not keep and any("выше максимума" in r for r in reasons)
    assert prefilter(make_listing(price=None), search)[0]


def test_prefilter_personal_too_expensive():
    search = SearchConfig(name="p", purpose="personal", target_price=200)
    assert prefilter(make_listing(price=250), search)[0]
    keep, reasons = prefilter(make_listing(price=300), search)
    assert not keep and any("дорого" in r for r in reasons)


def test_prefilter_score():
    est = sold_estimate(600)
    great = prefilter_score(make_listing(price=250), est, RESALE, PRICING)
    fair = prefilter_score(make_listing(price=600), est, RESALE, PRICING)
    bad = prefilter_score(make_listing(price=900), est, RESALE, PRICING)
    assert great > 80 and 15 <= fair <= 25 and bad == 0
    assert prefilter_score(make_listing(price=250), None, RESALE, PRICING) == 40
    assert prefilter_score(make_listing(price=None, is_free=True), None, RESALE, PRICING) >= 80
    assert prefilter_score(make_listing(title="RTX 3090 defekt", price=50), est, RESALE, PRICING) <= 10
    refs = PricingConfig(reference_prices=[ReferencePrice(keywords=["3090"], price=600)])
    assert prefilter_score(make_listing(price=250), None, RESALE, refs) > 80
    personal = SearchConfig(name="p", purpose="personal", target_price=300)
    assert prefilter_score(make_listing(price=280), None, personal, PRICING) > 60


# --------------------------------------------------------------------------- evaluate


def test_evaluate_great_resale_deal():
    ev = evaluate(make_listing(price=250), sold_estimate(600, 15), AI_BUY, RESALE, PRICING)
    assert ev.verdict == "buy"
    assert ev.score >= 80
    assert ev.buy_price == 250
    assert ev.expected_profit == pytest.approx(290.0)  # 600*0.9 - 250
    assert ev.roi == pytest.approx(1.16)
    assert ev.max_buy_price == 432  # min(540-40, 540/1.25)
    assert "Цена 250 € — на 58% ниже рынка (~600 €)" in ev.reasons
    assert "Чистая прибыль ≈ 290 € (ROI 116%)" in ev.reasons
    assert "Оценка по 15 проданным на eBay" in ev.reasons
    assert any(r.startswith("ИИ: покупать (уверенность 80%)") for r in ev.reasons)
    assert ev.red_flags == []


def test_evaluate_mediocre_is_maybe():
    ev = evaluate(make_listing(price=230), sold_estimate(300, 10), None, RESALE, PRICING)
    assert ev.verdict == "maybe"
    assert 40 <= ev.score <= 60
    assert any("Выгодно только при цене до 216 €" in r for r in ev.reasons)


def test_evaluate_overpriced_is_skip():
    ev = evaluate(make_listing(price=320), sold_estimate(300, 10), None, RESALE, PRICING)
    assert ev.verdict == "skip"
    assert ev.expected_profit < 0
    assert ev.score <= 20
    assert any("Убыток" in r for r in ev.reasons)


def test_evaluate_severe_flag_skips():
    listing = make_listing(title="iPhone 13 defekt für Bastler", price=100)
    ev = evaluate(listing, sold_estimate(400), AI_BUY, RESALE, PRICING)
    assert ev.verdict == "skip"
    assert ev.score <= 20
    assert DEFECT in ev.red_flags
    assert any("Серьёзные проблемы" in r for r in ev.reasons)


def test_evaluate_ai_defective_condition_is_severe():
    ai = AI_BUY.model_copy(update={"condition": "defective"})
    ev = evaluate(make_listing(price=250), sold_estimate(600), ai, RESALE, PRICING)
    assert ev.verdict == "skip" and ev.score <= 20 and DEFECT in ev.red_flags


def test_evaluate_free_item():
    listing = make_listing(title="Stuhl Vitra", price=None, is_free=True)
    ev = evaluate(listing, sold_estimate(100), None, RESALE, PRICING)
    assert ev.buy_price == 0.0
    assert ev.expected_profit == pytest.approx(90.0)
    assert ev.verdict == "buy"
    assert any("бесплатно" in r for r in ev.reasons)
    # v0.2 final round (N11): something worth 150 €+ given away is usually a lure
    dear = evaluate(listing, sold_estimate(200), None, RESALE, PRICING)
    assert dear.verdict == "maybe" and dear.action == "watch"
    assert "Бесплатно дорогая вещь — часто приманка" in dear.reasons


def test_evaluate_no_price():
    listing = make_listing(price=None, negotiable=True, price_text="VB")
    ev = evaluate(listing, sold_estimate(600), None, RESALE, PRICING)
    assert ev.verdict == "skip"
    assert ev.expected_profit is None and ev.roi is None
    assert "Цена не указана — уточни у продавца" in ev.reasons
    assert ev.max_buy_price == 432
    # v0.2 fix round: a "skip" never carries a haggle hint (action and reasons must agree)
    assert "Выгодно только при цене до 432 €" in ev.reasons and ev.action == "skip"
    ev_ai = evaluate(listing, sold_estimate(600), AI_BUY, RESALE, PRICING)
    assert ev_ai.verdict == "maybe"
    assert ev_ai.score <= 40


def test_evaluate_placeholder_price_treated_as_unknown():
    ev = evaluate(make_listing(price=1, negotiable=True), sold_estimate(600), None, RESALE, PRICING)
    assert ev.buy_price is None and ev.verdict == "skip"
    assert any("заглушку" in r for r in ev.reasons)


def test_evaluate_no_market_data():
    ev = evaluate(make_listing(price=250), PriceEstimate(), None, RESALE, PRICING)
    assert ev.verdict == "skip"
    assert "Не хватило данных для оценки рыночной цены" in ev.reasons
    ai_maybe = AIVerdict(verdict="maybe", confidence=0.5)
    assert evaluate(make_listing(price=250), PriceEstimate(), ai_maybe, RESALE, PRICING).verdict == "maybe"


def test_evaluate_ai_market_price_fallback():
    # v0.2: a market price only guessed by the AI is never a "buy" and never alerts
    ai = AI_BUY.model_copy(update={"estimated_market_price": 600.0})
    ev = evaluate(make_listing(price=250), PriceEstimate(), ai, RESALE, PRICING)
    assert ev.estimate.source == "ai" and ev.estimate.market_price == 600
    assert ev.verdict == "maybe" and ev.no_alert and ev.score <= 55
    assert ev.expected_profit == pytest.approx(290.0)  # profit still shown
    assert "⚠ Рынок оценён только ИИ — проверь цены сам" in ev.reasons
    unsure = ai.model_copy(update={"confidence": 0.5})
    assert evaluate(make_listing(price=250), PriceEstimate(), unsure, RESALE, PRICING).verdict == "maybe"


def test_evaluate_reference_price_overrides():
    search = SearchConfig(name="s", reference_price=500)
    ev = evaluate(make_listing(price=250), sold_estimate(900), None, search, PRICING)
    assert ev.estimate.source == "reference"
    assert ev.estimate.market_price == 500
    assert ev.expected_profit == pytest.approx(200.0)


def test_evaluate_search_overrides_and_fees():
    search = SearchConfig(name="s", min_profit=300, min_roi=0.1)
    pricing = PricingConfig(selling_fee_percent=10, payment_fee_percent=0, default_shipping_cost=10)
    ev = evaluate(make_listing(price=250), sold_estimate(600), None, search, pricing)
    # v0.2: fees on the resale price itself: 600 - 60 margin - 60 fees - 10 shipping - 250
    # = 220 < min_profit 300 -> maybe
    assert ev.fees == pytest.approx(60.0)
    assert ev.shipping_cost == 10
    assert ev.expected_profit == pytest.approx(220.0)
    assert ev.verdict == "maybe"


def test_evaluate_personal_under_target():
    search = SearchConfig(name="p", purpose="personal", target_price=300)
    ev = evaluate(make_listing(price=250), sold_estimate(320), None, search, PRICING)
    assert ev.purpose == "personal"
    assert ev.verdict == "buy"
    assert ev.expected_profit == pytest.approx(70.0)
    assert ev.max_buy_price == 300
    assert "Для себя: экономия 70 € (цель ≤ 300 €)" in ev.reasons
    over = evaluate(make_listing(price=330), sold_estimate(320), None, search, PRICING)
    assert over.verdict == "maybe"  # within 15% above target
    way_over = evaluate(make_listing(price=400), sold_estimate(320), None, search, PRICING)
    assert way_over.verdict == "skip"


def test_evaluate_photo_mismatch_caps():
    ai = AI_BUY.model_copy(update={"photo_matches_description": False})
    ev = evaluate(make_listing(price=250), sold_estimate(600), ai, RESALE, PRICING)
    assert ev.verdict == "maybe"
    assert ev.score <= 60
    assert "⚠ Фото не совпадает с описанием" in ev.reasons


def test_evaluate_ai_verdict_no_longer_overrides_the_math():
    # v0.2 final round (N4): the AI's own buy/skip opinion doesn't veto; only its structured
    # findings do (stock photos -> at most "maybe", a sure broken screen -> skip)
    ai = AI_BUY.model_copy(update={"verdict": "skip", "confidence": 0.8, "reasoning": "Стоковые фото"})
    ev = evaluate(make_listing(price=250), sold_estimate(600), ai, RESALE, PRICING)
    assert ev.verdict == "buy"
    stock = ai.model_copy(update={"stock_photos": True})
    assert evaluate(make_listing(price=250), sold_estimate(600), stock, RESALE, PRICING).verdict == "maybe"
    broken = ai.model_copy(update={"defects": ["screen_broken"]})
    assert evaluate(make_listing(price=250), sold_estimate(600), broken, RESALE, PRICING).verdict == "skip"


def test_evaluate_ai_never_upgrades_maybe_to_buy():
    # v0.2: the AI may only downgrade — roi just under 25% stays "maybe" even with a sure AI
    listing = make_listing(price=175)
    est = sold_estimate(240)  # resale 216 -> profit 41, roi 0.234
    assert evaluate(listing, est, None, RESALE, PRICING).verdict == "maybe"
    assert evaluate(listing, est, AI_BUY, RESALE, PRICING).verdict == "maybe"


def test_evaluate_includes_delivery_cost():
    listing = make_listing(price=250, shipping_cost=10, source="ebay", buying_options=["FIXED_PRICE"])
    ev = evaluate(listing, sold_estimate(600), None, RESALE, PRICING)
    assert ev.buy_price == 260
    assert ev.expected_profit == pytest.approx(280.0)
    assert ev.max_buy_price == 422  # 432 - delivery
    assert any("доставка 10 €" in r for r in ev.reasons)


def test_evaluate_auction_running_is_capped():
    listing = make_listing(
        price=250,
        source="ebay",
        buying_options=["AUCTION"],
        bid_count=5,
        ends_at=utcnow() + timedelta(days=2),
    )
    ev = evaluate(listing, sold_estimate(600), AI_BUY, RESALE, PRICING)
    assert ev.verdict == "maybe" and ev.action == "bid"
    assert ev.score <= 60
    assert ev.expected_profit is None and ev.roi is None  # never from the current bid
    assert "Аукцион ещё идёт — итоговая цена будет выше" in ev.reasons
    assert "Аукцион: ставь максимум 432 € (сейчас 250 €)" in ev.reasons
    assert any("5 ставок" in r for r in ev.reasons)


def test_evaluate_auction_ending_soon_is_a_bid_not_a_buy():
    # v0.2: an auction is never "buy" — the answer is how high to bid
    now = utcnow()
    listing = make_listing(
        price=250, buying_options=["AUCTION"], bid_count=3, ends_at=now + timedelta(hours=1)
    )
    ev = evaluate(listing, sold_estimate(600), AI_BUY, RESALE, PRICING, now=now)
    assert ev.verdict == "maybe" and ev.action == "bid" and ev.max_buy_price == 432
    assert any("3 ставки" in r and "1 ч" in r for r in ev.reasons)
    ended = listing.model_copy(update={"ends_at": now - timedelta(minutes=5)})
    assert evaluate(ended, sold_estimate(600), AI_BUY, RESALE, PRICING, now=now).verdict == "skip"


def test_evaluate_auction_with_buy_now_is_not_capped():
    listing = make_listing(price=250, buying_options=["AUCTION", "FIXED_PRICE"], ends_at=utcnow() + timedelta(days=5))
    assert evaluate(listing, sold_estimate(600), AI_BUY, RESALE, PRICING).verdict == "buy"


def test_evaluate_ebay_seller_flags():
    few = make_listing(price=250, source="ebay", seller_feedback_score=3, seller_feedback_percent=100)
    ev = evaluate(few, sold_estimate(600), None, RESALE, PRICING)
    assert "Мало отзывов у продавца" in ev.red_flags
    assert ev.verdict == "buy"  # not severe
    low = make_listing(price=250, source="ebay", seller_feedback_score=500, seller_feedback_percent=95.2)
    assert "Низкий рейтинг продавца" in evaluate(low, sold_estimate(600), None, RESALE, PRICING).red_flags
    good = make_listing(price=250, source="ebay", seller_feedback_score=500, seller_feedback_percent=99.8)
    assert evaluate(good, sold_estimate(600), None, RESALE, PRICING).red_flags == []


def test_evaluate_ebay_condition_defect():
    listing = make_listing(price=250, source="ebay", condition="Als Ersatzteil / defekt")
    ev = evaluate(listing, sold_estimate(600), None, RESALE, PRICING)
    assert ev.verdict == "skip" and DEFECT in ev.red_flags


def test_evaluate_merges_ai_flags_and_soft_penalty():
    ai = AI_BUY.model_copy(update={"red_flags": ["Стоковые фото", "Нет комплектующих"]})
    listing = make_listing(price=250, description="Ohne Netzteil")
    ev = evaluate(listing, sold_estimate(600), ai, RESALE, PRICING)
    assert ev.red_flags == [MISSING, "Стоковые фото"]
    assert ev.verdict == "buy"
    assert any(r.startswith("Обратить внимание") for r in ev.reasons)


def test_fmt_money():
    assert fmt_money(1234.4) == "1.234 €"
    assert fmt_money(290) == "290 €"
    assert fmt_money(7.5) == "7,50 €"
    assert fmt_money(-30) == "-30 €"


def test_exclude_keywords_are_word_start_and_negation_aware():
    ex = ["defekt", "bastler", "tausch", "suche", "mining"]
    ok = "Privatverkauf, kein Umtausch und keine Rücknahme. Nie für Mining genutzt, keine Defekte."
    assert matches_keywords(ok, [], ex)
    for bad in ("Karte defekt, für Bastler", "Nur Tausch gegen PS5", "Tausche gegen 4070",
                "Wurde fürs Mining benutzt", "Suche RTX 3080"):
        assert not matches_keywords(bad, [], ex), bad
    from ebeyparser.pricing.text import matched_exclude_keyword

    assert matched_exclude_keyword("Nur Tausch", ex) == "tausch"
    assert matched_exclude_keyword(ok, ex) is None


def test_prefilter_keeps_kein_umtausch_ads():
    from ebeyparser.config import SearchConfig
    from ebeyparser.models import Listing
    from ebeyparser.pricing.estimator import prefilter

    search = SearchConfig(name="gpu", exclude_keywords=["defekt", "tausch", "mining"])
    listing = Listing(ad_id="1", url="u", title="ZOTAC RTX 3080 Trinity OC LHR 10GB", price=320,
                      description="Läuft einwandfrei, nie für Mining. Privatverkauf, kein Umtausch.")
    assert prefilter(listing, search) == (True, [])
    # v0.2: stop words look at the title only ("immer mit Hülle benutzt" in a description killed
    # real deals); a swap-only description is caught by the red flags instead
    assert prefilter(listing.model_copy(update={"description": "Nur Tausch"}), search) == (True, [])
    keep, reasons = prefilter(listing.model_copy(update={"title": "RTX 3080 – nur Tausch"}), search)
    assert not keep and reasons == ["Стоп-слова: «tausch»"]


@pytest.mark.parametrize(("title", "query"), [
    ("MSI NVIDIA RTX 3080 Ti VENTUS 3X 12G OC Gaming Grafikkarte", "rtx 3080 ti"),
    ("ZOTAC RTX 3080 Trinity OC LHR 10GB GDDR6X – kaum genutzt", "rtx 3080"),
    ("Gigabyte RTX3090 24GB", "rtx 3090"),
    ("AMD Radeon RX 7900 XTX", "rx 7900 xtx"),
    ("Apple iPhone 13 Pro Max 256GB", "apple iphone 13 pro max"),
])
def test_make_search_query_keeps_model_and_suffix(title, query):
    assert make_search_query(title) == query


@pytest.mark.parametrize(("query", "title", "relevant"), [
    ("rtx 3080", "Gigabyte GeForce RTX 3080 Gaming OC 10G", True),
    ("rtx 3080", "RTX3080 MSI Ventus", True),
    ("rtx 3080", "Gaming PC mit RTX 3080, Ryzen 7 5800X, 32GB", False),
    ("rtx 3080", "Gaming-PC RTX 3080", False),
    ("rtx 3080", "MSI RTX 3080 Ti Suprim X", False),
    ("rtx 3080", "RTX 3080Ti Founders Edition", False),
    ("rtx 3080", "EVGA RTX 3080 FTW3 Kühler", False),
    ("rtx 3080", "Alphacool Eisblock RTX 3080 waterblock", False),
    ("rtx 3080", "Laptop Lenovo Legion RTX 3080", False),
    ("rtx 3080", "Suche RTX 3080", False),
    ("rtx 3080", "RTX 3080 defekt", False),
    ("rtx 3080", "RTX 3070", False),
    ("rtx 3080 ti", "MSI RTX 3080 Ti Suprim X", True),
    ("apple iphone 13 pro max", "iPhone 13 Pro Max 256GB Graphit", True),
    ("apple iphone 13", "Apple iPhone 13 Pro 128GB", False),
    ("apple iphone 13", "iPhone 13 128GB", True),
    ("lenovo thinkpad t480", "Lenovo ThinkPad T480 Laptop i5 16GB", True),
    ("sony ps5", "PS5 Disc Edition", True),
])
def test_comparable_relevance(query, title, relevant):
    from ebeyparser.pricing.estimator import comparable_is_relevant

    assert comparable_is_relevant(query, title) is relevant


def test_gaming_pcs_no_longer_inflate_gpu_market_price():
    from ebeyparser.models import Comparable
    from ebeyparser.pricing.estimator import estimate_from_comparables, relevant_comparables

    cards = [Comparable(title=f"RTX 3080 {b}", price=p, source="kleinanzeigen")
             for b, p in [("MSI", 380), ("Zotac", 350), ("Asus TUF", 420), ("Palit", 400), ("Gigabyte", 390)]]
    pcs = [Comparable(title=f"Gaming PC RTX 3080 Ryzen {i}", price=1100 + 50 * i, source="kleinanzeigen")
           for i in range(8)]
    tis = [Comparable(title="RTX 3080 Ti", price=560, source="kleinanzeigen")] * 3
    everything = cards + pcs + tis
    assert estimate_from_comparables(everything, asking_price_discount=0.85).market_price > 700
    clean = estimate_from_comparables(relevant_comparables("rtx 3080", everything), asking_price_discount=0.85)
    assert clean.sample_size == 5 and 320 <= clean.market_price <= 350
