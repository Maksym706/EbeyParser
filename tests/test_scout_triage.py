"""AI scout, stage A: the batch triage prompt/schema, the tolerant parser, the engine (batching,
adaptive size, split retries, script fallback, deadline, hourly cap, model down) and the real
VisionLLM client on a mocked OpenAI-compatible server. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from ebeyparser.ai.client import LLMError, VisionLLM
from ebeyparser.ai.prompts_triage import PROMPT_KINDS, SYSTEM_PROMPT, TRIAGE_SCHEMA, ad_block, build_user_prompt
from ebeyparser.ai.triage import (
    TriageEngine,
    TriageItem,
    batch_for_model,
    blind_spot,
    check_sample,
    expected_sec_per_ad,
    model_params_b,
    parse_triage,
    sample_ads,
    scout_priority,
    script_item,
    too_small_for_triage,
)
from ebeyparser.config import LLMSettings, parse_config
from ebeyparser.models import Listing


def ad(ad_id: str, title: str, price: float | None = 100.0, text: str = "", **kw) -> Listing:
    return Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}", title=title, price=price,
                   description=text, **kw)


def item(i: int, **kw) -> dict:
    base = {"i": i, "k": "single", "p": "Apple iPhone 13 128GB", "n": 1, "c": [], "q": "iphone 13 128gb",
            "z": "used", "h": [], "x": [], "s": 6, "r": "обычный iPhone"}
    return {**base, **kw}


# ------------------------------------------------------------------ prompt / schema


def test_schema_is_strict_and_flat():
    items = TRIAGE_SCHEMA["properties"]["items"]["items"]
    assert TRIAGE_SCHEMA["additionalProperties"] is False and items["additionalProperties"] is False
    assert set(items["required"]) == set(items["properties"]) == {"i", "k", "p", "n", "c", "q", "z", "h", "x", "s", "r"}
    # flat: no nested objects inside an item (small models get lost in them)
    assert all(p["type"] != "object" for p in items["properties"].values())
    # the model sees "sale" as the explicit default kind; the parser maps it to the internal "single"
    assert items["properties"]["k"]["enum"] == list(PROMPT_KINDS) and PROMPT_KINDS[0] == "sale"
    assert "single" not in PROMPT_KINDS


def test_prompt_names_the_default_kind_and_the_german_trigger_words():
    for words in ("Default: sale", "wanted = Suche, Suche nach", "swap = Tausche, Tausch", "für Bastler",
                  "Ersatzteil", "box = nur OVP, nur Karton", "Konvolut, Sammlung", "Paket", "at most 8 words",
                  "Never copy German text", "minified JSON on one line", '"k":"sale"'):
        assert words in SYSTEM_PROMPT, words
    assert '"k":"single"' not in SYSTEM_PROMPT


def test_prompt_never_asks_for_prices_and_numbers_the_ads():
    assert "never" in SYSTEM_PROMPT and "guess prices" in SYSTEM_PROMPT
    assert "estimated" not in SYSTEM_PROMPT.lower() and "market price" not in SYSTEM_PROMPT.lower()
    listings = [ad("1", "Alter PC", 150, "RTX 3070 drin", negotiable=True), ad("2", "Sofa", None, price_text="VB"),
                ad("3", "Stuhl", None, is_free=True)]
    prompt = build_user_prompt(listings, categories=["PCs", "", ""], hints="The user's feedback:\n- good buy: x")
    assert "[0] Titel: Alter PC | Preis: 150 € VB | Kategorie: PCs | Text: RTX 3070 drin" in prompt
    assert "[1] Titel: Sofa | Preis: VB" in prompt and "[2] Titel: Stuhl | Preis: zu verschenken" in prompt
    assert "exactly 3 objects, i = 0..2" in prompt and "good buy" in prompt


def test_ad_block_clips_long_text():
    block = ad_block(0, ad("1", "T" * 300, 10, "wort " * 200))
    assert len(block) < 520 and block.endswith("…")


# ------------------------------------------------------------------------ parser


def test_parse_clean_answer():
    text = json.dumps({"items": [item(0), item(1, k="pc", p="", c=["RTX 3070", "2x DualSense Controller"], s=9,
                                                h=["pc_parts", "vague"])]})
    got = parse_triage(text, 2, model="m")
    assert got[0].product == "Apple iPhone 13 128GB" and got[0].kind == "single" and got[0].model == "m"
    assert got[1].kind == "pc" and got[1].contents == ["RTX 3070", "2x DualSense Controller"]
    assert got[1].hidden == ["pc_parts", "vague"] and got[1].interest == 9 and got[1].source == "ai"


@pytest.mark.parametrize("wrap", [
    lambda s: f"```json\n{s}\n```",
    lambda s: f"<think>hmm</think>Hier: {s} Fertig.",
    lambda s: s.replace("}", ",}").replace("]", ",]"),  # trailing commas
])
def test_parse_survives_wrappers(wrap):
    text = wrap(json.dumps({"items": [item(0), item(1, s=2)]}))
    got = parse_triage(text, 2)
    assert set(got) == {0, 1} and got[1].interest == 2


def test_parse_truncated_answer_keeps_complete_items():
    full = json.dumps({"items": [item(0), item(1), item(2)]})
    cut = full[: full.index('"i": 2') + 20]  # the third object is cut off
    got = parse_triage(cut, 3)
    assert set(got) == {0, 1}


def test_parse_other_shapes():
    assert set(parse_triage(json.dumps([item(0), item(1)]), 2)) == {0, 1}  # bare list
    by_key = {"0": {k: v for k, v in item(0).items() if k != "i"}, "1": {k: v for k, v in item(1).items() if k != "i"}}
    assert set(parse_triage(json.dumps(by_key), 2)) == {0, 1}
    assert parse_triage("Die Anzeigen sehen gut aus.", 2) == {}
    assert parse_triage("", 1) == {}


def test_parse_indexes():
    # no "i": placed by order only when the count matches
    no_i = [{k: v for k, v in item(n).items() if k != "i"} for n in range(2)]
    assert set(parse_triage(json.dumps({"items": no_i}), 2)) == {0, 1}
    assert parse_triage(json.dumps({"items": no_i}), 3) == {}
    # out of range and duplicate indexes are dropped
    got = parse_triage(json.dumps({"items": [item(0), item(0, p="dup"), item(7)]}), 2)
    assert set(got) == {0} and got[0].product == "Apple iPhone 13 128GB"


def test_parse_coerces_sloppy_values():
    raw = {"index": "1", "type": "Konvolut", "product": "none", "qty": "2 Stück", "contents": "RTX 3070, 16GB RAM",
           "query": "a b c d e f g h", "condition": "gut", "hidden": "typo; lot, nonsense", "risk": ["Betrug", "x"],
           "interest": "8/10", "reason": "  много   пробелов  "}
    got = parse_triage(json.dumps({"items": [raw]}), 2)[1]
    assert got.kind == "lot" and got.product == "" and got.qty == 2
    assert got.contents == ["RTX 3070", "16GB RAM"] and got.query == "a b c d e f"
    assert got.condition == "good" and got.hidden == ["typo", "lot"] and got.risks == ["scam"]
    assert got.interest == 8 and got.reason == "много пробелов"
    assert parse_triage(json.dumps({"items": [item(0, s=0.7)]}), 1)[0].interest == 7
    assert parse_triage(json.dumps({"items": [item(0, s=85)]}), 1)[0].interest == 8
    assert parse_triage(json.dumps({"items": [item(0, k="spaceship")]}), 1)[0].kind == "other"


def test_parse_sale_kind_and_russian_reason_only():
    got = parse_triage(json.dumps({"items": [
        item(0, k="sale", r="iPhone 13, опечатка в названии"),
        item(1, r="Akku 87 %, kleine Kratzer am Rahmen"),  # German text copied from the ad
        item(2, r="очень " * 20 + "длинная причина"),
    ]}), 3)
    assert got[0].kind == "single" and got[0].reason == "iPhone 13, опечатка в названии"
    assert got[1].reason == ""  # not Russian: dropped (the UI builds its own line)
    assert len(got[2].reason.split()) == 12


def test_script_item_and_priority():
    pc = ad("1", "Alter Gaming PC", 150, "RTX 3070 drin")
    phone = ad("2", "iPhone 13 128GB", 350)
    wanted = ad("3", "Suche PS5", None)
    lot = ad("4", "Konvolut Dachbodenfund", 50)
    assert script_item(phone).source == "script" and script_item(phone).product == "iphone 13 128gb"
    assert scout_priority(lot) > scout_priority(phone) and scout_priority(pc) > scout_priority(phone)
    assert scout_priority(wanted) < 0
    assert blind_spot(lot) and blind_spot(pc) and not blind_spot(phone)


# ------------------------------------------------------------------------ engine


class FakeLLM:
    """ChatModel double: answers from a function of the batch prompt, costs simulated seconds."""

    def __init__(self, respond, *, seconds_per_ad: float = 1.0, clock=None):
        self.respond = respond
        self.seconds_per_ad = seconds_per_ad
        self.clock = clock
        self.batches: list[int] = []
        self.systems: list[str] = []
        self.schemas: list[dict | None] = []

    async def chat_json(self, system, user, images=None, schema=None):
        n = sum(1 for line in user.splitlines() if line.startswith("["))
        self.batches.append(n)
        self.systems.append(system)
        self.schemas.append(schema)
        if self.clock is not None:
            self.clock.now += self.seconds_per_ad * n
        return self.respond(n, user)

    async def health(self):
        return {"ok": True}

    async def aclose(self):
        return None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def good(n, user):
    return json.dumps({"items": [item(i, p="Apple iPhone 13 128GB") for i in range(n)]})


def ads(n: int) -> list[Listing]:
    return [ad(str(i), f"iPhone 13 128GB Nr {i}", 300.0 + i) for i in range(n)]


async def test_engine_batches_and_tags_items():
    clock = Clock()
    llm = FakeLLM(good, clock=clock)
    engine = TriageEngine(llm, model="m", batch_size=4, min_batch=2, max_batch=8, clock=clock, wall=clock)
    run = await engine.triage(ads(10))
    assert llm.batches == [4, 4, 2] and run.calls == 3 and run.ai_count == 10 and not run.overflow
    assert {i.ad_id for i in run.items.values()} == {str(i) for i in range(10)}
    assert llm.schemas[0] is TRIAGE_SCHEMA and llm.systems[0] == SYSTEM_PROMPT
    assert engine.stats.sec_per_ad == pytest.approx(1.0) and engine.stats.triaged_last_hour == 10


async def test_engine_grows_after_successes_and_shrinks_on_broken_answers():
    clock = Clock()
    engine = TriageEngine(FakeLLM(good, clock=clock), batch_size=4, min_batch=2, max_batch=8, clock=clock, wall=clock)
    await engine.triage(ads(12))  # 3 full successes -> bigger batches
    assert engine.batch_size == 6

    def half(n, user):  # the model loses track after two ads
        return json.dumps({"items": [item(i) for i in range(min(n, 2))]})

    engine = TriageEngine(FakeLLM(half, clock=clock), batch_size=8, min_batch=2, max_batch=8, clock=clock, wall=clock)
    run = await engine.triage(ads(8))
    assert engine.batch_size == 4 and run.ai_count == 8  # split retries recovered every ad


async def test_engine_split_retry_then_script_fallback():
    calls = []

    def flaky(n, user):
        calls.append(n)
        if n > 1:
            return "kaputt"  # batches never parse
        if "Nr 3" in user:
            return "auch kaputt"  # this ad never parses
        return json.dumps({"items": [item(0)]})

    clock = Clock()
    engine = TriageEngine(FakeLLM(flaky, clock=clock), batch_size=4, min_batch=1, max_batch=4, clock=clock,
                          wall=clock)
    run = await engine.triage(ads(4))
    assert calls == [4, 2, 1, 1, 2, 1, 1]  # halves, then single ads (depth first)
    assert run.failed == ["3"] and run.items["3"].source == "script" and run.items["3"].model == "script"
    assert run.ai_count == 3 and engine.stats.failed_last_hour == 1


async def test_engine_deadline_and_hourly_cap_leave_overflow():
    clock = Clock()
    engine = TriageEngine(FakeLLM(good, seconds_per_ad=10.0, clock=clock), batch_size=2, min_batch=2, max_batch=2,
                          clock=clock, wall=clock)
    run = await engine.triage(ads(10), deadline=clock() + 45)
    assert run.ai_count in (4, 6) and len(run.overflow) == 10 - run.ai_count  # stops at the deadline
    capped = TriageEngine(FakeLLM(good, clock=clock), batch_size=4, max_per_hour=5, clock=clock, wall=clock)
    run = await capped.triage(ads(10))
    assert run.ai_count == 5 and len(run.overflow) == 5 and capped.cap_left() == 0
    clock.now += 3601
    assert capped.cap_left() == 5  # the window moves on


async def test_engine_stops_when_the_model_is_down():
    class Down(FakeLLM):
        async def chat_json(self, system, user, images=None, schema=None):
            raise LLMError("Локальная модель недоступна по адресу http://x (ConnectError)")

    engine = TriageEngine(Down(good), batch_size=4)
    run = await engine.triage(ads(6))
    assert run.error and run.items == {} and len(run.overflow) == 6
    assert engine.stats.last_error and engine.stats.snapshot(share=0.5, max_per_hour=600)["last_error"]


def test_model_size_decides_the_batch():
    assert model_params_b("qwen3.5:2b-q4_K_M") == 2.0 and model_params_b("Qwen2.5-1.5B-Instruct") == 1.5
    assert model_params_b("qwen3.6-35b-a3b") == 35.0 and model_params_b("unsloth/Qwen3.5-9B-GGUF:Q4_K_M") == 9.0
    assert model_params_b("qwen/qwen3.5-4b") == 4.0 and model_params_b("fake-scout") is None
    assert batch_for_model("qwen/qwen3.5-2b") == (5, 5) and batch_for_model("qwen3.5:4b-q4_K_M") == (10, 16)
    assert batch_for_model("llama3.2:3b") == (8, 10) and batch_for_model("") == (8, 16)
    assert too_small_for_triage("qwen3.5:0.8b-q4_K_M") and too_small_for_triage("Qwen2.5-1.5B-Instruct")
    assert not too_small_for_triage("qwen/qwen3.5-2b") and not too_small_for_triage("") \
        and not too_small_for_triage("gpt-4o")
    # the measured / catalog speed shown before the scout measured its own
    assert expected_sec_per_ad("qwen3.5:2b-q4_K_M") == 6.0 and expected_sec_per_ad("qwen/qwen3.5-4b") == 15.5
    assert expected_sec_per_ad("qwen/qwen3.5-4b", "T1") == 6.0 and expected_sec_per_ad("qwen/qwen3.5-2b", "T1") == 2.5
    assert expected_sec_per_ad("qwen3.5:0.8b") is None and expected_sec_per_ad("my-model") is None


async def test_engine_from_config_batch_by_model_size_and_halving():
    base = parse_config({}).ai.scout
    assert base.batch_size == 0 and base.max_batch == 0 and base.min_interest == 0  # 0 = by the model
    small = TriageEngine.from_config(FakeLLM(good), base.model_copy(update={"model": "qwen3.5:2b-q4_K_M"}))
    assert (small.batch_size, small.max_batch) == (5, 5)
    big = TriageEngine.from_config(FakeLLM(good), base.model_copy(update={"model": "qwen/qwen3.5-4b"}))
    assert (big.batch_size, big.max_batch) == (10, 15)  # 16, but the answer must fit max_tokens 1800
    unknown = TriageEngine.from_config(FakeLLM(good), base.model_copy(update={"model": "my-model"}))
    assert (unknown.batch_size, unknown.max_batch) == (8, 15)
    mine = TriageEngine.from_config(FakeLLM(good), base.model_copy(update={"model": "qwen3.5:2b", "batch_size": 8}))
    assert (mine.batch_size, mine.max_batch) == (8, 8)  # a set value wins

    def half(n, user):  # a 2B loses track: the adaptive halving still works
        return json.dumps({"items": [item(i) for i in range(min(n, 2))]})

    clock = Clock()
    engine = TriageEngine.from_config(FakeLLM(half, clock=clock), base.model_copy(update={"model": "qwen/qwen3.5-2b"}),
                                      clock=clock, wall=clock)
    run = await engine.triage(ads(5))
    assert engine.batch_size == 2 and run.ai_count == 5


def test_engine_caps_batch_by_answer_tokens():
    engine = TriageEngine(FakeLLM(good), batch_size=16, max_batch=16, max_tokens=700)
    assert engine.max_batch == 5 and engine.batch_size == 5


async def test_stats_text_numbers():
    clock = Clock()
    engine = TriageEngine(FakeLLM(good, clock=clock), batch_size=4, clock=clock, wall=clock)
    engine.note_arrivals(20)
    await engine.triage(ads(8))
    snap = engine.stats.snapshot(share=0.5, max_per_hour=10_000)
    assert snap["seen_last_hour"] == 20 and snap["triaged_last_hour"] == 8
    assert snap["capacity_per_hour"] == 1800  # 1 s per ad at half of the hour


async def test_sample_check():
    def answer(n, user):
        return json.dumps({"items": [
            item(0, k="pc", p="", c=["RTX 3070", "Intel Core i5-9600K"], s=9),
            item(1, p="Apple iPhone 13 128GB", h=["typo"]),
            item(2, k="wanted", p="Sony DualSense Controller", s=0),
            item(3, k="other", p="", s=2),
        ]})

    engine = TriageEngine(FakeLLM(answer), batch_size=4)
    result = check_sample(await engine.triage(sample_ads()))
    assert result["answered"] == 4 and result["hidden_gpu"] and result["typo_fixed"] and result["wanted_seen"]


# ------------------------------------------------ the real client on a mocked server


def openai_server(answer_for, seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        fmt = (body.get("response_format") or {}).get("type")
        if fmt == "json_schema":  # an old server that knows only json_object
            return httpx.Response(400, json={"error": {"message": "response_format json_schema not supported"}})
        n = sum(1 for line in body["messages"][1]["content"].splitlines() if line.startswith("["))
        return httpx.Response(200, json={"choices": [{"message": {"content": answer_for(n)}}]})

    return httpx.MockTransport(handler)


async def test_visionllm_text_only_with_schema_fallback_chain():
    seen: list[dict] = []
    llm = VisionLLM(LLMSettings(provider="openai", base_url="http://127.0.0.1:8080/v1", model="qwen2.5-3b-instruct",
                                max_tokens=1800),
                    transport=openai_server(lambda n: json.dumps({"items": [item(i) for i in range(n)]}), seen))
    engine = TriageEngine(llm, model="qwen2.5-3b-instruct", batch_size=3)
    run = await engine.triage(ads(3))
    await llm.aclose()
    assert run.ai_count == 3
    assert [b.get("response_format", {}).get("type") for b in seen] == ["json_schema", "json_object"]
    assert isinstance(seen[0]["messages"][1]["content"], str)  # text only: a plain string, no image parts
    assert seen[0]["max_tokens"] == 1800 and seen[0]["messages"][0]["content"] == SYSTEM_PROMPT


def test_config_defaults_and_own_endpoint():
    cfg = parse_config({})
    sc = cfg.ai.scout
    assert sc.enabled is False and sc.mode == "auto" and sc.base_url == "" and cfg.ai.vision_wait_minutes == 45
    assert cfg.notifications.super_deals.enabled and cfg.notifications.daily_top.enabled is False
    cfg = parse_config({"ai": {"scout": {"enabled": True, "base_url": "http://nas:8080/v1", "model": "qwen3.5-2b",
                                         "batch_size": 6}}})
    assert cfg.ai.scout.base_url == "http://nas:8080/v1" and cfg.ai.scout.batch_size == 6


def test_triage_item_roundtrip():
    it = TriageItem(ad_id="1", kind="pc", contents=["RTX 3070"], interest=9)
    again = TriageItem.model_validate(json.loads(it.model_dump_json()))
    assert again == it and again.is_ai


async def test_scout_client_turns_thinking_off():
    from ebeyparser.monitor import make_scout_llm

    ollama = make_scout_llm(LLMSettings(provider="ollama", base_url="http://nas:11434", model="qwen3.5:2b"))
    assert ollama._extra_body == {"think": False}
    await ollama.aclose()
    seen: list[dict] = []
    llm = VisionLLM(LLMSettings(provider="openai", base_url="http://nas:8080/v1", model="qwen3.5-2b"),
                    transport=openai_server(lambda n: json.dumps({"items": [item(i) for i in range(n)]}), seen),
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    await TriageEngine(llm, batch_size=2).triage(ads(2))
    await llm.aclose()
    assert all(body["chat_template_kwargs"] == {"enable_thinking": False} for body in seen)
    assert make_scout_llm(LLMSettings(provider="openai", base_url="http://x/v1", model="m"))._extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}}
