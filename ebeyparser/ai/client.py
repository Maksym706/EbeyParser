"""Client for a vision / text LLM: a LOCAL server — Ollama (native API) or any OpenAI-compatible
one (LM Studio, llama.cpp server, vLLM, Ollama's /v1 endpoint, ...) — or a free CLOUD endpoint
(OpenRouter, the NVIDIA API catalog, an OmniRoute gateway; docs/design/CLOUD_AI.md) behind the
quota limiter of ai/cloud.py."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from typing import Any, Protocol

import httpx

from ..config import LLMSettings

log = logging.getLogger(__name__)

# Photos are expensive in tokens: 3 images at ~1000 tokens each overflow Ollama's
# default 2k/4k context, so ask for a bigger window.
OLLAMA_NUM_CTX = 8192
# The JSON answer is ~300-450 tokens; the cap stops a 7B model that starts rambling or
# repeating (the parser salvages a cut-off answer).
MAX_OUTPUT_TOKENS = 700
# Images for the model: long side <= 1024 px (a 7B VL model gains nothing from more,
# each extra pixel costs tokens and time), JPEG q85, and a cap on the total payload.
IMAGE_MAX_SIDE = 1024
IMAGE_JPEG_QUALITY = 85
IMAGE_REENCODE_BYTES = 350_000  # re-compress even small-dimension images above this size
MAX_TOTAL_IMAGE_BYTES = 3_000_000


class LLMError(Exception):
    """Any failure talking to the model; the message is user-facing (Russian)."""


class ChatModel(Protocol):
    """What AIEvaluator needs from a model client (VisionLLM, ClaudeVision, ...)."""

    async def chat_json(
        self, system: str, user: str, images: list[bytes] | None = None, schema: dict | None = None
    ) -> str: ...

    async def health(self) -> dict: ...

    async def aclose(self) -> None: ...


def _sniff_mime(data: bytes) -> str | None:
    """Mime type from magic bytes (jpeg/png/webp/gif), None if not one of those."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def image_mime(data: bytes) -> str:
    """Mime type from magic bytes (jpeg/png/webp/gif); jpeg if unknown."""
    return _sniff_mime(data) or "image/jpeg"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _shrink(data: bytes, max_side: int, quality: int) -> bytes | None:
    """Downscale/re-encode one image with Pillow (optional dependency).
    Returns the new bytes, the input when nothing needs to change, or None when the
    bytes are not an image at all (e.g. an HTML error page)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # Pillow not installed: pass through unchanged
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            too_big = max(img.size) > max_side
            if not too_big and fmt in ("JPEG", "PNG") and len(data) <= IMAGE_REENCODE_BYTES:
                return data
            img.seek(0)  # first frame of a GIF / animated WebP
            out = ImageOps.exif_transpose(img)
            if out.mode not in ("RGB", "L"):
                out = out.convert("RGBA")
                background = Image.new("RGB", out.size, (255, 255, 255))  # transparent -> white
                background.paste(out, mask=out.getchannel("A"))
                out = background
            if too_big:
                out.thumbnail((max_side, max_side), getattr(Image, "Resampling", Image).LANCZOS)
            buf = io.BytesIO()
            out.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    except Exception as exc:  # noqa: BLE001 - broken/truncated file or odd format
        if _sniff_mime(data) is not None:
            log.debug("Pillow could not process an image (%s), sending it unchanged", exc)
            return data
        log.info("Skipping a download that is not an image (%d bytes)", len(data))
        return None
    small = buf.getvalue()
    # WebP/GIF/... always become JPEG (not every local server decodes them)
    return small if too_big or fmt not in ("JPEG", "PNG") or len(small) < len(data) else data


def prepare_images(
    images: list[bytes] | None,
    *,
    max_side: int = IMAGE_MAX_SIDE,
    quality: int = IMAGE_JPEG_QUALITY,
    max_total_bytes: int = MAX_TOTAL_IMAGE_BYTES,
) -> list[bytes]:
    """Images ready for a vision model: long side <= `max_side` px as JPEG (when Pillow is
    installed; otherwise unchanged), non-images dropped, and no more than
    `max_total_bytes` in total (later images are dropped, the first one is always kept).
    Idempotent: prepared images pass through unchanged."""
    out: list[bytes] = []
    total = 0
    for data in images or []:
        if not data:
            continue
        small = _shrink(data, max_side, quality)
        if small is None:
            continue
        if out and total + len(small) > max_total_bytes:
            log.info("Image payload cap %d bytes reached, sending %d image(s)", max_total_bytes, len(out))
            break
        out.append(small)
        total += len(small)
    return out


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


HEALTH_CACHE_SECONDS = 300.0
_HEALTH_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

VISION_HINTS = ("vl", "vision", "llava", "gemma-3", "gemma3", "minicpm-v", "minicpmv", "pixtral",
                "moondream", "internvl", "molmo", "multimodal", "mistral-small-3", "glm-4.1v",
                "glm-4.5v", "kimi-vl", "qwen3-vl", "qwen2.5-omni", "llama-4", "nano-omni", "nemotron-nano-vl",
                # natively multimodal families (docs/design/AI_MODELS.md): photos need the vision projector
                "qwen3.5", "qwen3.6", "qwen3.8", "gemma-4", "gemma4")


def looks_like_vision_model(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in VISION_HINTS) and "embed" not in low


class VisionLLM:
    """chat_json / health / aclose for Ollama, OpenAI-compatible servers and free cloud endpoints.

    `purpose` ("vision" = the photo check, "triage" = the scout, "planner", "second_opinion") picks
    what thinking="auto" means (off for the first three) and, on a cloud endpoint, whose share of
    the daily quota a call uses. `quota`: the endpoint's limiter (default: the shared one of
    ai.cloud.registry() for a cloud endpoint, none for a local server)."""

    def __init__(self, cfg: LLMSettings, transport: httpx.AsyncBaseTransport | None = None,
                 extra_body: dict[str, Any] | None = None, *, purpose: str = "vision", quota: Any = None,
                 force_quota: bool = False):
        from . import cloud as _cloud

        if cfg.provider not in ("ollama", "openai"):
            raise LLMError(
                f"VisionLLM поддерживает только ollama и openai-совместимые серверы, а не «{cfg.provider}»"
            )
        self.cfg = cfg
        self.cloud = _cloud.cloud_kind(cfg)  # "" = a local server
        self.provider = "openai" if self.cloud else cfg.provider
        self.purpose = purpose
        # thinking off/on for this runtime (llama.cpp / LM Studio chat_template_kwargs, Ollama think,
        # OpenRouter reasoning, NVIDIA's Nemotron system line) + explicit extra request fields
        controls = _cloud.thinking_controls(provider=self.provider, cloud=self.cloud, model=cfg.model,
                                            thinking=str(getattr(cfg, "thinking", "auto") or "auto"), purpose=purpose)
        self._extra_body = {**controls.body, **dict(extra_body or {})}
        self._system_prefix = controls.system_prefix
        self._dropped: set[str] = set()  # extra fields the server rejected (never sent again)
        self._format_start = 0  # cloud: the response_format that worked (no quota spent on rejected ones)
        self._resolved_model: str | None = None  # the server's exact id for cfg.model
        self._resolve_tried = False
        self.last_limited: Exception | None = None  # the last CloudLimited of chat_json (the monitor's status)
        base = (cfg.base_url or "").strip().rstrip("/")
        p = _cloud.preset(self.cloud)
        if p is not None and p.base_url and (not base or (self.cloud in ("openrouter", "nvidia")
                                                          and _cloud.detect_cloud(base) != self.cloud)):
            base = p.base_url.rstrip("/")  # "cloud: openrouter" left with the local default address
        if not base:
            base = "http://localhost:11434" if self.provider == "ollama" else "http://localhost:1234"
        if self.provider == "ollama":
            # people paste the OpenAI-style URL for Ollama too
            for suffix in ("/v1", "/api"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
        self.base_url = base
        self._api_key = _cloud.resolve_api_key(cfg)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        if self.cloud == "openrouter":
            headers.update(_cloud.OPENROUTER_HEADERS)
        # optional config knobs (not in LLMSettings yet): ai.max_tokens / ai.image_max_side
        self._max_tokens = int(getattr(cfg, "max_tokens", None) or MAX_OUTPUT_TOKENS)
        if self.cloud:  # free tokens: room for 20 ads per scout call / a reasoning model's JSON
            floor = _cloud.CLOUD_TRIAGE_MAX_TOKENS if purpose == "triage" else _cloud.CLOUD_VISION_MAX_TOKENS
            self._max_tokens = max(self._max_tokens, floor)
        self._image_max_side = int(getattr(cfg, "image_max_side", None) or IMAGE_MAX_SIDE)
        self.quota = quota
        if self.quota is None and self.cloud:
            self.quota = _cloud.registry().get(self.base_url, self._api_key, kind=self.cloud,
                                               rpm=int(getattr(cfg, "rpm", 0) or 0),
                                               daily_limit=int(getattr(cfg, "daily_limit", 0) or 0), label=cfg.model)
        self._force_quota = force_quota  # a user's «Проверить»: counted, never refused by the day plan
        timeout = httpx.Timeout(cfg.timeout_seconds, connect=min(10.0, cfg.timeout_seconds))
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)

    @property
    def service_name(self) -> str:
        from .cloud import preset

        p = preset(self.cloud)
        return p.name if p else ""

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
        if self.cloud == "omniroute":
            return f"OmniRoute не отвечает по адресу {self.base_url} — запущен ли он на этом компьютере?"
        if self.cloud:
            return f"Облако {self.service_name} недоступно — нет интернета или сервис сейчас не отвечает"
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
            if self.cloud:
                raise LLMError(f"Облако {self.service_name} не ответило за {self.cfg.timeout_seconds:g} с — "
                               "бесплатные модели иногда перегружены, попробую позже") from exc
            raise LLMError(
                f"Модель не ответила за {self.cfg.timeout_seconds:g} с — возьми модель поменьше "
                "или увеличь ai.timeout_seconds"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMError(f"{self._unreachable()} ({type(exc).__name__})") from exc

    async def _send(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """One chat request, through the endpoint's quota: waits a few seconds at most, 429s are
        retried once after a short pause, longer limits raise CloudLimited (the caller's fallback)."""
        from .cloud import CloudLimited

        quota = self.quota
        if quota is None:
            return await self._request("POST", url, payload)
        for attempt in range(2):
            await quota.acquire(self.purpose, force=self._force_quota)
            started = time.monotonic()
            try:
                resp = await self._request("POST", url, payload)
            except LLMError as exc:
                quota.note_failure(str(exc))
                raise
            quota.note_headers(resp.headers)
            if resp.status_code == 429:
                wait = quota.note_429(resp.headers, resp.text[:500])
                if attempt == 0 and wait <= quota.max_wait:
                    continue
                blocked = quota.blocked(self.purpose)
                raise blocked or CloudLimited(
                    f"{self.service_name} просит подождать (слишком много запросов) — повторю позже",
                    reason="429", retry_at=quota.wall() + wait, provider=self.cloud)
            if resp.status_code >= 500:
                quota.note_failure(f"HTTP {resp.status_code}")
            elif resp.status_code < 400:
                quota.note_success(int((time.monotonic() - started) * 1000))
            return resp
        raise CloudLimited(f"{self.service_name} просит подождать — повторю позже", reason="429",
                           provider=self.cloud)  # pragma: no cover - the loop always returns or raises

    _REJECT_WORDS = {"chat_template_kwargs": ("chat_template_kwargs", "enable_thinking"),
                     "reasoning": ("reasoning",), "think": ("think",)}

    def _drop_rejected(self, payload: dict[str, Any], text: str) -> bool:
        """A 400/422 about one of our extra fields (thinking switch): drop it for good and retry."""
        low = text.lower()
        for key in list(self._extra_body):
            words = self._REJECT_WORDS.get(key, (key.lower(),))
            if key in payload and key not in self._dropped and any(w in low for w in words):
                self._dropped.add(key)
                payload.pop(key, None)
                log.info("Model server rejected %r, sending without it", key)
                return True
        return False

    def _extra(self) -> dict[str, Any]:
        return {k: v for k, v in self._extra_body.items() if k not in self._dropped}

    def _http_error(self, resp: httpx.Response) -> LLMError:
        from .cloud import redact_key

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
        detail = redact_key(detail[:300], self._api_key)
        if self.cloud:
            name = self.service_name
            if resp.status_code in (401, 403):
                return LLMError(f"Ключ {name} не подходит (HTTP {resp.status_code}) — вставь ключ заново "
                                "в настройках нейросети")
            if resp.status_code == 402:
                return LLMError(f"{name}: на аккаунте нужен положительный баланс (HTTP 402) — пополни его "
                                "или выбери бесплатную модель с «:free»")
            if resp.status_code in (400, 404) and "model" in detail.lower() and any(
                    w in detail.lower() for w in ("not found", "not a valid", "invalid model", "does not exist", "no endpoints")):
                return LLMError(f"Модели «{self.cfg.model}» нет у {name} — выбери другую в настройках нейросети "
                                f"({detail})")
            return LLMError(f"Ошибка {name} (HTTP {resp.status_code}): {detail}")
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
        """Ask for a JSON answer; returns the text content of the reply without <think> blocks."""
        from .cloud import EBAY_LOCAL_RU, CloudLimited, local_only_reason, redact_contacts, strip_think

        self.last_limited = None
        try:
            if self.cloud:
                if local_only_reason():  # an eBay ad: never to a cloud endpoint (eBay API license)
                    raise CloudLimited(EBAY_LOCAL_RU, reason="ebay", provider=self.cloud)
                user = redact_contacts(user)  # phone numbers / e-mails of sellers stay here
            if self._system_prefix:
                system = f"{self._system_prefix}\n{system}"
            images = prepare_images(images, max_side=self._image_max_side)
            if self.provider == "ollama":
                text = await self._chat_ollama(system, user, images, schema)
            else:
                text = await self._chat_openai(system, user, images, schema)
        except CloudLimited as exc:
            self.last_limited = exc
            raise
        return strip_think(text)

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
            "options": {
                "temperature": self.cfg.temperature,
                "num_ctx": OLLAMA_NUM_CTX,
                "num_predict": self._max_tokens,
            },
            **self._extra(),
        }
        resp = await self._request("POST", self._chat_url(), payload)
        if resp.status_code == 400 and self._drop_rejected(payload, resp.text):
            resp = await self._request("POST", self._chat_url(), payload)  # e.g. a model without "think"
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
            "max_tokens": self._max_tokens,
            "stream": False,
            **self._extra(),
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
        """Strictest response_format first; a rejected one steps down (json_schema → json_object →
        none), a rejected extra field (thinking switch) is dropped. On a cloud endpoint the format
        that worked is remembered, so no quota goes to requests the server rejects anyway."""
        url = self._chat_url()
        i = min(self._format_start, len(formats) - 1) if self.cloud else 0
        while True:
            fmt = formats[i]
            if fmt is None:
                payload.pop("response_format", None)
            else:
                payload["response_format"] = fmt
            resp = await self._send(url, payload)
            if resp.status_code in (400, 422):
                low = resp.text.lower()
                format_word = any(word in low for word in ("response_format", "json_schema", "json_object"))
                if not format_word and self._drop_rejected(payload, low):
                    continue
                if format_word and i < len(formats) - 1:
                    log.info("Server rejected response_format %s, trying a simpler one", fmt and fmt["type"])
                    i += 1
                    continue
            if self.cloud and resp.status_code < 400:
                self._format_start = i
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

    async def probe_key(self) -> dict[str, Any] | None:
        """OpenRouter GET /key: the account's limits (is_free_tier → 50 or 1000 requests a day).
        Costs no quota. None for other endpoints; raises LLMError for a bad key / no connection."""
        if self.cloud != "openrouter":
            return None
        from .cloud import parse_key_info

        resp = await self._request("GET", self._openai_url("/key"))
        if resp.status_code >= 400:
            raise self._http_error(resp)
        info = parse_key_info(self._json(resp))
        if self.quota is not None:
            self.quota.set_probe(info)
        return info

    async def health(self) -> dict:
        """Is the server up and is the configured model installed/loaded? A cloud endpoint: is the
        key accepted (OpenRouter /key) and is the model in its list (cached for a few minutes: the
        OpenRouter list is large and the Состояние page asks often)."""
        if self.cloud:
            key = (self.base_url, self._api_key, self.cfg.model)
            cached = _HEALTH_CACHE.get(key)
            if cached is not None and time.monotonic() - cached[0] < HEALTH_CACHE_SECONDS:
                return dict(cached[1])
            info = await self._health()
            _HEALTH_CACHE[key] = (time.monotonic(), dict(info))
            return info
        return await self._health()

    async def _health(self) -> dict:
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
        if self.cloud:
            info["cloud"] = self.cloud
        try:
            if self.cloud == "openrouter":
                info["key_info"] = await self.probe_key()
            resp = await self._request("GET", self._models_url())
            if resp.status_code >= 400:
                raise self._http_error(resp)
            data = self._json(resp)
        except LLMError as exc:
            info["error"] = str(exc)
            if self.cloud and "HTTP 40" in str(exc):
                info["server_ok"] = True  # the service answered: the key or the model is the problem
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
            if self.cloud:
                info["error"] = f"Модели «{self.cfg.model}» нет у {self.service_name} — выбери другую в настройках нейросети"
            elif self.provider == "ollama":
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
