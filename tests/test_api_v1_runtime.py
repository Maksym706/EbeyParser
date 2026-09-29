"""Monitor hooks for the web UI (events, pause/resume, progress) and the launch experience
of `python -m ebeyparser` (config bootstrap, browser, restart loop)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ebeyparser import cli
from ebeyparser.config import load_config
from ebeyparser.db import Database
from ebeyparser.monitor import Monitor
from test_monitor import GOOD_AI, build, config, make_listing


# ------------------------------------------------------------------ monitor hooks
async def test_run_emits_started_progress_deal_and_finished() -> None:
    listings = [make_listing("1001", "Gigabyte RTX 3080 Gaming OC 10GB", 250.0),
                make_listing("1002", "RTX 3080 FE", 560.0)]
    monitor, db, source, notifier = build(config(), listings, verdict=GOOD_AI)
    events: list[tuple[str, dict[str, Any]]] = []
    monitor.on_event.append(lambda kind, data: events.append((kind, data)))
    monitor.on_event.append(lambda kind, data: 1 / 0)  # a broken hook never breaks the pass
    summary = await monitor.run_once()
    kinds = [k for k, _ in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished" and summary.deals_found == 1
    assert events[0][1]["searches"] == 1 and events[0][1]["run_id"] == summary.id
    progress = next(d for k, d in events if k == "run_progress")
    assert progress["index"] == 1 and progress["total"] == 1 and progress["search_name"] == "GPU"
    deals = [d for k, d in events if k == "deal_found"]
    assert [d["ad_id"] for d in deals] == ["1001"] and deals[0]["verdict"] == "buy"
    assert events[-1][1]["deals_found"] == 1 and monitor.progress is None


async def test_run_without_searches_reports_finished() -> None:
    monitor, *_ = build(config(searches=[]), [])
    events: list[str] = []
    monitor.on_event.append(lambda kind, data: events.append(kind))
    await monitor.run_once()
    assert events == ["run_finished"]


async def test_health_alert_event_even_without_channels() -> None:
    monitor, db, source, _ = build(config(), [])
    monitor._notifiers = []
    events: list[dict[str, Any]] = []
    monitor.on_event.append(lambda kind, data: events.append(data) if kind == "health_alert" else None)
    summary = db.start_run()
    await monitor._health("ai_down", "⚠ Нейросеть недоступна", summary)
    await monitor._health("ai_down", "⚠ Нейросеть недоступна", summary)  # throttled
    await monitor._health("blocked", "⚠ Kleinanzeigen ограничил запросы", summary)
    assert [e["kind"] for e in events] == ["ai_down", "blocked"] and events[0]["text"].startswith("⚠")


async def test_pause_skips_passes_persists_and_resume_runs_overdue_pass() -> None:
    monitor, db, source, _ = build(config(general={"interval_minutes": 60, "baseline_first_run": False}), [])
    events: list[str] = []
    monitor.on_event.append(lambda kind, data: events.append(kind))
    monitor.pause()
    monitor.pause()  # idempotent
    assert monitor.paused and events == ["monitor_paused"] and db.get_state("monitor:paused")[0] == "1"
    assert Monitor(monitor.config, db).paused  # survives a restart
    stop = asyncio.Event()
    task = asyncio.create_task(monitor.run_forever(stop))
    await asyncio.sleep(0.05)
    assert source.search_calls == 0 and monitor.next_run_at is None
    monitor.resume()
    await asyncio.sleep(0.05)
    assert source.search_calls == 1 and monitor.next_run_at is not None
    assert events[1:] == ["monitor_resumed", "run_started", "run_progress", "run_finished"]
    monitor.pause()
    await asyncio.sleep(0.02)
    assert monitor.next_run_at is None
    monitor.resume()
    await asyncio.sleep(0.02)
    assert source.search_calls == 1  # the next pass is not due yet
    stop.set()
    await asyncio.wait_for(task, 1)
    assert not Monitor(monitor.config, db).paused


async def test_evaluate_url_reports_progress() -> None:
    monitor, db, source, _ = build(config(), [], verdict=GOOD_AI)
    stages: list[str] = []
    deal = await monitor.evaluate_url("https://www.kleinanzeigen.de/s-anzeige/rtx-3080/2891234567-225-3331",
                                      progress=lambda stage, text: stages.append(stage))
    assert deal.listing.ad_id == "2891234567"
    assert stages[0] == "fetch" and "market" in stages and "ai" in stages
    other: list[str] = []
    await monitor.evaluate_listing(deal.listing, monitor.config.searches[0],
                                   progress=lambda stage, text: other.append(stage))
    assert "market" in other and monitor._stage_cb is None


async def test_update_config_rebuilds_http_client_after_in_place_change(tmp_path: Path) -> None:
    cfg = config(general={"data_dir": str(tmp_path), "baseline_first_run": False})
    monitor = Monitor(cfg, Database())
    monitor._ensure_components()
    assert monitor._client is not None
    monitor.update_config(cfg)  # nothing changed: the client stays
    assert monitor._client is not None
    cfg.general = cfg.general.model_copy(update={"max_requests_per_hour": 60})  # what the web UI does
    monitor.update_config(cfg)
    assert monitor._client is None and monitor._scraper is None
    await asyncio.sleep(0)
    await monitor.aclose()


# ------------------------------------------------------------------ launch experience
def test_bootstrap_config_creates_empty_onboarding_config(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.yaml"
    assert cli.bootstrap_config(path) is True
    assert cli.bootstrap_config(path) is False  # never overwrites
    cfg = load_config(path)
    assert cfg.searches == [] and cfg.ai.enabled is False
    text = path.read_text(encoding="utf-8")
    assert "searches: []" in text and "# ─── Как считать выгоду" in text  # comments of the example kept
    assert (path.parent / ".env").is_file()


def _args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**{"no_browser": False, **kw})


def test_browser_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    for key in ("EBEYPARSER_NO_BROWSER", "CI", "INVOCATION_ID", "container", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli.Path, "exists", lambda self: False if str(self) == "/.dockerenv" else Path.is_file(self))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", Tty(True))
    assert cli.browser_allowed(_args())
    assert not cli.browser_allowed(_args(no_browser=True))
    monkeypatch.setattr(sys, "stdin", None)  # pythonw started from a shortcut
    assert cli.browser_allowed(_args())
    monkeypatch.setattr(sys, "stdin", Tty(False))
    assert not cli.browser_allowed(_args())
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", Tty(True))
    assert not cli.browser_allowed(_args())  # no desktop
    monkeypatch.setenv("DISPLAY", ":0")
    assert cli.browser_allowed(_args())
    monkeypatch.setenv("INVOCATION_ID", "systemd")
    assert not cli.browser_allowed(_args())
    monkeypatch.delenv("INVOCATION_ID")
    monkeypatch.setenv("EBEYPARSER_NO_BROWSER", "1")
    assert not cli.browser_allowed(_args())


def test_open_browser_when_ready_once(tmp_path: Path) -> None:
    class Server:
        started = False
        should_exit = False

    opened: list[str] = []
    server = Server()
    cli.open_browser_when_ready(server, "http://localhost:8000/", tmp_path, opener=opened.append,
                                sleep=lambda s: time.sleep(0.001))
    time.sleep(0.05)
    assert opened == []  # not before the server listens
    server.started = True
    for _ in range(100):
        if opened:
            break
        time.sleep(0.01)
    assert opened == ["http://localhost:8000/"]
    again: list[str] = []
    cli.open_browser_when_ready(server, "http://localhost:8000/", tmp_path, opener=again.append)
    time.sleep(0.1)
    assert again == []  # opened a moment ago (crash-restart loop): no second tab
    later: list[str] = []
    cli.open_browser_when_ready(server, "http://x/", tmp_path, opener=later.append,
                                clock=lambda: time.time() + cli.BROWSER_QUIET_SECONDS + 5)
    for _ in range(100):
        if later:
            break
        time.sleep(0.01)
    assert later == ["http://x/"]
    gave_up: list[str] = []
    stopped = Server()
    stopped.should_exit = True
    cli.open_browser_when_ready(stopped, "http://y/", tmp_path / "other", opener=gave_up.append)
    time.sleep(0.05)
    assert gave_up == []


def test_parser_no_browser_everywhere() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["run", "--no-browser"]).no_browser is True
    assert parser.parse_args(["--no-browser", "run"]).no_browser is True
    assert parser.parse_args(["run"]).no_browser is False


def test_cmd_run_bootstraps_opens_browser_once_and_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EBEYPARSER_NO_BROWSER", "")
    servers: list[Any] = []
    opened: list[str] = []

    class FakeServer:
        def __init__(self, app: Any) -> None:
            self.app = app
            self.started = False
            self.should_exit = False

        def run(self) -> None:
            servers.append(self)
            self.started = True
            assert self.app.state.api is not None  # /api/v1 mounted
            if len(servers) == 1:
                self.app.state.restart_callback()  # e.g. POST /api/v1/system/restart after a host change
                assert self.should_exit and self.app.state.api_events.closed

    monkeypatch.setattr(cli, "_make_server", lambda app, host, port, verbose: FakeServer(app))
    monkeypatch.setattr(cli, "browser_allowed", lambda args: True)
    monkeypatch.setattr(cli, "open_browser_when_ready", lambda server, url, data_dir: opened.append(url))
    args = cli.build_parser().parse_args(["-c", str(tmp_path / "config.yaml"), "run", "--no-monitor"])
    assert cli.cmd_run(args) == 0
    assert (tmp_path / "config.yaml").is_file() and load_config(tmp_path / "config.yaml").searches == []
    db = Database(tmp_path / "data" / "ebeyparser.sqlite3")
    assert db.get_state("onboarding:bootstrapped") is not None
    assert len(servers) == 2 and opened == ["http://localhost:8000/"]  # the browser opens on the first start only


def test_cmd_run_ctrl_c_is_a_clean_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    class Interrupted:
        started = should_exit = False

        def run(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_make_server", lambda app, host, port, verbose: Interrupted())
    args = cli.build_parser().parse_args(["-c", str(tmp_path / "config.yaml"), "--no-browser", "run"])
    assert cli.cmd_run(args) == 0


def test_make_server_closes_live_streams_on_signal() -> None:
    from ebeyparser.web.api.events import EventHub

    class State:
        api_events = EventHub()

    class App:
        state = State()

    server = cli._make_server(App(), "127.0.0.1", 8765, False)
    server.handle_exit(2, None)
    assert server.should_exit and App.state.api_events.closed


def test_shutdown_ends_sse_and_jobs(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from api_v1_helpers import LOCAL, make_app

    app, *_ = make_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/api/v1/app").status_code == 200
    assert app.state.api.hub.closed


def test_database_migration_adds_pipeline_columns(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE deal_state (ad_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'new',"
                 " note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO deal_state VALUES ('1', 'bought', 'old note', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    db = Database(path)
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(deal_state)")}
    assert {"bought_price", "bought_at", "sold_price", "sold_at", "extra_costs", "hidden_reason", "contacted_at",
            "seen_at"} <= cols
    assert db.deal_extras(["1"])["1"]["bought_price"] is None
    extras = db.update_deal_state("1", status="sold", sold_price=100.0)
    assert extras["sold_price"] == 100.0
    assert db._conn.execute("SELECT note FROM deal_state WHERE ad_id = '1'").fetchone()[0] == "old note"
    with pytest.raises(ValueError):
        db.update_deal_state("1", status="lost")
    with pytest.raises(ValueError):
        db.update_deal_state("1", colour="red")
    Database(path)  # a second open is a no-op migration


def test_threads_can_publish_events() -> None:
    from ebeyparser.web.api.events import EventHub, sse_stream

    hub = EventHub()

    async def main() -> str:
        async def reader() -> str:
            out = []
            async for chunk in sse_stream(hub, max_events=1, timeout=2):
                out.append(chunk)
            return "".join(out)

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.05)
        threading.Thread(target=lambda: hub.publish("health_alert", {"kind": "x"})).start()
        return await task

    assert "event: health_alert" in asyncio.run(main())
