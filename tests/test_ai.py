from __future__ import annotations

import base64
import json
from datetime import timedelta

import httpx
import pytest

from ebeyparser.ai.client import LLMError, VisionLLM, image_mime
from ebeyparser.ai.evaluator import PARSE_FAILED, AIEvaluator, parse_verdict
from ebeyparser.ai.prompts import SYSTEM_PROMPT, VERDICT_SCHEMA, build_user_prompt
from ebeyparser.config import AIConfig
from ebeyparser.models import Comparable, Listing, PriceEstimate, utcnow

JPEG = b"\xff\xd8\xff\xe0" + b"jpegdata"
PNG = b"\x89PNG\r\n\x1a\n" + b"pngdata"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
GIF = b"GIF89a" + b"gifdata"

GOOD_ANSWER = {
    "product": "NVIDIA GeForce RTX 3090",
    "search_query": "rtx 3090",
    "photo_matches_description": True,
    "condition": "good",
    "red_flags": [],
    "estimated_market_price": 600,
    "reasoning": "Реальные фото, цена хорошая.",
    "verdict": "buy",
    "confidence": 0.8,
}


def make_listing(**kw) -> Listing:
    data = {
        "ad_id": "42",
        "url": "https://www.kleinanzeigen.de/s-anzeige/42",
        "title": "Gigabyte RTX 3090 Gaming OC 24GB",
        "price": 450.0,
        "negotiable": True,
        "location": "10115 Mitte",
        "description": "Verkaufe meine Grafikkarte. Läuft einwandfrei.",
        "image_urls": ["a", "b", "c", "d"],
        "seller_type": "private",
        "attributes": {"Zustand": "Gut"},
    }
    data.update(kw)
    return Listing(**data)


class Recorder:
    """httpx.MockTransport handler that records requests and replays responses."""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def make_llm(recorder: Recorder, **cfg) -> VisionLLM:
    return VisionLLM(AIConfig(**cfg), transport=httpx.MockTransport(recorder))


# --------------------------------------------------------------------------- parse_verdict


def test_parse_clean_json():
    v = parse_verdict(json.dumps(GOOD_ANSWER), model="m")
    assert v.verdict == "buy" and v.confidence == 0.8
    assert v.product == "NVIDIA GeForce RTX 3090"
    assert v.estimated_market_price == 600
    assert v.photo_matches_description is True
    assert v.model == "m"


def test_parse_fenced_json_with_coercions():
    text = (
        "Hier ist meine Analyse:\n```json\n"
        '{"verdict": "kaufen", "confidence": 80, "estimated_market_price": "1.200 €", '
        '"condition": "wie neu", "photo_matches_description": "ja", '
        '"red_flags": "Vorkasse, нет зарядки"}\n```\nViel Erfolg!'
    )
    v = parse_verdict(text)
    assert v.verdict == "buy"
    assert v.confidence == pytest.approx(0.8)
    assert v.estimated_market_price == 1200
    assert v.condition == "like_new"
    assert v.photo_matches_description is True
    assert v.red_flags == ["Vorkasse", "нет зарядки"]


def test_parse_noisy_text_outermost_object():
    text = (
        'Sure! {"product": "iPhone 13", "verdict": "не покупать", "confidence": "0,7", '
        '"reasoning": "Похоже на {подделку}", "red_flags": ["нет"], "extra": {"a": 1}} thanks'
    )
    v = parse_verdict(text)
    assert v.product == "iPhone 13"
    assert v.verdict == "skip"
    assert v.confidence == pytest.approx(0.7)
    assert v.reasoning == "Похоже на {подделку}"
    assert v.red_flags == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("buy", "buy"),
        ("Kaufen", "buy"),
        ("купить", "buy"),
        ("yes", "buy"),
        ("vielleicht", "maybe"),
        ("Возможно", "maybe"),
        ("не уверен", "maybe"),
        ("nicht kaufen", "skip"),
        ("нет", "skip"),
        ("пропустить", "skip"),
        ("SKIP", "skip"),
        ("???", "maybe"),
    ],
)
def test_parse_verdict_synonyms(raw, expected):
    assert parse_verdict(json.dumps({"verdict": raw, "confidence": 0.5})).verdict == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("neu", "new"),
        ("новый", "new"),
        ("wie neu", "like_new"),
        ("neuwertig", "like_new"),
        ("gut", "good"),
        ("хорошее", "good"),
        ("gebraucht", "used"),
        ("б/у", "used"),
        ("defekt", "defective"),
        ("сломан", "defective"),
        ("хорошее, без дефектов", "good"),
        ("like_new", "like_new"),
        ("???", "unclear"),
    ],
)
def test_parse_condition_synonyms(raw, expected):
    assert parse_verdict(json.dumps({"verdict": "maybe", "condition": raw})).condition == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("да", True), ("yes", True), ("nein", False), ("не совпадает", False), (None, None), ("unklar", None)],
)
def test_parse_photo_match(raw, expected):
    v = parse_verdict(json.dumps({"verdict": "maybe", "photo_matches_description": raw}))
    assert v.photo_matches_description is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("450 €", 450.0), ("450,00", 450.0), ("1.200", 1200.0), ("ca. 400-500 €", 450.0), (350, 350.0), ("unbekannt", None), (0, None)],
)
def test_parse_price(raw, expected):
    v = parse_verdict(json.dumps({"verdict": "maybe", "estimated_market_price": raw}))
    assert v.estimated_market_price == expected


@pytest.mark.parametrize(("raw", "expected"), [(80, 0.8), ("80%", 0.8), (0.65, 0.65), (150, 1.0), (-1, 0.0), ("hoch", 0.8)])
def test_parse_confidence(raw, expected):
    assert parse_verdict(json.dumps({"verdict": "buy", "confidence": raw})).confidence == pytest.approx(expected)


def test_parse_python_style_and_trailing_comma():
    v = parse_verdict("{'verdict': 'skip', 'confidence': 0.9, 'photo_matches_description': None,}")
    assert v.verdict == "skip" and v.confidence == 0.9


def test_parse_think_block_and_aliases():
    text = '<think>{"verdict": "buy"}</think>{"Verdict": "maybe", "Reason": "Мало данных", "Flags": ["Стоковые фото"]}'
    v = parse_verdict(text)
    assert v.verdict == "maybe"
    assert v.reasoning == "Мало данных"
    assert v.red_flags == ["Стоковые фото"]


def test_parse_truncated_output_salvaged():
    v = parse_verdict('{"product": "PS5", "verdict": "buy", "confidence": 0.9, "reasoning": "Цена ни')
    assert v.verdict == "buy" and v.confidence == 0.9 and v.product == "PS5"


@pytest.mark.parametrize("garbage", ["", "no json here", "{broken", "[1, 2, 3]", None])
def test_parse_garbage_never_raises(garbage):
    v = parse_verdict(garbage, model="m")  # type: ignore[arg-type]
    assert v.verdict == "maybe"
    assert v.confidence == 0.0
    assert v.reasoning == PARSE_FAILED
    assert v.model == "m"


# --------------------------------------------------------------------------- prompts


def test_schema_is_strict_and_complete():
    props = VERDICT_SCHEMA["properties"]
    assert set(props) == {
        "product",
        "search_query",
        "photo_matches_description",
        "condition",
        "red_flags",
        "estimated_market_price",
        "verdict",
        "confidence",
        "reasoning",
    }
    assert set(VERDICT_SCHEMA["required"]) == set(props)
    assert VERDICT_SCHEMA["additionalProperties"] is False
    dumped = json.dumps(VERDICT_SCHEMA)
    for forbidden in ("minimum", "maximum", "minLength", "maxLength"):
        assert forbidden not in dumped
    assert "Russian" in SYSTEM_PROMPT and "JSON" in SYSTEM_PROMPT


def test_build_user_prompt_kleinanzeigen():
    est = PriceEstimate(
        market_price=600,
        low=550,
        high=650,
        sample_size=12,
        source="ebay_sold",
        comparables=[Comparable(title=f"RTX 3090 #{i}", price=500 + i, sold=True) for i in range(8)],
    )
    text = build_user_prompt(make_listing(description="x" * 5000), estimate=est, n_images=3)
    assert "Kleinanzeigen" in text
    assert "Gigabyte RTX 3090 Gaming OC 24GB" in text
    assert "450 € VB" in text
    assert "10115 Mitte" in text
    assert "Zustand: Gut" in text
    assert "privat" in text
    assert "Fotos: 3 angehängt (von 4" in text
    assert "~600 €" in text and "550 €" in text and "12" in text
    assert text.count("RTX 3090 #") == 5
    assert "x" * 2500 in text and "x" * 2600 not in text
    assert "resale" in text


def test_build_user_prompt_personal_free_ebay_auction():
    listing = make_listing(
        source="ebay",
        price=120,
        negotiable=False,
        condition="Gebraucht",
        shipping_cost=6.99,
        buying_options=["AUCTION"],
        bid_count=4,
        ends_at=utcnow() + timedelta(hours=5),
        seller_feedback_score=250,
        seller_feedback_percent=99.5,
    )
    text = build_user_prompt(listing, purpose="personal", target_price=300)
    assert "eBay" in text
    assert "Gebraucht" in text
    assert "6,99 €" in text
    assert "AUCTION" in text and "4 Gebote" in text and "endet" in text
    assert "250 Bewertungen" in text and "99.5%" in text
    assert "personal" in text and "300 €" in text
    free = build_user_prompt(make_listing(price=None, is_free=True, image_urls=[]), n_images=0)
    assert "Zu verschenken" in free and "keine angehängt" in free


# --------------------------------------------------------------------------- client: Ollama


async def test_ollama_request_body_with_images_and_schema():
    rec = Recorder(httpx.Response(200, json={"message": {"role": "assistant", "content": '{"verdict":"buy"}'}}))
    llm = make_llm(rec, provider="ollama", base_url="http://gpu-box:11434/", model="qwen2.5vl:7b", temperature=0.1)
    out = await llm.chat_json("SYS", "USER", [JPEG, PNG], VERDICT_SCHEMA)
    await llm.aclose()
    assert out == '{"verdict":"buy"}'
    req = rec.requests[0]
    assert req.method == "POST" and str(req.url) == "http://gpu-box:11434/api/chat"
    body = rec.body()
    assert body["model"] == "qwen2.5vl:7b"
    assert body["stream"] is False
    assert body["format"] == VERDICT_SCHEMA
    assert body["options"]["temperature"] == 0.1
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    user = body["messages"][1]
    assert user["role"] == "user" and user["content"] == "USER"
    assert [base64.b64decode(i) for i in user["images"]] == [JPEG, PNG]


async def test_ollama_without_schema_uses_json_mode_and_strips_v1():
    rec = Recorder(httpx.Response(200, json={"message": {"content": "{}"}}))
    llm = make_llm(rec, provider="ollama", base_url="http://localhost:11434/v1")
    await llm.chat_json("s", "u")
    assert str(rec.requests[0].url) == "http://localhost:11434/api/chat"
    body = rec.body()
    assert body["format"] == "json"
    assert "images" not in body["messages"][1]


async def test_ollama_model_missing_error():
    rec = Recorder(httpx.Response(404, json={"error": "model 'foo' not found, try pulling it first"}))
    llm = make_llm(rec, provider="ollama", model="foo")
    with pytest.raises(LLMError, match="ollama pull foo"):
        await llm.chat_json("s", "u")


async def test_ollama_health():
    rec = Recorder(
        httpx.Response(200, json={"models": [{"name": "qwen2.5vl:7b"}, {"name": "gemma3:latest"}]})
    )
    llm = make_llm(rec, provider="ollama", model="gemma3")
    info = await llm.health()
    assert str(rec.requests[0].url) == "http://localhost:11434/api/tags"
    assert info["ok"] is True and info["model_available"] is True
    assert info["models"] == ["qwen2.5vl:7b", "gemma3:latest"]
    assert info["provider"] == "ollama" and info["error"] is None

    missing = await make_llm(rec, provider="ollama", model="minicpm-v").health()
    assert missing["ok"] is False and missing["model_available"] is False
    assert "ollama pull minicpm-v" in missing["error"]


async def test_health_connection_refused():
    rec = Recorder(httpx.ConnectError("refused"))
    info = await make_llm(rec, provider="ollama").health()
    assert info["ok"] is False
    assert "запущена ли Ollama" in info["error"]


async def test_connection_error_is_llm_error():
    rec = Recorder(httpx.ConnectError("refused"))
    llm = make_llm(rec, provider="ollama", base_url="http://127.0.0.1:11434")
    with pytest.raises(LLMError, match="недоступна по адресу http://127.0.0.1:11434"):
        await llm.chat_json("s", "u", [JPEG])


async def test_timeout_is_llm_error():
    rec = Recorder(httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError, match="не ответила"):
        await make_llm(rec, provider="ollama").chat_json("s", "u")


def test_unsupported_provider():
    with pytest.raises(LLMError):
        VisionLLM(AIConfig(provider="anthropic"))


# --------------------------------------------------------------------------- client: OpenAI-compatible


def openai_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


async def test_openai_body_with_data_urls_and_auth():
    rec = Recorder(openai_reply('{"verdict": "maybe"}'))
    llm = make_llm(rec, provider="openai", base_url="http://localhost:1234", model="qwen2-vl", api_key="sk-local")
    out = await llm.chat_json("SYS", "USER", [JPEG, PNG, WEBP, GIF], VERDICT_SCHEMA)
    assert out == '{"verdict": "maybe"}'
    req = rec.requests[0]
    assert str(req.url) == "http://localhost:1234/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-local"
    body = rec.body()
    assert body["model"] == "qwen2-vl"
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "verdict", "strict": True, "schema": VERDICT_SCHEMA},
    }
    assert body["temperature"] == 0.2
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "USER"}
    urls = [p["image_url"]["url"] for p in parts[1:]]
    assert urls[0] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    assert urls[1].startswith("data:image/png;base64,")
    assert urls[2].startswith("data:image/webp;base64,")
    assert urls[3].startswith("data:image/gif;base64,")


async def test_openai_v1_not_doubled_and_no_auth_header():
    rec = Recorder(openai_reply("{}"))
    llm = make_llm(rec, provider="openai", base_url="http://localhost:8080/v1/")
    await llm.chat_json("s", "u")
    assert str(rec.requests[0].url) == "http://localhost:8080/v1/chat/completions"
    assert "authorization" not in rec.requests[0].headers
    assert rec.body()["messages"][1]["content"] == "u"  # plain string without images


async def test_openai_retries_without_response_format():
    rec = Recorder(
        httpx.Response(400, json={"error": "'response_format.type' must be 'json_schema' or 'text'"}),
        openai_reply('{"verdict": "skip"}'),
    )
    llm = make_llm(rec, provider="openai", base_url="http://localhost:1234")
    out = await llm.chat_json("s", "u", [JPEG])
    assert out == '{"verdict": "skip"}'
    assert len(rec.requests) == 2
    assert "response_format" in rec.body(0)
    assert "response_format" not in rec.body(1)


async def test_openai_falls_back_from_json_schema_to_json_object():
    rec = Recorder(
        httpx.Response(400, json={"error": "response_format json_schema is not supported"}),
        openai_reply('{"verdict": "buy"}'),
    )
    llm = make_llm(rec, provider="openai", base_url="http://localhost:1234")
    out = await llm.chat_json("s", "u", [JPEG], VERDICT_SCHEMA)
    assert out == '{"verdict": "buy"}'
    assert rec.body(0)["response_format"]["type"] == "json_schema"
    assert rec.body(1)["response_format"] == {"type": "json_object"}


async def test_openai_other_400_is_error_without_retry():
    rec = Recorder(httpx.Response(400, json={"error": {"message": "context length exceeded"}}))
    llm = make_llm(rec, provider="openai", base_url="http://localhost:1234")
    with pytest.raises(LLMError, match="context length exceeded"):
        await llm.chat_json("s", "u")
    assert len(rec.requests) == 1


async def test_openai_health():
    rec = Recorder(httpx.Response(200, json={"data": [{"id": "qwen2.5-vl-7b-instruct"}, {"id": "gemma-3-4b"}]}))
    llm = make_llm(rec, provider="openai", base_url="http://localhost:1234/v1", model="gemma-3-4b")
    info = await llm.health()
    assert str(rec.requests[0].url) == "http://localhost:1234/v1/models"
    assert info["ok"] is True and info["model_available"] is True
    assert info["models"] == ["qwen2.5-vl-7b-instruct", "gemma-3-4b"]
    assert info["provider"] == "openai"


def test_image_mime():
    assert image_mime(JPEG) == "image/jpeg"
    assert image_mime(PNG) == "image/png"
    assert image_mime(WEBP) == "image/webp"
    assert image_mime(GIF) == "image/gif"
    assert image_mime(b"????") == "image/jpeg"


# --------------------------------------------------------------------------- AIEvaluator


async def test_evaluator_limits_images_and_sets_model():
    rec = Recorder(httpx.Response(200, json={"message": {"content": json.dumps(GOOD_ANSWER)}}))
    cfg = AIConfig(provider="ollama", model="gemma3:4b", max_images=2)
    evaluator = AIEvaluator(VisionLLM(cfg, transport=httpx.MockTransport(rec)), cfg)
    v = await evaluator.evaluate(make_listing(), [JPEG, PNG, JPEG, PNG], purpose="personal", target_price=500)
    assert v.verdict == "buy" and v.model == "gemma3:4b"
    body = rec.body()
    assert len(body["messages"][1]["images"]) == 2
    assert body["messages"][0]["content"] == SYSTEM_PROMPT
    assert body["format"] == VERDICT_SCHEMA
    assert "Fotos: 2 angehängt" in body["messages"][1]["content"]
    assert "personal" in body["messages"][1]["content"]


async def test_evaluator_no_images_means_no_photo_judgement():
    rec = Recorder(httpx.Response(200, json={"message": {"content": json.dumps(GOOD_ANSWER)}}))
    cfg = AIConfig(provider="ollama")
    v = await AIEvaluator(VisionLLM(cfg, transport=httpx.MockTransport(rec)), cfg).evaluate(make_listing(), [])
    assert v.photo_matches_description is None


async def test_evaluator_fallback_on_connection_error(caplog):
    rec = Recorder(httpx.ConnectError("refused"))
    cfg = AIConfig(provider="ollama", model="qwen2.5vl:7b")
    evaluator = AIEvaluator(VisionLLM(cfg, transport=httpx.MockTransport(rec)), cfg)
    with caplog.at_level("WARNING", logger="ebeyparser.ai.evaluator"):
        v = await evaluator.evaluate(make_listing(), [JPEG])
    assert v.verdict == "maybe" and v.confidence == 0.0
    assert "недоступна" in v.reasoning
    assert v.model == "qwen2.5vl:7b"
    assert any("AI check failed" in r.message for r in caplog.records)


async def test_evaluator_works_with_duck_typed_client():
    class FakeClaude:
        def __init__(self):
            self.calls = []

        async def chat_json(self, system, user, images=None, schema=None):
            self.calls.append((system, user, images, schema))
            return json.dumps({**GOOD_ANSWER, "verdict": "skip", "confidence": 0.9})

        async def health(self):
            return {"ok": True}

        async def aclose(self):
            return None

    fake = FakeClaude()
    cfg = AIConfig(provider="anthropic", model="claude-x", max_images=1)
    v = await AIEvaluator(fake, cfg).evaluate(make_listing(), [JPEG, PNG])
    assert v.verdict == "skip" and v.model == "claude-x"
    assert fake.calls[0][2] == [JPEG]


async def test_evaluator_unexpected_exception_does_not_raise():
    class Broken:
        async def chat_json(self, *a, **k):
            raise RuntimeError("boom")

    cfg = AIConfig()
    v = await AIEvaluator(Broken(), cfg).evaluate(make_listing(), [])  # type: ignore[arg-type]
    assert v.verdict == "maybe" and v.confidence == 0.0 and "boom" in v.reasoning
