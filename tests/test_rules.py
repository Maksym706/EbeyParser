"""v0.2 hard rules in evaluate(): money-losing alerts must not happen."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ebeyparser.config import PricingConfig, SearchConfig
from ebeyparser.models import AIVerdict, Comparable, Evaluation, Listing, PriceEstimate, utcnow
from ebeyparser.pricing.estimator import (
    estimate_from_comparables,
    evaluate,
    market_says_no_deal,
    no_deal_reasons,
    prefilter_score,
)
from ebeyparser.pricing.text import FLAG_BAIT, SEVERE_FLAGS, detect_red_flags, is_remote_only, is_wanted_ad

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


@pytest.mark.parametrize("item_type", ["complete_pc", "part", "accessory", "box_only", "wanted"])
def test_ai_item_type_vetoes(item_type):
    ai = SURE_AI.model_copy(update={"item_type": item_type})
    assert evaluate(listing(price=250), market(600), ai, RESALE, PRICING).verdict == "skip"
    unsure = ai.model_copy(update={"confidence": 0.4})
    assert evaluate(listing(price=250), market(600), unsure, RESALE, PRICING).verdict != "skip"


def test_bundle_is_no_veto():
    # v0.2 fix round: "PS5 + 2 Controller" is a fine resale item (priced from bundles only)
    ai = SURE_AI.model_copy(update={"item_type": "bundle"})
    assert evaluate(listing(price=250), market(600), ai, RESALE, PRICING).verdict == "buy"


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


# ---------------------------------------------------------------------------- fix round (benchmark)

LOCKED = "Заблокировано (iCloud/аккаунт)"
DEFECT = "Дефект / для мастера"
SWAP = "Только обмен"
WANTED = "Это не продажа, а поиск"
SCAM = "Признаки мошенничества"


@pytest.mark.parametrize(("text", "flag"), [
    ("Apple-ID vom Vorbesitzer ist noch angemeldet, sonst top.", LOCKED),
    ("Hängt in der Aktivierung fest, Apple-ID unbekannt.", LOCKED),
    ("Hängt in der Aktivierungssperre fest", LOCKED),
    ("Icloud gespert, kann man bestimmt entsperren lassen.", LOCKED),
    ("I-Cloud ist noch aktiv", LOCKED),
    ("Aktivierungsperre drin", LOCKED),
    ("Google-Konto gesperrt (FRP)", LOCKED),
    ("Samsung Konto ist noch drauf", LOCKED),
    ("Ist mit einem Code gesichert, den ich leider nicht mehr weiß.", LOCKED),
    ("Ist noch mit dem Konto meines Bruders verbunden.", LOCKED),
    ("Ich bin gerade auf Montage in Polen, Versand über DHL.", SCAM),
    ("Vorrauskasse, Versand versichert.", SCAM),
    ("Nur Vorauskasse", SCAM),
    ("Tausch gg. PS5, kein Geld.", SWAP),
    ("Tausche gg Xbox", SWAP),
    ("Hätte lieber eine Xbox dafür, Geld interessiert mich eher nicht.", SWAP),
    ("Nur gegen Switch, Verkauf nicht gewünscht.", SWAP),
    ("Brauche dringend ein iPhone 13, melde dich!", WANTED),
    ("Suche dringend eine PS5", WANTED),
    ("Zahle gut, bitte alles anbieten", WANTED),
    ("Wir kaufen dein iPhone – sofort Bargeld.", WANTED),
    ("Lädt nicht mehr, Ladebuchse vermutlich hinüber.", DEFECT),
    ("Geht nicht mehr an.", DEFECT),
    ("Startet nicht.", DEFECT),
    ("Das Display hat einen Sprung quer über den ganzen Bildschirm.", DEFECT),
    ("Display hat einen Riss", DEFECT),
    ("Leider Wasserschaden, schaltet sich nicht mehr ein.", DEFECT),
    ("Hatte Wasserkontakt, geht seitdem nicht mehr.", DEFECT),
    ("Ist mir runtergefallen, seitdem bleibt der Bildschirm schwarz.", DEFECT),
    ("Akku ist leicht aufgebläht", DEFECT),
    ("Ist leider defeckt, geht nicht an.", DEFECT),
    ("Face ID geht nicht, ansonsten top.", DEFECT),
])
def test_natural_phrasings_and_typos_are_flagged(text, flag):
    assert flag in detect_red_flags("Apple iPhone 13\n" + text)


@pytest.mark.parametrize("text", [
    "Keine Garantie, keine Rücknahme. Für eventuelle Defekte wird nicht gehaftet.",
    "Privatverkauf, keine Garantie oder Rücknahme bei Defekten.",
    "Da Privatverkauf: keine Gewährleistung, keine Rücknahme, auch nicht bei Defekten.",
    "Privatverkauf unter Ausschluss jeglicher Gewährleistung – keine Haftung für Defekte.",
    "Privatverkauf. Keine Garantie, kein Umtausch, keine Rücknahme bei späteren Defekten.",
    "Nicht gesperrt, iCloud ist abgemeldet.", "Apple-ID abgemeldet, zurückgesetzt.",
    "Google-Konto abgemeldet, auf Werkseinstellungen zurückgesetzt.",
    "Keine Defekte, kein Wasserschaden.", "Display ohne Risse, nie gebrochen.", "Kein Tausch, nur Verkauf.",
    "Keine Vorkasse, nur Abholung oder PayPal.", "Nie für Mining genutzt, keine Bildfehler.",
    "Kein Verkauf an Händler.", "Brauche dringend ein neues Handy, deshalb günstig.", "Nicht original verpackt.",
])
def test_legit_wordings_stay_clean(text):
    assert not [f for f in detect_red_flags("Apple iPhone 13\n" + text) if f in SEVERE_FLAGS]


def test_wanted_titles():
    for title in ("iPhone 13 – zahle gut", "Brauche dringend PS5", "Wer verkauft Switch OLED?",
                  "Kaufe iPhones aller Art an", "Suche dringend RTX 3080"):
        assert is_wanted_ad(title), title
    for title in ("Verkaufe iPhone 13", "PS5 abzugeben", "Verkaufe PS5, suche Xbox"):
        assert not is_wanted_ad(title), title


def test_free_defective_item_is_never_a_buy():
    ad = listing(price=None, is_free=True, description="Lädt nicht mehr, Ladebuchse vermutlich hinüber.")
    ev = evaluate(ad, market(600), None, RESALE, PRICING)
    assert ev.verdict == "skip" and ev.score <= 20


def test_whatsapp_or_prepay_bait_is_severe_only_when_cheap():
    wa = "Schreib mir gern direkt auf WhatsApp, hier bin ich selten online."
    assert evaluate(listing(price=300, description=wa), market(600), None, RESALE, PRICING).verdict == "skip"
    assert FLAG_BAIT in evaluate(listing(price=300, description=wa), market(600), None, RESALE, PRICING).red_flags
    assert evaluate(listing(price=420, description=wa), market(600), None, RESALE, PRICING).verdict != "skip"
    new = "Neu und unbenutzt, Versand erfolgt direkt nach Zahlungseingang."
    assert evaluate(listing(price=280, description=new), market(600), None, RESALE, PRICING).verdict == "skip"
    fair = evaluate(listing(price=420, description=new), market(600), None, RESALE, PRICING)
    assert FLAG_BAIT not in fair.red_flags


def test_email_or_telegram_contact_is_bait_only_when_cheap():
    for text in ("Bei Interesse bitte an max.muster@gmail.com schreiben.", "Schreib mir auf Telegram, bin selten hier."):
        cheap = evaluate(listing(price=300, description=text), market(600), None, RESALE, PRICING)
        assert cheap.verdict == "skip" and FLAG_BAIT in cheap.red_flags, text
        assert evaluate(listing(price=420, description=text), market(600), None, RESALE, PRICING).verdict != "skip"


# B — missing parts --------------------------------------------------------------------------


def test_missing_parts_cap_resale_at_maybe():
    tool = Listing(ad_id="1", url="u", title="DeWalt DCD796", price=60.0,
                   description="Verkaufe nur das Grundgerät, Akkus und Lader behalte ich.")
    ev = evaluate(tool, market(160), None, SearchConfig(name="tools"), PRICING)
    assert ev.verdict == "maybe" and any(r.startswith("Некомплект:") and "рыночная цена ниже" in r
                                          for r in ev.reasons)
    laptop = Listing(ad_id="2", url="u", title="Apple MacBook Air M2 8GB 256GB", price=400.0,
                     description="Ohne Netzteil.")
    ev = evaluate(laptop, market(760), None, SearchConfig(name="macs"), PRICING)
    assert ev.verdict == "maybe" and "Некомплект: netzteil — рыночная цена ниже" in ev.reasons
    personal = evaluate(laptop, market(760), None, SearchConfig(name="p", purpose="personal"), PRICING)
    assert personal.verdict == "buy"  # for yourself a missing charger is no reason to pass


def test_no_missing_parts_cap_for_phones_without_brick_or_solo_tools():
    phone = Listing(ad_id="1", url="u", title="Apple iPhone 13 128GB", price=250.0,
                    description="Mit Ladekabel, ohne Netzteil.")
    assert evaluate(phone, market(450), None, RESALE, PRICING).verdict == "buy"
    solo = Listing(ad_id="2", url="u", title="DeWalt DCD796 solo", price=50.0)
    ev = evaluate(solo, market(110), None, SearchConfig(name="tools"), PRICING)  # priced from solo tools
    assert ev.verdict == "buy" and not any(r.startswith("Некомплект") for r in ev.reasons)


# E — haggle consistency ---------------------------------------------------------------------


def test_negotiation_hint_always_comes_with_a_haggle_offer():
    vb = listing(price=170, negotiable=True, price_text="170 € VB")
    ev = evaluate(vb, market(300), None, RESALE, PRICING)
    assert ev.verdict == "buy" and ev.action == "haggle"
    assert ev.offer_price is not None and ev.offer_price <= 170 * 0.95
    assert any(r.startswith("Торгуйся: выгодно до") for r in ev.reasons)
    for price in (170, 220, 250, 300):
        e = evaluate(listing(price=price, negotiable=True), market(300), None, RESALE, PRICING)
        if any(r.startswith("Торгуйся: выгодно до") for r in e.reasons) and e.verdict != "skip":
            assert e.action == "haggle" and e.offer_price, price


# G — spread after robust outlier removal ------------------------------------------------------


def test_a_few_trap_prices_do_not_make_the_market_unclear():
    prices = [780, 790, 800, 770, 810, 795, 785, 391, 420]  # two traps among nine
    comps = [Comparable(title="Thermomix TM6", price=p, sold=True, source="ebay_sold") for p in prices]
    est = estimate_from_comparables(comps)
    assert (est.high - est.low) / est.market_price < 0.1
    ev = evaluate(Listing(ad_id="1", url="u", title="Thermomix TM6", price=510.0), est, None, RESALE, PRICING)
    assert not any("разбросаны" in r for r in ev.reasons)


def test_bundle_titles_never_stand_in_for_the_bare_product():
    from ebeyparser.pricing.estimator import comparable_fits, comparable_is_relevant, looks_like_bundle

    assert looks_like_bundle("Lenovo ThinkPad T480 mit Monitor, Tastatur und Maus")
    assert looks_like_bundle("Sony PS5 Disc + 2 Controller + 5 Spiele")
    assert not looks_like_bundle("iPhone 13 mit Hülle und Panzerglas")  # cheap extras: still the phone
    assert not looks_like_bundle("Steam Deck OLED Controller")
    assert not comparable_is_relevant("lenovo thinkpad t480", "Lenovo ThinkPad T480 mit Monitor, Tastatur und Maus")
    bundle = Comparable(title="PS5 Disc + 2 Controller + Spiele", price=550)
    assert not comparable_fits("Sony PS5 Disc Edition", bundle)
    assert comparable_fits("Sony PS5 Disc + 2 Controller + 5 Spiele", bundle)
