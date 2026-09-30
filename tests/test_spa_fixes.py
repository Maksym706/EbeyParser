"""Regression checks for the SPA fixes from the UX report (P0/P1/P2): no CLI or config-file
advice anywhere in the UI code, no broken settings links, the wizard never replaces searches by
default, one decision logic everywhere (auctions are never «Покупай»), Berlin time and the
error sanitizer — the pure helpers run under Node when it is installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ebeyparser.web.spa import SPA_DIR

JS = SPA_DIR / "js"
ALL_JS = sorted(p for p in JS.rglob("*.js"))
# lib/api.js holds the sanitizer: it names these patterns on purpose, to replace them
UI_JS = [p for p in ALL_JS if p.name != "api.js"]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("pattern", [r"python -m", r"config\.yaml", r"\.env\b", r"ebeyparser (run|setup|check|debug)"])
def test_no_cli_or_config_advice_anywhere(pattern: str) -> None:
    hits = [p.relative_to(JS).as_posix() for p in UI_JS if re.search(pattern, _text(p))]
    assert not hits, f"{pattern} in {hits}"


def test_settings_links_point_to_real_sections() -> None:
    index = _text(JS / "screens" / "settings" / "index.js")
    keys = set(re.findall(r'\{ key: "([a-z]+)", label:', index))
    assert {"region", "money", "ai", "notifications", "ebay", "access", "data", "about"} <= keys
    bad = {}
    for path in ALL_JS:
        # hrefs only (routes.js imports the module "./screens/settings/index.js")
        for slug in re.findall(r"""(?<![./\w])/settings/([a-z]+)\b(?!\.js)""", _text(path)):
            if slug not in keys:
                bad.setdefault(path.relative_to(JS).as_posix(), []).append(slug)
    assert not bad, f"links to settings sections that do not exist: {bad}"
    # old links keep working
    assert re.search(r"search:\s*\"region\"", index)


def test_wizard_merges_by_default() -> None:
    draft = _text(JS / "screens" / "onboarding" / "draft.js")
    assert "replace: false," in draft and not re.search(r"^\s+replace: true,", draft, re.M)
    done = _text(JS / "screens" / "onboarding" / "done.js")
    assert "Заменить текущие поиски или добавить к ним?" in done
    assert "setupBody(draft, { replace" in done


def test_check_link_uses_the_shared_decision() -> None:
    text = _text(JS / "shell" / "checklink.js")
    assert "decisionOf" not in text
    assert "decide(" in text


def test_filter_chip_is_not_cryptic() -> None:
    assert "Без ⚠" not in _text(JS / "screens" / "feed" / "filters.js")


def test_number_inputs_never_clamp_silently() -> None:
    forms = _text(JS / "ui" / "forms.js")
    body = forms[forms.index("export function NumberInput") :]
    body = body[: body.index("\n}\n")]
    assert "rangeError(" in body
    assert "n = min" not in body and "n = max" not in body


def test_ids_are_unique_across_render_roots() -> None:
    html = _text(JS / "lib" / "html.js")
    # preact's useId restarts in every render() root (our Portals): the SPA uses its own
    assert re.search(r"^export function useId\(", html, re.M)
    assert not re.search(r"^\s+useId,\s*$", html, re.M)


NODE_HOOKS = r"""
const V = "VENDOR_URI/";
const MAP = { "preact": V + "preact.module.js", "preact/hooks": V + "preact-hooks.module.js", "htm": V + "htm.module.js" };
export async function resolve(spec, ctx, next) {
  if (MAP[spec]) return { url: MAP[spec], shortCircuit: true };
  return next(spec, ctx);
}
"""

NODE_SCRIPT = r"""
import { register } from "node:module";
register(process.argv[3], import.meta.url);
globalThis.window = { location: { href: "http://x/", origin: "http://x" }, matchMedia: () => ({ matches: false }) };
globalThis.sessionStorage = { getItem: () => null, setItem() {} };
globalThis.history = { replaceState() {}, state: null };
const base = process.argv[2];
const dm = await import(base + "/features/deal-model.js");
const f = await import(base + "/lib/format.js");
const { humanize } = await import(base + "/lib/api.js");
const ends = new Date(Date.now() + 2 * 3600e3).toISOString();
const now = new Date("2026-09-29T19:20:00Z");
const out = {
  // an auction with an empty / "buy" action is still a bid (P1-4)
  auctionEmpty: dm.decide({ action: "", verdict: "buy", price: 310, max_buy_price: 367, auction: { bid_count: 7, ends_at: ends } }),
  auctionBuy: dm.decide({ action: "buy", verdict: "buy", buying_options: ["AUCTION"], price: 310, max_buy_price: 367 }),
  derived: dm.decide({ action: "", verdict: "buy", price: 100, profit: 50 }).verb,
  free: dm.decide({ action: "buy", is_free: true, market_price: 70, score: 90 }).badge,
  skip: dm.decide({ action: "skip", score: 20 }).badge,
  cond: [dm.ruCondition("Gut"), dm.ruCondition("In Ordnung"), dm.ruCondition("Gebraucht"), dm.ruCondition("Neu")],
  date: [dm.ruDateText("Verkauft 15.09.2026", now), dm.ruDateText("Heute, 19:35", now), dm.ruDateText("Gestern", now)],
  attr: [dm.ruAttrKey("Zustand"), dm.ruAttrValue("Zustand", "Gut"), dm.ruTag("Versand möglich")],
  num: dm.ruNumbers("вкл. доставку 6.99 €"),
  time: [f.clockTime("2026-09-29T19:34:42.416009+00:00"), f.localizeText("пауза до 2026-09-29T19:34:42+00:00", now), f.untilTime("2026-09-30T01:10:00Z", now), f.berlinDay(new Date("2026-09-29T23:30:00Z"))],
  errors: [
    humanize("Для поиска по eBay заполни ebay.client_id и ebay.client_secret в config.yaml/.env"),
    humanize("Локальная модель недоступна по адресу http://localhost:1234/v1 (ConnectError)"),
    humanize("Нет связи с Telegram (ConnectError) — проверь интернет"),
    humanize("Kleinanzeigen поменял вёрстку. Запусти `python -m ebeyparser debug-search` и пришли вывод."),
    humanize("Поиск сохранён"),
  ],
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_fixed_helpers_under_node(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(NODE_HOOKS.replace("VENDOR_URI", (SPA_DIR / "vendor").as_uri()), encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    run = subprocess.run(["node", str(script), JS.as_posix(), hooks.as_uri()], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    for key in ("auctionEmpty", "auctionBuy"):
        d = out[key]
        assert d["kind"] == "bid" and d["verb"] == "Аукцион", d
        assert d["title"].startswith("Аукцион: ставь максимум 367"), d
        assert "Покупай" not in json.dumps(d, ensure_ascii=False)
    assert out["auctionEmpty"]["pill"].startswith("Ставь максимум 367")
    assert out["derived"] == "Покупай"
    assert out["free"] == "Забирай · 90" and out["skip"] == "Не выгодно · 20"
    assert out["cond"] == ["Хорошее", "Нормальное", "Б/у", "Новое"]
    assert out["date"] == ["15.09", "сегодня в 19:35", "вчера"]
    assert out["attr"] == ["Состояние", "Хорошее", "Доставка возможна"]
    assert out["num"] == "вкл. доставку 6,99 €"
    assert out["time"] == ["21:34", "пауза до 21:34", "завтра 03:10", "2026-09-30"]
    messages = [e["message"] for e in out["errors"]]
    for text in messages:
        assert not re.search(r"python -m|config\.yaml|\.env|ConnectError|localhost", text), text
    assert "eBay не подключён" in messages[0] and out["errors"][0]["details"]
    assert "LM Studio" in messages[1]
    assert messages[2] == "Нет связи с Telegram — проверь интернет"
    assert messages[3] == "Kleinanzeigen поменял вёрстку."
    assert out["errors"][4] == {"message": "Поиск сохранён", "details": ""}
