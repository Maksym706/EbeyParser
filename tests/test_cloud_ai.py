"""Free cloud AI (docs/design/CLOUD_AI.md): the quota limiter (RPM bucket, daily cap persisted and
reset at UTC midnight, 429 Retry-After / X-RateLimit-Reset, backoff, circuit breaker), the budget
planner and the scout's «только непонятные» switch, the local fallback, the key in .env (masked,
never logged), privacy (no seller data, no contacts, eBay ads stay local), /key and /models
parsing, the thinking switch per runtime (+ <think> answers still parse), and the API
(/ai/cloud/test with a mocked OpenRouter / NVIDIA, PUT /ai/cloud, the «Облако» block).
Every network call is an httpx.MockTransport; keys are fakes (sk-or-test / nvapi-test)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ebeyparser.ai import cloud
from ebeyparser.ai.client import LLMError, VisionLLM
from ebeyparser.ai.cloud import CloudLimited, CloudRouter, QuotaLimiter
from ebeyparser.ai.evaluator import AIEvaluator
from ebeyparser.ai.prompts import VERDICT_SCHEMA
from ebeyparser.ai.scout import feedback_hints
from ebeyparser.ai.triage import TriageEngine, batch_for_model, parse_triage
from ebeyparser.config import AIConfig, LLMSettings, parse_config
from ebeyparser.db import Database
from ebeyparser.models import Listing
from ebeyparser.monitor import Monitor, make_scout_llm

from api_v1_helpers import LOCAL, make_app

OR_URL = "https://openrouter.ai/api/v1"
NV_URL = "https://integrate.api.nvidia.com/v1"
NOON = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp()
KEY_ENVS = ("OPENROUTER_API_KEY", "NVIDIA_API_KEY", "OMNIROUTE_API_KEY", "CLOUD_API_KEY")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    """A fresh process-wide quota registry and no cloud keys in the environment (restored after)."""
    for name in KEY_ENVS:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    cloud.registry().attach(None)
    cloud.registry().reset()
    yield
    cloud.registry().attach(None)
    cloud.registry().reset()


class Clock:
    """Monotonic + wall clock + sleep for the limiter, all simulated."""

    def __init__(self, wall: float = NOON):
        self.t = wall
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def limiter(clock: Clock, **kw: Any) -> QuotaLimiter:
    kw.setdefault("kind", "openrouter")
    return QuotaLimiter("test", clock=clock, wall=clock, sleep=clock.sleep, **kw)


def reply(content: str, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]}, headers=headers or {})


def triage_answer(user: str) -> str:
    n = sum(1 for line in user.splitlines() if line.startswith("["))
    items = [{"i": i, "k": "pc" if i == 0 else ("wanted" if i == 2 else "sale"),
              "p": "Apple iPhone 13 128GB" if i == 1 else "", "n": 1, "c": ["RTX 3070"] if i == 0 else [],
              "q": "x", "z": "used", "h": [], "x": [], "s": 5, "r": "проверка"} for i in range(n)]
    return json.dumps({"items": items})


def ad(**kw: Any) -> Listing:
    data = {"ad_id": "1", "url": "https://www.kleinanzeigen.de/s-anzeige/x/1", "title": "RTX 3080 10GB",
            "price": 300.0, "description": "Läuft super."}
    data.update(kw)
    return Listing(**data)


# =========================================================================== limiter
async def test_rpm_token_bucket_waits_briefly_then_fails_fast() -> None:
    clock = Clock()
    q = limiter(clock, rpm=20, daily_limit=1000)
    for _ in range(20):  # a full minute's burst
        await q.acquire("vision")
    assert clock.slept == []
    await q.acquire("vision")  # the next token in 3 s: a short wait is fine
    assert clock.slept == [pytest.approx(3.0)]
    slow = limiter(Clock(), rpm=2, daily_limit=1000)
    await slow.acquire()
    await slow.acquire()
    with pytest.raises(CloudLimited) as err:  # 30 s for the next token: never wait that long
        await slow.acquire()
    assert err.value.reason == "rpm" and "в минуту" in err.value.message_ru


async def test_daily_cap_persists_in_the_db_and_resets_at_utc_midnight() -> None:
    db = Database()
    clock = Clock()
    q = QuotaLimiter("acct", kind="openrouter", daily_limit=5, store=db, clock=clock, wall=clock, sleep=clock.sleep)
    for _ in range(5):
        await q.acquire("vision")
    with pytest.raises(CloudLimited) as err:
        await q.acquire("vision")
    assert err.value.reason == "daily" and "исчерпан" in err.value.message_ru
    again = QuotaLimiter("acct", kind="openrouter", daily_limit=5, store=db, clock=clock, wall=clock, sleep=clock.sleep)
    assert again.used == 5 and again.by_purpose == {"vision": 5}  # survives a restart
    assert again.blocked("vision") is not None
    assert json.loads(db.get_state("cloud:quota:acct")[0])["used"] == 5
    clock.t = datetime(2026, 10, 1, 0, 0, 5, tzinfo=timezone.utc).timestamp()  # 00:00:05 UTC
    assert again.blocked("vision") is None
    await again.acquire("vision")
    assert again.used == 1 and again.day == "2026-10-01"


async def test_retry_after_is_honoured_and_a_short_429_is_retried_once() -> None:
    clock = Clock()
    q = limiter(clock, rpm=20, daily_limit=1000)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}}, headers={"Retry-After": "1"})
        return reply('{"verdict": "buy"}', headers={"x-ratelimit-limit": "20", "x-ratelimit-remaining": "18"})

    llm = VisionLLM(LLMSettings(provider="openai", base_url=OR_URL, model="m:free", api_key="sk-or-test"),
                    transport=httpx.MockTransport(handler), quota=q)
    out = await llm.chat_json("s", "u", None, VERDICT_SCHEMA)
    await llm.aclose()
    assert out == '{"verdict": "buy"}' and len(calls) == 2
    assert q.count_429 == 1 and q.strikes == 0 and q.used == 2
    assert clock.slept and clock.slept[0] >= 1.0  # at least what the server asked for
    assert q.last_headers["x-ratelimit-remaining"] == "18"


async def test_long_retry_after_fails_fast_and_later_calls_send_nothing() -> None:
    clock = Clock()
    q = limiter(clock, rpm=20, daily_limit=1000)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429, text="slow down", headers={"Retry-After": "120"})

    llm = VisionLLM(LLMSettings(provider="openai", base_url=OR_URL, model="m:free", api_key="sk-or-test"),
                    transport=httpx.MockTransport(handler), quota=q)
    with pytest.raises(CloudLimited):
        await llm.chat_json("s", "u")
    assert len(seen) == 1 and q.cooldown_until >= clock.t + 119
    assert isinstance(llm.last_limited, CloudLimited)
    with pytest.raises(CloudLimited) as err:  # the pause: no request at all
        await llm.chat_json("s", "u")
    assert len(seen) == 1 and err.value.reason == "cooldown"
    clock.t += 125
    assert q.blocked("vision") is None
    await llm.aclose()


def test_rate_limit_headers_parse() -> None:
    now = NOON
    assert cloud.parse_retry({"retry-after": "7"}, now) == 7
    assert cloud.parse_retry({"x-ratelimit-reset": str(int((now + 30) * 1000))}, now) == pytest.approx(30, abs=1)
    assert cloud.parse_retry({"x-ratelimit-reset": str(int(now + 45))}, now) == pytest.approx(45, abs=1)
    assert cloud.parse_retry({"x-ratelimit-reset": "12"}, now) == 12
    assert cloud.parse_retry({"retry-after": "Wed, 30 Sep 2026 12:01:00 GMT"}, now) == pytest.approx(60, abs=1)
    assert cloud.parse_retry({}, now) is None
    assert cloud.parse_retry({"retry-after": "soon"}, now) is None


def test_per_day_429_blocks_until_the_reset_and_learns_the_real_limit() -> None:
    clock = Clock()
    q = limiter(clock)
    q.set_probe({"is_free_tier": False, "daily_limit": 1000})  # paid once — but less than $10
    q.used = 50
    q.note_429({}, '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}')
    why = q.blocked("vision")
    assert why is not None and why.reason == "daily"
    assert q.learned_daily == 50 and q.daily_limit == 50
    assert why.retry_at == cloud.next_utc_midnight(clock.t)


def test_backoff_grows_and_the_circuit_opens_after_repeated_failures() -> None:
    clock = Clock()
    q = limiter(clock, rpm=0, daily_limit=0, kind="nvidia")
    q.rng.seed(1)
    waits = [q.note_429({}, "") for _ in range(2)]
    assert 1.4 <= waits[0] <= 2.6 and waits[1] > waits[0]  # 2 s, 4 s ± jitter
    q.strikes = 0
    q.cooldown_until = 0
    for _ in range(3):
        q.note_failure("HTTP 503")
    why = q.blocked("vision")
    assert why is not None and why.reason == "cooldown"  # circuit open: ~60 s
    clock.t += 70
    assert q.blocked("vision") is None
    q.note_success(120)
    assert q.strikes == 0 and q.latency_ms == 120


# =========================================================================== budget + scout mode
def test_budget_planner_numbers() -> None:
    free = cloud.plan_budget(20, 50)
    assert (free["photos_per_day"], free["triage_calls_per_day"], free["ads_per_day"]) == (10, 40, 800)
    assert free["text_ru"] == "хватит на ~800 объявлений и 10 проверок фото в день"
    paid = cloud.plan_budget(20, 1000)
    assert (paid["photos_per_day"], paid["ads_per_day"]) == (200, 16000)
    assert "16 000" in paid["text_ru"]
    nv = cloud.plan_budget(40, 0)
    assert nv["ads_per_day"] is None and nv["text_ru"].startswith("без дневного лимита")
    assert cloud.limits_text("openrouter", 20, 50) == \
        "20 запросов в минуту · 50 в день (купи $10 кредитов один раз → 1000 в день)"
    assert cloud.limits_text("nvidia", 40, 0) == "40 запросов в минуту · без дневного лимита"


async def test_photos_come_first_the_scout_is_paced_over_the_day() -> None:
    clock = Clock(NOON)  # half the UTC day is over
    q = limiter(clock, rpm=1000, daily_limit=50)
    assert q.triage_cap() == 45  # 10 photos reserved, half of it released by noon
    for _ in range(26):  # 40 × 0.5 + a burst of 6
        await q.acquire("triage")
    with pytest.raises(CloudLimited) as err:
        await q.acquire("triage")
    assert err.value.reason == "pace"
    assert q.quota_low()
    await q.acquire("vision")  # photos of the top candidates still go
    snap = q.snapshot()
    assert snap["scout_low"] and snap["state"] == "low" and snap["used_today"] == 27


class QuotaLLM:
    """A scout model with a cloud quota attached (what the monitor looks at)."""

    def __init__(self, quota: QuotaLimiter):
        self.quota = quota
        self.last_limited = None

    async def chat_json(self, system, user, images=None, schema=None):
        await self.quota.acquire("triage")
        return triage_answer(user)

    async def health(self):
        return {"ok": True}

    async def aclose(self):
        return None


async def test_scout_switches_to_candidates_on_low_quota_and_back_after_the_reset() -> None:
    clock = Clock(NOON)
    q = limiter(clock, rpm=0, daily_limit=50)
    engine = TriageEngine(QuotaLLM(q), batch_size=5, max_batch=5)
    cfg = parse_config({"ai": {"scout": {"enabled": True, "mode": "auto"}}})
    monitor = Monitor(cfg, Database(), scout=engine)
    assert monitor._scout_mode() == "all"
    q.by_purpose["triage"] = q.used = 35
    assert monitor._scout_mode() == "candidates"
    status = monitor.scout_status()
    assert status["cloud"] == "openrouter" and "бережёт бесплатный лимит" in status["mode_ru"]
    clock.t = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc).timestamp()
    assert monitor._scout_mode() == "all"


# =========================================================================== fallback
def cloud_llm(handler, *, quota: QuotaLimiter | None = None, model: str = "nvidia/nemotron-nano-12b-v2-vl:free",
              purpose: str = "vision", **cfg: Any) -> VisionLLM:
    settings = LLMSettings(provider="openai", base_url=OR_URL, model=model, api_key="sk-or-test", **cfg)
    return VisionLLM(settings, transport=httpx.MockTransport(handler), quota=quota, purpose=purpose)


def local_llm(answer: str, seen: list | None = None) -> VisionLLM:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return reply(answer)

    return VisionLLM(LLMSettings(provider="openai", base_url="http://localhost:1234/v1", model="qwen/qwen3.5-9b"),
                     transport=httpx.MockTransport(handler))


async def test_router_uses_the_local_model_while_the_cloud_quota_is_used_up() -> None:
    clock = Clock()
    q = limiter(clock, daily_limit=1)
    q.used = 1
    hits: list[int] = []
    primary = cloud_llm(lambda r: hits.append(1) or reply("{}"), quota=q)
    router = CloudRouter(primary, local_llm('{"verdict": "maybe"}'))
    assert await router.chat_json("s", "u") == '{"verdict": "maybe"}'
    assert hits == [] and q.fallback_calls == 1
    assert q.snapshot(fallback_configured=True)["fallback_active"]
    await router.aclose()


async def test_router_falls_back_when_the_cloud_errors() -> None:
    q = limiter(Clock(), daily_limit=100)
    primary = cloud_llm(lambda r: httpx.Response(503, text="upstream down"), quota=q)
    router = CloudRouter(primary, local_llm('{"ok": 1}'))
    assert await router.chat_json("s", "u") == '{"ok": 1}'
    assert q.errors == 1 and q.strikes == 1
    assert router.cloud == "openrouter" and router.quota is q
    await router.aclose()


def hybrid_config(**extra: Any) -> Any:
    data = {"ai": {"enabled": True, "provider": "openai", "cloud": "openrouter", "base_url": OR_URL,
                   "model": "nvidia/nemotron-nano-12b-v2-vl:free",
                   "fallback": {"enabled": True, "base_url": "http://localhost:1234/v1", "model": "qwen/qwen3.5-9b"},
                   "scout": {"enabled": True, "model": "nvidia/nemotron-3-super-120b-a12b:free"}}}
    for k, v in extra.items():
        data["ai"][k] = v
    return parse_config(data)


async def test_monitor_builds_cloud_first_clients_and_picks_the_local_scout_when_limited(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monitor = Monitor(hybrid_config(), Database())
    monitor._ensure_components()
    assert isinstance(monitor._llm, CloudRouter)
    assert monitor._scout.batch_size == 16 and monitor._scout.max_batch == 20  # a big cloud model
    assert monitor._scout_local is not None and monitor._scout_local.max_batch <= 16
    quota = monitor._scout_quota()
    assert quota is monitor._llm.quota  # one account: the scout and the photos share one quota
    assert monitor._scout_pick(ebay=False) is monitor._scout
    quota.used = quota.daily_limit
    assert monitor._scout_pick(ebay=False) is monitor._scout_local
    status = monitor.cloud_status()
    assert status["enabled"] and status["provider"] == "openrouter" and status["limited"]
    assert status["fallback_configured"] and status["fallback_active"]
    assert set(status["roles"]) == {"vision", "scout"} and len(status["endpoints"]) == 1
    await monitor.aclose()


# =========================================================================== keys
@pytest.fixture()
def api(tmp_path: Path):
    app, config, db, path, _ = make_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as client:
        yield client, app, path


def test_cloud_key_is_stored_in_env_masked_and_used(api) -> None:
    client, app, path = api
    key = "sk-or-test-1234567890abcd"
    r = client.put("/api/v1/secrets", json={"openrouter_api_key": key})
    assert r.status_code == 200, r.text
    assert f"OPENROUTER_API_KEY={key}" in (path.parent / ".env").read_text()
    assert key not in path.read_text()  # never in config.yaml
    settings = client.get("/api/v1/settings")
    assert key not in settings.text
    info = settings.json()["secrets"]["openrouter_api_key"]
    assert info["set"] and info["masked"] == "…abcd" and info["in_env_file"]
    assert cloud.resolve_api_key(LLMSettings(provider="openai", cloud="openrouter", base_url=OR_URL)) == key
    bad = client.put("/api/v1/secrets", json={"nvidia_api_key": "nvapi test"})
    assert bad.status_code == 422


async def test_the_key_never_reaches_logs_or_error_texts(caplog) -> None:
    key = "sk-or-test-secret-9876"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {key}"
        return httpx.Response(401, json={"error": {"message": f"Invalid key {key}"}})

    llm = VisionLLM(LLMSettings(provider="openai", base_url=OR_URL, model="m:free", api_key=key),
                    transport=httpx.MockTransport(handler), quota=limiter(Clock(), daily_limit=100))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError) as err:
            await llm.chat_json("s", "u")
        health = await llm.health()
    await llm.aclose()
    assert "не подходит" in str(err.value) and key not in str(err.value)
    assert key not in caplog.text and key not in json.dumps(health)


# =========================================================================== privacy
async def test_cloud_prompts_carry_the_ad_only() -> None:
    listing = ad(title="RTX 3080 10GB", location="10115 Mitte", postal_code="10115", distance_km=3.2,
                 seller_name="Max Mustermann", seller_type="private", seller_feedback_score=12,
                 attributes={"Zustand": "Gut", "Aktiv seit": "01.01.2019", "Nutzertyp": "Privat"},
                 description="Läuft super. Meldet euch unter 0176 12345678 oder max.m@example.de, Preis 300 €.")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return reply('{"verdict": "maybe", "confidence": 0.5}')

    cfg = AIConfig(provider="openai", base_url=OR_URL, cloud="openrouter", model="m:free", api_key="sk-or-test")
    llm = VisionLLM(cfg, transport=httpx.MockTransport(handler), quota=limiter(Clock(), daily_limit=100))
    await AIEvaluator(llm, cfg).evaluate(listing, [])
    await llm.aclose()
    text = json.dumps(seen[0], ensure_ascii=False)
    for secret in ("Mustermann", "10115", "0176", "12345678", "max.m@example.de", "Aktiv seit", "3.2"):
        assert secret not in text, secret
    assert "RTX 3080 10GB" in text and "Mitte" in text and "Zustand: Gut" in text and "300" in text
    assert "[Telefon]" in text and "[E-Mail]" in text
    # a local model still gets the whole ad (nothing leaves the computer)
    local_seen: list[dict] = []
    local = local_llm('{"verdict": "maybe", "confidence": 0.5}', local_seen)
    await AIEvaluator(local, AIConfig(provider="openai")).evaluate(listing, [])
    await local.aclose()
    assert "Mustermann" in json.dumps(local_seen[0], ensure_ascii=False)


async def test_scout_prompt_to_the_cloud_has_contacts_masked() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return reply(triage_answer(body["messages"][1]["content"]))

    llm = cloud_llm(handler, quota=limiter(Clock(), daily_limit=100), model="nvidia/nemotron-3-super-120b-a12b:free",
                    purpose="triage")
    run = await TriageEngine(llm, batch_size=2).triage([ad(description="Anrufen: +49 176 1234567, Abholung Mitte")])
    await llm.aclose()
    assert run.ai_count == 1
    user = seen[0]["messages"][1]["content"]
    assert "1234567" not in user and "[Telefon]" in user


def test_feedback_hints_for_a_cloud_scout_are_titles_only() -> None:
    examples = {"good": [{"title": "Gaming PC RTX 3070", "bought": 350, "sold": 520}],
                "hidden": [{"title": "iPhone 8 defekt", "reason": "мой сосед продаёт"}]}
    local = feedback_hints(examples)
    assert "bought 350" in local and "сосед" in local
    private = feedback_hints(examples, private=True)
    assert "Gaming PC RTX 3070" in private and "iPhone 8 defekt" in private
    assert "350" not in private and "520" not in private and "сосед" not in private


async def test_ebay_ads_never_go_to_the_cloud(monkeypatch) -> None:
    hits: list[int] = []
    primary = cloud_llm(lambda r: hits.append(1) or reply("{}"), quota=limiter(Clock(), daily_limit=100))
    with cloud.local_only("ebay"):
        with pytest.raises(CloudLimited) as err:
            await primary.chat_json("s", "u")
        assert err.value.reason == "ebay" and "eBay" in err.value.message_ru
        router = CloudRouter(primary, local_llm('{"local": true}'))
        assert await router.chat_json("s", "u") == '{"local": true}'
    assert hits == []
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ebay_ad = ad(ad_id="ebay-1", source="ebay")
    cloud_only = Monitor(hybrid_config(fallback={"enabled": False}), Database())
    cloud_only._ensure_components()
    assert cloud_only._vision_policy(ebay_ad) == "none" and cloud_only._vision_policy(ad()) == ""
    assert cloud_only._scout_pick(ebay=True) is None  # the script path
    hybrid = Monitor(hybrid_config(), Database())
    hybrid._ensure_components()
    assert hybrid._vision_policy(ebay_ad) == "local"
    assert hybrid._scout_pick(ebay=True) is hybrid._scout_local
    allowed = Monitor(hybrid_config(cloud_send_ebay=True), Database())
    allowed._ensure_components()
    assert allowed._vision_policy(ebay_ad) == "" and allowed._scout_pick(ebay=True) is allowed._scout
    for m in (cloud_only, hybrid, allowed):
        await m.aclose()


def test_config_defaults_keep_the_local_behaviour() -> None:
    cfg = parse_config({})
    assert cfg.ai.cloud == "" and cfg.ai.rpm == 0 and cfg.ai.daily_limit == 0 and cfg.ai.thinking == "auto"
    assert not cfg.ai.fallback.usable and cfg.ai.cloud_send_ebay is False and cfg.ai.scout.fallback.enabled is False
    assert cloud.cloud_kind(cfg.ai) == ""
    assert cloud.cloud_kind(LLMSettings(base_url=NV_URL)) == "nvidia"
    assert cloud.cloud_kind(LLMSettings(base_url="http://localhost:20128/v1")) == "omniroute"
    assert cloud.registry().for_settings(cfg.ai) is None  # no quota for a local server


# =========================================================================== parsing
def test_parse_key_info_is_defensive() -> None:
    free = cloud.parse_key_info({"data": {"label": "sk-or-v1-abc…", "limit": None, "usage": 0.0, "is_free_tier": True,
                                          "rate_limit": {"requests": 20, "interval": "10s"}}})
    assert free["is_free_tier"] is True and free["daily_limit"] == 50 and free["rpm"] == 20
    assert free["rate_limit_rpm"] == 120
    paid = cloud.parse_key_info({"data": {"is_free_tier": False, "limit_remaining": 7.5}})
    assert paid["daily_limit"] == 1000 and paid["credits_remaining"] == 7.5
    for junk in (None, [], "x", {"data": "nope"}, {"data": {"is_free_tier": "yes"}}):
        info = cloud.parse_key_info(junk)
        assert info["is_free_tier"] is None and info["daily_limit"] is None


OR_MODELS = {"data": [
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "NVIDIA: Nemotron 3 Super (free)",
     "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262144,
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "supported_parameters": ["reasoning", "response_format"]},
    {"id": "nvidia/nemotron-nano-12b-v2-vl:free", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["text"]}},
    {"id": "nvidia/nemotron-3-super-120b-a12b", "pricing": {"prompt": "0.0000002", "completion": "0.0000008"}},
    {"id": "openai/text-embedding-3-small", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "black-forest-labs/flux-free:free", "architecture": {"output_modalities": ["image"]}},
    {"bogus": True}, "junk",
]}


def test_parse_models_marks_free_and_vision() -> None:
    models = cloud.parse_models(OR_MODELS, "openrouter")
    by = {m["id"]: m for m in models}
    assert set(by) == {"nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-nano-12b-v2-vl:free",
                       "nvidia/nemotron-3-super-120b-a12b"}
    assert by["nvidia/nemotron-3-super-120b-a12b:free"]["free"] and not by["nvidia/nemotron-3-super-120b-a12b"]["free"]
    assert by["nvidia/nemotron-nano-12b-v2-vl:free"]["vision"] and not by["nvidia/nemotron-3-super-120b-a12b:free"]["vision"]
    assert by["nvidia/nemotron-3-super-120b-a12b:free"]["context"] == 262144
    assert by["nvidia/nemotron-3-super-120b-a12b:free"]["reasoning"]
    nv = cloud.parse_models({"data": [{"id": "nvidia/nemotron-nano-12b-v2-vl"}, {"id": "nvidia/nv-embedqa-e5-v5"},
                                      {"id": "nvidia/llama-3.3-nemotron-super-49b-v1.5"}]}, "nvidia")
    assert [m["id"] for m in nv] == ["nvidia/llama-3.3-nemotron-super-49b-v1.5", "nvidia/nemotron-nano-12b-v2-vl"]
    assert all(m["free"] for m in nv) and nv[1]["vision"]
    assert cloud.parse_models(None) == [] and cloud.parse_models({"data": "x"}) == []


def test_model_picks_follow_the_live_list() -> None:
    free = [m for m in cloud.parse_models(OR_MODELS, "openrouter") if m["free"]]
    assert cloud.pick_models(free, "openrouter") == {"text": "nvidia/nemotron-3-super-120b-a12b:free",
                                                     "vision": "nvidia/nemotron-nano-12b-v2-vl:free"}
    renamed = cloud.parse_models({"data": [
        {"id": "nvidia/nemotron-3.1-super-130b-a13b:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "meta-llama/llama-3.3-8b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "nvidia/nemotron-nano-13b-v3-vl:free", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["image", "text"]}}]}, "openrouter")
    picks = cloud.pick_models(renamed, "openrouter")
    assert picks == {"text": "nvidia/nemotron-3.1-super-130b-a13b:free", "vision": "nvidia/nemotron-nano-13b-v3-vl:free"}
    assert cloud.pick_models([], "openrouter") == {"text": None, "vision": None}
    # the user's pick: Nemotron 3.5 Lightning reads the ads when the live list offers it, matched by name
    lightning = cloud.parse_models({"data": OR_MODELS["data"] + [
        {"id": "nvidia/nemotron-3.5-lightning:free", "pricing": {"prompt": "0", "completion": "0"}}]}, "openrouter")
    assert cloud.pick_models([m for m in lightning if m["free"]], "openrouter")["text"] == "nvidia/nemotron-3.5-lightning:free"
    renamed_l = cloud.parse_models({"data": OR_MODELS["data"] + [
        {"id": "nvidia/nemotron-3.6-lightning-v2:free", "pricing": {"prompt": "0", "completion": "0"}}]}, "openrouter")
    assert cloud.pick_models([m for m in renamed_l if m["free"]], "openrouter")["text"] == "nvidia/nemotron-3.6-lightning-v2:free"
    assert cloud.PRESETS["openrouter"].text_picks[0] == "nvidia/nemotron-3.5-lightning:free"
    text_only = [m for m in renamed if not m["vision"]]
    assert cloud.pick_models(text_only, "openrouter")["vision"] is None


def test_big_cloud_models_read_16_to_20_ads_per_call() -> None:
    assert batch_for_model("nvidia/nemotron-3-super-120b-a12b:free") == (16, 20)
    assert batch_for_model("nvidia/nemotron-3.5-lightning:free") == (16, 20)
    assert batch_for_model("nvidia/nemotron-3-super-120b-a12b") == (16, 20)
    assert batch_for_model("qwen3.5-2b") == (5, 5) and batch_for_model("qwen/qwen3.5-9b") == (10, 16)
    cfg = parse_config({"ai": {"scout": {"model": "nvidia/nemotron-3-super-120b-a12b:free", "max_tokens": 2600}}})
    engine = TriageEngine.from_config(object(), cfg.ai.scout)  # type: ignore[arg-type]
    assert (engine.batch_size, engine.max_batch) == (16, 20)


# =========================================================================== thinking
async def _first_body(settings: LLMSettings, purpose: str = "vision", answer: str = '{"verdict": "maybe"}') -> tuple[dict, httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": answer}})
        return reply(answer)

    llm = VisionLLM(settings, transport=httpx.MockTransport(handler), purpose=purpose,
                    quota=limiter(Clock(), daily_limit=100) if cloud.cloud_kind(settings) else None)
    await llm.chat_json("SYS", "USER", None, VERDICT_SCHEMA)
    await llm.aclose()
    return json.loads(seen[0].content), seen[0]


async def test_the_photo_check_turns_thinking_off_on_every_runtime() -> None:
    body, _ = await _first_body(LLMSettings(provider="openai", base_url="http://localhost:8080/v1", model="qwen3.5-9b"))
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["content"] == "SYS"  # Qwen3.5 has no soft switch
    body, _ = await _first_body(LLMSettings(provider="openai", base_url="http://localhost:1234/v1", model="qwen3-8b"))
    assert body["messages"][0]["content"] == "/no_think\nSYS"  # Qwen3 hybrid: the soft switch too
    body, _ = await _first_body(LLMSettings(provider="ollama", base_url="http://localhost:11434", model="qwen3.5:9b"))
    assert body["think"] is False
    body, req = await _first_body(LLMSettings(provider="openai", base_url=OR_URL, model="nvidia/nemotron-nano-12b-v2-vl:free",
                                              api_key="sk-or-test"))
    assert body["reasoning"] == {"enabled": False} and "chat_template_kwargs" not in body
    assert req.headers["x-title"] == "EbeyParser" and req.headers["http-referer"].startswith("http://localhost")
    assert body["max_tokens"] >= cloud.CLOUD_VISION_MAX_TOKENS
    body, _ = await _first_body(LLMSettings(provider="openai", base_url=NV_URL, api_key="nvapi-test",
                                            model="nvidia/llama-3.3-nemotron-super-49b-v1"))
    assert body["messages"][0]["content"].startswith("detailed thinking off\n")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    body, _ = await _first_body(LLMSettings(provider="openai", base_url=NV_URL, api_key="nvapi-test",
                                            model="nvidia/nemotron-nano-12b-v2-vl"))
    assert body["messages"][0]["content"].startswith("/no_think\n")
    # the second opinion keeps the model's own default; "on" asks for it explicitly
    body, _ = await _first_body(LLMSettings(provider="openai", base_url="http://localhost:1234/v1", model="qwen3.5-9b"),
                                purpose="second_opinion")
    assert "chat_template_kwargs" not in body
    body, _ = await _first_body(LLMSettings(provider="openai", base_url=OR_URL, api_key="sk-or-test", model="x:free",
                                            thinking="on"))
    assert body["reasoning"] == {"enabled": True}
    # the scout's client (purpose triage) too
    assert make_scout_llm(LLMSettings(provider="openai", base_url=OR_URL, model="m:free"))._extra_body == {
        "reasoning": {"enabled": False}}


async def test_a_rejected_thinking_switch_is_dropped_and_not_sent_again() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning" in body:
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        return reply('{"verdict": "maybe"}')

    llm = cloud_llm(handler, quota=limiter(Clock(), daily_limit=100))
    await llm.chat_json("s", "u", None, VERDICT_SCHEMA)
    await llm.chat_json("s", "u", None, VERDICT_SCHEMA)
    await llm.aclose()
    assert "reasoning" in bodies[0] and "reasoning" not in bodies[1] and "reasoning" not in bodies[2]
    assert len(bodies) == 3
    assert bodies[1]["response_format"]["type"] == "json_schema"


async def test_answers_with_think_blocks_still_parse() -> None:
    answer = ('<think>The ad says RTX 3080 {"not": "this"}.</think>\n'
              '{"product": "NVIDIA RTX 3080", "verdict": "maybe", "confidence": 0.7}')
    cfg = AIConfig(provider="openai", base_url="http://localhost:1234/v1", model="qwen3.5-9b")
    llm = VisionLLM(cfg, transport=httpx.MockTransport(lambda r: reply(answer)))
    verdict = await AIEvaluator(llm, cfg).evaluate(ad(), [])
    await llm.aclose()
    assert verdict.product == "NVIDIA RTX 3080" and verdict.confidence == 0.7
    assert cloud.strip_think("reasoning without a tag</think>{\"a\": 1}") == '{"a": 1}'
    assert cloud.strip_think("<think>cut off before the answer") == "cut off before the answer"
    assert cloud.strip_think('{"a": 1}') == '{"a": 1}'
    items = parse_triage(cloud.strip_think("<think>hmm</think>" + triage_answer("[0] x\n[1] y")), 2)
    assert set(items) == {0, 1}


# =========================================================================== API
def openrouter(*, key: str = "sk-or-test", free_tier: bool = True, fail_chat: int = 0):
    state = {"chat": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        auth = request.headers.get("authorization")
        if request.url.path.endswith("/key"):
            if auth != f"Bearer {key}":
                return httpx.Response(401, json={"error": {"message": "No auth credentials found"}})
            return httpx.Response(200, json={"data": {"label": "x", "is_free_tier": free_tier, "usage": 0}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OR_MODELS)
        state["chat"] += 1
        if state["chat"] <= fail_chat:
            return httpx.Response(429, text="Rate limit exceeded: free-models-per-min", headers={"Retry-After": "90"})
        body = json.loads(request.content)
        user = body["messages"][1]["content"]
        headers = {"x-ratelimit-limit": "20", "x-ratelimit-remaining": "19",
                   "x-ratelimit-reset": str(int((datetime.now(timezone.utc).timestamp() + 40) * 1000))}
        if isinstance(user, list):
            return reply(json.dumps({"product": "Apple iPhone 13 128GB", "search_query": "iphone 13 128gb",
                                     "verdict": "maybe", "confidence": 0.8}), headers=headers)
        return reply(triage_answer(user), headers=headers)

    return handler, state


def test_cloud_test_endpoint_reports_key_limits_models_and_capacity(api) -> None:
    client, app, path = api
    handler, state = openrouter()
    app.state.api.http_transport = httpx.MockTransport(handler)
    r = client.post("/api/v1/ai/cloud/test", json={"provider": "openrouter", "api_key": "sk-or-test", "photo": True,
                                                   "save_key": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["key_ok"] and d["key_saved"]
    assert d["summary_ru"].startswith("Ключ работает · 20 запросов в минуту · 50 в день (купи $10 кредитов один раз "
                                      "→ 1000 в день) · ответ за ")
    assert d["summary_ru"].endswith("· хватит на ~800 объявлений в день")
    assert d["picks"] == {"text": "nvidia/nemotron-3-super-120b-a12b:free", "vision": "nvidia/nemotron-nano-12b-v2-vl:free"}
    assert {m["id"] for m in d["models"]} == {"nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-nano-12b-v2-vl:free"}
    assert d["triage"]["answered"] == 5 and d["triage"]["hidden_gpu"] and d["photo"]["recognised"]
    assert d["rate_headers"]["x-ratelimit-limit"] == "20" and d["limits"]["is_free_tier"] is True
    assert d["budget"]["photos_per_day"] == 10 and d["key_masked"] == "…test"
    assert "OPENROUTER_API_KEY=sk-or-test" in (path.parent / ".env").read_text()
    assert "sk-or-test" not in r.text.replace('"key_masked": "…test"', "")
    chat = [q for q in state["requests"] if q.url.path.endswith("/chat/completions")]
    assert all(q.headers["x-title"] == "EbeyParser" for q in chat)
    assert all(json.loads(q.content)["reasoning"] == {"enabled": False} for q in chat)
    photo = next(json.loads(q.content) for q in chat if isinstance(json.loads(q.content)["messages"][1]["content"], list))
    image = photo["messages"][1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/") and "localhost" not in image  # base64, never a local URL
    # the paid tier: 1000 a day
    handler2, _ = openrouter(free_tier=False)
    app.state.api.http_transport = httpx.MockTransport(handler2)
    d2 = client.post("/api/v1/ai/cloud/test", json={"provider": "openrouter", "triage": True}).json()
    assert "1000 в день · ответ" in d2["summary_ru"] and "16 000" in d2["summary_ru"]


def test_cloud_test_bad_key_missing_key_and_a_429(api) -> None:
    client, app, _ = api
    handler, _ = openrouter()
    app.state.api.http_transport = httpx.MockTransport(handler)
    bad = client.post("/api/v1/ai/cloud/test", json={"provider": "openrouter", "api_key": "sk-or-wrong"}).json()
    assert not bad["ok"] and bad["key_ok"] is False and "не подходит" in bad["error_ru"]
    missing = client.post("/api/v1/ai/cloud/test", json={"provider": "nvidia"})
    assert missing.status_code == 422 and "api_key" in missing.json()["error"]["fields"]
    busy, _ = openrouter(fail_chat=5)
    app.state.api.http_transport = httpx.MockTransport(busy)
    r = client.post("/api/v1/ai/cloud/test", json={"provider": "openrouter", "api_key": "sk-or-test"}).json()
    assert not r["ok"] and "подожд" in r["error_ru"]


def test_cloud_test_nvidia_has_no_daily_cap(api) -> None:
    client, app, _ = api

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3-super-120b-a12b"},
                                                      {"id": "nvidia/nemotron-nano-12b-v2-vl"},
                                                      {"id": "nvidia/nv-embedqa-e5-v5"}]})
        assert request.headers["authorization"] == "Bearer nvapi-test"
        body = json.loads(request.content)
        assert body["messages"][0]["content"].startswith("/no_think\n")
        return reply("<think>ok</think>" + triage_answer(body["messages"][1]["content"]))

    app.state.api.http_transport = httpx.MockTransport(handler)
    d = client.post("/api/v1/ai/cloud/test", json={"provider": "nvidia", "api_key": "nvapi-test"}).json()
    assert d["ok"], d
    assert d["summary_ru"].startswith("Ключ работает · 40 запросов в минуту · без дневного лимита · ответ за ")
    assert d["summary_ru"].endswith("хватит на все объявления")
    assert d["picks"]["text"] == "nvidia/nemotron-3-super-120b-a12b" and d["picks"]["vision"] == "nvidia/nemotron-nano-12b-v2-vl"


def test_saving_cloud_hybrid_and_back_to_local(api) -> None:
    client, app, path = api
    r = client.put("/api/v1/ai/cloud", json={"mode": "cloud", "provider": "openrouter", "api_key": "sk-or-test-abcdef",
                                             "text_model": "nvidia/nemotron-3-super-120b-a12b:free",
                                             "vision_model": "nvidia/nemotron-nano-12b-v2-vl:free"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "cloud" and d["keys"]["openrouter"]["set"] and d["keys"]["openrouter"]["masked"] == "…cdef"
    cfg = app.state.config.ai
    assert cfg.cloud == "openrouter" and cfg.base_url == OR_URL and cfg.enabled and cfg.api_key == ""
    assert cfg.scout.enabled and cfg.scout.model == "nvidia/nemotron-3-super-120b-a12b:free"
    assert cfg.fallback.model == "qwen/qwen2.5-vl-7b" and not cfg.fallback.enabled  # the computer's model, remembered
    text = path.read_text()
    assert "sk-or-test" not in text and "# мой конфиг" in text  # comments kept, no key
    status = d["status"]
    assert status["enabled"] and status["daily_limit"] == 50 and status["used_today"] == 0
    h = client.put("/api/v1/ai/cloud", json={"mode": "hybrid", "provider": "openrouter",
                                             "text_model": "nvidia/nemotron-3-super-120b-a12b:free",
                                             "vision_model": "nvidia/nemotron-nano-12b-v2-vl:free", "send_ebay": False})
    assert h.json()["mode"] == "hybrid" and app.state.config.ai.fallback.usable
    back = client.put("/api/v1/ai/cloud", json={"mode": "local"}).json()
    ai = app.state.config.ai
    assert back["mode"] == "local" and ai.cloud == "" and ai.base_url == "http://localhost:1234/v1"
    assert ai.model == "qwen/qwen2.5-vl-7b"
    missing = client.put("/api/v1/ai/cloud", json={"mode": "cloud", "provider": "nvidia", "text_model": "x"})
    assert missing.status_code == 422 and "api_key" in missing.json()["error"]["fields"]


def test_health_and_monitor_show_the_cloud_block(api, monkeypatch) -> None:
    client, app, _ = api
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client.put("/api/v1/ai/cloud", json={"mode": "cloud", "provider": "openrouter",
                                         "text_model": "nvidia/nemotron-3-super-120b-a12b:free",
                                         "vision_model": "nvidia/nemotron-nano-12b-v2-vl:free"})
    mon = client.get("/api/v1/monitor").json()["cloud"]
    assert mon["enabled"] and mon["provider_name"] == "OpenRouter" and mon["daily_limit"] == 50
    assert mon["next_reset"] and mon["count_429_today"] == 0 and mon["fallback_active"] is False
    assert "хватит на ~800" in mon["estimate_ru"]
    quota = cloud.registry().for_settings(app.state.config.ai)
    quota.used = 50
    quota.save()
    health = client.get("/api/v1/health?ai=0").json()
    assert health["cloud"]["limited"] and health["cloud"]["state"] == "limited"
    assert any("исчерпан" in p["text_ru"] and p["action"]["href"] == "/settings/ai#cloud" for p in health["problems"])
    assert client.get("/api/v1/ai/cloud").json()["status"]["used_today"] == 50


# =========================================================================== the monitor, end to end
async def test_a_pass_with_the_cloud_out_of_quota_takes_the_ai_down_path_without_waiting(monkeypatch) -> None:
    """Photo check on a cloud endpoint whose free quota is used up: no request, no «AI down» alert
    but the quota one; the would-be deal waits in the vision queue (up to ai.vision_wait_minutes)."""
    from test_monitor import SOLD, FakeNotifier, FakeSold, FakeSource, config, make_listing

    cfg = config(ai={"enabled": True, "provider": "openai", "cloud": "openrouter", "base_url": OR_URL,
                     "model": "nvidia/nemotron-nano-12b-v2-vl:free", "api_key": "sk-or-test"})
    sent: list[httpx.Request] = []
    q = limiter(Clock(), daily_limit=3)
    q.used = 3
    llm = VisionLLM(cfg.ai, transport=httpx.MockTransport(lambda r: sent.append(r) or reply("{}")), quota=q)
    db = Database()
    monitor = Monitor(cfg, db, scraper=FakeSource([make_listing("7001", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0)]),
                      ebay=FakeSold(SOLD), evaluator=AIEvaluator(llm, cfg.ai), notifiers=[FakeNotifier()])
    events: list[tuple[str, dict]] = []
    monitor.on_event.append(lambda kind, data: events.append((kind, data)))
    summary = await monitor.run_once()
    await monitor.aclose()
    ev = db.get_evaluation("7001")
    assert sent == []  # fail fast: nothing went out
    assert ev.ai_checked is False and ev.would_buy and ev.verdict == "maybe"
    assert any("лимит" in r.lower() for r in ev.reasons) or "лимит" in (ev.ai.reasoning if ev.ai else "").lower()
    assert [a for a, *_ in db.vision_queue()] == ["7001"]  # waits for the quota / the reset
    kinds = [d["kind"] for k, d in events if k == "health_alert"]
    assert "cloud_quota" in kinds and "ai_down" not in kinds
    assert summary.ai_calls >= 0


async def test_a_paced_cloud_scout_raises_no_scout_down_alert(monkeypatch) -> None:
    from test_monitor import SOLD, FakeNotifier, FakeSold, FakeSource, config, make_listing

    clock = Clock(datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc).timestamp())  # early in the UTC day
    q = limiter(clock, rpm=1000, daily_limit=50)
    q.by_purpose["triage"] = q.used = 10  # far ahead of the even pace
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append("x")
        return reply(triage_answer(json.loads(request.content)["messages"][1]["content"]))

    llm = VisionLLM(LLMSettings(provider="openai", base_url=OR_URL, model="nvidia/nemotron-3-super-120b-a12b:free",
                                api_key="sk-or-test"), transport=httpx.MockTransport(handler), quota=q, purpose="triage")
    engine = TriageEngine(llm, model=llm.cfg.model, batch_size=16, max_batch=20, max_tokens=2600)
    cfg = config(ai={"scout": {"enabled": True}})
    listings = [make_listing(f"80{i:02d}", f"Alter Rechner {i}", 100.0 + i) for i in range(4)]
    monitor = Monitor(cfg, Database(), scraper=FakeSource(listings), ebay=FakeSold(SOLD), notifiers=[FakeNotifier()],
                      scout=engine)
    events: list[tuple[str, dict]] = []
    monitor.on_event.append(lambda kind, data: events.append((kind, data)))
    summary = await monitor.run_once()
    await llm.aclose()
    assert seen == [] and summary.scout_read == 0  # paced: the script path, no request
    assert not [d for k, d in events if k == "health_alert" and d["kind"] == "scout_down"]
    status = monitor.scout_status()
    assert status["state"] == "quota" and "обычным способом" in status["text_ru"]
    assert monitor._scout_mode() == "candidates"
