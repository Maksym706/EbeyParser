"""Which ads fit a build slot, and what a part typically costs: the app's own price history
(price_points) and the offers of the project's searches first, the knowledge base's rough
«ориентир» only as a fallback. Never an LLM price."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..models import Comparable, Listing, utcnow
from ..pricing.estimator import estimate_from_history
from ..pricing.identity import comparable_matches, identify
from ..pricing.text import SEVERE_FLAGS, detect_red_flags, normalize
from .knowledge import ROUGH_LABEL, Part
from .models import PlanOption

log = logging.getLogger(__name__)

HISTORY_DAYS = 60
MIN_POINTS = 5
# kinds of offer that are never a part for the build (identity.classify_kind)
HARD_BAD_KINDS = frozenset({"wanted", "swap", "service", "box_only", "defect"})
BAD_KINDS = HARD_BAD_KINDS | {"complete_pc", "laptop", "bundle", "part", "accessory"}
# identity's kinds are tuned for whole products: for GPUs, CPUs and boards they are reliable; RAM,
# PSUs, drives, cases, coolers ("Gehäuse" = part, "Netzteil" = accessory, "Server-RAM" = PC) are
# checked by their own attributes, and only whole computers are rejected by word
STRICT_KINDS = frozenset({"gpu", "cpu", "board"})
_COMPUTER_WORDS = ("pc", "computer", "rechner", "komplettsystem", "komplett pc", "gaming pc", "workstation")
_WATTS_RE = re.compile(r"(\d{3,4})\s*(?:w|watt)\b")
_DDR_RE = re.compile(r"\b(?:lp)?ddr([345])")
_KIT_RE = re.compile(r"\b([1-8])\s*x\s*(\d{1,3})\s*gb\b")
_GB_RE = re.compile(r"\b(\d{1,3})\s*gb\b")
_SIZE_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(tb|gb)\b")
_REG_WORDS = ("reg", "registered", "rdimm", "lrdimm")
_SODIMM_WORDS = ("sodimm", "so dimm", "laptop", "notebook")
_HDD_WORDS = ("hdd", "festplatte", "ironwolf", "wd red", "exos", "ultrastar", "barracuda", "nas festplatte", "toshiba n300")


def _words(text: str) -> list[str]:
    return normalize(text).split()


def has_phrase(text: str, phrase: str) -> bool:
    """Every word of `phrase` starts a word of `text` (normalized: case, umlauts, punctuation)."""
    words = _words(text)
    wanted = _words(phrase)
    return bool(wanted) and all(any(w.startswith(p) for w in words) for p in wanted)


def _kind(title: str, description: str) -> str:
    try:
        return identify(title, description).kind
    except Exception:  # noqa: BLE001
        return "unknown"


def watts(text: str) -> int | None:
    found = [int(m.group(1)) for m in _WATTS_RE.finditer(text.lower())]
    found = [w for w in found if 300 <= w <= 2000]
    return max(found) if found else None


def ram_attrs(text: str) -> dict[str, Any]:
    low = normalize(text)
    gen = _DDR_RE.search(low)
    kit = _KIT_RE.search(low)
    size: int | None = None
    if kit:
        size = int(kit.group(1)) * int(kit.group(2))
    else:
        sizes = [int(m.group(1)) for m in _GB_RE.finditer(low) if 2 <= int(m.group(1)) <= 512]
        size = max(sizes) if sizes else None
    return {"gen": f"ddr{gen.group(1)}" if gen else None, "size_gb": size,
            "registered": any(has_phrase(low, w) for w in _REG_WORDS) and "unbuffered" not in low,
            "sodimm": any(has_phrase(low, w) for w in _SODIMM_WORDS)}


def storage_attrs(text: str) -> dict[str, Any]:
    low = normalize(text)
    words = low.split()
    kind = None
    if "nvme" in words or "m.2" in text.lower() or "m2" in words:
        kind = "nvme"
    elif any(has_phrase(low, w) for w in _HDD_WORDS):
        kind = "hdd"
    elif "ssd" in words:
        kind = "ssd"
    size = None
    for m in _SIZE_RE.finditer(low):
        value = float(m.group(1).replace(",", "."))
        tb = value if m.group(2) == "tb" else value / 1000
        if 0.1 <= tb <= 40:
            size = max(size or 0.0, tb)
    return {"type": kind, "size_tb": size, "external": has_phrase(low, "extern")}


def part_matches(part: Part, title: str, description: str = "") -> bool:
    """Is this ad the part itself (not a wanted ad, a defect, a PC with it, a bundle)?"""
    if not _kind_ok(part, _kind(title, description), title):
        return False
    text = f"{title} {description[:300]}"
    if part.kind in ("gpu", "cpu"):
        for phrase in part.match:
            try:
                ok = comparable_matches(phrase, title, description)[0]
            except Exception:  # noqa: BLE001
                ok = False
            if ok or (not ok and _identity_blind(title) and has_phrase(title, phrase)):
                if not any(has_phrase(title, ex) for ex in part.exclude):
                    return True
        return False
    if part.kind == "ram":
        attrs = ram_attrs(title)
        s = part.specs
        return (attrs["gen"] == s["gen"] and (attrs["size_gb"] or 0) >= s["size_gb"]
                and attrs["registered"] == s["registered"] and attrs["sodimm"] == s["sodimm"])
    if part.kind == "psu":
        found = watts(title) or watts(text)
        return found is not None and found >= part.specs["watts"] and not has_phrase(title, "adapter")
    if part.kind == "storage":
        attrs = storage_attrs(title)
        s = part.specs
        wanted = s["type"]
        type_ok = attrs["type"] == wanted or (wanted == "ssd" and attrs["type"] == "nvme")
        return type_ok and (attrs["size_tb"] or 0) >= s["size_tb"] * 0.9 and not attrs["external"]
    return any(has_phrase(title, phrase) for phrase in part.match)


def _kind_ok(part: Part, kind: str, title: str) -> bool:
    if kind in HARD_BAD_KINDS:
        return False
    if part.kind in STRICT_KINDS:
        return kind not in BAD_KINDS
    words = normalize(title).split()
    return not any(w in words for w in _COMPUTER_WORDS if " " not in w) and \
        not any(has_phrase(title, w) for w in _COMPUTER_WORDS if " " in w)


def _identity_blind(title: str) -> bool:
    """identity does not know this product (e.g. a Tesla / Instinct card written oddly)."""
    try:
        return identify(title).key is None
    except Exception:  # noqa: BLE001
        return True


def option_matches(option: PlanOption, part: Part | None, title: str, description: str = "") -> bool:
    if part is not None:
        return part_matches(part, title, description)
    query = option.query or option.label
    if not query.strip():
        return False
    if _kind(title, description) in HARD_BAD_KINDS:
        return False
    try:
        if comparable_matches(query, title, description)[0]:
            return True
    except Exception:  # noqa: BLE001
        pass
    return has_phrase(title, query)


def severe(title: str, description: str = "", flags: Iterable[str] = ()) -> bool:
    found = set(flags) | set(detect_red_flags(f"{title}\n{description}"))
    return any(f in SEVERE_FLAGS for f in found)


# ------------------------------------------------------------------ prices
@dataclass
class PriceInfo:
    typical: float | None
    low: float | None
    high: float | None
    source: str  # history | kb | none
    label_ru: str
    rough: bool
    sample_size: int = 0
    new: float | None = None
    new_label: str = ""
    notes_ru: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"typical": _r(self.typical), "low": _r(self.low), "high": _r(self.high), "source": self.source,
                "label_ru": self.label_ru, "rough": self.rough, "sample_size": self.sample_size,
                "new": _r(self.new), "new_label_ru": self.new_label, "notes_ru": self.notes_ru}


def _r(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), 2)


def kb_price(part: Part | None) -> PriceInfo:
    if part is None:
        return PriceInfo(None, None, None, "none", "цены пока нет — узнаю из объявлений", True)
    p = part.price
    return PriceInfo(p.typical, p.low, p.high, "kb", ROUGH_LABEL, True, 0, p.new, p.new_label)


class KbMarket:
    """Knowledge-base prices only (no database)."""

    def price(self, part: Part | None, option: PlanOption | None = None,
              extra: Iterable[tuple[Comparable, datetime, datetime]] = ()) -> PriceInfo:
        return kb_price(part)

    def points(self, part: Part | None, option: PlanOption | None = None,
               extra: Iterable[tuple[Comparable, datetime, datetime]] = ()) -> list[tuple[Comparable, datetime, datetime]]:
        return list(extra)


class Market(KbMarket):
    """Price history (+ the offers of linked searches) filtered by the part matcher; cached for
    `ttl` seconds per part/option."""

    def __init__(self, db: Any, config: Any = None, *, days: int = HISTORY_DAYS, min_points: int = MIN_POINTS,
                 ttl: float = 300.0) -> None:
        self.db = db
        self.config = config
        self.days = days
        self.min_points = min_points
        self.ttl = ttl
        self._cache: dict[str, tuple[float, list[tuple[Comparable, datetime, datetime]]]] = {}

    def _discount(self) -> float:
        pricing = getattr(self.config, "pricing", None)
        return float(getattr(pricing, "asking_price_discount", 0.85) or 0.85)

    def _history(self, key: str, query: str, fits: Any) -> list[tuple[Comparable, datetime, datetime]]:
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        out: list[tuple[Comparable, datetime, datetime]] = []
        if query.strip():
            try:
                rows = self.db.price_history_dated(query, utcnow() - timedelta(days=self.days), limit=800)
            except (sqlite3.Error, AttributeError) as exc:
                log.info("price history for %r failed: %s", query, exc)
                rows = []
            for comp, seen in rows:
                try:
                    if fits(comp.title):
                        out.append((comp, seen, seen))
                except Exception:  # noqa: BLE001
                    continue
        self._cache[key] = (time.monotonic(), out)
        return out

    def points(self, part: Part | None, option: PlanOption | None = None,
               extra: Iterable[tuple[Comparable, datetime, datetime]] = ()) -> list[tuple[Comparable, datetime, datetime]]:
        if part is not None:
            key, query = f"kb:{part.key}", part.query
            fits = lambda title: part_matches(part, title)  # noqa: E731
        elif option is not None and (option.query or option.label):
            key, query = f"q:{normalize(option.query or option.label)}", option.query or option.label
            fits = lambda title: option_matches(option, None, title)  # noqa: E731
        else:
            return list(extra)
        merged: dict[str, tuple[Comparable, datetime, datetime]] = {}
        for comp, first, last in [*self._history(key, query, fits), *extra]:
            ident = comp.url or f"{comp.title}|{comp.price}"
            merged.setdefault(ident, (comp, first, last))
        return list(merged.values())

    def price(self, part: Part | None, option: PlanOption | None = None,
              extra: Iterable[tuple[Comparable, datetime, datetime]] = ()) -> PriceInfo:
        points = self.points(part, option, extra)
        est = None
        if len(points) >= self.min_points:
            query = part.query if part is not None else (option.query or option.label if option else "")
            est = estimate_from_history(query, points, asking_price_discount=self._discount(),
                                        min_points=self.min_points, days=self.days, fits=lambda c: True)
        base = kb_price(part)
        if est is None or est.market_price is None:
            if points:
                base.notes_ru = f"в истории пока {len(points)} цен — мало, показываю ориентир"
            return base
        n = est.sample_size
        return PriceInfo(est.market_price, est.low, est.high, "history",
                         f"рынок по {n} объявлениям за {self.days} дней", False, n, base.new, base.new_label,
                         est.notes)


def listing_point(listing: Listing, first: datetime | None, last: datetime | None) -> tuple[Comparable, datetime, datetime]:
    now = datetime.now(timezone.utc)
    comp = Comparable(title=listing.title, price=float(listing.price or 0), url=listing.url,
                      source="ebay" if listing.source == "ebay" else "kleinanzeigen")
    return comp, first or now, last or now


__all__ = [
    "BAD_KINDS", "HARD_BAD_KINDS", "KbMarket", "Market", "PriceInfo", "has_phrase", "kb_price", "listing_point", "option_matches",
    "part_matches", "ram_attrs", "severe", "storage_attrs", "watts",
]
