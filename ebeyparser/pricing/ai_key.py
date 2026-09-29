"""Product keys for what the AI scout identified (docs/design/AI_SCOUT.md, stage B).

The scout names products in words ("Apple iPhone 13 Pro 256GB", "NVIDIA RTX 3080 10GB").
Prices must come from real data, so every name is turned into a key of our price history:

* identity.py recognises the name -> its ProductKey (the same keys the script uses, so the AI
  and the script share one history: "rtx|3080", "iphone|13|pro|256gb");
* otherwise a normalized AI key "ai:<model words>|<sorted attributes>" ("ai:soundlink flex",
  "ai:tm5|1tb"): prices of products the regex layer doesn't know accumulate under it.

`grounded()` is the guard against a small model's inventions: a product counts only when its
model number (and a family word, typos allowed) is really written in the ad — and not in a
negated phrase ("ohne Grafikkarte", "RTX 3080 ausgebaut", "war eine 3080 drin").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .identity import ProductKey, _tokens, product_category, product_key
from .text import _BRANDS, _COLORS, _FILLER, normalize

AI_KEY_PREFIX = "ai:"
_ATTR_RE = re.compile(r"^\d+(?:\.\d+)?(?:gb|tb|mm|zoll|inch|in|w|mah|ah|v)$")
# words that describe, not identify (kept out of an AI key)
_AI_NOISE = frozenset("""
    gebraucht neu neuwertig defekt original ovp set bundle konvolut paket stueck stk version
    edition modell model generation gen farbe color colour
""".split()) | _FILLER | _COLORS
_QTY_RE = re.compile(r"^\s*(\d{1,2})\s*(?:x|×|stk\.?|stück|stueck)\s+", re.IGNORECASE)
_QTY_TAIL_RE = re.compile(r"\s+(?:x\s*(\d{1,2})|\((\d{1,2})\s*(?:x|stk\.?|stück)\))\s*$", re.IGNORECASE)


def split_quantity(text: str) -> tuple[int, str]:
    """'2x DualSense Controller' -> (2, 'DualSense Controller'); 'RTX 3070' -> (1, 'RTX 3070')."""
    text = " ".join(str(text or "").split())
    m = _QTY_RE.match(text)
    if m:
        return max(1, int(m.group(1))), text[m.end():].strip()
    m = _QTY_TAIL_RE.search(text)
    if m:
        return max(1, int(m.group(1) or m.group(2))), text[: m.start()].strip()
    return 1, text


@lru_cache(maxsize=4096)
def ai_key(product: str) -> str | None:
    """Normalized key of a product name the regex identity doesn't know: model words in order
    (brand, colours, filler dropped), attributes (storage, size, power) sorted after '|'.
    'Bose SoundLink Flex Schwarz' -> 'ai:soundlink flex'; None when nothing identifying is left."""
    toks = [t for t in _tokens(product or "") if t not in _BRANDS and t not in _AI_NOISE]
    words = [t for t in toks if not _ATTR_RE.fullmatch(t)]
    attrs = sorted({t for t in toks if _ATTR_RE.fullmatch(t)})
    if not words or (len(words) == 1 and len(words[0]) < 3 and not any(c.isdigit() for c in words[0])):
        return None
    key = AI_KEY_PREFIX + " ".join(words[:6])
    return f"{key}|{','.join(attrs)}" if attrs else key


def is_ai_key(key: str | None) -> bool:
    return bool(key) and str(key).startswith(AI_KEY_PREFIX)


def ai_keys_compatible(ref: str, other: str) -> bool:
    """Two AI keys are the same product: same model words; attributes equal when both state them."""
    ref_words, _, ref_attrs = ref.partition("|")
    other_words, _, other_attrs = other.partition("|")
    if ref_words != other_words:
        return False
    if ref_attrs and other_attrs and set(ref_attrs.split(",")) != set(other_attrs.split(",")):
        return False
    return True


@dataclass(frozen=True)
class ProductRef:
    """A product name resolved for pricing."""

    name: str  # as the AI wrote it (quantity removed)
    identity: ProductKey | None  # identity.py's key, when it recognises the name
    key: str  # full key to store prices under ("iphone|13|pro|256gb" / "ai:soundlink flex")
    lookup: str  # prefix to read prices with (identity: coarse key; AI: the key)
    query: str  # search phrase for comparables
    category: str | None = None

    @property
    def is_ai(self) -> bool:
        return self.identity is None


# identity's catch-all matcher turns any "word + number" into a key ("iphne|13" for a typo):
# those are no better than an AI key, so the AI's own spelling wins there
_WEAK_CATEGORIES = frozenset({None, "other"})


def resolve(product: str) -> ProductRef | None:
    """identity key when identity.py recognises the name (not only by its catch-all rule),
    else the normalized AI key; None for an empty / unidentifiable name."""
    qty, name = split_quantity(product)
    name = name.strip(" .,;:-")
    if not name:
        return None
    try:
        key = product_key(name)
        category = product_category(name)
    except Exception:  # noqa: BLE001 - identity must never break the scout
        key, category = None, None
    if key is not None and category not in _WEAK_CATEGORIES:
        return ProductRef(name, key, key.key(), key.coarse_key(), key.query() or normalize(name), category)
    akey = ai_key(name)
    if akey is None:
        return None
    return ProductRef(name, None, akey, akey.partition("|")[0], _query_words(name), category)


def _query_words(name: str) -> str:
    toks = [t for t in normalize(name).split() if t not in _AI_NOISE]
    return " ".join(toks[:5])


# ---------------------------------------------------------------------------
# Grounding: is the product really written in the ad?
# ---------------------------------------------------------------------------

_NEG_BEFORE = frozenset("ohne kein keine keinen keiner nicht fehlt fehlen fehlende ausgebaut entfernt war".split())
_NEG_AFTER = frozenset("fehlt fehlen ausgebaut entfernt verkauft nicht defekt kaputt drin verbaut".split())
_NEG_AFTER_EXCUSE = frozenset({"drin", "verbaut"})  # "war ... drin": only with "war" before
_GENERIC_FAMILY = frozenset({"pc", "computer", "rechner", "gaming", "set", "controller", "konsole",
                             "grafikkarte", "handy", "smartphone", "laptop", "notebook", "ram"})


def _edit1(a: str, b: str) -> bool:
    """Levenshtein distance <= 1 (one typo), or a transposition ("playstaion")."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if la > lb:
        a, b = b, a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
        elif skipped:
            return False
        else:
            skipped = True
        j += 1
    return True


def _fuzzy_in(word: str, tokens: tuple[str, ...]) -> list[int]:
    """Positions of `word` in `tokens`: exact, glued prefix ("rtx3080" was split already),
    or one typo for words of 5+ letters ("iphne", "samsnug"). Model numbers must be exact."""
    has_digit = any(c.isdigit() for c in word)
    out = []
    for i, tok in enumerate(tokens):
        if tok == word:
            out.append(i)
        elif not has_digit and len(word) >= 5 and _edit1(word, tok):
            out.append(i)
        elif not has_digit and len(word) >= 6 and len(tok) >= 5 and (tok.startswith(word[:5]) and _edit1(word[:len(tok)], tok)):
            out.append(i)
    return out


def _negated_at(tokens: tuple[str, ...], pos: int) -> bool:
    before = tokens[max(0, pos - 4):pos]
    after = tokens[pos + 1:pos + 4]
    if any(t in _NEG_BEFORE and t != "war" for t in before):
        return True
    was = "war" in before or "waren" in before
    for t in after:
        if t in _NEG_AFTER_EXCUSE:
            if was:
                return True
        elif t in _NEG_AFTER:
            return True
    return False


_CLAUSE_RE = re.compile(r"[,;:!?\n|()\[\]/+]+|\.(?!\d)|\s[-–—]\s")


def _clauses(text: str) -> list[tuple[str, ...]]:
    """Token lists per clause: a negation never reaches across a comma ("PC ohne Monitor, RTX 3080")."""
    return [toks for part in _CLAUSE_RE.split(text or "") if (toks := _tokens(part))]


def _present(word: str, clauses: list[tuple[str, ...]]) -> bool:
    """`word` appears (see _fuzzy_in) somewhere not negated."""
    variants = [word]
    core = re.sub(r"^[a-z]{1,3}(?=\d)", "", word)  # "i7-8700k" is often written "8700k"
    if core and core != word and any(c.isdigit() for c in word):
        variants.append(core)
    for variant in variants:
        for toks in clauses:
            if any(not _negated_at(toks, p) for p in _fuzzy_in(variant, toks)):
                return True
    return False


def _weak_model(tok: str) -> bool:
    """'7', '13', '5', 'i7', 'r5': a short token names nothing without its product line
    (and "i7" is only the series of an "8700k")."""
    return len(tok) <= 2


def grounded(product: str, text: str) -> bool:
    """Is `product` actually named in `text` (title + description)? Every model-number token of
    the product must appear, not negated; a bare small number ("iPhone 7") also needs its
    product line; without model numbers, the most specific word must appear (one typo allowed
    in long words). Brand words and storage sizes are not required."""
    name = split_quantity(product)[1]
    ptoks = [t for t in _tokens(name) if t not in _BRANDS and t not in _AI_NOISE]
    if not ptoks:
        return False
    clauses = _clauses(text)
    if not clauses:
        return False
    models = [t for t in ptoks if any(c.isdigit() for c in t) and not _ATTR_RE.fullmatch(t)]
    words = [t for t in ptoks if not any(c.isdigit() for c in t) and t not in _GENERIC_FAMILY
             and not _ATTR_RE.fullmatch(t)]
    if models:
        strong = [t for t in models if not _weak_model(t)]
        if not all(_present(tok, clauses) for tok in strong or models):
            return False
        if not strong and words:
            return any(_present(w, clauses) for w in words)
        return True
    if not words:
        return False
    return _present(max(words, key=len), clauses)
