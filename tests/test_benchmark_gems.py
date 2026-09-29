"""Hidden-gems benchmark (script-only vs the AI scout) on a small world: the scout finds more of
the gems, precision stays 100 %, no trap is bought, even with a weak model on a CPU that
breaks JSON and can't read every ad. Deterministic."""

from __future__ import annotations

import json

from ebeyparser.ai.triage import parse_triage
from ebeyparser.benchmark_gems import (
    GEM_FLAVOURS,
    GEM_TRAPS,
    SimClock,
    SimTriageLLM,
    build_gem_market,
    format_comparison,
    run_gems,
)
from ebeyparser.ai.prompts_triage import build_user_prompt

SEED, N = 3, 120


def test_market_has_every_gem_and_trap_kind():
    gm = build_gem_market(SEED, N)
    flavours = {gm.market.by_id[a].truth.flavour for a in gm.gems}
    traps = {gm.market.by_id[a].truth.flavour for a in gm.gem_traps}
    assert flavours == set(GEM_FLAVOURS) and traps == set(GEM_TRAPS)
    assert all(a in gm.market.by_id for a in gm.gems + gm.gem_traps)


async def test_sim_llm_answers_json_text_with_indexes():
    gm = build_gem_market(SEED, N)
    clock = SimClock()
    llm = SimTriageLLM(gm, "oracle", seed=SEED, clock=clock, hardware="cpu_3b")
    ads = [gm.market.by_id[a].card(1) for a in gm.gems[:5]]
    answer = await llm.chat_json("system", build_user_prompt(ads), None, None)
    parsed = parse_triage(answer, len(ads))
    assert set(parsed) == set(range(len(ads))) and json.loads(answer)["items"][0]["i"] == 0
    assert clock() > 1_800_000_000.0  # the call cost simulated CPU time


def test_scout_finds_more_gems_without_losing_precision():
    rows = [run_gems(SEED, N, "oracle", scout=False), run_gems(SEED, N, "oracle", scout=True),
            run_gems(SEED, N, "weak", scout=True)]
    script, oracle, weak = rows
    for r in rows:
        assert r.precision == 1.0 and r.trap_buys == 0 and r.severe_trap_buys == 0, r.false_buys
    assert oracle.gem_buys > script.gem_buys and oracle.found_by_scout_buys > 0
    assert weak.gem_buys > script.gem_buys
    # the weak model on a CPU: broken answers happen and not every ad is read — nothing breaks
    assert weak.scout_broken_answers > 0 and weak.scout_overflow > 0 and weak.scout_read > 0
    assert oracle.scout_overflow == 0 and script.scout_read == 0
    text = format_comparison(rows)
    assert "Скрытые находки" in text and "cpu_3b" in text


def test_deterministic():
    a = run_gems(SEED, 80, "noisy", scout=True)
    b = run_gems(SEED, 80, "noisy", scout=True)
    assert (a.buys, a.correct_buys, a.by_flavour, a.scout_read) == (b.buys, b.correct_buys, b.by_flavour, b.scout_read)
