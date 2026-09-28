"""The monitoring pipeline.

For every enabled search:
  scrape results -> store -> for new (or price-dropped) ads:
  prefilter -> fetch ad page -> estimate market price (reference / eBay sold /
  Kleinanzeigen comparables) -> local vision LLM verdict -> score -> notify.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import timedelta
from typing import Any

import httpx

from .ai.claude import make_llm
from .ai.evaluator import AIEvaluator
from .config import AppConfig, SearchConfig
from .db import Database
from .models import DealView, Evaluation, Listing, PriceEstimate, RunSummary, utcnow
from .notify.base import build_notifiers
from .pricing.estimator import (
    estimate_from_comparables,
    evaluate,
    find_reference_price,
    prefilter,
    prefilter_score,
)
from .pricing.text import make_search_query, normalize
from .scraper.ebay_api import EbayAPIError, EbayBrowseClient
from .scraper.ebay_sold import EbaySoldScraper
from .scraper.http import BlockedError, PoliteClient
from .scraper.kleinanzeigen import KleinanzeigenScraper, PageLayoutError

log = logging.getLogger(__name__)

COMPS_CACHE_TTL = 6 * 3600  # seconds
PRICE_DROP_RATIO = 0.9  # re-evaluate a known ad when its price falls by 10 %+


class AlreadyRunningError(RuntimeError):
    pass


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
                if not deals:
                    continue
                if self.config.notifications.mode == "instant":
                    summary.notified += await self._notify(deals, summary)
                else:
                    digest.extend(deals)
            if digest:
                summary.notified += await self._notify(digest, summary)
        finally:
            summary.finished_at = utcnow()
            self.db.finish_run(summary)
            self.last_summary = summary
            self._running = False
        log.info(
            "Run finished: %d new, %d evaluated, %d deals, %d notified, %d errors",
            summary.new_listings, summary.evaluated, summary.deals_found,
            summary.notified, len(summary.errors),
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
        max_pages = search.max_pages or self.config.general.max_pages
        listings = await self._source_for(search).search(search, max_pages=max_pages)
        summary.listings_seen += len(listings)

        todo: list[Listing] = []
        for listing in listings:
            listing.search_name = search.name
            previous = self.db.get_listing(listing.ad_id)
            self.db.upsert_listing(listing)
            if previous is None:
                todo.append(listing)
                summary.new_listings += 1
            elif _price_dropped(previous, listing):
                log.info("Price drop on %s: %s -> %s", listing.ad_id, previous.price, listing.price)
                todo.append(self.db.get_listing(listing.ad_id) or listing)
        todo = todo[: self.config.general.max_new_per_search]

        to_notify: list[DealView] = []
        for listing in todo:
            try:
                evaluation = await self.evaluate_listing(listing, search)
            except BlockedError:
                raise
            except Exception as exc:
                log.exception("Evaluating %s failed", listing.ad_id)
                summary.errors.append(f"{search.name} / {listing.title[:40]}: {exc}")
                continue
            summary.evaluated += 1
            if evaluation.verdict == "buy":
                summary.deals_found += 1
            if self._should_notify(evaluation):
                deal = self.db.get_deal(listing.ad_id)
                if deal is not None:
                    to_notify.append(deal)
        return to_notify

    # ------------------------------------------------------------ evaluation
    async def evaluate_listing(self, listing: Listing, search: SearchConfig) -> Evaluation:
        """Full pipeline for one ad; stores the listing + evaluation."""
        self._ensure_components()
        source = self._source_for(listing)
        keep, reasons = prefilter(listing, search)
        if keep and self.config.general.fetch_details and not listing.detail_loaded:
            try:
                listing = await source.fetch_detail(listing)
            except BlockedError:
                raise
            except Exception as exc:  # evaluate from the search-card data instead
                log.warning("Detail page of %s failed: %s", listing.ad_id, exc)
            else:
                listing.search_name = listing.search_name or search.name
                self.db.upsert_listing(listing)
                keep, reasons = prefilter(listing, search)  # full text may reveal more
        if not keep:
            evaluation = Evaluation(
                ad_id=listing.ad_id, purpose=search.purpose, buy_price=listing.price,
                verdict="skip", score=0.0, reasons=reasons,
            )
            self.db.save_evaluation(evaluation)
            return evaluation

        estimate = await self._estimate(listing, search)
        verdict = None
        ai_cfg = self.config.ai
        if self._evaluator is not None and (
            ai_cfg.run_for == "all"
            or prefilter_score(listing, estimate, search, self.config.pricing) >= ai_cfg.min_prefilter_score
        ):
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
                ai_estimate = await self._estimate(listing, search, query=verdict.search_query)
                if ai_estimate.market_price is not None:
                    estimate = ai_estimate

        evaluation = evaluate(listing, estimate, verdict, search, self.config.pricing)
        evaluation = await self._maybe_second_opinion(listing, search, estimate, evaluation, source)
        self.db.save_evaluation(evaluation)
        return evaluation

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

    async def _estimate(
        self, listing: Listing, search: SearchConfig, *, query: str | None = None
    ) -> PriceEstimate:
        pricing = self.config.pricing
        if search.reference_price is not None:
            return PriceEstimate(
                market_price=search.reference_price, source="reference",
                notes="Цена задана в настройках поиска",
            )
        ref = find_reference_price(f"{listing.title} {listing.description[:300]}", pricing.reference_prices)
        if ref is not None:
            return PriceEstimate(
                market_price=ref, source="reference", notes="Цена из справочника reference_prices"
            )

        query = (query or make_search_query(listing.title)).strip()
        if not query:
            return PriceEstimate(notes="Не удалось составить запрос для поиска аналогов")
        key = normalize(query)
        cached = self._comps_cache.get(key)
        if cached and time.monotonic() - cached[0] < COMPS_CACHE_TTL:
            return cached[1]

        comps = []
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

        estimate = estimate_from_comparables(
            comps, asking_price_discount=pricing.asking_price_discount, query=query
        )
        self._comps_cache[key] = (time.monotonic(), estimate)
        return estimate

    # --------------------------------------------------------- notifications
    def _should_notify(self, evaluation: Evaluation) -> bool:
        cfg = self.config.notifications
        if evaluation.verdict not in cfg.verdicts or evaluation.score < cfg.min_score:
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
