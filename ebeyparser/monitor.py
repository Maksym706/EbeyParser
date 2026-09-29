"""The monitoring pipeline: a request-budgeted funnel.

For every enabled search: scrape results -> store -> remember their prices (price history)
-> for new, deferred, price-dropped (and re-checked) ads:

  free, for every ad:   prefilter, general.min_listing_price, reference price / own price
                        history -> "no deal by market data" ends here (no request, no AI);
  paid, best first:     ad page -> prefilter on the full text -> comparables (only while the
                        market is unknown) -> local vision LLM -> score -> notify at once.

Comparables lookups, ad pages and AI calls have per-pass budgets; an ad that hits an
exhausted budget is deferred (left without evaluation) and picked up by the next pass.
The first pass of a new search only learns prices (general.baseline_first_run).
The single-ad check (evaluate_url) runs every step without budgets.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import OrderedDict
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .ai import scout as scout_ai
from .ai.claude import make_llm
from .ai.evaluator import AIEvaluator, same_variant_comparables
from .ai.prompts import MAX_PROMPT_COMPARABLES, prompt_comparables
from .ai.triage import TriageEngine, TriageItem, blind_spot, scout_priority
from .config import AppConfig, GeneralConfig, LLMSettings, SearchConfig
from .db import Database
from .models import AIVerdict, Comparable, DealView, Evaluation, Listing, PriceEstimate, RunSummary, utcnow
from .notify.base import build_notifiers
from .notify.render import super_title
from .pricing.estimator import (
    below_min_price,
    comparable_fits,
    estimate_from_comparables,
    estimate_from_history,
    evaluate,
    find_reference_price,
    history_key_for,
    history_lookup_key,
    listing_identity,
    market_says_no_deal,
    no_deal_reasons,
    prefilter,
    prefilter_score,
)
from .pricing.ai_key import ProductRef, ai_keys_compatible
from .pricing.identity import Identity, ProductKey, identify
from .pricing.tiers import TIER_SUPER, deal_tier
from .pricing.text import SEVERE_FLAGS, detect_red_flags, is_model_key, make_search_query, normalize
from .errors_ru import AI_DOWN_RU, ai_problem, budget_message, humanize
from .scraper.ebay_api import EBAY_NOT_CONNECTED_RU, EbayAPIError, EbayBrowseClient
from .scraper.ebay_sold import EbaySoldScraper
from .scraper.http import BlockedError, PoliteClient, RateBudgetExceeded
from .scraper.kleinanzeigen import BASE_URL as KA_BASE_URL
from .scraper.kleinanzeigen import KleinanzeigenScraper, PageLayoutError
from .timefmt import apply_config as apply_timezone
from .timefmt import at_label, now_local, when_label

log = logging.getLogger(__name__)

COMPS_CACHE_TTL = 6 * 3600  # seconds
COMPS_CACHE_MAX = 2000  # estimates kept in memory (least recently used ones go first)
LISTING_RETENTION_DAYS = 60  # skipped listings not seen for this long are forgotten ...
RUN_RETENTION_DAYS = 90  # ... and so are old run summaries
PRICE_DROP_RATIO = 0.9  # re-evaluate a known ad when its price falls by 10 %+
PENDING_MAX_AGE = timedelta(days=2)  # older deferred ads are probably gone: not worth a request
PENDING_MIN_BATCH = 50  # stored-but-unevaluated ads pulled back per search and pass (at least)
REFERENCE_CHECK_MIN_POINTS = 8  # history this big may contradict a configured reference price ...
REFERENCE_CHECK_TOLERANCE = 0.3  # ... when it is more than 30 % away
RECHECK_AFTER = timedelta(hours=6)  # an ad skipped by market data is re-checked when seen again
# early "no deal" from our own price history needs a solid sample of a concrete model
EARLY_SKIP_MIN_POINTS = 8
EARLY_SKIP_MAX_SPREAD = 0.35
UNKNOWN_DISCOUNT = 0.75  # candidates with an unknown market rank after clear bargains
HEALTH_ALERT_EVERY = timedelta(hours=6)  # the same service alert at most this often
MAX_DELIVERY_ATTEMPTS = 5  # a channel that fails gets the deal again on later passes ...
DELIVERY_RETRY_WINDOW = timedelta(hours=24)  # ... within this time
REPOST_WINDOW = timedelta(days=14)  # same seller + product + price already sent: no second alert
REPOST_PRICE_TOLERANCE = 0.05
CATEGORY_MIN_PAGES = 3  # category scans page on until the first already-known ad
_COMPS_SOURCES = frozenset({"ebay_sold", "mixed", "kleinanzeigen", "history"})  # estimates from offers

# RunSummary counters of the funnel, and which GeneralConfig limit caps each paid step
_FUNNEL_COUNTERS = (
    "prefiltered", "early_skips", "history_hits", "comps_lookups", "details_fetched", "ai_calls", "deferred",
)
_BUDGET_LIMITS = {
    "comps_lookups": "max_comps_lookups_per_run",
    "details_fetched": "max_details_per_run",
    "ai_calls": "max_ai_per_run",
}
_BUDGET_RU = {"comps_lookups": "поисков аналогов", "details_fetched": "страниц объявлений", "ai_calls": "вызовов ИИ"}
# price filter inside a pasted Kleinanzeigen URL: /preis:100:400/ or ?minPrice=100
_URL_PRICE_FILTER_RE = re.compile(r"preis:\d|preis::\d|[?&](?:minPrice|maxPrice)=\d", re.IGNORECASE)
# Event hooks (web UI live updates): on_event(kind, data) with kind one of run_started,
# run_finished, deal_found, health_alert, monitor_paused, monitor_resumed.
EventHook = Callable[[str, dict[str, Any]], Any]
PAUSED_STATE_KEY = "monitor:paused"  # kv_state: "1" = passes are skipped (survives restarts)
SCOUT_STATS_KEY = "scout:stats"  # kv_state: the AI scout's last throughput snapshot (JSON)
scout_feedback_hints = scout_ai.feedback_hints
scout_status_view = scout_ai.status_view
DEAL_EVENT_ACTIONS = frozenset({"buy", "haggle", "bid"})


class AlreadyRunningError(RuntimeError):
    pass


@dataclass
class _Candidate:
    """An ad that survived the free part of the funnel."""

    listing: Listing
    estimate: PriceEstimate | None  # reference / history market price; None = look up comparables
    hint: PriceEstimate | None = None  # any price known so far (only to rank candidates)
    # AI scout (docs/design/AI_SCOUT.md): its priced reading of the ad, who made it a candidate,
    # its rank when the market is unknown, and what the script alone would have stored
    plan: Any = None  # ai.scout.ScoutPlan
    found_by: str = "script"
    priority: float | None = None
    script_skip: Evaluation | None = None

    @property
    def discount(self) -> float:
        """Buy cost / market price: lower = more promising. Free ads first. Without a market
        price the AI scout's interest decides (junk after everything else)."""
        listing = self.listing
        if listing.is_free:
            return 0.0
        if self.priority is not None and (self.estimate is None or self.found_by == scout_ai.FOUND_BY_SCOUT):
            return self.priority
        est = self.estimate or self.hint
        if listing.price is None or listing.price <= 1 or est is None or not est.market_price:
            return UNKNOWN_DISCOUNT
        return (listing.price + (listing.shipping_cost or 0.0)) / est.market_price


@dataclass(frozen=True)
class _Target:
    """What is being priced and how comparables must match it."""

    query: str  # search phrase for comparables ("iphone 13 128gb")
    ref: str | ProductKey  # comparable_fits() reference: the ad's title (its kind counts) or a key
    history_key: str | None  # stored-key prefix in the price history ("iphone|13", "bundle:playstation|5")
    kind: str = "item"

    @property
    def cache_key(self) -> str:
        """Same search phrase + same product/kind being matched. "DeWalt DCD796 solo" and the kit
        share the phrase "dewalt dcd796" but must never share an estimate."""
        if isinstance(self.ref, ProductKey):
            match = self.ref.key()
        else:
            try:
                key = identify(self.ref).key
            except Exception:  # noqa: BLE001
                key = None
            match = key.key() if key is not None else normalize(self.ref)
        return f"{normalize(self.query)}|{self.kind}|{match}"


# kinds of offer that are not the product itself (identity.classify_kind)
_KIND_SKIP_RU = {
    "wanted": "Это объявление о поиске/покупке, а не продажа",
    "swap": "Только обмен, не продажа",
    "service": "Это услуга, а не товар",
    "box_only": "Только коробка, без самого товара",
    "defect": "Неисправный товар — не для перепродажи",
    "part": "Запчасть или некомплект — не целый товар",
    "accessory": "Аксессуар, а не сам товар",
    "complete_pc": "Целый компьютер, а не отдельный товар",
    "laptop": "Это ноутбук, а не отдельная комплектующая",
}
_ADDON_KINDS = frozenset({"part", "accessory"})


class _Deferred(Exception):
    """A budget of this pass is used up: the listing stays without evaluation (= pending)
    and the next pass picks it up."""


@dataclass
class _Budget:
    """Network/AI allowance of one monitoring pass (shared by all searches), plus the
    per-search cap general.max_new_per_search on listings that may use it."""

    summary: RunSummary
    general: GeneralConfig
    search_left: int | None = None  # None = no per-search cap
    _charged: bool = False  # the current listing already counts against search_left

    def start_search(self) -> None:
        cap = self.general.max_new_per_search
        self.search_left = cap if cap > 0 else None

    def start_listing(self) -> None:
        self._charged = False

    def check(self, kind: str) -> bool:
        """True: go ahead. False: the step is switched off (its limit is 0).
        Raises _Deferred when the pass (or this search) has used up its allowance."""
        limit = getattr(self.general, _BUDGET_LIMITS[kind])
        if limit <= 0:
            return False
        if not self._charged and self.search_left is not None and self.search_left <= 0:
            raise _Deferred(f"исчерпан лимит поиска — {self.general.max_new_per_search} объявлений за проверку")
        if getattr(self.summary, kind) >= limit:
            raise _Deferred(f"исчерпан лимит {_BUDGET_RU[kind]} за эту проверку ({limit})")
        return True

    def spend(self, kind: str) -> bool:
        if not self.check(kind):
            return False
        if not self._charged:
            self._charged = True
            if self.search_left is not None:
                self.search_left -= 1
        setattr(self.summary, kind, getattr(self.summary, kind) + 1)
        return True


def ad_id_from_url(url: str) -> str | None:
    """'.../s-anzeige/rtx-3090/2891234567-225-3331' -> '2891234567',
    'https://www.ebay.de/itm/123456789012' -> 'ebay-123456789012'."""
    if "ebay." in url:
        match = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", url)
        return f"ebay-{match.group(1)}" if match else None
    match = re.search(r"/s-anzeige/[^/]+/(\d+)", url) or re.search(r"(\d{8,})", url)
    return match.group(1) if match else None


class Monitor:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        scraper: KleinanzeigenScraper | None = None,
        ebay: EbaySoldScraper | None = None,
        ebay_api: EbayBrowseClient | None = None,
        evaluator: AIEvaluator | None = None,
        second_evaluator: AIEvaluator | None = None,
        notifiers: list[Any] | None = None,
        web_base_url: str | None = None,
        scout: TriageEngine | None = None,
    ):
        self.config = config
        self.db = db
        self.web_base_url = web_base_url
        apply_timezone(config)
        self._client: PoliteClient | None = None
        self._scraper = scraper
        self._ebay = ebay
        self._ebay_api = ebay_api
        self._evaluator = evaluator
        self._second = second_evaluator
        self._llm: Any = None
        self._second_llm: Any = None
        self._notifiers = notifiers
        self._injected = {
            "scraper": scraper is not None,
            "ebay": ebay is not None,
            "ebay_api": ebay_api is not None,
            "evaluator": evaluator is not None,
            "second": second_evaluator is not None,
            "notifiers": notifiers is not None,
            "scout": scout is not None,
        }
        # AI scout (docs/design/AI_SCOUT.md): a small text model reads every new ad first
        self._scout = scout
        self._scout_llm: Any = None
        self._scout_items: dict[str, TriageItem] = {}  # this pass: ad id -> the scout's reading
        self._scout_plans: dict[str, Any] = {}  # ad id -> ai.scout.ScoutPlan (priced reading)
        self._scout_deadline: float | None = None  # engine clock: end of this pass's scout time
        self._scout_searches_left = 0
        self._scout_hints_text: str | None = None
        self._scout_down_reported = False
        self._second_calls = 0
        self._rate_limited: dict[str, datetime] = {}  # host -> retry_at, this pass
        self._budget: _Budget | None = None
        self._comps_cache: OrderedDict[str, tuple[float, PriceEstimate]] = OrderedDict()
        self._ai_answered = 0  # AI calls of this pass that got a real answer ...
        self._ai_failed = 0  # ... and that failed (LM Studio down)
        self._upkeep_done = False
        self._ebay_blocked = False
        self._running = False
        self.last_summary: RunSummary | None = None
        self.next_run_at = None
        self.on_event: list[EventHook] = []  # live-update hooks, see EventHook
        self._wake: asyncio.Event | None = None  # interrupts run_forever's wait (pause/resume)
        self._event_alerts: dict[str, datetime] = {}  # health_alert events: kind -> last emitted
        self._paused = self._load_paused()
        self.progress: dict[str, Any] | None = None  # current pass: search N of M (run_progress)
        self._stage_cb: Callable[[str, str], Any] | None = None  # single-ad check: step reporter

    # ------------------------------------------------------------ lifecycle
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        """Automatic passes are skipped while paused (a manual run_once still works)."""
        return self._paused

    def pause(self) -> None:
        """Skip automatic passes until resume(); the current pass (if any) finishes. Persisted."""
        if self._paused:
            return
        self._paused = True
        self._store_paused()
        self.next_run_at = None
        self._poke()
        self._emit("monitor_paused", {"paused": True})

    def resume(self) -> None:
        """Automatic passes again; an overdue pass starts right away."""
        if not self._paused:
            return
        self._paused = False
        self._store_paused()
        self._poke()
        self._emit("monitor_resumed", {"paused": False})

    def _load_paused(self) -> bool:
        try:
            state = self.db.get_state(PAUSED_STATE_KEY)
        except (sqlite3.Error, AttributeError):
            return False
        return bool(state and state[0] == "1")

    def _store_paused(self) -> None:
        try:
            self.db.set_state(PAUSED_STATE_KEY, "1" if self._paused else "0")
        except (sqlite3.Error, AttributeError) as exc:
            log.warning("Pause state not saved: %s", exc)

    def _poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        """Call every on_event hook; a failing hook never breaks monitoring."""
        for hook in list(self.on_event):
            try:
                result = hook(kind, data)
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001
                log.exception("Event hook failed (%s)", kind)

    def update_config(self, config: AppConfig) -> None:
        """Apply a new config; network/AI components are rebuilt lazily."""
        old_general = self.config.general
        self.config = config
        apply_timezone(config)
        self._comps_cache.clear()
        # compare with what the client was built from: the web UI changes the shared config in place
        built_from = getattr(self, "_client_settings", None) or _request_settings(old_general)
        if self._client is not None and built_from != _request_settings(config.general):
            self._client = _close_soon(self._client)  # new delays / limits / user agent
            if not self._injected["scraper"]:
                self._scraper = None
            if not self._injected["ebay"]:
                self._ebay = None
        if not self._injected["notifiers"]:
            self._notifiers = None
        if not self._injected["evaluator"]:
            self._evaluator = None
            self._llm = _close_soon(self._llm)
        if not self._injected["second"]:
            self._second = None
            self._second_llm = _close_soon(self._second_llm)
        if not self._injected["ebay_api"]:
            self._ebay_api = _close_soon(self._ebay_api)
        if not self._injected["scout"]:
            self._scout = None
            self._scout_llm = _close_soon(self._scout_llm)

    def _scout_llm_settings(self) -> LLMSettings:
        """The scout's endpoint: its own (the always-on small model) or, left empty, the ai one."""
        ai, sc = self.config.ai, self.config.ai.scout
        own = bool(sc.base_url.strip())
        return LLMSettings(
            provider=sc.provider if own else ai.provider,
            base_url=sc.base_url.strip() if own else ai.base_url,
            model=(sc.model or "").strip() or ai.model,
            api_key=(sc.api_key if own else ai.api_key) or "",
            max_images=0,
            timeout_seconds=sc.timeout_seconds,
            temperature=sc.temperature,
            max_tokens=sc.max_tokens,
        )

    @property
    def scout_enabled(self) -> bool:
        return self._scout is not None and (self._injected["scout"] or self.config.ai.scout.enabled)

    def _ensure_components(self) -> None:
        if self._client is None and not (self._injected["scraper"] and self._injected["ebay"]):
            # cooldowns after blocks and the hourly page window survive restarts
            self._client = PoliteClient.from_config(
                self.config.general, state_path=self.config.data_path / "http_state.json"
            )
            self._client_settings = _request_settings(self.config.general)
        if self._scraper is None:
            self._scraper = KleinanzeigenScraper(self._client, debug_dir=self.config.data_path / "debug")
        if self._ebay is None:
            self._ebay = EbaySoldScraper(self._client)
        if self._ebay_api is None and self.config.ebay.configured:
            self._ebay_api = EbayBrowseClient(
                self.config.ebay, timeout=self.config.general.request_timeout_seconds
            )
        ai = self.config.ai
        if self._evaluator is None and ai.enabled and not self._injected["evaluator"]:
            self._llm = make_llm(ai)
            self._evaluator = AIEvaluator(self._llm, ai)
        so = ai.second_opinion
        if self._second is None and so.enabled and not self._injected["second"]:
            try:
                self._second_llm = make_llm(so)
                self._second = AIEvaluator(self._second_llm, so)
            except Exception as exc:  # e.g. anthropic package missing
                log.warning("Second opinion disabled: %s", exc)
        sc = ai.scout
        if self._scout is None and sc.enabled and not self._injected["scout"]:
            try:
                settings = self._scout_llm_settings()
                self._scout_llm = make_scout_llm(settings)
                self._scout = TriageEngine.from_config(self._scout_llm, sc.model_copy(update={"model": settings.model}))
            except Exception as exc:  # e.g. anthropic package missing: the script path works as before
                log.warning("AI scout disabled: %s", exc)
        if self._notifiers is None:
            self._notifiers = build_notifiers(
                self.config.notifications, web_base_url=self.web_base_url
            )

    async def aclose(self) -> None:
        for name in ("_llm", "_second_llm", "_scout_llm", "_ebay_api"):
            obj = getattr(self, name)
            if obj is not None:
                await obj.aclose()
                setattr(self, name, None)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def http_status(self) -> dict[str, Any]:
        """Per host: pages in the last hour, hourly limit, cooldown after a block (status page)."""
        return self._client.host_status() if self._client is not None else {}

    async def ai_health(self) -> dict[str, Any]:
        ai = self.config.ai
        base = {"provider": ai.provider, "base_url": ai.base_url, "model": ai.model}
        if not ai.enabled:
            return {**base, "ok": False, "enabled": False, "model_available": False,
                    "error": "Нейросеть выключена в настройках"}
        try:
            llm = self._llm or make_llm(ai)
        except Exception as exc:
            return {**base, "ok": False, "enabled": True, "model_available": False, "error": str(exc)}
        try:
            result = await llm.health()
        finally:
            if llm is not self._llm:
                await llm.aclose()
        so = ai.second_opinion
        second = {"enabled": so.enabled, "provider": so.provider, "model": so.model}
        return {**base, "enabled": True, **result, "second_opinion": second}

    # --------------------------------------------------------------- running
    async def run_forever(self, stop: asyncio.Event) -> None:
        """A pass every general.interval_minutes (±10 %) until `stop`; passes are skipped while
        paused, and after resume() an overdue pass starts at once."""
        due: datetime | None = None
        while not stop.is_set():
            if self._paused:
                self.next_run_at = None
                await self._idle(stop, None)
                continue
            if due is not None:
                left = (due - utcnow()).total_seconds()
                if left > 0:
                    self.next_run_at = due
                    await self._idle(stop, left)
                    continue
            try:
                await self.run_once()
            except AlreadyRunningError:
                pass
            except Exception:  # never let the loop die
                log.exception("Monitoring run crashed")
            interval = max(1.0, self.config.general.interval_minutes) * 60
            interval *= random.uniform(0.9, 1.1)  # don't hit the site on an exact beat
            due = self._after_cooldown(utcnow() + timedelta(seconds=interval))
            self.next_run_at = due
        self.next_run_at = None

    def kleinanzeigen_cooldown_end(self) -> datetime | None:
        """When Kleinanzeigen's block cooldown ends (None = not cooling down)."""
        client = self._client
        if client is None:
            return None
        try:
            remaining = client.cooldown_remaining(KA_BASE_URL)
        except Exception:  # noqa: BLE001 - a test double without the method
            return None
        return utcnow() + timedelta(seconds=remaining) if remaining > 0 else None

    def _after_cooldown(self, due: datetime) -> datetime:
        """Only Kleinanzeigen searches and the site asked for a pause: the next pass right after
        the pause, not a pass that would only meet it (the UI says "продолжу в 21:34")."""
        end = self.kleinanzeigen_cooldown_end()
        enabled = [s for s in self.config.searches if s.enabled]
        if end is None or end <= due or not enabled or any(s.source != "kleinanzeigen" for s in enabled):
            return due
        return end + timedelta(seconds=random.uniform(30, 120))

    async def _idle(self, stop: asyncio.Event, timeout: float | None) -> None:
        """Wait for `stop`, a pause/resume poke or `timeout` seconds (None = no timeout)."""
        if self._wake is None:
            self._wake = asyncio.Event()
        wake = self._wake
        waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(wake.wait())]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        wake.clear()

    async def run_once(self) -> RunSummary:
        if self._running:
            raise AlreadyRunningError("Проверка уже идёт")
        searches = [s for s in self.config.searches if s.enabled]
        if not searches:
            summary = RunSummary(finished_at=utcnow())
            _add_error(summary, "Нет активных поисков — добавь их на странице «Поиски»")
            self.last_summary = summary
            self._emit("run_finished", summary.model_dump(mode="json"))
            return summary

        self._running = True
        self._ebay_blocked = False
        self._rate_limited = {}
        self._second_calls = 0
        self._ai_answered = self._ai_failed = 0
        summary = self.db.start_run()
        digest: list[DealView] = []
        self._emit("run_started", {"run_id": summary.id, "started_at": summary.started_at.isoformat(),
                                   "searches": len(searches)})
        try:
            self._ensure_components()
            self._upkeep()
            self._prune_history()
            self._scout_begin_pass(len(searches))
            await self._retry_deliveries(summary)
            await self._drain_vision_queue(summary)
            for search in searches:
                summary.searches += 1
                self.progress = {"run_id": summary.id, "index": summary.searches, "total": len(searches),
                                 "search_name": search.name, "started_at": summary.started_at.isoformat()}
                self._emit("run_progress", dict(self.progress))
                seen_before = summary.listings_seen
                try:
                    deals = await self._process_search(search, summary)
                except BlockedError as exc:
                    if getattr(exc, "cooling_down", False):  # no request was sent this time
                        _add_error(summary, f"Kleinanzeigen попросил паузу — продолжу сам{_until_text(exc)}"
                                   if _until_text(exc) else "Kleinanzeigen попросил паузу — продолжу сам позже",
                                   f"{exc}{_minutes_left(exc)}")
                        log.info("Kleinanzeigen still cooling down after a block: %s", exc)
                    else:
                        _add_error(summary, "Kleinanzeigen временно ограничил запросы — делаю паузу и продолжу сам"
                                   f"{_until_text(exc)}. Чтобы это случалось реже, проверяй реже", str(exc))
                        log.warning("Blocked by Kleinanzeigen: %s", exc)
                        await self._health("blocked", f"⚠ Kleinanzeigen попросил паузу — продолжу сам{_until_text(exc)}."
                                           " Ничего делать не нужно.", summary)
                    break
                except RateBudgetExceeded as exc:  # our own hourly cap: not a block, try next pass
                    self._note_rate_limit(exc, summary)
                    continue
                except PageLayoutError as exc:
                    log.warning("Search %r: %s", search.name, exc)
                    _add_error(summary, f"{search.name}: {exc.message_ru}", str(exc))
                    await self._check_search_health(search, 0, summary, error=str(exc))
                    continue
                except EbayAPIError as exc:
                    log.warning("Search %r: %s", search.name, exc)
                    _add_exc(summary, search.name, exc, "ebay")
                    continue
                except httpx.HTTPError as exc:
                    log.warning("Search %r: network error %s", search.name, exc)
                    _add_exc(summary, search.name, exc, search.source)
                    continue
                except Exception as exc:
                    log.exception("Search %r failed", search.name)
                    _add_exc(summary, search.name, exc, search.source)
                    continue
                await self._check_search_health(search, summary.listings_seen - seen_before, summary)
                digest.extend(deals)  # instant mode: already sent while evaluating
            digest.extend(await self._scout_rescue(summary))
            digest = [d for d in digest if not self._is_repost(d.listing)]
            if digest:
                summary.notified += await self._notify(digest, summary)
            await self._after_run(summary)
        finally:
            summary.finished_at = utcnow()
            self._scout_save_stats(summary)
            self.db.finish_run(summary)
            self.last_summary = summary
            self._running = False
            self.progress = None
            self._emit("run_finished", summary.model_dump(mode="json"))
        log.info(
            "Run finished: %d new, %d evaluated (%d early skips, %d from history), %d deals, %d notified,"
            " %d deferred; %d comps lookups, %d ad pages, %d AI calls; %d errors",
            summary.new_listings, summary.evaluated, summary.early_skips, summary.history_hits,
            summary.deals_found, summary.notified, summary.deferred, summary.comps_lookups,
            summary.details_fetched, summary.ai_calls, len(summary.errors),
        )
        return summary

    def _source_for(self, search_or_listing: SearchConfig | Listing) -> Any:
        """Kleinanzeigen scraper or eBay API client, depending on the source."""
        if search_or_listing.source == "ebay":
            if self._ebay_api is None:
                raise EbayAPIError(
                    "Для поиска по eBay заполни ebay.client_id и ebay.client_secret в config.yaml/.env",
                    message_ru=EBAY_NOT_CONNECTED_RU, code="ebay_not_connected",
                )
            return self._ebay_api
        assert self._scraper is not None
        return self._scraper

    async def _process_search(self, search: SearchConfig, summary: RunSummary) -> list[DealView]:
        """One search: scrape, store, feed the price history, run the funnel. Instant
        notifications go out as soon as a deal is found; returns the deals for a digest."""
        listings = await self._search(search)
        summary.listings_seen += len(listings)
        before = {name: getattr(summary, name) for name in _FUNNEL_COUNTERS}
        evaluated_before = summary.evaluated

        baseline = self.config.general.baseline_first_run and not self.db.search_has_run(search.name)
        baseline_at = self.db.search_baseline_at(search.name)
        todo: list[Listing] = []
        price_dropped: set[str] = set()
        rechecks: set[str] = set()
        fresh: set[str] = set()  # first seen in this pass
        new_here = 0
        for listing in listings:
            listing.search_name = search.name
            previous = self.db.get_listing(listing.ad_id)
            self.db.upsert_listing(listing)
            if previous is None:
                new_here += 1
                fresh.add(listing.ad_id)
                todo.append(listing)
                continue
            stored = self.db.get_listing(listing.ad_id) or listing
            evaluation = self.db.get_evaluation(listing.ad_id)
            if evaluation is None:
                learned = baseline_at is not None and _aware(previous.first_seen) <= _aware(baseline_at)
                if not learned:  # deferred / run interrupted: retry it
                    todo.append(stored)
                elif _price_dropped(previous, listing):  # known from the learning pass, now cheaper
                    log.info("Цена снизилась: %s %s € → %s €", listing.title[:50], previous.price, listing.price)
                    todo.append(stored)
            elif _price_dropped_since(evaluation, listing):
                log.info("Цена снизилась: %s %s € → %s €", listing.title[:50], evaluation.buy_price, listing.price)
                todo.append(stored)
                price_dropped.add(listing.ad_id)
            elif evaluation.stage == "market" and utcnow() - _aware(evaluation.evaluated_at) >= RECHECK_AFTER:
                todo.append(stored)  # skipped by market data earlier: the history may know better now
                rechecks.add(listing.ad_id)
        summary.new_listings += new_here
        remembered = self._remember_prices(search, listings)

        if baseline:
            self.db.mark_search_run(search.name, baseline=True)
            if _price_filtered(search):
                learned = "цены не запоминаю — поиск с фильтром цены на сайте"
            else:
                learned = f"запомнил цены {remembered} из {len(listings)} объявлений"
            log.info("🎓 %s: первая проверка — %s; оценивать и присылать сделки начну со следующей проверки",
                     search.name, learned)
            return []
        self._expire_backlog(search, baseline_at, summary)
        self.db.mark_search_run(search.name)

        # deferred ads that have dropped off the result pages since
        on_page = {listing.ad_id for listing in listings}
        backlog = [item for item in self._pending(search, baseline_at) if item.ad_id not in on_page]
        todo = sorted(backlog + todo, key=lambda item: _aware(item.first_seen))  # oldest first
        log.info("🔎 %s: объявлений %d, новых %d, к оценке %d%s", search.name, len(listings), new_here,
                 len(todo) - len(rechecks), f" (из очереди {len(backlog)})" if backlog else "")

        budget = self._budget_for(summary)
        budget.start_search()
        instant = self.config.notifications.mode == "instant"
        to_notify: list[DealView] = []

        # phase 0 — the AI scout reads the new ads (bounded time; what it doesn't reach goes the usual way)
        await self._scout_read(search, todo, fresh, summary)

        # phase 1 — free: filters, reference price / price history, early "no deal"
        candidates: list[_Candidate] = []
        for listing in todo:
            recheck = listing.ad_id in rechecks
            try:
                outcome = self._triage(listing, search, budget, recheck=recheck)
            except Exception as exc:
                log.exception("Evaluating %s failed", listing.ad_id)
                _add_exc(summary, f"{search.name} / {listing.title[:40]}", exc, search.source)
                continue
            if isinstance(outcome, _Candidate):
                candidates.append(outcome)
            elif not recheck:
                summary.evaluated += 1
                log.info("   · %s — %s → %s", listing.title[:60], _price_label(listing),
                         outcome.reasons[0] if outcome.reasons else "пропустить")

        # phase 2 — requests and AI, the most promising (cheapest vs. market) first
        candidates.sort(key=lambda c: (c.discount, _aware(c.listing.first_seen)))
        for i, cand in enumerate(candidates, 1):
            listing = cand.listing
            log.info("   [%d/%d] %s — %s", i, len(candidates), listing.title[:60], _price_label(listing))
            budget.start_listing()
            try:
                evaluation = await self._finish(cand, search, budget)
            except (_Deferred, RateBudgetExceeded) as why:
                summary.deferred += 1
                if listing.ad_id in price_dropped:  # the old verdict is stale: retry at the new price
                    self.db.delete_evaluation(listing.ad_id)
                if isinstance(why, RateBudgetExceeded):
                    self._note_rate_limit(why, summary)
                log.info("         → отложено до следующей проверки: %s", why)
                continue
            except BlockedError:
                raise
            except Exception as exc:
                log.exception("Evaluating %s failed", listing.ad_id)
                _add_exc(summary, f"{search.name} / {listing.title[:40]}", exc, search.source)
                continue
            summary.evaluated += 1
            why = f" — {evaluation.reasons[0]}" if evaluation.verdict == "skip" and evaluation.reasons else ""
            log.info("         → %s, балл %.0f%s%s", _VERDICT_RU.get(evaluation.verdict, evaluation.verdict),
                     evaluation.score, _profit_note(evaluation), why)
            deal = await self._after_evaluation(listing, search, evaluation, summary, instant=instant)
            if deal is not None:
                to_notify.append(deal)

        evaluated_here = summary.evaluated - evaluated_before
        if evaluated_here >= 6 and summary_buy_share(self.db, todo) >= 0.7:
            warning = (
                f"{search.name}: «покупать» у большинства объявлений — похоже, оценка рынка завышена."
                " Проверь цены вручную."
            )
            log.warning(warning)
            _add_error(summary, warning)
        self._log_funnel(search, summary, before, evaluated_here)
        return to_notify

    async def _search(self, search: SearchConfig) -> list[Listing]:
        """Result pages of a search. Pages on only while they bring unknown ads; category
        scans (no query) get a few pages since many new ads arrive between passes."""
        source = self._source_for(search)
        max_pages = search.max_pages or self.config.general.max_pages
        if _is_category_scan(search):
            max_pages = max(max_pages, CATEGORY_MIN_PAGES)
            if search.min_price is not None or search.max_price is not None:
                # ask the site for the whole category: every price feeds the history; the price
                # range is applied here for free (prefilter)
                search = search.model_copy(update={"min_price": None, "max_price": None})
        kwargs: dict[str, Any] = {}
        if _accepts(source.search, "seen"):
            kwargs["seen"] = lambda ad_id: self.db.get_listing(ad_id) is not None
        return await source.search(search, max_pages=max_pages, **kwargs)

    def _note_rate_limit(self, exc: RateBudgetExceeded, summary: RunSummary) -> None:
        """Our own hourly page cap for a host is used up: say so once per pass."""
        host = getattr(exc, "host", "") or "?"
        if host in self._rate_limited:
            return
        self._rate_limited[host] = getattr(exc, "retry_at", None) or utcnow()
        log.info("Hourly request budget: %s", exc)
        _add_error(summary, f"Часть объявлений отложена до следующей проверки. {budget_message(exc)}", str(exc))

    def _expire_backlog(self, search: SearchConfig, baseline_at: datetime | None, summary: RunSummary) -> None:
        """Deferred ads the budgets never reached within PENDING_MAX_AGE: give up on them
        visibly (stored as "expired", counted, logged) instead of letting them rot silently."""
        try:
            stale = self.db.stale_pending(search.name, before=utcnow() - PENDING_MAX_AGE, after=baseline_at)
        except sqlite3.Error as exc:
            log.warning("Backlog of %r: %s", search.name, exc)
            return
        for listing in stale:
            self._save(Evaluation(
                ad_id=listing.ad_id, purpose=search.purpose, buy_price=listing.price, verdict="skip",
                action="skip", stage="expired",
                reasons=[f"Не успел проверить за {PENDING_MAX_AGE.days} дня (лимиты запросов) — объявление могло уйти"],
            ))
        if stale:
            summary.expired += len(stale)
            log.warning("⌛ %s: %d объявлений ждали проверки дольше %d дней — пропущены (подними лимиты"
                        " general.max_*_per_run или interval_minutes)", search.name, len(stale), PENDING_MAX_AGE.days)

    def backlog_status(self) -> dict[str, int]:
        """For the status page: ads waiting in the deferred backlog, and ads given up on in 24 h."""
        names = [s.name for s in self.config.searches if s.enabled]
        try:
            return {
                "pending": self.db.pending_count(names, since=utcnow() - PENDING_MAX_AGE),
                "expired_24h": self.db.expired_since(utcnow() - timedelta(days=1)),
            }
        except sqlite3.Error:
            return {"pending": 0, "expired_24h": 0}

    def _upkeep(self) -> None:
        """Once per process: drop v0.1 verdicts. Once per day: forget old skipped listings/runs."""
        try:
            if not self._upkeep_done:
                self._upkeep_done = True
                removed = self.db.migrate_v01_evaluations()
                if removed:
                    log.info("Обновление: удалил %d старых оценок версии 0.1 — объявления оценю заново", removed)
            today = utcnow().date().isoformat()
            state = self.db.get_state("upkeep:retention")
            if state is None or state[0] != today:
                gone = self.db.apply_retention(listing_days=LISTING_RETENTION_DAYS, run_days=RUN_RETENTION_DAYS)
                self.db.set_state("upkeep:retention", today)
                if gone["listings"] or gone["runs"]:
                    log.info("Уборка: забыл %d пропущенных объявлений старше %d дней и %d старых проверок",
                             gone["listings"], LISTING_RETENTION_DAYS, gone["runs"])
        except sqlite3.Error as exc:
            log.warning("Upkeep failed: %s", exc)

    def _budget_for(self, summary: RunSummary) -> _Budget:
        if self._budget is None or self._budget.summary is not summary:
            self._budget = _Budget(summary, self.config.general)
        return self._budget

    def _pending(self, search: SearchConfig, baseline_at: datetime | None) -> list[Listing]:
        """Stored ads of this search still waiting for an evaluation (not the ones only
        learned from during the first pass)."""
        since = utcnow() - PENDING_MAX_AGE
        if baseline_at is not None and _aware(baseline_at) > since:
            since = _aware(baseline_at)
        limit = max(PENDING_MIN_BATCH, 4 * self.config.general.max_new_per_search)
        try:
            return self.db.pending_listings(search.name, since=since, limit=limit)
        except sqlite3.Error as exc:
            log.warning("Pending listings of %r: %s", search.name, exc)
            return []

    def _remember_prices(self, search: SearchConfig, listings: list[Listing]) -> int:
        """Feed the price history with this page of results, each ad under its identity key
        (plain items under the product, bundles/parts/... under a kind prefix; wanted, swap,
        box-only, model-less ads and ads missing parts not at all). Not from price-filtered
        searches — a list cut at max_price drags medians down."""
        if _price_filtered(search):
            return 0
        rows = [(key, item) for item in listings if (key := history_key_for(item))]
        if not rows:
            return 0
        try:
            return self.db.record_price_points(rows)
        except sqlite3.Error as exc:
            log.warning("Price history: can't store prices of %r: %s", search.name, exc)
            return 0

    def _prune_history(self) -> None:
        days = max(1, self.config.pricing.history_days)
        try:
            removed = self.db.prune_price_points(days)
        except sqlite3.Error as exc:
            log.warning("Price history pruning failed: %s", exc)
            return
        if removed:
            log.info("История цен: удалено %d цен старше %d дн.", removed, days)

    @staticmethod
    def _log_funnel(search: SearchConfig, summary: RunSummary, before: dict[str, int], evaluated: int) -> None:
        d = {name: getattr(summary, name) - before[name] for name in _FUNNEL_COUNTERS}
        log.info(
            "📊 %s: оценено %d (фильтр %d, по рынку без запросов %d, цена из истории %d) · запросы:"
            " аналоги %d, страницы %d, ИИ %d · отложено %d",
            search.name, evaluated, d["prefiltered"], d["early_skips"], d["history_hits"],
            d["comps_lookups"], d["details_fetched"], d["ai_calls"], d["deferred"],
        )

    # ------------------------------------------------------------ the funnel
    def _triage(
        self, listing: Listing, search: SearchConfig, budget: _Budget, *, recheck: bool = False
    ) -> Evaluation | _Candidate:
        """Free part of the funnel (no requests): prefilter, min price, reference price /
        price history, early "no deal". Returns the stored evaluation, or the candidate for
        the paid part. `recheck`: an earlier early skip seen again — not counted twice."""
        counted = not recheck
        plan = self._scout_plan_for(listing)  # the AI scout's priced reading (None: not read / off)
        keep, reasons = prefilter(listing, search)
        kind_vetoed = False
        if keep:
            floor = search.min_price if search.min_price is not None else self.config.general.min_listing_price
            too_cheap = below_min_price(listing, floor)
            if too_cheap:
                keep, reasons = False, [too_cheap]
        ident = listing_identity(listing)
        if keep:
            veto = self._kind_veto(ident, search)
            if veto:
                keep, reasons, kind_vetoed = False, [veto], True
        if not keep:
            skip = self._skip_evaluation(listing, search, reasons)
            # "alter PC mit RTX 3080": the script drops the kind, the scout priced its parts
            if (kind_vetoed and plan is not None and _scout_may_overrule(ident.kind, plan)
                    and self._scout_worth(listing, search, plan)):
                return self._scout_candidate(listing, plan, skip, budget.summary, counted)
            if counted:
                budget.summary.prefiltered += 1
            return self._save(skip)

        known = self._reference_estimate(listing, search)
        if known is None:
            target = self._listing_target(listing, ident)
            known = self._history_estimate(listing, target) if target is not None else None
        hint = known
        if known is not None:
            best = evaluate(listing, known, None, search, self.config.pricing)
            if market_says_no_deal(best):
                if not self._may_skip_early(known, ident):
                    known = None  # thin/unclear history says "no deal": check comparables first
                else:
                    skip = best.model_copy(update={"reasons": no_deal_reasons(best), "stage": "market"})
                    # the title reads as one product, the scout saw another (or a bundle) worth more
                    if (plan is not None and self._scout_sees_other(plan, ident)
                            and self._scout_worth(listing, search, plan)):
                        return self._scout_candidate(listing, plan, skip, budget.summary, counted)
                    if counted:
                        budget.summary.early_skips += 1
                        budget.summary.history_hits += known.source == "history"
                    return self._save(skip)
            if known is not None and known.source == "history" and counted:
                budget.summary.history_hits += 1
        if plan is not None:
            return self._scout_enrich(listing, search, plan, ident, known, hint, budget.summary, counted)
        return _Candidate(listing, known, hint)

    def _may_skip_early(self, est: PriceEstimate, ident: Identity) -> bool:
        """Is this market price solid enough to drop an ad without looking at it? A reference
        price, or a history of ≥8 close prices of an exactly identified product."""
        if est.source == "reference":
            return True
        if est.source != "history" or est.sample_size < EARLY_SKIP_MIN_POINTS:
            return False
        if est.low is None or est.high is None or not est.market_price:
            return False
        if (est.high - est.low) / est.market_price > EARLY_SKIP_MAX_SPREAD:
            return False
        return ident.priceable

    def _kind_veto(self, ident: Identity, search: SearchConfig) -> str | None:
        """Skip ads that aren't the product itself (identity kinds). Bundles stay (priced from
        bundles only). Defects/parts/accessories/PCs stay when the search itself asks for such
        things ("rtx 3080 kühler", "gaming pc"): they are then priced from their own kind."""
        kind = ident.kind
        if kind not in _KIND_SKIP_RU:
            return None
        if kind not in ("wanted", "swap", "service", "box_only") and search.query.strip():
            try:
                wanted_kind = identify(search.query).kind
            except Exception:  # noqa: BLE001
                wanted_kind = None
            if wanted_kind == kind or (kind in _ADDON_KINDS and wanted_kind in _ADDON_KINDS):
                return None
        reason = _KIND_SKIP_RU[kind]
        if kind == "part" and ident.missing:
            reason += f" (нет: {', '.join(ident.missing)})"
        return reason

    @staticmethod
    def _listing_target(listing: Listing, ident: Identity | None = None) -> _Target | None:
        """Price the ad as what identity says it is; None = the title names no product."""
        ident = ident or listing_identity(listing)
        if ident.key is None:
            return None
        return _Target(ident.key.query() or listing.title, listing.title, history_lookup_key(ident), ident.kind)

    @staticmethod
    def _query_target(query: str, listing: Listing) -> _Target:
        """Price the ad by the AI's own search phrase (vague title / no market data). Searching
        uses the AI's wording ("Apple iPad 10 64GB" finds what sellers write). Matching stays the
        ad's own product when identity agrees on family and model (a "solo" tool never takes the
        kit's price, a bundle only bundles); when the two differ, the title was probably misread
        ("MacBookAir", "iPadAir(5. Gen)") and the AI's product decides."""
        phrase = " ".join(query.split())
        try:
            key = identify(query).key
        except Exception:  # noqa: BLE001
            key = None
        own = listing_identity(listing)
        same_line = (key is not None and own.key is not None
                     and (key.family, key.model) == (own.key.family, own.key.model))
        if own.key is not None and (own.kind == "bundle" or same_line or key is None):
            return _Target(phrase, listing.title, history_lookup_key(own), own.kind)
        if key is None:
            return _Target(phrase, phrase, None)
        return _Target(phrase, key, key.coarse_key())

    async def _finish(self, cand: _Candidate, search: SearchConfig, budget: _Budget) -> Evaluation:
        """Paid part of the funnel: ad page → prefilter on the full text → comparables (only
        if the market is still unknown) → local AI. Raises _Deferred when a budget is used up."""
        self._ensure_components()
        listing, estimate = cand.listing, cand.estimate
        plan, found_by = cand.plan, cand.found_by
        source = self._source_for(listing)
        pricing = self.config.pricing

        wants_ai = estimate is not None and self._wants_ai(listing, estimate, search)
        if wants_ai and not budget.check("ai_calls"):  # raises _Deferred before the page is fetched
            wants_ai = False  # AI switched off for monitoring passes (max_ai_per_run: 0)

        # 3: ad page — full text and all photos
        if (self.config.general.fetch_details and not listing.detail_loaded
                and budget.spend("details_fetched")):
            try:
                listing, keep, reasons = await self._load_detail(listing, search, source)
            except RateBudgetExceeded:
                budget.summary.details_fetched -= 1  # no request was sent
                raise
            if not keep:
                budget.summary.prefiltered += 1
                return self._save_skip(listing, search, reasons)
        if plan is not None:  # the full text may change the scout's picture ("ohne Grafikkarte")
            plan = self._scout_replan(listing, plan)
            if found_by == scout_ai.FOUND_BY_SCOUT and not self._scout_worth(listing, search, plan):
                if cand.script_skip is not None:
                    return self._save(cand.script_skip)  # back to what the script alone decided
                plan, found_by, estimate = None, scout_ai.FOUND_BY_SCRIPT, None  # the script's own way
            elif found_by == scout_ai.FOUND_BY_SCOUT:
                estimate = plan.estimate
                if plan.kind in scout_ai.BUNDLE_KINDS and scout_ai.unpriced_parts(plan):
                    estimate = await self._scout_price(listing, search, plan, budget) or estimate

        # 4: comparables, only when neither reference nor history knew the market
        if estimate is None and plan is not None and plan.usable:
            ident = listing_identity(listing)
            if plan.estimate is not None and plan.estimate.market_price:
                estimate = plan.estimate  # the history knows the scout's product by now
            elif self._scout_sees_other(plan, ident) or found_by == scout_ai.FOUND_BY_SCOUT:
                estimate = await self._scout_price(listing, search, plan, budget)
            if estimate is not None and (found_by == scout_ai.FOUND_BY_SCOUT or self._scout_sees_other(plan, ident)):
                found_by = scout_ai.FOUND_BY_SCOUT
        if estimate is None:
            estimate = await self._market_estimate(listing, search, budget, use_history=False)
        best = evaluate(listing, estimate, None, search, pricing)
        if market_says_no_deal(best):  # the AI can't turn this into a deal
            return self._save(best.model_copy(update={"stage": "full", "found_by": found_by}))

        # 5: local AI
        wants_ai = self._wants_ai(listing, estimate, search) and budget.spend("ai_calls")
        return await self._conclude(listing, search, estimate, source, budget if wants_ai else None,
                                    ask_ai=wants_ai, plan=plan, found_by=found_by)

    async def _conclude(
        self, listing: Listing, search: SearchConfig, estimate: PriceEstimate, source: Any,
        budget: _Budget | None, *, ask_ai: bool, plan: Any = None, found_by: str = "script",
    ) -> Evaluation:
        """AI verdict (if asked) → final evaluation → second opinion → store. `plan`: the AI
        scout's priced reading (a PC / bundle it priced by parts is no veto for the photo check)."""
        verdict = None
        from_ai_query = False
        if ask_ai:
            verdict, estimate, from_ai_query = await self._ask_ai(listing, search, estimate, source, budget,
                                                                  plan=plan)
            verdict = scout_ai.vision_for_plan(verdict, plan)
        scout_knows = plan is not None and plan.usable and (plan.identified or plan.estimate is not None)
        vague = not from_ai_query and not scout_knows and self._listing_target(listing) is None
        evaluation = evaluate(listing, estimate, verdict, search, self.config.pricing, ai_expected=ask_ai,
                              vague=vague)
        warning = scout_ai.vision_disagrees(verdict, plan)
        if warning:
            evaluation = _capped_maybe(evaluation, warning)
        evaluation = await self._maybe_second_opinion(listing, search, estimate, evaluation, source)
        update: dict[str, Any] = {"stage": "full", "found_by": found_by}
        if found_by == scout_ai.FOUND_BY_SCOUT and plan is not None and plan.reason_ru():
            reasons = list(evaluation.reasons)
            reasons.insert(min(1, len(reasons)), f"🔎 {scout_ai.FOUND_BY_LABEL_RU[found_by]}: {plan.reason_ru()}")
            update["reasons"] = reasons
        evaluation = self._save(evaluation.model_copy(update=update))
        self._hold_for_vision(listing, search, evaluation)
        return evaluation

    # ------------------------------------------------------------ evaluation
    async def evaluate_listing(self, listing: Listing, search: SearchConfig, *,
                               progress: Callable[[str, str], Any] | None = None) -> Evaluation:
        """Full pipeline for one ad — no budgets, no shortcuts (single-ad check);
        stores the listing + evaluation. `progress(stage, text_ru)` hears about each step."""
        previous, self._stage_cb = self._stage_cb, progress or self._stage_cb
        try:
            return await self._evaluate(listing, search, None)
        finally:
            self._stage_cb = previous

    def _stage(self, stage: str, text_ru: str) -> None:
        if self._stage_cb is None:
            return
        try:
            self._stage_cb(stage, text_ru)
        except Exception:  # noqa: BLE001 - a progress reporter never breaks a check
            log.exception("progress callback failed")

    async def _evaluate(self, listing: Listing, search: SearchConfig, budget: _Budget | None = None) -> Evaluation:
        """Every step for one ad: prefilter → ad page → prefilter on the full text →
        market price (reference / history / comparables) → AI → evaluation → second opinion."""
        self._ensure_components()
        source = self._source_for(listing)
        keep, reasons = prefilter(listing, search)
        if keep and self.config.general.fetch_details and not listing.detail_loaded:
            listing, keep, reasons = await self._load_detail(listing, search, source)
        if not keep:
            return self._save_skip(listing, search, reasons)
        self._stage("market", "Ищу цены похожих…")
        estimate = await self._market_estimate(listing, search, budget)
        ask_ai = self._wants_ai(listing, estimate, search)
        if ask_ai:
            self._stage("ai", "Нейросеть смотрит фото…")
        return await self._conclude(listing, search, estimate, source, budget, ask_ai=ask_ai)

    def _save(self, evaluation: Evaluation) -> Evaluation:
        self.db.save_evaluation(evaluation)
        return evaluation

    def _save_skip(self, listing: Listing, search: SearchConfig, reasons: list[str]) -> Evaluation:
        return self._save(self._skip_evaluation(listing, search, reasons))

    @staticmethod
    def _skip_evaluation(listing: Listing, search: SearchConfig, reasons: list[str]) -> Evaluation:
        return Evaluation(
            ad_id=listing.ad_id, purpose=search.purpose, buy_price=listing.price,
            verdict="skip", action="skip", score=0.0, reasons=reasons, stage="prefilter", found_by="script",
        )

    async def _load_detail(self, listing: Listing, search: SearchConfig, source: Any) -> tuple[Listing, bool, list[str]]:
        """Open the ad page (full text, all photos) and re-run the prefilter on it.
        A failed page is not fatal: the search-card data is used instead."""
        try:
            listing = await source.fetch_detail(listing)
        except (BlockedError, RateBudgetExceeded):
            raise
        except Exception as exc:
            log.warning("Detail page of %s failed: %s", listing.ad_id, exc)
            return listing, True, []
        listing.search_name = listing.search_name or search.name
        self.db.upsert_listing(listing)
        keep, reasons = prefilter(listing, search)  # full text may reveal more
        return listing, keep, reasons

    def _wants_ai(self, listing: Listing, estimate: PriceEstimate, search: SearchConfig) -> bool:
        ai_cfg = self.config.ai
        return self._evaluator is not None and (
            ai_cfg.run_for == "all"
            or prefilter_score(listing, estimate, search, self.config.pricing) >= ai_cfg.min_prefilter_score
        )

    async def _ask_ai(
        self, listing: Listing, search: SearchConfig, estimate: PriceEstimate, source: Any,
        budget: _Budget | None, *, plan: Any = None,
    ) -> tuple[AIVerdict, PriceEstimate, bool]:
        """Local AI verdict. The model sees a few comparables (never our market price — it
        guesses its own blind) and says which of them are the same variant. The third value:
        the market price now comes from the AI's own search query. A bundle priced by its
        parts shows no comparables (the parts are not "the same variant" as the whole)."""
        assert self._evaluator is not None
        images = await self._images(listing, source, self.config.ai.max_images)
        by_parts = plan is not None and plan.kind in scout_ai.BUNDLE_KINDS and estimate is plan.estimate
        shown = [] if by_parts else prompt_comparables(None, _comparables_for_ai(estimate))
        verdict = await self._evaluator.evaluate(
            listing, images, purpose=search.purpose,
            estimate=estimate if estimate.market_price is not None else None,
            target_price=search.target_price,
            **self._comparables_kwarg(self._evaluator, shown),
        )
        if verdict.confidence > 0:
            self._ai_answered += 1
        else:
            self._ai_failed += 1
        estimate = self._variant_checked(estimate, same_variant_comparables(verdict, shown) if shown else None)
        # The model often identifies the product better than the raw title does. Not for a PC /
        # bundle / lot the scout read: its phrase names ONE part ("rtx 3070") and would price the
        # whole thing as that part — without real part prices it stays at most "maybe".
        from_query = False
        whole = plan is not None and plan.kind in scout_ai.BUNDLE_KINDS
        if estimate.market_price is None and verdict.search_query and not whole:
            ai_estimate = await self._market_estimate(
                listing, search, budget, query=verdict.search_query, defer=False
            )
            if ai_estimate.market_price is not None:
                estimate, from_query = ai_estimate, True
        return verdict, _cross_check(estimate, verdict), from_query

    async def _images(self, listing: Listing, source: Any, max_images: int) -> list[bytes]:
        if not listing.image_urls or max_images <= 0:
            return []
        try:
            return await source.download_images(listing, max_images=max_images)
        except RateBudgetExceeded as exc:  # hourly cap: the AI works from the text alone
            log.info("Images of %s skipped: %s", listing.ad_id, exc)
            return []

    @staticmethod
    def _comparables_kwarg(evaluator: Any, shown: list[Comparable]) -> dict[str, Any]:
        return {"comparables": shown} if _accepts(evaluator.evaluate, "comparables") else {}

    def _variant_checked(self, estimate: PriceEstimate, matched: list[Comparable] | None) -> PriceEstimate:
        """Use the AI's "same variant" picks: enough of them → re-estimate from exactly those;
        none → mark the estimate (evaluate() then caps the verdict at "maybe")."""
        if matched is None or estimate.market_price is None or estimate.source not in _COMPS_SOURCES:
            return estimate
        pricing = self.config.pricing
        if len(matched) >= max(3, pricing.min_comparables):
            exact = estimate_from_comparables(
                matched, asking_price_discount=pricing.asking_price_discount, query=estimate.query
            )
            if exact.market_price is not None:
                return exact.model_copy(update={
                    "notes": f"ИИ подтвердил {len(matched)} точных аналогов. {exact.notes}",
                    "ai_variant_matches": len(matched),
                })
        return estimate.model_copy(update={"ai_variant_matches": len(matched)})

    async def _maybe_second_opinion(
        self, listing: Listing, search: SearchConfig, estimate: PriceEstimate,
        evaluation: Evaluation, source: Any,
    ) -> Evaluation:
        """Ask the optional (cloud) model to double-check only the best candidates."""
        so = self.config.ai.second_opinion
        if (
            self._second is None
            or evaluation.verdict not in so.verdicts
            or evaluation.score < so.min_score
            or self._second_calls >= so.max_per_run
        ):
            return evaluation
        self._second_calls += 1
        images = await self._images(listing, source, so.max_images)
        second = await self._second.evaluate(
            listing, images, purpose=search.purpose,
            estimate=estimate if estimate.market_price is not None else None,
            target_price=search.target_price,
            **self._comparables_kwarg(self._second, prompt_comparables(None, _comparables_for_ai(estimate))),
        )
        if second.confidence <= 0:  # the call failed -> keep the local result
            return evaluation.model_copy(update={"ai_second": second})
        final = evaluate(listing, estimate, second, search, self.config.pricing)
        return final.model_copy(update={"ai": evaluation.ai or final.ai, "ai_second": second})

    async def evaluate_url(
        self, url: str, *, purpose: str = "resale", target_price: float | None = None,
        progress: Callable[[str, str], Any] | None = None,
    ) -> DealView:
        """Check a single ad by its link ("should I buy this?"). `progress(stage, text_ru)`
        hears about each step: fetch, market, ai."""
        ad_id = ad_id_from_url(url)
        if not ad_id:
            raise ValueError("Не похоже на ссылку на объявление Kleinanzeigen или eBay")
        previous, self._stage_cb = self._stage_cb, progress or self._stage_cb
        try:
            return await self._evaluate_url(url, ad_id, purpose=purpose, target_price=target_price)
        finally:
            self._stage_cb = previous

    async def _evaluate_url(self, url: str, ad_id: str, *, purpose: str, target_price: float | None) -> DealView:
        self._ensure_components()
        self._stage("fetch", "Открываю объявление…")
        source_name = "ebay" if ad_id.startswith("ebay-") else "kleinanzeigen"
        search = SearchConfig(
            name="Ручная проверка", source=source_name, purpose=purpose, target_price=target_price  # type: ignore[arg-type]
        )
        listing = self.db.get_listing(ad_id) or Listing(
            ad_id=ad_id, source=source_name, url=url, title="", search_name=search.name  # type: ignore[arg-type]
        )
        listing = listing.model_copy(update={"detail_loaded": False})
        listing = await self._source_for(listing).fetch_detail(listing)
        listing.search_name = listing.search_name or search.name
        self.db.upsert_listing(listing)
        await self.evaluate_listing(listing, search)
        deal = self.db.get_deal(ad_id)
        assert deal is not None
        return deal

    # ---------------------------------------------------------- market price
    async def _estimate(
        self, listing: Listing, search: SearchConfig, *, query: str | None = None
    ) -> PriceEstimate:
        """Market price without budgets: reference > price history > comparables."""
        return await self._market_estimate(listing, search, None, query=query)

    async def _market_estimate(
        self, listing: Listing, search: SearchConfig, budget: _Budget | None, *,
        query: str | None = None, defer: bool = True, use_history: bool = True,
    ) -> PriceEstimate:
        """Reference price, then our own price history (both free), then comparables from the
        network (cached; counts against the pass budget — `defer=False` returns "no estimate"
        instead of deferring when it is used up). `query`: price by the AI's search phrase
        instead of the ad's identity. A title that names no product gets no estimate at all —
        comparables for "Tablet zu verkaufen" would be any tablet."""
        if query is None:
            ref = self._reference_estimate(listing, search)
            if ref is not None:
                return ref
            target = self._listing_target(listing)
            if target is None:
                return PriceEstimate(notes="Непонятно, что за товар — аналоги по названию не ищу")
        else:
            target = self._query_target(query, listing)
        history = self._history_estimate(listing, target) if use_history else None
        if history is not None:
            if budget is not None:
                budget.summary.history_hits += 1
            return history
        cached = self._cached_comps(target)
        if cached is not None:
            return cached
        if budget is not None:
            try:
                allowed = budget.spend("comps_lookups")
            except _Deferred:
                if defer:
                    raise
                return PriceEstimate(query=target.query, notes="Лимит поиска аналогов на эту проверку исчерпан")
            if not allowed:
                return PriceEstimate(query=target.query,
                                     notes="Поиск аналогов выключен (general.max_comps_lookups_per_run: 0)")
        try:
            estimate = await self._comps_estimate(listing, target)
        except RateBudgetExceeded as exc:  # hourly page cap: no request was sent
            if budget is not None:
                budget.summary.comps_lookups -= 1
                if defer:
                    raise
            return PriceEstimate(query=target.query, notes=f"Аналоги не искал: {exc}")
        if estimate.market_price is None and query is None:
            estimate = await self._retry_with_title_words(listing, target, budget) or estimate
        return estimate

    async def _retry_with_title_words(
        self, listing: Listing, target: _Target, budget: _Budget | None
    ) -> PriceEstimate | None:
        """Identity's canonical query found nothing ("ipad 2022 64gb" while sellers write
        "iPad 10"): one more lookup with the title's own words, same strict matching."""
        words = make_search_query(listing.title)
        if not words or normalize(words) == normalize(target.query):
            return None
        alt = _Target(words, target.ref, target.history_key, target.kind)
        cached = self._cached_comps(alt)
        if cached is not None:
            return cached if cached.market_price is not None else None
        if budget is not None:
            try:
                if not budget.spend("comps_lookups"):
                    return None
            except _Deferred:
                return None  # the first lookup already counted: no deferral for the bonus one
        try:
            estimate = await self._comps_estimate(listing, alt)
        except RateBudgetExceeded:
            if budget is not None:
                budget.summary.comps_lookups -= 1
            return None
        return estimate if estimate.market_price is not None else None

    def _reference_estimate(self, listing: Listing, search: SearchConfig) -> PriceEstimate | None:
        if search.reference_price is not None:
            est = PriceEstimate(
                market_price=search.reference_price, source="reference",
                notes="Цена задана в настройках поиска",
            )
        else:
            ref = find_reference_price(
                f"{listing.title} {listing.description[:300]}", self.config.pricing.reference_prices
            )
            if ref is None:
                return None
            est = PriceEstimate(market_price=ref, source="reference", notes="Цена из справочника reference_prices")
        return self._checked_reference(listing, est)

    def _checked_reference(self, listing: Listing, est: PriceEstimate) -> PriceEstimate:
        """A reference price is trusted — but say so when our own history (≥ 8 prices) says the
        market is more than 30 % away from it."""
        target = self._listing_target(listing)
        history = self._history_estimate(listing, target) if target is not None else None
        ref = est.market_price or 0.0
        if (history is None or history.market_price is None or history.sample_size < REFERENCE_CHECK_MIN_POINTS
                or ref <= 0 or abs(ref - history.market_price) / history.market_price <= REFERENCE_CHECK_TOLERANCE):
            return est
        return est.model_copy(update={
            "warning": f"⚠ reference_price ({ref:.0f} €) расходится с рынком (~{history.market_price:.0f} €)",
        })

    def _history_estimate(self, listing: Listing, target: _Target) -> PriceEstimate | None:
        """Market price from prices seen before (search results, comparables) — no requests.
        Fetched by the product's coarse key, kept only if identity calls them the same product
        and kind of offer as this ad."""
        pricing = self.config.pricing
        if not pricing.use_price_history or not target.history_key:
            return None
        days = max(1, pricing.history_days)
        try:
            points = self.db.price_history_prefix(
                target.history_key, utcnow() - timedelta(days=days), exclude_ad_id=listing.ad_id
            )
        except sqlite3.Error as exc:
            log.warning("Price history lookup for %r failed: %s", target.history_key, exc)
            return None
        return estimate_from_history(
            target.query, points, asking_price_discount=pricing.asking_price_discount,
            min_points=pricing.history_min_points, days=days, fits=lambda c: comparable_fits(target.ref, c),
        )

    def _cached_comps(self, target: _Target | str) -> PriceEstimate | None:
        key = target.cache_key if isinstance(target, _Target) else f"{normalize(target)}|item"
        cached = self._comps_cache.get(key)
        if cached and time.monotonic() - cached[0] < COMPS_CACHE_TTL:
            return cached[1]
        return None

    async def _comps_estimate(self, listing: Listing, target: _Target) -> PriceEstimate:
        """Look up comparables (eBay sold, eBay API, Kleinanzeigen), keep the ones identity
        calls the same product and kind of offer, remember every price in the history."""
        pricing = self.config.pricing
        query = target.query
        comps: list[Comparable] = []
        limited: RateBudgetExceeded | None = None  # a source skipped by the hourly page cap
        if pricing.use_ebay_sold_comps and not self._ebay_blocked and self._ebay is not None:
            try:
                comps += await self._ebay.sold_comparables(query, limit=pricing.comps_limit)
            except RateBudgetExceeded as exc:
                limited = exc
            except BlockedError as exc:
                # eBay being grumpy must not stop Kleinanzeigen; during its cooldown the
                # scraper fails without a request, so just stop asking for this pass
                self._ebay_blocked = True
                if getattr(exc, "cooling_down", False):
                    log.info("eBay sold comps paused (cooldown after a block): %s", exc)
                else:
                    log.warning("eBay blocked sold-comps lookup: %s", exc)
            except Exception as exc:
                log.warning("eBay sold comps for %r failed: %s", query, exc)
        if self._ebay_api is not None:
            try:
                comps += await self._ebay_api.comparables(
                    query, exclude_ad_id=listing.ad_id, limit=pricing.comps_limit
                )
            except RateBudgetExceeded as exc:
                limited = exc
            except Exception as exc:
                log.warning("eBay API comps for %r failed: %s", query, exc)
        if pricing.use_kleinanzeigen_comps and self._scraper is not None:
            try:
                comps += await self._scraper.comparables(
                    query, exclude_ad_id=listing.ad_id, limit=pricing.comps_limit
                )
            except RateBudgetExceeded as exc:
                limited = exc
            except BlockedError:
                raise
            except Exception as exc:
                log.warning("Kleinanzeigen comps for %r failed: %s", query, exc)
        if limited is not None and not comps:
            raise limited  # nothing to go on: defer, retry next pass

        relevant = [c for c in comps if comparable_fits(target.ref, c)]
        if len(relevant) < len(comps):
            log.debug("Comparables for %r: kept %d of %d (other variants, bundles, parts...)",
                      query, len(relevant), len(comps))
        self._remember_comparables(comps)
        estimate = estimate_from_comparables(
            relevant, asking_price_discount=pricing.asking_price_discount, query=query
        )
        if limited is None:  # a partial lookup is used once but not cached
            self._cache_comps(target.cache_key, estimate)
        return estimate

    def _cache_comps(self, key: str, estimate: PriceEstimate) -> None:
        self._comps_cache[key] = (time.monotonic(), estimate)
        self._comps_cache.move_to_end(key)
        while len(self._comps_cache) > COMPS_CACHE_MAX:
            self._comps_cache.popitem(last=False)

    def _remember_comparables(self, comps: list[Comparable]) -> None:
        """Every comparable is a price of *some* product: store each under its own identity key
        (the Pro found while looking for the plain model helps the next Pro ad)."""
        rows: list[tuple[str, Comparable]] = []
        for comp in comps:
            if not comp.price or comp.price <= 1:
                continue
            try:
                ident = identify(comp.title)
            except Exception:  # noqa: BLE001
                continue
            key = ident.history_key()
            if key and not ident.missing and not _severe_title(comp.title):
                rows.append((key, comp))
        if not rows:
            return
        try:
            self.db.record_price_points(rows)
        except sqlite3.Error as exc:
            log.warning("Price history: can't store comparables: %s", exc)

    # ------------------------------------------------------------- AI scout
    # docs/design/AI_SCOUT.md. Stage A (_scout_read): a small text model reads the new ads in
    # batches; stage B (_scout_plan_for): its reading is grounded in the ad text and priced from
    # our own history; the free funnel (_triage) then may promote an ad the script dismissed,
    # rank candidates by it, or give them a market price; stage C/D are the usual paid steps.
    def _scout_begin_pass(self, n_searches: int) -> None:
        self._scout_items = {}
        self._scout_plans = {}
        self._scout_hints_text = None
        self._scout_searches_left = max(1, n_searches)
        self._scout_deadline = None
        if self.scout_enabled:
            assert self._scout is not None
            sc = self.config.ai.scout
            seconds = max(1.0, self.config.general.interval_minutes) * 60 * max(0.0, min(1.0, sc.pass_share))
            self._scout_deadline = self._scout.clock() + seconds

    def _scout_mode(self) -> str:
        """"all" while the model keeps up with the new ads, else "candidates" (only where the
        script is blind); ai.scout.mode forces one of them."""
        sc = self.config.ai.scout
        mode = sc.mode
        if mode == "auto" and self._scout is not None:
            stats = self._scout.stats
            capacity = stats.capacity_per_hour(sc.pass_share, sc.max_per_hour)
            mode = "candidates" if capacity is not None and stats.seen_last_hour > capacity else "all"
        if self._scout is not None:
            self._scout.stats.mode = mode
        return mode

    def _scout_search_deadline(self) -> float | None:
        """This search's share of the pass's scout time (time left over rolls on to the next)."""
        if self._scout_deadline is None or self._scout is None:
            return None
        now = self._scout.clock()
        left = max(0.0, self._scout_deadline - now)
        return now + left / max(1, self._scout_searches_left)

    def _scout_load(self, ad_ids: list[str]) -> None:
        """Readings stored by earlier passes (an ad is read once)."""
        missing = [a for a in ad_ids if a not in self._scout_items]
        if not missing:
            return
        try:
            stored = self.db.get_triage(missing)
        except sqlite3.Error as exc:
            log.warning("AI scout: stored readings unavailable: %s", exc)
            return
        for ad_id, data in stored.items():
            try:
                self._scout_items[ad_id] = TriageItem.model_validate(data)
            except Exception:  # noqa: BLE001 - an old / broken row is simply read again
                continue

    async def _scout_read(self, search: SearchConfig, todo: list[Listing], fresh: set[str],
                          summary: RunSummary) -> None:
        """Stage A for one search: read the ads nobody has read yet, fresh first, then where the
        script is blind; within this search's time share and the hourly cap. Never raises."""
        if not self.scout_enabled or not todo:
            return
        engine = self._scout
        assert engine is not None
        self._scout_load([item.ad_id for item in todo])
        engine.note_arrivals(len(fresh))
        unread = [item for item in todo if item.ad_id not in self._scout_items]
        mode = self._scout_mode()
        queue = unread if mode == "all" else [item for item in unread if blind_spot(item)]
        not_offered = len(unread) - len(queue)
        queue.sort(key=lambda item: (item.ad_id not in fresh, -scout_priority(item), -_aware(item.first_seen).timestamp()))
        deadline = self._scout_search_deadline()
        self._scout_searches_left = max(1, self._scout_searches_left - 1)
        summary.scout_overflow += not_offered
        if not queue:
            return
        run = await self._scout_run(queue, deadline, {item.ad_id: search.category_name for item in queue}, summary)
        if run is None:
            return
        by_id = {item.ad_id: item for item in queue}
        if not _price_filtered(search):
            self._scout_remember([by_id[a] for a in run.items if a in by_id and a in fresh])

    async def _scout_run(self, queue: list[Listing], deadline: float | None, categories: dict[str, str],
                         summary: RunSummary, *, rescue: bool = False) -> Any:
        engine = self._scout
        assert engine is not None
        try:
            run = await engine.triage(queue, deadline=deadline, categories=categories, hints=self._scout_hints(),
                                      count_overflow=not rescue)
        except Exception as exc:  # noqa: BLE001 - the scout never breaks a pass
            log.exception("AI scout failed")
            summary.scout_overflow += len(queue)
            await self._scout_down(str(exc), summary)
            return None
        self._scout_items.update(run.items)
        try:
            self.db.save_triage([item.model_dump(mode="json") for item in run.items.values()])
        except sqlite3.Error as exc:
            log.warning("AI scout: readings not stored: %s", exc)
        summary.scout_read += run.ai_count
        summary.scout_overflow += 0 if rescue else len(run.overflow)
        summary.scout_failed += len(run.failed)
        summary.scout_calls += run.calls
        summary.scout_seconds = round(summary.scout_seconds + run.seconds, 1)
        if run.ai_count:
            log.info("🔎 Разведчик прочитал %d объявл. за %.0f с (%d вызов.), не успел %d, не разобрал %d",
                     run.ai_count, run.seconds, run.calls, len(run.overflow), len(run.failed))
            self._scout_down_reported = False
        if run.error:
            await self._scout_down(run.error, summary)
        return run

    async def _scout_down(self, error: str, summary: RunSummary) -> None:
        if self._scout_down_reported:
            return
        self._scout_down_reported = True
        settings = self._scout_llm_settings()
        why = ai_problem(settings.provider, settings.base_url, settings.model, error, server_ok=None)
        await self._health("scout_down", f"⚠ Нейросеть-разведчик не отвечает — новые объявления смотрю обычным"
                                         f" способом. {why}", summary)

    def _scout_hints(self) -> str:
        """The user's taste from feedback (hidden / bought / sold deals), a few lines in the prompt."""
        if self._scout_hints_text is not None:
            return self._scout_hints_text
        text = ""
        if self.config.ai.scout.learn_from_feedback:
            try:
                text = scout_feedback_hints(self.db.feedback_examples(limit=4))
            except sqlite3.Error:
                text = ""
        self._scout_hints_text = text
        return text

    def _scout_plan_for(self, listing: Listing) -> Any:
        """Stage B: the scout's reading of this ad, grounded and priced (cached per pass)."""
        if not self.scout_enabled:
            return None
        if listing.ad_id not in self._scout_items:
            self._scout_load([listing.ad_id])
        item = self._scout_items.get(listing.ad_id)
        if item is None or not item.is_ai:
            return None
        cached = self._scout_plans.get(listing.ad_id)
        if cached is not None and cached[0] == len(listing.description):
            return cached[1]
        sc = self.config.ai.scout
        try:
            plan = scout_ai.plan(listing, item, self._scout_lookup(listing), bundle_discount=sc.bundle_discount,
                                 pc_discount=sc.pc_discount, min_priced_share=sc.min_priced_share)
        except Exception:  # noqa: BLE001 - a broken reading is no reading
            log.exception("AI scout plan failed for %s", listing.ad_id)
            return None
        self._scout_plans[listing.ad_id] = (len(listing.description), plan)
        return plan

    def _scout_replan(self, listing: Listing, plan: Any) -> Any:
        self._scout_plans.pop(listing.ad_id, None)
        return self._scout_plan_for(listing) or plan

    def _scout_lookup(self, listing: Listing) -> Callable[[ProductRef], PriceEstimate | None]:
        """Market price of a product the scout named, from our own history only (no requests):
        identity keys match like the script's ("same product and kind"), AI keys by attributes."""
        pricing = self.config.pricing

        def lookup(ref: ProductRef) -> PriceEstimate | None:
            if not pricing.use_price_history:
                return None
            days = max(1, pricing.history_days)
            since = utcnow() - timedelta(days=days)
            try:
                if ref.identity is not None:
                    points: list[Any] = self.db.price_history_spans(ref.lookup, since, exclude_ad_id=listing.ad_id)
                    key = ref.identity

                    def fits(c: Comparable) -> bool:
                        return comparable_fits(key, c)
                else:
                    rows = self.db.price_history_keyed(ref.lookup, since, exclude_ad_id=listing.ad_id)
                    points = [(c, first, last) for c, first, last, stored in rows if ai_keys_compatible(ref.key, stored)]

                    def fits(c: Comparable) -> bool:
                        return not _severe_title(c.title)
            except sqlite3.Error as exc:
                log.warning("AI scout: price history of %r unavailable: %s", ref.key, exc)
                return None
            return estimate_from_history(ref.query, points, asking_price_discount=pricing.asking_price_discount,
                                         min_points=pricing.history_min_points, days=days, fits=fits)

        return lookup

    def _scout_remember(self, listings: list[Listing]) -> None:
        """Ads the scout identified where the script's identity knows nothing (typos, product only
        in the text, products the regexes don't know): their prices go into the history under
        the scout's key — the history grows for what the regexes can't read."""
        rows: list[tuple[str, Listing]] = []
        for listing in listings:
            ident = listing_identity(listing)
            if ident.key is not None and ident.category not in (None, "other"):
                continue  # identity knows it: its own key is already stored
            if history_key_for(listing) is None and ident.key is not None:
                continue  # not a price signal (wanted, defect, missing parts, ...)
            plan = self._scout_plan_for(listing)
            if plan is None:
                continue
            from .pricing.estimator import history_worthy

            if not history_worthy(listing):
                continue
            rows += scout_ai.record_rows(listing, plan)
        if not rows:
            return
        try:
            self.db.record_price_points(rows)
        except sqlite3.Error as exc:
            log.warning("AI scout: prices not stored: %s", exc)

    def _deal_possible(self, listing: Listing, search: SearchConfig, estimate: PriceEstimate) -> bool:
        return not market_says_no_deal(evaluate(listing, estimate, None, search, self.config.pricing))

    def _scout_worth(self, listing: Listing, search: SearchConfig, plan: Any) -> bool:
        return scout_ai.worth_a_look(plan, deal_math=lambda est: self._deal_possible(listing, search, est),
                                     min_interest=self.config.ai.scout.min_interest)

    @staticmethod
    def _scout_sees_other(plan: Any, ident: Identity) -> bool:
        """The scout's product differs from what the title's identity says (or it is a bundle
        the script priced as one item)."""
        if plan.kind in scout_ai.BUNDLE_KINDS:
            return ident.kind not in ("bundle", "complete_pc")
        if not plan.identified:
            return False
        if ident.key is None:
            return True
        if plan.ref.identity is None:  # an AI key beats only identity's catch-all reading of a title
            return ident.category in (None, "other")
        return plan.ref.identity.coarse_key() != ident.key.coarse_key()

    def _scout_candidate(self, listing: Listing, plan: Any, skip: Evaluation, summary: RunSummary,
                         counted: bool) -> _Candidate:
        """A dismissed ad the scout brings back for the paid stage."""
        if counted:
            summary.scout_promoted += 1
        log.info("   🔎 %s — %s → второй взгляд: %s", listing.title[:60], _price_label(listing), plan.reason_ru())
        return _Candidate(listing, plan.estimate, plan.estimate, plan=plan, found_by=scout_ai.FOUND_BY_SCOUT,
                          priority=plan.priority(listing), script_skip=skip)

    def _scout_enrich(self, listing: Listing, search: SearchConfig, plan: Any, ident: Identity,
                      known: PriceEstimate | None, hint: PriceEstimate | None, summary: RunSummary,
                      counted: bool) -> Evaluation | _Candidate:
        """A candidate of the script. The script knows the market: unchanged. It doesn't: the
        scout's reading ranks it (junk last) and may give it a market price from history; a solid
        price that says "no deal" ends it here, like the script's own early skip."""
        if known is not None or not plan.item.is_ai:
            return _Candidate(listing, known, hint, plan=plan)
        est = plan.estimate if plan.usable else None
        if est is None or not est.market_price:
            return _Candidate(listing, None, hint, plan=plan, priority=plan.priority(listing))
        found_by = scout_ai.FOUND_BY_SCOUT if self._scout_sees_other(plan, ident) else scout_ai.FOUND_BY_SCRIPT
        if market_says_no_deal(evaluate(listing, est, None, search, self.config.pricing)):
            # the scout never drops an ad on its own (a weak model may have read a cheaper variant):
            # no deal by its price = the script's own way, ranked after the unknown ones
            return _Candidate(listing, None, hint, priority=SCOUT_NO_DEAL_PRIORITY)
        return _Candidate(listing, est, hint, plan=plan, found_by=found_by, priority=plan.priority(listing))

    async def _scout_price(self, listing: Listing, search: SearchConfig, plan: Any,
                           budget: _Budget | None) -> PriceEstimate | None:
        """Paid stage, the history didn't know: comparables for what the SCOUT read — the product
        (its exact key, or its German phrase for AI keys), or the two main parts of a bundle —
        within the comparables budget (never deferred for this)."""
        if plan.kind in scout_ai.BUNDLE_KINDS:
            parts = scout_ai.unpriced_parts(plan)
            for part in parts:
                if part.ref is not None:
                    part.estimate = await self._component_estimate(listing, part.ref, budget)
            if not parts:
                return None
            sc = self.config.ai.scout
            plan = scout_ai.reprice(plan, bundle_discount=sc.bundle_discount, pc_discount=sc.pc_discount,
                                    min_priced_share=sc.min_priced_share)
            return plan.estimate
        if not plan.identified:
            return None
        if plan.ref.identity is not None:
            est = await self._component_estimate(listing, plan.ref, budget)
        elif plan.query and self._listing_target(listing) is None:
            est = await self._market_estimate(listing, search, budget, query=plan.query, defer=False)
        else:
            return None
        if est is not None and est.market_price and plan.item.qty > 1:
            est = est.model_copy(update={"market_price": round(est.market_price * plan.item.qty, 2)})
        return est if est is not None and est.market_price else None

    async def _component_estimate(self, listing: Listing, ref: ProductRef, budget: _Budget | None) -> PriceEstimate | None:
        """Market price of one product the scout named (identity key): history, cache, comparables."""
        assert ref.identity is not None
        target = _Target(ref.query, ref.identity, ref.identity.coarse_key())
        est = self._history_estimate(listing, target)
        if est is not None:
            return est
        cached = self._cached_comps(target)
        if cached is not None:
            return cached if cached.market_price else None
        if budget is not None:
            try:
                if not budget.spend("comps_lookups"):
                    return None
            except _Deferred:
                return None
        try:
            est = await self._comps_estimate(listing, target)
        except RateBudgetExceeded:
            if budget is not None:
                budget.summary.comps_lookups -= 1
            return None
        return est if est.market_price else None

    async def _scout_rescue(self, summary: RunSummary) -> list[DealView]:
        """End of a pass, with the scout time left: a second look at recent ads the script
        dismissed — the ones not read yet are read now; those whose (new or stored) reading
        shows a possible deal go through the paid steps. Returns deals for a digest."""
        if not self.scout_enabled or self._scout is None:
            return []
        sc = self.config.ai.scout
        since = utcnow() - timedelta(hours=max(0.0, sc.backlog_hours))
        try:
            backlog = self.db.scout_backlog(since, min_interest=sc.min_interest, limit=200)
        except sqlite3.Error as exc:
            log.warning("AI scout backlog: %s", exc)
            return []
        if not backlog:
            return []
        self._scout_load([item.ad_id for item in backlog])
        unread = [item for item in backlog if not (item.ad_id in self._scout_items and self._scout_items[item.ad_id].is_ai)]
        if self._scout_mode() != "all":
            unread = [item for item in unread if blind_spot(item)]
        if unread and self._scout_deadline is not None and self._scout.clock() < self._scout_deadline:
            unread.sort(key=lambda item: (-scout_priority(item), -_aware(item.first_seen).timestamp()))
            await self._scout_run(unread, self._scout_deadline, {}, summary, rescue=True)
        budget = self._budget_for(summary)
        instant = self.config.notifications.mode == "instant"
        found: list[tuple[_Candidate, SearchConfig]] = []
        for listing in backlog:
            ev = self.db.get_evaluation(listing.ad_id)
            search = self.config.search_by_name(listing.search_name)
            plan = self._scout_plan_for(listing)
            if ev is None or search is None or plan is None or not self._rescuable(listing, ev, plan, search):
                continue
            summary.scout_promoted += 1
            found.append((_Candidate(listing, plan.estimate, plan.estimate, plan=plan,
                                     found_by=scout_ai.FOUND_BY_SCOUT, priority=plan.priority(listing),
                                     script_skip=ev), search))
        digest: list[DealView] = []
        for cand, search in sorted(found, key=lambda pair: pair[0].discount):
            budget.start_search()
            budget.start_listing()
            log.info("   🔎 второй взгляд: %s — %s", cand.listing.title[:60], _price_label(cand.listing))
            try:
                evaluation = await self._finish(cand, search, budget)
            except (_Deferred, RateBudgetExceeded) as why:
                if isinstance(why, RateBudgetExceeded):
                    self._note_rate_limit(why, summary)
                break  # the pass's budget is gone; the reading is stored, the next pass tries again
            except BlockedError:
                break
            except Exception:  # noqa: BLE001
                log.exception("AI scout rescue of %s failed", cand.listing.ad_id)
                continue
            deal = await self._after_evaluation(cand.listing, search, evaluation, summary, instant=instant)
            if deal is not None:
                digest.append(deal)
        return digest

    def _rescuable(self, listing: Listing, ev: Evaluation, plan: Any, search: SearchConfig) -> bool:
        """Was this a dismissal the scout may overrule (kind veto / early skip, see _triage)?"""
        if ev.found_by == scout_ai.FOUND_BY_SCOUT or ev.verdict != "skip":
            return False
        if ev.stage == "prefilter":
            vetoed = next((kind for kind in SCOUT_OVERRULES if ev.reasons and ev.reasons[0].startswith(_KIND_SKIP_RU[kind])),
                          None)
            if vetoed is None or not _scout_may_overrule(vetoed, plan):
                return False
        elif ev.stage == "market":
            if not self._scout_sees_other(plan, listing_identity(listing)):
                return False
        else:
            return False
        return self._scout_worth(listing, search, plan)

    def _scout_save_stats(self, summary: RunSummary) -> None:
        if self._scout is None:
            return
        sc = self.config.ai.scout
        snap = self._scout.stats.snapshot(share=sc.pass_share, max_per_hour=sc.max_per_hour)
        snap["saved_at"] = utcnow().isoformat()
        try:
            self.db.set_state(SCOUT_STATS_KEY, json.dumps(snap))
        except (sqlite3.Error, AttributeError) as exc:
            log.debug("AI scout stats not saved: %s", exc)

    def scout_status(self) -> dict[str, Any]:
        """For the UI (Настройки → Нейросеть, Состояние): on/off, endpoint, mode, "reads N of M new
        ads per hour", speed, the vision queue."""
        sc = self.config.ai.scout
        enabled = bool(sc.enabled or self._injected["scout"])
        settings = self._scout_llm_settings()
        snap: dict[str, Any] = {}
        if self._scout is not None:
            snap = self._scout.stats.snapshot(share=sc.pass_share, max_per_hour=sc.max_per_hour)
        else:
            try:
                state = self.db.get_state(SCOUT_STATS_KEY)
                snap = json.loads(state[0]) if state and state[0] else {}
            except (sqlite3.Error, ValueError, AttributeError):
                snap = {}
        try:
            waiting = len(self.db.vision_queue())
        except (sqlite3.Error, AttributeError):
            waiting = 0
        return scout_status_view(enabled=enabled, mode_setting=sc.mode, provider=settings.provider,
                                 base_url=settings.base_url, model=settings.model, own_endpoint=bool(sc.base_url.strip()),
                                 snap=snap, vision_waiting=waiting,
                                 vision_wait_minutes=self.config.ai.vision_wait_minutes)

    # ------------------------------------------------------------ vision queue
    def _hold_for_vision(self, listing: Listing, search: SearchConfig, evaluation: Evaluation) -> None:
        """A would-be deal whose photos the (offline) vision model couldn't check waits for it."""
        if (evaluation.would_buy and evaluation.ai_checked is False and self.config.ai.vision_wait_minutes > 0
                and self._evaluator is not None):
            try:
                self.db.queue_vision(listing.ad_id, search.name)
            except sqlite3.Error as exc:
                log.warning("Vision queue: %s", exc)

    def _vision_hold(self, ad_id: str) -> bool:
        wait = self.config.ai.vision_wait_minutes
        if wait <= 0:
            return False
        try:
            queued = self.db.vision_queued_at(ad_id)
        except (sqlite3.Error, AttributeError):
            return False
        return queued is not None and utcnow() - _aware(queued) < timedelta(minutes=wait)

    async def _drain_vision_queue(self, summary: RunSummary) -> None:
        """Start of a pass: would-be deals held for the photo check. The vision model is back:
        check them now; still off after ai.vision_wait_minutes: send them marked "фото не проверены"."""
        try:
            queue = self.db.vision_queue()
        except (sqlite3.Error, AttributeError):
            return
        if not queue:
            return
        wait = timedelta(minutes=max(0.0, self.config.ai.vision_wait_minutes))
        vision_up: bool | None = None
        for ad_id, search_name, queued_at in queue:
            ev, listing = self.db.get_evaluation(ad_id), self.db.get_listing(ad_id)
            if ev is None or listing is None or ev.ai_checked is not False or not ev.would_buy:
                self.db.unqueue_vision([ad_id])
                continue
            search = self.config.search_by_name(search_name) or SearchConfig(
                name=search_name or listing.search_name or "—", purpose=ev.purpose)
            expired = utcnow() - _aware(queued_at) >= wait
            if not expired and self._evaluator is not None and vision_up is not False:
                try:
                    new = await self._conclude(listing, search, ev.estimate, self._source_for(listing), None,
                                               ask_ai=True, plan=self._scout_plan_for(listing),
                                               found_by=ev.found_by or scout_ai.FOUND_BY_SCRIPT)
                except (BlockedError, RateBudgetExceeded, EbayAPIError):
                    vision_up = False
                    continue
                except Exception:  # noqa: BLE001
                    log.exception("Vision re-check of %s failed", ad_id)
                    vision_up = False
                    continue
                summary.ai_calls += 1
                if new.ai_checked is True:
                    vision_up = True
                    self.db.unqueue_vision([ad_id])
                    log.info("📷 Фото проверены после ожидания: %s → %s", listing.title[:60], new.verdict)
                    self._emit("deal_updated", self._deal_event(listing, search.name, new))
                    await self._after_evaluation(listing, search, new, summary, instant=True, count=False)
                    continue
                vision_up = False
            if expired:
                self.db.unqueue_vision([ad_id])
                log.info("📷 Нейросеть так и не проверила фото «%s» — присылаю с пометкой", listing.title[:60])
                await self._after_evaluation(listing, search, ev, summary, instant=True, count=False)
            else:
                summary.vision_waiting += 1

    # ---------------------------------------------------------- after a verdict
    def _deal_event(self, listing: Listing, search_name: str, evaluation: Evaluation) -> dict[str, Any]:
        """deal_found / deal_updated payload: the old keys, plus found_by, tier, the listing, the
        evaluation and (when the web layer is installed) the deal card."""
        payload: dict[str, Any] = {
            "ad_id": listing.ad_id, "search_name": search_name, "title": listing.title,
            "verdict": evaluation.verdict, "action": evaluation.action, "score": evaluation.score,
            "found_by": evaluation.found_by,
            "tier": deal_tier(evaluation, self.config.notifications.super_deals),
            "listing": listing.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }
        try:
            from .web.api.presenters import deal_card

            deal = self.db.get_deal_extras(listing.ad_id)
            if deal is not None:
                payload["card"] = deal_card(deal[0], deal[1], config=self.config)
        except Exception:  # noqa: BLE001 - an event must never break monitoring
            log.debug("deal card for the event failed", exc_info=True)
        return payload

    async def _after_evaluation(self, listing: Listing, search: SearchConfig, evaluation: Evaluation,
                                summary: RunSummary, *, instant: bool, count: bool = True) -> DealView | None:
        """Count, announce and alert one evaluated ad. «🔥 Супер-находка» goes out at once (past
        the hourly cap and the digest mode); in digest mode other deals are returned for it."""
        if count and evaluation.verdict == "buy":
            summary.deals_found += 1
            if evaluation.found_by == scout_ai.FOUND_BY_SCOUT:
                summary.scout_deals += 1
        if evaluation.verdict == "buy" or evaluation.action in DEAL_EVENT_ACTIONS:
            self._emit("deal_found", self._deal_event(listing, search.name, evaluation))
        if not self._should_notify(evaluation):
            return None
        deal = self.db.get_deal(listing.ad_id)
        if deal is None:
            return None
        if deal_tier(evaluation, self.config.notifications.super_deals) == TIER_SUPER:
            await self._alert(deal, summary, super_find=True)
            return None
        if instant:  # a good deal is gone in minutes: don't wait for the rest of the search
            await self._alert(deal, summary)
            return None
        return deal

    # --------------------------------------------------------- notifications
    def _channels(self) -> list[str]:
        return [getattr(n, "name", "?") for n in self._notifiers or []]

    def _should_notify(self, evaluation: Evaluation) -> bool:
        """A deal the user should hear about, not yet delivered on every channel. With the AI
        down, would-be buys go out too (notifications.unchecked_deals), marked by the renderer."""
        cfg = self.config.notifications
        if evaluation.no_alert:
            return False
        unchecked = (evaluation.would_buy and evaluation.ai_checked is False and cfg.unchecked_deals
                     and "buy" in cfg.verdicts)
        if unchecked and self._vision_hold(evaluation.ad_id):
            return False  # waiting for the vision model (ai.vision_wait_minutes)
        if not unchecked and (evaluation.verdict not in cfg.verdicts or evaluation.score < cfg.min_score):
            return False
        deal = self.db.get_deal(evaluation.ad_id)
        if deal is None or deal.status == "ignored":
            return False
        channels = self._channels()
        if not channels:
            return not self.db.was_notified(evaluation.ad_id)
        return any(not self.db.was_notified(evaluation.ad_id, ch) for ch in channels)

    async def _alert(self, deal: DealView, summary: RunSummary, *, super_find: bool = False) -> None:
        """Instant alert for one deal: repost check, then the hourly cap (overflow → digest later).
        A «🔥 Супер-находка» skips the cap and gets its own headline."""
        if self._is_repost(deal.listing):
            return
        if super_find:
            delivered = await self._notify([deal], summary, title=super_title(deal))
            summary.notified += delivered
            summary.super_deals += 1 if delivered else 0
            return
        if self._alert_cap_reached():
            self.db.queue_alert(deal.listing.ad_id)
            summary.queued_alerts += 1
            log.info("Лимит %d уведомлений в час: «%s» пришлю позже одной сводкой",
                     self.config.notifications.max_alerts_per_hour, deal.listing.title[:50])
            return
        delivered = await self._notify([deal], summary)
        if delivered:
            self._count_alert_message()
        summary.notified += delivered

    def _alert_window(self) -> list[datetime]:
        state = self.db.get_state("alerts:window")
        stamps: list[datetime] = []
        for raw in (state[0].split(",") if state and state[0] else []):
            try:
                stamps.append(_aware(datetime.fromisoformat(raw)))
            except ValueError:
                continue
        hour_ago = utcnow() - timedelta(hours=1)
        return [t for t in stamps if t >= hour_ago]

    def _alert_cap_reached(self) -> bool:
        cap = self.config.notifications.max_alerts_per_hour
        return cap > 0 and len(self._alert_window()) >= cap

    def _count_alert_message(self) -> None:
        window = self._alert_window() + [utcnow()]
        self.db.set_state("alerts:window", ",".join(t.isoformat() for t in window))

    def _is_repost(self, listing: Listing) -> bool:
        """The same seller re-posting the same thing at ~the same price (±5 %) within 14 days
        was already sent: don't alert again."""
        seller = (listing.seller_name or "").strip().lower()
        if not seller or listing.price is None:
            return False
        key = history_key_for(listing) or listing_identity(listing).history_key()
        if not key:
            return False
        for old in self.db.notified_since(utcnow() - REPOST_WINDOW):
            if old.ad_id == listing.ad_id or (old.seller_name or "").strip().lower() != seller:
                continue
            if old.price is None or abs(old.price - listing.price) > REPOST_PRICE_TOLERANCE * old.price:
                continue
            if listing_identity(old).history_key() != key:
                continue
            log.info("Повтор объявления «%s» (%s, как %s) — уже присылал, не шлю", listing.title[:50],
                     seller, old.ad_id)
            self.db.mark_notified(listing.ad_id, "_repost")
            return True
        return False

    async def _notify(self, deals: list[DealView], summary: RunSummary, *, title: str | None = None) -> int:
        """Send `deals` on every channel that hasn't delivered them yet; a failed channel is
        recorded (retried on later passes) and reported through the other channels."""
        if not self._notifiers:
            return 0
        deals = sorted(
            deals, key=lambda d: d.evaluation.score if d.evaluation else 0, reverse=True
        )
        delivered: set[str] = set()
        for notifier in self._notifiers:
            name = getattr(notifier, "name", "?")
            todo = [d for d in deals if not self.db.was_notified(d.listing.ad_id, name)]
            if not todo:
                continue
            try:
                if title:
                    await notifier.send(todo, title=title)
                else:
                    await notifier.send(todo)  # notifiers build an informative subject themselves
            except Exception as exc:
                log.warning("Notifier %s failed: %s", name, exc)
                human = humanize(exc, name)
                _add_error(summary, f"Уведомление через {_channel_label(name)} не ушло: {human.message_ru}",
                           human.details)
                for deal in todo:
                    self.db.note_delivery_failure(deal.listing.ad_id, name, human.message_ru)
                await self._health(f"notify:{name}", f"⚠ Не удалось отправить уведомление через {_channel_label(name)}:"
                                   f" {human.message_ru}. Повторю при следующих проверках.", summary, exclude={name})
                continue
            for deal in todo:
                self.db.mark_notified(deal.listing.ad_id, name)
                delivered.add(deal.listing.ad_id)
        return len(delivered)

    async def _retry_deliveries(self, summary: RunSummary) -> None:
        """Channels that failed on earlier passes get the deal again (up to 5 tries in 24 h)."""
        if not self._notifiers:
            return
        by_name = {getattr(n, "name", "?"): n for n in self._notifiers}
        groups: dict[str, list[DealView]] = {}
        for ad_id, channel in self.db.retryable_deliveries(
                max_attempts=MAX_DELIVERY_ATTEMPTS, since=utcnow() - DELIVERY_RETRY_WINDOW):
            deal = self.db.get_deal(ad_id)
            if channel in by_name and deal is not None and deal.status != "ignored":
                groups.setdefault(channel, []).append(deal)
        for channel, deals in groups.items():
            notifier = by_name[channel]
            try:
                await notifier.send(deals)
            except Exception as exc:
                log.info("Retry via %s failed: %s", channel, exc)
                for deal in deals:
                    tries = self.db.note_delivery_failure(deal.listing.ad_id, channel, humanize(exc, channel).message_ru)
                    if tries >= MAX_DELIVERY_ATTEMPTS:
                        log.warning("Уведомление о %s через %s так и не ушло (%d попыток)", deal.listing.ad_id,
                                    channel, tries)
                continue
            for deal in deals:
                self.db.mark_notified(deal.listing.ad_id, channel)
            summary.notified += len(deals)
            log.info("Повторная отправка через %s: %d сделок", channel, len(deals))

    async def _flush_alert_queue(self, summary: RunSummary) -> None:
        """Deals held back by the hourly cap go out as one digest once there is room again."""
        queued = self.db.queued_alerts()
        if not queued or self._alert_cap_reached():
            return
        deals = []
        for ad_id in queued:
            deal = self.db.get_deal(ad_id)
            if deal is not None and deal.status != "ignored" and deal.evaluation is not None:
                deals.append(deal)
        self.db.unqueue_alerts(queued)
        if not deals:
            return
        cap = self.config.notifications.max_alerts_per_hour
        n = len(deals)
        delivered = await self._notify(deals, summary, title=f"Ещё {n} {_deals_word(n)} — сверх лимита {cap} в час")
        if delivered:
            self._count_alert_message()
        summary.notified += delivered

    # ------------------------------------------------------------ health
    async def _health(self, kind: str, text: str, summary: RunSummary, *, exclude: set[str] | None = None,
                      every: timedelta | None = None) -> bool:
        """A service message ("AI down", "site blocks us", ...) through every channel that can
        send one; the same kind at most once per 6 hours (persisted)."""
        self._emit_health(kind, text, every or HEALTH_ALERT_EVERY)
        if not self.config.notifications.health_alerts or not self._notifiers:
            return False
        key = f"alert:{kind}"
        state = self.db.get_state(key)
        if state is not None and utcnow() - _aware(state[1]) < (every or HEALTH_ALERT_EVERY):
            return False
        sent = await self._send_text(text, exclude=exclude)
        if sent:
            self.db.set_state(key, text)
            summary.health_alerts += 1
        return sent

    def _emit_health(self, kind: str, text: str, every: timedelta) -> None:
        """health_alert event for the web UI (even without notification channels), the same
        kind at most once per `every` per process."""
        last = self._event_alerts.get(kind)
        now = utcnow()
        if last is not None and now - last < every:
            return
        self._event_alerts[kind] = now
        # text: as sent to Telegram / e-mail; text_ru: for a UI toast (it has its own icon, no "⚠")
        self._emit("health_alert", {"kind": kind, "text": text, "text_ru": text.lstrip("⚠️ ").strip(),
                                    "at": now.isoformat(), "at_label": when_label(now)})

    async def _send_text(self, text: str, *, exclude: set[str] | None = None) -> bool:
        sent = False
        for notifier in self._notifiers or []:
            name = getattr(notifier, "name", "?")
            send_text = getattr(notifier, "send_text", None)
            if send_text is None or name in (exclude or set()):
                continue
            try:
                await send_text(text)
                sent = True
            except Exception as exc:  # noqa: BLE001 - a service message must never break a pass
                log.warning("Служебное сообщение через %s не отправлено: %s", name, exc)
        return sent

    async def _check_search_health(self, search: SearchConfig, found: int, summary: RunSummary,
                                   error: str = "") -> None:
        """Two passes in a row without a single ad (or with an unreadable page): say so."""
        key = f"streak:{search.name}"
        state = self.db.get_state(key)
        if found and not error:
            if state is not None and state[0] != "0":
                self.db.set_state(key, "0")
            return
        streak = (int(state[0]) if state is not None and state[0].isdigit() else 0) + 1
        self.db.set_state(key, str(streak))
        if streak < 2:
            return
        if error:
            text = (f"⚠ Поиск «{search.name}»: страницу Kleinanzeigen не получается разобрать уже {streak} проверки"
                    " подряд — возможно, сайт изменился. Если так и останется, обнови программу.")
        else:
            text = (f"⚠ Поиск «{search.name}» уже {streak} проверки подряд не находит ни одного объявления —"
                    " проверь ссылку и фильтры.")
        await self._health(f"search:{search.name}", text, summary)

    async def _after_run(self, summary: RunSummary) -> None:
        """End of a pass: the AI health check, held-back alerts, the daily heartbeat."""
        if self._evaluator is not None and self._ai_failed and not self._ai_answered:
            wait = self.config.ai.vision_wait_minutes
            tail = ""
            if self.config.notifications.unchecked_deals:
                tail = (f" Выгодные по цене объявления жду до {wait:.0f} мин, потом присылаю с пометкой"
                        " «фото не проверены»." if wait > 0 else
                        " Выгодные по цене объявления присылаю с пометкой «фото не проверены».")
            hint = "Запусти приложение Ollama" if self.config.ai.provider == "ollama" else AI_DOWN_HINT_RU
            await self._health("ai_down", f"⚠ {AI_DOWN_RU}. {hint}." + tail, summary)
        await self._flush_alert_queue(summary)
        await self._maybe_heartbeat(summary)
        await self._maybe_daily_top(summary)

    async def _maybe_daily_top(self, summary: RunSummary) -> None:
        """«Топ за день»: once a day at notifications.daily_top.hour (local time), the best deals
        of the last 24 h per search as one message — including ones already sent."""
        cfg = self.config.notifications.daily_top
        if not cfg.enabled or not self._notifiers:
            return
        local = now_local()
        today = local.date().isoformat()
        state = self.db.get_state("daily_top")
        if local.hour < cfg.hour or (state is not None and state[0] == today):
            return
        deals = self.db.top_deals_since(utcnow() - timedelta(days=1), verdicts=cfg.verdicts,
                                        per_search=max(1, cfg.per_search))
        self.db.set_state("daily_top", today)
        if not deals:
            return
        title = f"Топ за день: {len(deals)} {_deals_word(len(deals))}"
        for notifier in self._notifiers:
            try:
                await notifier.send(deals, title=title)
            except Exception as exc:  # noqa: BLE001 - a digest never breaks a pass
                log.warning("Топ за день через %s не ушёл: %s", getattr(notifier, "name", "?"), exc)
        summary.health_alerts += 1

    async def _maybe_heartbeat(self, summary: RunSummary) -> None:
        """Once a day at notifications.heartbeat_hour (local time): "still alive" + 24 h stats."""
        hour = self.config.notifications.heartbeat_hour
        if hour is None or not self._notifiers:
            return
        local = now_local()  # general.timezone, not the host's (UTC in Docker)
        today = local.date().isoformat()
        state = self.db.get_state("heartbeat")
        if local.hour < hour or (state is not None and state[0] == today):
            return
        since = utcnow() - timedelta(days=1)
        runs = [r for r in self.db.list_runs(limit=2000) if _aware(r.started_at) >= since and r.id != summary.id]
        runs.append(summary)
        checked = sum(r.evaluated for r in runs)
        deals = sum(r.deals_found for r in runs)
        errors = sum(len(r.errors) for r in runs)
        text = f"Жив: за сутки проверено {checked} объявлений, {deals} выгодных, ошибок {errors}"
        if await self._send_text(text):
            self.db.set_state("heartbeat", today)
            summary.health_alerts += 1


AI_DOWN_HINT_RU = "Открой LM Studio → Developer → Start Server"
_CHANNEL_LABELS = {"telegram": "Telegram", "email": "почту"}


def _channel_label(name: str) -> str:
    return _CHANNEL_LABELS.get(name, name)


def _add_error(summary: RunSummary, text: str, details: str = "") -> None:
    """A run error for the web UI (plain Russian) + its technical text (for «Подробнее»)."""
    summary.errors.append(text)
    summary.error_details.append(details or text)


def _add_exc(summary: RunSummary, prefix: str, exc: BaseException, service: str = "") -> None:
    human = humanize(exc, service)
    _add_error(summary, f"{prefix}: {human.message_ru}", human.details)


def _deals_word(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "сделка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "сделки"
    return "сделок"


def summary_buy_share(db: Database, listings: list[Listing]) -> float:
    verdicts = [ev.verdict for item in listings if (ev := db.get_evaluation(item.ad_id)) is not None]
    return sum(v == "buy" for v in verdicts) / len(verdicts) if verdicts else 0.0


# kind vetoes the scout may overrule: only where hidden value is plausible AND the scout reads the
# ad the same way (a PC priced by its parts, a laptop as an identified product). Never "part"
# (missing components: "Switch nur Tablet"), accessories, defects, boxes, wanted/swap/service ads.
SCOUT_OVERRULES = {"complete_pc": scout_ai.BUNDLE_KINDS, "laptop": frozenset({"single"})}
SCOUT_NO_DEAL_PRIORITY = 0.9  # rank of a candidate the scout's price calls no deal (after the unknown ones)


def _scout_may_overrule(kind: str, plan: Any) -> bool:
    allowed = SCOUT_OVERRULES.get(kind)
    if allowed is None or plan.kind not in allowed:
        return False
    return plan.kind != "single" or plan.identified


def make_scout_llm(settings: LLMSettings) -> Any:
    """The scout's text client: thinking off (Qwen3.x small models, docs/design/AI_MODELS.md §5)."""
    if settings.provider in ("openai", "ollama"):
        from .ai.client import VisionLLM

        extra: dict[str, Any] = ({"think": False} if settings.provider == "ollama"
                                 else {"chat_template_kwargs": {"enable_thinking": False}})
        return VisionLLM(settings, extra_body=extra)
    return make_llm(settings)


def _capped_maybe(evaluation: Evaluation, warning: str) -> Evaluation:
    """At most "maybe" (score ≤ 65), with the warning as a reason."""
    update: dict[str, Any] = {"reasons": [*evaluation.reasons, warning], "score": min(evaluation.score, 65.0)}
    if evaluation.verdict == "buy":
        update["verdict"] = "maybe"
        if evaluation.action == "buy":
            update["action"] = "watch"
    return evaluation.model_copy(update=update)


def _cross_check(estimate: PriceEstimate, verdict: AIVerdict | None) -> PriceEstimate:
    """Comparables say one thing, the model (which saw the photos) another: be careful —
    for buying decisions an inflated market price is the expensive mistake."""
    if (
        verdict is None or verdict.confidence <= 0 or not verdict.estimated_market_price
        or estimate.market_price is None or estimate.source == "reference"
    ):
        return estimate
    ai_price = verdict.estimated_market_price
    if estimate.market_price > ai_price * 1.5:
        careful = round(max(ai_price, estimate.market_price / 1.5), 2)
        note = (f"Аналоги дают ~{estimate.market_price:.0f} €, нейросеть ~{ai_price:.0f} € —"
                f" взял осторожную оценку {careful:.0f} €")
        return estimate.model_copy(update={
            "market_price": careful,
            "notes": f"{estimate.notes}. {note}" if estimate.notes else note,
        })
    return estimate


_VERDICT_RU = {"buy": "ПОКУПАТЬ", "maybe": "подумать", "skip": "пропустить"}


def _profit_note(ev: Evaluation) -> str:
    if ev.expected_profit is None:
        return ""
    label = "экономия" if ev.purpose == "personal" else "прибыль"
    note = f", {label} ≈ {ev.expected_profit:.0f} €"
    if ev.estimate.market_price is not None:
        note += f" (рынок ~{ev.estimate.market_price:.0f} €)"
    return note


def _price_label(listing: Listing) -> str:
    if listing.price_text:
        return listing.price_text
    if listing.is_free:
        return "бесплатно"
    return f"{listing.price:g} €" if listing.price is not None else "цена ?"


def _comparables_for_ai(estimate: PriceEstimate) -> list[Comparable]:
    """The comparables to show the model: the most typical ones (closest to the market
    price), not the cheapest — those are often defects or other variants."""
    comps = [c for c in estimate.comparables if c.source != "reference" and c.price and c.price > 0]
    if not comps:
        return []
    n = MAX_PROMPT_COMPARABLES
    if estimate.market_price:
        market = estimate.market_price
        chosen = sorted(comps, key=lambda c: abs(c.price - market) / market)[:n]
    else:
        ordered = sorted(comps, key=lambda c: c.price)
        step = max(1, len(ordered) / n)
        chosen = [ordered[int(i * step)] for i in range(min(n, len(ordered)))]
    return sorted(chosen, key=lambda c: c.price)


def _accepts(func: Any, name: str) -> bool:
    """Does `func` take a keyword argument `name`? (Test fakes may not.)"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _minutes_left(exc: BaseException) -> str:
    until = getattr(exc, "cooldown_until", None)
    if not isinstance(until, datetime):
        return ""
    minutes = max(1, round((_aware(until) - utcnow()).total_seconds() / 60))
    return f" ещё {minutes} мин"


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _is_category_scan(search: SearchConfig) -> bool:
    """A whole category (no keywords, no pasted URL), e.g. every new ad in "Handy & Telefon"."""
    return search.category_id is not None and not search.query.strip() and not search.url


def _price_filtered(search: SearchConfig) -> bool:
    """Does the site only return a price range? (Its results are not the whole market.)
    Category scans ask the site for everything and apply the range locally."""
    if search.url:
        return bool(_URL_PRICE_FILTER_RE.search(search.url))
    if _is_category_scan(search):
        return False
    return (search.min_price or 0) > 0 or search.max_price is not None


def _request_settings(general: GeneralConfig) -> tuple[Any, ...]:
    return (tuple(general.request_delay_seconds), general.request_timeout_seconds, general.user_agent,
            general.max_requests_per_hour, tuple(general.block_cooldown_hours))


def _until_text(exc: BaseException) -> str:
    until = getattr(exc, "cooldown_until", None)
    if not isinstance(until, datetime):
        return ""
    return " " + at_label(until)  # " в 21:34" in the user's time zone (general.timezone), not the host's


def _severe_title(title: str) -> bool:
    return any(f in SEVERE_FLAGS for f in detect_red_flags(title))


def _is_model_key(key: str) -> bool:
    """Does the key name a concrete model? pricing.identity decides when installed (it
    recognises a product), else: the key contains a model number."""
    try:
        from .pricing import identity
    except ImportError:
        identity = None  # type: ignore[assignment]
    key_of = getattr(identity, "product_key", None)
    if key_of is not None:
        try:
            return key_of(key) is not None
        except Exception:  # noqa: BLE001
            pass
    return is_model_key(key)


def _price_dropped_since(evaluation: Evaluation, current: Listing) -> bool:
    """Is the ad now clearly cheaper than when it was evaluated? (Compared with the price at
    evaluation time, so slow step-by-step reductions add up.)"""
    before = evaluation.buy_price
    if before is None or before <= 0:
        return False
    if current.is_free:
        return True
    if current.price is None or current.price <= 1:
        return False
    return current.price + (current.shipping_cost or 0.0) <= before * PRICE_DROP_RATIO


def _price_dropped(previous: Listing, current: Listing) -> bool:
    return (
        previous.price is not None
        and current.price is not None
        and previous.price > 0
        and current.price <= previous.price * PRICE_DROP_RATIO
    )


def _close_soon(obj: Any) -> None:
    """Close an async resource in the background; always returns None."""
    if obj is None:
        return None
    try:
        asyncio.get_running_loop().create_task(obj.aclose())
    except RuntimeError:  # no loop running: close synchronously
        asyncio.run(obj.aclose())
    return None
