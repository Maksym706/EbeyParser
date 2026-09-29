"""Shared fakes and app builders for the /api/v1 tests (no network anywhere)."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from ebeyparser.config import AppConfig, load_config
from ebeyparser.db import Database
from ebeyparser.demo import seed_demo
from ebeyparser.models import DealView, Evaluation, Listing, PriceEstimate, RunSummary, utcnow
from ebeyparser.web.app import create_app

LOCAL = "http://localhost"
RTX = "2894398213"  # demo: buy, 4.8 km, shipping possible
IPHONE_SKIP = "2893876502"  # demo: skip
MACMINI = "2894311780"  # demo: starred
EBAY = "ebay-306512349871"
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL",
              "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
              "AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "EBEYPARSER_ALLOWED_HOSTS")
BOT_TOKEN = "123456789:AAHfakeTokenFakeTokenFake_12345"

CONFIG = """# мой конфиг — комментарии должны пережить запись
general:
  interval_minutes: 30   # как часто проверять
  data_dir: {data}
pricing:
  min_profit: 40         # прибыль
ai:
  enabled: false
  provider: openai
  base_url: http://localhost:1234/v1
  model: qwen/qwen2.5-vl-7b
notifications:
  min_score: 70
  email:
    enabled: false
    username: ${{SMTP_USER}}
    password: ${{SMTP_PASSWORD}}
    from_addr: ${{SMTP_USER}}
    to_addrs: ${{NOTIFY_EMAIL}}
  telegram:
    enabled: false
    bot_token: ${{TELEGRAM_BOT_TOKEN}}
    chat_id: ${{TELEGRAM_CHAT_ID}}
ebay:
  client_id: ${{EBAY_CLIENT_ID}}
  client_secret: ${{EBAY_CLIENT_SECRET}}
web:
  host: 127.0.0.1
  port: 8000
searches: []
"""


class FakeMonitor:
    """Duck-typed Monitor: records calls, emits events through on_event like the real one."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db
        self.on_event: list[Any] = []
        self.is_running = False
        self.paused = False
        self.progress: dict[str, Any] | None = None
        self.next_run_at = utcnow() + timedelta(minutes=12)
        self.last_summary: RunSummary | None = None
        self.calls = 0
        self.release = threading.Event()
        self.release.set()
        self.updated: AppConfig | None = None
        self.evaluated: list[tuple[str, dict[str, Any]]] = []
        self.health: dict[str, Any] = {"ok": True, "server_ok": True, "model_available": True,
                                       "provider": "openai", "model": "qwen/qwen2.5-vl-7b", "error": None}

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        for hook in list(self.on_event):
            hook(kind, data)

    async def run_once(self) -> RunSummary:
        self.calls += 1
        self.is_running = True
        self._emit("run_started", {"run_id": 99, "searches": 1})
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        finally:
            self.is_running = False
        self.last_summary = RunSummary(finished_at=utcnow(), new_listings=2, deals_found=1)
        self._emit("run_finished", self.last_summary.model_dump(mode="json"))
        return self.last_summary

    async def run_forever(self, stop: asyncio.Event) -> None:
        await stop.wait()

    def pause(self) -> None:
        self.paused = True
        self.next_run_at = None
        self._emit("monitor_paused", {"paused": True})

    def resume(self) -> None:
        self.paused = False
        self._emit("monitor_resumed", {"paused": False})

    async def ai_health(self) -> dict[str, Any]:
        return dict(self.health)

    def update_config(self, config: AppConfig) -> None:
        self.updated = config

    def http_status(self) -> dict[str, Any]:
        return {"www.kleinanzeigen.de": {"requests_last_hour": 30, "limit_per_hour": 150, "images_last_hour": 4,
                                         "blocked": False, "strikes": 0}}

    def backlog_status(self) -> dict[str, int]:
        return {"pending": 3, "expired_24h": 1}

    def _store(self, ad_id: str, url: str, purpose: str) -> DealView:
        assert self.db is not None
        listing = Listing(ad_id=ad_id, url=url, title="Sony WH-1000XM4 Kopfhörer", price=120.0, negotiable=True,
                          image_urls=["https://img.example/1.jpg"], search_name="Ручная проверка", detail_loaded=True)
        self.db.upsert_listing(listing)
        self.db.save_evaluation(Evaluation(
            ad_id=ad_id, purpose=purpose, buy_price=120.0, verdict="buy", action="haggle", offer_price=100.0,
            max_buy_price=130.0, expected_profit=45.0, roi=0.37, score=81.0, stage="full",
            estimate=PriceEstimate(market_price=190.0, low=170.0, high=210.0, sample_size=9, source="kleinanzeigen"),
            reasons=["Цена ниже рынка"]))
        deal = self.db.get_deal(ad_id)
        assert deal is not None
        return deal

    async def evaluate_url(self, url: str, *, purpose: str = "resale", target_price: float | None = None,
                           progress: Any = None) -> DealView:
        from ebeyparser.monitor import ad_id_from_url

        self.evaluated.append((url, {"purpose": purpose, "target_price": target_price}))
        if progress:
            progress("market", "Ищу цены похожих…")
            progress("ai", "Нейросеть смотрит фото…")
        await asyncio.sleep(0)
        if "fail" in url:
            raise ValueError("Объявление удалено")
        return self._store(ad_id_from_url(url) or "x", url, purpose)

    async def evaluate_listing(self, listing: Listing, search: Any, *, progress: Any = None) -> Evaluation:
        self.evaluated.append((listing.ad_id, {"search": search.name}))
        if progress:
            progress("ai", "Нейросеть смотрит фото…")
        evaluation = Evaluation(ad_id=listing.ad_id, verdict="maybe", score=55.0, stage="full",
                                reasons=["Переоценено"])
        assert self.db is not None
        self.db.save_evaluation(evaluation)
        return evaluation


def clean_environ() -> dict[str, str]:
    saved = dict(os.environ)
    for key in SECRET_ENV:
        os.environ.pop(key, None)
    return saved


def restore_environ(saved: dict[str, str]) -> None:
    os.environ.clear()
    os.environ.update(saved)


def make_app(tmp_path: Path, *, monitor: Any = "fake", seed: bool = True, config_text: str | None = None,
             **kwargs: Any) -> tuple[Any, AppConfig, Database, Path, Any]:
    path = tmp_path / "config.yaml"
    path.write_text((config_text or CONFIG).format(data=tmp_path / "data"), encoding="utf-8")
    config = load_config(path)
    db = Database()
    if seed:
        seed_demo(db)
    mon = FakeMonitor(db) if monitor == "fake" else monitor
    app = create_app(config, db, config_path=path, monitor=mon, **kwargs)
    return app, config, db, path, mon


def events_of(app: Any, kind: str | None = None) -> list[Any]:
    return [e for e in app.state.api.hub.recent(200) if kind is None or e.type == kind]


def wait_job(client: Any, job_id: str, tries: int = 200) -> dict[str, Any]:
    import time

    for _ in range(tries):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")
