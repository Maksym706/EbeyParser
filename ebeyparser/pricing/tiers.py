"""Alert tiers of an evaluated deal (notifications.super_deals).

super      «🔥 Супер-находка»: "buy" with a big profit AND ROI at the asking price, a high score,
           a market price from real data, photos checked by the AI and not a single warning.
           Sent at once, past the hourly cap and the digest mode.
deal       any other "buy" (the usual rules).
unchecked  would be a deal, but the photos were not checked (the vision model is offline).
maybe      worth a look.
skip       no deal.
"""

from __future__ import annotations

from typing import Any

from ..models import Evaluation

TIER_SUPER = "super"
TIER_DEAL = "deal"
TIER_UNCHECKED = "unchecked"
TIER_MAYBE = "maybe"
TIER_SKIP = "skip"
TIER_LABELS_RU = {
    TIER_SUPER: "🔥 Супер-находка",
    TIER_DEAL: "Выгодно",
    TIER_UNCHECKED: "Выгодно, фото не проверены",
    TIER_MAYBE: "Присмотреться",
    TIER_SKIP: "Не брать",
}
_REAL_MARKET = frozenset({"ebay_sold", "mixed", "kleinanzeigen", "history", "reference"})


def is_super(ev: Evaluation | None, cfg: Any) -> bool:
    """`cfg`: config.SuperDealsConfig (None / disabled = never)."""
    if ev is None or cfg is None or not getattr(cfg, "enabled", False):
        return False
    return (
        ev.verdict == "buy"
        and ev.action in ("buy", "haggle")
        and ev.ai_checked is True
        and not ev.no_alert
        and not ev.red_flags
        and ev.estimate.source in _REAL_MARKET
        and ev.expected_profit is not None and ev.expected_profit >= cfg.min_profit
        and ev.roi is not None and ev.roi >= cfg.min_roi
        and ev.score >= cfg.min_score
    )


def deal_tier(ev: Evaluation | None, cfg: Any = None) -> str:
    """super | deal | unchecked | maybe | skip ("" without an evaluation)."""
    if ev is None:
        return ""
    if ev.verdict == "buy":
        return TIER_SUPER if is_super(ev, cfg) else TIER_DEAL
    if ev.would_buy:
        return TIER_UNCHECKED
    if ev.verdict == "maybe":
        return TIER_MAYBE
    return TIER_SKIP
