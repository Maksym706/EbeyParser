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
from ..pricing.text import FLAG_EMAIL, FLAG_WHATSAPP, SEVERE_FLAGS, detect_red_flags, is_remote_only
from .triage import TriageItem

# triage kinds that are never worth a second look, and risk tags that forbid one
NO_PROMOTE_KINDS = frozenset({"wanted", "swap", "service", "box", "defect", "other", "acc", "part"})
BLOCKING_RISKS = frozenset({"scam", "locked", "fake", "rent", "defect", "missing", "reserved"})
BUNDLE_KINDS = frozenset({"bundle", "pc", "lot"})
# The code's own red flags (pricing/text.py) back up what a small model misses (a 2B missed the
# WhatsApp / e-mail / «Sicher bezahlen» scams, AI_MODELS.md §4): the scout never promotes such an ad.
CODE_BLOCK_FLAGS = SEVERE_FLAGS | {FLAG_WHATSAPP, FLAG_EMAIL}
REMOTE_ONLY_RU = "только пересылка, без встречи"
UNKNOWN_PRIORITY = 0.75  # same scale as monitor._Candidate.discount (cost / market, lower = better)
DEMOTED_PRIORITY = 0.97  # blocked (wanted, swap, scam, …): after everything else
# The interest 0..10 of models up to 4B barely separates gems from junk (AUC 0.43 for 2B, 0.63 for
# 4B): among candidates without a price it only breaks ties, it never outranks real prices.
INTEREST_TIE_BREAK = 0.002

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
        """Rank in the paid stage (lower first): cost / market when priced from real data; unpriced
        ones rank together (the interest only breaks ties); blocked ones last."""
        if not self.item.is_ai:
            return UNKNOWN_PRIORITY
        if self.blocked:
            return DEMOTED_PRIORITY
        est = self.estimate
        if listing.is_free:
            return 0.0
        if est is not None and est.market_price and listing.price and listing.price > 1:
            return (listing.price + (listing.shipping_cost or 0.0)) / est.market_price
        return UNKNOWN_PRIORITY - INTEREST_TIE_BREAK * (max(0, min(10, self.interest)) - 5)

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


def code_red_flags(listing: Listing) -> list[str]:
    """The script's red-flag rules on the ad text, whatever the model said: scam phrasings
    (Vorkasse, PayPal Freunde, Western Union, «Sicher bezahlen» links), WhatsApp / Telegram /
    e-mail contact, wanted / swap / defect / locked … and, on Kleinanzeigen, "nur Versand"
    (a far-too-cheap find the seller won't show is the classic scam)."""
    text = ad_text(listing)
    flags = [f for f in detect_red_flags(text) if f in CODE_BLOCK_FLAGS]
    if listing.source != "ebay" and is_remote_only(text):
        flags.append(REMOTE_ONLY_RU)
    return flags


def _blocked(item: TriageItem, listing: Listing | None = None) -> str:
    if not item.is_ai:
        return "нет ответа нейросети"
    if item.kind in NO_PROMOTE_KINDS:
        return f"вид объявления: {item.kind}"
    risks = BLOCKING_RISKS.intersection(item.risks)
    if risks:
        return "риск: " + ", ".join(sorted(risks))
    flags = code_red_flags(listing) if listing is not None else []
    if flags:
        return "признаки: " + ", ".join(flags)
    return ""


def plan(listing: Listing, item: TriageItem, lookup: PriceLookup, *, bundle_discount: float = 0.15,
         pc_discount: float = 0.25, min_priced_share: float = 0.5) -> ScoutPlan:
    """Stage B for one ad: ground the product / parts in the ad text, price them from history."""
    text = ad_text(listing)
    kind = item.kind
    result = ScoutPlan(item=item, kind=kind, interest=item.interest, blocked=_blocked(item, listing),
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


def worth_a_look(plan_: ScoutPlan, *, deal_math: Callable[[PriceEstimate], bool], min_interest: int = 0) -> bool:
    """Promote an ad the script dismissed? Decided by data, not by the model's interest:

    * a real, unblocked AI reading whose product (or a bundle's parts) is grounded in the ad text;
    * and a market price from history whose math shows a possible deal, or something concrete
      for the paid stage to price: a named single product the history doesn't know (comparables
      with the scout's phrase), a bundle's model-numbered parts without a price yet.

    A bundle with nothing priceable is not promoted (no data, at most a guess). The interest
    is only a weak veto: below `min_interest` (0 = off) the ad is not promoted."""
    if not plan_.usable:
        return False
    if min_interest and plan_.interest < min_interest:
        return False
    est = plan_.estimate
    if est is not None and est.market_price:
        if deal_math(est):
            return True
        if plan_.kind not in BUNDLE_KINDS:
            return False  # the history knows this product: no deal
    if plan_.kind in BUNDLE_KINDS:
        # the parts' sum is a lower bound: model-numbered parts without a price yet ("i7-8700K")
        # may still make it a deal once the paid stage looks up their comparables
        return bool(unpriced_parts(plan_))
    return plan_.identified and bool(plan_.query)


def unpriced_parts(plan_: ScoutPlan, limit: int = 2) -> list[Component]:
    """Grounded, model-numbered parts identity.py knows but the history had no price for."""
    return [c for c in plan_.components if c.grounded and c.estimate is None and c.major
            and c.ref is not None and c.ref.identity is not None][:limit]


def vision_for_plan(verdict: AIVerdict | None, plan_: ScoutPlan | None) -> AIVerdict | None:
    """A PC / lot / bundle the scout read as one is expected to BE one: the vision model's
    item_type "complete_pc" / "bundle" / "laptop" is then no veto (bundle = several products
    sold together, priced from parts — or, without prices, at most "maybe" by the old rule).
    Everything else in the verdict stays (part, accessory, box only, wanted still veto)."""
    if verdict is None or plan_ is None or plan_.kind not in BUNDLE_KINDS or not plan_.usable:
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


def reprice(plan_: ScoutPlan, *, bundle_discount: float = 0.15, pc_discount: float = 0.25,
            min_priced_share: float = 0.5) -> ScoutPlan:
    """Value the bundle again after parts got prices (comparables in the paid stage)."""
    if plan_.kind not in BUNDLE_KINDS:
        return plan_
    discount = pc_discount if plan_.kind in ("pc", "lot") else bundle_discount
    plan_.bundle = value_bundle(plan_.components, discount=discount, min_priced_share=min_priced_share,
                                label=KIND_LABEL_RU.get(plan_.kind, "Комплект"))
    plan_.estimate = plan_.bundle.estimate
    return plan_


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


# ---------------------------------------------------------------------------
# Learning loop and status (for the monitor and the UI)
# ---------------------------------------------------------------------------

HINTS_LIMIT = 600  # characters of user preferences in the prompt


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def feedback_hints(examples: dict[str, list[dict[str, Any]]], *, limit: int = HINTS_LIMIT) -> str:
    """The user's feedback as a few prompt lines: hidden deals (+ reason) mean "not interested
    in things like this", bought / sold ones "more like this". Capped at `limit` characters."""
    lines: list[str] = []
    for row in examples.get("good", [])[:3]:
        bits = [f"bought {row['bought']:.0f} €" if row.get("bought") else "",
                f"sold {row['sold']:.0f} €" if row.get("sold") else ""]
        extra = ", ".join(b for b in bits if b)
        lines.append(f"- good buy: {_clip(row.get('title'), 60)}" + (f" ({extra})" if extra else ""))
    for row in examples.get("hidden", [])[:4]:
        reason = _clip(row.get("reason"), 40)
        lines.append(f"- not interesting: {_clip(row.get('title'), 60)}" + (f" ({reason})" if reason else ""))
    if not lines:
        return ""
    text = "The user's feedback (rate similar ads accordingly):\n" + "\n".join(lines)
    return text[:limit]


MODE_RU = {
    "all": "читает все новые объявления",
    "candidates": "читает только непонятные объявления: без модели, ПК, комплекты, лоты",
}


def _words(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


TOO_SMALL_RU = ("Модель {model} слишком маленькая для разведчика (меньше 2B): она путает объявления."
                " Объявления смотрю обычным способом. Возьми Qwen3.5 2B или больше")


def status_view(*, enabled: bool, mode_setting: str, provider: str, base_url: str, model: str, own_endpoint: bool,
                snap: dict[str, Any], vision_waiting: int, vision_wait_minutes: float,
                expected_sec_per_ad: float | None = None, pass_share: float = 0.5, max_per_hour: int = 0,
                too_small: bool = False) -> dict[str, Any]:
    """Everything the Settings → Нейросеть and Состояние screens show about the scout.
    `expected_sec_per_ad`: the model research's speed for this model (before it measured its own)."""
    seen = int(snap.get("seen_last_hour") or 0)
    read = int(snap.get("triaged_last_hour") or 0)
    capacity = snap.get("capacity_per_hour")
    sec = snap.get("sec_per_ad")
    mode = snap.get("mode") or ("all" if mode_setting == "auto" else mode_setting)
    error, error_at, ok_at = snap.get("last_error") or "", snap.get("last_error_at"), snap.get("last_ok_at")
    down = bool(error) and (ok_at is None or (error_at or 0) > ok_at)
    if not enabled:
        state, text = "off", "Разведчик выключен — объявления отбираю по названию и истории цен"
    elif too_small:
        state, text = "too_small", TOO_SMALL_RU.format(model=model or "?")
    elif down:
        state, text = "down", "Разведчик не отвечает — пока смотрю объявления обычным способом"
    elif seen == 0 and read == 0:
        state, text = "idle", "Разведчик готов — новых объявлений за последний час не было"
    else:
        state = "ok" if read >= seen else "behind"
        text = f"Успевает смотреть {read} из {seen} {_words(seen, 'нового объявления', 'новых объявлений', 'новых объявлений')} в час"
    speed = ""
    expected = False
    if not sec and expected_sec_per_ad and not too_small:
        sec, expected = float(expected_sec_per_ad), True
        per_hour = 3600.0 * max(0.05, pass_share) / sec
        capacity = int(min(max_per_hour, per_hour) if max_per_hour > 0 else per_hour)
    if sec:  # Russian decimal comma; an estimate is flagged by speed_expected (the app's «оценка» mark)
        speed = f"≈ {sec:.1f} с на объявление".replace(".", ",")
        if capacity:
            speed += f", до {capacity} {_words(int(capacity), 'объявления', 'объявлений', 'объявлений')} в час"
    return {
        "enabled": enabled,
        "state": state,
        "text_ru": text,
        "speed_ru": speed,
        "mode": mode,
        "mode_setting": mode_setting,
        "mode_ru": MODE_RU.get(mode, ""),
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "own_endpoint": own_endpoint,
        "seen_last_hour": seen,
        "read_last_hour": read,
        "overflow_last_hour": int(snap.get("overflow_last_hour") or 0),
        "failed_last_hour": int(snap.get("failed_last_hour") or 0),
        "sec_per_ad": sec,
        "speed_expected": expected,
        "too_small": too_small,
        "capacity_per_hour": capacity,
        "batch_size": snap.get("batch_size") or None,
        "vision_queue": {
            "waiting": vision_waiting,
            "max_wait_minutes": vision_wait_minutes,
            "text_ru": (f"Ждут проверки фото: {vision_waiting}" if vision_waiting else ""),
        },
    }
