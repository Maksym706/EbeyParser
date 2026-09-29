"""Curated knowledge for «Сборки» (build projects): parts with their real specs, platform rules,
PSU sizing and LLM memory maths. Everything here is hand-checked; a spec we are not sure about is
None (the checks then say «проверь в описании»), never a guess.

Prices are rough «ориентир» ranges for USED parts in Germany (autumn 2026: the memory crisis made
GPUs, RAM and SSDs far more expensive than a year before). They are only a fallback: the app's own
price history (price_points) and the offers of the project's searches always win. Prices never
come from the LLM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

PRICES_AS_OF = "осень 2026"
ROUGH_LABEL = f"ориентир (грубо, {PRICES_AS_OF})"
ROUGH_NOTE_RU = ("Цены-ориентиры грубые: в 2026 видеокарты, память и SSD сильно подорожали и цены скачут. "
                 "Точнее скажет история цен из объявлений — она появится после первых проверок.")
# 1 ГБ in this module = 1024³ bytes, like the "24 GB" of a graphics card
GIB = 1024 ** 3


@dataclass(frozen=True)
class PriceRange:
    """Used price in EUR (low / typical / high) and what buying new costs (or its nearest new
    equivalent, explained by `new_label`); `new` None = not sold new."""

    low: float
    typical: float
    high: float
    new: float | None = None
    new_label: str = ""


@dataclass(frozen=True)
class Part:
    """One buyable thing. `query` is the Kleinanzeigen search phrase; `match` are phrases an ad's
    title must fit (identity-aware for GPUs/CPUs, word-start matching otherwise); RAM, PSUs and
    drives are matched by their parsed attributes (see projects.tracker.part_matches)."""

    key: str
    kind: str  # gpu | cpu | board | ram | psu | storage | case | cooling | riser | hba
    label: str
    query: str
    price: PriceRange
    specs: dict[str, Any] = field(default_factory=dict)
    match: tuple[str, ...] = ()
    pros: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    risk: float = 0.0  # 0..1: tinkering / software trouble (lowers it as a default choice)
    exclude: tuple[str, ...] = ()  # extra stop words for its search (titles)
    where_ru: str = ""  # where it is usually found, when not on Kleinanzeigen


def _gpu(key: str, label: str, query: str, price: PriceRange, *, vram: int, bw: int, tdp: int, slots: int,
         display: bool | None, cooling: str, lanes: int, pcie: str, power: str | None, stack: str,
         eff: tuple[float, float], match: tuple[str, ...], perf: int | None = None, server: bool = False,
         memory: str = "", pros: tuple[str, ...] = (), caveats: tuple[str, ...] = (), risk: float = 0.0,
         exclude: tuple[str, ...] = (), where_ru: str = "") -> Part:
    specs = {"vram_gb": vram, "bandwidth_gbs": bw, "tdp_w": tdp, "slot_width": slots, "display": display,
             "cooling": cooling, "pcie_lanes": lanes, "pcie": pcie, "power": power, "stack": stack,
             "efficiency": eff, "server": server, "memory": memory, "perf_1080p": perf}
    return Part(key, "gpu", label, query, price, specs, match, pros, caveats, risk, exclude, where_ru)


WITHOUT_EQUIVALENT = "новыми не продаются"
NEW_24GB = "новой такой нет; ближайшая новая с 24 ГБ — RX 7900 XTX"

GPUS: dict[str, Part] = {p.key: p for p in (
    _gpu("rtx3090", "RTX 3090 24 ГБ", "rtx 3090", PriceRange(900, 1050, 1300, 1100, NEW_24GB),
         vram=24, bw=936, tdp=350, slots=3, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="2–3 × 8-pin (у Founders Edition — 12-pin через переходник)", stack="cuda", eff=(0.55, 0.75),
         memory="GDDR6X", match=("rtx 3090",), exclude=("3090 ti", "3090ti"),
         pros=("Лучшее соотношение цены и скорости среди карт с 24 ГБ",
               "CUDA: работает всё — LM Studio, llama.cpp, vLLM, ExLlama"),
         caveats=("Половина памяти на обратной стороне платы и сильно греется — нужен обдув, часто меняют "
                  "термопрокладки",
                  "Короткие скачки потребления до 1,5–2× от 350 Вт — блок питания нужен с запасом",
                  "Многие карты прошли майнинг — проверь под нагрузкой 10 минут"),
         risk=0.1),
    _gpu("rtx3090ti", "RTX 3090 Ti 24 ГБ", "rtx 3090 ti", PriceRange(1000, 1150, 1400, 1100, NEW_24GB),
         vram=24, bw=1008, tdp=450, slots=3, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="16-pin 12VHPWR (переходник на 3–4 × 8-pin)", stack="cuda", eff=(0.55, 0.75), memory="GDDR6X",
         match=("rtx 3090 ti",),
         pros=("Вся память с одной стороны платы — греется заметно меньше, чем у 3090",
               "Чуть быстрее обычной 3090"),
         caveats=("450 Вт и разъём 16-pin: блок питания нужен мощнее",
                  "Карты большие — 3 слота и больше"),
         risk=0.12),
    _gpu("rtx3060_12", "RTX 3060 12 ГБ", "rtx 3060 12gb", PriceRange(180, 220, 260, 320, "новая RTX 3060 12 ГБ"),
         vram=12, bw=360, tdp=170, slots=2, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="1 × 8-pin (у некоторых 8 + 6-pin)", stack="cuda", eff=(0.55, 0.75), memory="GDDR6", perf=105,
         match=("rtx 3060",), exclude=("3060 ti", "3060ti"),
         pros=("Дёшево, тихо, всего 170 Вт", "CUDA — всё работает из коробки"),
         caveats=("Бывает версия на 8 ГБ — она не подходит, бери только 12 ГБ",
                  "Медленная память (360 ГБ/с): большие модели будут идти неспешно")),
    _gpu("rtx4060ti_16", "RTX 4060 Ti 16 ГБ", "rtx 4060 ti 16gb",
         PriceRange(380, 430, 500, 500, "новая (снята с производства, остатки)"),
         vram=16, bw=288, tdp=165, slots=2, display=True, cooling="open", lanes=8, pcie="PCIe 4.0 x8",
         power="1 × 8-pin", stack="cuda", eff=(0.55, 0.75), memory="GDDR6", match=("rtx 4060 ti 16gb",),
         pros=("16 ГБ и мало потребляет",),
         caveats=("Узкая шина: 288 ГБ/с — на больших моделях медленнее даже RTX 3060",
                  "Бывает версия на 8 ГБ — не подходит"),
         risk=0.05),
    _gpu("rtx5060ti_16", "RTX 5060 Ti 16 ГБ", "rtx 5060 ti 16gb", PriceRange(430, 480, 550, 520, "новая в магазине"),
         vram=16, bw=448, tdp=180, slots=2, display=True, cooling="open", lanes=8, pcie="PCIe 5.0 x8",
         power="1 × 8-pin", stack="cuda", eff=(0.55, 0.75), memory="GDDR7", match=("rtx 5060 ti 16gb",),
         pros=("Можно купить новой с гарантией", "Быстрая память GDDR7: 448 ГБ/с"),
         caveats=("Бывает версия на 8 ГБ — не подходит", "Нужны свежие драйверы (CUDA 12.8 и новее)"),
         risk=0.05),
    _gpu("rtx4090", "RTX 4090 24 ГБ", "rtx 4090", PriceRange(1900, 2200, 2600, None, "новыми почти не продаются"),
         vram=24, bw=1008, tdp=450, slots=4, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="16-pin 12VHPWR", stack="cuda", eff=(0.55, 0.75), memory="GDDR6X", match=("rtx 4090",),
         pros=("Самая быстрая из доступных б/у карт",),
         caveats=("Очень дорогая", "450 Вт и разъём 16-pin — штекер должен быть вставлен до щелчка",
                  "Толщина 3,5–4 слота: две такие карты рядом не встанут"),
         risk=0.05),
    _gpu("rx7900xtx", "Radeon RX 7900 XTX 24 ГБ", "rx 7900 xtx", PriceRange(790, 900, 1100, 1100, "новая в магазине"),
         vram=24, bw=960, tdp=355, slots=3, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="2–3 × 8-pin", stack="rocm", eff=(0.5, 0.7), memory="GDDR6", match=("rx 7900 xtx",),
         pros=("24 ГБ и быстрая память", "Официально поддерживается ROCm, llama.cpp работает хорошо"),
         caveats=("Не CUDA: часть программ (vLLM, ExLlama) работает хуже или не работает",
                  "Под Windows поддержка скромнее, чем под Linux (LM Studio работает)"),
         risk=0.2),
    _gpu("arc_a770_16", "Intel Arc A770 16 ГБ", "arc a770 16gb", PriceRange(200, 250, 300, 300, "новая в магазине"),
         vram=16, bw=560, tdp=225, slots=2, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="8 + 6-pin", stack="sycl", eff=(0.4, 0.6), memory="GDDR6", match=("arc a770",),
         pros=("Дёшево за 16 ГБ и быструю память",),
         caveats=("Слабая поддержка программ: llama.cpp через SYCL или Vulkan, многое не работает",
                  "В BIOS нужен Resizable BAR"),
         risk=0.35),
    _gpu("tesla_p40", "Tesla P40 24 ГБ", "tesla p40", PriceRange(200, 250, 300, None, WITHOUT_EQUIVALENT),
         vram=24, bw=346, tdp=250, slots=2, display=False, cooling="passive", lanes=16, pcie="PCIe 3.0 x16",
         power="8-pin CPU (EPS) — нужен переходник с 2 × 8-pin PCIe", stack="cuda", eff=(0.45, 0.65),
         memory="GDDR5", server=True, match=("tesla p40", "p40 24gb"),
         pros=("Самые дешёвые 24 ГБ", "CUDA: LM Studio и llama.cpp работают"),
         caveats=("Пассивное охлаждение: без своего вентилятора (турбина + переходник) перегреется за минуту",
                  "Нет видеовыхода — нужен процессор со встроенной графикой",
                  "Питание через 8-pin CPU (EPS), а не PCIe: нужен переходник",
                  "Старая архитектура Pascal: медленная в FP16, NVIDIA сворачивает поддержку в новых драйверах",
                  "В BIOS нужен Above 4G Decoding"),
         risk=0.35, where_ru="чаще на eBay (из дата-центров), на Kleinanzeigen редко"),
    _gpu("mi50_32", "AMD Instinct MI50 32 ГБ", "mi50 32gb", PriceRange(170, 220, 300, None, WITHOUT_EQUIVALENT),
         vram=32, bw=1024, tdp=300, slots=2, display=None, cooling="passive", lanes=16, pcie="PCIe 4.0 x16",
         power=None, stack="rocm", eff=(0.35, 0.55), memory="HBM2", server=True,
         match=("instinct mi50 32gb", "mi50 32gb"),
         pros=("32 ГБ очень быстрой памяти HBM2 (1 ТБ/с) за небольшие деньги", "Больше всего гигабайт за евро"),
         caveats=("Официальная поддержка ROCm закончилась: работает через llama.cpp (Vulkan или сборки "
                  "сообщества), многие программы не заработают",
                  "Лучше под Linux — под Windows драйверов почти нет",
                  "Пассивное охлаждение: нужен свой вентилятор",
                  "Бывает версия на 16 ГБ — бери 32 ГБ",
                  "Видеовыход (mini-DisplayPort) обычно не работает — нужна встроенная графика в процессоре",
                  "В BIOS нужен Above 4G Decoding"),
         risk=0.55, where_ru="чаще на eBay/AliExpress (из Китая), на Kleinanzeigen редко"),
    # 1080p gaming (perf_1080p: rough relative index, RX 6600 = 100)
    _gpu("rx6600", "Radeon RX 6600 8 ГБ", "rx 6600", PriceRange(150, 170, 200, 230, "новая в магазине"),
         vram=8, bw=224, tdp=132, slots=2, display=True, cooling="open", lanes=8, pcie="PCIe 4.0 x8",
         power="1 × 8-pin", stack="rocm", eff=(0.5, 0.7), memory="GDDR6", perf=100, match=("rx 6600",),
         exclude=("6600 xt", "6600xt"),
         pros=("Экономичная (132 Вт) и тихая", "Хватает для 1080p на высоких"),
         caveats=("8 ГБ памяти — в новых играх иногда придётся снижать текстуры",)),
    _gpu("rtx3060ti", "RTX 3060 Ti 8 ГБ", "rtx 3060 ti", PriceRange(200, 230, 270, None, "снята с производства"),
         vram=8, bw=448, tdp=200, slots=2, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="1 × 8-pin (у некоторых 8 + 6)", stack="cuda", eff=(0.55, 0.75), memory="GDDR6", perf=130,
         match=("rtx 3060 ti",),
         pros=("Заметно быстрее RTX 3060 в играх", "DLSS"),
         caveats=("8 ГБ памяти — в новых играх иногда придётся снижать текстуры",)),
    _gpu("rx6700xt", "Radeon RX 6700 XT 12 ГБ", "rx 6700 xt", PriceRange(230, 260, 300, None, "снята с производства"),
         vram=12, bw=384, tdp=230, slots=2, display=True, cooling="open", lanes=16, pcie="PCIe 4.0 x16",
         power="8 + 6-pin", stack="rocm", eff=(0.5, 0.7), memory="GDDR6", perf=135, match=("rx 6700 xt",),
         pros=("12 ГБ памяти — запас на будущее", "Быстрее RTX 3060 в 1080p"),
         caveats=("Потребляет больше (230 Вт) — БП от 650 Вт",)),
)}
LLM_GPUS = ("rtx3090", "rtx3090ti", "rtx3060_12", "rtx4060ti_16", "rtx5060ti_16", "rx7900xtx", "arc_a770_16",
            "tesla_p40", "mi50_32", "rtx4090")
GAMING_GPUS = ("rx6600", "rtx3060_12", "rtx3060ti", "rx6700xt")
# GPU options a plan always shows when they fit (the ones people compare for an LLM server)
LLM_ALWAYS_SHOWN = ("rtx3090", "tesla_p40", "mi50_32", "rtx3060_12")


def _cpu(key: str, label: str, query: str, price: PriceRange, *, socket: str, cores: int, threads: int,
         tdp: int, igpu: bool, cooler: bool, pcie: str, match: tuple[str, ...], pros: tuple[str, ...] = (),
         caveats: tuple[str, ...] = (), lanes: int | None = None, risk: float = 0.0) -> Part:
    specs = {"socket": socket, "cores": cores, "threads": threads, "tdp_w": tdp, "igpu": igpu,
             "cooler_included": cooler, "pcie": pcie, "cpu_lanes": lanes}
    return Part(key, "cpu", label, query, price, specs, match, pros, caveats, risk)


CPUS: dict[str, Part] = {p.key: p for p in (
    _cpu("r5_5600", "Ryzen 5 5600 / 5600X", "ryzen 5 5600", PriceRange(70, 85, 100, 110, "новый в магазине"),
         socket="AM4", cores=6, threads=12, tdp=65, igpu=False, cooler=True, pcie="PCIe 4.0", lanes=24,
         match=("ryzen 5 5600", "ryzen 5 5600x"),
         pros=("Хватает с запасом и для LLM-сервера, и для игр",),
         caveats=("Нет встроенной графики — нужна видеокарта с видеовыходом",
                  "Б/у часто продают без кулера — тогда нужен кулер (~20 €)")),
    _cpu("r5_5600g", "Ryzen 5 5600G", "ryzen 5 5600g", PriceRange(80, 95, 110, 120, "новый в магазине"),
         socket="AM4", cores=6, threads=12, tdp=65, igpu=True, cooler=True, pcie="PCIe 3.0", lanes=24,
         match=("ryzen 5 5600g",),
         pros=("Встроенная графика: можно ставить серверные карты без видеовыхода",),
         caveats=("Линии PCIe 3.0 — для работы нейросети почти не важно",
                  "Б/у часто продают без кулера — тогда нужен кулер (~20 €)")),
    _cpu("r7_5700x", "Ryzen 7 5700X", "ryzen 7 5700x", PriceRange(100, 120, 140, 150, "новый в магазине"),
         socket="AM4", cores=8, threads=16, tdp=65, igpu=False, cooler=False, pcie="PCIe 4.0", lanes=24,
         match=("ryzen 7 5700x",),
         pros=("8 ядер — быстрее, если часть модели считается на процессоре",),
         caveats=("Нет встроенной графики", "Продаётся без кулера")),
    _cpu("xeon_2680v4", "Xeon E5-2680 v4", "xeon e5 2680 v4", PriceRange(15, 22, 35, None, WITHOUT_EQUIVALENT),
         socket="LGA2011-3", cores=14, threads=28, tdp=120, igpu=False, cooler=False, pcie="PCIe 3.0", lanes=40,
         match=("xeon e5-2680 v4",),
         pros=("40 линий PCIe — три видеокарты без узких мест", "Стоит копейки"),
         caveats=("Нет встроенной графики", "Продаётся без кулера", "Много потребляет в простое"),
         risk=0.15),
    _cpu("i5_12400f", "Core i5-12400F / 12400", "i5 12400", PriceRange(80, 95, 110, 120, "новый в магазине"),
         socket="LGA1700", cores=6, threads=12, tdp=65, igpu=False, cooler=True, pcie="PCIe 5.0", lanes=20,
         match=("i5 12400f", "i5 12400"),
         pros=("Быстрый в играх, в коробке есть кулер",),
         caveats=("Версия F — без встроенной графики",)),
    _cpu("r5_7600", "Ryzen 5 7600", "ryzen 5 7600", PriceRange(120, 145, 170, 190, "новый в магазине"),
         socket="AM5", cores=6, threads=12, tdp=65, igpu=True, cooler=True, pcie="PCIe 5.0", lanes=28,
         match=("ryzen 5 7600",),
         pros=("Современная платформа AM5 с запасом на апгрейд",),
         caveats=("Нужна только DDR5 — в 2026 она очень дорогая",)),
)}


def _board(key: str, label: str, query: str, price: PriceRange, *, socket: str, ram: tuple[str, ...],
           rdimm: bool, sodimm: bool, x16: int | None, lanes_ru: str, x8x8: bool, sata: int | None,
           match: tuple[str, ...], cpu_included: bool = False, igpu: bool = False, tdp: int = 0,
           idle_w: float | None = None, pros: tuple[str, ...] = (), caveats: tuple[str, ...] = (),
           risk: float = 0.0) -> Part:
    specs = {"socket": socket, "ram_types": ram, "rdimm": rdimm, "sodimm": sodimm, "x16_slots": x16,
             "lanes_ru": lanes_ru, "x8x8": x8x8, "sata_ports": sata, "cpu_included": cpu_included, "igpu": igpu,
             "tdp_w": tdp, "idle_w": idle_w}
    return Part(key, "board", label, query, price, specs, match, pros, caveats, risk)


BOARDS: dict[str, Part] = {p.key: p for p in (
    _board("b550", "Плата AM4 на B550 (ATX)", "b550 mainboard", PriceRange(70, 90, 120, 120, "новая в магазине"),
           socket="AM4", ram=("ddr4",), rdimm=False, sodimm=False, x16=2, x8x8=False, sata=4,
           lanes_ru="первый слот x16 от процессора, второй — x4 от чипсета", match=("b550",),
           pros=("Дешёвая и распространённая",),
           caveats=("Второй длинный слот работает как x4 — для работы нейросети нормально",
                    "Бывают платы mATX с одним длинным слотом — для двух карт нужна ATX")),
    _board("x570", "Плата AM4 на X570 (x8/x8)", "x570 mainboard", PriceRange(100, 130, 170, 180, "новая в магазине"),
           socket="AM4", ram=("ddr4",), rdimm=False, sodimm=False, x16=2, x8x8=True, sata=6,
           lanes_ru="два слота по x8 от процессора (PCIe 4.0)", match=("x570",),
           pros=("Две карты на честных x8/x8",),
           caveats=("У многих плат шумит вентилятор чипсета",)),
    _board("x99", "Плата X99 (LGA2011-3, 3 длинных слота)", "x99 mainboard", PriceRange(60, 80, 110, 110, "новая с AliExpress"),
           socket="LGA2011-3", ram=("ddr4",), rdimm=True, sodimm=False, x16=3, x8x8=True, sata=6,
           lanes_ru="до 40 линий от процессора: x16/x16/x8", match=("x99",),
           pros=("Много линий PCIe и слотов, дешёвая серверная память ECC REG",),
           caveats=("Китайские X99 (Huananzhi, Machinist) — лотерея по качеству: бери с отзывами",
                    "Число длинных слотов зависит от модели — проверь в описании"),
           risk=0.15),
    _board("b660_ddr4", "Плата LGA1700 на B660/B760 (DDR4)", "b660 ddr4 mainboard",
           PriceRange(80, 100, 130, 120, "новая в магазине"),
           socket="LGA1700", ram=("ddr4",), rdimm=False, sodimm=False, x16=2, x8x8=False, sata=4,
           lanes_ru="первый слот x16 от процессора, второй — x4 от чипсета", match=("b660 ddr4", "b760 ddr4"),
           caveats=("Бывают версии под DDR4 и под DDR5 — проверь, что DDR4",)),
    _board("b650", "Плата AM5 на B650", "b650 mainboard", PriceRange(110, 135, 170, 150, "новая в магазине"),
           socket="AM5", ram=("ddr5",), rdimm=False, sodimm=False, x16=2, x8x8=False, sata=4,
           lanes_ru="первый слот x16 от процессора, второй — x4 от чипсета", match=("b650",),
           caveats=("Только DDR5",)),
    _board("n100_itx", "Плата с Intel N100 (Mini-ITX, процессор уже на плате)", "n100 mainboard",
           PriceRange(100, 125, 160, 140, "новая в магазине"),
           socket="N100", ram=(), rdimm=False, sodimm=True, x16=None, x8x8=False, sata=None,
           lanes_ru="процессор распаян на плате", match=("n100",), cpu_included=True, igpu=True, tdp=6, idle_w=10,
           pros=("Очень экономичная: ~10 Вт в простое", "Процессор уже на плате, пассивное охлаждение"),
           caveats=("Число SATA-портов и тип памяти (DDR4 или DDR5 SO-DIMM) зависят от модели — проверь в описании",)),
)}


def _ram(key: str, label: str, query: str, price: PriceRange, *, gen: str, size: int, reg: bool = False,
         sodimm: bool = False, pros: tuple[str, ...] = (), caveats: tuple[str, ...] = ()) -> Part:
    note = "Память в 2026 подорожала в 3–4 раза — б/у заметно дешевле; проверь MemTest86"
    return Part(key, "ram", label, query, price, {"gen": gen, "size_gb": size, "registered": reg, "sodimm": sodimm},
                (), pros, (*caveats, note))


RAMS: dict[str, Part] = {p.key: p for p in (
    _ram("ddr4_16", "16 ГБ DDR4 (2 × 8)", "ddr4 16gb ram", PriceRange(50, 70, 100, 110, "новая"), gen="ddr4", size=16),
    _ram("ddr4_32", "32 ГБ DDR4 (2 × 16)", "ddr4 32gb ram", PriceRange(110, 150, 200, 210, "новая"), gen="ddr4", size=32),
    _ram("ddr4_64", "64 ГБ DDR4 (2 × 32 или 4 × 16)", "ddr4 64gb ram", PriceRange(220, 300, 400, 420, "новая"),
         gen="ddr4", size=64, pros=("Можно держать часть большой модели в ОЗУ",)),
    _ram("ddr4_ecc_64", "64 ГБ DDR4 ECC REG (серверная, 4 × 16)", "ddr4 ecc reg 64gb",
         PriceRange(120, 180, 260, None, WITHOUT_EQUIVALENT), gen="ddr4", size=64, reg=True,
         pros=("Серверная память из списанных серверов — дешевле обычной",),
         caveats=("Только для серверных плат и Xeon (X99): на обычных платах AM4/Intel не заработает",)),
    _ram("ddr5_32", "32 ГБ DDR5 (2 × 16)", "ddr5 32gb ram", PriceRange(250, 340, 450, 460, "новая"), gen="ddr5", size=32),
    _ram("ddr4_sodimm_16", "16 ГБ DDR4 SO-DIMM (ноутбучная)", "ddr4 16gb sodimm", PriceRange(45, 65, 90, 100, "новая"),
         gen="ddr4", size=16, sodimm=True),
    _ram("ddr5_sodimm_16", "16 ГБ DDR5 SO-DIMM (ноутбучная)", "ddr5 16gb sodimm", PriceRange(100, 140, 200, 220, "новая"),
         gen="ddr5", size=16, sodimm=True),
)}


def _psu(watts: int, price: PriceRange) -> Part:
    caveats = ("Для каждой видеокарты — отдельные кабели 8-pin PCIe, не «косичка» на две",) if watts >= 850 else ()
    return Part(f"psu_{watts}", "psu", f"Блок питания {watts} Вт (80+ Gold)", f"netzteil {watts}w", price,
                {"watts": watts}, (), ("80+ Gold — меньше греется и тише",) if watts >= 650 else (), caveats)


PSUS: dict[str, Part] = {p.key: p for p in (
    _psu(450, PriceRange(30, 40, 55, 60, "новый")),
    _psu(550, PriceRange(35, 50, 65, 70, "новый")),
    _psu(650, PriceRange(45, 60, 80, 85, "новый")),
    _psu(750, PriceRange(55, 70, 90, 100, "новый")),
    _psu(850, PriceRange(65, 85, 110, 120, "новый")),
    _psu(1000, PriceRange(85, 115, 150, 160, "новый")),
    _psu(1200, PriceRange(130, 160, 200, 230, "новый")),
    _psu(1300, PriceRange(140, 175, 220, 250, "новый")),
    _psu(1600, PriceRange(200, 260, 320, 380, "новый")),
)}
PSU_SIZES = tuple(sorted(p.specs["watts"] for p in PSUS.values()))


def _storage(key: str, label: str, query: str, price: PriceRange, *, kind: str, size_tb: float,
             caveats: tuple[str, ...] = ()) -> Part:
    return Part(key, "storage", label, query, price, {"type": kind, "size_tb": size_tb}, (), (), caveats)


HDD_NOTE = "Смотри часы работы и SMART (CrystalDiskInfo) — б/у диски часто из серверов"
STORAGE: dict[str, Part] = {p.key: p for p in (
    _storage("nvme_1tb", "NVMe SSD 1 ТБ", "nvme ssd 1tb", PriceRange(60, 85, 110, 145, "новый"), kind="nvme", size_tb=1),
    _storage("nvme_2tb", "NVMe SSD 2 ТБ", "nvme ssd 2tb", PriceRange(110, 150, 200, 250, "новый"), kind="nvme", size_tb=2),
    _storage("ssd_sata_256", "SATA SSD 240–256 ГБ (для системы)", "ssd 256gb", PriceRange(15, 25, 35, 45, "новый"),
             kind="ssd", size_tb=0.24),
    _storage("hdd_4tb", "Жёсткий диск 4 ТБ", "festplatte 4tb", PriceRange(50, 75, 100, 150, "новый"), kind="hdd",
             size_tb=4, caveats=(HDD_NOTE,)),
    _storage("hdd_8tb", "Жёсткий диск 8 ТБ", "festplatte 8tb", PriceRange(110, 150, 200, 290, "новый"), kind="hdd",
             size_tb=8, caveats=(HDD_NOTE,)),
)}


def _thing(key: str, kind: str, label: str, query: str, price: PriceRange, match: tuple[str, ...],
           specs: dict[str, Any] | None = None, pros: tuple[str, ...] = (), caveats: tuple[str, ...] = (),
           where_ru: str = "") -> Part:
    return Part(key, kind, label, query, price, dict(specs or {}), match, pros, caveats, 0.0, (), where_ru)


OTHER: dict[str, Part] = {p.key: p for p in (
    _thing("case_atx", "case", "Корпус ATX (7 слотов расширения)", "atx gehäuse", PriceRange(25, 40, 70, 70, "новый"),
           ("atx gehäuse", "midi tower", "big tower"), {"expansion_slots": 7, "open_frame": False, "bays_35": 2}),
    _thing("case_big", "case", "Большой корпус: 8+ слотов расширения (Define 7 XL, Meshify 2 XL, Enthoo Pro)",
           "big tower gehäuse", PriceRange(60, 90, 130, 160, "новый"),
           ("define 7 xl", "meshify 2 xl", "enthoo pro", "big tower", "full tower"),
           {"expansion_slots": 8, "open_frame": False, "bays_35": 4},
           pros=("Две толстые видеокарты влезают, есть место для воздуха",)),
    _thing("open_frame", "case", "Открытый стенд (майнинг-рама на 6–8 карт)", "mining rig rahmen",
           PriceRange(25, 40, 60, 55, "новый"), ("mining rahmen", "mining rig", "mining gestell", "open air rahmen"),
           {"expansion_slots": None, "open_frame": True},
           pros=("Любое число карт и отличный обдув",),
           caveats=("Карты ставятся через райзеры", "Открытое железо: пыль, шум, осторожно с домашними животными")),
    _thing("case_nas", "case", "Корпус для NAS с 4+ отсеками 3,5″ (Node 304 / 804, Jonsbo N2 / N3)",
           "node 304 gehäuse", PriceRange(50, 75, 110, 120, "новый"),
           ("node 304", "node 804", "jonsbo n2", "jonsbo n3", "nas gehäuse"), {"bays_35": 4, "open_frame": False}),
    _thing("cpu_cooler", "cooling", "Башенный кулер для процессора", "cpu kühler", PriceRange(15, 22, 35, 30, "новый"),
           ("cpu kühler", "cpu cooler", "tower kühler", "prozessorkühler")),
    _thing("gpu_fan_kit", "cooling", "Вентилятор-турбина с переходником для Tesla / Instinct", "tesla lüfter",
           PriceRange(15, 25, 35, 30, "новый"), ("tesla lüfter", "tesla fan", "mi50 lüfter", "fan shroud", "lüfter shroud"),
           where_ru="чаще на eBay/AliExpress или напечатать на 3D-принтере"),
    _thing("riser", "riser", "Райзер-кабель PCIe 4.0 x16", "pcie riser x16", PriceRange(25, 35, 50, 45, "новый"),
           ("riser x16", "riser kabel", "pcie riser", "pci e riser")),
    _thing("hba", "hba", "SATA-контроллер LSI 9211-8i / 9207-8i (IT-режим)", "lsi 9211-8i",
           PriceRange(25, 35, 50, None, WITHOUT_EQUIVALENT), ("9211 8i", "9207 8i", "9300 8i"),
           {"sata_ports": 8}, pros=("+8 дисков через два кабеля SFF-8087",)),
)}

PARTS: dict[str, Part] = {**GPUS, **CPUS, **BOARDS, **RAMS, **PSUS, **STORAGE, **OTHER}


def part(key: str | None) -> Part | None:
    return PARTS.get(key or "")


# ============================================================ LLM memory maths
@dataclass(frozen=True)
class Quant:
    key: str
    label: str
    bits: float  # effective bits per weight of a GGUF file (incl. the higher-precision layers)


QUANTS: dict[str, Quant] = {q.key: q for q in (
    Quant("q2_k", "Q2_K", 3.0),
    Quant("q3_k_m", "Q3_K_M", 3.9),
    Quant("q4_k_m", "Q4_K_M", 4.85),
    Quant("q5_k_m", "Q5_K_M", 5.7),
    Quant("q6_k", "Q6_K", 6.6),
    Quant("q8_0", "Q8_0", 8.5),
    Quant("fp16", "FP16", 16.0),
)}
DEFAULT_QUANT = "q4_k_m"
DEFAULT_CONTEXT = 8192
GPU_OVERHEAD_GIB = 1.0  # CUDA/ROCm context + compute buffers per card (llama.cpp, rough)
# typical dense architectures by size: (params B, layers, KV heads, head dim) —
# Llama 3.2 3B, Llama 3.1 8B, Qwen2.5 14B, Qwen2.5 32B, Llama 3 70B / Qwen2.5 72B, Mistral Large 2, Llama 3.1 405B
ARCHS: tuple[tuple[float, int, int, int], ...] = (
    (3, 28, 8, 128), (8, 32, 8, 128), (14, 48, 8, 128), (32, 64, 8, 128), (70, 80, 8, 128), (123, 88, 8, 128),
    (405, 126, 8, 128),
)


def quant(key: str | None) -> Quant:
    return QUANTS.get(normalize_quant(key) or DEFAULT_QUANT, QUANTS[DEFAULT_QUANT])


def normalize_quant(text: str | None) -> str | None:
    """'Q4' / 'q4_k_m' / '4-bit' / 'FP16' / 'Q8' -> a QUANTS key (None when unknown)."""
    t = (text or "").strip().lower().replace("-", "_").replace(" ", "")
    if not t:
        return None
    if t in QUANTS:
        return t
    if t in ("fp16", "f16", "bf16", "16bit", "16", "16бит"):
        return "fp16"
    aliases = {"q2": "q2_k", "q3": "q3_k_m", "q4": "q4_k_m", "q5": "q5_k_m", "q6": "q6_k", "q8": "q8_0",
               "4bit": "q4_k_m", "4": "q4_k_m", "4бит": "q4_k_m", "8bit": "q8_0", "8": "q8_0", "8бит": "q8_0",
               "3bit": "q3_k_m", "5bit": "q5_k_m", "6bit": "q6_k", "awq": "q4_k_m", "gptq": "q4_k_m", "int4": "q4_k_m",
               "int8": "q8_0"}
    if t in aliases:
        return aliases[t]
    for prefix, key in (("q2", "q2_k"), ("q3", "q3_k_m"), ("q4", "q4_k_m"), ("q5", "q5_k_m"), ("q6", "q6_k"),
                        ("q8", "q8_0"), ("iq4", "q4_k_m"), ("iq3", "q3_k_m"), ("iq2", "q2_k")):
        if t.startswith(prefix):
            return key
    return None


def arch_for(params_b: float) -> tuple[float, int, int, int]:
    """The typical architecture nearest to `params_b` (log distance)."""
    size = max(params_b, 0.5)
    return min(ARCHS, key=lambda a: abs(math.log(a[0]) - math.log(size)))


def kv_bytes_per_token(params_b: float, kv_bytes: float = 2.0) -> float:
    _, layers, kv_heads, head_dim = arch_for(params_b)
    return 2 * layers * kv_heads * head_dim * kv_bytes


def fmt_gb(value: float) -> str:
    """42.44 -> '42,4', 48 -> '48'."""
    if abs(value - round(value)) < 0.05:
        return f"{round(value):d}"
    return f"{value:.1f}".replace(".", ",")


def fmt_int(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


@dataclass
class LlmMemory:
    params_b: float
    quant: Quant
    context: int
    gpus: int
    weights_gib: float
    kv_gib: float
    overhead_gib: float
    active_b: float | None = None

    @property
    def total_gib(self) -> float:
        return self.weights_gib + self.kv_gib + self.overhead_gib

    @property
    def active_weights_gib(self) -> float:
        if self.active_b and self.active_b < self.params_b:
            return self.weights_gib * self.active_b / self.params_b
        return self.weights_gib

    def lines_ru(self) -> list[str]:
        q = self.quant
        _, layers, kv_heads, head_dim = arch_for(self.params_b)
        lines = [
            f"Веса: {fmt_gb(self.params_b)} млрд параметров × {str(q.bits).replace('.', ',')} бита ({q.label}) ÷ 8 "
            f"≈ {fmt_gb(self.weights_gib)} ГБ",
            f"Контекст {fmt_int(self.context)} токенов (KV-кэш): {layers} слоёв × {kv_heads} KV-голов × {head_dim} × 2 × "
            f"2 байта ≈ {fmt_gb(self.kv_gib)} ГБ",
            f"Служебное: ~{fmt_gb(GPU_OVERHEAD_GIB)} ГБ на каждую видеокарту × {self.gpus} = {fmt_gb(self.overhead_gib)} ГБ",
            f"Итого нужно ≈ {fmt_gb(self.total_gib)} ГБ видеопамяти",
        ]
        if self.active_b and self.active_b < self.params_b:
            lines.insert(1, f"Модель MoE: на каждый токен читается только ~{fmt_gb(self.active_b)} млрд активных "
                            "параметров — поэтому быстрее, но в память всё равно нужны все веса")
        return lines


def llm_memory(params_b: float, quant_key: str | None = None, context: int | None = None, gpus: int = 1,
               *, active_b: float | None = None) -> LlmMemory:
    """Video memory a GGUF model needs (llama.cpp, KV cache in FP16)."""
    q = quant(quant_key)
    ctx = int(context or DEFAULT_CONTEXT)
    weights = params_b * 1e9 * q.bits / 8 / GIB
    kv = kv_bytes_per_token(params_b) * ctx / GIB
    return LlmMemory(params_b, q, ctx, max(1, gpus), weights, kv, GPU_OVERHEAD_GIB * max(1, gpus), active_b)


def max_model_b(vram_gib: float, quant_key: str | None = None, context: int | None = None, gpus: int = 1) -> float:
    """Largest dense model (billions of params) that fits `vram_gib` with a small reserve."""
    lo, hi = 0.0, 1000.0
    for _ in range(40):
        mid = (lo + hi) / 2
        need = llm_memory(mid, quant_key, context, gpus).total_gib
        if need <= vram_gib * 0.96:
            lo = mid
        else:
            hi = mid
    return lo


def speed_estimate(weights_gib: float, cards: list[tuple[Part, int]]) -> dict[str, Any] | None:
    """Rough decode speed: every token reads all (active) weights once; with a layer split each card
    reads its share (proportional to its memory) at its own bandwidth. Real speed is a fraction of
    that ceiling (per-card efficiency from the knowledge base)."""
    units = [(p, n) for p, n in cards if p.specs.get("bandwidth_gbs") and p.specs.get("vram_gb")]
    if not units or weights_gib <= 0:
        return None
    total_vram = sum(p.specs["vram_gb"] * n for p, n in units)
    seconds = 0.0
    eff_lo = min(p.specs["efficiency"][0] for p, _ in units)
    eff_hi = min(p.specs["efficiency"][1] for p, _ in units)
    for p, n in units:
        share = weights_gib * GIB * (p.specs["vram_gb"] * n) / total_vram
        seconds += share / (p.specs["bandwidth_gbs"] * 1e9)
    ceiling = 1.0 / seconds
    bw = min(p.specs["bandwidth_gbs"] for p, _ in units)
    low, high = ceiling * eff_lo, ceiling * eff_hi
    text = (f"Скорость (грубо): память {fmt_int(bw)} ГБ/с ÷ {fmt_gb(weights_gib)} ГБ весов → потолок "
            f"~{fmt_int(ceiling)} ток/с, на практике ≈ {_rate(low)}–{_rate(high)} ток/с")
    return {"ceiling": round(ceiling, 1), "low": round(low, 1), "high": round(high, 1), "text_ru": text,
            "label_ru": f"≈ {_rate(low)}–{_rate(high)} ток/с"}


def _rate(value: float) -> str:
    return fmt_int(value) if value >= 10 else f"{value:.1f}".replace(".", ",")


# ================================================================== PSU sizing
BASE_SYSTEM_W = 75  # board, RAM, SSD, fans
HDD_W = 10  # a 3.5" drive under load (spin-up peaks ~25 W are covered by the headroom)
PSU_HEADROOM = 1.3  # 30 % for transient spikes and the efficiency sweet spot
SPIKY_GPUS = frozenset({"rtx3090", "rtx3090ti", "rtx4090"})
SPIKE_ALLOWANCE_W = 100  # per card with known short spikes (NVIDIA itself asks 750 W for one RTX 3090)


def psu_recommendation(gpus: list[tuple[Part, int]], cpu_tdp: float, *, drives: int = 0) -> dict[str, Any]:
    """Load under full use, recommended PSU (next standard size above load × 1.3), plain-Russian maths."""
    parts: list[str] = []
    load = 0.0
    for p, n in gpus:
        tdp = float(p.specs.get("tdp_w") or 0)
        if n and tdp:
            load += tdp * n
            parts.append(f"{n} × {fmt_int(tdp)} Вт ({p.label})" if n > 1 else f"{fmt_int(tdp)} Вт ({p.label})")
    if cpu_tdp:
        load += cpu_tdp
        parts.append(f"{fmt_int(cpu_tdp)} Вт (процессор)")
    load += BASE_SYSTEM_W
    parts.append(f"~{BASE_SYSTEM_W} Вт (плата, память, SSD, вентиляторы)")
    if drives:
        load += HDD_W * drives
        parts.append(f"{drives} × {HDD_W} Вт (диски)")
    spiky = [(p, n) for p, n in gpus if n and p.key in SPIKY_GPUS]
    spike_w = SPIKE_ALLOWANCE_W * sum(n for _, n in spiky)
    target = load * PSU_HEADROOM + spike_w
    recommended = next((w for w in PSU_SIZES if w >= target), PSU_SIZES[-1])
    maths = f"{fmt_int(load)} × 1,3" + (f" + {fmt_int(spike_w)} Вт на скачки" if spike_w else "")
    lines = [" + ".join(parts) + f" = {fmt_int(load)} Вт под полной нагрузкой",
             f"С запасом 30 % на пики и тишину: {maths} ≈ {fmt_int(target)} Вт → блок питания "
             f"от {fmt_int(recommended)} Вт"]
    if spiky:
        lines.append(f"У {spiky[0][0].label.split(' 24')[0]} бывают короткие скачки до 1,5–2× от паспортной мощности — "
                     "слабый блок питания будет выключать компьютер")
    gpu_w = sum(float(p.specs.get("tdp_w") or 0) * n for p, n in gpus)
    limited = None
    if gpu_w >= 500:
        # power-limited cards lose little speed in LLM work (memory bound)
        cut = sum(float(p.specs.get("tdp_w") or 0) * 0.8 * n for p, n in gpus)
        limited_load = load - gpu_w + cut
        limited = next((w for w in PSU_SIZES if w >= limited_load * PSU_HEADROOM + spike_w), PSU_SIZES[-1])
        if limited < recommended:
            lines.append(f"Если ограничить мощность видеокарт до ~80 % (для нейросетей скорость почти не падает), "
                         f"хватит {fmt_int(limited)} Вт")
    return {"load_w": round(load), "target_w": round(target), "recommended_w": recommended,
            "limited_w": limited, "lines_ru": lines}


# ==================================================================== templates
@dataclass(frozen=True)
class SlotSpec:
    key: str
    label: str
    kind: str
    options: tuple[str, ...] = ()
    when: str | None = None  # condition, see planner.CONDITIONS
    per_gpu: bool = False  # quantity follows the number of GPUs (fans, risers)
    hint: str = ""


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    subtitle: str
    icon: str
    kind: str  # llm | nas | gaming | custom
    budget: float | None
    slots: tuple[SlotSpec, ...]
    vram_gb: float | None = None
    example_goal: str = ""


_LLM_SLOTS = (
    SlotSpec("gpu", "Видеокарты", "gpu", hint="Главное для нейросети — сколько видеопамяти и насколько она быстрая"),
    SlotSpec("cpu", "Процессор", "cpu", ("r5_5600", "r5_5600g", "r7_5700x", "xeon_2680v4")),
    SlotSpec("board", "Материнская плата", "board", ("b550", "x570", "x99")),
    SlotSpec("ram", "Оперативная память", "ram", ("ddr4_32", "ddr4_64", "ddr4_ecc_64")),
    SlotSpec("psu", "Блок питания", "psu", ("psu_650", "psu_750", "psu_850", "psu_1000", "psu_1200", "psu_1300",
                                            "psu_1600")),
    SlotSpec("storage", "SSD", "storage", ("nvme_1tb", "nvme_2tb"), hint="Модель 70B в Q4 — это ~40 ГБ на диске"),
    SlotSpec("case", "Корпус", "case", ("case_atx", "case_big", "open_frame")),
    SlotSpec("gpu_cooling", "Охлаждение серверных карт", "cooling", ("gpu_fan_kit",), when="passive_gpu", per_gpu=True,
             hint="У серверных карт нет своих вентиляторов"),
    SlotSpec("risers", "Райзеры PCIe", "riser", ("riser",), when="open_frame", per_gpu=True),
    SlotSpec("cooler", "Кулер процессора", "cooling", ("cpu_cooler",), when="cpu_needs_cooler"),
)

TEMPLATES: dict[str, Template] = {t.key: t for t in (
    Template("llm_24", "LLM-сервер 24 ГБ VRAM", "Модели до ~32B в Q4 (Qwen 32B, Gemma 27B) целиком в видеопамяти",
             "cpu", "llm", 1000, _LLM_SLOTS, vram_gb=24,
             example_goal="Сервер для локальных нейросетей: модели до 32B, бюджет 1000 €"),
    Template("llm_48", "LLM-сервер 48 ГБ VRAM", "70B в Q4 (Llama 3.3 70B, Qwen 72B) целиком в видеопамяти",
             "server", "llm", 1600, _LLM_SLOTS, vram_gb=48,
             example_goal="Сервер для локальных LLM, чтобы тянул 70B в Q4, бюджет 1500 €"),
    Template("nas", "Домашний NAS", "Файлы, фото и бэкапы: тихий и экономный, диски зеркалом", "hard-drive", "nas", 600, (
        SlotSpec("board", "Плата и процессор", "board", ("n100_itx", "b550")),
        SlotSpec("cpu", "Процессор", "cpu", ("r5_5600g",), when="board_needs_cpu"),
        SlotSpec("ram", "Оперативная память", "ram", ("ddr4_sodimm_16", "ddr5_sodimm_16", "ddr4_16")),
        SlotSpec("hdd", "Жёсткие диски", "storage", ("hdd_8tb", "hdd_4tb"),
                 hint="Два одинаковых диска зеркалом: один умрёт — данные останутся"),
        SlotSpec("boot", "Диск для системы", "storage", ("ssd_sata_256", "nvme_1tb")),
        SlotSpec("psu", "Блок питания", "psu", ("psu_450", "psu_550")),
        SlotSpec("case", "Корпус", "case", ("case_nas",)),
        SlotSpec("hba", "SATA-контроллер", "hba", ("hba",), when="hba_needed"),
    ), example_goal="Домашний NAS на 8 ТБ для фото и бэкапов, бюджет 600 €"),
    Template("gaming_1080p", "Игровой ПК 1080p", "Современные игры в Full HD на высоких настройках", "gamepad-2",
             "gaming", 800, (
                 SlotSpec("gpu", "Видеокарта", "gpu", GAMING_GPUS),
                 SlotSpec("cpu", "Процессор", "cpu", ("r5_5600", "i5_12400f", "r5_7600")),
                 SlotSpec("board", "Материнская плата", "board", ("b550", "b660_ddr4", "b650")),
                 SlotSpec("ram", "Оперативная память", "ram", ("ddr4_16", "ddr4_32", "ddr5_32")),
                 SlotSpec("psu", "Блок питания", "psu", ("psu_550", "psu_650", "psu_750")),
                 SlotSpec("storage", "SSD", "storage", ("nvme_1tb", "nvme_2tb")),
                 SlotSpec("case", "Корпус", "case", ("case_atx",)),
                 SlotSpec("cooler", "Кулер процессора", "cooling", ("cpu_cooler",), when="cpu_needs_cooler"),
             ), example_goal="Игровой ПК для 1080p, бюджет 800 €"),
    Template("custom", "Свой список", "Любые вещи: перечисли, что нужно, и бюджет", "list-checks", "custom", None, (),
             example_goal="ThinkPad T480, док-станция, монитор 27 дюймов — бюджет 400 €"),
)}
TEMPLATE_ORDER = ("llm_24", "llm_48", "nas", "gaming_1080p", "custom")


def template(key: str | None) -> Template | None:
    return TEMPLATES.get(key or "")


__all__ = [
    "BOARDS", "CPUS", "DEFAULT_CONTEXT", "DEFAULT_QUANT", "GAMING_GPUS", "GIB", "GPUS", "LLM_ALWAYS_SHOWN", "LLM_GPUS",
    "LlmMemory", "OTHER", "PARTS", "PRICES_AS_OF", "PSUS", "PSU_SIZES", "Part", "PriceRange", "QUANTS", "Quant", "RAMS",
    "ROUGH_LABEL", "ROUGH_NOTE_RU", "STORAGE", "SlotSpec", "TEMPLATES", "TEMPLATE_ORDER", "Template", "arch_for",
    "fmt_gb", "fmt_int", "kv_bytes_per_token", "llm_memory", "max_model_b", "normalize_quant", "part",
    "psu_recommendation", "quant", "speed_estimate", "template",
]
