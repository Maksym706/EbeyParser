"""Home server 24/7: the headless first run («Открой http://<ip>:8000/?token=… на телефоне или ПК»
+ QR), Host checks for LAN / Tailscale, the small-model plan and Ollama pull, the scout on the
local Ollama, low-resource fixes, and the Docker / systemd / installer files."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from ebeyparser import cli, homeserver, qr, runtime
from ebeyparser.config import AppConfig, load_config
from ebeyparser.db import Database
from ebeyparser.web import security
from ebeyparser.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "k3y-for-tests-0123456789abcdef"


# ------------------------------------------------------------------ QR code
def _bits(matrix: list[list[bool]]) -> str:
    return "".join("1" if cell else "0" for row in matrix for cell in row)


def test_qr_matches_reference_encoder() -> None:
    # golden value cross-checked module by module against the `qrcode` library (ISO 18004 encoder)
    # for 672 text/version/level/mask combinations
    matrix = qr.encode("http://192.168.178.23:8000/?token=AbCdEfGhIjKlMnOpQrStUvWxYz012345", ecl="M", mask=3)
    assert matrix is not None and len(matrix) == 37  # version 5
    assert hashlib.sha256(_bits(matrix).encode()).hexdigest() == \
        "7519212250608e1781c8097c107529cc4da3ee3b4db05a5b879e302d6c2f706e"


def test_qr_structure_sizes_and_limits() -> None:
    finder = [[True] * 7, *[[True, False, False, False, False, False, True]] * 1,
              *[[True, False, True, True, True, False, True]] * 3,
              [True, False, False, False, False, False, True], [True] * 7]
    for text, size in (("a", 21), ("x" * 40, 29), ("http://100.101.102.103:8000/?token=" + "t" * 32, 37)):
        m = qr.encode(text)
        assert m is not None and len(m) == size and all(len(row) == size for row in m)
        for oy, ox in ((0, 0), (0, size - 7), (size - 7, 0)):  # the three finder patterns
            assert [row[ox:ox + 7] for row in m[oy:oy + 7]] == finder
        assert m[size - 8][8] is True  # the dark module
    assert qr.encode("y" * 134) is not None and qr.encode("y" * 135) is None
    assert qr.render("y" * 200) is None
    lines = qr.render("http://192.168.1.5:8000/?token=abc")
    assert lines is not None and len({len(line) for line in lines}) == 1
    assert set("".join(lines)) <= set("█▀▄ ")


# ------------------------------------------------------------------ Host header checks
def test_trusted_hosts_add_tailscale_names_only_in_network_mode() -> None:
    assert "*.ts.net" not in security.trusted_hosts("127.0.0.1")
    assert "*.ts.net" in security.trusted_hosts("0.0.0.0")
    assert "*.ts.net" in security.trusted_hosts("100.64.1.2")


def test_host_guard_accepts_ip_literals_and_tailscale_names_with_the_key(db: Database) -> None:
    app = create_app(AppConfig(), db, bind_host="0.0.0.0", access_token=TOKEN)
    header = {"X-EbeyParser-Token": TOKEN}
    for base in ("http://192.168.178.40:8000", "http://100.101.102.103:8000", "http://[fd7a:115c::1]:8000",
                 "http://my-server.tail1234.ts.net:8000", "http://localhost:8000"):
        with TestClient(app, base_url=base) as c:
            assert c.get("/api/v1/app", headers=header).status_code == 200, base
    with TestClient(app, base_url="http://evil.example:8000") as c:  # DNS rebinding needs a name
        assert c.get("/api/v1/app", headers=header).status_code == 400
    with TestClient(app, base_url="http://192.168.178.40:8000") as c:
        assert c.get("/api/v1/app").status_code == 401  # the key is still required


def test_host_guard_stays_strict_on_loopback(db: Database) -> None:
    app = create_app(AppConfig(), db)  # web.host 127.0.0.1, no key
    with TestClient(app, base_url="http://192.168.178.40:8000") as c:
        assert c.get("/").status_code == 400
    with TestClient(app, base_url="http://my-pc.tail1234.ts.net:8000") as c:
        assert c.get("/").status_code == 400


def test_host_of_parses_ports_and_ipv6() -> None:
    def scope(host: str) -> dict[str, Any]:
        return {"headers": [(b"host", host.encode())]}

    assert security._host_of(scope("192.168.1.5:8000")) == "192.168.1.5"
    assert security._host_of(scope("[fe80::1]:8000")) == "fe80::1"
    assert security._host_of(scope("my-pc.local")) == "my-pc.local"
    assert security._is_ip_literal("fe80::1%eth0") and not security._is_ip_literal("my-pc")


def test_local_addresses_include_all_interfaces_but_not_docker_bridges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "interface_addresses", lambda: {
        "lo": "127.0.0.1", "enp3s0": "192.168.178.40", "tailscale0": "100.101.102.103",
        "docker0": "172.17.0.1", "br-1a2b": "172.18.0.1", "veth9": "169.254.3.3"})
    addrs = security.local_addresses()
    assert {"192.168.178.40", "100.101.102.103"} <= addrs
    assert not {"172.17.0.1", "172.18.0.1"} & addrs
    assert security.lan_urls(8000, "T")[-1] == "http://100.101.102.103:8000/?token=T"  # home network first


def test_interface_addresses_on_linux() -> None:
    import sys

    found = security.interface_addresses()
    if sys.platform.startswith("linux"):
        assert found.get("lo") == "127.0.0.1"
    else:
        assert found == {}


# ------------------------------------------------------------------ links for the phone
ADDRS = ["127.0.0.1", "192.168.178.40", "100.101.102.103", "fe80::1", "my-server", "10.0.0.7"]


def test_access_links_order_and_modes() -> None:
    links = homeserver.access_links("0.0.0.0", 8000, "T", addresses=ADDRS, tailscale="srv.tail1.ts.net",
                                    allowed_hosts=["srv.tail1.ts.net", "nas.local", "*"])
    assert links == [("домашняя сеть", "http://10.0.0.7:8000/?token=T"),
                     ("домашняя сеть", "http://192.168.178.40:8000/?token=T"),
                     ("Tailscale", "http://100.101.102.103:8000/?token=T"),
                     ("Tailscale", "http://srv.tail1.ts.net:8000/?token=T"),
                     ("nas.local", "http://nas.local:8000/?token=T")]
    assert homeserver.access_links("100.64.0.9", 8000, "T") == [("Tailscale", "http://100.64.0.9:8000/?token=T")]
    assert homeserver.access_links("127.0.0.1", 8000, None) == [("этот компьютер", "http://localhost:8000/")]


def test_access_lines_say_where_to_open_with_a_qr_code(tmp_path: Path) -> None:
    lines = homeserver.access_lines("0.0.0.0", 8000, "T0K", tmp_path / "web_token.txt", addresses=ADDRS,
                                    bridge=False)
    text = "\n".join(lines)
    assert "📱 Открой http://192.168.178.40:8000/?token=T0K на телефоне или ПК (домашняя сеть)" in text
    assert "📱 Открой http://100.101.102.103:8000/?token=T0K на телефоне или ПК (Tailscale)" in text
    assert "QR-код" in text and "█" in text
    assert "http://localhost:8000/?token=T0K" in text and str(tmp_path / "web_token.txt") in text
    assert "python -m ebeyparser access" in text
    bridge = "\n".join(homeserver.access_lines("0.0.0.0", 8000, "T0K", tmp_path / "t", addresses=ADDRS, bridge=True))
    assert "http://<IP-сервера>:8000/?token=T0K" in bridge and "hostname -I" in bridge and "█" not in bridge
    local = homeserver.access_lines("127.0.0.1", 8000, None, tmp_path / "t")
    assert local == ["🚀 EbeyParser: открой http://localhost:8000 (панель открыта только на этом компьютере)"]


def test_docker_bridge_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def iface(name: str, index: int, link: int) -> None:
        (tmp_path / name).mkdir()
        (tmp_path / name / "ifindex").write_text(f"{index}\n")
        (tmp_path / name / "iflink").write_text(f"{link}\n")

    iface("lo", 1, 1)
    iface("eth0", 5, 6)  # a veth: its peer lives on the host
    monkeypatch.setattr(homeserver, "in_container", lambda: True)
    assert homeserver.docker_bridge(tmp_path) is True
    iface("enp3s0", 2, 2)  # network_mode: host sees the real card
    assert homeserver.docker_bridge(tmp_path) is False
    monkeypatch.setattr(homeserver, "in_container", lambda: False)
    assert homeserver.docker_bridge(tmp_path) is False


# ------------------------------------------------------------------ headless first run (cli)
class FakeServer:
    def __init__(self, app: Any, host: str, port: int) -> None:
        self.app, self.host, self.port = app, host, port
        self.started = self.should_exit = False

    def run(self) -> None:
        self.started = True


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> tuple[int, str, list[FakeServer]]:
    monkeypatch.chdir(tmp_path)
    servers: list[FakeServer] = []
    opened: list[str] = []
    monkeypatch.setattr(cli, "_make_server", lambda app, host, port, verbose: servers.append(
        FakeServer(app, host, port)) or servers[-1])
    monkeypatch.setattr(cli, "open_browser_when_ready", lambda *a, **k: opened.append("opened"))
    monkeypatch.setattr(security, "interface_addresses", lambda: {"lo": "127.0.0.1", "eth0": "192.168.178.40"})
    monkeypatch.setattr(security, "tailscale_name", lambda timeout=3.0: None)
    monkeypatch.setattr(homeserver, "docker_bridge", lambda sys_net=None: False)
    monkeypatch.delenv(homeserver.OLLAMA_URL_ENV, raising=False)
    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["-c", str(tmp_path / "config.yaml"), *argv])
    assert opened == []  # a server never opens a browser
    return rc, out.getvalue(), servers


def test_run_server_first_start_opens_panel_to_the_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, out, servers = _run(tmp_path, monkeypatch, "run", "--server", "--no-monitor")
    assert rc == 0 and servers[0].host == "0.0.0.0" and servers[0].port == 8000
    config = load_config(tmp_path / "config.yaml")
    assert config.web.host == "0.0.0.0" and config.searches == []
    token = (tmp_path / "data" / "web_token.txt").read_text().strip()
    assert f"📱 Открой http://192.168.178.40:8000/?token={token} на телефоне или ПК (домашняя сеть)" in out
    assert "█" in out and "настройка продолжится на телефоне или ПК" in out
    assert servers[0].app.state.access_token == token  # the onboarding needs the key
    with TestClient(servers[0].app, base_url="http://192.168.178.40:8000") as c:
        assert c.get("/api/v1/onboarding").status_code == 401
        r = c.get(f"/?token={token}", follow_redirects=False)
        assert r.status_code == 303 and "ebp_token" in r.headers["set-cookie"]
        assert c.get("/api/v1/onboarding").status_code == 200  # the browser remembers the key
    db = Database(tmp_path / "data" / "ebeyparser.sqlite3")
    assert db.get_state("onboarding:bootstrapped") is not None


def test_run_server_keeps_an_existing_local_config_and_says_how_to_open_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.bootstrap_config(tmp_path / "config.yaml")  # made on a desktop: this computer only
    rc, out, servers = _run(tmp_path, monkeypatch, "run", "--server", "--no-monitor")
    assert rc == 0 and servers[0].host == "127.0.0.1" and servers[0].app.state.access_token is None
    assert "python -m ebeyparser access lan" in out


def test_bootstrap_config_can_set_the_host(tmp_path: Path) -> None:
    assert cli.bootstrap_config(tmp_path / "config.yaml", host="0.0.0.0")
    assert load_config(tmp_path / "config.yaml").web.host == "0.0.0.0"
    assert not cli.bootstrap_config(tmp_path / "config.yaml", host="127.0.0.1")  # never overwrites
    assert load_config(tmp_path / "config.yaml").web.host == "0.0.0.0"


def test_access_command_switches_modes_and_prints_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, out, _ = _run(tmp_path, monkeypatch, "access", "lan")
    assert rc == 0 and load_config(tmp_path / "config.yaml").web.host == "0.0.0.0"
    assert "Открой http://192.168.178.40:8000/?token=" in out and "systemctl restart ebeyparser" in out
    assert Database(tmp_path / "data" / "ebeyparser.sqlite3").get_state("onboarding:bootstrapped") is not None
    rc, out, _ = _run(tmp_path, monkeypatch, "access", "tailscale")
    assert rc == 2 and "Tailscale не найден" in out
    rc, out, _ = _run(tmp_path, monkeypatch, "access", "tailscale", "--host", "100.70.1.2", "--no-qr")
    assert rc == 0 and load_config(tmp_path / "config.yaml").web.host == "100.70.1.2"
    assert "http://100.70.1.2:8000/?token=" in out and "█" not in out
    rc, out, _ = _run(tmp_path, monkeypatch, "access", "local")
    assert rc == 0 and "только на этом компьютере" in out


def test_web_base_url_for_alerts_is_reachable_from_the_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig()
    assert cli._web_base_url(cfg) == "http://localhost:8000"
    monkeypatch.setattr(cli, "_REACHABLE", ["my-server.tail1234.ts.net"])
    cfg.web.host = "0.0.0.0"
    assert cli._web_base_url(cfg) == "http://my-server.tail1234.ts.net:8000"
    assert cli._web_base_url(cfg, "100.64.0.9", 8080) == "http://100.64.0.9:8080"
    monkeypatch.setattr(cli, "_REACHABLE", [])
    monkeypatch.setattr(security, "tailscale_name", lambda timeout=3.0: None)
    monkeypatch.setattr(security, "interface_addresses", lambda: {"eth0": "192.168.178.40", "tailscale0": "100.99.1.1"})
    assert cli._web_base_url(cfg) == "http://100.99.1.1:8000"  # Tailscale works outside home too


# ------------------------------------------------------------------ model plan and Ollama pull
def test_server_plan_picks_by_ram_and_ollama_limit() -> None:
    small = homeserver.server_plan(4, arm=True)
    assert small["pull"] == ["qwen3.5:2b-q4_K_M", "qwen3-embedding:0.6b"] and small["tier"] == "T0"
    big = homeserver.server_plan(16)
    assert big["pull"][0] == "qwen3.5:4b-q4_K_M" and big["ram_needed_gb"] == 5.0
    capped = homeserver.server_plan(16, limit_gb=3.5)  # docker-compose.yml default OLLAMA_MEM_LIMIT
    assert capped["capped"] and capped["pull"][0] == "qwen3.5:2b-q4_K_M" and capped["ram_needed_gb"] <= 3.5
    assert homeserver.server_plan(None)["pull"][0] == "qwen3.5:2b-q4_K_M"  # unknown: the weakest box
    lines = homeserver.describe_plan(capped, homeserver.Hardware(16, 4, "x86_64", False))
    assert any("qwen3.5:2b-q4_K_M" in line for line in lines) and any("OLLAMA_MEM_LIMIT" in line for line in lines)


def test_parse_size_and_model_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    assert homeserver.parse_size_gb("3584m") == 3.5
    assert homeserver.parse_size_gb("6g") == 6.0 and homeserver.parse_size_gb("4GiB") == 4.0
    assert homeserver.parse_size_gb("8", default_unit="g") == 8.0
    assert homeserver.parse_size_gb("") is None and homeserver.parse_size_gb("lots") is None
    monkeypatch.setenv(homeserver.OLLAMA_MEM_ENV, "3584m")
    assert homeserver.model_ram_gb(homeserver.Hardware(16, 4, "x86_64", False)) == 4.5
    monkeypatch.setenv(homeserver.RAM_ENV, "6")
    assert homeserver.detect_hardware().ram_gb == 6.0


class FakeOllama:
    def __init__(self, installed: list[str] | None = None, fail: str | None = None) -> None:
        self.installed = list(installed or [])
        self.fail = fail
        self.pulled: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.installed]})
        if request.url.path == "/api/pull":
            name = json.loads(request.content)["model"]
            if name == self.fail:
                return httpx.Response(200, text=json.dumps({"error": "pull model manifest: file does not exist"}))
            self.pulled.append(name)
            self.installed.append(name)
            events = [{"status": "pulling manifest"}] + [
                {"status": "pulling", "total": 1000, "completed": c} for c in range(0, 1001, 250)
            ] + [{"status": "success"}]
            return httpx.Response(200, text="\n".join(json.dumps(e) for e in events))
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_pull_models_downloads_only_what_is_missing() -> None:
    fake = FakeOllama(installed=["qwen3-embedding:0.6b"])
    said: list[str] = []
    failed = homeserver.pull_models("http://ollama:11434", ["qwen3.5:2b-q4_K_M", "qwen3-embedding:0.6b"],
                                    client=fake.client(), say=said.append)
    assert failed == [] and fake.pulled == ["qwen3.5:2b-q4_K_M"]
    assert any("100%" in s for s in said) and any("уже скачана" in s for s in said)
    bad = FakeOllama(fail="qwen3.5:2b-q4_K_M")
    assert homeserver.pull_models("http://ollama:11434", ["qwen3.5:2b-q4_K_M"], client=bad.client(),
                                  say=said.append) == ["qwen3.5:2b-q4_K_M"]


def test_pull_models_gives_up_when_ollama_is_down() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    said: list[str] = []
    failed = homeserver.pull_models("http://ollama:11434", ["a:1"], client=httpx.Client(transport=httpx.MockTransport(down)),
                                    say=said.append, wait_seconds=0, sleep=lambda s: None)
    assert failed == ["a:1"] and "не отвечает" in said[-1]


def test_server_models_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv(homeserver.OLLAMA_MEM_ENV, "3584m")
    assert cli.main(["server-models", "--ram-gb", "8", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["pull"] == ["qwen3.5:2b-q4_K_M", "qwen3-embedding:0.6b"] and plan["hardware"]["ram_gb"] == 8
    assert cli.main(["server-models", "--ram-gb", "8", "--pull", "--dry-run"]) == 0
    assert "[dry-run] скачал бы" in capsys.readouterr().out
    calls: list[Any] = []
    monkeypatch.setattr(homeserver, "pull_models", lambda url, names, **kw: calls.append((url, names)) or ["x"])
    assert cli.main(["server-models", "--ram-gb", "8", "--pull", "--ollama", "http://o:1", "--never-fail"]) == 0
    assert cli.main(["server-models", "--ram-gb", "8", "--pull", "--ollama", "http://o:1"]) == 1
    assert calls[0] == ("http://o:1", ["qwen3.5:2b-q4_K_M", "qwen3-embedding:0.6b"])


# ------------------------------------------------------------------ the scout on the local Ollama
def _tags(*models: str):
    return lambda url: {"models": [{"name": m} for m in models]} if url.endswith("/api/tags") else None


def test_autoconfigure_scout_once_and_never_over_the_users_choice(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cli.bootstrap_config(path)
    config = load_config(path)
    config.general.data_dir = str(tmp_path / "data")
    note = homeserver.autoconfigure_scout(path, config, "http://ollama:11434",
                                          probe=_tags("qwen3.5:2b-q4_K_M", "qwen3-embedding:0.6b"), ram_gb=4.5)
    assert note and "qwen3.5:2b-q4_K_M" in note
    sc = load_config(path).ai.scout
    assert (sc.enabled, sc.provider, sc.base_url, sc.model) == (True, "ollama", "http://ollama:11434/v1",
                                                                "qwen3.5:2b-q4_K_M")
    assert runtime.remembered(config.data_path, homeserver.SCOUT_HINT)
    # switched off in the UI later: stays off
    fresh = load_config(path)
    fresh.general.data_dir = config.general.data_dir
    fresh.ai.scout.enabled, fresh.ai.scout.base_url = False, ""
    assert homeserver.autoconfigure_scout(path, fresh, "http://ollama:11434", probe=_tags("qwen3.5:2b-q4_K_M")) is None


def test_autoconfigure_scout_waits_for_ollama_and_its_models(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cli.bootstrap_config(path)
    config = load_config(path)
    config.general.data_dir = str(tmp_path / "data")
    before = path.read_text()
    assert homeserver.autoconfigure_scout(path, config, "http://ollama:11434", probe=lambda url: None) is None
    hint = homeserver.autoconfigure_scout(path, config, "http://ollama:11434", probe=_tags("qwen3-embedding:0.6b"))
    assert hint and "пока нет" in hint
    assert path.read_text() == before and not runtime.remembered(config.data_path, homeserver.SCOUT_HINT)
    config.ai.scout.base_url = "http://127.0.0.1:8080/v1"  # the user's own llama.cpp
    assert homeserver.autoconfigure_scout(path, config, "http://ollama:11434", probe=_tags("qwen3.5:2b-q4_K_M")) is None
    assert homeserver.autoconfigure_scout(path, config, "", probe=_tags("qwen3.5:2b-q4_K_M")) is None


def test_scout_hook_switches_the_scout_on_live_after_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cli.bootstrap_config(path)
    config = load_config(path)
    config.general.data_dir = str(tmp_path / "data")
    db = Database(tmp_path / "data" / "ebeyparser.sqlite3")
    app = create_app(config, db, config_path=path)
    models: list[str] = []
    hook = homeserver.scout_autoconfig_hook(app, "http://ollama:11434", probe=lambda url: _tags(*models)(url))
    assert hook is not None and homeserver.scout_autoconfig_hook(app, "") is None
    hook("run_started", {})
    hook("run_finished", {})  # still downloading
    assert not app.state.config.ai.scout.enabled
    models += ["qwen3.5:2b-q4_K_M"]
    hook("run_finished", {})
    assert app.state.config.ai.scout.enabled and app.state.config.ai.scout.model == "qwen3.5:2b-q4_K_M"
    assert load_config(path).ai.scout.enabled  # written to config.yaml too
    db.close()


def test_run_attaches_the_scout_hook_only_with_a_local_ollama(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Mon:
        on_event: list[Any] = []

    monkeypatch.setenv(homeserver.OLLAMA_URL_ENV, "http://ollama:11434")
    mon = Mon()
    mon.on_event = []
    cli._attach_scout_autoconfig(mon, object())
    assert len(mon.on_event) == 1
    monkeypatch.delenv(homeserver.OLLAMA_URL_ENV)
    mon.on_event = []
    cli._attach_scout_autoconfig(mon, object())
    assert mon.on_event == []


# ------------------------------------------------------------------ low-resource mode
def test_expired_comparables_are_dropped_from_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from ebeyparser import monitor as monitor_mod
    from ebeyparser.models import PriceEstimate

    mon = monitor_mod.Monitor.__new__(monitor_mod.Monitor)
    mon._comps_cache = OrderedDict()
    now = [1000.0]
    monkeypatch.setattr(monitor_mod.time, "monotonic", lambda: now[0])
    mon._cache_comps("old", PriceEstimate(market_price=100))
    now[0] += 60
    mon._cache_comps("newer", PriceEstimate(market_price=200))
    now[0] += monitor_mod.COMPS_CACHE_TTL - 30  # "old" expired, "newer" not yet
    mon._cache_comps("fresh", PriceEstimate(market_price=300))
    assert list(mon._comps_cache) == ["newer", "fresh"]


def test_uvicorn_runs_one_process_without_access_log() -> None:
    server = cli._make_server(object(), "127.0.0.1", 8000, verbose=False)
    assert server.config.workers in (None, 1) and server.config.access_log is False
    assert cli._make_server(object(), "127.0.0.1", 8000, verbose=True).config.access_log is True


def test_remembered_does_not_mark(tmp_path: Path) -> None:
    assert not runtime.remembered(tmp_path, "k")
    assert not runtime.remembered(tmp_path, "k")
    assert runtime.once(tmp_path, "k")
    assert runtime.remembered(tmp_path, "k") and not runtime.once(tmp_path, "k")


def test_startup_imports_stay_light() -> None:
    code = ("import sys, ebeyparser.cli, ebeyparser.homeserver, ebeyparser.qr; "
            "heavy = [m for m in ('PIL', 'anthropic', 'numpy', 'uvicorn', 'fastapi') if m in sys.modules]; "
            "print(','.join(heavy))")
    out = subprocess.run([__import__("sys").executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                         check=True).stdout.strip()
    assert out == ""


# ------------------------------------------------------------------ deploy files
def test_docker_compose_profiles_limits_and_scout_wiring() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    app, ollama, pull = (compose["services"][k] for k in ("ebeyparser", "ollama", "ollama-pull"))
    assert "profiles" not in app and ollama["profiles"] == ["ai"] and pull["profiles"] == ["ai"]
    assert app["restart"] == ollama["restart"] == "unless-stopped" and pull["restart"] == "no"
    assert app["environment"]["EBEYPARSER_OLLAMA_URL"] == "http://ollama:11434"
    assert "ollama:127.0.0.1" in app["extra_hosts"] and app["network_mode"] == "host"
    assert app["depends_on"]["ollama"] == {"condition": "service_healthy", "required": False}
    assert app["volumes"] == ["ebeyparser-data:/app/data"] and "ollama-models" in compose["volumes"]
    assert ollama["environment"]["OLLAMA_HOST"].startswith("127.0.0.1:")  # never open to the LAN
    for service in (app, ollama):
        limit = homeserver.parse_size_gb(service["mem_limit"].split(":-")[-1].rstrip("}"))
        assert limit is not None and limit <= 4
    assert "server-models" in pull["command"] and "--never-fail" in pull["command"]
    assert pull["image"] == app["image"] and pull["pull_policy"] == "never"


def test_dockerfile_is_slim_non_root_with_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in text and "USER ebey" in text and "TZ=Europe/Berlin" in text
    assert 'VOLUME ["/app/data"]' in text and "HEALTHCHECK" in text and "healthcheck.py" in text
    assert '"run", "--server"' in text and "EBEYPARSER_NO_BROWSER=1" in text
    for needed in ("README.md", "config.example.yaml", ".env.example", "ebeyparser", "deploy/healthcheck.py"):
        assert needed in text
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", "data/", "config.yaml", ".git"} <= set(ignored)


def test_systemd_unit_template() -> None:
    unit = (ROOT / "deploy" / "ebeyparser.service").read_text(encoding="utf-8")
    assert "ExecStart=@DIR@/.venv/bin/python -m ebeyparser run --server" in unit
    assert "User=@USER@" in unit and "WorkingDirectory=@DIR@" in unit and "Restart=always" in unit
    assert "PYTHONUNBUFFERED=1" in unit and "EBEYPARSER_OLLAMA_URL=http://127.0.0.1:11434" in unit


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
@pytest.mark.parametrize("script", ["install-linux.sh", "update-linux.sh"])
def test_shell_scripts_parse_and_have_help(script: str) -> None:
    path = ROOT / "deploy" / script
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text(encoding="utf-8")
    assert "--dry-run" in text and "set -euo pipefail" in text


class _Health(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        _Health.seen.append((self.path, self.headers.get("X-EbeyParser-Token")))
        if self.path.startswith("/api/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_docker_healthcheck_uses_config_port_and_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "web_token.txt").write_text("sekret\n")
        cfg = tmp_path / "data" / "config.yaml"
        cfg.write_text(yaml.safe_dump({"web": {"host": "0.0.0.0", "port": server.server_address[1]},
                                       "general": {"data_dir": str(tmp_path / "data")}}))
        env = {"EBEYPARSER_CONFIG": str(cfg), "PATH": "/usr/bin:/bin"}
        script = ROOT / "deploy" / "healthcheck.py"
        ok = subprocess.run([__import__("sys").executable, str(script)], env=env, capture_output=True, text=True,
                            timeout=30)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert ("/api/v1/health?ai=0", "sekret") in _Health.seen  # falls back from the legacy endpoint
        cfg.write_text(yaml.safe_dump({"web": {"host": "0.0.0.0", "port": 1}}))
        bad = subprocess.run([__import__("sys").executable, str(script)], env=env, capture_output=True, text=True,
                             timeout=30)
        assert bad.returncode == 1 and "unhealthy" in bad.stdout
    finally:
        server.shutdown()


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def db() -> Database:
    database = Database()
    yield database
    database.close()

