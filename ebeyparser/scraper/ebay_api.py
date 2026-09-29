"""eBay via the official Browse API (no HTML scraping, no bans).

Needs an eBay developer app (free): https://developer.ebay.com -> Application Keys.
With App ID (client_id) + Cert ID (client_secret) an application token is minted
and refreshed automatically (client-credentials grant). A pasted token also works
until it expires (~2 h).
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..config import EbayConfig, SearchConfig
from ..models import Comparable, Listing, utcnow

log = logging.getLogger(__name__)

SCOPE = "https://api.ebay.com/oauth/api_scope"
PAGE_SIZE = 50
# Comparables: used items from private sellers only (dealers ask retail prices).
COMPS_SELLER_FILTER = "sellerAccountTypes:{INDIVIDUAL}"


class EbayAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def seller_account_type(item: dict[str, Any]) -> str:
    """"BUSINESS" / "INDIVIDUAL" / "" — the Browse API puts it under `seller` (older
    responses and our fixtures also have it at the top level)."""
    value = item.get("sellerAccountType") or (item.get("seller") or {}).get("sellerAccountType") or ""
    return str(value).upper()


def _api_base(cfg: EbayConfig) -> str:
    return "https://api.sandbox.ebay.com" if cfg.sandbox else "https://api.ebay.com"


def _money(obj: dict[str, Any] | None) -> float | None:
    if not obj or obj.get("value") in (None, ""):
        return None
    try:
        return float(obj["value"])
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ebay_ad_id(item: dict[str, Any]) -> str:
    legacy = item.get("legacyItemId")
    if not legacy:
        parts = str(item.get("itemId", "")).split("|")
        legacy = parts[1] if len(parts) > 1 else item.get("itemId", "")
    return f"ebay-{legacy}"


def large_image_url(url: str, size: int = 800) -> str:
    """i.ebayimg.com/.../s-l225.jpg -> s-l800.jpg"""
    return re.sub(r"s-l\d+\.", f"s-l{size}.", url)


def html_to_text(raw: str, limit: int = 6000) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def build_filters(search: SearchConfig, cfg: EbayConfig, now: datetime | None = None) -> str:
    parts: list[str] = []
    if search.min_price is not None or search.max_price is not None:
        low = f"{search.min_price:g}" if search.min_price is not None else ""
        high = f"{search.max_price:g}" if search.max_price is not None else ""
        parts += [f"price:[{low}..{high}]", "priceCurrency:EUR"]
    if search.buying_options:
        parts.append("buyingOptions:{" + "|".join(search.buying_options) + "}")
    if search.ebay_conditions:
        parts.append("conditions:{" + "|".join(search.ebay_conditions) + "}")
    if cfg.item_location_country:
        parts.append(f"itemLocationCountry:{cfg.item_location_country}")
    if search.ending_within_hours:
        end = (now or utcnow()) + timedelta(hours=search.ending_within_hours)
        parts.append(f"itemEndDate:[..{_iso(end)}]")
    if search.local_pickup_only and search.location:
        parts += [
            "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
            f"pickupCountry:{cfg.item_location_country or 'DE'}",
            f"pickupPostalCode:{search.location}",
            f"pickupRadius:{search.radius_km or 25}",
            "pickupRadiusUnit:km",
        ]
    return ",".join(parts)


def item_to_listing(item: dict[str, Any], search_name: str = "") -> Listing:
    options = list(item.get("buyingOptions") or [])
    is_auction = "AUCTION" in options and "FIXED_PRICE" not in options
    price = _money(item.get("currentBidPrice")) if is_auction else None
    if price is None:
        price = _money(item.get("price"))
    images = []
    if (item.get("image") or {}).get("imageUrl"):
        images.append(item["image"]["imageUrl"])
    images += [i["imageUrl"] for i in item.get("additionalImages") or [] if i.get("imageUrl")]

    shipping = None
    for opt in item.get("shippingOptions") or []:
        cost = _money(opt.get("shippingCost"))
        if cost is not None:
            shipping = cost if shipping is None else min(shipping, cost)
    loc = item.get("itemLocation") or {}
    seller = item.get("seller") or {}
    feedback = seller.get("feedbackPercentage")
    tags = []
    if "BEST_OFFER" in options:
        tags.append("Preisvorschlag")
    if is_auction:
        tags.append("Auktion")
    if shipping == 0:
        tags.append("Kostenloser Versand")

    price_text = f"{price:.2f} €".replace(".", ",") if price is not None else ""
    if is_auction and price is not None:
        price_text += f" ({item.get('bidCount', 0)} Gebote)"

    return Listing(
        ad_id=ebay_ad_id(item),
        source="ebay",
        url=item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or "",
        title=item.get("title", "").strip(),
        price=price,
        price_text=price_text,
        negotiable="BEST_OFFER" in options,
        location=" ".join(x for x in (loc.get("postalCode"), loc.get("city")) if x),
        postal_code=loc.get("postalCode"),
        description=item.get("shortDescription", "") or "",
        image_urls=images,
        shipping_possible=bool(item.get("shippingOptions")) or None,
        tags=tags,
        condition=item.get("condition", "") or "",
        buying_options=options,
        bid_count=item.get("bidCount"),
        ends_at=_parse_dt(item.get("itemEndDate")),
        shipping_cost=shipping,
        # no eBay usernames are stored: keeps the "I do not persist eBay user data" exemption honest
        seller_name="",
        seller_type="commercial" if seller_account_type(item) == "BUSINESS"
        else "private" if seller_account_type(item) == "INDIVIDUAL" else "unknown",
        seller_feedback_percent=float(feedback) if feedback not in (None, "") else None,
        seller_feedback_score=seller.get("feedbackScore"),
        posted_at_text=item.get("itemCreationDate", "") or "",
        search_name=search_name,
    )


class EbayBrowseClient:
    """Same public surface as KleinanzeigenScraper, so the monitor can treat both alike."""

    def __init__(self, cfg: EbayConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 25.0):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=True,
            headers={"Accept-Language": "de-DE", "Accept": "application/json"},
        )
        self._token: str | None = cfg.oauth_token or None
        self._token_expires = float("inf") if cfg.oauth_token and not cfg.client_id else 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- auth
    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expires:
                return self._token
            if not (self.cfg.client_id and self.cfg.client_secret):
                if self._token and not force:
                    return self._token
                raise EbayAPIError(
                    "eBay: токен истёк или не задан. Укажи ebay.client_id и ebay.client_secret "
                    "(App ID и Cert ID с developer.ebay.com) — тогда токен будет обновляться сам."
                )
            basic = base64.b64encode(f"{self.cfg.client_id}:{self.cfg.client_secret}".encode()).decode()
            resp = await self._http.post(
                f"{_api_base(self.cfg)}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
            if resp.status_code != 200:
                raise EbayAPIError(f"eBay OAuth: {resp.status_code} {resp.text[:200]}")
            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expires = time.monotonic() + int(payload.get("expires_in", 7200)) - 120
            return self._token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{_api_base(self.cfg)}{path}"
        for attempt in range(3):
            token = await self._get_token(force=attempt > 0 and bool(self.cfg.client_id))
            resp = await self._http.get(
                url, params=params,
                headers={"Authorization": f"Bearer {token}",
                         "X-EBAY-C-MARKETPLACE-ID": self.cfg.marketplace_id},
            )
            if resp.status_code == 401 and self.cfg.client_id and attempt == 0:
                continue  # token revoked/expired early -> refresh once
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                raise EbayAPIError("eBay: токен недействителен или истёк (401).")
            if resp.status_code >= 400:
                raise EbayAPIError(f"eBay API {resp.status_code}: {resp.text[:300]}", resp.status_code)
            return resp.json()
        raise EbayAPIError(f"eBay API не отвечает ({resp.status_code})")

    # ------------------------------------------------------------ searching
    async def search(
        self,
        search: SearchConfig,
        max_pages: int = 1,
        seen: Callable[[str], bool] | None = None,
    ) -> list[Listing]:
        """Same contract as KleinanzeigenScraper.search: with `seen(ad_id)`, stop after the
        first page that contains a known item (newest-first searches only; ending-soon
        auction searches ignore it)."""
        params: dict[str, Any] = {
            "q": search.query,
            "limit": PAGE_SIZE,
            "sort": "endingSoonest" if search.ending_within_hours else "newlyListed",
        }
        if search.ebay_category_ids:
            params["category_ids"] = ",".join(search.ebay_category_ids)
        filters = build_filters(search, self.cfg)
        if filters:
            params["filter"] = filters
        if not search.query and not search.ebay_category_ids:
            raise EbayAPIError(f"Поиск «{search.name}»: для eBay нужен query или ebay_category_ids")

        found: dict[str, Listing] = {}
        for page in range(max(1, max_pages)):
            params["offset"] = page * PAGE_SIZE
            data = await self._get("/buy/browse/v1/item_summary/search", params)
            items = data.get("itemSummaries") or []
            page_ids: list[str] = []
            for item in items:
                listing = item_to_listing(item, search.name)
                found.setdefault(listing.ad_id, listing)
                page_ids.append(listing.ad_id)
            if len(items) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= int(data.get("total", 0)):
                break
            if seen is not None and not search.ending_within_hours and any(seen(i) for i in page_ids):
                break
        return list(found.values())

    async def fetch_detail(self, listing: Listing) -> Listing:
        legacy = listing.ad_id.removeprefix("ebay-")
        data = await self._get(
            "/buy/browse/v1/item/get_item_by_legacy_id", {"legacy_item_id": legacy}
        )
        full = item_to_listing(data, listing.search_name)
        attributes = {a["name"]: a.get("value", "") for a in data.get("localizedAspects") or [] if a.get("name")}
        description = html_to_text(data.get("description", "")) or data.get("shortDescription", "")
        if data.get("conditionDescription"):
            description = f"Zustand: {data['conditionDescription']}\n{description}"
        return listing.model_copy(update={
            "title": full.title or listing.title,
            "price": full.price if full.price is not None else listing.price,
            "price_text": full.price_text or listing.price_text,
            "description": description or listing.description,
            "image_urls": full.image_urls or listing.image_urls,
            "attributes": {**listing.attributes, **attributes},
            "condition": full.condition or listing.condition,
            "shipping_cost": full.shipping_cost if full.shipping_cost is not None else listing.shipping_cost,
            "bid_count": full.bid_count if full.bid_count is not None else listing.bid_count,
            "ends_at": full.ends_at or listing.ends_at,
            "seller_type": full.seller_type if full.seller_type != "unknown" else listing.seller_type,
            "detail_loaded": True,
        })

    async def comparables(self, query: str, *, exclude_ad_id: str | None = None, limit: int = 30,
                          min_price: float | None = None, max_price: float | None = None) -> list[Comparable]:
        """Current fixed-price offers of USED items from private sellers on eBay.de (asking
        prices, not sales). Business sellers are excluded: shop prices are not what a
        private reseller gets."""
        search = SearchConfig(name="comps", source="ebay", query=query, buying_options=["FIXED_PRICE"],
                              ebay_conditions=["USED"], min_price=min_price, max_price=max_price)
        base_filter = build_filters(search, self.cfg)
        params = {"q": query, "limit": min(limit * 2, 100), "filter": f"{base_filter},{COMPS_SELLER_FILTER}"}
        try:
            data = await self._get("/buy/browse/v1/item_summary/search", params)
        except EbayAPIError as exc:
            if exc.status_code != 400:
                raise
            # marketplace without the seller-type filter: filter the results ourselves
            log.info("eBay rejected %s (%s), filtering business sellers locally", COMPS_SELLER_FILTER, exc)
            data = await self._get("/buy/browse/v1/item_summary/search", {**params, "filter": base_filter})
        comps: list[Comparable] = []
        for item in data.get("itemSummaries") or []:
            listing = item_to_listing(item)
            if listing.ad_id == exclude_ad_id or not listing.price:
                continue
            if seller_account_type(item) == "BUSINESS":
                continue
            if "defekt" in listing.condition.lower() or "ersatzteil" in listing.condition.lower():
                continue
            comps.append(Comparable(title=listing.title, price=listing.price, url=listing.url,
                                    source="ebay", sold=False))
            if len(comps) >= limit:
                break
        return comps

    async def rate_limits(self, api_context: str | None = "buy") -> list[dict[str, Any]]:
        """Exact call quotas of *your* key (eBay Developer Analytics API).
        Tries the application limits first, then the user-token limits."""
        params = {"api_context": api_context} if api_context else None
        try:
            data = await self._get("/developer/analytics/v1_beta/rate_limit/", params)
        except EbayAPIError as app_error:
            try:
                data = await self._get("/developer/analytics/v1_beta/user_rate_limit/", params)
            except EbayAPIError:
                raise app_error
        rows: list[dict[str, Any]] = []
        for api in data.get("rateLimits") or []:
            for resource in api.get("resources") or []:
                for rate in resource.get("rates") or []:
                    rows.append({
                        "api": f"{api.get('apiContext', '')}/{api.get('apiName', '')} {api.get('apiVersion', '')}".strip(),
                        "resource": resource.get("name", ""),
                        "count": rate.get("count"),
                        "limit": rate.get("limit"),
                        "remaining": rate.get("remaining"),
                        "reset": _parse_dt(rate.get("reset")),
                        "window_seconds": rate.get("timeWindow"),
                    })
        return rows

    async def download_images(self, listing: Listing, max_images: int = 3) -> list[bytes]:
        images: list[bytes] = []
        for url in listing.image_urls[:max_images]:
            try:
                resp = await self._http.get(large_image_url(url))
                resp.raise_for_status()
                images.append(resp.content)
            except httpx.HTTPError as exc:
                log.debug("eBay image %s failed: %s", url, exc)
        return images


def item_url(legacy_id: str) -> str:
    return f"https://www.ebay.de/itm/{quote(legacy_id)}"
