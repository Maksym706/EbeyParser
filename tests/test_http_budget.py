"""PoliteClient ban protection: hourly page budget, growing persisted cooldowns, host_status."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from ebeyparser.config import GeneralConfig
from ebeyparser.scraper.ebay_sold import EbaySoldScraper
from ebeyparser.scraper.http import BlockedError, PoliteClient, RateBudgetExceeded, format_duration

KA = "https://www.kleinanzeigen.de"
IMG = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/bb?rule=$_57.JPG"
PAGE = "<html><head><title>Ergebnisse</title></head><body><ul id='srchrslt-adtable'></ul></body></html>"
CAPTCHA = "<html><head><title>kleinanzeigen.de</title></head><body><script src='https://ct.captcha-delivery.com/c.js'></script></body></html>"
T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Handler:
    """MockTransport handler: status per path prefix, records every request."""

    def __init__(self, routes: dict[str, int | str] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        for prefix, result in self.routes.items():
            if request.url.path.startswith(prefix):
                if isinstance(result, int):
                    return httpx.Response(result, text="<html><title>x</title></html>")
                return httpx.Response(200, text=result)
        if request.url.host.startswith("img."):
            return httpx.Response(200, content=b"\xff\xd8\xffimg")
        return httpx.Response(200, text=PAGE)


def make(handler: Handler, clock: Clock, **kwargs) -> PoliteClient:
    client = PoliteClient(delay_range=(0, 0), max_retries=kwargs.pop("max_retries", 0),
                          transport=httpx.MockTransport(handler), clock=clock, **kwargs)

    async def no_sleep(seconds: float) -> None:
        return None

    client._sleep = no_sleep  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------- hourly budget


async def test_budget_raises_without_request_and_slides() -> None:
    clock, handler = Clock(), Handler()
    async with make(handler, clock, max_requests_per_hour=3) as client:
        for i in range(3):
            await client.get_text(f"{KA}/s-{i}/k0")
            clock.advance(60)
        with pytest.raises(RateBudgetExceeded) as info:
            await client.get_text(f"{KA}/s-x/k0")
        assert len(handler.requests) == 3  # nothing was sent
        err = info.value
        assert not isinstance(err, BlockedError)  # a budget is not a block: defer, don't stop
        assert err.host == "www.kleinanzeigen.de" and err.limit == 3
        assert err.retry_at.timestamp() == pytest.approx(T0 + 3600)
        assert "лимит 3 страниц в час" in str(err) and "57 мин" in str(err)
        assert client.budget_left(KA) == 0
        # other hosts have their own budget
        await client.get_text("https://www.ebay.de/sch/i.html?_nkw=x")
        # the oldest request leaves the window after an hour
        clock.t = T0 + 3600 + 1
        assert client.budget_left(f"{KA}/anything") == 1
        await client.get_text(f"{KA}/s-y/k0")
        assert len(handler.requests) == 5


async def test_retries_and_redirects_count_against_budget() -> None:
    clock = Clock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/s-suchanfrage.html":
            return httpx.Response(302, headers={"Location": "/s-rtx/k0"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, text=PAGE)

    client = PoliteClient(delay_range=(0, 0), transport=httpx.MockTransport(handler), clock=clock,
                          max_requests_per_hour=10)

    async def no_sleep(seconds: float) -> None:
        return None

    client._sleep = no_sleep  # type: ignore[method-assign]
    async with client:
        await client.get_text(f"{KA}/s-suchanfrage.html?keywords=rtx")  # 302 -> 503 -> retry: 302 -> 200
        assert calls["n"] == 4
        assert client.host_status()["www.kleinanzeigen.de"]["requests_last_hour"] == 4


async def test_images_have_their_own_generous_cap() -> None:
    clock, handler = Clock(), Handler()
    async with make(handler, clock, max_requests_per_hour=2) as client:
        assert client.image_requests_per_hour == 20  # default: 10x the page cap
        for _ in range(5):
            await client.get_bytes(IMG)
        await client.get_text(f"{KA}/a")
        await client.get_text(f"{KA}/b")  # images did not use up the page budget
    async with make(Handler(), Clock(), max_requests_per_hour=2, image_requests_per_hour=2) as client:
        await client.get_bytes(IMG)
        await client.get_bytes(IMG)
        with pytest.raises(RateBudgetExceeded, match="картинок"):
            await client.get_bytes(IMG)


async def test_unlimited_by_default_when_constructed_directly() -> None:
    clock, handler = Clock(), Handler()
    async with make(handler, clock) as client:
        for i in range(200):
            await client.get_text(f"{KA}/{i}")
        assert client.budget_left(KA) is None
        assert client.host_status()["www.kleinanzeigen.de"]["remaining"] is None


# ---------------------------------------------------------------- cooldowns


async def test_403_starts_growing_cooldown_and_fails_fast() -> None:
    clock, handler = Clock(), Handler({"/blocked": 403})
    async with make(handler, clock) as client:
        with pytest.raises(BlockedError) as first:
            await client.get_text(f"{KA}/blocked")
        assert first.value.status_code == 403 and not first.value.cooling_down
        assert first.value.cooldown_until.timestamp() == pytest.approx(T0 + 3600)
        assert "на паузе 1 ч" in str(first.value) and "№1" in str(first.value)

        clock.advance(10 * 60)
        with pytest.raises(BlockedError) as cooling:
            await client.get_text(f"{KA}/s-ok/k0")
        assert cooling.value.cooling_down and cooling.value.status_code is None
        assert "ещё 50 мин" in str(cooling.value) and "HTTP 403" in str(cooling.value)
        assert len(handler.requests) == 1  # nothing sent while cooling down
        assert client.cooldown_remaining(KA) == pytest.approx(50 * 60)
        await client.get_text("https://www.ebay.de/sch/i.html")  # other hosts are unaffected
        await client.get_bytes(IMG)  # and so are the images of the same site

        durations = []
        for _ in range(5):  # blocked again right after every cooldown: 1 h -> 2 h -> 4 h -> 12 h -> 12 h
            clock.t = client._hosts["www.kleinanzeigen.de"].cooldown_until + 1
            with pytest.raises(BlockedError) as again:
                await client.get_text(f"{KA}/blocked")
            durations.append(round((again.value.cooldown_until.timestamp() - clock.t) / 3600, 2))
        assert durations == [2.0, 4.0, 12.0, 12.0, 12.0]
        assert client.host_status()["www.kleinanzeigen.de"]["strikes"] == 6


async def test_strikes_reset_after_a_clean_day() -> None:
    clock, handler = Clock(), Handler({"/blocked": 403})
    async with make(handler, clock) as client:
        for _ in range(2):
            with pytest.raises(BlockedError):
                await client.get_text(f"{KA}/blocked")
            clock.t = client._hosts["www.kleinanzeigen.de"].cooldown_until + 1
        assert client._hosts["www.kleinanzeigen.de"].strikes == 2
        clock.advance(24 * 3600)  # a whole day without a block after the last cooldown
        await client.get_text(f"{KA}/fine")
        assert client.host_status()["www.kleinanzeigen.de"]["strikes"] == 0
        with pytest.raises(BlockedError) as info:
            await client.get_text(f"{KA}/blocked")
        assert info.value.cooldown_until.timestamp() - clock.t == pytest.approx(3600)  # back to step 1


@pytest.mark.parametrize(("routes", "path"), [({"/cap": CAPTCHA}, "/cap"), ({"/limit": 429}, "/limit")])
async def test_captcha_and_persistent_429_start_cooldown(routes, path) -> None:
    clock, handler = Clock(), Handler(routes)
    async with make(handler, clock, max_retries=1) as client:
        with pytest.raises(BlockedError):
            await client.get_text(f"{KA}{path}")
        assert client.cooldown_remaining(KA) == pytest.approx(3600)


async def test_server_errors_and_image_403_do_not_start_cooldown() -> None:
    clock, handler = Clock(), Handler({"/broken": 500, "/api/v1/prod-ads/images/gone": 403})
    async with make(handler, clock) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_text(f"{KA}/broken")
        with pytest.raises(BlockedError):
            await client.get_bytes("https://img.kleinanzeigen.de/api/v1/prod-ads/images/gone")
        assert client.cooldown_remaining(KA) == 0
        assert client.cooldown_remaining("img.kleinanzeigen.de") == 0
        await client.get_text(f"{KA}/fine")
        await client.get_bytes(IMG)


async def test_empty_cooldown_steps_disable_the_pause() -> None:
    clock, handler = Clock(), Handler({"/blocked": 403})
    async with make(handler, clock, cooldown_hours=[]) as client:
        with pytest.raises(BlockedError) as info:
            await client.get_text(f"{KA}/blocked")
        assert info.value.cooldown_until is None
        await client.get_text(f"{KA}/fine")


async def test_ebay_sold_scrape_is_quiet_while_cooling_down() -> None:
    clock, handler = Clock(), Handler({"/sch/": 403})
    async with make(handler, clock) as client:
        sold = EbaySoldScraper(client)
        with pytest.raises(BlockedError):
            await sold.sold_comparables("rtx 3090")
        for _ in range(3):  # every later pass within the hour: no request at all
            clock.advance(15 * 60 - 1)
            with pytest.raises(BlockedError) as info:
                await sold.sold_comparables("rtx 3090")
            assert info.value.cooling_down
        assert len(handler.requests) == 1


# ---------------------------------------------------------------- persistence


async def test_state_file_keeps_cooldown_and_window_across_restarts(tmp_path) -> None:
    state = tmp_path / "data" / "http_state.json"
    clock, handler = Clock(), Handler({"/blocked": 403})
    async with make(handler, clock, max_requests_per_hour=5, state_path=state) as client:
        await client.get_text(f"{KA}/a")
        await client.get_text(f"{KA}/b")
        with pytest.raises(BlockedError):
            await client.get_text("https://www.ebay.de/blocked")
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["hosts"]["www.kleinanzeigen.de"]["requests"]) == 2
    assert data["hosts"]["www.ebay.de"]["strikes"] == 1

    clock.advance(20 * 60)
    handler2 = Handler()
    async with make(handler2, clock, max_requests_per_hour=5, state_path=state) as client:
        with pytest.raises(BlockedError) as info:
            await client.get_text("https://www.ebay.de/sch/i.html")
        assert info.value.cooling_down and "ещё 40 мин" in str(info.value)
        assert client.budget_left(KA) == 3  # the two earlier pages still count
        status = client.host_status()
        assert status["www.ebay.de"]["blocked"] and status["www.ebay.de"]["cooldown_seconds"] == pytest.approx(2400)
    assert handler2.requests == []

    clock.advance(3600)  # the window has passed and the cooldown is over
    async with make(Handler(), clock, max_requests_per_hour=5, state_path=state) as client:
        assert client.budget_left(KA) == 5
        await client.get_text("https://www.ebay.de/sch/i.html")


async def test_corrupt_state_file_is_ignored(tmp_path) -> None:
    state = tmp_path / "http_state.json"
    state.write_text("{not json", encoding="utf-8")
    async with make(Handler(), Clock(), state_path=state) as client:
        assert await client.get_text(f"{KA}/a") == PAGE
    assert json.loads(state.read_text(encoding="utf-8"))["hosts"]["www.kleinanzeigen.de"]["requests"]


async def test_routine_state_writes_are_throttled(tmp_path) -> None:
    state = tmp_path / "http_state.json"
    clock = Clock()
    client = make(Handler(), clock, state_path=state)
    await client.get_text(f"{KA}/a")
    first = state.read_text(encoding="utf-8")
    await client.get_text(f"{KA}/b")  # within 30 s: not written yet
    assert state.read_text(encoding="utf-8") == first
    clock.advance(31)
    await client.get_text(f"{KA}/c")
    assert len(json.loads(state.read_text(encoding="utf-8"))["hosts"]["www.kleinanzeigen.de"]["requests"]) == 3
    await client.aclose()


# ---------------------------------------------------------------- status / config


async def test_host_status_shape() -> None:
    clock, handler = Clock(), Handler({"/blocked": 403})
    async with make(handler, clock, max_requests_per_hour=150) as client:
        await client.get_text(f"{KA}/a")
        await client.get_bytes(IMG)
        with pytest.raises(BlockedError):
            await client.get_text("https://www.ebay.de/blocked")
        status = client.host_status()
    ka = status["www.kleinanzeigen.de"]
    assert ka["requests_last_hour"] == 1 and ka["limit_per_hour"] == 150 and ka["remaining"] == 149
    assert ka["blocked"] is False and ka["cooldown_until"] is None and ka["strikes"] == 0
    assert "страниц за час: 1 из 150" == ka["note"]
    assert status["img.kleinanzeigen.de"]["images_last_hour"] == 1
    ebay = status["www.ebay.de"]
    assert ebay["blocked"] is True and ebay["strikes"] == 1 and ebay["last_block_reason"] == "HTTP 403"
    assert ebay["cooldown_until"] - ebay["last_block_at"] == timedelta(hours=1)
    assert ebay["note"].startswith("пауза после блокировки ещё 1 ч")


async def test_from_config_applies_limits_and_state_path(tmp_path) -> None:
    general = GeneralConfig(max_requests_per_hour=40, block_cooldown_hours=[0.5, 6])
    client = PoliteClient.from_config(general, state_path=tmp_path / "s.json")
    assert client.max_requests_per_hour == 40 and client.image_requests_per_hour == 400
    assert client.cooldown_hours == (0.5, 6.0) and client.state_path == tmp_path / "s.json"
    await client.aclose()
    default = PoliteClient.from_config(GeneralConfig())
    assert default.max_requests_per_hour == 150 and default.cooldown_hours == (1.0, 2.0, 4.0, 12.0)
    assert default.state_path is None
    await default.aclose()


def test_format_duration() -> None:
    assert [format_duration(s) for s in (0, 20, 60, 3600, 3720, 12 * 3600)] == [
        "меньше минуты", "1 мин", "1 мин", "1 ч", "1 ч 2 мин", "12 ч"]
