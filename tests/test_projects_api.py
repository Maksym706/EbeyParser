"""/api/v1/projects («Сборки»): plan preview (rules and a mocked local LLM), CRUD, tracking
searches, purchases, offers, alerts from the monitor's events and the project_updated SSE event.
No network: the LLM answers through httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api_v1_helpers import LOCAL, clean_environ, events_of, make_app, restore_environ
from ebeyparser.models import Listing
from ebeyparser.web.api.events import EVENT_TYPES

GOAL = "Сервер для локальных LLM, чтобы тянул 70B в Q4, бюджет 1500 €"


@pytest.fixture(autouse=True)
def isolated_env():
    saved = clean_environ()
    yield
    restore_environ(saved)


class FakeNotifier:
    name = "telegram"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_text(self, text: str) -> None:
        self.texts.append(text)


@pytest.fixture()
def api(tmp_path: Path):
    app, config, db, path, monitor = make_app(tmp_path, seed=False)
    notifier = FakeNotifier()
    app.state.api.project_notifiers = [notifier]
    with TestClient(app, base_url=LOCAL) as client:
        yield client, app, config, db, monitor, notifier


def llm_transport(answer: Any, calls: list[dict], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append({"url": str(request.url), "body": body})
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "server exploded"}})
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


def create_project(c: TestClient, **extra: Any) -> dict[str, Any]:
    plan = c.post("/api/v1/projects/plan", json={"goal": GOAL, "use_ai": False}).json()
    r = c.post("/api/v1/projects", json={"plan": plan["plan"], **extra})
    assert r.status_code == 201, r.text
    return r.json()


def track(c: TestClient, pid: int, **body: Any) -> dict[str, Any]:
    r = c.post(f"/api/v1/projects/{pid}/track", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def linked(project: dict[str, Any], slot: str) -> list[str]:
    return [s["name"] for s in project["searches_list"] if s["slot"] == slot]


# ================================================================ templates / plan
def test_templates_list(api) -> None:
    c, *_ = api
    data = c.get("/api/v1/projects/templates").json()
    assert [t["key"] for t in data["items"]] == ["llm_24", "llm_48", "nas", "gaming_1080p", "custom"]
    assert data["items"][1]["title"] == "LLM-сервер 48 ГБ VRAM" and data["items"][0]["default_budget"]
    assert data["ai"]["available"] is False and "Настройки" in data["ai"]["label_ru"]
    assert {q["key"] for q in data["quants"]} >= {"q4_k_m", "q8_0"}


def test_plan_preview_rules_only(api) -> None:
    c, app, config, db, *_ = api
    r = c.post("/api/v1/projects/plan", json={"goal": GOAL})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["template"] == "llm_48" and p["budget"] == 1500 and p["kind"] == "llm"
    assert p["ai"]["used"] is False and "правилам" in p["ai"]["message_ru"]
    gpu = next(s for s in p["slots"] if s["key"] == "gpu")
    assert gpu["chosen"] == "mi50_32x2"
    options = {o["key"]: o for o in gpu["options"]}
    assert {"rtx3090x2", "tesla_p40x2", "mi50_32x2"} <= set(options)
    mi50 = options["mi50_32x2"]
    assert mi50["vram_gb"] == 64 and mi50["vram_status"] == "ok" and mi50["speed"]["label_ru"].startswith("≈")
    assert mi50["price"]["source"] == "kb" and mi50["price"]["rough"] and "ориентир" in mi50["price"]["label_ru"]
    assert mi50["build_total"] and mi50["fits_budget"] is True and mi50["caveats"]
    assert options["rtx3090x2"]["fits_budget"] is False and options["rtx3090x2"]["value_score"] is not None
    checks = {ch["key"]: ch for ch in p["checks"]}
    assert checks["vram"]["status"] == "ok" and any("млрд" in line for line in checks["vram"]["lines"])
    assert checks["budget"]["status"] == "ok" and p["checks_summary"]["status"] in ("ok", "warn")
    assert p["totals"]["typical_total"] < 1500 and p["totals"]["new_label_ru"].startswith("Новым")
    assert p["prices_rough"] and p["summary_ru"]
    assert db.count_price_points() == 0 and c.get("/api/v1/projects").json()["count"] == 0  # nothing saved


def test_plan_validation(api) -> None:
    c, *_ = api
    r = c.post("/api/v1/projects/plan", json={"goal": "", "use_ai": False})
    assert r.status_code == 422 and r.json()["error"]["fields"]["goal"]
    r = c.post("/api/v1/projects/plan", json={"template": "spaceship"})
    assert r.status_code == 422 and "шаблон" in r.json()["error"]["fields"]["template"]
    r = c.post("/api/v1/projects/plan", json={"goal": GOAL, "budget": -5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation" and "budget" in r.json()["error"]["fields"]
    r = c.post("/api/v1/projects/plan", json={"goal": GOAL, "evil": 1})
    assert r.status_code == 422


def test_plan_with_local_llm_mocked(api) -> None:
    c, app, config, *_ = api
    config.ai.enabled = True
    calls: list[dict] = []
    answer = {"template": "llm_48", "choices": {"gpu": "tesla_p40x2", "cpu": "made_up_cpu"},
              "reasons": {"gpu": "Проще всего: CUDA работает из коробки"},
              "extra_items": [{"label": "Монитор для настройки", "query": "monitor 22 zoll", "why": "для BIOS",
                               "price": 3}],
              "summary": "Две Tesla P40 — дёшево и надёжно.", "price_total": 1}
    app.state.api.http_transport = llm_transport(f"<think>...</think>```json\n{json.dumps(answer)}\n```", calls)
    r = c.post("/api/v1/projects/plan", json={"goal": GOAL})
    assert r.status_code == 200, r.text
    p = r.json()
    assert calls and calls[0]["url"].endswith("/v1/chat/completions")
    sent = calls[0]["body"]
    system, user = sent["messages"][0]["content"], sent["messages"][1]["content"]
    assert "Never write prices" in system and "tesla_p40x2" in user and "mi50_32x2" in user
    assert isinstance(user, str)  # text only, no photos
    assert p["ai"]["used"] and p["ai"]["ok"] and p["ai"]["applied"] == {"gpu": "tesla_p40x2"}
    assert "cpu=made_up_cpu" in p["ai"]["ignored"] and p["ai"]["summary_ru"].startswith("Две Tesla")
    gpu = next(s for s in p["slots"] if s["key"] == "gpu")
    assert gpu["chosen"] == "tesla_p40x2"
    chosen = next(o for o in gpu["options"] if o["chosen"])
    assert chosen["why"].startswith("Проще") and chosen["price"]["source"] == "kb"
    extra = next(s for s in p["slots"] if s["label"] == "Монитор для настройки")
    assert extra["options"][0]["source"] == "ai" and extra["options"][0]["price"]["typical"] is None
    assert extra["options"][0]["query"] == "monitor 22 zoll"


def test_plan_llm_failure_falls_back_to_rules(api) -> None:
    c, app, config, *_ = api
    config.ai.enabled = True
    calls: list[dict] = []
    app.state.api.http_transport = llm_transport({}, calls, status=500)
    p = c.post("/api/v1/projects/plan", json={"goal": GOAL}).json()
    assert calls and p["ai"]["used"] and p["ai"]["ok"] is False and "по правилам" in p["ai"]["message_ru"]
    assert next(s for s in p["slots"] if s["key"] == "gpu")["chosen"] == "mi50_32x2"
    app.state.api.http_transport = llm_transport("не JSON вообще", calls)
    p = c.post("/api/v1/projects/plan", json={"goal": GOAL}).json()
    assert p["ai"]["ok"] is False and "непонятно" in p["ai"]["message_ru"]


# ======================================================================= CRUD
def test_create_get_patch_flow(api) -> None:
    c, app, *_ = api
    project = create_project(c, name="Мой LLM-сервер", choices={"gpu": "rtx3090x2"})
    pid = project["id"]
    assert project["name"] == "Мой LLM-сервер" and project["status"] == "draft"
    assert project["status_label"] == "Черновик" and project["next_step"]["key"] == "track"
    slots = {s["key"]: s for s in project["slots"]}
    assert slots["gpu"]["chosen"] == "rtx3090x2" and slots["psu"]["chosen"] == "psu_1300"  # adjusted
    assert slots["case"]["chosen"] == "case_big" and not slots["gpu_cooling"]["active"]
    assert project["progress_label"] == "Собрано 0 из 7" and project["total_label"].endswith("1.500 €")
    budget = next(ch for ch in project["checks"] if ch["key"] == "budget")
    assert budget["status"] == "fail" and budget["fix"]["slot"] == "gpu"
    ev = [e for e in events_of(app, "project_updated") if e.data["reason"] == "created"]
    assert ev and ev[-1].data["id"] == pid and ev[-1].data["card"]["name"] == "Мой LLM-сервер"
    assert "project_updated" in EVENT_TYPES

    got = c.get(f"/api/v1/projects/{pid}").json()
    assert got["id"] == pid and got["searches_list"] == [] and got["alerts_list"] == []
    assert any(s["trend"]["label_ru"] for s in got["slots"] if s.get("trend"))

    r = c.patch(f"/api/v1/projects/{pid}", json={"choices": {"gpu": "tesla_p40x2"}, "budget": 1200,
                                                 "slots": {"storage": {"target_price": 40}, "cpu": {"note": "есть у друга"}}})
    assert r.status_code == 200, r.text
    p = r.json()
    slots = {s["key"]: s for s in p["slots"]}
    assert p["budget"] == 1200 and slots["gpu"]["chosen"] == "tesla_p40x2"
    assert slots["cpu"]["chosen"] == "r5_5600g" and slots["gpu_cooling"]["active"]  # needed for P40s now
    assert slots["psu"]["chosen"] == "psu_1300"  # still big enough: the user's choice is not downsized silently
    assert slots["storage"]["target_unit"] == 40 and slots["cpu"]["note"] == "есть у друга"
    storage_opt = next(o for o in slots["storage"]["options"] if o["chosen"])
    assert storage_opt["target_by"] == "user"
    r = c.patch(f"/api/v1/projects/{pid}", json={"slots": {"cooler": {"status": "have"}, "nope": {"status": "have"}}})
    assert r.status_code == 422 and "slots.nope" in r.json()["error"]["fields"]
    r = c.patch(f"/api/v1/projects/{pid}", json={"choices": {"gpu": "rtx9999"}})
    assert r.status_code == 422 and r.json()["error"]["fields"]["choices.gpu"] == "Нет такого варианта"
    r = c.patch(f"/api/v1/projects/{pid}", json={"status": "tracking"})
    assert r.status_code == 409
    r = c.patch(f"/api/v1/projects/{pid}", json={"add_items": [{"label": "Монитор", "query": "monitor 24 zoll",
                                                                "target_price": 60}]})
    assert r.status_code == 200 and any(s["label"] == "Монитор" for s in r.json()["slots"])
    assert c.get("/api/v1/projects/999").status_code == 404
    assert c.get("/api/v1/projects/999").json()["error"]["message_ru"].startswith("Сборка не найдена")
    listing = c.get("/api/v1/projects").json()
    assert listing["count"] == 1 and listing["items"][0]["id"] == pid


def test_create_from_goal_and_custom_list(api) -> None:
    c, *_ = api
    r = c.post("/api/v1/projects", json={"goal": "ThinkPad T480 до 200 €, монитор 27 дюймов — бюджет 400 €",
                                         "use_ai": False})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["template"] == "custom" and [s["label"] for s in p["slots"]] == ["ThinkPad T480", "Монитор 27 дюймов"]
    assert p["slots"][0]["target_unit"] == 200 and p["slots"][1]["options"][0]["query"] == "monitor 27 zoll"
    assert c.post("/api/v1/projects", json={"template": "nas", "use_ai": False}).status_code == 201


# =================================================================== tracking
def test_track_creates_personal_searches(api) -> None:
    c, app, config, db, *_ = api
    pid = create_project(c)["id"]
    dry = track(c, pid, dry_run=True)
    assert dry["dry_run"] and len(dry["created"]) == 10 and config.searches == []
    assert "поисков" in dry["message_ru"] and dry["estimate"]
    res = track(c, pid)
    assert res["message_ru"].startswith("Слежу за 10 поисками")
    project = res["project"]
    assert project["status"] == "tracking" and project["next_step"]["key"] in ("wait", "buy")
    names = {s.name: s for s in config.searches}
    gpu_names = linked(project, "gpu")
    assert len(gpu_names) == 3  # chosen MI50 + 2 alternatives (the key part)
    mi50 = names[next(n for n in gpu_names if "MI50" in n)]
    assert mi50.purpose == "personal" and mi50.query == "mi50 32gb" and mi50.target_price
    # no price range on the site: the price history keeps learning (the monitor cuts at 1.3× target itself)
    assert mi50.max_price is None and mi50.min_price is None and "defekt" in mi50.exclude_keywords
    gpu_slot = next(s for s in project["slots"] if s["key"] == "gpu")
    assert gpu_slot["max_unit"] >= gpu_slot["target_unit"] * 1.3
    rtx = names[next(n for n in gpu_names if "3090" in n)]
    assert "3090 ti" in rtx.exclude_keywords
    assert all(s.enabled for s in config.searches)
    assert len(linked(project, "cpu")) == 1  # no alternatives for the other parts by default
    assert any(e.data["reason"] == "tracking" for e in events_of(app, "project_updated"))
    assert events_of(app, "searches_changed")
    filtered = c.post(f"/api/v1/projects/{pid}/track", json={"dry_run": True, "price_filter": True}).json()
    assert filtered["searches"] == [] or all(x["max_price"] for x in filtered["searches"])
    # again: nothing duplicated
    again = track(c, pid)
    assert again["created"] == [] and len(config.searches) == 10
    # pause / resume switches the searches off and on
    r = c.patch(f"/api/v1/projects/{pid}", json={"status": "paused"})
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert not any(s.enabled for s in config.searches)
    r = c.patch(f"/api/v1/projects/{pid}", json={"status": "tracking"})
    assert r.json()["status"] == "tracking" and all(s.enabled for s in config.searches)


def test_track_needs_writable_settings(api) -> None:
    c, app, *_ = api
    pid = create_project(c)["id"]
    app.state.config_path = None
    r = c.post(f"/api/v1/projects/{pid}/track", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "read_only"
    assert c.post(f"/api/v1/projects/{pid}/track", json={"dry_run": True}).status_code == 200


def _add_ad(db, ad_id: str, title: str, price: float, search_name: str) -> None:
    db.upsert_listing(Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}", title=title,
                              price=price, search_name=search_name, distance_km=7.0, location="10115 Mitte"))


def test_deal_found_alert_bought_and_offers(api) -> None:
    c, app, config, db, monitor, notifier = api
    pid = create_project(c)["id"]
    project = track(c, pid)["project"]
    mi50_search = next(n for n in linked(project, "gpu") if "MI50" in n)
    p40_search = next(n for n in linked(project, "gpu") if "P40" in n)
    gpu_target = next(s for s in project["slots"] if s["key"] == "gpu")["target_unit"]
    _add_ad(db, "9001", "AMD Instinct MI50 32GB HBM2", gpu_target - 30, mi50_search)
    _add_ad(db, "9002", "AMD Instinct MI50 32GB", gpu_target + 60, mi50_search)
    _add_ad(db, "9003", "Nvidia Tesla P40 24GB", 240, p40_search)
    _add_ad(db, "9004", "Suche MI50 32GB", 20, mi50_search)
    monitor._emit("deal_found", {"ad_id": "9001", "search_name": mi50_search, "verdict": "buy", "action": "buy",
                                 "score": 80})
    assert len(notifier.texts) == 1
    text = notifier.texts[0]
    assert "MI50" in text and "Ниже твоей цели" in text and "https://www.kleinanzeigen.de/s-anzeige/x/9001" in text
    assert f"/projects/{pid}" in text
    alert_events = [e for e in events_of(app, "project_updated") if e.data["reason"] == "alert"]
    assert alert_events and alert_events[-1].data["alert"]["text_ru"].endswith("ниже цели")
    monitor._emit("deal_found", {"ad_id": "9001"})
    assert len(notifier.texts) == 1  # once per ad
    monitor._emit("run_finished", {})  # the sweep sends nothing new either
    assert len(notifier.texts) == 1

    view = c.get(f"/api/v1/projects/{pid}").json()
    gpu = next(s for s in view["slots"] if s["key"] == "gpu")
    assert gpu["best"]["ad_id"] == "9001" and gpu["best"]["under_target"] and "ниже цели" in gpu["best"]["vs_target_label"]
    assert [o["ad_id"] for o in gpu["picked"]] == ["9001", "9002"]  # two cards needed
    assert gpu["best_cost"] == gpu_target - 30 + gpu_target + 60
    alt = next(a for a in gpu["alternatives"] if a["option"] == "tesla_p40x2")
    assert alt["best"]["ad_id"] == "9003"
    assert view["alerts_list"][0]["kind"] == "slot_target" and view["alerts_list"][0]["delivered"]
    assert view["gpu_ranking"] and view["gpu_ranking"][0]["value_score"] is not None
    assert view["headline_ru"].startswith("Ниже цели")

    offers = c.get(f"/api/v1/projects/{pid}/offers", params={"slot": "gpu"}).json()
    assert offers["slots"][0]["offers"][0]["ad_id"] == "9001" and offers["count"] == 3
    assert "9004" not in json.dumps(offers)
    assert c.get(f"/api/v1/projects/{pid}/offers", params={"slot": "nope"}).status_code == 404

    # bought one card through the ad: the deal goes to «Купил», the GPU slot still needs one more
    r = c.post(f"/api/v1/projects/{pid}/slots/gpu/bought", json={"price": gpu_target - 30, "ad_id": "9001", "qty": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Записал" in body["message_ru"] and "Купил" in body["message_ru"]
    deal = c.get("/api/v1/deals/9001").json()
    assert deal["status"] == "bought" and deal["bought_price"] == gpu_target - 30
    gpu = next(s for s in body["project"]["slots"] if s["key"] == "gpu")
    assert gpu["need_qty"] == 1 and gpu["status"] == "open" and gpu["spent"] == gpu_target - 30
    assert body["project"]["spent"] == gpu_target - 30
    chosen_search = next(s["name"] for s in body["project"]["searches_list"] if s["option"] == "mi50_32x2")
    assert {s.name: s.enabled for s in config.searches}[chosen_search]  # the second card must match the first
    assert body["paused_searches"] and all("MI50" not in n for n in body["paused_searches"])
    # the second card: the slot is filled and its searches pause
    r = c.post(f"/api/v1/projects/{pid}/slots/gpu/bought", json={"price": 200})
    gpu = next(s for s in r.json()["project"]["slots"] if s["key"] == "gpu")
    assert gpu["status"] == "bought" and gpu["status_label"] == "Куплено"
    gpu_names = set(linked(r.json()["project"], "gpu"))
    assert r.json()["paused_searches"] == [chosen_search]  # the alternatives were paused with the first card
    assert not any(s.enabled for s in config.searches if s.name in gpu_names)
    assert r.json()["project"]["progress_label"] == "Собрано 1 из 8"
    # undo
    r = c.delete(f"/api/v1/projects/{pid}/slots/gpu/bought")
    assert r.status_code == 200 and {s.name: s.enabled for s in config.searches}[chosen_search]
    assert r.json()["project"]["slots"][0]["status"] == "open"
    assert c.post(f"/api/v1/projects/{pid}/slots/nope/bought", json={"price": 1}).status_code == 404
    assert c.post(f"/api/v1/projects/{pid}/slots/gpu/bought", json={"price": 0}).status_code == 422


def test_budget_fit_alert_after_pass(api) -> None:
    c, app, config, db, monitor, notifier = api
    pid = create_project(c)["id"]
    project = track(c, pid)["project"]
    for slot in project["slots"]:
        if not slot["active"]:
            continue
        name = linked(project, slot["key"])[0] if linked(project, slot["key"]) else None
        assert name, slot["key"]
    titles = {"gpu": "AMD Instinct MI50 32GB", "cpu": "Ryzen 5 5600G", "board": "ASUS TUF B550-PLUS Mainboard",
              "ram": "Corsair 32GB DDR4 3200 2x16GB", "psu": "be quiet 1000W Netzteil", "storage": "Samsung 970 Evo 1TB NVMe",
              "case": "Midi Tower ATX Gehäuse", "gpu_cooling": "Tesla Lüfter Shroud"}
    n = 0
    for slot in project["slots"]:
        if not slot["active"]:
            continue
        chosen_search = next(s["name"] for s in project["searches_list"] if s["slot"] == slot["key"]
                             and s["option"] == slot["chosen"])
        for _ in range(slot["qty"]):
            n += 1
            _add_ad(db, f"70{n:02d}", titles[slot["key"]], max(5.0, (slot["target_unit"] or 50) + 5), chosen_search)
    monitor._emit("run_finished", {"run_id": 1})
    fit = [t for t in notifier.texts if "укладывается в бюджет" in t or "укладываешься в бюджет" in t]
    assert fit, notifier.texts
    card = c.get(f"/api/v1/projects/{pid}").json()
    assert card["fits_budget"] is True and card["best_complete"]
    assert any(a["kind"] == "budget_fit" for a in card["alerts_list"])
    count = len(notifier.texts)
    monitor._emit("run_finished", {"run_id": 2})
    assert len(notifier.texts) == count  # the fit is news only once


def test_delete_removes_project_searches(api) -> None:
    c, app, config, *_ = api
    pid = create_project(c)["id"]
    track(c, pid)
    assert len(config.searches) == 10
    r = c.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 200 and len(r.json()["searches_removed"]) == 10 and config.searches == []
    assert c.get(f"/api/v1/projects/{pid}").status_code == 404
    assert any(e.data["reason"] == "deleted" for e in events_of(app, "project_updated"))
    pid2 = create_project(c)["id"]
    track(c, pid2)
    r = c.delete(f"/api/v1/projects/{pid2}", params={"keep_searches": True})
    assert r.json()["searches_removed"] == [] and len(config.searches) == 10


def test_track_nothing_open_and_rename_read_only(api) -> None:
    c, app, config, *_ = api
    created = create_project(c)
    pid = created["id"]
    every = {s["key"]: {"status": "have"} for s in created["slots"]}
    r = c.patch(f"/api/v1/projects/{pid}", json={"slots": every})
    assert r.status_code == 200 and r.json()["progress_label"].startswith("Собрано")
    r = c.post(f"/api/v1/projects/{pid}/track", json={})
    assert r.status_code == 409 and "Нечего отслеживать" in r.json()["error"]["message_ru"]
    assert c.get(f"/api/v1/projects/{pid}").json()["status"] == "draft" and config.searches == []
    app.state.config_path = None  # settings read-only: renaming still works (it touches no search)
    r = c.patch(f"/api/v1/projects/{pid}", json={"name": "Сервер в шкафу"})
    assert r.status_code == 200 and r.json()["name"] == "Сервер в шкафу"


def test_nas_and_gaming_plans_through_the_api(api) -> None:
    c, *_ = api
    nas = c.post("/api/v1/projects/plan", json={"goal": "Домашний NAS на 8 ТБ для фото, бюджет 600 €"}).json()
    assert nas["template"] == "nas" and {ch["key"] for ch in nas["checks"]} >= {"capacity", "electricity", "psu"}
    hdd = next(s for s in nas["slots"] if s["key"] == "hdd")
    assert hdd["qty"] == 2 and hdd["chosen"] == "hdd_8tb"
    game = c.post("/api/v1/projects/plan", json={"template": "gaming_1080p", "budget": 800, "use_ai": False}).json()
    gpu = next(s for s in game["slots"] if s["key"] == "gpu")
    assert gpu["chosen"] in {"rx6600", "rtx3060_12", "rtx3060ti", "rx6700xt"}
    assert all(ch["status"] != "fail" for ch in game["checks"] if ch["key"] != "budget")
