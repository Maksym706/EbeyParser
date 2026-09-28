"""Shared data models used by every part of the app.

All money values are in EUR. All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

Purpose = Literal["resale", "personal"]
Source = Literal["kleinanzeigen", "ebay"]
Verdict = Literal["buy", "maybe", "skip"]
DealStatus = Literal["new", "starred", "contacted", "bought", "ignored"]
DEAL_STATUSES: tuple[str, ...] = ("new", "starred", "contacted", "bought", "ignored")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Listing(BaseModel):
    """One ad (Kleinanzeigen or eBay). Filled from search results, enriched from the ad page."""

    ad_id: str  # Kleinanzeigen: numeric id; eBay: "ebay-<legacy item id>"
    source: Source = "kleinanzeigen"
    url: str
    title: str
    price: float | None = None  # None = no number given (e.g. only "VB")
    price_text: str = ""  # raw text, e.g. "150 € VB"
    negotiable: bool = False  # "VB" = Verhandlungsbasis
    is_free: bool = False  # "Zu verschenken"
    location: str = ""  # e.g. "10115 Mitte"
    postal_code: str | None = None
    distance_km: float | None = None
    posted_at_text: str = ""  # raw, e.g. "Heute, 12:34"
    description: str = ""  # snippet from search page, full text after detail fetch
    image_urls: list[str] = Field(default_factory=list)
    shipping_possible: bool | None = None
    tags: list[str] = Field(default_factory=list)  # e.g. ["Versand möglich", "Direkt kaufen"]
    attributes: dict[str, str] = Field(default_factory=dict)  # e.g. {"Zustand": "Gut"}
    seller_name: str = ""
    seller_type: Literal["private", "commercial", "unknown"] = "unknown"
    is_top_ad: bool = False
    # mostly eBay:
    condition: str = ""  # e.g. "Gebraucht", "Neu", "Als Ersatzteil / defekt"
    buying_options: list[str] = Field(default_factory=list)  # FIXED_PRICE / AUCTION / BEST_OFFER
    bid_count: int | None = None
    ends_at: datetime | None = None  # auction end
    shipping_cost: float | None = None  # what the buyer pays for delivery, None = unknown / pickup
    seller_feedback_percent: float | None = None
    seller_feedback_score: int | None = None
    search_name: str = ""  # which configured search found this ad
    detail_loaded: bool = False
    first_seen: datetime = Field(default_factory=utcnow)


class Comparable(BaseModel):
    """A similar item used to estimate market value."""

    title: str
    price: float
    url: str = ""
    source: Literal["kleinanzeigen", "ebay", "ebay_sold", "reference", "other"] = "other"
    sold: bool = False  # True = real sale price (eBay sold), False = asking price
    date_text: str = ""


class PriceEstimate(BaseModel):
    """What the item is realistically worth when resold."""

    market_price: float | None = None  # conservative typical resale price
    low: float | None = None
    high: float | None = None
    sample_size: int = 0
    # history = our own database of prices seen for this product (no extra requests)
    source: Literal["reference", "history", "kleinanzeigen", "ebay_sold", "mixed", "ai", "none"] = "none"
    query: str = ""  # query used to find comparables
    comparables: list[Comparable] = Field(default_factory=list)
    notes: str = ""
    history_days: int | None = None  # source "history": the look-back window
    age_days: float | None = None  # source "history": weighted mean age of the prices used


class AIVerdict(BaseModel):
    """Opinion of the (local) vision LLM about one listing."""

    product: str = ""  # identified product, e.g. "NVIDIA GeForce RTX 3090 24GB"
    search_query: str = ""  # short query to find comparable offers
    photo_matches_description: bool | None = None
    condition: Literal["new", "like_new", "good", "used", "defective", "unclear"] = "unclear"
    red_flags: list[str] = Field(default_factory=list)
    estimated_market_price: float | None = None
    verdict: Verdict = "maybe"
    confidence: float = 0.0  # 0..1
    reasoning: str = ""  # short explanation in Russian
    model: str = ""
    # structured extraction (v0.2): what the photos/text show, used for hard vetoes in code
    item_type: Literal[
        "single", "bundle", "complete_pc", "laptop", "part", "accessory", "box_only", "wanted", "unclear"
    ] = "unclear"
    variant: dict[str, str] = Field(default_factory=dict)  # e.g. {"model": "iPhone 13", "storage_gb": "128"}
    defects: list[str] = Field(default_factory=list)
    locked: bool | None = None  # iCloud / activation / account lock visible or mentioned
    stock_photos: bool | None = None  # photos look like catalogue/internet images


class Evaluation(BaseModel):
    """Final decision for one listing: how good the deal is."""

    ad_id: str
    purpose: Purpose = "resale"
    buy_price: float | None = None
    estimate: PriceEstimate = Field(default_factory=PriceEstimate)
    ai: AIVerdict | None = None  # local model
    ai_second: AIVerdict | None = None  # optional second opinion (e.g. Claude)
    max_buy_price: float | None = None  # highest price/bid that still meets your profit targets
    # what to do: buy now / haggle (VB, offer_price) / bid (auction, up to max_buy_price) / watch
    action: Literal["buy", "haggle", "bid", "watch", "skip", ""] = ""
    offer_price: float | None = None  # suggested offer for VB / Preisvorschlag
    ai_checked: bool | None = None  # False = AI enabled but unavailable -> photos NOT checked
    no_alert: bool = False  # never notify (reserved, market known only from the AI)
    # how far the funnel went: "prefilter" (free checks), "market" (no deal by market data,
    # no ad page / AI; re-checked when seen again), "full"; "" = evaluated by an older version
    stage: Literal["", "prefilter", "market", "full"] = ""
    fees: float = 0.0  # selling fees when reselling
    shipping_cost: float = 0.0
    expected_profit: float | None = None  # resale: net profit; personal: savings vs market
    roi: float | None = None  # expected_profit / buy_price
    score: float = 0.0  # 0..100
    verdict: Verdict = "skip"
    reasons: list[str] = Field(default_factory=list)  # human readable, Russian
    red_flags: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=utcnow)


class DealView(BaseModel):
    """Listing + evaluation + user state, as shown in the UI / notifications."""

    listing: Listing
    evaluation: Evaluation | None = None
    status: DealStatus = "new"
    note: str = ""
    notified: bool = False


class RunSummary(BaseModel):
    """Result of one monitoring pass over all searches."""

    id: int | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    searches: int = 0
    listings_seen: int = 0
    new_listings: int = 0
    evaluated: int = 0
    deals_found: int = 0  # verdict == "buy"
    notified: int = 0
    errors: list[str] = Field(default_factory=list)
    # funnel (v0.2): how much work the pass did and what it saved
    prefiltered: int = 0  # dropped by free checks (keywords, wanted ad, below min price)
    early_skips: int = 0  # market known without requests and no deal -> no ad page / AI
    history_hits: int = 0  # market price taken from our own price history
    comps_lookups: int = 0  # comparables searches that went to the network (cache hits are free)
    details_fetched: int = 0  # ad pages opened
    ai_calls: int = 0  # local AI evaluations
    deferred: int = 0  # left for the next pass because a budget was used up
