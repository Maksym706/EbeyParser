"""Optional cloud model (Claude) with the same interface as the local VisionLLM.

Only used when you switch it on (ai.provider: anthropic, or ai.second_opinion).
Needs `pip install anthropic` and an API key (ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from .client import LLMError, image_mime

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeVision:
    """chat_json / health / aclose — drop-in for VisionLLM."""

    def __init__(self, cfg: Any, client: Any | None = None):
        self.cfg = cfg
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # optional dependency
                error = LLMError("Для Claude установи пакет: pip install anthropic (или pip install -e .[claude])")
                error.message_ru = ("Для проверки через Claude не хватает модуля — переустанови программу "  # type: ignore[attr-defined]
                                    "с поддержкой Claude")
                raise error from exc
            kwargs: dict[str, Any] = {"timeout": cfg.timeout_seconds, "max_retries": 2}
            if cfg.api_key:
                kwargs["api_key"] = cfg.api_key
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            client = anthropic.AsyncAnthropic(**kwargs)
        self._client = client

    async def chat_json(
        self, system: str, user: str, images: list[bytes] | None = None, schema: dict | None = None
    ) -> str:
        """Same contract as VisionLLM.chat_json. The evaluator passes the shared extraction
        schema (prompts.VERDICT_SCHEMA), which Claude's structured outputs accept as is:
        closed objects, all fields required, nullable via type arrays, no numeric limits."""
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_mime(img),
                    "data": base64.standard_b64encode(img).decode("ascii"),
                },
            }
            for img in (images or [])[: self.cfg.max_images]
        ]
        content.append({"type": "text", "text": user})
        request: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",  # a declined request is retried server-side on another model
        }
        if schema:
            request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        try:
            response = await self._client.beta.messages.create(**request)
        except Exception as exc:
            raise LLMError(f"Claude API: {exc}") from exc
        if response.stop_reason == "refusal":
            raise LLMError("Claude отказался оценивать это объявление")
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        if not text:
            raise LLMError(f"Claude вернул пустой ответ (stop_reason={response.stop_reason})")
        return text

    async def health(self) -> dict[str, Any]:
        base = {"provider": "anthropic", "base_url": self.cfg.base_url or "https://api.anthropic.com",
                "model": self.cfg.model, "models": []}
        try:
            model = await self._client.models.retrieve(self.cfg.model)
        except Exception as exc:
            return {**base, "ok": False, "model_available": False, "error": f"Claude API: {exc}"}
        return {**base, "ok": True, "model_available": True, "models": [model.id], "error": None}

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def make_llm(cfg: Any) -> Any:
    """Build the right client for cfg.provider."""
    if cfg.provider == "anthropic":
        return ClaudeVision(cfg)
    from .client import VisionLLM

    return VisionLLM(cfg)
