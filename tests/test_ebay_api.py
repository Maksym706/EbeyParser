from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ebeyparser.config import EbayConfig, SearchConfig
from ebeyparser.models import Listing
from ebeyparser.scraper.ebay_api import (
    EbayAPIError,
    EbayBrowseClient,
    build_filters,
    html_to_text,
    item_to_listing,
    large_image_url,
)

AUCTION_ITEM = {
    "itemId": "v1|123456789012|0",
    "legacyItemId": "123456789012",
    "title": "Gigabyte RTX 3080 Gaming OC 10GB",
    "price": {"value": "180.00", "currency": "EUR"},
    "currentBidPrice": {"value": "151.00", "currency": "EUR"},
    "bidCount": 7,
    "buyingOptions": ["AUCTION"],
    "itemEndDate": "2026-09-28T18:30:00.000Z",
    "itemWebUrl": "https://www.ebay.de/itm/123456789012",
    "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l225.jpg"},
    "additionalImages": [{"imageUrl": "https://i.ebayimg.com/images/g/def/s-l225.jpg"}],
    "itemLocation": {"postalCode": "10*** ", "country": "DE", "city": "Berlin"},
    "condition": "Gebraucht",
    "seller": {"username": "hans_91", "feedbackPercentage": "99.2", "feedbackScore": 311},
    "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "6.99", "currency": "EUR"}}],
    "sellerAccountType": "INDIVIDUAL",
}
BIN_ITEM = {
    "itemId": "v1|222222222222|0",
    "title": "Lenovo ThinkPad T480 i5 16GB",
    "price": {"value": "199.00", "currency": "EUR"},
    "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
    "itemWebUrl": "https://www.ebay.de/itm/222222222222",
    "condition": "Gebraucht",
    "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "EUR"}}],
}


def test_item_to_listing_auction_uses_current_bid():
    listing = item_to_listing(AUCTION_ITEM, "gpu")
    assert listing.ad_id == "ebay-123456789012"
    assert listing.source == "ebay"
    assert listing.price == 151.0
    assert listing.bid_count == 7
    assert listing.ends_at == datetime(2026, 9, 28, 18, 30, tzinfo=timezone.utc)
    assert listing.shipping_cost == 6.99
    assert listing.seller_feedback_percent == 99.2
    assert listing.seller_type == "private"
    assert len(listing.image_urls) == 2
    assert "Auktion" in listing.tags
    assert listing.search_name == "gpu"


def test_item_to_listing_buy_it_now_with_best_offer():
    listing = item_to_listing(BIN_ITEM)
    assert listing.ad_id == "ebay-222222222222"  # parsed from itemId when legacyItemId missing
    assert listing.price == 199.0
    assert listing.negotiable is True
    assert listing.shipping_cost == 0.0
    assert "Kostenloser Versand" in listing.tags


def test_build_filters():
    search = SearchConfig(
        name="x", source="ebay", query="rtx", min_price=50, max_price=400,
        buying_options=["AUCTION"], ebay_conditions=["USED"], ending_within_hours=3,
        local_pickup_only=True, location="10115", radius_km=40,
    )
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    f = build_filters(search, EbayConfig(), now)
    assert "price:[50..400]" in f and "priceCurrency:EUR" in f
    assert "buyingOptions:{AUCTION}" in f
    assert "conditions:{USED}" in f
    assert "itemLocationCountry:DE" in f
    assert "itemEndDate:[..2026-09-27T15:00:00Z]" in f
    assert "pickupPostalCode:10115" in f and "pickupRadius:40" in f
    assert build_filters(SearchConfig(name="y", max_price=100), EbayConfig()).startswith("price:[..100]")


def test_helpers():
    assert large_image_url("https://i.ebayimg.com/images/g/x/s-l225.jpg") == "https://i.ebayimg.com/images/g/x/s-l800.jpg"
    assert html_to_text("<p>Hallo<br>Welt</p><script>x()</script><b>&amp;</b>") == "Hallo\nWelt\n &"


def _client(handler, **cfg) -> EbayBrowseClient:
    return EbayBrowseClient(EbayConfig(**cfg), transport=httpx.MockTransport(handler))


async def test_search_mints_token_and_paginates():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth2/token"):
            assert request.headers["Authorization"].startswith("Basic ")
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 7200})
        assert request.headers["Authorization"] == "Bearer TOKEN"
        assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
        qs = parse_qs(urlparse(str(request.url)).query)
        offset = int(qs["offset"][0])
        items = [dict(BIN_ITEM, itemId=f"v1|{900000000000 + offset + i}|0") for i in range(50 if offset == 0 else 3)]
        return httpx.Response(200, json={"total": 53, "itemSummaries": items})

    client = _client(handler, client_id="id", client_secret="secret")
    listings = await client.search(SearchConfig(name="t", source="ebay", query="thinkpad"), max_pages=3)
    await client.aclose()
    assert len(listings) == 53
    token_calls = [c for c in calls if c.url.path.endswith("/oauth2/token")]
    assert len(token_calls) == 1  # token cached between pages
    qs = parse_qs(urlparse(str(calls[1].url)).query)
    assert qs["q"] == ["thinkpad"] and qs["sort"] == ["newlyListed"]


async def test_static_token_and_401_message():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer v^1.1#static"
        return httpx.Response(401, json={"errors": [{"message": "expired"}]})

    client = _client(handler, oauth_token="v^1.1#static")
    with pytest.raises(EbayAPIError, match="истёк"):
        await client.search(SearchConfig(name="t", source="ebay", query="x"))
    await client.aclose()


async def test_missing_credentials():
    client = _client(lambda r: httpx.Response(500))
    with pytest.raises(EbayAPIError, match="client_id"):
        await client.search(SearchConfig(name="t", source="ebay", query="x"))
    await client.aclose()


async def test_fetch_detail_and_comparables_and_images():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        if request.url.path.endswith("get_item_by_legacy_id"):
            assert request.url.params["legacy_item_id"] == "123456789012"
            return httpx.Response(200, json={
                **AUCTION_ITEM,
                "description": "<div>Karte läuft einwandfrei.<br>Mit OVP.</div>",
                "conditionDescription": "Leichte Kratzer",
                "localizedAspects": [{"name": "Speichergröße", "value": "10 GB"}],
            })
        if request.url.path.endswith("item_summary/search"):
            assert "buyingOptions:{FIXED_PRICE}" in request.url.params["filter"]
            return httpx.Response(200, json={"itemSummaries": [
                AUCTION_ITEM,  # excluded id
                dict(BIN_ITEM, price={"value": "420.00"}),
                dict(BIN_ITEM, itemId="v1|3|0", condition="Als Ersatzteil / defekt", price={"value": "90"}),
            ]})
        if request.url.host == "i.ebayimg.com":
            assert "s-l800" in request.url.path
            return httpx.Response(200, content=b"\xff\xd8\xffIMG")
        return httpx.Response(404)

    client = _client(handler, client_id="id", client_secret="s")
    base = item_to_listing(AUCTION_ITEM, "gpu")
    full = await client.fetch_detail(base)
    assert full.detail_loaded and full.search_name == "gpu"
    assert full.description.startswith("Zustand: Leichte Kratzer\nKarte läuft einwandfrei.")
    assert full.attributes["Speichergröße"] == "10 GB"
    comps = await client.comparables("rtx 3080", exclude_ad_id=base.ad_id)
    assert [c.price for c in comps] == [420.0] and comps[0].source == "ebay"
    images = await client.download_images(full, max_images=2)
    assert images == [b"\xff\xd8\xffIMG"] * 2
    await client.aclose()


def test_listing_roundtrip_json():
    listing = item_to_listing(AUCTION_ITEM)
    again = Listing.model_validate_json(listing.model_dump_json())
    assert again == listing
    assert json.loads(listing.model_dump_json())["source"] == "ebay"
