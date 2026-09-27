"""Tests for ebeyparser.notify: rendering, e-mail (fake SMTP) and Telegram (mock HTTP)."""

from __future__ import annotations

import json
import logging
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest

from ebeyparser.config import EmailConfig, NotificationsConfig, TelegramConfig
from ebeyparser.models import AIVerdict, DealView, Evaluation, Listing, PriceEstimate
from ebeyparser.notify import (
    EmailNotifier,
    Notifier,
    NotifyError,
    TelegramNotifier,
    build_notifiers,
    deal_headline,
    email_subject,
    format_money,
    format_percent,
    missing_settings,
    render_email_html,
    render_telegram,
    render_text,
    sample_deals,
    verdict_label,
)
from ebeyparser.notify import emailer as emailer_mod
from ebeyparser.notify import render

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ012345678"


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "_now", lambda: NOW)


def make_deal(
    *,
    ad_id: str = "2901234567",
    title: str = "RTX 3090",
    price: float | None = 450.0,
    negotiable: bool = False,
    is_free: bool = False,
    purpose: str = "resale",
    profit: float | None = 160.0,
    roi: float | None = 0.355,
    verdict: str = "buy",
    score: float = 82.4,
    images: list[str] | None = None,
    description: str = "Top Zustand",
    reasoning: str = "Карта целая, цена ниже рынка.",
    reasons: list[str] | None = None,
    red_flags: list[str] | None = None,
    with_ai: bool = True,
    **listing_extra: Any,
) -> DealView:
    listing = Listing(
        ad_id=ad_id,
        url=f"https://www.kleinanzeigen.de/s-anzeige/rtx-3090/{ad_id}-225-3331",
        title=title,
        price=price,
        negotiable=negotiable,
        is_free=is_free,
        location="10115 Mitte",
        distance_km=12.0,
        description=description,
        image_urls=["https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/1.jpg"] if images is None else images,
        **listing_extra,
    )
    ai = (
        AIVerdict(
            product="NVIDIA GeForce RTX 3090 24GB",
            photo_matches_description=True,
            verdict="buy",
            confidence=0.8,
            reasoning=reasoning,
            model="qwen2.5vl:7b",
        )
        if with_ai
        else None
    )
    ev = Evaluation(
        ad_id=ad_id,
        purpose=purpose,
        buy_price=price,
        estimate=PriceEstimate(market_price=610.0, low=560.0, high=680.0, sample_size=12, source="ebay_sold"),
        ai=ai,
        expected_profit=profit,
        roi=roi,
        score=score,
        verdict=verdict,
        reasons=reasons if reasons is not None else ["Цена на 26 % ниже рынка", "Частный продавец"],
        red_flags=red_flags or [],
    )
    return DealView(listing=listing, evaluation=ev)


class TagChecker(HTMLParser):
    """Validates Telegram HTML: only supported tags, properly nested."""

    ALLOWED = {"b", "i", "a", "code", "pre", "u", "s"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.ALLOWED:
            self.errors.append(f"tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")


def assert_valid_telegram_html(text: str) -> None:
    checker = TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors and not checker.stack, (checker.errors, checker.stack)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1234.5, "1.235 €"),
        (7.5, "7,50 €"),
        (None, "—"),
        (450, "450 €"),
        (450.0, "450 €"),
        (99.99, "99,99 €"),
        (0, "0 €"),
        (-12.5, "-12,50 €"),
        (1234567, "1.234.567 €"),
        (float("nan"), "—"),
    ],
)
def test_format_money(value: float | None, expected: str) -> None:
    assert format_money(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.643, "64 %"), (None, "—"), (0.125, "13 %"), (2.5, "250 %"), (0, "0 %"), (-0.1, "-10 %")],
)
def test_format_percent(value: float | None, expected: str) -> None:
    assert format_percent(value) == expected


def test_verdict_label() -> None:
    assert verdict_label("buy") == "Покупать"
    assert verdict_label("maybe") == "Подумать"
    assert verdict_label("skip") == "Пропустить"


def test_email_subject_single_deal() -> None:
    assert email_subject([make_deal()]) == "🔥 1 выгодное предложение: RTX 3090 за 450 €"
    assert email_subject([make_deal(negotiable=True)]) == "🔥 1 выгодное предложение: RTX 3090 за 450 € VB"
    assert "бесплатно" in email_subject([make_deal(price=None, is_free=True)])
    assert email_subject([make_deal(price=None)]) == "🔥 1 выгодное предложение: RTX 3090"


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (2, "🔥 2 выгодных предложения на Kleinanzeigen"),
        (4, "🔥 4 выгодных предложения на Kleinanzeigen"),
        (5, "🔥 5 выгодных предложений на Kleinanzeigen"),
        (11, "🔥 11 выгодных предложений на Kleinanzeigen"),
        (21, "🔥 21 выгодное предложение на Kleinanzeigen"),
        (22, "🔥 22 выгодных предложения на Kleinanzeigen"),
        (25, "🔥 25 выгодных предложений на Kleinanzeigen"),
    ],
)
def test_email_subject_plural(n: int, expected: str) -> None:
    deals = [make_deal(ad_id=str(i)) for i in range(n)]
    assert email_subject(deals) == expected


def test_email_subject_mixed_sources() -> None:
    deals = [make_deal(ad_id="1"), make_deal(ad_id="ebay-2", source="ebay")]
    assert email_subject(deals) == "🔥 2 выгодных предложения на Kleinanzeigen и eBay"


def test_deal_headline() -> None:
    assert deal_headline(make_deal()) == "RTX 3090 — 450 € → прибыль ≈ 160 € · Kleinanzeigen"
    assert "экономия ≈ 160 €" in deal_headline(make_deal(purpose="personal"))
    assert deal_headline(make_deal(profit=None)) == "RTX 3090 — 450 € · Kleinanzeigen"


# --------------------------------------------------------------------------- #
# E-mail HTML / plain text
# --------------------------------------------------------------------------- #


def test_render_email_html_escapes_and_contains_essentials() -> None:
    deal = make_deal(
        title="<script>alert(1)</script> RTX 3090",
        reasoning='Выглядит <b>подозрительно</b> & "дёшево"',
        reasons=["<img src=x onerror=alert(1)>"],
        red_flags=["Нет <фото> серийника"],
        negotiable=True,
    )
    out = render_email_html([deal], title="Находки <b>тест</b>", web_base_url="http://localhost:8000")
    assert "<script" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt; RTX 3090" in out
    assert "<img src=x" not in out and "&lt;img src=x onerror=alert(1)&gt;" in out
    assert "&lt;b&gt;подозрительно&lt;/b&gt; &amp; &quot;дёшево&quot;" in out
    assert "Находки &lt;b&gt;тест&lt;/b&gt;" in out
    # Prices, profit, score, verdict, market with source and sample
    assert "450&nbsp;€" in out
    assert "VB (торг)" in out
    assert "Прибыль +160&nbsp;€ · ROI 36 %" in out
    assert "Оценка 82/100" in out
    assert "Покупать" in out and "#dcfce7" in out  # green verdict chip
    assert "≈ 610&nbsp;€" in out and "продажи eBay, выборка: 12" in out
    assert "⚠ Нет &lt;фото&gt; серийника" in out
    assert "NVIDIA GeForce RTX 3090 24GB" in out and "✅ фото соответствует описанию" in out
    # Links and photo
    assert 'src="https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/1.jpg"' in out
    assert 'href="https://www.kleinanzeigen.de/s-anzeige/rtx-3090/2901234567-225-3331"' in out
    assert "Открыть объявление" in out
    assert 'href="http://localhost:8000/deal/2901234567"' in out and "Подробнее в EbeyParser" in out
    # Layout rules: inline CSS only, header count, footer
    assert "<style" not in out.lower() and "<link" not in out.lower()
    assert "1 выгодное предложение на Kleinanzeigen" in out
    assert "EbeyParser · бот-охотник за выгодными объявлениями" in out


def test_render_email_html_verdict_colors_and_no_web_link() -> None:
    maybe = render_email_html([make_deal(verdict="maybe")])
    skip = render_email_html([make_deal(verdict="skip")])
    assert "#fef3c7" in maybe and "Подумать" in maybe
    assert "#e5e7eb" in skip and "Пропустить" in skip
    assert "Подробнее в EbeyParser" not in maybe


def test_render_email_html_rejects_javascript_urls() -> None:
    deal = make_deal(images=["javascript:alert(1)"])
    deal.listing.url = "javascript:alert(1)"
    out = render_email_html([deal])
    assert "javascript:" not in out
    assert "<img" not in out


def test_render_text_contents() -> None:
    deal = make_deal(
        negotiable=True,
        reasons=["r1", "r2", "r3", "r4", "r5"],
        red_flags=["Нет чека"],
    )
    out = render_text([deal], title="Тест", web_base_url="http://192.168.1.5:8000/")
    assert out.startswith("Тест\n")
    assert "Цена: 450 € VB (торг)" in out
    assert "Рынок: ≈ 610 € (продажи eBay, выборка: 12; диапазон 560–680 €)" in out
    assert "Прибыль: ≈ 160 € · ROI 36 %" in out
    assert "Вердикт: Покупать · оценка 82/100" in out
    assert "10115 Mitte · 12 км" in out
    assert "⚠ Нет чека" in out
    assert "• r3" in out and "• r4" not in out  # top 3 reasons only
    assert "Открыть объявление: https://www.kleinanzeigen.de/" in out
    assert "Подробнее в EbeyParser: http://192.168.1.5:8000/deal/2901234567" in out
    assert "EbeyParser · бот-охотник за выгодными объявлениями" in out


def test_price_variants() -> None:
    free = render_text([make_deal(price=None, is_free=True, profit=None)])
    unknown = render_text([make_deal(price=None, negotiable=True, profit=None)])
    assert "Цена: бесплатно" in free
    assert "Цена: цена не указана, VB (торг)" in unknown


def test_personal_deal_says_savings() -> None:
    deal = make_deal(purpose="personal", profit=45.0, roi=0.47)
    text = render_text([deal])
    html_out = render_email_html([deal])
    tg = render_telegram(deal)
    for out in (text, html_out, tg):
        assert "Экономия" in out
        assert "Прибыль" not in out
    assert "Экономия: ≈ 45 € · на 7 % дешевле рынка" in text
    assert "Для себя" in html_out


def test_deal_without_evaluation_renders() -> None:
    deal = DealView(listing=Listing(ad_id="1", url="https://www.kleinanzeigen.de/s-anzeige/x/1", title="Sofa"))
    assert "Sofa" in render_text([deal])
    assert "Sofa" in render_email_html([deal])
    assert "Sofa" in render_telegram(deal)
    assert deal_headline(deal) == "Sofa — цена не указана · Kleinanzeigen"


def test_email_digest_is_capped() -> None:
    deals = [make_deal(ad_id=str(i)) for i in range(render.MAX_EMAIL_DEALS + 5)]
    out = render_text(deals)
    assert "…и ещё 5 предложений" in out
    assert "…и ещё 5 предложений" in render_email_html(deals)


# --------------------------------------------------------------------------- #
# eBay specifics
# --------------------------------------------------------------------------- #


def ebay_auction() -> DealView:
    deal = make_deal(
        ad_id="ebay-187654321098",
        source="ebay",
        buying_options=["AUCTION"],
        bid_count=7,
        ends_at=NOW + timedelta(hours=1, minutes=40),
        shipping_cost=6.99,
        seller_feedback_percent=99.6,
        seller_feedback_score=412,
        condition="Gebraucht",
    )
    deal.listing.url = "https://www.ebay.de/itm/187654321098"
    assert deal.evaluation is not None
    deal.evaluation.max_buy_price = 375.0
    deal.evaluation.ai_second = AIVerdict(verdict="buy", confidence=0.8, model="claude-opus-5", reasoning="Ок")
    return deal


def test_ebay_auction_rendering() -> None:
    deal = ebay_auction()
    text = render_text([deal])
    html_out = render_email_html([deal])
    tg = render_telegram(deal)
    for out in (text, html_out, tg):
        assert "eBay" in out
        assert "Открыть на eBay" in out
        assert "🔨" in out
        assert "7 ставок" in out
        assert "заканчивается через 1 ч 40 мин" in out
        assert "Максимальная ставка 375" in out
        assert "доставка 6,99" in out
        assert "Второе мнение (claude-opus-5): покупать, 80 %" in out
    assert "Текущая ставка: 450 € + доставка 6,99 €" in text
    assert "99,6 % положительных отзывов (412 оценок)" in text
    assert "Состояние (по продавцу): Gebraucht" in text
    assert "Открыть объявление" not in tg
    assert deal_headline(deal) == "RTX 3090 — ставка 450 € → прибыль ≈ 160 € · eBay"
    assert email_subject([deal]) == "🔥 1 выгодное предложение: RTX 3090 — ставка 450 €, осталось 1 ч 40 мин"


def test_auction_bids_plural_and_ended() -> None:
    deal = ebay_auction()
    deal.listing.bid_count = 1
    assert "1 ставка" in render_text([deal])
    deal.listing.bid_count = 3
    assert "3 ставки" in render_text([deal])
    deal.listing.ends_at = NOW - timedelta(minutes=1)
    assert "аукцион завершён" in render_text([deal])


def test_max_buy_price_for_fixed_price() -> None:
    deal = make_deal()
    assert deal.evaluation is not None
    deal.evaluation.max_buy_price = 520.0
    assert "Выгодно до 520 €" in render_text([deal])
    assert "Выгодно до 520 €" in render_telegram(deal)


# --------------------------------------------------------------------------- #
# Telegram rendering
# --------------------------------------------------------------------------- #


def test_render_telegram_fits_caption_limit_with_huge_texts() -> None:
    long = "Очень длинное описание & <тег> 🚀 " * 400
    deal = make_deal(
        title="Grafikkarte <RTX> & mehr " * 30,
        description=long,
        reasoning=long,
        reasons=[long, long, long, long],
        red_flags=[long, long, long],
        negotiable=True,
    )
    deal.listing.location = "Sehr lange Ortsangabe " * 20
    out = render_telegram(deal, web_base_url="https://ebey.example.com")
    assert len(out) <= 1024
    assert render.telegram_length(out) <= 1024
    assert_valid_telegram_html(out)
    assert "<тег>" not in out and "<RTX>" not in out
    assert "&lt;RTX&gt;" in out
    assert "450 €" in out
    assert 'href="https://www.kleinanzeigen.de/s-anzeige/rtx-3090/2901234567-225-3331"' in out


def test_render_telegram_regular_deal() -> None:
    deal = make_deal(title="RTX 3090 <FE> & Box", red_flags=["Нет чека"])
    out = render_telegram(deal, web_base_url="https://ebey.example.com/")
    assert_valid_telegram_html(out)
    assert "<b>RTX 3090 &lt;FE&gt; &amp; Box</b>" in out
    assert "🟢 <b>Покупать</b> · 82/100 · Kleinanzeigen" in out
    assert "Прибыль ≈ <b>160 €</b> · ROI 36 %" in out
    assert "рынок ≈ 610 € (продажи eBay, выборка: 12)" in out
    assert "📍 10115 Mitte · 12 км" in out
    assert "✅ фото соответствует описанию" in out
    assert "<i>Карта целая, цена ниже рынка.</i>" in out
    assert "⚠ Нет чека" in out
    assert "• Цена на 26 % ниже рынка" in out
    assert '<a href="https://ebey.example.com/deal/2901234567">Подробнее в EbeyParser</a>' in out


def test_render_telegram_local_web_url_is_plain_text() -> None:
    out = render_telegram(make_deal(), web_base_url="http://127.0.0.1:8000")
    assert "http://127.0.0.1:8000/deal/2901234567" in out
    assert 'href="http://127.0.0.1' not in out


def test_sample_deals() -> None:
    deals = sample_deals()
    assert len(deals) == 3
    evs = [d.evaluation for d in deals]
    assert all(ev is not None for ev in evs)
    assert any(d.listing.source == "ebay" and "AUCTION" in d.listing.buying_options for d in deals)
    assert any(ev.purpose == "personal" and ev.verdict == "buy" for ev in evs if ev)
    assert any(ev.verdict == "maybe" for ev in evs if ev)
    auction = next(d for d in deals if d.listing.ends_at)
    assert auction.listing.ends_at is not None and auction.listing.ends_at > NOW
    for d in deals:
        tg = render_telegram(d, web_base_url="https://ebey.example.com")
        assert len(tg) <= 1024
        assert_valid_telegram_html(tg)
    assert "Тест" in render_email_html(deals, title="Тест")
    assert "3 выгодных предложения" in email_subject(deals)


# --------------------------------------------------------------------------- #
# EmailNotifier (fake SMTP)
# --------------------------------------------------------------------------- #


class FakeSMTP:
    def __init__(self, host: str, port: int, *, use_ssl: bool, timeout: float, fail: dict | None = None) -> None:
        self.calls: list[tuple] = [("connect", host, port, use_ssl)]
        self.fail = fail or {}
        self.sent: list[Any] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def starttls(self, context: Any = None) -> None:
        self.calls.append(("starttls",))
        self._maybe_fail("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))
        self._maybe_fail("login")

    def send_message(self, msg: Any) -> None:
        self.calls.append(("send_message",))
        self._maybe_fail("send_message")
        self.sent.append(msg)

    def quit(self) -> None:
        self.calls.append(("quit",))


class FakeFactory:
    def __init__(self, fail: dict | None = None, connect_error: BaseException | None = None) -> None:
        self.instances: list[FakeSMTP] = []
        self.fail = fail
        self.connect_error = connect_error

    def __call__(self, host: str, port: int, *, use_ssl: bool, timeout: float) -> FakeSMTP:
        if self.connect_error is not None:
            raise self.connect_error
        smtp = FakeSMTP(host, port, use_ssl=use_ssl, timeout=timeout, fail=self.fail)
        self.instances.append(smtp)
        return smtp


def email_cfg(**kw: Any) -> EmailConfig:
    base = dict(
        enabled=True,
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        username="student@gmail.com",
        password="abcd efgh ijkl mnop",
        to_addrs="me@example.com, friend@example.com",
    )
    base.update(kw)
    return EmailConfig(**base)


async def test_email_starttls_path() -> None:
    factory = FakeFactory()
    notifier = EmailNotifier(email_cfg(), web_base_url="http://localhost:8000", smtp_factory=factory)
    assert isinstance(notifier, Notifier)
    await notifier.send([make_deal()])
    smtp = factory.instances[0]
    assert [c[0] for c in smtp.calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert smtp.calls[0] == ("connect", "smtp.gmail.com", 587, False)
    assert smtp.calls[4] == ("login", "student@gmail.com", "abcd efgh ijkl mnop")
    msg = smtp.sent[0]
    assert msg["Subject"] == "🔥 1 выгодное предложение: RTX 3090 за 450 €"
    assert "student@gmail.com" in msg["From"] and "EbeyParser" in msg["From"]
    assert msg["To"] == "me@example.com, friend@example.com"
    assert msg.is_multipart()
    types = [part.get_content_type() for part in msg.iter_parts()]
    assert types == ["text/plain", "text/html"]
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    text_part = msg.get_body(preferencelist=("plain",)).get_content()
    assert "https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/1.jpg" in html_part
    assert "http://localhost:8000/deal/2901234567" in html_part
    assert "Цена: 450 €" in text_part


async def test_email_ssl_path_and_title_subject() -> None:
    factory = FakeFactory()
    cfg = email_cfg(smtp_port=465, use_ssl=True, from_addr="Deals <bot@example.com>")
    await EmailNotifier(cfg, smtp_factory=factory).send([make_deal(), make_deal(ad_id="2")], title="Тест EbeyParser")
    smtp = factory.instances[0]
    assert [c[0] for c in smtp.calls] == ["connect", "login", "send_message", "quit"]
    assert smtp.calls[0] == ("connect", "smtp.gmail.com", 465, True)
    msg = smtp.sent[0]
    assert msg["Subject"] == "Тест EbeyParser"
    assert "bot@example.com" in msg["From"] and "Deals" in msg["From"]


async def test_email_empty_deals_is_noop() -> None:
    factory = FakeFactory()
    await EmailNotifier(email_cfg(), smtp_factory=factory).send([])
    assert factory.instances == []


async def test_email_auth_error_is_wrapped_with_app_password_hint() -> None:
    factory = FakeFactory(fail={"login": smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")})
    with pytest.raises(NotifyError) as err:
        await EmailNotifier(email_cfg(), smtp_factory=factory).send([make_deal()])
    msg = str(err.value)
    assert "Пароль приложения" in msg and "App Password" in msg
    assert "535" in msg
    assert factory.instances[0].calls[-1] == ("quit",)  # connection closed anyway


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (socket.gaierror(-2, "Name or service not known"), "не удалось найти SMTP-сервер"),
        (TimeoutError("timed out"), "таймаут"),
        (ConnectionRefusedError(111, "refused"), "отказал в подключении"),
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "use_ssl"),
    ],
)
async def test_email_connection_errors_are_wrapped(exc: BaseException, needle: str) -> None:
    notifier = EmailNotifier(email_cfg(), smtp_factory=FakeFactory(connect_error=exc))
    with pytest.raises(NotifyError, match=needle):
        await notifier.send([make_deal()])


async def test_email_recipient_refused() -> None:
    factory = FakeFactory(fail={"send_message": smtplib.SMTPRecipientsRefused({"me@example.com": (550, b"no")})})
    with pytest.raises(NotifyError, match="получателей"):
        await EmailNotifier(email_cfg(), smtp_factory=factory).send([make_deal()])


async def test_email_missing_recipients_raises() -> None:
    with pytest.raises(NotifyError, match="to_addrs"):
        await EmailNotifier(email_cfg(to_addrs=[]), smtp_factory=FakeFactory()).send([make_deal()])


def test_default_smtp_factory_picks_class(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple] = []
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout: made.append(("plain", host, port)) or "plain")
    monkeypatch.setattr(
        smtplib, "SMTP_SSL", lambda host, port, timeout, context: made.append(("ssl", host, port)) or "ssl"
    )
    assert emailer_mod.default_smtp_factory("h", 587, use_ssl=False, timeout=5) == "plain"
    assert emailer_mod.default_smtp_factory("h", 465, use_ssl=True, timeout=5) == "ssl"
    assert made == [("plain", "h", 587), ("ssl", "h", 465)]


# --------------------------------------------------------------------------- #
# TelegramNotifier (httpx.MockTransport)
# --------------------------------------------------------------------------- #


class FakeTelegram:
    """Records Bot API calls; `responses` is a queue of (status, json) per call."""

    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.responses = list(responses or [])
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path == f"/bot{TOKEN}/{method}"
        self.requests.append((method, json.loads(request.content)))
        status, body = self.responses.pop(0) if self.responses else (200, {"ok": True, "result": {"message_id": 1}})
        return httpx.Response(status, json=body)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def notifier(self, web_base_url: str | None = "https://ebey.example.com") -> TelegramNotifier:
        cfg = TelegramConfig(enabled=True, bot_token=TOKEN, chat_id="987654321")
        return TelegramNotifier(
            cfg, web_base_url=web_base_url, transport=httpx.MockTransport(self.handler), sleep=self.sleep
        )


async def test_telegram_send_photo_body() -> None:
    tg = FakeTelegram()
    deal = make_deal()
    await tg.notifier().send([deal])
    assert len(tg.requests) == 1
    method, body = tg.requests[0]
    assert method == "sendPhoto"
    assert body["chat_id"] == "987654321"
    assert body["photo"] == "https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/1.jpg"
    assert body["parse_mode"] == "HTML"
    assert body["caption"] == render_telegram(deal, web_base_url="https://ebey.example.com")
    rows = body["reply_markup"]["inline_keyboard"]
    assert rows[0] == [{"text": "Открыть объявление", "url": deal.listing.url}]
    assert rows[1] == [{"text": "Подробнее в EbeyParser", "url": "https://ebey.example.com/deal/2901234567"}]
    assert tg.sleeps == []


async def test_telegram_ebay_button_text() -> None:
    tg = FakeTelegram()
    await tg.notifier().send([ebay_auction()])
    rows = tg.requests[0][1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "Открыть на eBay"


async def test_telegram_falls_back_to_send_message_on_400() -> None:
    bad = {"ok": False, "error_code": 400, "description": "Bad Request: wrong file identifier/HTTP URL specified"}
    tg = FakeTelegram([(400, bad)])
    deal = make_deal()
    await tg.notifier().send([deal])
    assert [m for m, _ in tg.requests] == ["sendPhoto", "sendMessage"]
    body = tg.requests[1][1]
    assert body["text"] == tg.requests[0][1]["caption"]
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is False
    assert body["reply_markup"]["inline_keyboard"][0][0]["url"] == deal.listing.url


async def test_telegram_drops_buttons_when_markup_rejected() -> None:
    bad = {"ok": False, "error_code": 400, "description": "Bad Request: BUTTON_URL_INVALID"}
    tg = FakeTelegram([(400, bad), (400, bad)])
    await tg.notifier().send([make_deal()])
    assert [m for m, _ in tg.requests] == ["sendPhoto", "sendMessage", "sendMessage"]
    assert "reply_markup" not in tg.requests[2][1]


async def test_telegram_400_everywhere_raises_with_description() -> None:
    bad = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}
    tg = FakeTelegram([(400, bad)] * 3)
    with pytest.raises(NotifyError, match="can't parse entities"):
        await tg.notifier().send([make_deal()])


async def test_telegram_chat_not_found_no_fallback() -> None:
    bad = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    tg = FakeTelegram([(400, bad)])
    with pytest.raises(NotifyError, match="/start"):
        await tg.notifier().send([make_deal()])
    assert len(tg.requests) == 1


async def test_telegram_no_photo_uses_send_message() -> None:
    tg = FakeTelegram()
    await tg.notifier().send([make_deal(images=[])])
    assert [m for m, _ in tg.requests] == ["sendMessage"]
    assert tg.requests[0][1]["disable_web_page_preview"] is False


async def test_telegram_429_retry_after() -> None:
    limited = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 5",
               "parameters": {"retry_after": 5}}
    tg = FakeTelegram([(429, limited)])
    await tg.notifier().send([make_deal()])
    assert [m for m, _ in tg.requests] == ["sendPhoto", "sendPhoto"]
    assert tg.sleeps == [5.0]


async def test_telegram_429_wait_is_capped_and_second_429_fails() -> None:
    limited = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 600",
               "parameters": {"retry_after": 600}}
    tg = FakeTelegram([(429, limited), (429, limited)])
    with pytest.raises(NotifyError, match="слишком много"):
        await tg.notifier().send([make_deal()])
    assert tg.sleeps == [30.0]


async def test_telegram_localhost_web_button_omitted() -> None:
    tg = FakeTelegram()
    await tg.notifier(web_base_url="http://localhost:8000").send([make_deal()])
    rows = tg.requests[0][1]["reply_markup"]["inline_keyboard"]
    assert len(rows) == 1 and rows[0][0]["text"] == "Открыть объявление"
    assert "http://localhost:8000/deal/2901234567" in tg.requests[0][1]["caption"]


async def test_telegram_header_and_cap() -> None:
    tg = FakeTelegram()
    deals = [make_deal(ad_id=str(i)) for i in range(23)]
    await tg.notifier().send(deals, title="Находки <за> день")
    methods = [m for m, _ in tg.requests]
    assert methods[0] == "sendMessage"
    assert "<b>Находки &lt;за&gt; день</b>" in tg.requests[0][1]["text"]
    assert "23 выгодных предложения" in tg.requests[0][1]["text"]
    assert methods[1:21] == ["sendPhoto"] * 20
    assert methods[21] == "sendMessage"
    assert "…и ещё 3 предложения" in tg.requests[21][1]["text"]
    assert len(methods) == 22
    assert all(s == 0.5 for s in tg.sleeps) and len(tg.sleeps) == 21  # pauses between messages


async def test_telegram_no_header_for_single_deal() -> None:
    tg = FakeTelegram()
    await tg.notifier().send([make_deal()], title="Находки")
    assert [m for m, _ in tg.requests] == ["sendPhoto"]


async def test_telegram_empty_is_noop() -> None:
    tg = FakeTelegram()
    await tg.notifier().send([])
    assert tg.requests == []


@pytest.mark.parametrize(
    ("status", "desc", "needle"),
    [
        (401, "Unauthorized", "bot_token"),
        (403, "Forbidden: bot was blocked by the user", "/start"),
        (500, "Internal Server Error", "500"),
    ],
)
async def test_telegram_errors(status: int, desc: str, needle: str) -> None:
    tg = FakeTelegram([(status, {"ok": False, "error_code": status, "description": desc})])
    with pytest.raises(NotifyError, match=needle):
        await tg.notifier().send([make_deal()])


async def test_telegram_network_error_hides_token() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    cfg = TelegramConfig(enabled=True, bot_token=TOKEN, chat_id="1")
    notifier = TelegramNotifier(cfg, transport=httpx.MockTransport(boom))
    with pytest.raises(NotifyError) as err:
        await notifier.send([make_deal()])
    assert "сетевая ошибка" in str(err.value)
    assert TOKEN not in str(err.value)


# --------------------------------------------------------------------------- #
# build_notifiers / missing_settings
# --------------------------------------------------------------------------- #


def test_build_notifiers_filters_disabled_and_misconfigured(caplog: pytest.LogCaptureFixture) -> None:
    assert build_notifiers(NotificationsConfig()) == []

    cfg = NotificationsConfig(
        email=EmailConfig(enabled=True, username="me@gmail.com", password="", to_addrs=[]),
        telegram=TelegramConfig(enabled=True, bot_token=TOKEN, chat_id="42"),
    )
    with caplog.at_level(logging.WARNING, logger="ebeyparser.notify.base"):
        notifiers = build_notifiers(cfg, web_base_url="http://localhost:8000")
    assert [n.name for n in notifiers] == ["telegram"]
    assert isinstance(notifiers[0], TelegramNotifier)
    assert notifiers[0].web_base_url == "http://localhost:8000"
    assert any("email" in r.getMessage() and "password" in r.getMessage() for r in caplog.records)

    full = NotificationsConfig(
        email=email_cfg(),
        telegram=TelegramConfig(enabled=False, bot_token=TOKEN, chat_id="42"),
    )
    notifiers = build_notifiers(full)
    assert [n.name for n in notifiers] == ["email"]
    assert isinstance(notifiers[0], EmailNotifier)
    assert all(isinstance(n, Notifier) for n in notifiers)


def test_missing_settings() -> None:
    assert missing_settings(NotificationsConfig()) == {}
    cfg = NotificationsConfig(
        email=EmailConfig(enabled=True, username="me@gmail.com"),
        telegram=TelegramConfig(enabled=True, bot_token=TOKEN, chat_id="42"),
    )
    assert missing_settings(cfg) == {"email": ["password", "to_addrs"], "telegram": []}

    empty = NotificationsConfig(email=EmailConfig(enabled=True), telegram=TelegramConfig(enabled=True))
    assert missing_settings(empty) == {
        "email": ["username", "password", "to_addrs"],
        "telegram": ["bot_token", "chat_id"],
    }

    # A local relay needs no login, but then a sender address is required.
    relay = NotificationsConfig(email=EmailConfig(enabled=True, smtp_host="localhost", smtp_port=25, to_addrs=["a@b.de"]))
    assert missing_settings(relay) == {"email": ["from_addr"]}
    relay.email.from_addr = "bot@home.lan"
    assert missing_settings(relay) == {"email": []}
