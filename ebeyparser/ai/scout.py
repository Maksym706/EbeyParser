"""AI scout, stages B-D: from what the model READ to a decision made with REAL prices.

Stage B (`plan()`): the triage item's product / bundle contents are grounded in the ad text
(pricing.ai_key.grounded — a small model's inventions don't count), mapped to price-history
keys (identity.py keys where possible, normalized AI keys otherwise) and priced from our own
history; a bundle / PC / lot is the sum of its priced parts minus a discount
(pricing.bundle). No data = no price: the paid stage may still look up comparables with the
scout's German search phrase, and without any market price the old rule holds (at most
"maybe", no alert).

Stage C is the existing vision check (monitor._ask_ai); `vision_for_plan()` only tells the
hard checks that a PC/bundle is EXPECTED to be one (priced by its parts, not vetoed).

Stage D is the existing evaluate(): fees, profit, ROI and every safety rule unchanged. A deal
that exists only because of the scout gets found_by = "ai_scout" (the «Нашла нейросеть» badge).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import AIVerdict, Listing, PriceEstimate
from ..pricing.ai_key import ProductRef, grounded, resolve, split_quantity
from ..pricing.bundle import BundleValue, Component, value_bundle
from .triage import TriageItem

# triage kinds that are never worth a second look, and risk tags that forbid one
NO_PROMOTE_KINDS = frozenset({"wanted", "swap", "service", "box", "defect", "other", "acc", "part"})
BLOCKING_RISKS = frozenset({"scam", "locked", "fake", "rent", "defect", "missing", "reserved"})
BUNDLE_KINDS = frozenset({"bundle", "pc", "lot"})
UNKNOWN_PRIORITY = 0.75  # same scale as monitor._Candidate.discount (cost / market, lower = better)
DEMOTED_PRIORITY = 0.97  # the scout calls it junk: after everything else

FOUND_BY_SCRIPT = "script"
FOUND_BY_SCOUT = "ai_scout"
FOUND_BY_LABEL_RU = {FOUND_BY_SCOUT: "Нашла нейросеть"}

KIND_LABEL_RU = {"pc": "ПК", "lot": "Лот", "bundle": "Комплект"}

PriceLookup = Callable[[ProductRef], PriceEstimate | None]


@dataclass
class ScoutPlan:
    """The scout's view of one ad, priced from real data."""

    item: TriageItem
    kind: str
    ref: ProductRef | None = None  # single item: what it is (grounded)
    grounded: bool = False
    estimate: PriceEstimate | None = None  # market price from history (single or bundle sum)
    bundle: BundleValue | None = None
    components: list[Component] = field(default_factory=list)
    query: str = ""  # search phrase for comparables when history knows nothing
    blocked: str = ""  # why the scout must not push this ad (kind / risk), Russian
    interest: int = 0

    @property
    def usable(self) -> bool:
        """A real AI answer about a product we can name (or a bundle with named parts)."""
        return self.item.is_ai and not self.blocked and (self.grounded or bool(self.components))

    @property
    def identified(self) -> bool:
        return self.ref is not None and self.grounded

    def priority(self, listing: Listing) -> float:
        """Rank in the paid stage (lower first): cost / market when priced, else by interest."""
        if not self.item.is_ai:
            return UNKNOWN_PRIORITY
        if self.blocked or self.interest <= 2:
            return DEMOTED_PRIORITY
        est = self.estimate
        if listing.is_free:
            return 0.0
        if est is not None and est.market_price and listing.price and listing.price > 1:
            return (listing.price + (listing.shipping_cost or 0.0)) / est.market_price
        return max(0.3, UNKNOWN_PRIORITY - 0.03 * (self.interest - 5))

    def reason_ru(self) -> str:
        reason = " ".join((self.item.reason or "").split())
        if reason:
            return reason[:140]
        if self.kind in BUNDLE_KINDS and self.components:
            return f"{KIND_LABEL_RU.get(self.kind, 'Комплект')}: {', '.join(c.name for c in self.components[:3])}"
        if self.ref is not None:
            return f"Это {self.ref.name}"
        return ""


def ad_text(listing: Listing) -> str:
    return f"{listing.title}\n{listing.description}"


def _blocked(item: TriageItem) -> str:
    if not item.is_ai:
        return "нет ответа нейросети"
    if item.kind in NO_PROMOTE_KINDS:
        return f"вид объявления: {item.kind}"
    risks = BLOCKING_RISKS.intersection(item.risks)
    if risks:
        return "риск: " + ", ".join(sorted(risks))
    return ""


def plan(listing: Listing, item: TriageItem, lookup: PriceLookup, *, bundle_discount: float = 0.15,
         pc_discount: float = 0.25, min_priced_share: float = 0.5) -> ScoutPlan:
    """Stage B for one ad: ground the product / parts in the ad text, price them from history."""
    text = ad_text(listing)
    kind = item.kind if item.kind in BUNDLE_KINDS or item.kind == "single" else item.kind
    result = ScoutPlan(item=item, kind=kind, interest=item.interest, blocked=_blocked(item),
                       query=item.query or "")
    if not item.is_ai:
        return result
    if kind in BUNDLE_KINDS:
        names = list(item.contents)
        if item.product and kind == "bundle":
            names = [item.product, *names]  # "PS5 + 2 Controller": the main product is part of the bundle
        seen: set[str] = set()
        for raw in names:
            qty, name = split_quantity(raw)
            ref = resolve(name)
            key = ref.key if ref else name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            comp = Component(name=name, qty=qty, ref=ref, grounded=grounded(name, text))
            if comp.grounded and ref is not None:
                comp.estimate = lookup(ref)
            result.components.append(comp)
        result.grounded = any(c.grounded for c in result.components)
        discount = pc_discount if kind in ("pc", "lot") else bundle_discount
        value = value_bundle(result.components, discount=discount, min_priced_share=min_priced_share,
                             label=KIND_LABEL_RU.get(kind, "Комплект"))
        result.bundle = value
        result.estimate = value.estimate
        return result
    # a single product
    ref = resolve(item.product) if item.product else None
    result.ref = ref
    result.grounded = ref is not None and grounded(ref.name, text)
    if result.grounded and ref is not None:
        est = lookup(ref)
        if est is not None and est.market_price and item.qty > 1:
            est = est.model_copy(update={
                "market_price": round(est.market_price * item.qty, 2),
                "low": round((est.low or est.market_price) * item.qty, 2),
                "high": round((est.high or est.market_price) * item.qty, 2),
                "notes": f"{item.qty} шт. × {est.market_price:.0f} €. {est.notes}".strip(),
            })
        result.estimate = est
        if not result.query:
            result.query = ref.query
    return result


def worth_a_look(plan_: ScoutPlan, *, deal_math: Callable[[PriceEstimate], bool], min_interest: int) -> bool:
    """Promote an ad the script dismissed? Only a real, unblocked, grounded AI answer; with a
    market price from history the math must show a possible deal; without one, only a high
    interest (single items get a comparables lookup with the scout's phrase; a bundle without
    priced parts needs an even higher interest — the vision check can't price it either)."""
    if not plan_.usable:
        return False
    if plan_.estimate is not None and plan_.estimate.market_price:
        return deal_math(plan_.estimate)
    if plan_.kind in BUNDLE_KINDS:
        return plan_.interest >= min_interest + 2 and plan_.grounded
    return plan_.identified and plan_.interest >= min_interest and bool(plan_.query)


def vision_for_plan(verdict: AIVerdict | None, plan_: ScoutPlan | None) -> AIVerdict | None:
    """A PC / lot / bundle the scout priced by its parts is expected to BE one: the vision
    model's item_type "complete_pc" / "bundle" / "laptop" is then no veto (bundle = several
    products sold together, priced from parts). Everything else in the verdict stays."""
    if verdict is None or plan_ is None or plan_.kind not in BUNDLE_KINDS or plan_.estimate is None:
        return verdict
    if verdict.item_type in ("complete_pc", "bundle", "laptop"):
        return verdict.model_copy(update={"item_type": "bundle"})
    return verdict


def vision_disagrees(verdict: AIVerdict | None, plan_: ScoutPlan | None) -> str:
    """The photo check names a different product than the scout (both identified by identity.py):
    the vision model saw the whole ad and the photos, so the scout's price may be for the wrong
    model. Returns a Russian warning, or ""."""
    if verdict is None or plan_ is None or plan_.ref is None or plan_.ref.identity is None:
        return ""
    if verdict.confidence <= 0 or not verdict.product:
        return ""
    other = resolve(verdict.product)
    if other is None or other.identity is None:
        return ""
    a, b = plan_.ref.identity, other.identity
    if (a.family, a.model, a.variant) != (b.family, b.model, b.variant):
        return f"⚠ Разведчик увидел «{plan_.ref.name}», проверка фото — «{verdict.product}»"
    return ""


def record_rows(listing: Listing, plan_: ScoutPlan) -> list[tuple[str, Listing]]:
    """Price-history rows the scout adds: a plain single item it identified (typo fixed,
    product only in the text, or a product identity.py doesn't know) is remembered under its
    key, so the history grows for what the regexes can't read. Never bundles, risks, defects."""
    item = plan_.item
    if not plan_.identified or plan_.kind != "single" or plan_.blocked or item.qty != 1:
        return []
    if item.condition == "defect" or item.risks:
        return []
    assert plan_.ref is not None
    return [(plan_.ref.key, listing)]


def summary(plan_: ScoutPlan) -> dict[str, Any]:
    """Compact dict for logs / the deal page."""
    return {
        "kind": plan_.kind, "product": plan_.ref.name if plan_.ref else plan_.item.product,
        "key": plan_.ref.key if plan_.ref else None, "grounded": plan_.grounded,
        "interest": plan_.interest, "blocked": plan_.blocked,
        "market": plan_.estimate.market_price if plan_.estimate else None,
        "parts": [{"name": c.name, "qty": c.qty, "grounded": c.grounded,
                   "price": c.estimate.market_price if c.estimate else None} for c in plan_.components],
    }
