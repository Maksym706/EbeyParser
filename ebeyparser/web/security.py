"""Who may open the web UI.

* Host header allow-list (Starlette TrustedHostMiddleware) against DNS rebinding:
  a foreign website must not be able to talk to http://localhost:8000 through a
  domain of its own.
* When the server listens on the network (web.host: 0.0.0.0 or a LAN address),
  an access token is required: generated once, stored in data/web_token.txt,
  accepted as ?token=... (then remembered in a cookie) or X-EbeyParser-Token header.
  Then any IP-literal Host is fine too (DNS rebinding needs a domain name; a home server's
  address changes with DHCP or when Tailscale comes up after the app), and so are Tailscale
  MagicDNS names (*.ts.net).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
ANY_ADDRESS = ("0.0.0.0", "::", "")
TOKEN_FILE = "web_token.txt"
TOKEN_PARAM = "token"
TOKEN_HEADER = "x-ebeyparser-token"
COOKIE_NAME = "ebp_token"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookies at ~400 days
ALLOWED_HOSTS_ENV = "EBEYPARSER_ALLOWED_HOSTS"  # comma-separated, until config has web.allowed_hosts
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_HOSTS = "*.ts.net"  # MagicDNS names (network mode only)
# interfaces whose addresses are no use to a phone: Docker/Podman/VM bridges, container veths
VIRTUAL_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vnet", "lxcbr", "lxdbr", "cni", "flannel",
                          "podman", "vmnet", "vboxnet", "kube")
SIOCGIFADDR = 0x8915


def is_loopback(host: str | None) -> bool:
    """True for localhost / 127.x / ::1 — the server is reachable from this computer only."""
    name = (host or "").strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def is_virtual_interface(name: str) -> bool:
    return name.lower().startswith(VIRTUAL_IFACE_PREFIXES)


def interface_addresses() -> dict[str, str]:
    """IPv4 address of every network interface, {"eth0": "192.168.1.5", "tailscale0": "100.x"}.
    Linux only (SIOCGIFADDR): there getaddrinfo(hostname) usually yields just 127.0.1.1, so a
    second network card or Tailscale would be missed. Elsewhere {} (getaddrinfo lists them all)."""
    if not sys.platform.startswith("linux"):
        return {}
    import fcntl
    import struct

    out: dict[str, str] = {}
    try:
        names = [name for _, name in socket.if_nameindex()]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for name in names:
                try:
                    packed = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
                except OSError:  # no IPv4 address on this interface
                    continue
                out[name] = socket.inet_ntoa(packed[20:24])
    except (OSError, ValueError):
        pass
    return out


def local_addresses() -> set[str]:
    """Names and IPs this computer is reachable under (for the Host allow-list and the links
    printed for the phone). Addresses of Docker/VM bridges are left out."""
    names: set[str] = set()
    try:
        hostname = socket.gethostname()
        names |= {hostname.lower(), f"{hostname.lower()}.local", socket.getfqdn().lower()}
        for info in socket.getaddrinfo(hostname, None):
            names.add(str(info[4][0]).split("%")[0])
    except OSError:
        pass
    try:  # primary LAN address: no packet is sent for a UDP "connect"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            names.add(sock.getsockname()[0])
    except OSError:
        pass
    interfaces = interface_addresses()
    names |= {ip for name, ip in interfaces.items() if not is_virtual_interface(name)}
    names -= {ip for name, ip in interfaces.items() if is_virtual_interface(name) and not is_loopback(ip)}
    return {n for n in names if n}


def is_tailscale_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in TAILSCALE_NET
    except ValueError:
        return False


def tailscale_name(timeout: float = 3.0) -> str | None:
    """This computer's MagicDNS name (my-server.tail1234.ts.net) from `tailscale status --json`,
    or None (no Tailscale CLI, logged out, MagicDNS off)."""
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=timeout,
                             check=False).stdout
        name = str((json.loads(out or "{}").get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    return name or None


def configured_hosts(extra: Iterable[str] | None) -> list[str]:
    hosts = [str(h).strip().lower() for h in (extra or []) if str(h).strip()]
    env = os.environ.get(ALLOWED_HOSTS_ENV, "")
    hosts += [h.strip().lower() for h in env.split(",") if h.strip()]
    return hosts


def trusted_hosts(bind_host: str | None, extra: Iterable[str] | None = None) -> list[str]:
    """Allowed Host header values: loopback names, configured extras (web.allowed_hosts,
    '*' = any), and — when listening on the network — this computer's names and IPs."""
    hosts = set(LOOPBACK_NAMES)
    wanted = configured_hosts(extra)
    if "*" in wanted:
        return ["*"]
    hosts |= set(wanted)
    bind = (bind_host or "").strip().strip("[]").lower()
    if not is_loopback(bind):
        hosts.add(TAILSCALE_HOSTS)  # Tailscale's own names: only devices of the tailnet resolve them
        if bind in ANY_ADDRESS:
            hosts |= local_addresses()
        else:
            hosts.add(bind)
    return sorted(hosts)


def _host_of(scope: dict[str, Any]) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"host":
            host = value.decode("latin-1").strip()
            if host.startswith("["):  # [IPv6]:port
                return host[1:host.find("]")] if "]" in host else host
            return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return ""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return True


class HostGuard:
    """Starlette's TrustedHostMiddleware, plus: with `allow_ip_literals` (network mode, where the
    access token guards every request) any Host that is a plain IP address passes. DNS rebinding
    works only through a domain name, and a home server's IP is not stable (DHCP, Tailscale
    coming up after the app, Docker)."""

    def __init__(self, app: Any, allowed_hosts: list[str], allow_ip_literals: bool = False) -> None:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        self.app = app
        self.allow_ip_literals = allow_ip_literals
        self.trusted = TrustedHostMiddleware(app, allowed_hosts=allowed_hosts)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.allow_ip_literals and scope.get("type") in ("http", "websocket") and _is_ip_literal(_host_of(scope)):
            await self.app(scope, receive, send)
            return
        await self.trusted(scope, receive, send)


def ensure_token(data_dir: str | Path) -> str:
    """The persistent access token (created on first use)."""
    path = Path(data_dir) / TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # Windows / odd filesystems
            pass
    except OSError as exc:
        log.warning("Cannot store the web access token in %s: %s (it changes on every start)", path, exc)
    return token


def token_matches(supplied: str | None, token: str) -> bool:
    return bool(supplied) and secrets.compare_digest(str(supplied).encode(), token.encode())


def lan_urls(port: int, token: str | None = None) -> list[str]:
    """http://192.168.x.y:8000/?token=... for printing at startup (home network first, then Tailscale)."""
    ips = sorted((a for a in local_addresses() if _is_lan_ip(a)), key=lambda a: (is_tailscale_ip(a), a))
    query = f"/?{TOKEN_PARAM}={token}" if token else "/"
    return [f"http://{ip}:{port}{query}" for ip in ips]


def _is_lan_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and not ip.is_loopback and not ip.is_link_local


__all__ = [
    "COOKIE_NAME",
    "TOKEN_HEADER",
    "TOKEN_PARAM",
    "HostGuard",
    "ensure_token",
    "interface_addresses",
    "is_loopback",
    "is_tailscale_ip",
    "lan_urls",
    "tailscale_name",
    "trusted_hosts",
    "token_matches",
]
