"""Running on a home server 24/7: which small models fit this box and pulling them into Ollama,
pointing the AI scout at that Ollama on the first start, and the links (+ QR) for the phone.

Used by `python -m ebeyparser run --server`, `server-models`, `access`, deploy/install-linux.sh
and the Docker `ai` profile (docker-compose.yml). No heavy imports: this runs at every start.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

OLLAMA_URL_ENV = "EBEYPARSER_OLLAMA_URL"  # e.g. http://ollama:11434 (Docker) or http://127.0.0.1:11434
RAM_ENV = "EBEYPARSER_RAM_GB"  # override the detected RAM
OLLAMA_MEM_ENV = "OLLAMA_MEM_LIMIT"  # the Ollama container's memory limit (docker-compose.yml), e.g. 3584m
SCOUT_HINT = "scout-autoconfig"
UNKNOWN_RAM_GB = 4.0  # can't tell: plan for the weakest box
ARM_MACHINES = frozenset({"arm64", "aarch64", "armv7l", "armv8l", "arm"})


# ------------------------------------------------------------------ hardware
@dataclass(frozen=True)
class Hardware:
    ram_gb: float | None
    cores: int | None
    arch: str
    arm: bool

    def describe_ru(self) -> str:
        ram = f"{self.ram_gb:g} ГБ памяти" if self.ram_gb else "память неизвестна"
        cores = f"{self.cores} {'ядро' if self.cores == 1 else 'ядра' if (self.cores or 0) < 5 else 'ядер'}" \
            if self.cores else "ядра неизвестны"
        return f"{ram}, {cores}, {self.arch or 'неизвестный процессор'}"


def _ram_bytes() -> int | None:
    try:
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(size) if pages > 0 and size > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def detect_hardware() -> Hardware:
    """RAM (GB), usable cores, architecture. EBEYPARSER_RAM_GB overrides the RAM."""
    info: dict[str, Any] = {}
    try:  # the AI settings use the same detection (Windows RAM, GPU); optional
        from .ai.hardware import detect_host

        info = dict(detect_host(gpu=False))
    except Exception:  # noqa: BLE001 - module missing or broken: fall back
        info = {}
    ram = info.get("ram_gb")
    if ram is None:
        raw = _ram_bytes()
        ram = round(raw / 1024 ** 3, 1) if raw else None
    override = parse_size_gb(os.environ.get(RAM_ENV, ""), default_unit="g")
    if override:
        ram = override
    try:
        cores: int | None = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        cores = info.get("cores") or os.cpu_count()
    arch = str(info.get("arch") or platform.machine() or "").lower()
    return Hardware(ram_gb=ram, cores=cores, arch=arch, arm=arch in ARM_MACHINES)


def parse_size_gb(text: str | None, *, default_unit: str = "b") -> float | None:
    """'3584m' / '3.5g' / '4GiB' / '6' (in `default_unit`) -> GB (2^30 bytes); None if empty/invalid."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([bkmgt]?)(?:i?b)?\s*", (text or "").lower())
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2) or default_unit
    factor = {"b": 1 / 1024 ** 3, "k": 1 / 1024 ** 2, "m": 1 / 1024, "g": 1.0, "t": 1024.0}[unit]
    gb = round(value * factor, 2)
    return gb if gb > 0 else None


# ------------------------------------------------------------------ models
def server_plan(ram_gb: float | None, *, arm: bool = False, limit_gb: float | None = None) -> dict[str, Any]:
    """The always-on server's pair from the model catalog: the scout (triage) and the embedding
    model. `limit_gb`: a memory cap for the model server (the Ollama container's limit) — both
    models must fit in it, otherwise the catalog is asked again for that much memory."""
    from .ai.model_catalog import APP_RAM_GB, recommend

    ram = float(ram_gb or UNKNOWN_RAM_GB)
    rec = recommend(ram, 0.0, arm)

    def need(r: dict[str, Any]) -> float:
        return sum(float(m["min_ram_gb"]) for m in (r.get("triage"), r.get("embed")) if m)

    capped = False
    if limit_gb and need(rec) > limit_gb:
        rec = recommend(min(ram, limit_gb + APP_RAM_GB), 0.0, arm)
        capped = True
    models = [m for m in (rec.get("triage"), rec.get("embed")) if m and m.get("ollama")]
    return {
        "ram_gb": ram_gb,
        "limit_gb": limit_gb,
        "capped": capped,
        "tier": rec["tier"],
        "tier_label_ru": rec["tier_label_ru"],
        "triage": rec.get("triage"),
        "embed": rec.get("embed"),
        "pull": [m["ollama"] for m in models],
        "ram_needed_gb": round(need(rec), 1),
        "notes_ru": list(rec.get("notes_ru") or []),
    }


def ollama_limit_gb() -> float | None:
    """The model server's memory cap (OLLAMA_MEM_LIMIT, set by docker-compose.yml) or None."""
    return parse_size_gb(os.environ.get(OLLAMA_MEM_ENV, ""))


def model_ram_gb(hw: Hardware | None = None) -> float | None:
    """Memory the models may use: this box's RAM, capped by OLLAMA_MEM_LIMIT (+ the app's share,
    which the catalog adds on top)."""
    from .ai.model_catalog import APP_RAM_GB

    hw = hw or detect_hardware()
    limit = ollama_limit_gb()
    if limit is None:
        return hw.ram_gb
    return min(hw.ram_gb or limit + APP_RAM_GB, limit + APP_RAM_GB)


def describe_plan(plan: dict[str, Any], hw: Hardware) -> list[str]:
    lines = [f"Этот сервер: {hw.describe_ru()} → {plan['tier_label_ru']}."]
    if plan.get("capped"):
        lines.append(f"Ollama ограничена {plan['limit_gb']:g} ГБ (OLLAMA_MEM_LIMIT) — беру модели, которые влезают.")
    triage, embed = plan.get("triage"), plan.get("embed")
    if triage:
        speed = triage.get("expected_ads_per_hour")
        lines.append(f"• Разведчик (читает каждое объявление): {triage['name']} — {triage['ollama']}, "
                     f"~{triage['file_gb']:g} ГБ на диске, ~{triage['min_ram_gb']:g} ГБ памяти"
                     + (f", ≈{speed} объявлений в час" if speed else ""))
    else:
        lines.append("• Разведчик: памяти не хватает даже на самую маленькую модель — включи его на другом компьютере.")
    if embed:
        lines.append(f"• Поиск похожих: {embed['name']} — {embed['ollama']}, ~{embed['file_gb']:g} ГБ на диске, "
                     f"~{embed['min_ram_gb']:g} ГБ памяти")
    lines += [f"  {note}" for note in plan.get("notes_ru") or []]
    lines.append("• Фото смотрит большая модель на игровом ПК (LM Studio) — настраивается в панели:"
                 " Настройки → Нейросеть.")
    return lines


# ------------------------------------------------------------------ Ollama
class OllamaError(RuntimeError):
    pass


def _http() -> Any:
    import httpx

    # the model server is on this machine or in the LAN: never through a proxy
    return httpx.Client(timeout=httpx.Timeout(30.0, read=None), trust_env=False)


def ollama_models(base_url: str, client: Any | None = None) -> list[str]:
    own = client is None
    client = client or _http()
    try:
        resp = client.get(f"{base_url.rstrip('/')}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise OllamaError(f"Ollama на {base_url} не отвечает: {exc}") from exc
    finally:
        if own:
            client.close()
    return [str(m.get("name") or m.get("model")) for m in data.get("models") or [] if isinstance(m, dict)]


def wait_for_ollama(base_url: str, seconds: float = 60.0, client: Any | None = None,
                    sleep: Callable[[float], Any] = time.sleep) -> list[str]:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return ollama_models(base_url, client)
        except OllamaError:
            if time.monotonic() >= deadline:
                raise
            sleep(2.0)


def _installed(name: str, installed: Iterable[str]) -> bool:
    want = name if ":" in name else f"{name}:latest"
    return any(m == want or m == name for m in installed)


def ollama_pull(base_url: str, name: str, *, client: Any | None = None,
                say: Callable[[str], Any] = print) -> None:
    """POST /api/pull with progress every ~10 %. Raises OllamaError."""
    own = client is None
    client = client or _http()
    last = -10
    try:
        with client.stream("POST", f"{base_url.rstrip('/')}/api/pull", json={"model": name, "stream": True}) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise OllamaError(f"Ollama не скачала {name}: HTTP {resp.status_code} {resp.text[:200]}")
            for line in resp.iter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("error"):
                    raise OllamaError(f"Ollama не скачала {name}: {event['error']}")
                total, done = event.get("total"), event.get("completed")
                if total and done is not None:
                    pct = int(done * 100 / total)
                    if pct >= last + 10 or pct == 100:
                        last = pct - pct % 10
                        say(f"   {name}: {pct}% из {total / 1024 ** 3:.1f} ГБ")
                elif event.get("status") == "success":
                    say(f"   ✔ {name} скачана")
    except OllamaError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OllamaError(f"Ollama не скачала {name}: {exc}") from exc
    finally:
        if own:
            client.close()


def pull_models(base_url: str, names: list[str], *, client: Any | None = None, say: Callable[[str], Any] = print,
                wait_seconds: float = 60.0, sleep: Callable[[float], Any] = time.sleep) -> list[str]:
    """Pull the models that are not installed yet; returns the failed ones (never raises)."""
    try:
        installed = wait_for_ollama(base_url, wait_seconds, client, sleep)
    except OllamaError as exc:
        say(f"✖ {exc}")
        return list(names)
    failed = []
    for name in names:
        if _installed(name, installed):
            say(f"   ✔ {name} уже скачана")
            continue
        say(f"⏳ Скачиваю {name} …")
        try:
            ollama_pull(base_url, name, client=client, say=say)
        except OllamaError as exc:
            say(f"✖ {exc}")
            failed.append(name)
    return failed


# ------------------------------------------------------------------ scout on the local Ollama
def scout_pending(config: Any) -> bool:
    """The AI scout was never set up here (and not auto-configured before): ours to switch on."""
    from .runtime import remembered

    sc = config.ai.scout
    return not (sc.enabled or sc.base_url.strip() or remembered(config.data_path, SCOUT_HINT))


def scout_updates(url: str | None, *, probe: Callable[[str], Any] | None = None,
                  ram_gb: float | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(config updates, Russian note) that point the AI scout at the Ollama on `url` with the best
    triage model installed there; (None, None) when Ollama doesn't answer (profile off, not up
    yet), (None, hint) when it has no suitable model yet."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return None, None
    from .web.localai import probe_json

    fetch = probe or probe_json
    server = url[:-3] if url.endswith("/v1") else url
    data = fetch(f"{server}/api/tags")
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None, None
    ids = [str(m.get("name") or m.get("model")) for m in data["models"] if isinstance(m, dict)]
    from .ai.model_catalog import best_installed

    model = best_installed(ids, task="triage", ram_gb=ram_gb)
    if not model:
        return None, (f"Ollama на {server} работает, но модели для разведчика там пока нет — она скачивается"
                      " (или: python -m ebeyparser server-models --pull)")
    updates = {"ai.scout.enabled": True, "ai.scout.provider": "ollama", "ai.scout.base_url": f"{server}/v1",
               "ai.scout.model": model}
    return updates, (f"Нейросеть-разведчик включена: {model} в Ollama ({server}). Поменять или выключить —"
                     " Настройки → Нейросеть в панели.")


def autoconfigure_scout(config_path: Path, config: Any, url: str | None, *,
                        probe: Callable[[str], Any] | None = None, ram_gb: float | None = None) -> str | None:
    """At startup (before the web app reads the config): the Docker `ai` profile or
    install-linux.sh put an Ollama next to us — switch the AI scout on there. Only once, and only
    while the scout was never set up: the user's own choice in the UI always wins."""
    from .runtime import once

    if not url or not scout_pending(config):
        return None
    updates, note = scout_updates(url, probe=probe, ram_gb=ram_gb)
    if updates:
        from .web.configfile import update_yaml_values

        update_yaml_values(config_path, updates)
        once(config.data_path, SCOUT_HINT)
    return note


def scout_autoconfig_hook(app: Any, url: str | None, *, probe: Callable[[str], Any] | None = None,
                          ram_gb: float | None = None) -> Callable[[str, dict[str, Any]], None] | None:
    """A monitor event hook for a running app: after each pass, while the scout is still pending,
    look again (the models are still downloading on the first start) and switch it on live
    through the web app's own settings writer."""
    from .runtime import once

    if not url:
        return None
    done = {"flag": False}

    def hook(kind: str, data: dict[str, Any]) -> None:
        if kind != "run_finished" or done["flag"]:
            return
        ctx = getattr(getattr(app, "state", None), "api", None)
        if ctx is None:
            return
        if not scout_pending(ctx.config):
            done["flag"] = True
            return
        updates, note = scout_updates(url, probe=probe, ram_gb=ram_gb)
        if not updates:
            return
        ctx.write_values(updates, reason="ai")
        once(ctx.config.data_path, SCOUT_HINT)
        done["flag"] = True
        log.info("%s", note)

    return hook


# ------------------------------------------------------------------ links for the phone
def in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("container"))


def _veth(name: str, sys_net: Path = Path("/sys/class/net")) -> bool:
    """A container's end of a virtual cable (its iflink points at the peer on the host)."""
    try:
        return (sys_net / name / "iflink").read_text().strip() != (sys_net / name / "ifindex").read_text().strip()
    except OSError:
        return False


def docker_bridge(sys_net: Path = Path("/sys/class/net")) -> bool:
    """Inside a container with its own network (not network_mode: host): the addresses we see
    are Docker's, not the server's."""
    if not in_container():
        return False
    try:
        names = [n.name for n in sys_net.iterdir() if n.name != "lo"]
    except OSError:
        return False
    return bool(names) and all(_veth(n, sys_net) for n in names)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def access_links(bind_host: str, port: int, token: str | None, *, allowed_hosts: Iterable[str] = (),
                 tailscale: str | None = None, addresses: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """[(where, url)] to open the panel from a phone or another PC: home network first, then
    Tailscale (IP, then the MagicDNS name), then the configured names. Loopback: this computer."""
    from .web.security import ANY_ADDRESS, TOKEN_PARAM, is_loopback, is_tailscale_ip, local_addresses

    host = (bind_host or "").strip().strip("[]")
    if is_loopback(host):
        return [("этот компьютер", f"http://localhost:{port}/")]
    query = f"/?{TOKEN_PARAM}={token}" if token else "/"

    def url(h: str) -> str:
        return f"http://{_url_host(h)}:{port}{query}"

    if host.lower() in ANY_ADDRESS:
        ips = []
        for addr in addresses if addresses is not None else local_addresses():
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip.version == 4 and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
                ips.append(addr)
        lan = sorted((a for a in ips if not is_tailscale_ip(a)), key=lambda a: tuple(map(int, a.split("."))))
        ts = sorted(a for a in ips if is_tailscale_ip(a))
    elif is_tailscale_ip(host):
        lan, ts = [], [host]
    else:
        lan, ts = [host], []
    links = [("домашняя сеть", url(ip)) for ip in lan] + [("Tailscale", url(ip)) for ip in ts]
    if tailscale and (ts or host.lower() in ANY_ADDRESS):
        links.append(("Tailscale", url(tailscale)))
    for name in allowed_hosts:
        name = str(name).strip().lower()
        if name and "*" not in name and all(name not in u for _, u in links):
            links.append((name, url(name)))
    return links


def access_lines(bind_host: str, port: int, token: str | None, token_file: Path, *,
                 allowed_hosts: Iterable[str] = (), qr: bool = True, tailscale: str | None = None,
                 addresses: Iterable[str] | None = None, bridge: bool | None = None) -> list[str]:
    """The startup text: where to open the panel (+ a QR code for the first link)."""
    from .web.security import is_loopback

    host = (bind_host or "").strip()
    if is_loopback(host):
        return [f"🚀 EbeyParser: открой http://localhost:{port} (панель открыта только на этом компьютере)"]
    everywhere = host in ("0.0.0.0", "::", "")
    bridge = docker_bridge() if bridge is None else bridge
    links = [] if bridge and everywhere else access_links(
        host, port, token, allowed_hosts=allowed_hosts, tailscale=tailscale, addresses=addresses)
    lines = [f"⚠ Панель открыта {'для всей сети' if everywhere else 'по адресу ' + host} — вход только по ссылке"
             " с ключом. Не делай так в общественном Wi-Fi (общежитие, кафе); для телефона лучше Tailscale."]
    for where, link in links:
        lines.append(f"📱 Открой {link} на телефоне или ПК ({where})")
    if not links:
        lines.append(f"📱 Открой http://<IP-сервера>:{port}/?token={token} на телефоне или ПК"
                     " (IP сервера покажет команда hostname -I на сервере)")
    if qr and links:
        from .qr import render

        code = render(links[0][1])
        if code:
            lines.append(f"   Или наведи камеру телефона на QR-код ({links[0][0]}):")
            lines += ["   " + row for row in code]
    if everywhere:
        lines.append(f"   На этом компьютере: http://localhost:{port}/?token={token}")
    lines.append(f"   Ключ хранится в {token_file} — удали файл, чтобы сменить ключ."
                 " Показать ссылки ещё раз: python -m ebeyparser access")
    return lines


def emit(lines: Iterable[str]) -> None:
    """Print now (a systemd / Docker log reads a pipe: no waiting for the buffer)."""
    for line in lines:
        print(line)
    try:
        sys.stdout.flush()
    except (AttributeError, OSError, ValueError):
        pass


__all__ = [
    "Hardware",
    "OLLAMA_URL_ENV",
    "OllamaError",
    "access_lines",
    "access_links",
    "autoconfigure_scout",
    "scout_autoconfig_hook",
    "scout_pending",
    "scout_updates",
    "describe_plan",
    "detect_hardware",
    "docker_bridge",
    "in_container",
    "model_ram_gb",
    "ollama_models",
    "ollama_pull",
    "parse_size_gb",
    "pull_models",
    "server_plan",
]
