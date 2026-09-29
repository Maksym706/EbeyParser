"""Value of a bundle / PC / lot from its parts (AI scout, stage B).

The scout lists what is inside ("RTX 3070", "Ryzen 5 3600", "2x DualSense Controller"); each
part is priced from REAL data (our price history, comparables) — never from the model. The
bundle is worth the sum of the priced parts minus a discount (a bundle sells for less than its
parts; parting out a PC is work). Parts without a price count as 0, so the value is a
conservative lower bound — and only given when enough of the model-numbered parts are priced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Comparable, PriceEstimate
from .ai_key import ProductRef

MAX_COMPARABLES = 30


@dataclass
class Component:
    name: str  # as the AI wrote it, quantity removed
    qty: int = 1
    ref: ProductRef | None = None
    estimate: PriceEstimate | None = None  # market price of ONE piece
    grounded: bool = True  # really written in the ad (pricing.ai_key.grounded)

    @property
    def major(self) -> bool:
        """Has a model number ("RTX 3070", "i7 8700k"): a part that can carry real value.
        "Gehäuse", "Tastatur", "Kabel" are minor and never block a valuation."""
        return any(ch.isdigit() for ch in self.name) or (self.ref is not None and not self.ref.is_ai)

    @property
    def value(self) -> float | None:
        if self.estimate is None or not self.estimate.market_price:
            return None
        return self.qty * self.estimate.market_price


@dataclass
class BundleValue:
    estimate: PriceEstimate | None
    priced: list[Component] = field(default_factory=list)
    unpriced: list[Component] = field(default_factory=list)
    reason: str = ""  # why there is no estimate (Russian)


def _fmt(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".") + " €"


def value_bundle(
    components: list[Component], *, discount: float = 0.15, min_priced_share: float = 0.5,
    label: str = "Комплект",
) -> BundleValue:
    """Sum of the priced, grounded parts × (1 - discount) as a PriceEstimate (source "history"
    when every part came from our history, else "mixed"; sample_size = the thinnest part's).
    None (with a reason) when no part is priced or fewer than `min_priced_share` of the
    model-numbered parts are."""
    usable = [c for c in components if c.grounded and c.name]
    priced = [c for c in usable if c.value is not None]
    unpriced = [c for c in usable if c.value is None]
    if not priced:
        return BundleValue(None, [], unpriced, "Ни для одной части нет рыночной цены")
    major = [c for c in usable if c.major]
    major_priced = [c for c in major if c.value is not None]
    if major and len(major_priced) / len(major) < min_priced_share:
        names = ", ".join(c.name for c in major if c.value is None)[:120]
        return BundleValue(None, priced, unpriced, f"Мало данных о ценах частей: нет цен для {names}")
    keep = 1.0 - min(max(discount, 0.0), 0.9)
    market = sum(c.value or 0.0 for c in priced) * keep
    low = sum(c.qty * (c.estimate.low or c.estimate.market_price or 0.0) for c in priced if c.estimate) * keep
    high = sum(c.qty * (c.estimate.high or c.estimate.market_price or 0.0) for c in priced if c.estimate) * keep
    sources = {c.estimate.source for c in priced if c.estimate}
    source = "history" if sources <= {"history"} else "mixed"
    samples = [c.estimate.sample_size for c in priced if c.estimate]
    ages = [(c.estimate.age_days, c.value or 0.0) for c in priced if c.estimate and c.estimate.age_days is not None]
    days = [c.estimate.history_days for c in priced if c.estimate and c.estimate.history_days]
    comps: list[Comparable] = []
    for c in sorted(priced, key=lambda c: -(c.value or 0.0)):
        comps += (c.estimate.comparables if c.estimate else [])[: max(3, MAX_COMPARABLES // max(1, len(priced)))]
    parts = " + ".join(
        f"{c.qty}× {c.name} ≈ {_fmt(c.estimate.market_price or 0.0)}" if c.qty > 1
        else f"{c.name} ≈ {_fmt(c.estimate.market_price or 0.0)}"
        for c in priced if c.estimate
    )
    notes = f"{label} по частям: {parts}"
    if discount > 0:
        notes += f", минус {round(discount * 100)} % за комплект"
    if unpriced:
        notes += f"; без цены (не считаю): {', '.join(c.name for c in unpriced)[:120]}"
    weight = sum(w for _, w in ages)
    estimate = PriceEstimate(
        market_price=round(market, 2),
        low=round(low, 2),
        high=round(high, 2),
        sample_size=min(samples) if samples else 0,
        source=source,  # type: ignore[arg-type]
        query=" + ".join(c.name for c in priced)[:200],
        comparables=comps[:MAX_COMPARABLES],
        notes=notes,
        history_days=min(days) if days else None,
        age_days=round(sum(a * w for a, w in ages) / weight, 1) if weight > 0 else None,
    )
    return BundleValue(estimate, priced, unpriced)
