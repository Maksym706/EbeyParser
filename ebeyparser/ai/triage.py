"""AI scout, stage A: text-only triage of every new ad, in batches (docs/design/AI_SCOUT.md).

A small local model reads 2-16 ads per call (title, price, category, snippet) and returns
per ad: kind, canonical product, quantity, bundle contents, a German search phrase,
condition, hidden-value tags, risk tags, interest 0..10 and a one-line Russian reason.
No prices: money comes from our own price history (pricing.ai_key / pricing.bundle).

Built for a weak home server: the batch size adapts to the parse success rate and to the
measured seconds per ad; a call that returns broken or partial JSON is retried for the
missing ads in smaller batches, and whatever still fails falls back to the script's own
identity (source "script"). A per-pass deadline and an hourly cap keep the CPU usable; ads
not reached ("overflow") simply take the script path. If the model is off or down, nothing
changes compared with the app without a scout.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, Field

from ..models import Listing, utcnow
from ..pricing.text import is_wanted_ad, normalize
from .client import ChatModel, LLMError
from .prompts_triage import (
    CONDITIONS,
    HIDDEN_TAGS,
    KINDS,
    MAX_CONTENTS,
    RISK_TAGS,
    SYSTEM_PROMPT,
    TRIAGE_SCHEMA,
    build_user_prompt,
)

log = logging.getLogger(__name__)

TRIAGE_VERSION = 1
MAX_SPLIT_DEPTH = 2  # a failed batch is retried in halves, then in quarters, then: script fallback
TOKENS_PER_ITEM = 110  # answer tokens per ad (flat schema, short keys); used to cap the batch
ANSWER_OVERHEAD_TOKENS = 60
TARGET_CALL_SECONDS = 120.0  # keep one call well inside the timeout (and the UI responsive)
EMA_ALPHA = 0.3
GROW_AFTER = 3  # full successes in a row before the batch grows

Source = Literal["ai", "script"]

# ---------------------------------------------------------------------------
# Model size: batch size and the "too small" guard (docs/design/AI_MODELS.md §4-5)
# ---------------------------------------------------------------------------

# Qwen3.5 "2B" has 1.9B parameters. The 0.8B renumbered ads and copied their text (kind 20 %):
# nothing under ~2B is used for triage.
MIN_TRIAGE_PARAMS_B = 1.8
SMALL_MODEL_PARAMS_B = 2.5  # up to here batch 5: the 2B reads kinds at 80 % in 5s, 63 % in 10s
BIG_MODEL_PARAMS_B = 4.0  # from here batch 10
DEFAULT_BATCH = (8, 16)  # size unknown: (start, max)
_SIZE_RE = re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")


def model_params_b(model_id: str) -> float | None:
    """Total parameters in billions: from the id ("qwen3.5:2b-q4_K_M" -> 2, "Qwen2.5-1.5B-Instruct"
    -> 1.5, "qwen3.6-35b-a3b" -> 35), else from the model catalog; None = unknown."""
    text = (model_id or "").strip().lower()
    sizes = [float(m.group(1)) for m in _SIZE_RE.finditer(text.rsplit("/", 1)[-1])]
    if sizes:
        return max(sizes)
    if not text:
        return None
    try:
        from .model_catalog import find_catalog_model

        found = find_catalog_model(model_id, "triage")
    except Exception:  # noqa: BLE001 - only a hint
        found = None
    return float(found.params_b) if found is not None else None


def too_small_for_triage(model_id: str) -> bool:
    size = model_params_b(model_id)
    return size is not None and size < MIN_TRIAGE_PARAMS_B


def expected_sec_per_ad(model_id: str, tier: str = "T0") -> float | None:
    """Seconds per ad the model research measured (T0: a shared 4-vCPU box, N100 class) or
    extrapolated for a hardware tier (model_catalog sec_per_ad): the scout's speed before it has
    measured its own. None = not a catalog triage model or no number for that tier."""
    if not (model_id or "").strip():
        return None
    try:
        from .model_catalog import find_catalog_model

        found = find_catalog_model(model_id, "triage")
    except Exception:  # noqa: BLE001 - only an estimate
        return None
    if found is None or found.task != "triage":
        return None
    return found.sec_per_ad.get(tier) or None


def batch_for_model(model_id: str) -> tuple[int, int]:
    """(first batch size, largest batch) for a model: 5/5 for ~2B, 8/10 for 3B, 10/16 for 4B+."""
    size = model_params_b(model_id)
    if size is None:
        return DEFAULT_BATCH
    if size <= SMALL_MODEL_PARAMS_B:
        return (5, 5)
    if size < BIG_MODEL_PARAMS_B:
        return (8, 10)
    return (10, 16)


class TriageItem(BaseModel):
    """What the scout says about one ad."""

    ad_id: str = ""
    kind: str = "other"  # prompts_triage.KINDS
    product: str = ""  # "Apple iPhone 13 Pro 256GB"; "" = no model named
    qty: int = 1
    contents: list[str] = Field(default_factory=list)  # bundle / pc / lot: ["RTX 3070", "2x DualSense"]
    query: str = ""  # German search phrase for comparables
    condition: str = "unknown"
    hidden: list[str] = Field(default_factory=list)  # prompts_triage.HIDDEN_TAGS
    risks: list[str] = Field(default_factory=list)  # prompts_triage.RISK_TAGS
    interest: int = 0  # 0..10
    reason: str = ""  # one line, Russian
    source: Source = "ai"
    model: str = ""
    triaged_at: datetime = Field(default_factory=utcnow)
    version: int = TRIAGE_VERSION

    @property
    def is_ai(self) -> bool:
        return self.source == "ai"


# ---------------------------------------------------------------------------
# Parsing (never raises)
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)(?:```|$)", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

_KEY_ALIASES = {
    "i": "i", "index": "i", "idx": "i", "nr": "i", "id": "i", "n_ad": "i", "ad": "i", "ad_index": "i",
    "k": "k", "kind": "k", "type": "k", "typ": "k", "art": "k", "category": "k",
    "p": "p", "product": "p", "produkt": "p", "name": "p", "model": "p", "modell": "p", "item": "p",
    "n": "n", "qty": "n", "quantity": "n", "anzahl": "n", "count": "n", "menge": "n",
    "c": "c", "contents": "c", "content": "c", "inhalt": "c", "parts": "c", "components": "c",
    "q": "q", "query": "q", "search": "q", "suche": "q", "search_query": "q", "suchbegriff": "q",
    "z": "z", "condition": "z", "zustand": "z", "state": "z",
    "h": "h", "hidden": "h", "tags": "h", "hidden_value": "h", "flags": "h",
    "x": "x", "risk": "x", "risks": "x", "red_flags": "x", "warnings": "x",
    "s": "s", "interest": "s", "score": "s", "rating": "s", "interesse": "s",
    "r": "r", "reason": "r", "grund": "r", "why": "r", "reasoning": "r", "comment": "r",
}
_ITEM_KEYS = frozenset({"i", "k", "p", "n", "c", "q", "z", "h", "x", "s", "r"})

_KIND_ALIASES = {
    "sale": "single", "for_sale": "single", "verkauf": "single", "angebot": "single",
    "item": "single", "product": "single", "einzel": "single", "einzeln": "single", "one": "single",
    "set": "bundle", "paket": "bundle", "kit": "bundle",
    "computer": "pc", "complete_pc": "pc", "rechner": "pc", "desktop": "pc", "gaming_pc": "pc",
    "konvolut": "lot", "nachlass": "lot", "sammlung": "lot", "mixed": "lot", "lots": "lot",
    "spare": "part", "ersatzteil": "part", "component": "part", "parts": "part",
    "accessory": "acc", "accessories": "acc", "zubehoer": "acc", "zubehör": "acc", "case": "acc",
    "box_only": "box", "ovp": "box", "packaging": "box", "karton": "box",
    "suche": "wanted", "gesuch": "wanted", "search": "wanted",
    "tausch": "swap", "trade": "swap",
    "dienstleistung": "service", "repair": "service",
    "defective": "defect", "defekt": "defect", "broken": "defect", "kaputt": "defect",
    "laptop": "single", "notebook": "single", "phone": "single", "console": "single",
}
_COND_ALIASES = {"like_new": "good", "sehr_gut": "good", "gut": "good", "neu": "new", "gebraucht": "used",
                 "defective": "defect", "defekt": "defect", "broken": "defect", "fair": "used", "ok": "used"}
_TAG_ALIASES = {
    "typo": "typo", "misspelled": "typo", "tippfehler": "typo",
    "vague": "vague", "unclear": "vague", "hidden": "vague",
    "unknown_model": "unknown_model", "unknown": "unknown_model", "seller_unaware": "unknown_model",
    "pc_parts": "pc_parts", "pc": "pc_parts", "parts": "pc_parts",
    "lot": "lot", "konvolut": "lot", "bundle": "bundle", "set": "bundle",
    "wrong_category": "wrong_category", "wrong_cat": "wrong_category", "category": "wrong_category",
    "cheap": "cheap", "underpriced": "cheap", "low_price": "cheap",
    "scam": "scam", "fraud": "scam", "betrug": "scam", "prepayment": "scam", "vorkasse": "scam",
    "defect": "defect", "broken": "defect", "defekt": "defect",
    "locked": "locked", "icloud": "locked", "lock": "locked",
    "fake": "fake", "replica": "fake", "copy": "fake",
    "missing": "missing", "incomplete": "missing",
    "reserved": "reserved", "reserviert": "reserved",
    "rent": "rent", "miete": "rent", "rental": "rent",
}


def _strip(text: str) -> str:
    text = _THINK_RE.sub("", text or "").strip()
    fences = _FENCE_RE.findall(text)
    return fences[0].strip() if fences and fences[0].strip() else text


def _loads(candidate: str) -> Any:
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            return json.loads(attempt)
        except (ValueError, RecursionError):
            pass
    py = _TRAILING_COMMA_RE.sub(r"\1", candidate)
    py = re.sub(r"\btrue\b", "True", py)
    py = re.sub(r"\bfalse\b", "False", py)
    py = re.sub(r"\bnull\b", "None", py)
    try:
        return ast.literal_eval(py)
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return None


def _balanced_from(text: str, start: int) -> str | None:
    """The balanced {...} starting at `start` (string-aware), None if it never closes."""
    depth, in_str, esc, quote = 0, False, False, ""
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        elif ch == '"':
            in_str, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    return None


def _canon(obj: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        canon = _KEY_ALIASES.get(str(key).strip().lower().replace(" ", "_"))
        if canon and canon not in out:
            out[canon] = value
    return out


def _item_like(obj: Any) -> bool:
    return isinstance(obj, dict) and len(_ITEM_KEYS & set(_canon(obj))) >= 3


def _raw_items(text: str) -> list[dict]:
    """Item objects from whatever the model wrote: {"items": [...]}, a bare list, {"0": {...}},
    several objects, a truncated answer (the complete objects are kept), fences, <think>."""
    body = _strip(text)
    data = _loads(body)
    if isinstance(data, dict):
        for key in ("items", "ads", "results", "anzeigen", "data", "output"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        if _item_like(data):
            return [data]
        values = [v for v in data.values() if isinstance(v, dict)]
        if values and all(_item_like(v) for v in values):  # {"0": {...}, "1": {...}}
            keyed: list[dict] = []
            for k, v in data.items():
                if isinstance(v, dict):
                    v = dict(v)
                    if str(k).isdigit() and not any(a in v for a in ("i", "index", "idx")):
                        v["i"] = int(k)
                    keyed.append(v)
            return keyed
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    # broken / truncated JSON: every complete item-like object anywhere in the text
    out: list[dict] = []
    pos = 0
    while True:
        start = body.find("{", pos)
        if start < 0:
            break
        chunk = _balanced_from(body, start)
        if chunk is None:
            pos = start + 1
            continue
        obj = _loads(chunk)
        if _item_like(obj):
            out.append(obj)
            pos = start + len(chunk)
        else:
            pos = start + 1
    return out


def _int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value or ""))
    if not m:
        return default
    try:
        return int(float(m.group(0).replace(",", ".")))
    except ValueError:
        return default


def _interest(value: Any) -> int:
    if isinstance(value, float) and 0 < value <= 1.0:
        return round(value * 10)
    text = str(value or "")
    if "/" in text:  # "7/10"
        n = _int(text.split("/")[0], 0) or 0
        return max(0, min(10, n))
    n = _int(value, 0) or 0
    return max(0, min(10, n if n <= 10 else round(n / 10)))


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return " ".join(str(value).split())[:limit]


def _kind(value: Any) -> str:
    text = _text(value, 40).lower().replace(" ", "_").replace("-", "_")
    if text in KINDS:
        return text
    return _KIND_ALIASES.get(text, "other")


def _tags(value: Any, allowed: tuple[str, ...]) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    items = re.split(r"[,;|]+", value) if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        tag = _text(item, 40).lower().replace(" ", "_").replace("-", "_")
        tag = tag if tag in allowed else _TAG_ALIASES.get(tag, "")
        if tag in allowed and tag not in out:
            out.append(tag)
    return out


def _contents(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    items = re.split(r"[;\n]+|,(?!\d)", value) if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = _text(item.get("name") or item.get("p") or item.get("product") or next(iter(item.values()), ""), 80)
            qty = _int(item.get("qty") or item.get("n") or item.get("anzahl"), 1) or 1
            item = f"{qty}x {name}" if qty > 1 else name
        text = _text(item, 80).strip(" -•*")
        if text and normalize(text) not in ("", "none", "keine", "nichts") and text not in out:
            out.append(text)
    return out[:MAX_CONTENTS]


_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
REASON_MAX_WORDS = 12  # the prompt asks for 8; a little slack, then cut


def _reason(value: Any) -> str:
    """The Russian one-liner. Small models copy the German ad text into it (AI_MODELS.md §5):
    a reason without a single Cyrillic letter is dropped (the UI then builds its own line)."""
    text = _text(value, 140)
    if not _CYRILLIC_RE.search(text):
        return ""
    return " ".join(text.split()[:REASON_MAX_WORDS])


def _to_item(raw: dict, *, model: str) -> TriageItem:
    data = _canon(raw)
    cond = _text(data.get("z"), 20).lower().replace(" ", "_")
    cond = cond if cond in CONDITIONS else _COND_ALIASES.get(cond, "unknown")
    product = _text(data.get("p"), 120)
    if normalize(product) in ("", "none", "null", "unknown", "unbekannt", "n a", "keine", "nichts"):
        product = ""
    return TriageItem(
        kind=_kind(data.get("k")),
        product=product,
        qty=max(1, min(99, _int(data.get("n"), 1) or 1)),
        contents=_contents(data.get("c")),
        query=" ".join(_text(data.get("q"), 80).split()[:6]),
        condition=cond,
        hidden=_tags(data.get("h"), HIDDEN_TAGS),
        risks=_tags(data.get("x"), RISK_TAGS),
        interest=_interest(data.get("s")),
        reason=_reason(data.get("r")),
        source="ai",
        model=model,
    )


def parse_triage(text: str, n: int, *, model: str = "") -> dict[int, TriageItem]:
    """Items by ad index 0..n-1. Uses "i" when it is a valid, unused index; items without a
    usable index are placed by order only when the answer has exactly n items. Never raises."""
    try:
        raws = _raw_items(text or "")
    except Exception:  # noqa: BLE001
        log.exception("triage parse failed")
        return {}
    out: dict[int, TriageItem] = {}
    unplaced: list[tuple[int, TriageItem]] = []
    for pos, raw in enumerate(raws):
        try:
            item = _to_item(raw, model=model)
        except Exception:  # noqa: BLE001 - one bad item never loses the others
            continue
        idx = _int(_canon(raw).get("i"))
        if idx is not None and 0 <= idx < n and idx not in out:
            out[idx] = item
        else:
            unplaced.append((pos, item))
    if unplaced and len(raws) == n:
        free = [i for i in range(n) if i not in out]
        for (_, item), idx in zip(unplaced, free):
            out[idx] = item
    return out


# ---------------------------------------------------------------------------
# Script fallback: the same shape from identity.py (never promotes anything)
# ---------------------------------------------------------------------------

_IDENTITY_KIND = {"item": "single", "bundle": "bundle", "complete_pc": "pc", "laptop": "single", "part": "part",
                  "accessory": "acc", "box_only": "box", "defect": "defect", "wanted": "wanted", "swap": "swap",
                  "service": "service", "unknown": "other"}


def script_item(listing: Listing) -> TriageItem:
    """What the regex identity knows about the ad, as a TriageItem with source "script"."""
    from ..pricing.estimator import listing_identity

    ident = listing_identity(listing)
    product = ident.key.query() if ident.key is not None else ""
    return TriageItem(ad_id=listing.ad_id, kind=_IDENTITY_KIND.get(ident.kind, "other"), product=product,
                      query=product, interest=5 if ident.key is not None else 3, source="script",
                      reason="", model="script")


# ---------------------------------------------------------------------------
# Priority: fresh first, then where the script is blind / the ad looks valuable
# ---------------------------------------------------------------------------

_LOT_WORDS = frozenset("""
    konvolut nachlass dachboden dachbodenfund keller kellerfund sammlung haushaltsaufloesung
    wohnungsaufloesung restposten flohmarkt kiste karton paket alles zusammen sammelsurium
""".split())
_PC_WORDS = frozenset("pc computer rechner tower desktop gamingpc".split())
_VAGUE_WORDS = frozenset("""
    alt alte alter altes weiss nicht keine ahnung unbekannt irgendwas elektronik technik teile
    zeug sachen geraet geraete verschiedenes diverses sonstiges
""".split())


def scout_priority(listing: Listing) -> float:
    """How much the AI can add for this ad (higher = read first): the script is blind to ads
    without a recognisable product, to PCs, bundles and lots; free ads and real prices help;
    wanted ads and junk prices go last."""
    from ..pricing.estimator import listing_identity

    words = set(normalize(f"{listing.title} {listing.description[:300]}").split())
    title_words = set(normalize(listing.title).split())
    score = 0.0
    try:
        ident = listing_identity(listing)
    except Exception:  # noqa: BLE001
        ident = None
    if ident is None or ident.key is None:
        score += 3.0
    elif ident.category in (None, "other"):
        score += 2.0  # catch-all key ("iphne 13"): probably a typo or an unknown product
    elif ident.kind != "item":
        score += 1.5
    if ident is not None and ident.kind in ("complete_pc", "bundle", "laptop"):
        score += 2.0
    if words & _LOT_WORDS:
        score += 3.0
    if title_words & _PC_WORDS:
        score += 2.0
    if title_words & _VAGUE_WORDS:
        score += 1.0
    if listing.is_free:
        score += 2.0
    elif listing.price is None:
        score += 0.5
    elif 20 <= listing.price <= 2000:
        score += 1.0
    elif listing.price < 10:
        score -= 3.0
    if is_wanted_ad(listing.title):
        score -= 6.0
    return score


def blind_spot(listing: Listing) -> bool:
    """"candidates" mode: only ads where the script can't see the value itself."""
    return scout_priority(listing) >= 3.0


# ---------------------------------------------------------------------------
# Throughput statistics
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    at: float  # wall clock (time.time)
    seen: int = 0  # new ads that arrived
    triaged: int = 0  # read by the model
    overflow: int = 0  # not reached (script path)
    failed: int = 0  # the model's answer unusable -> script fallback
    seconds: float = 0.0
    calls: int = 0


class ScoutStats:
    """Rolling one-hour window: how many new ads arrived and how many the model read."""

    WINDOW = 3600.0

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._events: deque[_Event] = deque()
        self.sec_per_ad: float | None = None
        self.last_error: str = ""
        self.last_error_at: float | None = None
        self.last_ok_at: float | None = None
        self.batch_size: int = 0
        self.mode: str = ""

    def _prune(self) -> None:
        cutoff = self._clock() - self.WINDOW
        while self._events and self._events[0].at < cutoff:
            self._events.popleft()

    def add(self, **counts: Any) -> None:
        self._events.append(_Event(at=self._clock(), **counts))
        self._prune()

    def _sum(self, name: str) -> float:
        self._prune()
        return sum(getattr(e, name) for e in self._events)

    @property
    def seen_last_hour(self) -> int:
        return int(self._sum("seen"))

    @property
    def triaged_last_hour(self) -> int:
        return int(self._sum("triaged"))

    @property
    def failed_last_hour(self) -> int:
        return int(self._sum("failed"))

    @property
    def overflow_last_hour(self) -> int:
        return int(self._sum("overflow"))

    def capacity_per_hour(self, share: float, max_per_hour: int) -> int | None:
        """Ads per hour the model can read at `share` of the time, capped by max_per_hour."""
        if not self.sec_per_ad:
            return None
        return int(min(max_per_hour, 3600.0 * max(0.05, share) / self.sec_per_ad))

    def snapshot(self, *, share: float, max_per_hour: int) -> dict[str, Any]:
        return {
            "seen_last_hour": self.seen_last_hour,
            "triaged_last_hour": self.triaged_last_hour,
            "overflow_last_hour": self.overflow_last_hour,
            "failed_last_hour": self.failed_last_hour,
            "sec_per_ad": round(self.sec_per_ad, 2) if self.sec_per_ad else None,
            "capacity_per_hour": self.capacity_per_hour(share, max_per_hour),
            "batch_size": self.batch_size,
            "mode": self.mode,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_ok_at": self.last_ok_at,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _read_timeout(exc: BaseException) -> bool:
    """The model server accepted the call but did not answer in time (httpx ReadTimeout behind
    the LLMError), as opposed to being down or unreachable."""
    cause = exc.__cause__
    name = type(cause).__name__ if cause is not None else ""
    return "Timeout" in name and "Connect" not in name and "Pool" not in name


@dataclass
class TriageRun:
    items: dict[str, TriageItem] = field(default_factory=dict)  # ad_id -> item (ai or script fallback)
    overflow: list[str] = field(default_factory=list)  # not reached: script path
    failed: list[str] = field(default_factory=list)  # model answer unusable: script fallback item
    calls: int = 0
    seconds: float = 0.0
    error: str = ""  # the model is down / refused: the rest of the pass goes the script path

    @property
    def ai_count(self) -> int:
        return sum(1 for item in self.items.values() if item.is_ai)


class TriageEngine:
    """Batches ads through a ChatModel (VisionLLM / ClaudeVision / a test fake)."""

    def __init__(self, llm: ChatModel, *, model: str = "", batch_size: int = 8, min_batch: int = 2,
                 max_batch: int = 16, max_tokens: int = 1800, max_per_hour: int = 600,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 target_call_seconds: float = TARGET_CALL_SECONDS):
        self.llm = llm
        self.model = model
        self.min_batch = max(1, int(min_batch))
        self.max_batch = max(self.min_batch, int(max_batch))
        # the answer must fit into max_tokens
        token_cap = max(1, (int(max_tokens) - ANSWER_OVERHEAD_TOKENS) // TOKENS_PER_ITEM)
        self.max_batch = max(1, min(self.max_batch, token_cap))
        self.min_batch = min(self.min_batch, self.max_batch)
        self.batch_size = max(self.min_batch, min(self.max_batch, int(batch_size)))
        self.max_per_hour = max(0, int(max_per_hour))
        self.clock = clock
        self.target_call_seconds = target_call_seconds
        self.stats = ScoutStats(wall)
        self.stats.batch_size = self.batch_size
        self._streak = 0
        self._done: deque[float] = deque()  # wall times of triaged ads (hourly cap)
        self._wall = wall

    @classmethod
    def from_config(cls, llm: ChatModel, cfg: Any, **kwargs: Any) -> "TriageEngine":
        """batch_size / max_batch 0 = by the model's size (batch_for_model); a set value wins."""
        model = str(getattr(cfg, "model", "") or "")
        start, cap = batch_for_model(model)
        batch = int(cfg.batch_size or 0) or start
        max_batch = int(cfg.max_batch or 0) or max(cap, batch)
        return cls(llm, model=model, batch_size=batch, min_batch=cfg.min_batch, max_batch=max_batch,
                   max_tokens=cfg.max_tokens, max_per_hour=cfg.max_per_hour,
                   target_call_seconds=min(TARGET_CALL_SECONDS, 0.6 * float(cfg.timeout_seconds)), **kwargs)

    # -- capacity ---------------------------------------------------------------

    def note_arrivals(self, n: int) -> None:
        """New ads that arrived (read or not): the M of "reads N of M new ads per hour"."""
        if n > 0:
            self.stats.add(seen=n)

    def cap_left(self) -> int | None:
        """Ads still allowed this hour (None = no cap)."""
        if self.max_per_hour <= 0:
            return None
        cutoff = self._wall() - 3600.0
        while self._done and self._done[0] < cutoff:
            self._done.popleft()
        return max(0, self.max_per_hour - len(self._done))

    def _fit_batch(self, remaining: int, deadline: float | None) -> int:
        if deadline is not None and self.clock() >= deadline:
            return 0
        size = min(self.batch_size, remaining)
        cap = self.cap_left()
        if cap is not None:
            size = min(size, cap)
        if deadline is not None and self.stats.sec_per_ad:
            left = deadline - self.clock()
            fits = int(left / self.stats.sec_per_ad)
            size = min(size, fits)
        return max(0, size)

    def _adapt(self, size: int, got: int, seconds: float) -> None:
        if size <= 0:
            return
        if got:
            per_ad = seconds / size
            self.stats.sec_per_ad = per_ad if self.stats.sec_per_ad is None else (
                EMA_ALPHA * per_ad + (1 - EMA_ALPHA) * self.stats.sec_per_ad)
        if got / size < 0.7:  # the model loses track in long batches: smaller ones
            self.batch_size = max(self.min_batch, self.batch_size // 2)
            self._streak = 0
        elif got == size:
            self._streak += 1
            if self._streak >= GROW_AFTER:
                self._streak = 0
                self.batch_size = min(self.max_batch, self.batch_size + 2)
        if self.stats.sec_per_ad:  # one call stays well inside the timeout
            by_time = int(self.target_call_seconds / self.stats.sec_per_ad)
            self.batch_size = max(self.min_batch, min(self.batch_size, max(1, by_time)))
        self.stats.batch_size = self.batch_size

    # -- calls ------------------------------------------------------------------

    async def _call(self, batch: list[Listing], categories: list[str], hints: str) -> tuple[dict[int, TriageItem], float]:
        prompt = build_user_prompt(batch, categories=categories, hints=hints)
        started = self.clock()
        answer = await self.llm.chat_json(SYSTEM_PROMPT, prompt, None, TRIAGE_SCHEMA)
        elapsed = max(0.0, self.clock() - started)
        return parse_triage(answer, len(batch), model=self.model), elapsed

    async def _run_batch(self, batch: list[Listing], categories: list[str], hints: str, run: TriageRun,
                         depth: int = 0) -> dict[str, TriageItem]:
        """One call; ads the answer missed are retried in halves (MAX_SPLIT_DEPTH levels)."""
        parsed, elapsed = await self._call(batch, categories, hints)
        run.calls += 1
        run.seconds += elapsed
        got: dict[str, TriageItem] = {}
        for idx, item in parsed.items():
            listing = batch[idx]
            got[listing.ad_id] = item.model_copy(update={"ad_id": listing.ad_id})
        if depth == 0:
            self._adapt(len(batch), len(got), elapsed)
        elif got:
            self._adapt_speed_only(len(batch), elapsed)
        missing = [i for i, listing in enumerate(batch) if listing.ad_id not in got]
        if missing and depth < MAX_SPLIT_DEPTH:
            half = max(1, math.ceil(len(missing) / 2))
            for start in range(0, len(missing), half):
                part = missing[start:start + half]
                sub = await self._run_batch([batch[i] for i in part], [categories[i] for i in part], hints, run,
                                            depth + 1)
                got.update(sub)
        return got

    def _adapt_speed_only(self, size: int, seconds: float) -> None:
        per_ad = seconds / max(1, size)
        self.stats.sec_per_ad = per_ad if self.stats.sec_per_ad is None else (
            EMA_ALPHA * per_ad + (1 - EMA_ALPHA) * self.stats.sec_per_ad)

    async def triage(self, listings: Iterable[Listing], *, deadline: float | None = None,
                     categories: dict[str, str] | None = None, hints: str = "",
                     count_overflow: bool = True) -> TriageRun:
        """Triage `listings` in the given order (put the most important first) until done,
        the `deadline` (engine clock) or the hourly cap. Ads not reached are `overflow`; ads
        the model answered badly get a script_item (in `failed`). An LLMError stops the run
        (the model is down): what is left is overflow and `error` says why. `count_overflow=False`:
        a second look at old ads — the ones not reached were counted in their own pass already."""
        queue = [listing for listing in listings]
        run = TriageRun()
        cats = categories or {}
        while queue:
            size = self._fit_batch(len(queue), deadline)
            if size <= 0:
                break
            batch, queue = queue[:size], queue[size:]
            started = self.clock()
            try:
                got = await self._run_batch(batch, [cats.get(x.ad_id, "") for x in batch], hints, run)
            except LLMError as exc:
                if _read_timeout(exc) and len(batch) > self.min_batch:
                    # the server is up but too slow for this batch (a 4B on a 4-core CPU at batch 10):
                    # remember the speed it showed at least, halve the batch and go on
                    elapsed = max(0.0, self.clock() - started)
                    run.seconds += elapsed
                    per_ad = elapsed / len(batch)
                    self.stats.sec_per_ad = max(self.stats.sec_per_ad or 0.0, per_ad) or None
                    self.batch_size = max(self.min_batch, len(batch) // 2)
                    self.stats.batch_size = self.batch_size
                    self._streak = 0
                    queue = batch + queue
                    log.warning("AI scout: %d ads took over the timeout, trying batches of %d", len(batch),
                                self.batch_size)
                    continue
                run.error = str(exc)
                self.stats.last_error = run.error
                self.stats.last_error_at = self._wall()
                queue = batch + queue
                log.warning("AI scout: model unavailable, the rest of this pass goes the usual way: %s", exc)
                break
            except Exception as exc:  # noqa: BLE001 - the scout never breaks a monitoring pass
                run.error = f"{type(exc).__name__}: {exc}"
                self.stats.last_error = run.error
                self.stats.last_error_at = self._wall()
                queue = batch + queue
                log.exception("AI scout crashed")
                break
            now = self._wall()
            for listing in batch:
                item = got.get(listing.ad_id)
                if item is None:
                    item = script_item(listing)
                    run.failed.append(listing.ad_id)
                else:
                    self._done.append(now)
                run.items[listing.ad_id] = item
            if got:
                self.stats.last_ok_at = now
        run.overflow = [listing.ad_id for listing in queue]
        self.stats.add(triaged=run.ai_count, overflow=len(run.overflow) if count_overflow else 0, failed=len(run.failed),
                       seconds=run.seconds, calls=run.calls)
        return run


# ---------------------------------------------------------------------------
# Built-in sample (Settings → Нейросеть → «Проверить разведчика»)
# ---------------------------------------------------------------------------

_SAMPLE = (
    ("scout-sample-0", "Alter Rechner vom Dachboden", 120.0, True,
     "PC von meinem Sohn, lange nicht benutzt. Drin ist eine Grafikkarte RTX 3070, i5 9600k, 16GB RAM. Nur Abholung."),
    ("scout-sample-1", "Iphne 13 128gb blau", 330.0, False, "Akku 87 %, kleine Kratzer am Rahmen, mit Hülle."),
    ("scout-sample-2", "Suche PS5 Controller", None, False, "Suche zwei DualSense Controller, zahle bis 60 €."),
    ("scout-sample-3", "Kinderwagen Bugaboo", 90.0, True, "Gebraucht, voll funktionsfähig."),
)


def sample_ads() -> list[Listing]:
    return [Listing(ad_id=ad_id, url=f"https://www.kleinanzeigen.de/s-anzeige/{ad_id}", title=title, price=price,
                    price_text=f"{price:.0f} € VB" if price is not None and vb else (f"{price:.0f} €" if price else "VB"),
                    negotiable=vb, description=text)
            for ad_id, title, price, vb, text in _SAMPLE]


def check_sample(run: TriageRun) -> dict[str, Any]:
    """How well the model read the sample: answers, the hidden GPU, the typo, the wanted ad."""
    items = run.items
    ai = {k: v for k, v in items.items() if v.is_ai}
    pc = ai.get("scout-sample-0")
    phone = ai.get("scout-sample-1")
    wanted = ai.get("scout-sample-2")
    found_gpu = pc is not None and any("3070" in c for c in pc.contents + [pc.product])
    return {
        "answered": len(ai),
        "total": len(_SAMPLE),
        "hidden_gpu": found_gpu,
        "typo_fixed": phone is not None and "iphone" in normalize(phone.product) and "13" in phone.product,
        "wanted_seen": wanted is not None and wanted.kind == "wanted",
        "items": [{"title": title, "kind": ai[ad_id].kind, "product": ai[ad_id].product,
                   "contents": ai[ad_id].contents, "interest": ai[ad_id].interest, "reason": ai[ad_id].reason}
                  for ad_id, title, *_ in _SAMPLE if ad_id in ai],
    }
