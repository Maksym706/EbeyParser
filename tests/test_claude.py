from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from ebeyparser.ai.claude import ClaudeVision, make_llm
from ebeyparser.ai.client import LLMError
from ebeyparser.config import AIConfig, SecondOpinionConfig


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeModels:
    def __init__(self, fail=False):
        self.fail = fail

    async def retrieve(self, model_id):
        if self.fail:
            raise RuntimeError("401 invalid x-api-key")
        return SimpleNamespace(id=model_id)


def fake_client(response=None, error=None, models_fail=False):
    messages = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages), models=FakeModels(models_fail)), messages


def text_response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


JPEG = b"\xff\xd8\xff\xe0data"
PNG = b"\x89PNG\r\n\x1a\nrest"


async def test_request_shape_images_schema_and_fallbacks():
    client, messages = fake_client(text_response('{"verdict": "buy"}'))
    cfg = SecondOpinionConfig(max_images=2)
    llm = ClaudeVision(cfg, client=client)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    out = await llm.chat_json("SYS", "USER", [JPEG, PNG, JPEG], schema)
    assert out == '{"verdict": "buy"}'
    req = messages.requests[0]
    assert req["model"] == "claude-opus-5" and req["system"] == "SYS"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert "temperature" not in req
    content = req["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]  # capped at max_images
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(content[0]["source"]["data"]) == JPEG


async def test_refusal_and_errors_become_llm_error():
    client, _ = fake_client(text_response("", stop_reason="refusal"))
    with pytest.raises(LLMError, match="отказался"):
        await ClaudeVision(SecondOpinionConfig(), client=client).chat_json("s", "u")
    client, _ = fake_client(error=RuntimeError("boom"))
    with pytest.raises(LLMError, match="boom"):
        await ClaudeVision(SecondOpinionConfig(), client=client).chat_json("s", "u")


async def test_health():
    client, _ = fake_client()
    ok = await ClaudeVision(SecondOpinionConfig(), client=client).health()
    assert ok["ok"] and ok["model_available"] and ok["provider"] == "anthropic"
    client, _ = fake_client(models_fail=True)
    bad = await ClaudeVision(SecondOpinionConfig(), client=client).health()
    assert not bad["ok"] and "invalid" in bad["error"]


def test_make_llm_picks_provider():
    from ebeyparser.ai.client import VisionLLM

    assert isinstance(make_llm(AIConfig()), VisionLLM)
    assert isinstance(make_llm(SecondOpinionConfig(api_key="sk-test")), ClaudeVision)
