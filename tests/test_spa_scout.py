"""SPA AI scout (docs/design/AI_SCOUT.md §12) and «Какую модель поставить» (GET /ai/recommend):
the stylesheet, the deal marks («Нашла нейросеть», «Супер-находка»), the feed «Супер» chip, the
settings paths the forms write, the API routes the screens call, copy rules, no stale model names,
and the pure helpers (speed line, deal flags) under Node when it is installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ebeyparser.config import AppConfig
from ebeyparser.db import Database
from ebeyparser.web.app import create_app
from ebeyparser.web.spa import SPA_DIR

LOCAL = "http://127.0.0.1:8000"
JS = SPA_DIR / "js"
SCOUT = JS / "features" / "scout.js"
SETTINGS = JS / "screens" / "settings" / "scout.js"
FILES = [SCOUT, SETTINGS]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strings(path: Path) -> str:
    """User-facing strings only (quoted / template text), not comments."""
    text = re.sub(r"^\s*(//|\*|/\*\*).*$", "", _read(path), flags=re.M)
    return " ".join(re.findall(r'"[^"\n]*"|`[^`]*`', text))


@pytest.fixture
def client() -> TestClient:
    app = create_app(AppConfig(), Database())
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def test_files_and_wiring() -> None:
    for path in FILES:
        assert path.exists(), path
    card, view = _read(JS / "features" / "deal-card.js"), _read(JS / "features" / "deal-view.js")
    assert "FoundByBadge" in card and "SuperMark" in card and "is-super" in card
    assert "FoundByBadge" in view and "SuperMark" in view and "ScoutNote" in view
    scout = _read(SCOUT)
    assert 'deal.found_by === "ai_scout"' in scout and 'deal.tier === "super"' in scout
    assert "found_by_label" in scout and "scout_reason" in scout and "tier_label" in scout
    # Settings → Нейросеть: «Разведчик» + «Какую модель поставить»; Уведомления: super / daily top
    a, b = _read(JS / "screens" / "settings" / "sections-a.js"), _read(JS / "screens" / "settings" / "sections-b.js")
    assert "<${ScoutGroup}" in a and "<${ModelsGroup}" in a
    assert "<${AlertTiersGroups}" in b
    # Состояние tile, onboarding AI step
    health = _read(JS / "screens" / "health.js")
    assert "ScoutTile" in health and "too_small" in health and "speed_expected" in health
    assert "<${ModelAdvice}" in _read(JS / "screens" / "onboarding" / "steps-connect.js")


def test_feed_super_chip() -> None:
    filters = _read(JS / "screens" / "feed" / "filters.js")
    assert re.search(r'key: "sup", label: "Супер"', filters)
    assert 'p.tier = "super"' in filters and 'p.verdict = "buy"' not in filters  # the server's tier filter
    assert "fc.tier && fc.tier.super" in filters  # the chip's exact count (facets)
    feed = _read(JS / "screens" / "feed.js")
    assert "moreSuper" not in feed and "feed.items.length < 300" not in feed  # no client-side paging for it
    assert 'd.tier === "super"' in feed  # only drops a card that stopped being super live


def test_stylesheet_linked_served_and_tokens_only(client: TestClient) -> None:
    index = _read(SPA_DIR / "index.html")
    assert '<link rel="stylesheet" href="/app/css/scout.css" />' in index
    assert index.index("screens.css") < index.index("scout.css")  # shares .tone-* / .tag / .htile
    r = client.get("/app/css/scout.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    css = _read(SPA_DIR / "css" / "scout.css")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), "colours come from tokens.css"
    assert not re.search(r"rgba?\(", css)
    assert "@media (pointer: coarse)" in css


def test_settings_paths_exist(client: TestClient) -> None:
    settings = client.get("/api/v1/settings").json()
    used = set(re.findall(r'form\.(?:bind|set|err)\("([a-z_.]+)"', _read(SETTINGS)))
    assert {"ai.scout.enabled", "ai.scout.mode", "ai.scout.model", "ai.scout.base_url", "ai.scout.batch_size",
            "ai.scout.min_interest", "ai.vision_wait_minutes", "notifications.super_deals.min_profit",
            "notifications.super_deals.min_roi", "notifications.super_deals.min_score",
            "notifications.daily_top.hour", "notifications.daily_top.per_search"} <= used
    for path in used:
        node = settings
        for key in path.split("."):
            assert isinstance(node, dict) and key in node, f"{path} is not a setting"
            node = node[key]
    # «Использовать» writes these groups (features/scout.js USE_PATH)
    use = re.search(r"const USE_PATH = \{([^}]*)\}", _read(SCOUT))
    assert use
    for path in re.findall(r':\s*"([a-z_.]+)"', use.group(1)):
        node = settings
        for key in path.split("."):
            node = node[key]
        assert {"provider", "base_url", "model", "enabled"} <= set(node), path


def test_api_paths_exist(client: TestClient) -> None:
    used = set()
    for path in (SCOUT, SETTINGS, JS / "screens" / "health.js"):
        used |= set(re.findall(r'api\.(get|post|patch)\("(/(?:ai|settings)[^"]*)"', _read(path)))
    assert {("post", "/ai/scout/test"), ("get", "/ai/recommend"), ("patch", "/settings")} <= used
    schema = client.get("/api/openapi.json").json()
    paths = {(m.upper(), path) for path, ops in schema["paths"].items() for m in ops}
    for verb, raw in used:
        assert (verb.upper(), "/api/v1" + raw) in paths, f"{verb.upper()} {raw} is not an API route"


def test_no_stale_model_names() -> None:
    """Qwen2.5-VL-7B was the old default; the recommendation now comes from /ai/recommend or gpu_presets."""
    for path in JS.rglob("*.js"):
        text = _read(path)
        assert not re.search(r"qwen2\.5", text, re.I), f"{path.relative_to(JS)} names Qwen2.5"


@pytest.mark.parametrize("pattern", [r"python -m", r"config\.ya?ml", r"\.env\b", r"localhost", r"ollama (pull|run)",
                                     r"\bpip\b", r"\bcurl\b", r"\bsudo\b"])
def test_no_cli_in_scout_ui(pattern: str) -> None:
    for path in FILES:
        assert not re.search(pattern, _strings(path)), f"{path.name} mentions {pattern}"


def test_zero_means_auto() -> None:
    """batch_size 0 = «авто по модели», min_interest 0 = «не отсеивать» (the field says so next to the 0)."""
    text = _read(SETTINGS)
    assert "авто по модели" in text and "не отсеивать" in text


NODE_HOOKS = r"""
const V = "VENDOR_URI/";
const MAP = { "preact": V + "preact.module.js", "preact/hooks": V + "preact-hooks.module.js", "htm": V + "htm.module.js", "uqr": V + "uqr.module.js" };
export async function resolve(spec, ctx, next) {
  if (MAP[spec]) return { url: MAP[spec], shortCircuit: true };
  return next(spec, ctx);
}
"""

NODE_SCRIPT = r"""
import { register } from "node:module";
register(process.argv[3], import.meta.url);
const noop = () => {};
globalThis.window = { location: { href: "http://x/", origin: "http://x", pathname: "/", search: "", hash: "" },
                      matchMedia: () => ({ matches: false, addEventListener: noop }), addEventListener: noop };
globalThis.document = { addEventListener: noop, querySelector: () => null, createElement: () => ({}),
                        documentElement: { style: {}, dataset: {}, classList: { toggle: noop, add: noop } } };
globalThis.sessionStorage = { getItem: () => null, setItem: noop };
globalThis.localStorage = { getItem: () => null, setItem: noop };
globalThis.history = { replaceState: noop, pushState: noop, state: null };
const s = await import(process.argv[2] + "/features/scout.js");
const { ICONS } = await import(process.argv[2] + "/ui/icons.js");
console.log(JSON.stringify({
  speed: [
    s.scoutSpeed({ speed_ru: "≈ 5,2 с на объявление, до 340 объявлений в час" }),
    s.scoutSpeed({ speed_ru: "≈ 6,0 с на объявление, до 300 объявлений в час", speed_expected: true }),
    s.scoutSpeed({ speed_ru: "" }), s.scoutSpeed(null),
  ],
  flags: [s.foundByScout({ found_by: "ai_scout" }), s.foundByScout({ found_by: "rules" }), s.foundByScout(null),
          s.isSuper({ tier: "super" }), s.isSuper({ tier: "normal" })],
  states: ["ok", "behind", "down", "too_small", "off", "idle", "weird"].map((st) => s.scoutState({ state: st, enabled: true }).tone),
  icons: ["telescope", "calendar-clock", "flame", "gauge", "cpu"].map((n) => n in ICONS),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_scout_helpers_under_node(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(NODE_HOOKS.replace("VENDOR_URI", (SPA_DIR / "vendor").as_uri()), encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    run = subprocess.run(["node", str(script), JS.as_posix(), hooks.as_uri()], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    # the server words it (Russian decimal comma); an estimate is only flagged: «оценка» is a separate mark
    assert out["speed"][0] == "≈ 5,2 с на объявление, до 340 объявлений в час"
    assert out["speed"][1] == "≈ 6,0 с на объявление, до 300 объявлений в час"
    assert out["speed"][2:] == ["", ""]
    assert out["flags"] == [True, False, False, True, False]
    assert out["states"] == ["profit", "haggle", "danger", "haggle", "neutral", "neutral", "neutral"]
    assert all(out["icons"])
