"""Docker HEALTHCHECK for EbeyParser: is the web panel answering?

Standard library + PyYAML only (no app import: this runs every minute on a small server).
Reads web.host / web.port and the access key from the config the container runs with
(EBEYPARSER_CONFIG, default /app/data/config.yaml) and asks the cheap /api/health?ai=0.
Exit code 0 = healthy, 1 = not.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("EBEYPARSER_CONFIG", "/app/data/config.yaml"))
ANY = {"", "0.0.0.0", "::", "localhost", "127.0.0.1", "::1"}


def _settings() -> tuple[str, int, Path]:
    host, port, data_dir = "0.0.0.0", 8000, "data"
    try:
        import yaml

        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8-sig")) or {}
        web = raw.get("web") or {}
        host = str(web.get("host") or host).strip()
        port = int(web.get("port") or port)
        data_dir = str((raw.get("general") or {}).get("data_dir") or data_dir)
    except Exception:  # noqa: BLE001 - no config yet (first start) or unreadable: defaults
        pass
    return host, port, Path(data_dir)


def main() -> int:
    host, port, data_dir = _settings()
    target = "127.0.0.1" if host.strip("[]") in ANY else host.strip("[]")
    if ":" in target:
        target = f"[{target}]"
    headers = {}
    token_file = data_dir / "web_token.txt"
    if token_file.is_file():
        headers["X-EbeyParser-Token"] = token_file.read_text(encoding="utf-8").strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    for path in ("/api/health?ai=0", "/api/v1/health?ai=0"):
        request = urllib.request.Request(f"http://{target}:{port}{path}", headers=headers)
        try:
            with opener.open(request, timeout=8) as response:  # noqa: S310 - local URL
                ok = response.status == 200 and json.loads(response.read() or b"{}") is not None
                return 0 if ok else 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            print(f"unhealthy: HTTP {exc.code} from {path}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"unhealthy: {exc}")
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
