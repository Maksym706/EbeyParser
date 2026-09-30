"""Value of an offer for a build slot: a per-kind metric from the knowledge base (€ per GB of video
memory, € per GB of RAM, € per TB, € per 100 W, € per core) and warning flags read from the ad
text. GPUs for an LLM server get a 0..100 value score from €/GB VRAM and memory bandwidth per €."""

from __future__ import annotations

import math
from typing import Any

from ..pricing.text import matched_exclude_keyword, normalize
from .knowledge import GPUS, Part, fmt_gb, fmt_int

# reference for the GPU value score: an RTX 3090 at its typical used price
_REF = GPUS["rtx3090"]
REF_EUR_PER_GB = _REF.price.typical / _REF.specs["vram_gb"]
REF_GBS_PER_EUR = _REF.specs["bandwidth_gbs"] / _REF.price.typical

MINING_WORDS = ("mining", "miner", "mining rig", "hashrate", "ethereum", "krypto", "crypto", "nicehash", "minen")
MULTI_WORDS = ("mehrere vorhanden", "mehrere verfügbar", "mehrere stück", "stückzahl", "mehrfach vorhanden",
               "2 stück", "3 stück", "4 stück", "5 stück", "6 stück")
BLOWER_WORDS = ("turbo", "blower", "radial")
WATER_WORDS = ("wasserkühlung", "wakü", "waterblock", "wasserblock", "water block", "ek block", "alphacool")
FE_WORDS = ("founders edition", "founders", "fe")
NO_OUTPUT_WORDS = ("ohne adapter", "ohne kabel", "ohne netzteilkabel")


def _hit(text: str, words: tuple[str, ...]) -> bool:
    return matched_exclude_keyword(text, list(words)) is not None


def _flag(key: str, level: str, text: str) -> dict[str, str]:
    return {"key": key, "level": level, "text_ru": text}


def offer_flags(part: Part | None, title: str, description: str = "", *, variant: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Warnings / hints for one ad. level: warn (check before buying) | info | good."""
    text = f"{title}\n{description}"
    flat = normalize(text)
    flags: list[dict[str, str]] = []
    if part is None:
        return flags
    if part.kind == "gpu":
        if _hit(text, MINING_WORDS):
            flags.append(_flag("mining", "warn", "Похоже, карта работала в майнинге: попроси тест под нагрузкой "
                                                 "10 минут и посмотри температуру памяти"))
        if _hit(text, MULTI_WORDS):
            flags.append(_flag("many", "warn", "У продавца несколько одинаковых карт — частый признак майнинг-фермы"))
        if _hit(text, WATER_WORDS):
            flags.append(_flag("water", "warn", "Карта под водяное охлаждение: без своей системы СЖО не заработает — "
                                                "спроси, есть ли родной кулер"))
        elif _hit(text, BLOWER_WORDS) and part.specs.get("cooling") != "passive":
            flags.append(_flag("blower", "info", "Турбина (blower): громче, но выдувает жар наружу и занимает 2 слота — "
                                                 "удобно ставить несколько карт рядом"))
        elif part.specs.get("cooling") == "open":
            flags.append(_flag("open_cooler", "info", "Открытый кулер: тише, но двум картам рядом нужен зазор для воздуха"))
        if part.key == "rtx3090":
            if _hit(text, FE_WORDS):
                flags.append(_flag("fe", "info", "Founders Edition: 3 слота, разъём 12-pin (спроси, есть ли переходник "
                                                 "на 2 × 8-pin), память сзади сильно греется — часто меняют термопрокладки"))
            else:
                flags.append(_flag("rtx3090_memory", "info", "Обычная 3090: половина памяти на обратной стороне платы — "
                                                             "проверь температуру памяти под нагрузкой (Memory Junction)"))
        elif part.key == "rtx3090ti":
            flags.append(_flag("rtx3090ti", "good", "3090 Ti: вся память спереди — греется меньше, но нужен разъём "
                                                    "16-pin и 450 Вт"))
        if part.key == "tesla_p40":
            flags.append(_flag("passive", "warn", "Пассивная карта: нужен свой вентилятор-турбина (~25 €), иначе "
                                                  "перегреется"))
            flags.append(_flag("eps", "info", "Питание 8-pin CPU (EPS): нужен переходник с 2 × 8-pin PCIe"))
            if "luefter" in flat or "fan" in flat.split() or "shroud" in flat:
                flags.append(_flag("fan_included", "good", "В объявлении упомянут вентилятор — уточни, в комплекте ли он"))
        if part.key == "mi50_32":
            flags.append(_flag("passive", "warn", "Пассивная карта: нужен свой вентилятор (~25 €)"))
            flags.append(_flag("rocm", "info", "Работает через llama.cpp (Vulkan или сборки сообщества) — "
                                               "официальный ROCm её больше не поддерживает"))
            if "16gb" in flat.split() and "32gb" not in flat.split():
                flags.append(_flag("mi50_16", "warn", "Похоже, это версия на 16 ГБ — нужна 32 ГБ"))
        if part.specs.get("display") is False:
            flags.append(_flag("no_display", "info", "Нет видеовыхода — экран подключается к встроенной графике"))
    if part.kind in ("psu", "gpu") and _hit(text, NO_OUTPUT_WORDS):
        flags.append(_flag("no_cables", "warn", "Продаётся без кабелей/переходников — докупать отдельно"))
    if part.kind == "ram" and part.specs.get("registered") is False and _hit(text, ("ecc reg", "registered", "rdimm")):
        flags.append(_flag("rdimm", "warn", "Это серверная память (REG) — на обычной плате не заработает"))
    if part.kind == "storage" and part.specs.get("type") == "hdd" and _hit(text, ("smr",)):
        flags.append(_flag("smr", "warn", "Диск SMR — для NAS и зеркал плохо подходит"))
    return flags


def value_metric(part: Part | None, unit_cost: float | None) -> dict[str, Any] | None:
    """The slot's own 'how much do I get per euro' number."""
    if part is None or not unit_cost or unit_cost <= 0:
        return None
    s = part.specs
    if part.kind == "gpu" and s.get("vram_gb"):
        per_gb = unit_cost / s["vram_gb"]
        return {"key": "eur_per_gb_vram", "label_ru": "€ за ГБ видеопамяти", "value": round(per_gb, 1),
                "text_ru": f"{fmt_int(per_gb)} € за ГБ видеопамяти · {fmt_int(s['bandwidth_gbs'])} ГБ/с"}
    if part.kind == "ram" and s.get("size_gb"):
        per_gb = unit_cost / s["size_gb"]
        return {"key": "eur_per_gb", "label_ru": "€ за ГБ памяти", "value": round(per_gb, 2),
                "text_ru": f"{fmt_gb(per_gb)} € за ГБ памяти"}
    if part.kind == "storage" and s.get("size_tb"):
        per_tb = unit_cost / s["size_tb"]
        return {"key": "eur_per_tb", "label_ru": "€ за ТБ", "value": round(per_tb, 1),
                "text_ru": f"{fmt_int(per_tb)} € за ТБ"}
    if part.kind == "psu" and s.get("watts"):
        per_100 = unit_cost / s["watts"] * 100
        return {"key": "eur_per_100w", "label_ru": "€ за 100 Вт", "value": round(per_100, 1),
                "text_ru": f"{fmt_gb(per_100)} € за каждые 100 Вт"}
    if part.kind == "cpu" and s.get("cores"):
        per_core = unit_cost / s["cores"]
        return {"key": "eur_per_core", "label_ru": "€ за ядро", "value": round(per_core, 1),
                "text_ru": f"{fmt_gb(per_core)} € за ядро"}
    return None


_PENALTY = {"mining": 10.0, "many": 5.0, "water": 10.0, "mi50_16": 40.0, "no_cables": 3.0}


def _log_share(ratio: float) -> float:
    """1× the reference -> 0.5, 2× as good -> 0.75, 4× -> 1.0, half as good -> 0.25."""
    if ratio <= 0:
        return 0.0
    return max(0.0, min(1.0, 0.5 + math.log2(ratio) / 4))


def gpu_value_score(part: Part | None, unit_cost: float | None, flags: list[dict[str, str]] | None = None) -> float | None:
    """0..100 for LLM work: 60 % from € per GB of video memory, 40 % from bandwidth per €, both
    relative to an RTX 3090 at its typical used price (= 50; log scale, 4× as good = 100); minus the
    part's software risk and warning flags."""
    if part is None or part.kind != "gpu" or not unit_cost or unit_cost <= 0:
        return None
    s = part.specs
    per_gb = unit_cost / s["vram_gb"]
    per_eur = s["bandwidth_gbs"] / unit_cost
    memory = _log_share(REF_EUR_PER_GB / per_gb)
    speed = _log_share(per_eur / REF_GBS_PER_EUR)
    score = 100 * (0.6 * memory + 0.4 * speed) - 20 * part.risk
    for flag in flags or []:
        score -= _PENALTY.get(flag["key"], 0.0)
    return round(max(0.0, min(100.0, score)), 1)


def value_label(score: float | None) -> str:
    if score is None:
        return ""
    if score >= 75:
        return "Отличная ценность"
    if score >= 55:
        return "Хорошая ценность"
    if score >= 40:
        return "Обычная цена"
    return "Дорого за то, что даёт"


__all__ = ["gpu_value_score", "offer_flags", "value_label", "value_metric"]
