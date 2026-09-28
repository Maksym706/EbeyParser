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
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .ai.claude import make_llm
from .ai.evaluator import AIEvaluator
from .config import AppConfig, GeneralConfig, SearchConfig
from .db import Database
from .models import AIVerdict, Comparable, DealView, Evaluation, Listing, PriceEstimate, RunSummary, utcnow
from .notify.base import build_notifiers
from .pricing.estimator import (
    below_min_price,
    estimate_from_comparables,
    estimate_from_history,
    evaluate,
    find_reference_price,
    history_worthy,
    market_says_no_deal,
    no_deal_reasons,
    prefilter,
    prefilter_score,
    relevant_comparables,
)
from .pricing.text import is_model_key, make_search_query, normalize
from .scraper.ebay_api import EbayAPIError, EbayBrowseClient
from .scraper.ebay_sold import EbaySoldScraper
from .scraper.http import BlockedError, PoliteClient
from .scraper.kleinanzeigen import KleinanzeigenScraper, PageLayoutError

log = logging.getLogger(__name__)

COMPS_CACHE_TTL = 6 * 3600  # seconds
PRICE_DROP_RATIO = 0.9  # re-evaluate a known ad when its price falls by 10 %+
PENDING_MAX_AGE = timedelta(days=2)  # older deferred ads are probably gone: not worth a request
PENDING_MIN_BATCH = 50  # stored-but-unevaluated ads pulled back per search and pass (at least)
RECHECK_AFTER = timedelta(hours=6)  # an ad skipped by market data is re-checked when seen again
# early "no deal" from our own price history needs a solid sample of a concrete model
EARLY_SKIP_MIN_POINTS = 8
EARLY_SKIP_MAX_SPREAD = 0.35
UNKNOWN_DISCOUNT = 0.75  # candidates with an unknown market rank after clear bargains

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


class AlreadyRunningError(RuntimeError):
    pass


@dataclass
class _Candidate:
    """An ad that survived the free part of the funnel."""

    listing: Listing
    estimate: PriceEstimate | None  # reference / history market price; None = look up comparables
    hint: PriceEstimate | None = None  # any price known so far (only to rank candidates)

    @property
    def discount(self) -> float:
        """Buy cost / market price: lower = more promising. Free ads first."""
        listing = self.listing
        if listing.is_free:
            return 0.0
        est = self.estimate or self.hint
        if listing.price is None or listing.price <= 1 or est is None or not est.market_price:
            return UNKNOWN_DISCOUNT
        return (listing.price + (listing.shipping_cost or 0.0)) / est.market_price


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
    ):
        self.config = config
        self.db = db
        self.web_base_url = web_base_url
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
        }
        self._second_calls = 0
        self._budget: _Budget | None = None
        self._comps_cache: dict[str, tuple[float, PriceEstimate]] = {}
        self._ebay_blocked = False
        self._running = False
        self.last_summary: RunSummary | None = None
        self.next_run_at = None

    # ------------------------------------------------------------ lifecycle
    @property
    def is_running(self) -> bool:
        return self._running

    def update_config(self, config: AppConfig) -> None:
        """Apply a new config; network/AI components are rebuilt lazily."""
        self.config = config
        self._comps_cache.clear()
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

    def _ensure_components(self) -> None:
        if self._client is None and not (self._injected["scraper"] and self._injected["ebay"]):
            self._client = PoliteClient.from_config(self.config.general)
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
        if self._notifiers is None:
            self._notifiers = build_notifiers(
                self.config.notifications, web_base_url=self.web_base_url
            )

    async def aclose(self) -> None:
        for name in ("_llm", "_second_llm", "_ebay_api"):
            obj = getattr(self, name)
            if obj is not None:
                await obj.aclose()
                setattr(self, name, None)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ai_health(self) -> dict[str, Any]:
        ai = self.config.ai
        base = {"provider": ai.provider, "base_url": ai.base_url, "model": ai.model}
        if not ai.enabled:
            return {**base, "ok": False, "enabled": False, "model_available": False,
                    "error": "ИИ выключен в конфиге (ai.enabled: false)"}
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
        while not stop.is_set():
            try:
                await self.run_once()
            except AlreadyRunningError:
                pass
            except Exception:  # never let the loop die
                log.exception("Monitoring run crashed")
            interval = max(1.0, self.config.general.interval_minutes) * 60
            interval *= random.uniform(0.9, 1.1)  # don't hit the site on an exact beat
            self.next_run_at = utcnow() + timedelta(seconds=interval)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        self.next_run_at = None

    async def run_once(self) -> RunSummary:
        if self._running:
            raise AlreadyRunningError("Проверка уже идёт")
        searches = [s for s in self.config.searches if s.enabled]
        if not searches:
            summary = RunSummary(finished_at=utcnow())
            summary.errors.append("Нет активных поисков — добавь их на странице «Поиски» или в config.yaml")
            self.last_summary = summary
            return summary

        self._running = True
        self._ebay_blocked = False
        self._second_calls = 0
        summary = self.db.start_run()
        digest: list[DealView] = []
        try:
            self._ensure_components()
            self._prune_history()
            for search in searches:
                summary.searches += 1
                try:
                    deals = await self._process_search(search, summary)
                except BlockedError as exc:
                    summary.errors.append(
                        f"Kleinanzeigen ограничил запросы ({exc}). Проверка остановлена — "
                        "увеличь general.interval_minutes и request_delay_seconds."
                    )
                    log.warning("Blocked by Kleinanzeigen: %s", exc)
                    break
                except (EbayAPIError, PageLayoutError) as exc:
                    log.warning("Search %r: %s", search.name, exc)
                    summary.errors.append(f"{search.name}: {exc}")
                    continue
                except httpx.HTTPError as exc:
                    log.warning("Search %r: network error %s", search.name, exc)
                    summary.errors.append(f"{search.name}: ошибка сети — {exc}")
                    continue
                except Exception as exc:
                    log.exception("Search %r failed", search.name)
                    summary.errors.append(f"{search.name}: {exc}")
                    continue
                digest.extend(deals)  # instant mode: already sent while evaluating
            if digest:
                summary.notified += await self._notify(digest, summary)
        finally:
            summary.finished_at = utcnow()
            self.db.finish_run(summary)
            self.last_summary = summary
            self._running = False
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
                    "Для поиска по eBay заполни ebay.client_id и ebay.client_secret в config.yaml/.env"
                )
            return self._ebay_api
        assert self._scraper is not None
        return self._scraper

    async def _process_search(self, search: SearchConfig, summary: RunSummary) -> list[DealView]:
        """One search: scrape, store, feed the price history, run the funnel. Instant
        notifications go out as soon as a deal is found; returns the deals for a digest."""
        max_pages = search.max_pages or self.config.general.max_pages
        listings = await self._source_for(search).search(search, max_pages=max_pages)
        summary.listings_seen += len(listings)
        before = {name: getattr(summary, name) for name in _FUNNEL_COUNTERS}
        evaluated_before = summary.evaluated

        baseline = self.config.general.baseline_first_run and not self.db.search_has_run(search.name)
        baseline_at = self.db.search_baseline_at(search.name)
        todo: list[Listing] = []
        price_dropped: set[str] = set()
        rechecks: set[str] = set()
        new_here = 0
        for listing in listings:
            listing.search_name = search.name
            previous = self.db.get_listing(listing.ad_id)
            self.db.upsert_listing(listing)
            if previous is None:
                new_here += 1
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
        self._remember_prices(search, listings)

        if baseline:
            self.db.mark_search_run(search.name, baseline=True)
            log.info("🎓 %s: обучение — собрал цены %d объявлений; оценивать и присылать сделки начну"
                     " со следующей проверки", search.name, len(listings))
            return []
        self.db.mark_search_run(search.name)

        # deferred ads that have dropped off the result pages since
        on_page = {listing.ad_id for listing in listings}
        backlog = [l for l in self._pending(search, baseline_at) if l.ad_id not in on_page]
        todo = sorted(backlog + todo, key=lambda l: _aware(l.first_seen))  # oldest first
        log.info("🔎 %s: объявлений %d, новых %d, к оценке %d%s", search.name, len(listings), new_here,
                 len(todo) - len(rechecks), f" (из очереди {len(backlog)})" if backlog else "")

        budget = self._budget_for(summary)
        budget.start_search()
        instant = self.config.notifications.mode == "instant"
        to_notify: list[DealView] = []

        # phase 1 — free: filters, reference price / price history, early "no deal"
        candidates: list[_Candidate] = []
        for listing in todo:
            recheck = listing.ad_id in rechecks
            try:
                outcome = self._triage(listing, search, budget, recheck=recheck)
            except Exception as exc:
                log.exception("Evaluating %s failed", listing.ad_id)
                summary.errors.append(f"{search.name} / {listing.title[:40]}: {exc}")
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
            except _Deferred as why:
                summary.deferred += 1
                if listing.ad_id in price_dropped:  # the old verdict is stale: retry at the new price
                    self.db.delete_evaluation(listing.ad_id)
                log.info("         → отложено до следующей проверки: %s", why)
                continue
            except BlockedError:
                raise
            except Exception as exc:
                log.exception("Evaluating %s failed", listing.ad_id)
                summary.errors.append(f"{search.name} / {listing.title[:40]}: {exc}")
                continue
            summary.evaluated += 1
            why = f" — {evaluation.reasons[0]}" if evaluation.verdict == "skip" and evaluation.reasons else ""
            log.info("         → %s, балл %.0f%s%s", _VERDICT_RU.get(evaluation.verdict, evaluation.verdict),
                     evaluation.score, _profit_note(evaluation), why)
            if evaluation.verdict == "buy":
                summary.deals_found += 1
            if self._should_notify(evaluation):
                deal = self.db.get_deal(listing.ad_id)
                if deal is None:
                    continue
                if instant:  # a good deal is gone in minutes: don't wait for the rest of the search
                    summary.notified += await self._notify([deal], summary)
                else:
                    to_notify.append(deal)

        evaluated_here = summary.evaluated - evaluated_before
        if evaluated_here >= 6 and summary_buy_share(self.db, todo) >= 0.7:
            fix = ("задай reference_price для этого поиска" if search.query.strip()
                   else "задай pricing.reference_prices для таких товаров")
            warning = (
                f"{search.name}: «покупать» у большинства объявлений — похоже, оценка рынка завышена."
                f" Проверь цены вручную или {fix}."
            )
            log.warning(warning)
            summary.errors.append(warning)
        self._log_funnel(search, summary, before, evaluated_here)
        return to_notify

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

    def _remember_prices(self, search: SearchConfig, listings: list[Listing]) -> None:
        """Feed the price history with this page of results: single items with a real price
        only. Not from price-filtered searches — a list cut at max_price drags medians down."""
        if _price_filtered(search):
            return
        rows = [(key, l) for l in listings if history_worthy(l) and (key := make_search_query(l.title))]
        if not rows:
            return
        try:
            self.db.record_price_points(rows)
        except sqlite3.Error as exc:
            log.warning("Price history: can't store prices of %r: %s", search.name, exc)

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
        keep, reasons = prefilter(listing, search)
        if keep:
            floor = search.min_price if search.min_price is not None else self.config.general.min_listing_price
            too_cheap = below_min_price(listing, floor)
            if too_cheap:
                keep, reasons = False, [too_cheap]
        if not keep:
            if counted:
                budget.summary.prefiltered += 1
            return self._save_skip(listing, search, reasons)

        known = self._reference_estimate(listing, search)
        if known is None:
            known = self._history_estimate(listing, make_search_query(listing.title))
        hint = known
        if known is not None:
            best = evaluate(listing, known, None, search, self.config.pricing)
            if market_says_no_deal(best):
                if not self._may_skip_early(known):
                    known = None  # thin/unclear history says "no deal": check comparables first
                else:
                    if counted:
                        budget.summary.early_skips += 1
                        budget.summary.history_hits += known.source == "history"
                    return self._save(best.model_copy(update={"reasons": no_deal_reasons(best), "stage": "market"}))
            if known is not None and known.source == "history" and counted:
                budget.summary.history_hits += 1
        return _Candidate(listing, known, hint)

    def _may_skip_early(self, est: PriceEstimate) -> bool:
        """Is this market price solid enough to drop an ad without looking at it?"""
        if est.source == "reference":
            return True
        if est.source != "history" or est.sample_size < EARLY_SKIP_MIN_POINTS:
            return False
        if est.low is None or est.high is None or not est.market_price:
            return False
        if (est.high - est.low) / est.market_price > EARLY_SKIP_MAX_SPREAD:
            return False
        return _is_model_key(est.query)

    async def _finish(self, cand: _Candidate, search: SearchConfig, budget: _Budget) -> Evaluation:
        """Paid part of the funnel: ad page → prefilter on the full text → comparables (only
        if the market is still unknown) → local AI. Raises _Deferred when a budget is used up."""
        self._ensure_components()
        listing, estimate = cand.listing, cand.estimate
        source = self._source_for(listing)
        pricing = self.config.pricing

        wants_ai = estimate is not None and self._wants_ai(listing, estimate, search)
        if wants_ai and not budget.check("ai_calls"):  # raises _Deferred before the page is fetched
            wants_ai = False  # AI switched off for monitoring passes (max_ai_per_run: 0)

        # 3: ad page — full text and all photos
        if (self.config.general.fetch_details and not listing.detail_loaded
                and budget.spend("details_fetched")):
            listing, keep, reasons = await self._load_detail(listing, search, source)
            if not keep:
                budget.summary.prefiltered += 1
                return self._save_skip(listing, search, reasons)

        # 4: comparables, only when neither reference nor history knew the market
        if estimate is None:
            estimate = await self._market_estimate(listing, search, budget, use_history=False)
        best = evaluate(listing, estimate, None, search, pricing)
        if market_says_no_deal(best):  # the AI can't turn this into a deal
            return self._save(best.model_copy(update={"stage": "full"}))

        # 5: local AI
        wants_ai = self._wants_ai(listing, estimate, search) and budget.spend("ai_calls")
        return await self._conclude(listing, search, estimate, source, budget if wants_ai else None,
                                    ask_ai=wants_ai)

    async def _conclude(
        self, listing: Listing, search: SearchConfig, estimate: PriceEstimate, source: Any,
        budget: _Budget | None, *, ask_ai: bool,
    ) -> Evaluation:
        """AI verdict (if asked) → final evaluation → second opinion → store."""
        verdict = None
        if ask_ai:
            verdict, estimate = await self._ask_ai(listing, search, estimate, source, budget)
        evaluation = evaluate(listing, estimate, verdict, search, self.config.pricing, ai_expected=ask_ai)
        evaluation = await self._maybe_second_opinion(listing, search, estimate, evaluation, source)
        return self._save(evaluation.model_copy(update={"stage": "full"}))

    # ------------------------------------------------------------ evaluation
    async def evaluate_listing(self, listing: Listing, search: SearchConfig) -> Evaluation:
        """Full pipeline for one ad — no budgets, no shortcuts (single-ad check);
        stores the listing + evaluation."""
        return await self._evaluate(listing, search, None)

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
        estimate = await self._market_estimate(listing, search, budget)
        ask_ai = self._wants_ai(listing, estimate, search)
        return await self._conclude(listing, search, estimate, source, budget, ask_ai=ask_ai)

    def _save(self, evaluation: Evaluation) -> Evaluation:
        self.db.save_evaluation(evaluation)
        return evaluation

    def _save_skip(self, listing: Listing, search: SearchConfig, reasons: list[str]) -> Evaluation:
        return self._save(Evaluation(
            ad_id=listing.ad_id, purpose=search.purpose, buy_price=listing.price,
            verdict="skip", action="skip", score=0.0, reasons=reasons, stage="prefilter",
        ))

    async def _load_detail(self, listing: Listing, search: SearchConfig, source: Any) -> tuple[Listing, bool, list[str]]:
        """Open the ad page (full text, all photos) and re-run the prefilter on it.
        A failed page is not fatal: the search-card data is used instead."""
        try:
            listing = await source.fetch_detail(listing)
        except BlockedError:
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
        budget: _Budget | None,
    ) -> tuple[AIVerdict, PriceEstimate]:
        assert self._evaluator is not None
        ai_cfg = self.config.ai
        images: list[bytes] = []
        if listing.image_urls and ai_cfg.max_images > 0:
            images = await source.download_images(listing, max_images=ai_cfg.max_images)
        verdict = await self._evaluator.evaluate(
            listing, images, purpose=search.purpose,
            estimate=estimate if estimate.market_price is not None else None,
            target_price=search.target_price,
        )
        # The model often identifies the product better than the raw title does.
        if estimate.market_price is None and verdict.search_query:
            ai_estimate = await self._market_estimate(
                listing, search, budget, query=verdict.search_query, defer=False
            )
            if ai_estimate.market_price is not None:
                estimate = ai_estimate
        return verdict, _cross_check(estimate, verdict)

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
        images: list[bytes] = []
        if listing.image_urls and so.max_images > 0:
            images = await source.download_images(listing, max_images=so.max_images)
        second = await self._second.evaluate(
            listing, images, purpose=search.purpose,
            estimate=estimate if estimate.market_price is not None else None,
            target_price=search.target_price,
        )
        if second.confidence <= 0:  # the call failed -> keep the local result
            return evaluation.model_copy(update={"ai_second": second})
        final = evaluate(listing, estimate, second, search, self.config.pricing)
        return final.model_copy(update={"ai": evaluation.ai or final.ai, "ai_second": second})

    async def evaluate_url(
        self, url: str, *, purpose: str = "resale", target_price: float | None = None
    ) -> DealView:
        """Check a single ad by its link ("should I buy this?")."""
        ad_id = ad_id_from_url(url)
        if not ad_id:
            raise ValueError("Не похоже на ссылку на объявление Kleinanzeigen или eBay")
        self._ensure_components()
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
        instead of deferring when it is used up)."""
        if query is None:
            ref = self._reference_estimate(listing, search)
            if ref is not None:
                return ref
        q = (query or make_search_query(listing.title)).strip()
        if not q:
            return PriceEstimate(notes="Не удалось составить запрос для поиска аналогов")
        history = self._history_estimate(listing, q) if use_history else None
        if history is not None:
            if budget is not None:
                budget.summary.history_hits += 1
            return history
        cached = self._cached_comps(q)
        if cached is not None:
            return cached
        if budget is not None:
            try:
                allowed = budget.spend("comps_lookups")
            except _Deferred:
                if defer:
                    raise
                return PriceEstimate(query=q, notes="Лимит поиска аналогов на эту проверку исчерпан")
            if not allowed:
                return PriceEstimate(query=q, notes="Поиск аналогов выключен (general.max_comps_lookups_per_run: 0)")
        return await self._comps_estimate(listing, q)

    def _reference_estimate(self, listing: Listing, search: SearchConfig) -> PriceEstimate | None:
        if search.reference_price is not None:
            return PriceEstimate(
                market_price=search.reference_price, source="reference",
                notes="Цена задана в настройках поиска",
            )
        ref = find_reference_price(
            f"{listing.title} {listing.description[:300]}", self.config.pricing.reference_prices
        )
        if ref is not None:
            return PriceEstimate(
                market_price=ref, source="reference", notes="Цена из справочника reference_prices"
            )
        return None

    def _history_estimate(self, listing: Listing, query: str) -> PriceEstimate | None:
        """Market price from prices seen before (search results, comparables) — no requests."""
        pricing = self.config.pricing
        if not pricing.use_price_history:
            return None
        days = max(1, pricing.history_days)
        try:
            points = self.db.price_history_dated(
                normalize(query), utcnow() - timedelta(days=days), exclude_ad_id=listing.ad_id
            )
        except sqlite3.Error as exc:
            log.warning("Price history lookup for %r failed: %s", query, exc)
            return None
        return estimate_from_history(
            query, points, asking_price_discount=pricing.asking_price_discount,
            min_points=pricing.history_min_points, days=days,
        )

    def _cached_comps(self, query: str) -> PriceEstimate | None:
        cached = self._comps_cache.get(normalize(query))
        if cached and time.monotonic() - cached[0] < COMPS_CACHE_TTL:
            return cached[1]
        return None

    async def _comps_estimate(self, listing: Listing, query: str) -> PriceEstimate:
        """Look up comparables (eBay sold, eBay API, Kleinanzeigen), remember their prices."""
        pricing = self.config.pricing
        comps: list[Comparable] = []
        if pricing.use_ebay_sold_comps and not self._ebay_blocked and self._ebay is not None:
            try:
                comps += await self._ebay.sold_comparables(query, limit=pricing.comps_limit)
            except BlockedError as exc:
                self._ebay_blocked = True  # eBay being grumpy must not stop Kleinanzeigen
                log.warning("eBay blocked sold-comps lookup: %s", exc)
            except Exception as exc:
                log.warning("eBay sold comps for %r failed: %s", query, exc)
        if self._ebay_api is not None:
            try:
                comps += await self._ebay_api.comparables(
                    query, exclude_ad_id=listing.ad_id, limit=pricing.comps_limit
                )
            except Exception as exc:
                log.warning("eBay API comps for %r failed: %s", query, exc)
        if pricing.use_kleinanzeigen_comps and self._scraper is not None:
            try:
                comps += await self._scraper.comparables(
                    query, exclude_ad_id=listing.ad_id, limit=pricing.comps_limit
                )
            except BlockedError:
                raise
            except Exception as exc:
                log.warning("Kleinanzeigen comps for %r failed: %s", query, exc)

        relevant = relevant_comparables(query, comps)
        if len(relevant) < len(comps):
            log.debug("Comparables for %r: kept %d of %d (dropped PCs, parts, other models...)",
                      query, len(relevant), len(comps))
        if relevant:
            try:
                self.db.add_price_points(normalize(query), relevant)
            except sqlite3.Error as exc:
                log.warning("Price history: can't store comparables of %r: %s", query, exc)
        estimate = estimate_from_comparables(
            relevant, asking_price_discount=pricing.asking_price_discount, query=query
        )
        self._comps_cache[normalize(query)] = (time.monotonic(), estimate)
        return estimate

    # --------------------------------------------------------- notifications
    def _should_notify(self, evaluation: Evaluation) -> bool:
        cfg = self.config.notifications
        if evaluation.no_alert or evaluation.verdict not in cfg.verdicts or evaluation.score < cfg.min_score:
            return False
        deal = self.db.get_deal(evaluation.ad_id)
        if deal is None or deal.status == "ignored":
            return False
        return not self.db.was_notified(evaluation.ad_id)

    async def _notify(self, deals: list[DealView], summary: RunSummary) -> int:
        if not self._notifiers:
            return 0
        deals = sorted(
            deals, key=lambda d: d.evaluation.score if d.evaluation else 0, reverse=True
        )
        delivered: set[str] = set()
        for notifier in self._notifiers:
            try:
                await notifier.send(deals)  # notifiers build an informative subject themselves
            except Exception as exc:
                log.warning("Notifier %s failed: %s", getattr(notifier, "name", notifier), exc)
                summary.errors.append(f"Уведомление ({getattr(notifier, 'name', '?')}): {exc}")
                continue
            for deal in deals:
                self.db.mark_notified(deal.listing.ad_id, notifier.name)
                delivered.add(deal.listing.ad_id)
        return len(delivered)


def summary_buy_share(db: Database, listings: list[Listing]) -> float:
    verdicts = [ev.verdict for l in listings if (ev := db.get_evaluation(l.ad_id)) is not None]
    return sum(v == "buy" for v in verdicts) / len(verdicts) if verdicts else 0.0


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


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _price_filtered(search: SearchConfig) -> bool:
    """Does the search only return a price range? (Its results are not the whole market.)"""
    return (
        (search.min_price or 0) > 0
        or search.max_price is not None
        or bool(search.url and _URL_PRICE_FILTER_RE.search(search.url))
    )


def _is_model_key(key: str) -> bool:
    """pricing.identity's opinion when installed, else: the key has a model number."""
    try:
        from .pricing import identity
    except ImportError:
        identity = None  # type: ignore[assignment]
    check = getattr(identity, "is_model_key", None)
    if check is not None:
        try:
            return bool(check(key))
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
