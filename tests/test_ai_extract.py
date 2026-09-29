"""v0.2 AI: structured extraction schema, no-anchoring prompt, tolerant parsing of the
new fields, output-token caps and image preparation."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from ebeyparser.ai.claude import ClaudeVision
from ebeyparser.ai.client import MAX_OUTPUT_TOKENS, VisionLLM, prepare_images
from ebeyparser.ai.evaluator import SAME_VARIANT_KEY, AIEvaluator, parse_verdict, same_variant_comparables
from ebeyparser.ai.prompts import VERDICT_SCHEMA, build_user_prompt, prompt_comparables
from ebeyparser.config import AIConfig, SecondOpinionConfig
from ebeyparser.models import AIVerdict, Comparable, Listing, PriceEstimate

JPEG = b"\xff\xd8\xff\xe0" + b"jpegdata"

FULL_ANSWER = {
    "product": "Apple iPhone 13 Pro 128GB",
    "item_type": "single",
    "variant": {"model": "iPhone 13 Pro", "storage_gb": "128", "ram_gb": None, "vram_gb": None, "edition": None},
    "condition": "used",
    "defects": ["battery_bad"],
    "locked": False,
    "stock_photos": False,
    "photo_matches_description": True,
    "red_flags": ["аккумулятор 78 %"],
    "same_variant_indexes": [0, 2],
    "estimated_market_price": 480,
    "search_query": "iphone 13 pro 128gb",
    "reasoning": "Реальные фото, аккумулятор изношен.",
    "verdict": "maybe",
    "confidence": 0.6,
}


def listing(**kw) -> Listing:
    data = {"ad_id": "1", "url": "https://www.kleinanzeigen.de/s-anzeige/x/1-173-1", "title": "iPhone 13 Pro 128GB",
            "price": 390.0, "description": "Akku 78 %, sonst top.", "image_urls": ["a", "b"], "seller_type": "private",
            "attributes": {"Zustand": "Gut", "Aktiv seit": "03.04.2016", "Bewertung": "TOP Zufriedenheit",
                           "Nutzertyp": "Privater Nutzer"}}
    data.update(kw)
    return Listing(**data)


def comps(n: int = 4) -> list[Comparable]:
    return [Comparable(title=f"iPhone 13 Pro {128 * (1 + i % 2)}GB #{i}", price=400 + 10 * i, source="ebay_sold", sold=True)
            for i in range(n)]


# ---------------------------------------------------------------- parse_verdict: new fields


def test_parse_full_extraction() -> None:
    v = parse_verdict(json.dumps(FULL_ANSWER), model="qwen", n_comparables=4)
    assert v.item_type == "single" and v.condition == "used"
    assert v.variant == {"model": "iPhone 13 Pro", "storage_gb": "128", SAME_VARIANT_KEY: "0,2"}
    assert v.defects == ["battery_bad"] and v.locked is False and v.stock_photos is False
    assert v.photo_matches_description is True and v.red_flags == ["аккумулятор 78 %"]
    assert v.estimated_market_price == 480 and v.verdict == "maybe" and v.confidence == 0.6


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("bundle", "bundle"), ("Komplett-PC", "complete_pc"), ("Gaming PC", "complete_pc"), ("Notebook", "laptop"),
     ("Ersatzteil", "part"), ("Zubehör", "accessory"), ("nur OVP", "box_only"), ("Gesuch", "wanted"),
     ("single item", "single"), ("Headset", "unclear"), (None, "unclear"), ("box_only", "box_only")],
)
def test_item_type_synonyms(raw, expected) -> None:
    assert parse_verdict(json.dumps({"verdict": "skip", "item_type": raw})).item_type == expected


def test_variant_coercion() -> None:
    v = parse_verdict(json.dumps({"verdict": "maybe", "variant": {
        "Modell": "Galaxy S21", "Speicher": "1 TB", "RAM": 8, "vram_gb": "unknown", "edition": "null", "extra": "x"}}))
    assert v.variant == {"model": "Galaxy S21", "storage_gb": "1024", "ram_gb": "8"}
    assert parse_verdict('{"verdict": "maybe", "variant": "RTX 3080 12GB"}').variant == {"model": "RTX 3080 12GB"}
    assert parse_verdict('{"verdict": "maybe", "variant": [1, 2]}').variant == {}


def test_defects_coercion_and_lock_consistency() -> None:
    v = parse_verdict(json.dumps({"verdict": "skip", "defects": "Display kaputt; iCloud gesperrt; ohne Netzteil; "
                                  "Wasserschaden; startet nicht; Akku schwach; Kratzer; unlocked; keine Mängel"}))
    assert v.defects == ["screen_broken", "locked", "missing_parts", "water_damage", "not_working", "battery_bad"]
    assert v.locked is True  # a lock among the defects sets the flag
    v = parse_verdict(json.dumps({"verdict": "skip", "defects": [], "locked": "yes"}))
    assert v.defects == ["locked"] and v.locked is True
    v = parse_verdict(json.dumps({"verdict": "buy", "defects": ["none"], "locked": None, "stock_photos": "nein"}))
    assert v.defects == [] and v.locked is None and v.stock_photos is False


def test_same_variant_indexes_tolerance() -> None:
    def picks(raw, n=4):
        return parse_verdict(json.dumps({"verdict": "buy", "same_variant_indexes": raw}), n_comparables=n).variant

    assert picks([3, 0, 0, 7, -1, "2", True]) == {SAME_VARIANT_KEY: "0,2,3"}  # out of range / junk dropped
    assert picks("[1], #3") == {SAME_VARIANT_KEY: "1,3"}
    assert picks([]) == {SAME_VARIANT_KEY: ""}  # asked, none match
    assert picks([1], n=0) == {}  # no comparables were shown: nothing to report
    assert SAME_VARIANT_KEY not in parse_verdict('{"verdict": "buy"}', n_comparables=4).variant  # not answered


def test_red_flags_capped_and_aliases() -> None:
    v = parse_verdict(json.dumps({"Verdict": "skip", "Item Type": "Konvolut", "Stock Photos": True,
                                  "Defekte": ["Riss im Display"], "red_flags": [f"флаг {i}" for i in range(9)]}))
    assert len(v.red_flags) == 5 and v.item_type == "bundle" and v.stock_photos is True
    assert v.defects == ["screen_broken"]


def test_truncated_extraction_is_salvaged() -> None:
    v = parse_verdict('{"product": "PS5", "item_type": "bundle", "locked": false, "verdict": "buy", "confidence": 0.7, "reas')
    assert (v.product, v.item_type, v.locked, v.verdict) == ("PS5", "bundle", False, "buy")


def test_same_variant_comparables_helper() -> None:
    shown = comps(4)
    v = parse_verdict(json.dumps(FULL_ANSWER), n_comparables=4)
    assert same_variant_comparables(v, shown) == [shown[0], shown[2]]
    assert same_variant_comparables(AIVerdict(), shown) is None
    assert same_variant_comparables(AIVerdict(variant={SAME_VARIANT_KEY: ""}), shown) == []
    assert same_variant_comparables(None, shown) is None


# ---------------------------------------------------------------- prompt


def test_prompt_comparables_selection() -> None:
    raw = [Comparable(title="Referenz", price=500, source="reference"), Comparable(title="A", price=0),
           Comparable(title="B", price=300), Comparable(title=" b ", price=300.0), *comps(10)]
    shown = prompt_comparables(PriceEstimate(market_price=450, comparables=raw))
    assert [c.title for c in shown][:2] == ["B", "iPhone 13 Pro 128GB #0"] and len(shown) == 8
    assert prompt_comparables(None) == [] and prompt_comparables(comparables=comps(2)) == comps(2)


def test_prompt_has_no_market_estimate_and_seller_line() -> None:
    est = PriceEstimate(market_price=777, low=700, high=850, sample_size=15, source="ebay_sold", comparables=comps(3))
    text = build_user_prompt(listing(), estimate=est, n_images=2)
    assert "777" not in text and "700 €" not in text and "850" not in text
    assert "[0] iPhone 13 Pro 128GB #0 — 400 € (verkauft)" in text and "[2]" in text and "[3]" not in text
    assert "Merkmale: Zustand: Gut" in text and "Aktiv seit" not in text.split("Merkmale:")[1].split("\n")[0]
    assert "Verkäufer: privat, Aktiv seit: 03.04.2016, Bewertung: TOP Zufriedenheit" in text
    bare = build_user_prompt(listing(image_urls=[]), n_images=0)
    assert "VERGLEICHSANGEBOTE" not in bare and "stock_photos = null" in bare


# ---------------------------------------------------------------- evaluator


class FakeModel:
    def __init__(self, answer: dict) -> None:
        self.answer, self.calls = answer, []

    async def chat_json(self, system, user, images=None, schema=None):
        self.calls.append({"system": system, "user": user, "images": images, "schema": schema})
        return json.dumps(self.answer)


async def test_evaluator_shows_comparables_and_maps_indexes() -> None:
    fake = FakeModel({**FULL_ANSWER, "same_variant_indexes": [1, 5]})
    est = PriceEstimate(market_price=999, comparables=comps(3))
    v = await AIEvaluator(fake, AIConfig(max_images=3)).evaluate(listing(), [JPEG], estimate=est)
    call = fake.calls[0]
    assert call["schema"] is VERDICT_SCHEMA and "999" not in call["user"] and "[2]" in call["user"]
    assert v.variant[SAME_VARIANT_KEY] == "1"  # index 5 was never shown
    assert same_variant_comparables(v, prompt_comparables(est)) == [est.comparables[1]]
    assert v.stock_photos is False and v.photo_matches_description is True


async def test_evaluator_explicit_comparables_and_no_images() -> None:
    fake = FakeModel(FULL_ANSWER)
    v = await AIEvaluator(fake, AIConfig()).evaluate(listing(), [], comparables=comps(2))
    assert "[1] iPhone 13 Pro 256GB #1" in fake.calls[0]["user"]
    assert v.stock_photos is None and v.photo_matches_description is None  # nothing to look at
    assert v.variant[SAME_VARIANT_KEY] == "0"


async def test_claude_gets_the_same_schema() -> None:
    requests: list[dict] = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(FULL_ANSWER))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    cfg = SecondOpinionConfig(max_images=1)
    v = await AIEvaluator(ClaudeVision(cfg, client=client), cfg).evaluate(listing(), [JPEG], estimate=PriceEstimate(comparables=comps(3)))
    fmt = requests[0]["output_config"]["format"]
    assert fmt == {"type": "json_schema", "schema": VERDICT_SCHEMA}
    assert requests[0]["max_tokens"] == 16000  # unchanged for Claude
    assert v.item_type == "single" and v.variant[SAME_VARIANT_KEY] == "0,2"


# ---------------------------------------------------------------- client: token caps


def _reply_openai(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_openai_payload_caps_output_tokens() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _reply_openai("{}")

    llm = VisionLLM(AIConfig(provider="openai", base_url="http://localhost:1234/v1"), transport=httpx.MockTransport(handler))
    await llm.chat_json("s", "u", [JPEG], VERDICT_SCHEMA)
    await llm.aclose()
    assert bodies[0]["max_tokens"] == MAX_OUTPUT_TOKENS == 700
    assert bodies[0]["response_format"]["json_schema"]["schema"] is not None


async def test_ollama_payload_caps_output_tokens_and_config_override() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})

    class Cfg(AIConfig):  # a future config field is picked up without code changes
        max_tokens: int = 300

    llm = VisionLLM(AIConfig(provider="ollama"), transport=httpx.MockTransport(handler))
    await llm.chat_json("s", "u")
    assert bodies[0]["options"]["num_predict"] == 700 and bodies[0]["options"]["num_ctx"] == 8192
    llm2 = VisionLLM(Cfg(provider="ollama"), transport=httpx.MockTransport(handler))
    await llm2.chat_json("s", "u")
    assert bodies[1]["options"]["num_predict"] == 300


# ---------------------------------------------------------------- image preparation


def test_prepare_images_without_pillow(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)  # "import PIL" raises ImportError
    big = b"\xff\xd8\xff" + b"x" * 2_000
    assert prepare_images([JPEG, b"", big]) == [JPEG, big]  # passed through unchanged
    capped = prepare_images([big, big, big], max_total_bytes=4_500)
    assert capped == [big, big]  # the third would exceed the total cap
    assert prepare_images([big], max_total_bytes=10) == [big]  # the first image is always kept
    assert prepare_images(None) == []


def _image(fmt: str, size: tuple[int, int], mode: str = "RGB") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else None).save(buf, fmt)
    return buf.getvalue()


def test_prepare_images_with_pillow() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    big_png = _image("PNG", (2000, 1500))
    small_jpeg = _image("JPEG", (800, 600))
    webp = _image("WEBP", (400, 300))
    out = prepare_images([big_png, small_jpeg, webp, b"<html>Fehler</html>", b"\xff\xd8\xff broken"])
    assert len(out) == 4  # the HTML error page is dropped, the broken JPEG passes through
    with Image.open(io.BytesIO(out[0])) as img:
        assert img.format == "JPEG" and img.size == (1024, 768)
    assert out[1] == small_jpeg  # already fine: untouched
    with Image.open(io.BytesIO(out[2])) as img:
        assert img.format == "JPEG" and img.size == (400, 300)  # WebP converted for picky servers
    assert out[3] == b"\xff\xd8\xff broken"
    assert prepare_images(out) == out  # idempotent
    with Image.open(io.BytesIO(prepare_images([_image("PNG", (1200, 900), "RGBA")], max_side=600)[0])) as img:
        assert img.mode == "RGB" and max(img.size) == 600


async def test_vision_llm_sends_prepared_images() -> None:
    pytest.importorskip("PIL")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})

    import base64

    from PIL import Image

    llm = VisionLLM(AIConfig(provider="ollama"), transport=httpx.MockTransport(handler))
    await llm.chat_json("s", "u", [_image("PNG", (3000, 1000))])
    with Image.open(io.BytesIO(base64.b64decode(bodies[0]["messages"][1]["images"][0]))) as img:
        assert img.size == (1024, 341)
