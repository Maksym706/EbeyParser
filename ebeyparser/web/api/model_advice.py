"""«Выбери своё железо → рекомендую модель»: GET /api/v1/ai/recommend, built from
ebeyparser/ai/model_catalog.recommend (docs/design/AI_MODELS.md). Pure functions: the route
passes in this machine's hardware and the local model servers it found."""

from __future__ import annotations

from typing import Any

from ...ai.model_catalog import MODELS, TIER_LABELS_RU, best_installed, recommend, tier_for

ROLES = ("server", "pc")
# response key -> catalog task
PICK_TASKS = {"triage": "triage", "vision": "vision", "embeddings": "embed", "second_opinion": "second_opinion"}
ROLE_TASKS = {"server": ("triage", "vision", "embeddings", "second_opinion"), "pc": ("vision", "second_opinion")}

TASK_LABELS_RU = {
    "triage": "Читает каждое объявление",
    "vision": "Смотрит фото лучших находок",
    "embeddings": "Ищет похожие объявления",
    "second_opinion": "Второе мнение по лучшим сделкам",
}
RUNTIME_LABELS = {"lmstudio": "LM Studio", "ollama": "Ollama", "llamacpp": "llama.cpp"}
SETUP_RU = {
    "lmstudio": "LM Studio: открой вкладку Discover, найди модель по названию ниже и скачай вариант Q4_K_M, "
                "потом включи сервер (Developer → Start Server).",
    "ollama": "Ollama: установи приложение с ollama.com и выбери модель по названию ниже — она скачается сама.",
    "llamacpp": "llama.cpp (для опытных): скачай файл модели GGUF, вариант Q4_K_M, с Hugging Face по названию ниже "
                "и запусти llama-server.",
}
VISION_SPEED_RU = {
    "T0": "≈ 40 с на фото — только для лучших кандидатов",
    "T1": "≈ 20–40 с на фото",
    "T2": "несколько секунд на фото",
    "T3": "пара секунд на фото",
}
SECOND_SPEED_RU = {"T1": "несколько минут на сделку — хватит на 1–2 в день",
                   "T2": "меньше минуты на сделку", "T3": "меньше минуты на сделку"}
ROLE_NOTES_RU = {
    "pc": "Объявления читает и похожие ищет сервер, который работает всегда. На ПК — только фото и второе мнение, "
          "когда он включён.",
}


def _num_ru(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _words(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _speed_ru(task: str, tier: str, sec: float | None, per_hour: int | None) -> str:
    if task == "triage" and sec:
        text = f"≈ {_num_ru(sec)} с на объявление"
        if per_hour:
            rounded = int(round(per_hour, -1)) if per_hour >= 100 else per_hour
            text += f", ~{rounded} {_words(rounded, 'объявление', 'объявления', 'объявлений')} в час"
        return text
    if task == "vision":
        return VISION_SPEED_RU.get(tier, "")
    if task == "embed":
        return "доли секунды на объявление"
    if task == "second_opinion":
        return SECOND_SPEED_RU.get(tier, "")
    return ""


def _get_ru(ids: dict[str, str | None]) -> dict[str, str]:
    """Where to get it, per runtime, in plain Russian (no shell commands)."""
    out: dict[str, str] = {}
    if ids.get("lmstudio"):
        out["lmstudio"] = f"В LM Studio найди «{ids['lmstudio']}» во вкладке Discover"
    if ids.get("ollama"):
        out["ollama"] = f"В Ollama выбери модель «{ids['ollama']}»"
    if ids.get("llamacpp"):
        repo, _, quant = str(ids["llamacpp"]).partition(":")
        out["llamacpp"] = f"На Hugging Face: {repo}" + (f", файл {quant}" if quant else "")
    return out


def pick_view(model: dict[str, Any] | None, key: str, tier: str) -> dict[str, Any] | None:
    """One recommended model (a ModelChoice.as_dict(tier) from recommend()) for the picker."""
    if model is None:
        return None
    task = PICK_TASKS[key]
    ids = {"ollama": model.get("ollama"), "lmstudio": model.get("lmstudio"), "llamacpp": model.get("llamacpp")}
    sec, per_hour = model.get("expected_sec_per_ad"), model.get("expected_ads_per_hour")
    return {
        "task": key,
        "task_ru": TASK_LABELS_RU[key],
        "key": model["key"],
        "name": str(model["name"]).replace(" + mmproj", ""),
        "label_ru": model.get("label_ru") or "",
        "desc_ru": model.get("desc_ru") or "",
        "ids": ids,
        "quant": model.get("quant"),
        "file_gb": model.get("file_gb"),
        "ram_gb": model.get("min_ram_gb"),
        "vram_gb": model.get("min_vram_gb"),
        "vision": bool(model.get("vision")),
        "arm_ok": bool(model.get("arm_ok")),
        "license": model.get("license"),
        "sec_per_ad": sec if task == "triage" else None,
        "ads_per_hour": per_hour if task == "triage" else None,
        "speed_ru": _speed_ru(task, tier, sec, per_hour),
        "get_ru": _get_ru(ids),
    }


def installed_view(servers: list[dict[str, Any]], role: str, ram_gb: float, vram_gb: float) -> dict[str, Any]:
    """Which models the found local servers already have that fit (model_catalog.best_installed)."""
    wanted = ROLE_TASKS[role]
    rows: list[dict[str, Any]] = []
    best: dict[str, dict[str, Any] | None] = {key: None for key in PICK_TASKS}
    for server in servers:
        models = list(server.get("models") or [])
        row = {"name": server.get("name"), "provider": server.get("provider"), "base_url": server.get("base_url"),
               "models": models}
        for key in PICK_TASKS:
            match = best_installed(models, task=PICK_TASKS[key], ram_gb=ram_gb, vram_gb=vram_gb) \
                if key in wanted else None
            row[key] = match
            if match and best[key] is None:
                best[key] = {"server": server.get("name"), "provider": server.get("provider"),
                             "base_url": server.get("base_url"), "model": match}
        rows.append(row)
    return {"servers": rows, **best}


def recommend_view(host: dict[str, Any], *, ram_gb: float | None = None, vram_gb: float | None = None,
                   arm: bool | None = None, role: str = "server",
                   servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The whole answer of GET /api/v1/ai/recommend. Missing parameters come from `host`."""
    role = role if role in ROLES else "server"
    gpu = host.get("gpu") or None
    from_host = ram_gb is None and vram_gb is None and arm is None
    ram = float(ram_gb if ram_gb is not None else (host.get("ram_gb") or 8.0))
    vram = float(vram_gb if vram_gb is not None else ((gpu or {}).get("vram_gb") or 0.0))
    is_arm = bool(arm if arm is not None else host.get("arm"))
    rec = recommend(ram, vram, is_arm)
    tier = rec["tier"]
    picks = {key: (pick_view(rec.get(task), key, tier) if key in ROLE_TASKS[role] else None)
             for key, task in PICK_TASKS.items()}
    notes = list(rec.get("notes_ru") or [])
    if role in ROLE_NOTES_RU:
        notes.insert(0, ROLE_NOTES_RU[role])
    triage, vision = picks.get("triage"), picks.get("vision")
    if triage:
        summary = f"{triage['name']} читает объявления: {triage['speed_ru']}" if triage["speed_ru"] \
            else f"{triage['name']} читает объявления"
    elif vision:
        summary = f"{vision['name']} смотрит фото: {vision['speed_ru']}".rstrip(": ")
    else:
        summary = "Для этого железа локальной модели нет — включи нейросеть на другом компьютере"
    host_ram = host.get("ram_gb")
    host_tier = tier_for(float(host_ram), float((gpu or {}).get("vram_gb") or 0.0)) if host_ram else None
    return {
        "host": {**host, "tier": host_tier, "tier_label_ru": TIER_LABELS_RU.get(host_tier or "", "")},
        "input": {"ram_gb": ram, "vram_gb": vram, "arm": is_arm, "role": role, "from_host": from_host},
        "role": role,
        "tier": tier,
        "tier_label_ru": rec["tier_label_ru"],
        "summary_ru": summary,
        "picks": picks,
        "notes_ru": notes,
        "installed_match": installed_view(servers or [], role, ram, vram) if servers is not None else None,
        "setup_ru": dict(SETUP_RU),
        "runtimes": dict(RUNTIME_LABELS),
    }


__all__ = ["MODELS", "ROLES", "installed_view", "pick_view", "recommend_view"]
