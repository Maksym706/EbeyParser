"""Build-project alerts without the web app (projects.hooks on Monitor.on_event: `run`, `monitor`,
`once`), one hook per monitor even when the web app attaches too, and «one ad → one message»: the
monitor's deal alert carries the project context (notify.extras) instead of a second message."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ebeyparser.config import SearchConfig, parse_config
from ebeyparser.db import Database
from ebeyparser.models import Comparable, Listing, RunSummary
from ebeyparser.monitor import Monitor
from ebeyparser.notify import extras
from ebeyparser.notify.render import render_telegram, render_text
from ebeyparser.projects import hooks
from ebeyparser.projects.models import Plan, PlanOption, PlanSlot, Requirements
from ebeyparser.projects.store import ProjectStore
from ebeyparser.projects.tracker import ProjectAlerter

SEARCH = "Сервер · Tesla P40"
P40_SOLD = [Comparable(title="Nvidia Tesla P40 24GB", price=p, source="ebay_sold", sold=True,
                       url=f"https://www.ebay.de/itm/{3000000000 + i}")
            for i, p in enumerate([220, 230, 240, 250, 235, 245, 225, 238])]


@pytest.fixture(autouse=True)
def clean_extras():
    yield
    extras.unregister(hooks.EXTRAS_NAME)


def listing(ad_id: str, title: str, price: float) -> Listing:
    return Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-225-3331", title=title,
                   price=price, image_urls=["https://img.kleinanzeigen.de/x.jpg"], distance_km=6.0)


class Source:
    def __init__(self, items: list[Listing]) -> None:
        self.items = items

    async def search(self, search: SearchConfig, max_pages: int = 1, seen: Any = None) -> list[Listing]:
        return [i.model_copy(update={"search_name": search.name}) for i in self.items]

    async def fetch_detail(self, item: Listing) -> Listing:
        return item.model_copy(update={"detail_loaded": True, "description": "Funktioniert einwandfrei."})

    async def comparables(self, query: str, **kw: Any) -> list[Comparable]:
        return []

    async def download_images(self, item: Listing, max_images: int = 3) -> list[bytes]:
        return []

    async def aclose(self) -> None:
        pass


class Sold:
    async def sold_comparables(self, query: str, limit: int = 30) -> list[Comparable]:
        return list(P40_SOLD)


class TelegramLike:
    """Renders like the real Telegram notifier and records every message it would send."""

    name = "telegram"

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, deals: list[Any], *, title: str | None = None) -> None:
        self.messages += [render_telegram(d) for d in deals]

    async def send_text(self, text: str) -> None:
        self.messages.append(text)


def monitor_config() -> Any:
    return parse_config({
        "general": {"max_new_per_search": 10, "baseline_first_run": False},
        "searches": [{"name": SEARCH, "query": "tesla p40", "purpose": "personal", "target_price": 150}],
        "pricing": {"min_profit": 40, "min_roi": 0.25},
        "notifications": {"min_score": 50, "verdicts": ["buy"]},
    })


def tracking_project(db: Database, *, target: float = 200) -> int:
    plan = Plan(name="Сервер", template="custom", requirements=Requirements(kind="custom"), slots=[
        PlanSlot(key="item1", label="Tesla P40", kind="generic", chosen="main", options=[
            PlanOption(key="main", label="Tesla P40", query="tesla p40", source="user", target_price=target,
                       target_by="user", qty=2)])])  # two cards: both cheapest offers matter
    store = ProjectStore(db)
    project = store.create(plan, status="tracking")
    store.link_search(project.id, "item1", "main", SEARCH)
    return project.id


def test_one_ad_one_message_with_a_real_monitor_pass() -> None:
    db = Database()
    pid = tracking_project(db)
    telegram = TelegramLike()
    monitor = Monitor(monitor_config(), db, scraper=Source([
        listing("140", "Nvidia Tesla P40 24GB", 140),  # under the search's own target: a normal "buy" deal
        listing("190", "Tesla P40 24GB GPU", 190),  # no deal for the monitor, but under the PROJECT's target
    ]), ebay=Sold(), notifiers=[telegram], web_base_url="http://localhost:8000")
    alerter = hooks.attach(monitor, db)

    async def once() -> RunSummary:
        summary = await monitor.run_once()
        await hooks.drain(monitor)  # what `once` / `monitor` do before exiting
        return summary

    summary = asyncio.run(once())
    assert summary.deals_found == 1
    by_ad = {ad: [m for m in telegram.messages if f"/x/{ad}-" in m] for ad in ("140", "190")}
    assert len(by_ad["140"]) == 1 and len(by_ad["190"]) == 1  # one ad → one message
    deal_msg = by_ad["140"][0]
    assert "📦 Для сборки «Сервер»: Nvidia Tesla P40 24GB за 140 € → итог 330 €" in deal_msg
    assert "ниже цели 200 €" in deal_msg
    own = by_ad["190"][0]
    assert own.startswith("🧩 Сборка «Сервер»") and "Ниже твоей цели 200 €" in own
    assert own.endswith(f"http://localhost:8000/projects/{pid}")
    alerts = {a.ad_id: a for a in ProjectStore(db).alerts(pid)}
    assert alerts["140"].delivered and "в уведомлении о сделке" in alerts["140"].text
    assert alerts["190"].delivered and alerts["190"].text == own
    assert alerter.sent == [own]
    # the next pass: nothing new, nothing sent again
    count = len(telegram.messages)
    asyncio.run(once())
    assert len(telegram.messages) == count


def test_attach_is_idempotent_and_the_web_app_reuses_it(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from api_v1_helpers import FakeMonitor, make_app

    db = Database()
    monitor = FakeMonitor(db)
    first = hooks.attach(monitor, db)
    assert hooks.attach(monitor, db) is first
    app, *_ = make_app(tmp_path, monitor=monitor, seed=False)
    assert app.state.api.project_alerts is first
    mine = [h for h in monitor.on_event if getattr(h, "__self__", None) is first]
    assert len(mine) == 1  # the CLI and the web app never register it twice
    assert first._publish is not None  # the web app added live updates to the same alerter
    assert hooks.EXTRAS_NAME in extras.providers()


class FakeCliMonitor:
    """Stands in for Monitor in the CLI commands: emits run_finished like the real pass does."""

    instances: list["FakeCliMonitor"] = []

    def __init__(self, config: Any, db: Database, **kw: Any) -> None:
        self.config = config
        self.db = db
        self.on_event: list[Any] = []
        self.web_base_url = kw.get("web_base_url")
        FakeCliMonitor.instances.append(self)

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        for hook in list(self.on_event):
            hook(kind, data)

    async def run_once(self) -> RunSummary:
        self._emit("run_finished", {"run_id": 1})
        return RunSummary()

    async def run_forever(self, stop: asyncio.Event) -> None:
        self._emit("run_finished", {"run_id": 1})

    async def aclose(self) -> None:
        pass


def _cli_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[int]]:
    import ebeyparser.monitor as monitor_module

    FakeCliMonitor.instances = []
    monkeypatch.setattr(monitor_module, "Monitor", FakeCliMonitor)
    sweeps: list[int] = []
    original = ProjectAlerter.sweep

    async def spy(self: ProjectAlerter) -> int:
        sweeps.append(1)
        return await original(self)

    monkeypatch.setattr(ProjectAlerter, "sweep", spy)
    config = tmp_path / "config.yaml"
    config.write_text(f"general:\n  data_dir: {tmp_path / 'data'}\nsearches: []\n", encoding="utf-8")
    return config, sweeps


def test_cli_once_attaches_and_sweeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from ebeyparser import cli

    config, sweeps = _cli_setup(tmp_path, monkeypatch)
    rc = cli.cmd_once(argparse.Namespace(config=str(config), reevaluate=False, top=5))
    assert rc == 0
    mon = FakeCliMonitor.instances[0]
    assert isinstance(hooks.attached(mon), ProjectAlerter) and sweeps == [1]  # the sweep ran before exit


def test_cli_monitor_loop_attaches_and_sweeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ebeyparser import cli
    from ebeyparser.config import load_config

    config, sweeps = _cli_setup(tmp_path, monkeypatch)
    cfg = load_config(config)
    asyncio.run(cli._monitor_loop(cfg, Database()))
    assert isinstance(hooks.attached(FakeCliMonitor.instances[0]), ProjectAlerter) and sweeps == [1]


def test_extras_registry_is_safe() -> None:
    def broken(listing: Listing, evaluation: Any) -> list[str]:
        raise RuntimeError("boom")

    extras.register("broken", broken)
    extras.register("ok", lambda listing, ev: ["📦 строка", "📦 строка", "  "])
    try:
        assert extras.lines_for(listing("1", "x", 1)) == ["📦 строка"]
        from ebeyparser.models import DealView

        text = render_text([DealView(listing=listing("1", "RTX 3090", 900))])
        assert "📦 строка" in text
    finally:
        extras.unregister("broken")
        extras.unregister("ok")
