"""«Сборки» (build projects): knowledge base, LLM memory maths, planner rules and checks, part
matching, market prices, value scoring, tracker totals / trends and alert conditions."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta

import pytest

from ebeyparser.db import Database
from ebeyparser.models import Comparable, Evaluation, Listing, utcnow
from ebeyparser.projects import knowledge as kb
from ebeyparser.projects.market import KbMarket, Market, part_matches, ram_attrs, storage_attrs, watts
from ebeyparser.projects.models import Plan, PlanOption, Project, Purchase
from ebeyparser.projects.planner import (
    PlanRequest,
    apply_ai_answer,
    apply_dependents,
    build_plan,
    compat_checks,
    parse_goal,
    parse_json_object,
    resolve,
    search_words,
    vram_status,
)
from ebeyparser.projects.scoring import gpu_value_score, offer_flags, value_metric
from ebeyparser.projects.store import ProjectStore
from ebeyparser.projects.tracker import (
    ProjectAlerter,
    alert_candidates,
    alert_text,
    collect_offers,
    price_trend,
    project_state,
    round_price,
    stretch_label,
)

GOAL_70B = "Сервер для локальных LLM, чтобы тянул 70B в Q4, бюджет 1500 €"


def checks_by_key(plan: Plan) -> dict[str, dict]:
    return {c["key"]: c for c in compat_checks(plan)}


# ============================================================ knowledge base
def test_knowledge_base_is_consistent() -> None:
    for part in kb.PARTS.values():
        p = part.price
        assert 0 < p.low <= p.typical <= p.high, part.key
        assert part.query and part.label
        if part.kind == "gpu":
            s = part.specs
            assert s["vram_gb"] > 0 and s["bandwidth_gbs"] > 0 and s["tdp_w"] > 0 and s["slot_width"] >= 1
            lo, hi = s["efficiency"]
            assert 0 < lo < hi < 1
            assert part.match
        if part.kind in ("cpu", "board", "case", "cooling", "riser", "hba"):
            assert part.match, part.key
    for tmpl in kb.TEMPLATES.values():
        for spec in tmpl.slots:
            for key in spec.options:
                assert key in kb.PARTS, (tmpl.key, key)
    # facts the checks rely on
    assert kb.GPUS["tesla_p40"].specs["display"] is False and kb.GPUS["tesla_p40"].specs["cooling"] == "passive"
    assert kb.GPUS["tesla_p40"].specs["bandwidth_gbs"] == 346 and kb.GPUS["tesla_p40"].specs["tdp_w"] == 250
    assert kb.GPUS["mi50_32"].specs["display"] is None  # mini-DP exists but usually does not work: not guaranteed
    assert kb.GPUS["mi50_32"].specs["bandwidth_gbs"] == 1024 and kb.GPUS["mi50_32"].specs["vram_gb"] == 32
    assert kb.GPUS["rtx3090"].specs["bandwidth_gbs"] == 936 and kb.GPUS["rtx3090"].specs["tdp_w"] == 350
    assert kb.GPUS["rtx3090ti"].specs["tdp_w"] == 450 and kb.GPUS["rtx3060_12"].specs["bandwidth_gbs"] == 360
    assert kb.CPUS["r5_5600g"].specs["igpu"] and not kb.CPUS["r5_5600"].specs["igpu"]
    assert kb.BOARDS["x99"].specs["rdimm"] and not kb.BOARDS["b550"].specs["rdimm"]


def test_llm_memory_maths_70b_q4() -> None:
    mem = kb.llm_memory(70, "q4", 8192, gpus=2)
    assert mem.quant.label == "Q4_K_M"
    assert mem.weights_gib == pytest.approx(39.5, abs=0.2)  # a 70B Q4_K_M GGUF is ~42.5 GB = ~39.6 GiB
    assert mem.kv_gib == pytest.approx(2.5, abs=0.05)  # 80 layers × 8 KV heads × 128 × 2 × 2 bytes × 8192
    assert mem.overhead_gib == 2.0
    assert mem.total_gib == pytest.approx(44.0, abs=0.3)
    text = "\n".join(mem.lines_ru())
    assert "70 млрд" in text and "Q4_K_M" in text and "Итого нужно ≈ 44 ГБ" in text
    # context grows the KV cache linearly
    assert kb.llm_memory(70, "q4", 32768, 2).kv_gib == pytest.approx(10.0, abs=0.1)
    # a MoE model reads only its active weights per token
    moe = kb.llm_memory(30, "q8_0", 8192, 1, active_b=3)
    assert moe.active_weights_gib == pytest.approx(moe.weights_gib / 10)


def test_vram_status_and_capacity() -> None:
    need = kb.llm_memory(70, "q4_k_m", 8192, 2).total_gib
    assert vram_status(48, need) == "ok"
    assert vram_status(48, kb.llm_memory(70, "q4_k_m", 16384, 2).total_gib) == "warn"
    assert vram_status(48, kb.llm_memory(70, "q4_k_m", 32768, 2).total_gib) == "fail"
    assert vram_status(24, kb.llm_memory(70, "q4_k_m", 8192, 1).total_gib) == "fail"
    assert 30 < kb.max_model_b(24) < 40  # 24 GB: ~32B in Q4
    assert 65 < kb.max_model_b(48, gpus=2) < 80  # 48 GB: 70B in Q4


def test_speed_estimate_uses_bandwidth() -> None:
    weights = kb.llm_memory(70, "q4", 8192, 2).weights_gib
    fast = kb.speed_estimate(weights, [(kb.GPUS["rtx3090"], 2)])
    slow = kb.speed_estimate(weights, [(kb.GPUS["tesla_p40"], 2)])
    assert fast["ceiling"] == pytest.approx(936e9 / (weights * kb.GIB), rel=0.01)
    assert slow["high"] < fast["low"]
    assert "ток/с" in fast["label_ru"] and "936 ГБ/с" in fast["text_ru"]


def test_quant_normalization() -> None:
    assert kb.normalize_quant("Q4") == "q4_k_m"
    assert kb.normalize_quant("q4_K_M") == "q4_k_m"
    assert kb.normalize_quant("Q5_K_S") == "q5_k_m"
    assert kb.normalize_quant("8bit") == "q8_0"
    assert kb.normalize_quant("bf16") == "fp16"
    assert kb.normalize_quant("IQ4_XS") == "q4_k_m"
    assert kb.normalize_quant("garbage") is None


def test_psu_sizing_rules() -> None:
    one = kb.psu_recommendation([(kb.GPUS["rtx3090"], 1)], 65)
    assert one["load_w"] == 490 and one["recommended_w"] == 750  # NVIDIA asks 750 W for one RTX 3090
    two = kb.psu_recommendation([(kb.GPUS["rtx3090"], 2)], 65)
    assert two["load_w"] == 840 and two["recommended_w"] == 1300
    assert two["limited_w"] == 1200 and any("скачки" in line for line in two["lines_ru"])
    assert kb.psu_recommendation([(kb.GPUS["tesla_p40"], 2)], 65)["recommended_w"] == 850
    nas = kb.psu_recommendation([], 6, drives=2)
    assert nas["recommended_w"] == 450 and "диски" in nas["lines_ru"][0]


# ================================================================ goal parsing
@pytest.mark.parametrize(("text", "expected"), [
    (GOAL_70B, {"kind": "llm", "budget": 1500, "model_size_b": 70, "quant": "q4_k_m"}),
    ("Qwen3-30B-A3B в 8 бит, контекст 32k, 1,5к евро",
     {"kind": "llm", "budget": 1500, "model_size_b": 30, "active_b": 3, "quant": "q8_0", "context": 32768}),
    ("gpt-oss-120b, бюджет 2.000 €", {"kind": "llm", "budget": 2000, "model_size_b": 117, "active_b": 5.1}),
    ("2 б/у видеокарты для нейросети 48 ГБ VRAM", {"kind": "llm", "vram_gb": 48, "model_size_b": None}),
    ("Домашний NAS на 8 ТБ, бюджет 600€", {"kind": "nas", "budget": 600, "storage_tb": 8}),
    ("Игровой ПК 1080p до 800 евро", {"kind": "gaming", "budget": 800}),
])
def test_parse_goal(text: str, expected: dict) -> None:
    info = parse_goal(text)
    for key, value in expected.items():
        assert getattr(info, key) == value, key


def test_parse_goal_custom_list_translates_words() -> None:
    info = parse_goal("ThinkPad T480 до 200 €, док-станция, монитор 27 дюймов — бюджет 400 €")
    assert info.kind == "custom" and info.budget == 400
    items = [(i.label, i.query, i.target_price) for i in info.items]
    assert items == [("ThinkPad T480", "ThinkPad T480", 200.0), ("Док-станция", "dockingstation", None),
                     ("Монитор 27 дюймов", "monitor 27 zoll", None)]
    assert search_words("нужен хороший ноутбук thinkpad") == "notebook thinkpad"


# ===================================================================== planner
def test_plan_70b_q4_1500_rules() -> None:
    plan = build_plan(PlanRequest(goal=GOAL_70B), KbMarket())
    assert plan.template == "llm_48" and plan.budget == 1500
    gpu = plan.slot("gpu")
    keys = [o.key for o in gpu.options]
    for key in ("rtx3090x2", "tesla_p40x2", "mi50_32x2"):
        assert key in keys
    assert "rtx3060_12x2" not in keys and "tesla_p40" not in keys  # 24 GB can't hold 70B Q4
    # 2× RTX 3090 costs ~2 800 € in 2026: out of reach; both budget options fit — the faster one wins
    assert gpu.chosen == "mi50_32x2"
    r = resolve(plan)
    assert plan.slot("cpu").chosen == "r5_5600g"  # server cards have no working display output
    assert r["gpu_cooling"].active and r["gpu_cooling"].qty == 2
    assert not r["risers"].active and r["risers"].inactive_ru
    checks = checks_by_key(plan)
    assert all(c["status"] != "fail" for c in checks.values())
    assert checks["vram"]["status"] == "ok" and "64 ГБ" in checks["vram"]["summary"]
    assert checks["display"]["status"] == "ok" and checks["cooling"]["status"] == "ok"
    assert "Понял так" in plan.notes[0]


def test_plan_with_bigger_budget_takes_3090s_with_matching_platform() -> None:
    plan = build_plan(PlanRequest(goal="LLM 70B Q4", budget=2500), KbMarket())
    assert plan.slot("gpu").chosen == "rtx3090x2"
    assert plan.slot("psu").chosen == "psu_1300"
    assert plan.slot("case").chosen == "case_big"  # two 3-slot cards
    assert plan.slot("cpu").chosen == "r5_5600"
    checks = checks_by_key(plan)
    assert checks["psu"]["status"] == "ok" and checks["vram"]["status"] == "ok"
    assert "display" not in checks and "cooling" not in checks


def test_template_24gb_without_model() -> None:
    plan = build_plan(PlanRequest(template="llm_24"), KbMarket())
    keys = {o.key for o in plan.slot("gpu").options}
    assert {"rtx3090", "tesla_p40", "mi50_32", "rtx3060_12x2"} <= keys
    vram = checks_by_key(plan)["vram"]
    assert vram["status"] == "ok" and "Q4" in vram["summary"]


def test_checks_psu_ram_socket_display_pcie() -> None:
    plan = build_plan(PlanRequest(goal=GOAL_70B), KbMarket())
    plan.slot("gpu").chosen = "rtx3090x2"
    plan.slot("psu").chosen = "psu_1000"
    plan.slot("case").chosen = "case_atx"
    checks = checks_by_key(plan)
    assert checks["psu"]["status"] == "warn" and checks["psu"]["fix"]["option"] == "psu_1300"
    assert "1 300 Вт" in "\n".join(checks["psu"]["lines"])
    assert checks["spacing"]["status"] == "warn" and checks["spacing"]["fix"]["option"] == "case_big"
    plan.slot("psu").chosen = "psu_850"
    assert checks_by_key(plan)["psu"]["status"] == "fail"  # 850 W for an 840 W load
    # server RAM on a desktop board
    plan.slot("ram").chosen = "ddr4_ecc_64"
    ram = checks_by_key(plan)["ram"]
    assert ram["status"] == "fail" and "ECC REG" in ram["summary"] and ram["fix"]["option"] == "ddr4_32"
    # CPU and board sockets
    plan.slot("ram").chosen = "ddr4_32"
    plan.slot("cpu").chosen = "xeon_2680v4"
    sock = checks_by_key(plan)["socket"]
    assert sock["status"] == "fail" and sock["fix"]["option"] == "x99"
    # P40 without integrated graphics
    plan.slot("cpu").chosen = "r5_5600"
    plan.slot("gpu").chosen = "tesla_p40x2"
    display = checks_by_key(plan)["display"]
    assert display["status"] == "warn" and display["fix"]["option"] == "r5_5600g"
    # three cards on a two-slot board
    plan3 = build_plan(PlanRequest(goal="LLM 70B q4 3 карты", budget=4000), KbMarket())
    opt3 = next(o for o in plan3.slot("gpu").options if o.qty == 3) if any(o.qty == 3 for o in plan3.slot("gpu").options) \
        else None
    plan3.slot("gpu").options.append(PlanOption(key="rtx5060ti_16x3", kb_key="rtx5060ti_16", qty=3))
    plan3.slot("gpu").chosen = opt3.key if opt3 else "rtx5060ti_16x3"
    plan3.slot("board").chosen = "b550"
    plan3.slot("case").chosen = "case_big"
    pcie = checks_by_key(plan3)["pcie"]
    assert pcie["status"] == "fail" and pcie["fix"]["option"] == "x99"


def test_apply_dependents_fixes_only_incompatible() -> None:
    plan = build_plan(PlanRequest(goal=GOAL_70B), KbMarket())
    plan.slot("gpu").chosen = "rtx3090x2"
    apply_dependents(plan, keep={"gpu"}, ideal=False)
    assert plan.slot("psu").chosen == "psu_1300"  # 1000 W was too small
    assert plan.slot("case").chosen == "case_big"
    assert plan.slot("cpu").chosen == "r5_5600g"  # still compatible: kept
    plan.slot("gpu").chosen = "rtx5060ti_16x3" if plan.slot("gpu").option("rtx5060ti_16x3") else "rtx3090x2"


def test_nas_and_gaming_templates() -> None:
    nas = build_plan(PlanRequest(goal="Домашний NAS на 8 ТБ, бюджет 600€"), KbMarket())
    assert nas.template == "nas"
    r = resolve(nas)
    assert r["hdd"].part.key == "hdd_8tb" and r["hdd"].qty == 2
    assert not r["cpu"].active and not r["hba"].active  # the N100 board has its CPU
    assert nas.slot("ram").chosen == "ddr4_sodimm_16"
    checks = checks_by_key(nas)
    assert checks["capacity"]["status"] == "ok" and "8 ТБ" in checks["capacity"]["summary"]
    assert "€ в год" in checks["electricity"]["summary"] and checks["sata"]["status"] == "info"
    nas.slot("board").chosen = "b550"
    nas.slot("hdd").options[0].qty = 6
    nas.slot("hdd").chosen = nas.slot("hdd").options[0].key
    r = resolve(nas)
    assert r["cpu"].active and r["hba"].active  # 6 disks > 4 SATA ports
    game = build_plan(PlanRequest(goal="Игровой ПК 1080p до 800 евро"), KbMarket())
    assert game.template == "gaming_1080p"
    assert game.slot("gpu").chosen in kb.GAMING_GPUS
    assert game.slot("board").chosen == "b550" and game.slot("ram").chosen == "ddr4_16"
    assert all(c["status"] != "fail" for c in compat_checks(game))


def test_custom_plan_from_items() -> None:
    plan = build_plan(PlanRequest(goal="ThinkPad T480 до 200 €, монитор 27 дюймов — бюджет 400 €"), KbMarket())
    assert plan.template == "custom" and [s.label for s in plan.slots] == ["ThinkPad T480", "Монитор 27 дюймов"]
    first = plan.slots[0].option()
    assert first.target_price == 200 and first.target_by == "user" and first.source == "user"
    assert compat_checks(plan) == []


# ========================================================================= LLM
def test_parse_json_object_tolerates_noise() -> None:
    assert parse_json_object('<think>hmm</think>```json\n{"a": 1,}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"choices": {"gpu": "x"}} bye') == {"choices": {"gpu": "x"}}
    assert parse_json_object("no json here") is None


def test_ai_answer_only_picks_known_options_and_never_prices() -> None:
    req = PlanRequest(goal=GOAL_70B)
    plan = build_plan(req, KbMarket())
    answer = {
        "template": "nas",  # ignored: the model size decides
        "choices": {"gpu": "tesla_p40x2", "psu": "psu_9999", "nonsense": "x"},
        "reasons": {"gpu": "Дешевле и проще: CUDA"},
        "extra_items": [{"label": "Монитор", "query": "monitor 24 zoll", "why": "для настройки", "price": 5}],
        "summary": "Две P40 — самый дешёвый путь. https://evil.example",
        "prices": {"gpu": 1},
    }
    plan = apply_ai_answer(plan, answer, KbMarket(), explicit_template=False, req=req)
    assert plan.template == "llm_48"
    assert plan.slot("gpu").chosen == "tesla_p40x2" and plan.slot("gpu").option().why == "Дешевле и проще: CUDA"
    assert plan.ai["applied"] == {"gpu": "tesla_p40x2"} and "psu=psu_9999" in plan.ai["ignored"]
    extra = plan.slot("ai1")
    assert extra is not None and extra.option().query == "monitor 24 zoll" and extra.option().source == "ai"
    assert extra.option().target_price is None  # the model's "price" is never read
    assert "http" not in plan.ai["summary_ru"]
    assert plan.slot("psu").chosen == "psu_850"  # dependents follow the AI's GPU pick


# ================================================================== matching
@pytest.mark.parametrize(("key", "title", "ok"), [
    ("rtx3090", "Gigabyte RTX 3090 Gaming OC 24GB", True),
    ("rtx3090", "RTX 3090Ti 24GB", False),
    ("rtx3090", "Suche RTX 3090", False),
    ("rtx3090", "PC mit RTX 3090", False),
    ("rtx3090", "RTX 3090 defekt", False),
    ("tesla_p40", "Nvidia P40 24GB GPU", True),
    ("tesla_p40", "Tesla P40 Lüfter Shroud", False),
    ("mi50_32", "AMD Instinct MI50 32GB HBM2", True),
    ("mi50_32", "AMD MI50 16GB", False),
    ("rtx3060_12", "RTX 3060 8GB", False),
    ("r5_5600", "AMD Ryzen 5 5600X", True),
    ("r5_5600", "Ryzen 5 5600G", False),
    ("b550", "Bundle B550 + Ryzen 5 5600 + 16GB", False),
    ("b660_ddr4", "MSI PRO B660M-A DDR5", False),
    ("ddr4_32", "Corsair Vengeance 32GB DDR4 3200 (2x16GB) Kit", True),
    ("ddr4_32", "64GB DDR4 ECC REG 4x16GB", False),
    ("ddr4_ecc_64", "64GB DDR4 ECC REG 4x16GB Server", True),
    ("ddr4_32", "Gaming PC 32GB DDR4 RTX 3060", False),
    ("psu_1000", "be quiet! Straight Power 11 1000W Netzteil", True),
    ("psu_1000", "Corsair RM850x 850 Watt", False),
    ("nvme_1tb", "Crucial P3 1 TB M.2", True),
    ("hdd_8tb", "WD Elements 8TB externe Festplatte", False),
    ("case_big", "Fractal Design Define 7 XL Gehäuse", True),
    ("gpu_fan_kit", "Tesla P40 Lüfter Shroud 3D Druck", True),
])
def test_part_matches(key: str, title: str, ok: bool) -> None:
    assert part_matches(kb.PARTS[key], title) is ok


def test_attribute_parsers() -> None:
    assert ram_attrs("Kingston 2x16GB DDR4 SO-DIMM") == {"gen": "ddr4", "size_gb": 32, "registered": False,
                                                         "sodimm": True}
    assert storage_attrs("Samsung 870 EVO 500GB SSD")["size_tb"] == 0.5
    assert watts("Netzteil 1.000 W") is None and watts("Seasonic 1300 Watt") == 1300


# ==================================================================== scoring
def test_offer_flags_and_negation() -> None:
    gpu = kb.GPUS["rtx3090"]
    keys = {f["key"] for f in offer_flags(gpu, "RTX 3090 FE", "lief im Mining, mehrere Stück verfügbar")}
    assert {"mining", "many", "fe"} <= keys
    assert "mining" not in {f["key"] for f in offer_flags(gpu, "RTX 3090", "kein Mining, nur Gaming")}
    assert "blower" in {f["key"] for f in offer_flags(gpu, "Gigabyte RTX 3090 Turbo 24G")}
    assert "water" in {f["key"] for f in offer_flags(gpu, "RTX 3090 mit Wasserkühlung EK Block")}
    assert "rtx3090ti" in {f["key"] for f in offer_flags(kb.GPUS["rtx3090ti"], "RTX 3090 Ti")}
    p40 = {f["key"] for f in offer_flags(kb.GPUS["tesla_p40"], "Tesla P40 24GB")}
    assert {"passive", "eps", "no_display"} <= p40
    assert "mi50_16" in {f["key"] for f in offer_flags(kb.GPUS["mi50_32"], "MI50 16GB")}


def test_gpu_value_score_ranks_per_gb_and_bandwidth() -> None:
    ref = gpu_value_score(kb.GPUS["rtx3090"], kb.GPUS["rtx3090"].price.typical)
    assert ref == pytest.approx(48.0)  # 50 at the reference price, minus a little risk
    cheaper = gpu_value_score(kb.GPUS["rtx3090"], 800)
    assert cheaper > ref
    assert gpu_value_score(kb.GPUS["mi50_32"], 220) > gpu_value_score(kb.GPUS["tesla_p40"], 250) > ref
    mining = [{"key": "mining", "level": "warn", "text_ru": ""}]
    assert gpu_value_score(kb.GPUS["rtx3090"], 800, mining) == pytest.approx(cheaper - 10)
    metric = value_metric(kb.GPUS["rtx3090"], 960)
    assert metric["key"] == "eur_per_gb_vram" and metric["value"] == 40.0 and "40 € за ГБ" in metric["text_ru"]
    assert value_metric(kb.RAMS["ddr4_32"], 96)["value"] == 3.0
    assert value_metric(kb.PSUS["psu_1000"], 100)["value"] == 10.0


# ===================================================================== market
def _points(db: Database, title: str, prices: list[float], *, days_ago: float = 1.0) -> None:
    comps = [Comparable(title=title, price=p, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{abs(hash((title, p, i)))}",
                        source="kleinanzeigen") for i, p in enumerate(prices)]
    from ebeyparser.pricing.identity import identify

    key = identify(title).history_key() or title.lower()
    db.add_price_points(key, comps, seen_at=utcnow() - timedelta(days=days_ago))


def test_market_prefers_own_history() -> None:
    db = Database()
    market = Market(db, None, ttl=0)
    assert market.price(kb.GPUS["rtx3090"]).source == "kb"
    _points(db, "Nvidia RTX 3090 24GB", [900, 950, 980, 1000, 1020, 1050, 1100])
    _points(db, "RTX 3090 Ti 24GB", [1500, 1500, 1500])  # another product: filtered out
    info = market.price(kb.GPUS["rtx3090"])
    assert info.source == "history" and info.sample_size == 7 and not info.rough
    assert 800 < info.typical < 900  # median 1000 × asking-price discount 0.85
    few = Market(db, None, ttl=0)
    _points(db, "Tesla P40 24GB", [240, 260])
    p40 = few.price(kb.GPUS["tesla_p40"])
    assert p40.source == "kb" and "мало" in p40.notes_ru


# ==================================================================== tracker
def _tracking_project(db: Database, *, goal: str = GOAL_70B, budget: float | None = None,
                      gpu: str | None = None) -> tuple[Project, ProjectStore]:
    plan = build_plan(PlanRequest(goal=goal, budget=budget), KbMarket())
    if gpu:
        plan.slot("gpu").chosen = gpu
        apply_dependents(plan, keep={"gpu"}, ideal=False)
    store = ProjectStore(db)
    project = store.create(plan, status="tracking")
    for slot in project.slots:
        for opt in slot.options:
            store.link_search(project.id, slot.key, opt.key, f"S · {slot.key} · {opt.key}")
    return project, store


def _ad(db: Database, ad_id: str, title: str, price: float, search: str, *, ev: Evaluation | None = None,
        status: str | None = None, shipping: float | None = None) -> None:
    db.upsert_listing(Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}", title=title,
                              price=price, search_name=search, shipping_cost=shipping, distance_km=5.0))
    if ev is not None:
        db.save_evaluation(ev)
    if status:
        db.set_status(ad_id, status)


def test_offers_totals_and_targets() -> None:
    db = Database()
    project, store = _tracking_project(db)
    gpu = "S · gpu · mi50_32x2"
    _ad(db, "1", "AMD Instinct MI50 32GB", 200, gpu)
    _ad(db, "2", "AMD MI50 32GB HBM2", 180, gpu, shipping=10)
    _ad(db, "3", "AMD MI50 32GB", 150, gpu, status="ignored")  # hidden by the user
    _ad(db, "4", "Suche MI50 32GB", 50, gpu)  # wanted ad
    _ad(db, "5", "AMD MI50 16GB", 90, gpu)  # other variant
    _ad(db, "6", "AMD Instinct MI50 32GB", 250, gpu, ev=Evaluation(ad_id="6", verdict="skip", stage="prefilter",
                                                                  reasons=["Стоп-слова: «defekt»"]))
    offers = collect_offers(project, store)
    assert [o.ad_id for o in offers[("gpu", "mi50_32x2")]] == ["2", "1"]  # 190 (incl. shipping), 200
    state = project_state(project, store, KbMarket())
    gpu_state = state.slots["gpu"]
    assert gpu_state.need == 2 and gpu_state.best_cost == 390 and gpu_state.best_complete
    t = state.totals
    assert t["best_complete"] is False and "Процессор" in t["missing_offers"]
    assert t["estimated_total"] == pytest.approx(t["typical_total"] - 440 + 390)
    assert t["fits_budget"] is None and t["fits_estimate"] is True and t["slots_total"] == 8
    # budget 1500 > market total: every target is 10 % under the market
    assert state.ratio > 1 and gpu_state.target_unit == round_price(220 * 0.9)
    assert "10 %" in stretch_label(state.ratio)


def test_purchases_fill_slots_and_shift_budget() -> None:
    db = Database()
    project, store = _tracking_project(db)
    project.slot("psu").purchases.append(Purchase(price=80, qty=1))
    project.slot("psu").status = "bought"
    state = project_state(project, store, KbMarket())
    assert state.totals["spent"] == 80 and state.totals["remaining_budget"] == 1420
    assert state.slots["psu"].need == 0 and state.totals["slots_done"] == 1
    project.slot("gpu").purchases.append(Purchase(price=210, qty=1))
    state = project_state(project, store, KbMarket())
    assert state.slots["gpu"].need == 1 and state.slots["gpu"].rs.slot.status == "open"


def test_price_trend() -> None:
    now = utcnow()
    points = [(Comparable(title="x", price=p), now - timedelta(days=d), now) for p, d in
              [(1000, 30), (1010, 25), (990, 20), (900, 5), (910, 3), (890, 1)]]
    trend = price_trend(points, now=now)
    assert trend["direction"] == "down" and trend["change_pct"] < -3 and "Дешевеет" in trend["label_ru"]
    assert len(trend["points"]) >= 3
    assert price_trend(points[:2], now=now)["direction"] == "unknown"


# ===================================================================== alerts
def test_alert_conditions_slot_target_and_budget_fit() -> None:
    db = Database()
    project, store = _tracking_project(db, budget=1500)
    market = KbMarket()
    _ad(db, "g1", "AMD Instinct MI50 32GB", 150, "S · gpu · mi50_32x2")
    state = project_state(project, store, market)
    target = state.slots["gpu"].target_unit
    assert target == round_price(220 * 0.9) == 190  # 10 % under the market, in 10 € steps
    drafts = alert_candidates(state, set())
    assert [(d.kind, d.offer.ad_id) for d in drafts] == [("slot_target", "g1")]
    assert alert_candidates(state, {("g1", "slot_target")}) == []
    assert alert_candidates(state, set(), only_ad="other") == []
    text = alert_text(state, drafts, project_url="http://localhost:8000/projects/1")
    assert "MI50" in text and "Ниже твоей цели 190 €" in text and "экономия ~70 €" in text
    assert "https://www.kleinanzeigen.de/s-anzeige/x/g1" in text and "/projects/1" in text
    # every slot gets an offer -> the whole build fits the budget
    cheap = {"cpu": ("Ryzen 5 5600G", 80), "board": ("ASUS B550-F Mainboard", 70),
             "ram": ("32GB DDR4 3200 2x16GB Kit", 120), "psu": ("Corsair RM1000x 1000W Netzteil", 90),
             "storage": ("Samsung 970 Evo 1TB NVMe", 60), "case": ("Midi Tower ATX Gehäuse", 30),
             "gpu_cooling": ("Tesla Lüfter Shroud 3D", 20)}
    for n, (slot, (title, price)) in enumerate(cheap.items()):
        chosen = project.slot(slot).chosen
        _ad(db, f"a{n}", title, price, f"S · {slot} · {chosen}")
        _ad(db, f"b{n}", title, price + 5, f"S · {slot} · {chosen}")
    _ad(db, "g2", "AMD Instinct MI50 32GB", 160, "S · gpu · mi50_32x2")
    state = project_state(project, store, market)
    assert state.totals["best_complete"] and state.totals["fits_budget"] is True
    drafts = alert_candidates(state, {("g1", "slot_target")})
    kinds = {(d.kind, d.offer.ad_id) for d in drafts}
    assert ("budget_fit", "g2") in kinds or any(k == "budget_fit" for k, _ in kinds)
    project.fits_alerted = True
    assert not any(d.kind == "budget_fit" for d in alert_candidates(state, set()))
    project.status = "paused"
    assert alert_candidates(state, set()) == []


class FakeNotifier:
    name = "telegram"

    def __init__(self, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.texts.append(text)


def test_alerter_sends_once_and_retries_after_failure() -> None:
    db = Database()
    project, store = _tracking_project(db, budget=1500)
    notifier = FakeNotifier()
    events: list[dict] = []
    alerter = ProjectAlerter(db, config=lambda: None, notifiers=lambda: [notifier], publish=events.append,
                             project_url=lambda pid: f"http://localhost:8000/projects/{pid}", market=KbMarket())
    _ad(db, "g1", "AMD Instinct MI50 32GB", 150, "S · gpu · mi50_32x2")
    assert asyncio.run(alerter.check_ad("g1")) == 1
    assert len(notifier.texts) == 1 and "MI50" in notifier.texts[0]
    assert asyncio.run(alerter.check_ad("g1")) == 0  # never twice
    assert asyncio.run(alerter.sweep()) == 0
    assert any(e["reason"] == "alert" and e["alert"]["delivered"] for e in events)
    stored = store.alerts(project.id)
    assert stored[0].kind == "slot_target" and stored[0].delivered
    # a failing channel: the alert is retried after the next pass
    broken = FakeNotifier(fail=True)
    alerter2 = ProjectAlerter(db, config=lambda: None, notifiers=lambda: [broken], market=KbMarket())
    _ad(db, "g2", "AMD Instinct MI50 32GB", 140, "S · gpu · mi50_32x2")
    assert asyncio.run(alerter2.check_ad("g2")) == 1
    assert ("g2", "slot_target") not in store.alerted(project.id)
    alerter2._notifiers = lambda: [notifier]
    assert asyncio.run(alerter2.sweep()) >= 1 and ("g2", "slot_target") in store.alerted(project.id)
    # no channels at all: recorded for the app, not "delivered"
    alerter3 = ProjectAlerter(db, config=lambda: None, notifiers=lambda: [], market=KbMarket())
    _ad(db, "g3", "AMD Instinct MI50 32GB", 130, "S · gpu · mi50_32x2")
    asyncio.run(alerter3.check_ad("g3"))
    rec = next(a for a in store.alerts(project.id) if a.ad_id == "g3")
    assert rec.delivered is False


def test_alerter_ignores_projects_not_tracking_and_unknown_ads() -> None:
    db = Database()
    project, store = _tracking_project(db)
    project.status = "draft"
    store.save(project)
    notifier = FakeNotifier()
    alerter = ProjectAlerter(db, config=lambda: None, notifiers=lambda: [notifier], market=KbMarket())
    _ad(db, "g1", "AMD Instinct MI50 32GB", 100, "S · gpu · mi50_32x2")
    assert asyncio.run(alerter.check_ad("g1")) == 0 and asyncio.run(alerter.check_ad("nope")) == 0
    assert notifier.texts == []


# ====================================================================== store
def test_store_roundtrip_and_cascade() -> None:
    db = Database()
    store = ProjectStore(db)
    plan = build_plan(PlanRequest(goal=GOAL_70B), KbMarket())
    project = store.create(plan)
    project.slot("gpu").purchases.append(Purchase(price=200, ad_id="x"))
    project.slot("gpu").option().target_price = 180
    project.slot("gpu").option().target_by = "user"
    store.save(project)
    again = store.get(project.id)
    assert again is not None and again.slot("gpu").purchases[0].price == 200
    assert again.slot("gpu").option().target_by == "user" and again.requirements.model_size_b == 70
    assert [s.key for s in again.slots] == [s.key for s in plan.slots]
    store.link_search(project.id, "gpu", "mi50_32x2", "A")
    assert store.projects_for_search("A") == [project.id]
    assert store.delete(project.id) and store.get(project.id) is None
    assert store.projects_for_search("A") == []


def test_migration_adds_tables_to_an_existing_database(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    db = Database(path)
    db.upsert_listing(Listing(ad_id="1", url="https://www.kleinanzeigen.de/s-anzeige/x/1", title="RTX 3090", price=900))
    for table in ("project_alerts", "project_searches", "project_options", "project_slots", "projects"):
        db._conn.execute(f"DROP TABLE {table}")
    db._conn.commit()
    db.close()
    conn = sqlite3.connect(path)
    assert "projects" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    db = Database(path)  # an old file: the migration adds the tables, data stays
    assert db.get_listing("1") is not None
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "project_slots", "project_options", "project_searches", "project_alerts"} <= tables
    store = ProjectStore(db)
    pid = store.create(build_plan(PlanRequest(template="nas"), KbMarket())).id
    db.close()
    db = Database(path)  # idempotent
    assert ProjectStore(db).get(pid) is not None
    db.close()


def test_alert_for_a_tracked_alternative() -> None:
    db = Database()
    project, store = _tracking_project(db, budget=1500)
    rtx = project.slot("gpu").option("rtx3090x2")
    rtx.target_price = 600  # frozen when tracking started: a steal for 2× RTX 3090 within the budget
    store.save(project)
    _ad(db, "r1", "Gigabyte RTX 3090 Gaming OC 24GB", 580, "S · gpu · rtx3090x2")
    _ad(db, "r2", "MSI RTX 3090 Ventus 24GB", 900, "S · gpu · rtx3090x2")
    state = project_state(project, store, KbMarket())
    drafts = [d for d in alert_candidates(state, set()) if d.alternative]
    assert [(d.offer.ad_id, d.target) for d in drafts] == [("r1", 600)]
    text = alert_text(state, drafts)
    assert "вместо AMD Instinct MI50 32 ГБ — RTX 3090 24 ГБ за 580 €" in text and "Ниже твоей цели 600 €" in text
    many = alert_text(state, drafts * 5)
    assert "5 предложений ниже цели" in many and "(вариант)" in many


def test_market_uses_the_comparables_of_project_offers() -> None:
    from ebeyparser.models import PriceEstimate

    db = Database()
    project, store = _tracking_project(db)
    comps = [Comparable(title="AMD Instinct MI50 32GB", price=p, url=f"https://www.ebay.de/itm/{1000000000 + i}",
                        source="ebay_sold", sold=True) for i, p in enumerate([230, 240, 250, 260, 270])]
    comps.append(Comparable(title="AMD MI50 16GB", price=90, url="https://www.ebay.de/itm/2000000000", sold=True))
    ev = Evaluation(ad_id="c1", verdict="maybe", stage="full", estimate=PriceEstimate(comparables=comps))
    _ad(db, "c1", "AMD Instinct MI50 32GB", 245, "S · gpu · mi50_32x2", ev=ev)
    state = project_state(project, store, Market(db, None, ttl=0))
    price = state.slots["gpu"].price
    assert price.source == "history" and price.sample_size == 6  # 5 sold comparables + the offer, not the 16 GB one
    assert 240 <= price.typical <= 250
