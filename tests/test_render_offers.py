"""Notification rendering of haggle deals, unchecked photos and the price-history source."""

from __future__ import annotations

from ebeyparser.models import AIVerdict, DealView, Evaluation, Listing, PriceEstimate
from ebeyparser.notify.render import (
    UNCHECKED_WARNING,
    deal_headline,
    email_subject,
    haggle_phrase,
    is_unchecked,
    offer_terms,
    render_email_html,
    render_telegram,
    render_text,
)


def _deal(**ev_kwargs) -> DealView:
    listing = Listing(ad_id="3100000001", url="https://www.kleinanzeigen.de/s-anzeige/rtx-3090/3100000001-225-3331",
                      title="RTX 3090 Founders Edition", price=450.0, negotiable=True, location="10115 Mitte")
    ev = Evaluation(ad_id=listing.ad_id, buy_price=450.0, expected_profit=-20.0, roi=-0.04, score=78,
                    verdict="buy", max_buy_price=430.0,
                    estimate=PriceEstimate(market_price=560.0, source="history", sample_size=9),
                    reasons=["Торгуйся: предложи 430 € — тогда прибыль ≈ 0 €", "Цена по истории: 9 объявлений"],
                    **ev_kwargs)
    return DealView(listing=listing, evaluation=ev)


def test_haggle_deals_show_profit_at_the_offer() -> None:
    deal = _deal(action="haggle", offer_price=400.0)
    offer, profit, roi = offer_terms(deal.evaluation, deal.listing)
    assert (offer, profit) == (400.0, 30.0) and round(roi, 3) == 0.075  # -20 € at 450 € -> +30 € at 400 €
    phrase = haggle_phrase(deal.evaluation, deal.listing)
    assert phrase.startswith("Торгуйся: предложи 400") and "→ прибыль ≈ 30" in phrase
    head = deal_headline(deal)
    assert "Торгуйся: предложи 400" in head and "прибыль ≈ 30" in head and "−20" not in head
    text = render_text([deal])
    assert "🤝 Торгуйся: предложи 400" in text and "Прибыль: ≈ −20" not in text
    assert text.count("Торгуйся") == 1  # the engine's own reason is not repeated
    tg = render_telegram(deal)
    assert "🤝 Торгуйся: предложи <b>400" in tg and "📈" not in tg
    html = render_email_html([deal])
    assert "🤝 Торгуйся: предложи 400" in html
    assert "предложи 400" in email_subject([deal])
    personal = _deal(action="haggle", offer_price=400.0, purpose="personal")
    assert "экономия ≈ 30" in haggle_phrase(personal.evaluation, personal.listing)
    assert offer_terms(_deal(action="buy").evaluation, deal.listing) is None


def test_unchecked_deals_are_marked_everywhere() -> None:
    deal = _deal(ai_checked=False, would_buy=True)
    deal.evaluation.verdict = "maybe"  # the engine stores unchecked would-be buys as "maybe"
    assert is_unchecked(deal.evaluation)
    assert deal_headline(deal).startswith(UNCHECKED_WARNING)
    assert email_subject([deal]).startswith(UNCHECKED_WARNING)
    assert render_text([deal]).splitlines()[4].strip() == UNCHECKED_WARNING  # right under the title
    assert render_telegram(deal).startswith(f"<b>{UNCHECKED_WARNING}</b>")
    assert UNCHECKED_WARNING in render_email_html([deal])
    checked = _deal(ai_checked=True, ai=AIVerdict(verdict="buy", product="RTX 3090"))
    assert not is_unchecked(checked.evaluation) and UNCHECKED_WARNING not in render_telegram(checked)
    assert is_unchecked(_deal(ai_checked=False).evaluation)  # verdict "buy" without an AI check
    not_a_deal = _deal(ai_checked=False)
    not_a_deal.evaluation.verdict = "maybe"
    assert not is_unchecked(not_a_deal.evaluation)


def test_history_source_is_translated() -> None:
    text = render_text([_deal()])
    assert "история цен, выборка: 9" in text and "history" not in text
