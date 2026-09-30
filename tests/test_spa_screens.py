"""Static checks for the SPA screens Лента / Сделка / Мои сделки / Поиски / Состояние
(ebeyparser/web/app/js/screens + js/features): every screen module has a default export,
every icon it names exists, no CLI / config-file advice leaks into the UI, colours come from
design tokens; plus the pure deal helpers (decision, offer rounding, German message) run
under Node when it is installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ebeyparser.web.spa import SPA_DIR

JS = SPA_DIR / "js"
SCREENS = ["feed", "deal", "pipeline", "searches", "health"]
FILES = sorted(
    [JS / "screens" / f"{name}.js" for name in SCREENS]
    + [p for sub in ("feed", "searches", "health") for p in (JS / "screens" / sub).glob("*.js")]
    + list((JS / "features").glob("*.js"))
)
ICON_RE = re.compile(r"""(?:\bname|\bicon|iconRight)(?:=|:\s?)\$?\{?"([a-z0-9-]+)\"""")
ICON_DEF_RE = re.compile(r'^\s+"([a-z0-9-]+)":', re.M)


def _icons() -> set[str]:
    names = set(ICON_DEF_RE.findall((JS / "ui" / "icons.js").read_text(encoding="utf-8")))
    # additive registrations: Frontend B's extras, «Сборки» (features/projects-common.js), the AI scout (features/scout.js)
    for extra in ("icons-extra.js", "projects-common.js", "scout.js"):
        names |= set(ICON_DEF_RE.findall((JS / "features" / extra).read_text(encoding="utf-8")))
    return names


def test_files_found() -> None:
    assert len(FILES) >= 15


@pytest.mark.parametrize("name", SCREENS)
def test_screen_modules_have_default_export(name: str) -> None:
    text = (JS / "screens" / f"{name}.js").read_text(encoding="utf-8")
    assert re.search(r"^export default function \w+\(", text, re.M), name
    assert "ScreenPlaceholder" not in text


def test_every_named_icon_exists() -> None:
    known = _icons()
    missing: dict[str, list[str]] = {}
    for path in FILES:
        for icon in ICON_RE.findall(path.read_text(encoding="utf-8")):
            if icon not in known:
                missing.setdefault(path.name, []).append(icon)
    assert not missing, f"icons missing from ui/icons.js and features/icons-extra.js: {missing}"


@pytest.mark.parametrize("pattern", [r"python -m", r"config\.yaml", r"\.env\b", r"ebeyparser (run|setup|check)"])
def test_no_cli_advice_in_ui(pattern: str) -> None:
    for path in FILES:
        assert not re.search(pattern, path.read_text(encoding="utf-8")), f"{path.name} mentions {pattern}"


def test_screen_styles_use_tokens() -> None:
    css = (SPA_DIR / "css" / "screens.css").read_text(encoding="utf-8")
    # the photo lightbox is always dark and slider thumbs are white in both themes (brief §6.8.4)
    hexes = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", css)}
    assert hexes <= {"#fff"}, hexes


NODE_SCRIPT = r"""
const dm = await import(process.argv[2] + "/features/deal-model.js");
const m = await import(process.argv[2] + "/features/messages.js");
const haggle = { action: "haggle", price: 360, offer_price: 293, max_buy_price: 330, profit: 35, profit_at_offer: 95,
                 negotiable: true, title: "iPhone 13 128GB Mitternacht, Top Zustand mit OVP!!!", score: 74 };
const out = {
  haggle: dm.decide(haggle),
  bid: dm.decide({ action: "bid", price: 180, max_buy_price: 260, score: 81,
                   auction: { bid_count: 5, ends_at: new Date(Date.now() + 3 * 3600e3).toISOString() } }),
  personal: dm.decide({ purpose: "personal", action: "buy", price: 470, profit: 80, market_price: 550 }),
  offers: [dm.humanOffer(293), dm.humanOffer(87), dm.humanOffer(1234)],
  template: m.defaultTemplate(haggle),
  message: m.quickMessage(haggle),
  title: m.shortTitle({ ai: { product: "NVIDIA GeForce RTX 3090 24GB (Gigabyte Gaming OC)" } }),
  flags: dm.explainFlags({ red_flags: ["Nur Tausch", "Vorkasse gewünscht"] }).map((f) => [f.text, f.scam]),
  type: dm.productType({ title: "Gigabyte RTX 3090 Gaming OC" }),
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_deal_helpers_under_node(tmp_path: Path) -> None:
    script = tmp_path / "check.mjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    run = subprocess.run(["node", str(script), JS.as_posix()], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["haggle"]["verb"] == "Торгуйся" and out["haggle"]["tone"] == "haggle"
    assert "Предложи 290" in out["haggle"]["pill"] and out["haggle"]["badge"] == "Торг · 74"
    assert out["bid"]["title"].startswith("Аукцион: ставь максимум 260")
    assert out["personal"]["kind"] == "personal" and "Экономия 80" in out["personal"]["pill"]
    assert out["offers"] == [290, 85, 1230]
    assert out["template"] == "offer"
    assert "Ich würde 290 € bieten" in out["message"] and out["message"].endswith("Viele Grüße")
    assert "iCloud" in out["message"]  # phone add-on
    assert out["title"] == "NVIDIA GeForce RTX 3090 24GB"
    assert out["flags"][0] == ["Vorkasse gewünscht", True]
    assert out["type"] == "gpu"
