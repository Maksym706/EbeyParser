"""The model catalog: pure data + recommend() for onboarding/settings and auto-pick."""

import re

import pytest

from ebeyparser.ai import model_catalog as mc


def test_catalog_entries_are_complete():
    for key, model in mc.MODELS.items():
        assert key == model.key
        assert model.task in mc.TASKS
        assert model.ollama or model.lmstudio or model.llamacpp, key
        assert model.quant and model.license
        assert model.file_gb > 0 and model.min_ram_gb > 0 and model.min_vram_gb >= 0
        assert model.label_ru and re.search("[а-яА-Я]", model.label_ru), key
        assert model.desc_ru and re.search("[а-яА-Я]", model.desc_ru), key
        assert len(model.desc_ru) <= 120, key  # one line in the UI
        for tier, sec in model.sec_per_ad.items():
            assert tier in mc.TIERS and sec > 0


def test_picks_reference_existing_models_of_the_right_task():
    for tier in mc.TIERS:
        assert set(mc.PICKS[tier]) == set(mc.TASKS)
        for task, key in mc.PICKS[tier].items():
            if key is not None:
                assert mc.MODELS[key].task == task or (task == "vision" and mc.MODELS[key].vision)
    for src, dst in mc.FALLBACKS.items():
        assert src in mc.MODELS and dst in mc.MODELS


def test_vision_picks_can_see():
    for tier in mc.TIERS:
        key = mc.PICKS[tier]["vision"]
        assert key is None or mc.MODELS[key].vision


@pytest.mark.parametrize(
    "ram, vram, tier",
    [(4, 0, "T0"), (8, 0, "T0"), (16, 0, "T1"), (32, 0, "T1"), (16, 8, "T2"), (32, 12, "T2"),
     (32, 24, "T3"), (64, 48, "T3"), (8, 4, "T0")],
)
def test_tier_for(ram, vram, tier):
    assert mc.tier_for(ram, vram) == tier


def test_recommend_weak_cpu_server():
    rec = mc.recommend(ram_gb=8, vram_gb=0, arm=False)
    assert rec["tier"] == "T0"
    assert rec["triage"]["key"] == "qwen3.5-2b"
    assert rec["triage"]["min_vram_gb"] == 0
    assert rec["triage"]["expected_sec_per_ad"] > 0
    assert rec["triage"]["expected_ads_per_hour"] > 0
    assert rec["embed"]["key"] == "qwen3-embedding-0.6b"
    assert rec["second_opinion"] is None  # no local second opinion without a GPU
    assert rec["notes_ru"]
    # triage + embeddings + the app fit in RAM together
    assert rec["triage"]["min_ram_gb"] + rec["embed"]["min_ram_gb"] + mc.APP_RAM_GB <= 8


def test_recommend_tiny_ram_falls_back_to_smaller_models():
    four = mc.recommend(ram_gb=4, vram_gb=0)
    assert four["tier"] == "T0"
    assert four["triage"]["key"] == "qwen3.5-2b"
    assert four["triage"]["min_ram_gb"] + four["embed"]["min_ram_gb"] + mc.APP_RAM_GB <= 4
    assert four["vision"]["key"] == "qwen3.5-0.8b-vision"  # 2B + projector does not fit next to the embedder
    tight = mc.recommend(ram_gb=3.6, vram_gb=0)
    assert tight["triage"]["key"] == "qwen3.5-2b"
    assert tight["embed"]["key"] == "embeddinggemma-300m"  # the embedding model steps down first
    nothing = mc.recommend(ram_gb=3, vram_gb=0)  # no triage model under 2B is good enough
    assert nothing["triage"] is None and nothing["notes_ru"]
    assert nothing["embed"]["key"] == "qwen3-embedding-0.6b"
    assert mc.recommend(ram_gb=1.8, vram_gb=0)["embed"]["key"] == "embeddinggemma-300m"


def test_recommend_cpu_t1_and_gpu_tiers():
    t1 = mc.recommend(ram_gb=32, vram_gb=0)
    assert t1["tier"] == "T1" and t1["triage"]["key"] == "qwen3.5-4b"
    assert t1["vision"]["vision"] is True
    t2 = mc.recommend(ram_gb=32, vram_gb=12)
    assert t2["tier"] == "T2" and t2["triage"]["key"] == "qwen3.5-9b"
    assert t2["vision"]["min_vram_gb"] <= 12
    t3 = mc.recommend(ram_gb=64, vram_gb=24)
    assert t3["tier"] == "T3"
    assert t3["second_opinion"]["key"] == "qwen3.8-27b"
    assert all(t3[t] is None or t3[t]["min_vram_gb"] <= 24 for t in mc.TASKS if t != "embed")


def test_recommend_small_gpu_uses_fallback():
    rec = mc.recommend(ram_gb=16, vram_gb=6)  # 6 GB card: 9B does not fit, 4B does
    assert rec["tier"] == "T2"
    assert rec["triage"]["key"] == "qwen3.5-4b"


def test_recommend_arm_board():
    rec = mc.recommend(ram_gb=8, vram_gb=0, arm=True)
    assert rec["triage"]["arm_ok"] is True
    assert any("ARM" in note for note in rec["notes_ru"])


def test_recommend_handles_garbage_input():
    rec = mc.recommend(ram_gb=None, vram_gb=None)  # type: ignore[arg-type]
    assert rec["tier"] == "T0"
    assert mc.recommend(-5, -1)["tier"] == "T0"


def test_recommend_split_server_and_pc():
    rec = mc.recommend_split(server_ram_gb=8, pc_vram_gb=12)
    assert rec["triage"]["key"] == "qwen3.5-2b"  # always-on server reads every ad
    assert rec["embed"]["key"] == "qwen3-embedding-0.6b"  # embeddings never move to the PC
    assert rec["vision"]["key"] == "qwen3.5-9b-vision"  # photos go to the GPU when it is on
    assert rec["vision_fallback"]["key"] == "qwen3.5-2b-vision"  # ... and to the server when it is off
    assert rec["second_opinion"] is not None


@pytest.mark.parametrize(
    "reported, key",
    [("qwen/qwen3.5-2b", "qwen3.5-2b"), ("qwen3.5:2b-q4_K_M", "qwen3.5-2b"),
     ("Qwen3.5-2B-Q4_K_M.gguf", "qwen3.5-2b"), ("qwen3.5:9b", "qwen3.5-9b"),
     ("qwen3.5:0.8b", "qwen3.5-0.8b-vision"),
     ("qwen3-embedding:0.6b", "qwen3-embedding-0.6b"), ("qwen3.8:27b", "qwen3.8-27b"),
     ("text-embedding-qwen3-embedding-0.6b", "qwen3-embedding-0.6b"), ("llama3.2:3b", None)],
)
def test_find_catalog_model(reported, key):
    found = mc.find_catalog_model(reported)
    assert (found.key if found else None) == key


def test_best_installed_prefers_stronger_model_that_fits():
    available = ["qwen/qwen3.5-2b", "qwen/qwen3.5-4b", "text-embedding-nomic-embed-text-v1.5"]
    assert mc.best_installed(available, "triage") == "qwen/qwen3.5-4b"
    assert mc.best_installed(available, "triage", ram_gb=8) == "qwen/qwen3.5-2b"  # T0: 4B fits but is too slow
    assert mc.best_installed(available, "triage", ram_gb=32) == "qwen/qwen3.5-4b"
    assert mc.best_installed(available, "vision") == "qwen/qwen3.5-4b"
    assert mc.best_installed(["llama3.2:3b"], "triage") is None
    assert mc.best_installed(["qwen3.5:0.8b"], "triage") is None  # too weak to read ads
    assert mc.best_installed(["qwen3.5:0.8b"], "vision") == "qwen3.5:0.8b"
    assert mc.best_installed(["qwen3-embedding:0.6b"], "embed") == "qwen3-embedding:0.6b"


def test_module_is_pure_data():
    # importing twice gives the same objects and nothing is mutated by recommend()
    before = {k: m.as_dict() for k, m in mc.MODELS.items()}
    mc.recommend(8, 0)
    mc.recommend_split(8, 24)
    assert before == {k: m.as_dict() for k, m in mc.MODELS.items()}
