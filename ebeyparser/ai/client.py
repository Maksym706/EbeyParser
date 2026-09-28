"""Client for a LOCAL vision LLM: Ollama (native API) or any OpenAI-compatible server
(LM Studio, llama.cpp server, vLLM, Ollama's /v1 endpoint, ...)."""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Protocol

import httpx

from ..config import LLMSettings

log = logging.getLogger(__name__)

# Photos are expensive in tokens: 3 images at ~1000 tokens each overflow Ollama's
# default 2k/4k context, so ask for a bigger window.
OLLAMA_NUM_CTX = 8192


class LLMError(Exception):
    """Any failure talking to the model; the message is user-facing (Russian)."""


class ChatModel(Protocol):
    """What AIEvaluator needs from a model client (VisionLLM, ClaudeVision, ...)."""

    async def chat_json(
        self, system: str, user: str, images: list[bytes] | None = None, schema: dict | None = None
    ) -> str: ...

    async def health(self) -> dict: ...

    async def aclose(self) -> None: ...


def image_mime(data: bytes) -> str:
    """Mime type from magic bytes (jpeg/png/webp/gif); jpeg if unknown."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _model_names_match(wanted: str, available: str) -> bool:
    w, a = wanted.strip().lower(), available.strip().lower()
    if not w or not a:
        return False
    if w == a or w.removesuffix(":latest") == a.removesuffix(":latest"):
        return True
    # llama.cpp / LM Studio report file paths or "publisher/model" ids
    tail = a.rsplit("/", 1)[-1]
    return w in (tail, tail.removesuffix(".gguf"))


_NOISE_TOKENS = re.compile(
    r"(?:^|[-_.:\s])(?:instruct|it|chat|gguf|mlx|latest|hf|q\d[\w]*|\d+bit|fp16|bf16|f16)(?=$|[-_.:\s])"
)


def canonical_model_name(name: str) -> str:
    """'qwen/Qwen2.5-VL-7B-Instruct-GGUF' and 'qwen2.5vl:7b' both -> 'qwen25vl7b'."""
    tail = name.strip().lower().replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".gguf")
    previous = None
    while previous != tail:  # tokens can be adjacent: "-instruct-q4_k_m"
        previous, tail = tail, _NOISE_TOKENS.sub("", tail)
    return re.sub(r"[^a-z0-9]", "", tail)


def find_model(wanted: str, available: list[str]) -> str | None:
    """The server's own id for the configured model: exact, then publisher/path, then fuzzy."""
    for name in available:
        if _model_names_match(wanted, name):
            return name
    key = canonical_model_name(wanted)
    if key:
        for name in available:
            if canonical_model_name(name) == key:
                return name
    return None


def _model_missing(body: str) -> bool:
    low = body.lower()
    return "model" in low and any(
        w in low for w in ("not found", "not loaded", "no model", "does not exist", "invalid model",
                           "unknown model", "failed to load")
    )


VISION_HINTS = ("vl", "vision", "llava", "gemma-3", "gemma3", "minicpm-v", "minicpmv", "pixtral",
                "moondream", "internvl", "molmo", "multimodal", "mistral-small-3", "glm-4.1v",
                "glm-4.5v", "kimi-vl", "qwen3-vl", "qwen2.5-omni", "llama-4")


def looks_like_vision_model(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in VISION_HINTS) and "embed" not in low


class VisionLLM:
    """chat_json / health / aclose for Ollama and OpenAI-compatible servers."""

    def __init__(self, cfg: LLMSettings, transport: httpx.AsyncBaseTransport | None = None):
        if cfg.provider not in ("ollama", "openai"):
            raise LLMError(
                f"VisionLLM поддерживает только ollama и openai-совместимые серверы, а не «{cfg.provider}»"
            )
        self.cfg = cfg
        self.provider = cfg.provider
        self._resolved_model: str | None = None  # the server's exact id for cfg.model
        self._resolve_tried = False
        base = (cfg.base_url or "").strip().rstrip("/")
        if not base:
            base = "http://localhost:11434" if cfg.provider == "ollama" else "http://localhost:1234"
        if cfg.provider == "ollama":
            # people paste the OpenAI-style URL for Ollama too
            for suffix in ("/v1", "/api"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
        self.base_url = base
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        timeout = httpx.Timeout(cfg.timeout_seconds, connect=min(10.0, cfg.timeout_seconds))
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)

    # -- urls -----------------------------------------------------------------------

    def _openai_url(self, path: str) -> str:
        base = self.base_url
        return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

    def _chat_url(self) -> str:
        if self.provider == "ollama":
            return f"{self.base_url}/api/chat"
        return self._openai_url("/chat/completions")

    def _models_url(self) -> str:
        if self.provider == "ollama":
            return f"{self.base_url}/api/tags"
        return self._openai_url("/models")

    # -- http -----------------------------------------------------------------------

    def _unreachable(self) -> str:
        if self.provider == "ollama":
            return (
                f"Локальная модель недоступна по адресу {self.base_url} — запущена ли Ollama? "
                "(команда: ollama serve)"
            )
        return (
            f"Локальная модель недоступна по адресу {self.base_url} — запущен ли сервер "
            "(LM Studio / llama.cpp / vLLM)?"
        )

    async def _request(self, method: str, url: str, payload: dict | None = None) -> httpx.Response:
        try:
            return await self._client.request(method, url, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Модель не ответила за {self.cfg.timeout_seconds:g} с — возьми модель поменьше "
                "или увеличь ai.timeout_seconds"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMError(f"{self._unreachable()} ({type(exc).__name__})") from exc

    def _http_error(self, resp: httpx.Response) -> LLMError:
        detail = resp.text.strip()
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    err = err.get("message")
                detail = str(err or detail)
        except ValueError:
            pass
        detail = detail[:300]
        if resp.status_code == 404 and "model" in detail.lower():
            hint = f" — выполни: ollama pull {self.cfg.model}" if self.provider == "ollama" else ""
            return LLMError(f"Модель «{self.cfg.model}» не найдена{hint} ({detail})")
        if resp.status_code in (401, 403):
            return LLMError(f"Сервер модели отказал в доступе (HTTP {resp.status_code}) — проверь api_key")
        return LLMError(f"Ошибка сервера модели (HTTP {resp.status_code}): {detail}")

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(f"Сервер модели вернул не JSON: {resp.text[:200]}") from exc

    # -- public API -----------------------------------------------------------------

    async def chat_json(
        self, system: str, user: str, images: list[bytes] | None = None, schema: dict | None = None
    ) -> str:
        """Ask for a JSON answer; returns the raw text content of the reply."""
        images = [img for img in (images or []) if img]
        if self.provider == "ollama":
            return await self._chat_ollama(system, user, images, schema)
        return await self._chat_openai(system, user, images, schema)

    async def _chat_ollama(
        self, system: str, user: str, images: list[bytes], schema: dict | None
    ) -> str:
        user_msg: dict[str, Any] = {"role": "user", "content": user}
        if images:
            user_msg["images"] = [_b64(img) for img in images]
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": system}, user_msg],
            "stream": False,
            "format": schema or "json",
            "options": {"temperature": self.cfg.temperature, "num_ctx": OLLAMA_NUM_CTX},
        }
        resp = await self._request("POST", self._chat_url(), payload)
        if resp.status_code == 400 and schema and "format" in resp.text.lower():
            # old Ollama without structured outputs: plain JSON mode
            log.info("Ollama rejected the JSON schema, retrying with format=json")
            payload["format"] = "json"
            resp = await self._request("POST", self._chat_url(), payload)
        if resp.status_code >= 400:
            raise self._http_error(resp)
        data = self._json(resp)
        try:
            content = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ Ollama: {json.dumps(data)[:200]}") from exc
        return content if isinstance(content, str) else json.dumps(content)

    async def _chat_openai(
        self, system: str, user: str, images: list[bytes], schema: dict | None = None
    ) -> str:
        if images:
            content: Any = [{"type": "text", "text": user}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image_mime(img)};base64,{_b64(img)}"},
                }
                for img in images
            ]
        else:
            content = user  # plain string: some servers reject list content without images
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        # Strictest first: json_schema (LM Studio, llama.cpp, vLLM constrain the output to
        # the schema), then json_object, then plain text (the prompt demands JSON anyway).
        formats: list[dict[str, Any] | None] = [{"type": "json_object"}, None]
        if schema:
            formats.insert(0, {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "strict": True, "schema": schema},
            })
        resp = await self._post_openai(payload, formats)
        if resp.status_code in (400, 404, 422) and _model_missing(resp.text) and not self._resolve_tried:
            # e.g. config says "qwen2.5-vl-7b-instruct", LM Studio calls it "qwen/qwen2.5-vl-7b"
            await self._resolve_model()
            if self._resolved_model and self._resolved_model != payload["model"]:
                log.info("Using server model id %r for %r", self._resolved_model, self.cfg.model)
                payload["model"] = self._resolved_model
                resp = await self._post_openai(payload, formats)
        if resp.status_code >= 400:
            raise self._http_error(resp)
        data = self._json(resp)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ сервера: {json.dumps(data)[:200]}") from exc
        content_out = message.get("content")
        if isinstance(content_out, list):  # some servers return content parts
            content_out = "".join(
                p.get("text", "") for p in content_out if isinstance(p, dict)
            )
        return content_out or ""

    async def _post_openai(
        self, payload: dict[str, Any], formats: list[dict[str, Any] | None]
    ) -> httpx.Response:
        url = self._chat_url()
        for i, fmt in enumerate(formats):
            if fmt is None:
                payload.pop("response_format", None)
            else:
                payload["response_format"] = fmt
            resp = await self._request("POST", url, payload)
            rejected = resp.status_code in (400, 422) and any(
                word in resp.text.lower() for word in ("response_format", "json_schema", "json_object")
            )
            if not rejected or i == len(formats) - 1:
                return resp
            log.info("Server rejected response_format %s, trying a simpler one", fmt and fmt["type"])
        return resp

    @property
    def model_name(self) -> str:
        """What we send as "model": the server's exact id when we found it, else the config value."""
        return self._resolved_model or self.cfg.model

    async def _resolve_model(self) -> None:
        """Once per client: map e.g. 'qwen2.5-vl-7b-instruct' to LM Studio's 'qwen/qwen2.5-vl-7b'."""
        if self._resolve_tried:
            return
        self._resolve_tried = True
        try:
            await self.health()
        except Exception as exc:  # never block a chat on this
            log.debug("Model name resolution failed: %s", exc)

    async def health(self) -> dict:
        """Is the server up and is the configured model installed/loaded?"""
        info: dict[str, Any] = {
            "ok": False,
            "server_ok": False,
            "resolved_model": None,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.cfg.model,
            "model_available": False,
            "models": [],
            "error": None,
        }
        try:
            resp = await self._request("GET", self._models_url())
            if resp.status_code >= 400:
                raise self._http_error(resp)
            data = self._json(resp)
        except LLMError as exc:
            info["error"] = str(exc)
            return info
        info["server_ok"] = True
        key, field = ("models", "name") if self.provider == "ollama" else ("data", "id")
        items = data.get(key, []) if isinstance(data, dict) else []
        names = [str(m.get(field) or m.get("model") or "") for m in items if isinstance(m, dict)]
        info["models"] = [n for n in names if n]
        match = find_model(self.cfg.model, info["models"])
        if match is not None:
            self._resolved_model = match
            info["resolved_model"] = match
        info["model_available"] = match is not None
        info["ok"] = info["model_available"]
        if not info["model_available"]:
            if self.provider == "ollama":
                info["error"] = f"Модель «{self.cfg.model}» не установлена — выполни: ollama pull {self.cfg.model}"
            else:
                info["error"] = f"Модель «{self.cfg.model}» не загружена на сервере"
        return info

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> VisionLLM:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
