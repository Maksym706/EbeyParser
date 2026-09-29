"""Recommended local AI models per hardware tier and task — pure data, no side effects.

Used by onboarding/settings ("Выбери своё железо → рекомендую модель") and to auto-pick a
model from what /ai/detect finds. Research, sources and the CPU benchmark behind every
number: docs/design/AI_MODELS.md (verified 2026-09-29).

Tiers:
    T0  CPU only, 2-4 cores, 4-8 GB RAM (N100 mini-PC, old laptop, Raspberry Pi 5 8 GB)
    T1  CPU only, ~8 cores, 16-32 GB RAM
    T2  GPU with 8-12 GB VRAM (RTX 3060 12 GB / 4060 8 GB)
    T3  GPU with 24 GB VRAM (RTX 3090 / 4090)
The usual home setup is split: a T0/T1 server runs the monitor, triage and embeddings 24/7,
while a T2/T3 gaming PC (LM Studio / Ollama over LAN or Tailscale, sometimes off) does the
photo check and the second opinion. `recommend()` answers for ONE machine; `recommend_split()`
combines an always-on server with an optional PC.

Seconds per ad are wall-clock for batched triage (5-10 ads per call, ~50 output tokens per ad,
compact prompt, strict JSON schema). Output tokens dominate on CPU, so they scale with generation
speed. Models under 2B are not listed for triage: in our benchmark Qwen3.5 0.8B renumbered ads and
copied their text (20 % kind accuracy); it only appears as a photo reader (OCR is fine).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TASKS = ("triage", "vision", "embed", "rerank", "translate", "second_opinion")
TIERS = ("T0", "T1", "T2", "T3")

TIER_LABELS_RU = {
    "T0": "Слабый сервер без видеокарты (2–4 ядра, 4–8 ГБ)",
    "T1": "Сервер без видеокарты (8 ядер, 16–32 ГБ)",
    "T2": "Видеокарта 8–12 ГБ (RTX 3060/4060)",
    "T3": "Видеокарта 24 ГБ (RTX 3090/4090)",
}


@dataclass(frozen=True)
class ModelChoice:
    key: str  # stable catalog id
    task: str  # one of TASKS
    name: str  # human name, e.g. "Qwen3.5 2B"
    params_b: float  # total parameters, billions
    active_b: float | None  # active parameters for MoE, None = dense
    quant: str  # recommended quantisation
    ollama: str | None  # `ollama pull <this>`
    lmstudio: str | None  # LM Studio catalog id or HF repo (`lms get <this>`), pick the quant in the app
    llamacpp: str | None  # `llama-server -hf <this>` (Hugging Face repo:quant)
    file_gb: float  # model file(s) at `quant`, incl. the vision projector when vision=True
    min_ram_gb: float  # RAM of the model process on CPU: weights + 8k-token KV cache + buffers
    min_vram_gb: float  # 0 = CPU is fine; else VRAM needed to keep it fully on the GPU
    vision: bool
    arm_ok: bool  # runs on ARM64 (Raspberry Pi 5, Apple Silicon) with llama.cpp/Ollama
    context_k: int  # native context, thousands of tokens
    license: str
    sec_per_ad: dict[str, float] = field(default_factory=dict)  # tier -> expected seconds (triage)
    label_ru: str = ""
    desc_ru: str = ""

    def ads_per_hour(self, tier: str) -> int | None:
        sec = self.sec_per_ad.get(tier)
        return int(3600 / sec) if sec else None

    def as_dict(self, tier: str | None = None) -> dict[str, Any]:
        data = asdict(self)
        if tier is not None:
            data["tier"] = tier
            data["expected_sec_per_ad"] = self.sec_per_ad.get(tier)
            data["expected_ads_per_hour"] = self.ads_per_hour(tier)
        return data


# ---------------------------------------------------------------------------------------------
# Catalog. Speeds: T0 measured on a shared 4-vCPU VM (~N100 class, see AI_MODELS.md), others
# extrapolated from memory bandwidth / published GPU numbers.
# ---------------------------------------------------------------------------------------------

MODELS: dict[str, ModelChoice] = {m.key: m for m in (
    # --- triage (text; the Qwen3.5 small models are also natively multimodal) ---
    ModelChoice(
        key="qwen3.5-2b", task="triage", name="Qwen3.5 2B", params_b=1.9, active_b=None,
        quant="Q4_K_M", ollama="qwen3.5:2b-q4_K_M", lmstudio="qwen/qwen3.5-2b",
        llamacpp="unsloth/Qwen3.5-2B-GGUF:Q4_K_M", file_gb=1.3, min_ram_gb=3.1, min_vram_gb=0,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        sec_per_ad={"T0": 6.0, "T1": 2.5, "T2": 0.5, "T3": 0.4},
        label_ru="Для слабого сервера",
        desc_ru="Для слабого сервера без видеокарты: читает каждое объявление (~6 с), но путает варианты — цены и проверки делает код.",
    ),
    ModelChoice(
        key="qwen3.5-4b", task="triage", name="Qwen3.5 4B", params_b=4.0, active_b=None,
        quant="Q4_K_M", ollama="qwen3.5:4b-q4_K_M", lmstudio="qwen/qwen3.5-4b",
        llamacpp="unsloth/Qwen3.5-4B-GGUF:Q4_K_M", file_gb=2.7, min_ram_gb=4.8, min_vram_gb=0,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        sec_per_ad={"T0": 15.5, "T1": 6.0, "T2": 0.8, "T3": 0.5},
        label_ru="Сбалансированная",
        desc_ru="Для сервера без видеокарты с 8+ ядрами: почти без ошибок в типе и мошенничестве, но в 2,5 раза медленнее 2B.",
    ),
    ModelChoice(
        key="qwen3.5-9b", task="triage", name="Qwen3.5 9B", params_b=9.0, active_b=None,
        quant="Q4_K_M", ollama="qwen3.5:9b-q4_K_M", lmstudio="qwen/qwen3.5-9b",
        llamacpp="unsloth/Qwen3.5-9B-GGUF:Q4_K_M", file_gb=6.6, min_ram_gb=8.5, min_vram_gb=8,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        sec_per_ad={"T1": 15.0, "T2": 1.5, "T3": 0.8},
        label_ru="Для видеокарты 8–12 ГБ",
        desc_ru="Для видеокарты 8–12 ГБ: одна модель и для текста, и для фото, лучшая в своём размере.",
    ),
    ModelChoice(
        key="qwen3.6-35b-a3b", task="triage", name="Qwen3.6 35B-A3B (MoE)", params_b=35.0, active_b=3.0,
        quant="UD-Q4_K_M", ollama="qwen3.6:35b-a3b-q4_K_M", lmstudio="qwen/qwen3.6-35b-a3b",
        llamacpp="unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M", file_gb=22.0, min_ram_gb=24, min_vram_gb=24,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        sec_per_ad={"T3": 0.5},
        label_ru="Быстрая большая (MoE)",
        desc_ru="Для видеокарты 24 ГБ: 35B знаний при скорости 3B, текст и фото, >100 ток/с на RTX 3090.",
    ),
    # --- vision (photo check of top candidates) ---
    ModelChoice(
        key="qwen3.5-2b-vision", task="vision", name="Qwen3.5 2B + mmproj", params_b=2.3, active_b=None,
        quant="Q4_K_M + F16 mmproj", ollama="qwen3.5:2b-q4_K_M", lmstudio="qwen/qwen3.5-2b",
        llamacpp="unsloth/Qwen3.5-2B-GGUF:Q4_K_M", file_gb=1.9, min_ram_gb=4.0, min_vram_gb=0,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Фото на процессоре",
        desc_ru="Проверка фото без видеокарты: читает экран блокировки, наклейки и серийники, ~40 с на фото.",
    ),
    ModelChoice(
        key="qwen3.5-0.8b-vision", task="vision", name="Qwen3.5 0.8B + mmproj", params_b=1.0, active_b=None,
        quant="Q4_K_M + F16 mmproj", ollama="qwen3.5:0.8b-q4_K_M", lmstudio="qwen/qwen3.5-0.8b",
        llamacpp="unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M", file_gb=0.75, min_ram_gb=2.0, min_vram_gb=0,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Фото, минимальная",
        desc_ru="Для 4–5 ГБ RAM: хорошо читает текст на фото (блокировка, наклейки), но о дефектах судит слабо.",
    ),
    ModelChoice(
        key="qwen3.5-4b-vision", task="vision", name="Qwen3.5 4B + mmproj", params_b=4.6, active_b=None,
        quant="Q4_K_M + F16 mmproj", ollama="qwen3.5:4b-q4_K_M", lmstudio="qwen/qwen3.5-4b",
        llamacpp="unsloth/Qwen3.5-4B-GGUF:Q4_K_M", file_gb=3.4, min_ram_gb=5.5, min_vram_gb=6,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Фото, баланс",
        desc_ru="Проверка фото на сервере с 16+ ГБ или на видеокарте 6 ГБ.",
    ),
    ModelChoice(
        key="qwen3.5-9b-vision", task="vision", name="Qwen3.5 9B", params_b=9.9, active_b=None,
        quant="Q4_K_M + F16 mmproj", ollama="qwen3.5:9b-q4_K_M", lmstudio="qwen/qwen3.5-9b",
        llamacpp="unsloth/Qwen3.5-9B-GGUF:Q4_K_M", file_gb=6.6, min_ram_gb=9.5, min_vram_gb=8,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Фото на видеокарте",
        desc_ru="Для видеокарты 8–12 ГБ: дефекты, iCloud-блокировка, стоковые фото — за секунды.",
    ),
    ModelChoice(
        key="qwen3.6-35b-a3b-vision", task="vision", name="Qwen3.6 35B-A3B (MoE)", params_b=35.0, active_b=3.0,
        quant="UD-Q4_K_M + mmproj", ollama="qwen3.6:35b-a3b-q4_K_M", lmstudio="qwen/qwen3.6-35b-a3b",
        llamacpp="unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M", file_gb=23.0, min_ram_gb=25, min_vram_gb=24,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Фото, максимум",
        desc_ru="Для видеокарты 24 ГБ: та же модель, что читает объявления, — без перезагрузки, секунды на фото.",
    ),
    # --- embeddings (must stay on the always-on server: vectors are only comparable within one model) ---
    ModelChoice(
        key="qwen3-embedding-0.6b", task="embed", name="Qwen3-Embedding 0.6B", params_b=0.6, active_b=None,
        quant="Q8_0", ollama="qwen3-embedding:0.6b", lmstudio="Qwen/Qwen3-Embedding-0.6B-GGUF",
        llamacpp="Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0", file_gb=0.64, min_ram_gb=1.0, min_vram_gb=0,
        vision=False, arm_ok=True, context_k=32, license="Apache-2.0",
        label_ru="Поиск похожих",
        desc_ru="Группирует одинаковые товары и ищет «найди мне X» на немецком, русском и английском.",
    ),
    ModelChoice(
        key="embeddinggemma-300m", task="embed", name="EmbeddingGemma 300M", params_b=0.3, active_b=None,
        quant="Q8_0", ollama="embeddinggemma", lmstudio="ggml-org/embeddinggemma-300M-GGUF",
        llamacpp="ggml-org/embeddinggemma-300M-GGUF:Q8_0", file_gb=0.33, min_ram_gb=0.5, min_vram_gb=0,
        vision=False, arm_ok=True, context_k=2, license="Gemma Terms of Use",
        label_ru="Поиск похожих, лёгкая",
        desc_ru="Вдвое легче и быстрее, чуть хуже качество — для 4 ГБ RAM или Raspberry Pi.",
    ),
    # --- reranker (optional, for "find me X" and comparable offers) ---
    ModelChoice(
        key="qwen3-reranker-0.6b", task="rerank", name="Qwen3-Reranker 0.6B", params_b=0.6, active_b=None,
        quant="Q8_0", ollama=None, lmstudio=None,
        llamacpp="ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", file_gb=0.64, min_ram_gb=1.0, min_vram_gb=0,
        vision=False, arm_ok=True, context_k=32, license="Apache-2.0",
        label_ru="Уточнение поиска",
        desc_ru="Переупорядочивает найденные объявления по смыслу; нужен llama-server с --reranking.",
    ),
    # --- translation DE -> RU (on demand, one description at a time) ---
    ModelChoice(
        key="translategemma-4b", task="translate", name="TranslateGemma 4B", params_b=4.0, active_b=None,
        quant="Q4_K_M", ollama="translategemma:4b", lmstudio=None,
        llamacpp=None, file_gb=3.3, min_ram_gb=4.5, min_vram_gb=4,
        vision=False, arm_ok=True, context_k=2, license="Gemma Terms of Use",
        label_ru="Перевод DE→RU",
        desc_ru="Переводит описание объявления на русский точнее универсальной модели того же размера.",
    ),
    # --- second opinion for the top 1-2 deals a day (thinking mode on, slow is fine) ---
    ModelChoice(
        key="qwen3.5-9b-think", task="second_opinion", name="Qwen3.5 9B (thinking)", params_b=9.0,
        active_b=None, quant="Q4_K_M", ollama="qwen3.5:9b-q4_K_M", lmstudio="qwen/qwen3.5-9b",
        llamacpp="unsloth/Qwen3.5-9B-GGUF:Q4_K_M", file_gb=6.6, min_ram_gb=9.5, min_vram_gb=8,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Второе мнение",
        desc_ru="Перепроверяет лучшие находки дня с рассуждением; на процессоре 16 ГБ — пару минут на сделку.",
    ),
    ModelChoice(
        key="qwen3.8-27b", task="second_opinion", name="Qwen3.8 27B", params_b=27.0, active_b=None,
        quant="Q4_K_M", ollama="qwen3.8:27b", lmstudio="qwen/qwen3.8-27b",
        llamacpp="unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M", file_gb=17.4, min_ram_gb=20, min_vram_gb=20,
        vision=True, arm_ok=True, context_k=262, license="Apache-2.0",
        label_ru="Второе мнение, максимум",
        desc_ru="Для видеокарты 24 ГБ: самая умная открытая модель такого размера (сентябрь 2026).",
    ),
)}

# Picks per tier: task -> catalog key (None = run it elsewhere: the PC or the cloud second opinion).
PICKS: dict[str, dict[str, str | None]] = {
    "T0": {"triage": "qwen3.5-2b", "vision": "qwen3.5-2b-vision", "embed": "qwen3-embedding-0.6b",
           "rerank": None, "translate": None, "second_opinion": None},
    "T1": {"triage": "qwen3.5-4b", "vision": "qwen3.5-4b-vision", "embed": "qwen3-embedding-0.6b",
           "rerank": "qwen3-reranker-0.6b", "translate": "translategemma-4b",
           "second_opinion": "qwen3.5-9b-think"},
    "T2": {"triage": "qwen3.5-9b", "vision": "qwen3.5-9b-vision", "embed": "qwen3-embedding-0.6b",
           "rerank": "qwen3-reranker-0.6b", "translate": "translategemma-4b",
           "second_opinion": "qwen3.5-9b-think"},
    "T3": {"triage": "qwen3.6-35b-a3b", "vision": "qwen3.6-35b-a3b-vision", "embed": "qwen3-embedding-0.6b",
           "rerank": "qwen3-reranker-0.6b", "translate": "translategemma-4b",
           "second_opinion": "qwen3.8-27b"},
}

# The runner-up per task when the pick does not fit (smaller RAM, ARM board, missing runtime).
FALLBACKS: dict[str, str] = {
    "qwen3.5-4b": "qwen3.5-2b",
    "qwen3.5-9b": "qwen3.5-4b",
    "qwen3.6-35b-a3b": "qwen3.5-9b",
    "qwen3.5-4b-vision": "qwen3.5-2b-vision",
    "qwen3.5-2b-vision": "qwen3.5-0.8b-vision",
    "qwen3.5-9b-vision": "qwen3.5-4b-vision",
    "qwen3.6-35b-a3b-vision": "qwen3.5-9b-vision",
    "qwen3-embedding-0.6b": "embeddinggemma-300m",
    "qwen3.8-27b": "qwen3.5-9b-think",
}

# RAM the app itself (monitor, SQLite, web UI) and a minimal Linux need next to the models.
APP_RAM_GB = 1.0


def tier_for(ram_gb: float, vram_gb: float = 0.0) -> str:
    """Hardware tier of ONE machine: GPU memory decides first, then system RAM."""
    if vram_gb >= 20:
        return "T3"
    if vram_gb >= 6:
        return "T2"
    if ram_gb >= 16:
        return "T1"
    return "T0"


def _fits(model: ModelChoice, tier: str, ram_gb: float, vram_gb: float, arm: bool, used_gb: float) -> bool:
    if arm and not model.arm_ok:
        return False
    if tier in ("T2", "T3") and model.min_vram_gb:
        return model.min_vram_gb <= vram_gb
    return model.min_ram_gb + used_gb + APP_RAM_GB <= ram_gb


def _chain(key: str | None) -> list[ModelChoice]:
    """The pick followed by its fallbacks, best first."""
    out: list[ModelChoice] = []
    while key and key not in {m.key for m in out}:
        out.append(MODELS[key])
        key = FALLBACKS.get(key)
    return out


def _pick(key: str | None, tier: str, ram_gb: float, vram_gb: float, arm: bool, used_gb: float) -> ModelChoice | None:
    return next((m for m in _chain(key) if _fits(m, tier, ram_gb, vram_gb, arm, used_gb)), None)


def _server_pair(tier: str, ram_gb: float, vram_gb: float, arm: bool) -> tuple[ModelChoice | None, ModelChoice | None]:
    """Triage + embedding model that stay loaded together 24/7. On CPU tiers both must fit in
    RAM at once: the best triage model wins, the embedding model steps down first."""
    triage_chain = _chain(PICKS[tier]["triage"])
    embed_chain = [m for m in _chain(PICKS[tier]["embed"]) if not (arm and not m.arm_ok)]
    on_gpu = tier in ("T2", "T3")
    for triage in triage_chain:
        if not _fits(triage, tier, ram_gb, vram_gb, arm, 0.0):
            continue
        used = 0.0 if on_gpu else triage.min_ram_gb
        embed = next((e for e in embed_chain if e.min_ram_gb + used + APP_RAM_GB <= ram_gb), None)
        if embed is not None:
            return triage, embed
    embed = next((e for e in embed_chain if e.min_ram_gb + APP_RAM_GB <= ram_gb), None)
    return None, embed


def recommend(ram_gb: float, vram_gb: float = 0.0, arm: bool = False) -> dict[str, Any]:
    """Models for ONE machine. Returns {"tier", "tier_label_ru", "notes_ru", <task>: model dict|None}.

    Each model dict is ModelChoice.as_dict(tier) (ids per runtime, quant, RAM/VRAM, expected
    seconds per ad and ads per hour for triage). None = do this task elsewhere (on the PC or with
    the cloud second opinion). On CPU tiers the triage and the embedding model must fit in RAM
    together; the vision model is loaded on demand (Ollama/LM Studio unload the other one)."""
    ram_gb = max(float(ram_gb or 0), 0.0)
    vram_gb = max(float(vram_gb or 0), 0.0)
    tier = tier_for(ram_gb, vram_gb)
    picks = PICKS[tier]
    out: dict[str, Any] = {"tier": tier, "tier_label_ru": TIER_LABELS_RU[tier], "notes_ru": []}
    triage, embed = _server_pair(tier, ram_gb, vram_gb, arm)
    # other tasks are loaded on demand next to the embedding model (the runtime unloads triage)
    used = embed.min_ram_gb if embed and tier in ("T0", "T1") else 0.0
    for task in TASKS:
        if task == "triage":
            model = triage
        elif task == "embed":
            model = embed
        else:
            model = _pick(picks[task], tier, ram_gb, vram_gb, arm, used)
        out[task] = model.as_dict(tier) if model else None
    notes = out["notes_ru"]
    if tier == "T0":
        notes.append("Без видеокарты фото проверяем только у лучших кандидатов (≈1 мин на фото); "
                     "если есть игровой ПК — отдай фото ему.")
        notes.append("Второе мнение — на ПК с видеокартой или через Claude (платно).")
    if arm:
        notes.append("ARM (Raspberry Pi 5): ставь Ollama или llama.cpp; скорость примерно как у N100, "
                     "держи не больше одной большой модели в памяти.")
    if out["triage"] is None:
        notes.append("Мало памяти даже для самой маленькой модели: включи AI-скаута на другом компьютере.")
    return out


def recommend_split(server_ram_gb: float, pc_vram_gb: float, server_arm: bool = False,
                    pc_ram_gb: float = 32.0) -> dict[str, Any]:
    """Always-on server (triage, embeddings) + optional gaming PC (vision, second opinion,
    translation). When the PC is off, vision falls back to the server's pick (if any)."""
    server = recommend(server_ram_gb, 0.0, server_arm)
    pc = recommend(pc_ram_gb, pc_vram_gb, False)
    out = {
        "tier": f"{server['tier']}+{pc['tier']}",
        "tier_label_ru": f"Сервер: {server['tier_label_ru']}; ПК: {pc['tier_label_ru']}",
        "notes_ru": ["Сервер читает все объявления 24/7; фото и второе мнение — на ПК, когда он включён."],
        "triage": server["triage"],
        "embed": server["embed"],
        "rerank": server["rerank"] or pc["rerank"],
        "vision": pc["vision"] or server["vision"],
        "vision_fallback": server["vision"],
        "translate": pc["translate"] or server["translate"],
        "second_opinion": pc["second_opinion"],
    }
    return out


def _canon(text: str) -> str:
    text = text.lower().rsplit("/", 1)[-1].removeprefix("text-embedding-")  # LM Studio embedding ids
    for junk in (".gguf", "-gguf", "-instruct", "-it"):
        text = text.replace(junk, "")
    return "".join(ch for ch in text if ch.isalnum())


def find_catalog_model(available_id: str, task: str | None = None) -> ModelChoice | None:
    """Catalog entry for a model id reported by LM Studio / Ollama / llama.cpp, or None.
    'qwen/qwen3.5-2b', 'qwen3.5:2b-q4_K_M' and 'Qwen3.5-2B-Q4_K_M.gguf' all match qwen3.5-2b.
    Several entries can share one download (a triage model that also does vision): `task`
    picks among them; without it the first catalog entry wins."""
    wanted = _canon(available_id)
    best_len, matches = 0, []
    for model in MODELS.values():
        for rid in (model.ollama, model.lmstudio, model.llamacpp):
            base = _canon(rid.split(":", 1)[0]) if rid else ""
            if not base or not wanted.startswith(base) or len(base) < best_len:
                continue
            if len(base) > best_len:
                best_len, matches = len(base), []
            if model not in matches:
                matches.append(model)
    if not matches:
        return None
    for model in matches:
        if task is None or model.task == task:
            return model
    return matches[0]


def best_installed(available_ids: list[str], task: str = "triage", ram_gb: float | None = None,
                   vram_gb: float = 0.0) -> str | None:
    """From ids a server reports (/ai/detect), the one the catalog ranks best for `task` that
    still fits the given hardware (when known). None if nothing suitable is installed."""
    order = [key for tier in reversed(TIERS) for t, key in PICKS[tier].items() if t == task and key]
    order += [key for key, m in MODELS.items() if m.task == task and key not in order]
    if task == "vision":  # every multimodal triage model can also look at photos
        order += [key for key, m in MODELS.items() if m.vision and key not in order]
    rank = {key: i for i, key in enumerate(dict.fromkeys(order))}
    candidates: list[tuple[int, str]] = []
    for model_id in available_ids:
        model = find_catalog_model(model_id, task)
        if model is None or model.key not in rank:
            continue
        if task == "vision" and not model.vision:
            continue
        if ram_gb is not None:
            tier = tier_for(ram_gb, vram_gb)
            if not _fits(model, tier, ram_gb, vram_gb, False, 0.0):
                continue
        candidates.append((rank[model.key], model_id))
    return min(candidates)[1] if candidates else None


__all__ = [
    "APP_RAM_GB",
    "MODELS",
    "PICKS",
    "TASKS",
    "TIERS",
    "TIER_LABELS_RU",
    "ModelChoice",
    "best_installed",
    "find_catalog_model",
    "recommend",
    "recommend_split",
    "tier_for",
]
