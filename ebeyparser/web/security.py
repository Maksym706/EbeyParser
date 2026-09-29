"""Who may open the web UI.

* Host header allow-list (Starlette TrustedHostMiddleware) against DNS rebinding:
  a foreign website must not be able to talk to http://localhost:8000 through a
  domain of its own.
* When the server listens on the network (web.host: 0.0.0.0 or a LAN address),
  an access token is required: generated once, stored in data/web_token.txt,
  accepted as ?token=... (then remembered in a cookie) or X-EbeyParser-Token header.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import socket
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
ANY_ADDRESS = ("0.0.0.0", "::", "")
TOKEN_FILE = "web_token.txt"
TOKEN_PARAM = "token"
TOKEN_HEADER = "x-ebeyparser-token"
COOKIE_NAME = "ebp_token"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookies at ~400 days
ALLOWED_HOSTS_ENV = "EBEYPARSER_ALLOWED_HOSTS"  # comma-separated, until config has web.allowed_hosts


def is_loopback(host: str | None) -> bool:
    """True for localhost / 127.x / ::1 — the server is reachable from this computer only."""
    name = (host or "").strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def local_addresses() -> set[str]:
    """Names and IPs this computer is reachable under (for the Host allow-list)."""
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
    return {n for n in names if n}


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
        if bind in ANY_ADDRESS:
            hosts |= local_addresses()
        else:
            hosts.add(bind)
    return sorted(hosts)


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
    """http://192.168.x.y:8000/?token=... for printing at startup."""
    ips = sorted(a for a in local_addresses() if _is_lan_ip(a))
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
    "ensure_token",
    "is_loopback",
    "lan_urls",
    "trusted_hosts",
    "token_matches",
]
