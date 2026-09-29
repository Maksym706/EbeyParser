"""SPA «Сборки» (docs/design/PROJECTS.md): routes and navigation (sidebar item, no 6th phone tab),
deep links, the stylesheet, the live event, the API paths the screens call, copy rules, and the
pure helpers (server-text clean-up, diff of chosen variants) under Node when it is installed."""

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
from ebeyparser.web.api.events import EVENT_TYPES
from ebeyparser.web.app import create_app
from ebeyparser.web.spa import SPA_DIR

LOCAL = "http://127.0.0.1:8000"
JS = SPA_DIR / "js"
FILES = sorted([*(JS / "screens" / "projects").glob("*.js"), *(JS / "features").glob("projects-*.js")])


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture
def client() -> TestClient:
    app = create_app(AppConfig(), Database())
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def test_files_found() -> None:
    names = {p.name for p in FILES}
    assert {"index.js", "list.js", "create.js", "project.js"} <= names
    assert {"projects-common.js", "projects-plan.js", "projects-track.js", "projects-live.js"} <= names


@pytest.mark.parametrize("path", ["/projects", "/projects/new", "/projects/new?template=llm_48", "/projects/7",
                                  "/projects/7/slot/gpu"])
def test_deep_links_return_the_spa(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, path
    assert '<div id="app">' in r.text


def test_route_and_navigation() -> None:
    routes = _read(JS / "routes.js")
    assert re.search(r'path: "/projects/\*", nav: "projects", title: "Сборки"', routes)
    assert 'import("./screens/projects/index.js")' in routes
    item = re.search(r'\{ id: "projects",[^}]*\}', routes)
    assert item and 'icon: "boxes"' in item.group(0) and "tab: false" in item.group(0) and 'parent: "deals"' in item.group(0)
    # between «Мои сделки» and «Поиски»
    order = re.findall(r'\{ id: "(\w+)"', routes)
    assert order.index("projects") == order.index("deals") + 1 == order.index("searches") - 1
    shell = _read(JS / "shell" / "shell.js")
    assert "item.tab !== false" in shell, "the phone tab bar keeps 5 destinations"
    assert "startProjectsLive()" in shell
    # phone: «Сделки · Сборки» switch inside «Мои сделки»
    assert "DealsSwitch" in _read(JS / "screens" / "pipeline.js")
    assert re.search(r"^export default function \w+\(", _read(JS / "screens" / "projects" / "index.js"), re.M)


def test_every_server_event_has_a_listener() -> None:
    known = re.search(r"const KNOWN = \[(.*?)\];", _read(JS / "lib" / "events.js"), re.S)
    assert known
    names = set(re.findall(r'"([a-z_]+)"', known.group(1)))
    assert set(EVENT_TYPES) <= names, set(EVENT_TYPES) - names
    assert "project_updated" in names


def test_stylesheet_linked_served_and_tokens_only(client: TestClient) -> None:
    index = _read(SPA_DIR / "index.html")
    assert '<link rel="stylesheet" href="/app/css/projects.css" />' in index
    assert index.index("screens.css") < index.index("projects.css")  # shares the .tone-* / .tag helpers
    r = client.get("/app/css/projects.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    css = _read(SPA_DIR / "css" / "projects.css")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), "colours come from tokens.css"
    assert not re.search(r"rgba?\(", css)
    assert "@media (pointer: coarse)" in css and "@media (prefers-reduced-motion: reduce)" in css


def test_api_paths_exist(client: TestClient) -> None:
    common = _read(JS / "features" / "projects-common.js")
    used = set(re.findall(r'api\.(get|post|patch|del)\(`?"?(/projects[^`"]*)', common))
    assert len(used) >= 9
    schema = client.get("/api/openapi.json").json()
    paths = {(m.upper(), path) for path, ops in schema["paths"].items() for m in ops}
    method = {"get": "GET", "post": "POST", "patch": "PATCH", "del": "DELETE"}
    for verb, raw in used:
        # `/projects/${encodeURIComponent(id)}/slots/${…}/bought` → /api/v1/projects/{…}/slots/{…}/bought
        shape = "/api/v1" + re.sub(r"\$\{[^}]*\}", "{}", raw)
        hit = any(m == method[verb] and re.sub(r"\{[^}]*\}", "{}", p) == shape for m, p in paths)
        assert hit, f"{method[verb]} {shape} is not an API route"


@pytest.mark.parametrize("pattern", [r"python -m", r"config\.ya?ml", r"\.env\b", r"localhost", r"\bLLM\b(?!-)"])
def test_no_cli_or_tech_words_in_projects_ui(pattern: str) -> None:
    for path in FILES:
        text = _read(path)
        # only look at user-facing strings (template / quoted text), not at comments
        strings = " ".join(re.findall(r'"[^"\n]*"|`[^`]*`', re.sub(r"^\s*//.*$", "", text, flags=re.M)))
        assert not re.search(pattern, strings), f"{path.name} mentions {pattern}"


def test_vocabulary() -> None:
    """Spec §2: сборка / часть / вариант / цель / проверка — never «проект», «слот», «таргет», «оффер»."""
    for path in FILES:
        strings = " ".join(re.findall(r'"[^"\n]*"|`[^`]*`', re.sub(r"^\s*//.*$", "", _read(path), flags=re.M)))
        for word in ("проект", "слот", "таргет", "оффер", "билд", "валидац", "мониторинг"):
            assert word not in strings.lower(), f"{path.name}: «{word}»"


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
globalThis.window = { location: { href: "http://x/projects", origin: "http://x", pathname: "/projects", search: "", hash: "" },
                      matchMedia: () => ({ matches: false, addEventListener: noop }), addEventListener: noop };
globalThis.document = { addEventListener: noop, querySelector: () => null, createElement: () => ({}),
                        documentElement: { style: {}, dataset: {}, classList: { toggle: noop, add: noop } } };
globalThis.sessionStorage = { getItem: () => null, setItem: noop };
globalThis.localStorage = { getItem: () => null, setItem: noop };
globalThis.history = { replaceState: noop, pushState: noop, state: null };
const c = await import(process.argv[2] + "/features/projects-common.js");
const live = await import(process.argv[2] + "/features/projects-live.js");
const before = { slots: [{ key: "gpu", label: "Видеокарты", chosen: "mi50" }, { key: "psu", label: "Блок питания", chosen: "psu_1000" },
                         { key: "case", label: "Корпус", chosen: "atx" }] };
const after = { slots: [{ key: "gpu", label: "Видеокарты", chosen: "rtx3090" },
                        { key: "psu", label: "Блок питания", chosen: "psu_1300", chosen_label: "Блок питания 1300 Вт (80+ Gold)" },
                        { key: "case", label: "Корпус", chosen: "big", chosen_label: "Большой корпус: 8+ слотов (Define 7 XL)" }] };
const diff = c.diffChosen(before, after, ["gpu"]);
console.log(JSON.stringify({
  ru: [c.ru("~1.065 € из 1.500 €"), c.ru("📦 Для сборки «X»: MI50 за 175 €"), c.ru("4,85 бита · Llama 3.3 70B"),
       c.ru("пауза до 2026-09-29T19:34:42+00:00"), c.ru(null)],
  diff: diff.map((d) => [d.key, d.from, d.to]),
  side: c.sideChangesText(diff),
  lower: [c.lower("Видеокарты"), c.lower("SSD")],
  badge: live.badgeFrom([{ status: "tracking", headline_ru: "Ниже цели: MI50 за 175 €" }, { status: "tracking", headline_ru: "" },
                         { status: "paused", headline_ru: "Ниже цели" }, { status: "draft" }]),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_projects_helpers_under_node(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(NODE_HOOKS.replace("VENDOR_URI", (SPA_DIR / "vendor").as_uri()), encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    run = subprocess.run(["node", str(script), JS.as_posix(), hooks.as_uri()], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    nb = " "
    assert out["ru"][0] == f"~1{nb}065 € из 1{nb}500 €"  # German «1.065 €» → the UI's «1 065 €»
    assert out["ru"][1] == "Для сборки «X»: MI50 за 175 €"  # no Telegram emoji in the UI
    assert out["ru"][2] == "4,85 бита · Llama 3.3 70B"  # decimals and model names untouched
    assert "2026-09-29T" not in out["ru"][3] and out["ru"][4] == ""
    assert out["diff"] == [["psu", "psu_1000", "psu_1300"], ["case", "atx", "big"]]
    assert out["side"] == "Заодно поменял: блок питания → 1300 Вт (80+ Gold); корпус → Большой корпус"
    assert out["lower"] == ["видеокарты", "SSD"]
    assert out["badge"] == 1  # only tracking builds with an offer below target
