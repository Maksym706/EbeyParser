"""Find local model servers (LM Studio first, then Ollama) and their vision models.

Used by the setup wizard and the /settings page. LM Studio is the expected setup
(http://localhost:1234/v1 with qwen/qwen2.5-vl-7b); Ollama is the alternative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

LMSTUDIO_URL = "http://localhost:1234/v1"
OLLAMA_URL = "http://localhost:11434"
DEFAULT_LMSTUDIO_MODEL = "qwen/qwen2.5-vl-7b"
DEFAULT_OLLAMA_MODEL = "qwen2.5vl:7b"
PREFERRED_MODEL_HINTS = ("qwen2.5-vl-7b", "qwen2.5vl:7b", "qwen2.5-vl", "qwen2.5vl")


@dataclass
class LocalAIServer:
    name: str  # "LM Studio" / "Ollama"
    provider: str  # config ai.provider: "openai" (OpenAI-compatible) or "ollama"
    base_url: str
    models: list[str] = field(default_factory=list)

    @property
    def vision_models(self) -> list[str]:
        """Models that can look at photos, preferred Qwen2.5-VL-7B first."""
        from ..ai.client import looks_like_vision_model

        vision = [m for m in self.models if looks_like_vision_model(m)]
        preferred = [m for hint in PREFERRED_MODEL_HINTS for m in vision if hint in m.lower()]
        return list(dict.fromkeys(preferred + vision))

    @property
    def default_model(self) -> str:
        vision = self.vision_models
        if vision:
            return vision[0]
        return DEFAULT_LMSTUDIO_MODEL if self.provider == "openai" else DEFAULT_OLLAMA_MODEL

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider, "base_url": self.base_url,
                "models": list(self.models), "vision_models": self.vision_models,
                "default_model": self.default_model}


def probe_json(url: str, timeout: float = 2.0) -> Any:
    """GET a local JSON endpoint; None on any problem. Ignores proxy settings (localhost)."""
    import httpx

    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(url)
        return response.json() if response.status_code == 200 else None
    except Exception:
        return None


def detect_local_ai(probe: Callable[[str], Any] = probe_json) -> list[LocalAIServer]:
    """LM Studio (:1234, OpenAI-compatible) and Ollama (:11434) that answer right now."""
    found: list[LocalAIServer] = []
    data = probe(f"{LMSTUDIO_URL}/models")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        models = [str(m["id"]) for m in data["data"] if isinstance(m, dict) and m.get("id")]
        found.append(LocalAIServer("LM Studio", "openai", LMSTUDIO_URL, models))
    data = probe(f"{OLLAMA_URL}/api/tags")
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        models = [str(m.get("name") or m.get("model")) for m in data["models"]
                  if isinstance(m, dict) and (m.get("name") or m.get("model"))]
        found.append(LocalAIServer("Ollama", "ollama", OLLAMA_URL, models))
    return found


__all__ = [
    "DEFAULT_LMSTUDIO_MODEL",
    "DEFAULT_OLLAMA_MODEL",
    "LMSTUDIO_URL",
    "OLLAMA_URL",
    "LocalAIServer",
    "detect_local_ai",
    "probe_json",
]
