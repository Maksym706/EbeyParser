"""Product identity for second-hand listings.

Two questions are answered for every ad title, without any I/O:

* *Which exact product is this?* -> :func:`product_key` returns a :class:`ProductKey`
  (family, model, variant suffixes, storage, memory, screen/case size) or ``None`` for
  generic titles ("iPhone", "Handy", "Fahrrad 28 Zoll").
* *What kind of offer is it?* -> :func:`classify_kind` returns a :data:`Kind`
  ("item", "part", "accessory", "complete_pc", "laptop", "box_only", "defect", ...).

:func:`comparable_matches` combines both into the comparables filter,
:func:`missing_parts` lists components the ad says are missing ("ohne Akku", "nur Tablet"),
and :func:`identify` / :func:`history_key` bundle everything for the pricing engine
(history key that never files a cooler, a gaming PC or a box under the GPU's key).

Core rules
----------
* Model numbers match exactly: "s2" != "s23", "5600" != "5600x", "3080" != "3080 ti".
  Glued forms are split first ("rtx3080ti" -> "rtx 3080 ti", "iphone13pro" -> "iphone 13 pro").
* Edition suffixes (ti, super, xt, pro, max, plus, ultra, mini, lite, slim, digital, ...)
  must match as a set: "pro" != "pro max", "ti" != "ti super".
* Ignored for price: brand words, colours, word order, "5g"/"lte", "lhr", GPU "FE"
  (Founders Edition is the reference card, priced like partner cards), "OC", "Edition".
  Samsung "FE" (Fan Edition) *is* a different phone and is kept.
* Storage (and RAM for Macs, case size for watches, screen size for MacBooks/iPad Pro)
  must be equal when both titles state it; a title that does not state it matches any.
* GPU VRAM is part of the identity only for cards sold with several sizes
  (RTX 3080 10/12 GB, RTX 3060 8/12 GB, GTX 1060 3/6 GB, ...); the common size is the
  default and is not written into the key.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from .text import normalize

Kind = Literal[
    "item", "bundle", "complete_pc", "laptop", "part", "accessory", "box_only", "defect",
    "wanted", "swap", "service", "unknown",
]
KINDS: tuple[str, ...] = (
    "item", "bundle", "complete_pc", "laptop", "part", "accessory", "box_only", "defect",
    "wanted", "swap", "service", "unknown",
)

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# "S21+" / "iPhone 8+" -> "s21 plus"; any other "+" / "&" is a connector ("PS5 + 2 Controller")
_PLUS_GLUED_RE = re.compile(r"(?<=[0-9A-Za-z])\+(?=$|[\s,;/)!.])")
# "8/256GB", "12 GB / 512 GB", "8/1TB" on phones: RAM / storage
_RAM_SLASH_RE = re.compile(
    r"\b([2-9]|1[0-9]|2[0-4])\s*(?:gb)?\s*/\s*(32|64|128|256|512)\s*(?:gb)?\b"
    r"|\b([2-9]|1[0-9]|2[0-4])\s*(?:gb)?\s*/\s*([124])\s*tb\b",
    re.IGNORECASE,
)
# glued family + number: "iphone13pro", "rtx3080ti", "fold5", "qc45", "airpods2"
_GLUE_PREFIX_RE = re.compile(
    r"^(iphone|ipad|galaxy|pixel|rtx|gtx|gt|rx|arc|radeon|geforce|ryzen|xbox|switch|note|fold|flip|"
    r"tab|buds|airpods|macbook|thinkpad|xps|qc|mavic|avata|quest|hero|eos|legion|nitro|playstation|"
    r"series|watch|mini|air|pocket|action)(\d+[a-z]*)$"
)
_SUFFIX_ALT = r"ti|super|xtx|xt|gre|pro|max|plus|ultra|mini|lite|fe|slim|oled|digital"
# "3080ti" -> "3080 ti", "13promax" -> "13 pro max"
_NUM_SUFFIX_RE = re.compile(rf"^(\d+)((?:{_SUFFIX_ALT})+)$")
# "s21ultra" -> "s21 ultra", "m1pro" -> "m1 pro"
_ALNUM_SUFFIX_RE = re.compile(r"^([a-z]{1,2}\d{1,3})((?:ultra|plus|pro|max|mini|lite|fe|ti|super)+)$")
_SUFFIX_SPLIT_RE = re.compile(_SUFFIX_ALT)
_PS_GLUED_RE = re.compile(r"^(ps[1-5])(slim|pro|digital|vr)$")
# "9. Generation", "2nd gen", "gen 2" -> "gen9" / "gen2"
_GEN_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th|te|ter|ten)? ?(?:gen|generation|generaton|genaration)\b"
    r"|\b(?:gen|generation) ?(\d{1,2})\b"
)
# "Mark IV", "Mk II", "mkii", "mk 2" -> "mk4" / "mk2"
_MARK_RE = re.compile(r"\b(?:mark|mk)\s?(ii|iii|iv|v|[2-5])\b")
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7"}


def _pre(text: str) -> str:
    """Raw-text fixes that must happen before normalize() drops the punctuation."""
    t = unicodedata.normalize("NFKC", text)
    t = _RAM_SLASH_RE.sub(
        lambda m: f" {m.group(1)}gb ram {m.group(2)}gb " if m.group(1)
        else f" {m.group(3)}gb ram {m.group(4)}tb ", t)
    t = _PLUS_GLUED_RE.sub(" plus ", t)
    t = t.replace("+", " und ").replace("&", " und ")
    t = t.replace("α", "alpha ")  # Sony "α7 III"
    return t


def _split_token(tok: str) -> list[str]:
    m = _PS_GLUED_RE.match(tok)
    if m:
        return [m.group(1), m.group(2)]
    if tok.startswith("psvr"):
        return ["ps", "vr"] + ([tok[4:]] if tok[4:] else [])
    m = _GLUE_PREFIX_RE.match(tok)
    if m:
        return [m.group(1)] + _split_token(m.group(2))
    m = _NUM_SUFFIX_RE.match(tok)
    if m:
        return [m.group(1)] + _SUFFIX_SPLIT_RE.findall(m.group(2))
    m = _ALNUM_SUFFIX_RE.match(tok)
    if m:
        return [m.group(1)] + _SUFFIX_SPLIT_RE.findall(m.group(2))
    return [tok]


@lru_cache(maxsize=8192)
def _tokens(text: str) -> tuple[str, ...]:
    """Normalized, glue-split tokens: 'Apple iPhone13 Pro+Hülle' -> apple iphone 13 pro und huelle."""
    if not text:
        return ()
    s = normalize(_pre(text))
    out: list[str] = []
    for tok in s.split():
        out.extend(_split_token(tok))
    s = " ".join(out)
    s = _GEN_RE.sub(lambda m: f"gen{m.group(1) or m.group(2)}", s)
    s = _MARK_RE.sub(lambda m: f"mk{_ROMAN.get(m.group(1), m.group(1))}", s)
    return tuple(s.split())


def _tok_index(s: str, pos: int) -> int:
    """Token index of character offset `pos` in a space-joined token string."""
    return s.count(" ", 0, pos)


# ---------------------------------------------------------------------------
# ProductKey
# ---------------------------------------------------------------------------

# canonical order of variant words so that "Max Pro" and "Pro Max" give the same key
_VARIANT_ORDER = (
    "evo", "qvo", "pro", "max", "plus", "ultra", "mini", "lite", "e", "se", "fe", "xl", "air", "ti", "super", "gre",
    "xt", "xtx", "slim", "digital", "new", "classic", "active", "carbon", "yoga", "extreme", "nano",
    "tablet", "titanium", "anc", "ecc", "sodimm", "cine", "combo", "kit", "solo",
)
_VARIANT_RANK = {w: i for i, w in enumerate(_VARIANT_ORDER)}


_GEN_RANK = _VARIANT_RANK["anc"] - 0.5  # "gen2"/"mk3" after edition words, before kit/solo


def _variant_rank(w: str) -> float:
    if w in _VARIANT_RANK:
        return _VARIANT_RANK[w]
    if re.fullmatch(r"(?:gen|mk)\d+", w):
        return _GEN_RANK
    return len(_VARIANT_RANK)


def _canon_variant(words: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    uniq = list(dict.fromkeys(w for w in words if w))
    return tuple(sorted(uniq, key=lambda w: (_variant_rank(w), w)))


@dataclass(frozen=True)
class ProductKey:
    """Identity of a product, independent of brand words, colours and word order.

    family   product line: "iphone", "rtx", "galaxy s", "playstation", "macbook air", ...
    model    exact model token(s): "13", "3080", "s21", "5", "m1", "t480", "5600x"
    variant  canonical, sorted edition suffixes: ("pro", "max"), ("ti", "super"), ("digital",)
    capacity storage "128gb" / "1tb" when stated (phones, tablets, consoles, Macs, SSDs)
    memory   RAM (Macs) or non-default VRAM (GPUs) when it defines the variant
    size     screen/case size when it defines the variant: "45mm", "14in", "12.9in"
    """

    family: str
    model: str
    variant: tuple[str, ...] = ()
    capacity: str | None = None
    memory: str | None = None
    size: str | None = None

    def key(self) -> str:
        """Stable string, e.g. 'iphone|13|pro max|128gb' (trailing empty fields dropped)."""
        parts = [self.family, self.model, " ".join(self.variant), self.capacity or "",
                 self.memory or "", self.size or ""]
        return "|".join(parts).rstrip("|")

    def coarse_key(self) -> str:
        """Fallback bucket without capacity/memory/size: 'iphone|13|pro max'."""
        return "|".join([self.family, self.model, " ".join(self.variant)]).rstrip("|")

    def query(self) -> str:
        """Short search phrase for finding comparables: 'iphone 13 pro max 128gb', 'ps5 digital',
        'sony a7 iii', 'airpods pro 2'. Use it for the marketplace search, then filter the
        results with comparable_matches()."""
        fam, model = _QUERY_FAMILY.get(self.family, self.family), self.model
        if self.family == "playstation":
            fam, model = f"ps{self.model}", ""
        elif self.family == "apple watch" and model.isdigit():
            model = f"series {model}"
        elif self.family.startswith("sony w") and self.family.endswith("-1000x"):
            fam, model = f"sony {self.family[5:7]}-1000{model}", ""
        words = [fam, model]
        for v in self.variant:
            if v.startswith("gen") and v[3:].isdigit():
                words.append(f"gen {v[3:]}" if self.family in ("thinkpad", "hp elitebook", "hp probook") else v[3:])
            elif v.startswith("mk") and v[2:].isdigit():
                words.append(_ROMAN_OF.get(v[2:], v[2:]))
            else:
                words.append(v)
        if self.capacity and self.family not in _NO_QUERY_CAPACITY:
            words.append(self.capacity)
        if self.memory and self.family in _GPU_FAMILIES:
            words.append(self.memory)
        return " ".join(w for w in words if w and w not in ("standard", "lcd"))


_QUERY_FAMILY = {"galaxy s": "galaxy", "galaxy a": "galaxy", "galaxy m": "galaxy", "sony alpha": "sony",
                 "ram ddr3": "ddr3", "ram ddr4": "ddr4", "ram ddr5": "ddr5"}
_ROMAN_OF = {"2": "ii", "3": "iii", "4": "iv", "5": "v"}
_NO_QUERY_CAPACITY = frozenset({"playstation", "xbox"})
_GPU_FAMILIES = frozenset({"rtx", "gtx", "gt", "rx", "rx vega", "arc"})


# ---------------------------------------------------------------------------
# Capacity / memory / size helpers
# ---------------------------------------------------------------------------

_GB_TOKEN_RE = re.compile(r"^(\d+(?:\.\d+)?)(gb|tb)$")
_KIT_TOKEN_RE = re.compile(r"^([1-8])x(\d{1,3})gb$")  # RAM kits "2x8gb"
_VRAM_G_RE = re.compile(r"^(\d{1,2})g$")  # MSI style "12G"
_RAM_WORDS = frozenset({"ram", "arbeitsspeicher", "memory", "ddr3", "ddr4", "ddr5", "lpddr4", "lpddr5",
                        "lpddr4x", "unified", "hauptspeicher"})
_STORAGE_WORDS = frozenset({"ssd", "speicher", "storage", "rom", "hdd", "festplatte", "interner",
                            "speicherplatz", "nvme", "flash"})
_VRAM_WORDS = frozenset({"gddr5", "gddr5x", "gddr6", "gddr6x", "gddr7", "vram", "grafikspeicher", "hbm2"})


def _gb_value(tok: str) -> float | None:
    m = _GB_TOKEN_RE.match(tok)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1024 if m.group(2) == "tb" else v


def _fmt_gb(v: float) -> str:
    if v in (1000, 2000, 4000, 8000):  # "1000gb" SSD marketing = 1 TB
        v = v / 1000 * 1024
    if v >= 1024 and v % 1024 == 0:
        return f"{int(v // 1024)}tb"
    if v >= 1024 and (v / 1024) % 0.5 == 0:
        return f"{v / 1024:g}tb"
    return f"{v:g}gb"


def _sizes(toks: tuple[str, ...]) -> list[tuple[int, float, str]]:
    """All GB/TB amounts with a label: 'ram', 'storage', 'vram' or '' (unknown)."""
    out: list[tuple[int, float, str]] = []
    for i, tok in enumerate(toks):
        kit = _KIT_TOKEN_RE.match(tok)
        if kit:
            out.append((i, float(int(kit.group(1)) * int(kit.group(2))), "ram"))
            continue
        v = _gb_value(tok)
        if v is None:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prv = toks[i - 1] if i else ""
        label = ""
        if nxt in _VRAM_WORDS:
            label = "vram"
        elif nxt in _RAM_WORDS or (prv in ("ram", "arbeitsspeicher") and not (i > 1 and _gb_value(toks[i - 2]))):
            label = "ram"
        elif nxt in _STORAGE_WORDS or prv in ("ssd", "speicher", "hdd", "nvme"):
            label = "storage"
        out.append((i, v, label))
    return out


def _storage(toks: tuple[str, ...], min_gb: float) -> str | None:
    """Largest stated storage (labelled, or unlabelled and >= min_gb); RAM-labelled amounts skipped."""
    vals = [v for _, v, lab in _sizes(toks) if lab == "storage" or (lab == "" and v >= min_gb)]
    return _fmt_gb(max(vals)) if vals else None


def _mac_memory(toks: tuple[str, ...]) -> tuple[str | None, str | None]:
    """(RAM, SSD) for Macs: RAM <= 64 GB unless labelled storage; SSD >= 128 GB."""
    ram: str | None = None
    ssd: float | None = None
    for _, v, lab in _sizes(toks):
        if lab == "ram" or (lab == "" and v <= 64 and ram is None):
            if ram is None and v <= 192:
                ram = _fmt_gb(v)
        elif lab == "storage" or v >= 128:
            ssd = max(ssd or 0, v)
    return ram, (_fmt_gb(ssd) if ssd else None)


def _inch(tok: str) -> float | None:
    m = re.match(r"^(\d{1,2}(?:\.\d)?)(?:zoll|inch|in)?$", tok)
    return float(m.group(1)) if m else None


def _size_label(v: float) -> str:
    return f"{v:g}in"


# ---------------------------------------------------------------------------
# Family matchers
# ---------------------------------------------------------------------------
# Each matcher looks at the space-joined token string `s` (and the token tuple) and returns
# a _Hit: the ProductKey (None = family recognised but no model -> no identity), the token
# index where the product is mentioned (for kind detection) and a category.


@dataclass(frozen=True)
class _Hit:
    key: ProductKey | None
    start: int
    category: str  # phone tablet watch audio laptop desktop console handheld gpu cpu ram storage
    #                camera drone vacuum tool other


# words that end the product clause: what follows is extras ("PS5 mit 2 Controllern")
_CONNECTORS = frozenset({"und", "mit", "inkl", "inklusive", "incl", "sowie", "samt", "zzgl", "plus_",
                         "nebst", "zusammen", "dazu", "fuer", "for", "passend", "kompatibel", "ohne"})
_IGNORE_AFTER_MODEL = frozenset({"5g", "4g", "lte", "wifi", "wlan", "cellular", "dual", "sim", "dualsim",
                                 "edition", "version", "modell", "model", "neu", "neuwertig", "oc"})


def _collect(toks: tuple[str, ...], i: int, allowed: frozenset[str] | set[str],
             ignore: frozenset[str] = _IGNORE_AFTER_MODEL, limit: int = 5) -> tuple[str, ...]:
    """Variant words right after the model token (skipping ignorable ones)."""
    out: list[str] = []
    while i < len(toks) and limit > 0:
        t = toks[i]
        if t in allowed:
            out.append(t)
        elif t not in ignore:
            break
        i += 1
        limit -= 1
    return _canon_variant(out)


def _clause(toks: tuple[str, ...], start: int, limit: int = 8) -> tuple[str, ...]:
    """Tokens from `start` up to the first connector ("mit", "und", "für", ...)."""
    out: list[str] = []
    for t in toks[start:start + limit]:
        if t in _CONNECTORS:
            break
        out.append(t)
    return tuple(out)


def _pre_variant(toks: tuple[str, ...], start: int, allowed: frozenset[str] | set[str]) -> list[str]:
    """Variant words written before the family word, in the same clause ("Pro Max iPhone 13")."""
    out: list[str] = []
    for t in reversed(toks[:start]):
        if t in _CONNECTORS:
            break
        if t in allowed:
            out.append(t)
    return out


def _gen_of(tok: str) -> str | None:
    """'gen2' / '2' / 'ii' -> '2'."""
    if tok.startswith("gen") and tok[3:].isdigit():
        return tok[3:]
    if tok.isdigit() and len(tok) <= 2:
        return tok
    return _ROMAN.get(tok)


_YEAR_RE = re.compile(r"^(20[012]\d)$")

# --- Apple ---------------------------------------------------------------

_IPHONE_RE = re.compile(r"\biphone ?(\d{1,2}e?|se|xs|xr|x)\b")
_SE_YEARS = {"2016": "1", "2020": "2", "2022": "3"}


def _m_iphone(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if "iphone" not in toks:
        return None
    m = _IPHONE_RE.search(s)
    start = toks.index("iphone")
    if not m:
        return _Hit(None, start, "phone")
    i = _tok_index(s, m.start(1))
    model = m.group(1)
    variant = _canon_variant(list(_collect(toks, i + 1, {"pro", "max", "plus", "mini"}))
                             + _pre_variant(toks, start, {"pro", "max", "plus", "mini"}))
    if model == "se":  # SE generations: 2016 / 2020 / 2022
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        gen = _SE_YEARS.get(nxt) or (_gen_of(nxt) if nxt[:3] == "gen" or nxt in ("1", "2", "3") else None)
        variant = (f"gen{gen}",) if gen and gen != "1" else ()
    cap = _storage(toks, 16)
    return _Hit(ProductKey("iphone", model, variant, cap), start, "phone")


_IPAD_RE = re.compile(r"\bipad(?: (air|pro|mini))?\b")
_IPAD_YEARS: dict[str, dict[str, str]] = {
    "ipad": {"gen5": "2017", "gen6": "2018", "gen7": "2019", "gen8": "2020", "gen9": "2021",
             "gen10": "2022", "gen11": "2025"},
    "ipad air": {"gen3": "2019", "gen4": "2020", "gen5": "2022", "m1": "2022", "gen6": "2024",
                 "m2": "2024", "gen7": "2025", "m3": "2025"},
    "ipad mini": {"gen5": "2019", "gen6": "2021", "gen7": "2024"},
    "ipad pro": {"m1": "2021", "m2": "2022", "m4": "2024", "m5": "2025"},
    "ipad pro 11": {"gen1": "2018", "gen2": "2020", "gen3": "2021", "gen4": "2022", "gen5": "2024"},
    "ipad pro 12.9": {"gen3": "2018", "gen4": "2020", "gen5": "2021", "gen6": "2022"},
}
_IPAD_PRO_SIZES = {9.7, 10.5, 11.0, 12.9, 13.0}


def _m_ipad(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _IPAD_RE.search(s)
    if not m:
        return None
    family = f"ipad {m.group(1)}" if m.group(1) else "ipad"
    start = _tok_index(s, m.start())
    after = _tok_index(s, m.end()) + 1
    chip = gen = year = None
    size: float | None = None
    for j, t in enumerate(_clause(toks, after, 8)):
        if re.fullmatch(r"m[1-5]", t):
            chip = chip or t
        elif re.fullmatch(r"gen\d{1,2}", t):
            gen = gen or t
        elif _YEAR_RE.match(t):
            year = year or t
        elif not t.isdigit() and _inch(t) is not None:  # "12.9", "11zoll", "12.9zoll"
            size = size or _inch(t)
        elif t.isdigit():
            n = int(t)
            if family in ("ipad pro", "ipad air") and float(n) in _IPAD_PRO_SIZES:
                size = size or float(n)
            elif j == 0 and 1 <= n <= 11:
                gen = gen or f"gen{n}"
    if family == "ipad air" and size not in (11.0, 13.0):
        size = None  # Air 1-5 come in one size only
    if family not in ("ipad pro", "ipad air"):
        size = None
    table = dict(_IPAD_YEARS.get(family, {}))
    if family == "ipad pro" and size is not None:
        table.update(_IPAD_YEARS.get(f"ipad pro {size:g}", {}))
    model = None
    for cand in (chip, gen):
        if cand and cand in table:
            model = table[cand]
            break
    model = model or year or chip or gen
    if model is None:
        return _Hit(None, start, "tablet")
    key = ProductKey(family, model, (), _storage(toks, 16), None, _size_label(size) if size else None)
    return _Hit(key, start, "tablet")


_MACBOOK_RE = re.compile(r"\bmac ?book(?: (air|pro))?\b")
_MAC_DESKTOP_RE = re.compile(r"\b(mac ?mini|imac|mac studio|mac pro)\b")
_CHIP_RE = re.compile(r"\bm([1-5])(?: (pro|max|ultra))?\b")
_APPLE_A_NO_RE = re.compile(r"\b(a[12]\d{3})\b")
_MAC_SIZES = {12.0: 12, 13.0: 13, 13.3: 13, 13.6: 13, 14.0: 14, 14.2: 14, 15.0: 15, 15.3: 15, 15.4: 15,
              16.0: 16, 16.2: 16, 17.0: 17}
_IMAC_SIZES = {21.5: 21.5, 24.0: 24, 27.0: 27}


def _m_mac(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _MACBOOK_RE.search(s)
    if m:
        family = f"macbook {m.group(1)}" if m.group(1) else "macbook"
        category, sizes = "laptop", _MAC_SIZES
    else:
        m = _MAC_DESKTOP_RE.search(s)
        if not m:
            return None
        family = m.group(1).replace("macmini", "mac mini")
        category, sizes = "desktop", (_IMAC_SIZES if family == "imac" else {})
    start = _tok_index(s, m.start())
    chip = _CHIP_RE.search(s, m.end())
    variant: tuple[str, ...] = ()
    if chip:
        model = f"m{chip.group(1)}"
        variant = (chip.group(2),) if chip.group(2) else ()
    else:
        year = next((t for t in toks if _YEAR_RE.match(t) and 2006 <= int(t) <= 2023), None)
        a_no = _APPLE_A_NO_RE.search(s)
        model = year or (a_no.group(1) if a_no else None)
    if model is None:
        return _Hit(None, start, category)
    size = None
    for t in toks[start + 1:]:
        v = _inch(t)
        if v is not None and v in sizes and not _YEAR_RE.match(t):
            size = sizes[v]
            break
    ram, ssd = _mac_memory(toks)
    key = ProductKey(family, model, variant, ssd, ram, _size_label(size) if size else None)
    return _Hit(key, start, category)


_AWATCH_RE = re.compile(r"\b(?:apple ?watch|iwatch|watch series)\b")
_WATCH_MM_RE = re.compile(r"\b(38|40|41|42|44|45|46|49)mm\b")
_AW_SE_YEARS = {"2020": "1", "2022": "2", "2025": "3"}


def _m_apple_watch(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _AWATCH_RE.search(s)
    if not m or re.search(r"\b(?:galaxy|pixel|huawei|samsung) watch\b", s):
        return None
    start = _tok_index(s, m.start())
    rest = _clause(toks, _tok_index(s, m.end()) + 1, 6)
    if m.group(0) == "watch series":
        rest = ("series",) + rest
    model: str | None = None
    variant: tuple[str, ...] = ()
    for j, t in enumerate(rest):
        nxt = rest[j + 1] if j + 1 < len(rest) else ""
        if t == "series" and nxt.isdigit():
            model = nxt
        elif re.fullmatch(r"s\d{1,2}", t):
            model = t[1:]
        elif t in ("se", "ultra"):
            model = t
            gen = _AW_SE_YEARS.get(nxt) if t == "se" else None
            gen = gen or (_gen_of(nxt) if nxt and (nxt.isdigit() or nxt.startswith("gen")) else None)
            variant = (f"gen{gen}",) if gen and gen != "1" else ()
        elif t.isdigit() and 1 <= int(t) <= 11 and j == 0:
            model = t
        else:
            continue
        break
    if model is None:
        return _Hit(None, start, "watch")
    mm = _WATCH_MM_RE.search(s)
    return _Hit(ProductKey("apple watch", model, variant, None, None, f"{mm.group(1)}mm" if mm else None),
                start, "watch")


_AIRPODS_RE = re.compile(r"\bair ?pods?\b")


def _m_airpods(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _AIRPODS_RE.search(s)
    if not m:
        return None
    start = _tok_index(s, m.start())
    rest = toks[_tok_index(s, m.end()) + 1:][:3]
    t1 = rest[0] if rest else ""
    t2 = rest[1] if len(rest) > 1 else ""
    model: str | None = None
    variant: list[str] = []
    if t1 in ("pro", "max"):
        model = t1
        gen = _gen_of(t2) if t2 and (t2.isdigit() or t2.startswith("gen") or t2 in _ROMAN) else None
        if gen and gen != "1":
            variant.append(f"gen{gen}")
    elif t1 and (t1.isdigit() or t1.startswith("gen")) and (gen := _gen_of(t1)):
        if t2 == "pro":
            model = "pro"
            if gen != "1":
                variant.append(f"gen{gen}")
        else:
            model = gen
    if model is None:
        return _Hit(None, start, "audio")
    if model == "4" and ("anc" in toks or "geraeuschunterdrueckung" in toks):
        variant.append("anc")
    return _Hit(ProductKey("airpods", model, _canon_variant(variant)), start, "audio")


# --- Samsung / Google ----------------------------------------------------

_SAMSUNG_BRAND = frozenset({"galaxy", "samsung"})
_FOLD_RE = re.compile(r"\b(?:galaxy |samsung |z )(?:z )?(fold|flip)(?: (\d))?\b")
_TAB_RE = re.compile(r"\btab (s\d{1,2}|a\d{0,2}|active ?\d?)\b")
_GWATCH_RE = re.compile(r"\bgalaxy watch\b")
_BUDS_RE = re.compile(r"\bgalaxy buds\b")
_NOTE_RE = re.compile(r"\bnote ?(\d{1,2})\b")
_GBOOK_RE = re.compile(r"\bgalaxy book ?(\d)?\b")
_GS_RE = re.compile(r"\b(s\d{1,2}e?)\b")
_GS_NOBRAND_RE = re.compile(r"\b(s(?:[1-2]\d))(?= (?:ultra|plus|fe)\b)")
_GA_RE = re.compile(r"\b([am]\d{2}s?)\b")
_XCOVER_RE = re.compile(r"\bxcover ?(\d)\b")
_PHONE_SUFFIX = frozenset({"ultra", "plus", "fe", "lite", "pro", "classic"})


def _m_samsung(s: str, toks: tuple[str, ...]) -> _Hit | None:
    branded = bool(_SAMSUNG_BRAND.intersection(toks))
    m = _FOLD_RE.search(s)
    if m:
        start = _tok_index(s, m.start())
        i = _tok_index(s, m.start(2) if m.group(2) else m.start(1))
        variant = _collect(toks, i + 1, {"fe", "ultra"})
        return _Hit(ProductKey(f"galaxy z {m.group(1)}", m.group(2) or "1", variant, _storage(toks, 64)),
                    start, "phone")
    if not branded:
        m = _GS_NOBRAND_RE.search(s)
        if not m:
            return None
        i = _tok_index(s, m.start(1))
        return _Hit(ProductKey("galaxy s", m.group(1), _collect(toks, i + 1, _PHONE_SUFFIX - {"pro", "classic"}),
                               _storage(toks, 64)), i, "phone")
    brand_i = next(i for i, t in enumerate(toks) if t in _SAMSUNG_BRAND)
    if (m := _GWATCH_RE.search(s)):
        start = _tok_index(s, m.start())
        rest = _clause(toks, start + 2, 5)
        num = next((t for t in rest if t.isdigit() and len(t) == 1), None)
        words = [t for t in rest if t in ("classic", "pro", "fe", "active", "ultra")]
        model = num or ("ultra" if "ultra" in words else None)
        if model is None:
            return _Hit(None, start, "watch")
        mm = _WATCH_MM_RE.search(s)
        return _Hit(ProductKey("galaxy watch", model, _canon_variant([w for w in words if w != model]),
                               None, None, f"{mm.group(1)}mm" if mm else None), start, "watch")
    if (m := _BUDS_RE.search(s)):
        start = _tok_index(s, m.start())
        rest = _clause(toks, start + 2, 3)
        if not rest:
            return _Hit(None, start, "audio")
        if rest[0].isdigit() and len(rest[0]) == 1:
            model, variant = rest[0], _collect(rest, 1, {"pro", "fe"})
        elif rest[0] in ("pro", "live", "plus", "fe", "core"):
            model, variant = rest[0], ()
        else:
            return _Hit(None, start, "audio")
        return _Hit(ProductKey("galaxy buds", model, variant), start, "audio")
    if (m := _GBOOK_RE.search(s)):
        start = _tok_index(s, m.start())
        if not m.group(1):
            return _Hit(None, start, "laptop")
        variant = _collect(toks, _tok_index(s, m.start(1)) + 1, {"pro", "ultra", "360"})
        return _Hit(ProductKey("galaxy book", m.group(1), variant), start, "laptop")
    if (m := _TAB_RE.search(s)):
        i = _tok_index(s, m.start(1))
        variant = _collect(toks, i + 1, {"ultra", "plus", "fe", "lite"})
        model = m.group(1).replace(" ", "")
        return _Hit(ProductKey("galaxy tab", model, variant, _storage(toks, 16)), _tok_index(s, m.start()),
                    "tablet")
    for rx, family in ((_NOTE_RE, "galaxy note"), (_XCOVER_RE, "galaxy xcover"), (_GS_RE, "galaxy s"),
                       (_GA_RE, "galaxy a")):
        m = rx.search(s)
        if m:
            i = _tok_index(s, m.start(1))
            model = m.group(1)
            if family == "galaxy a" and model.startswith("m"):
                family = "galaxy m"
            variant = _collect(toks, i + 1, _PHONE_SUFFIX - {"classic"} if family != "galaxy s"
                               else _PHONE_SUFFIX - {"classic", "pro"})
            return _Hit(ProductKey(family, model, variant, _storage(toks, 16)), min(brand_i, i), "phone")
    if "galaxy" in toks:
        return _Hit(None, brand_i, "phone")
    return None


_PIXEL_RE = re.compile(r"\bpixel (\d{1,2}a?|fold)\b")
_PIXEL_SUB_RE = re.compile(r"\bpixel (watch|buds|tablet)\b(?: (\d|pro|a))?")


def _m_pixel(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if "pixel" not in toks:
        return None
    s2 = re.sub(r"\bpixel (\d{1,2}) a\b", r"pixel \1a", s)
    m = _PIXEL_SUB_RE.search(s2)
    if m:
        start = _tok_index(s2, m.start())
        cat = {"watch": "watch", "buds": "audio", "tablet": "tablet"}[m.group(1)]
        model = m.group(2) or ("1" if m.group(1) == "tablet" else None)
        if model is None:
            return _Hit(None, start, cat)
        return _Hit(ProductKey(f"pixel {m.group(1)}", model), start, cat)
    m = _PIXEL_RE.search(s2)
    if not m:
        return _Hit(None, toks.index("pixel"), "phone") if "google" in toks else None
    i = _tok_index(s2, m.start(1))
    variant = _collect(tuple(s2.split()), i + 1, {"pro", "xl", "fold"})
    return _Hit(ProductKey("pixel", m.group(1), variant, _storage(toks, 64)), _tok_index(s2, m.start()), "phone")


# --- Consoles & handhelds ------------------------------------------------

_PS_RE = re.compile(r"\b(?:ps ?|playstation ?)([1-5])\b")
_PSVR_RE = re.compile(r"\b(?:ps|playstation) vr ?(\d)?\b")
_PORTAL_RE = re.compile(r"\b(?:ps|playstation) portal\b")


def _m_playstation(s: str, toks: tuple[str, ...]) -> _Hit | None:
    hits = [(m.start(), rx) for rx in (_PS_RE, _PSVR_RE, _PORTAL_RE) if (m := rx.search(s))]
    if not hits:
        return _Hit(None, toks.index("playstation"), "console") if "playstation" in toks else None
    pos, rx = min(hits, key=lambda h: h[0])
    m = rx.search(s)
    assert m is not None
    start = _tok_index(s, pos)
    if rx is _PSVR_RE:
        return _Hit(ProductKey("playstation vr", m.group(1) or "1"), start, "console")
    if rx is _PORTAL_RE:
        return _Hit(ProductKey("playstation portal", "1"), start, "console")
    model = m.group(1)
    clause = _clause(toks, _tok_index(s, m.start(1)) + 1, 7)
    variant = [t for t in clause if t in ("slim", "pro", "digital")]
    if "super" in clause and "slim" in clause:
        variant.append("slim")
    if re.search(r"\bohne (?:disc |disk )?laufwerk\b", s) and model == "5":
        variant.append("digital")
    return _Hit(ProductKey("playstation", model, _canon_variant(variant), _storage(toks, 64)), start, "console")


_XBOX_RE = re.compile(r"\bxbox ?(series ?([xs])|one(?: ([xs]))?|360)\b")


def _m_xbox(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if "xbox" not in toks:
        return None
    m = _XBOX_RE.search(s)
    start = toks.index("xbox")
    if not m:
        return _Hit(None, start, "console")
    if m.group(2):
        model = f"series {m.group(2)}"
    elif m.group(1).startswith("one"):
        model = f"one {m.group(3)}" if m.group(3) else "one"
    else:
        model = "360"
    variant = ("digital",) if model.startswith("one") and "digital" in toks else ()
    return _Hit(ProductKey("xbox", model, variant, _storage(toks, 64)), start, "console")


_SWITCH_RE = re.compile(r"\bswitch(?: (oled|lite|2|v1|v2))?\b")
_DS_RE = re.compile(r"\b(new )?([23]ds|dsi|ds)(?: (xl|ll|lite))?\b")


def _m_nintendo(s: str, toks: tuple[str, ...]) -> _Hit | None:
    nintendo = "nintendo" in toks
    m = _SWITCH_RE.search(s)
    if m and (nintendo or m.group(1) in ("oled", "lite", "2")):
        nxt = s[m.end():].split()[:1]
        if m.group(1) == "2" and nxt and nxt[0] in ("port", "ports", "fach", "kanal"):
            return None
        start = _tok_index(s, m.start())
        clause = _clause(toks, start + 1, 6)
        if m.group(1) == "2":
            model = "2"
        elif "oled" in clause or m.group(1) == "oled":
            model = "oled"
        elif "lite" in clause or m.group(1) == "lite":
            model = "lite"
        else:
            model = "standard"  # V1 / V2 / "Nintendo Switch": same price class
        if nintendo and toks.index("nintendo") < start:
            start = toks.index("nintendo")
        return _Hit(ProductKey("nintendo switch", model, (), _storage(toks, 64)), start, "console")
    m = _DS_RE.search(s)
    if m and (nintendo or m.group(2) in ("3ds", "2ds")):
        variant = [w for w in (m.group(1) and "new", m.group(3)) if w]
        variant = ["xl" if w == "ll" else w for w in variant]
        return _Hit(ProductKey("nintendo ds", m.group(2), _canon_variant(variant)), _tok_index(s, m.start()),
                    "console")
    m = re.search(r"\b(?:nintendo 64|n64)\b", s)
    if m:
        return _Hit(ProductKey("nintendo 64", "64"), _tok_index(s, m.start()), "console")
    return None


_DECK_RE = re.compile(r"\bsteam ?deck\b")
_ALLY_RE = re.compile(r"\brog ally(?: (x))?\b")
_LEGION_GO_RE = re.compile(r"\blegion go(?: (s))?\b")
_QUEST_RE = re.compile(r"\b(?:(?:meta|oculus) quest ?(\d s?|\ds?|pro)?|quest ?(\d s?|\ds?|pro))\b")


def _m_handheld(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if (m := _DECK_RE.search(s)):
        model = "oled" if "oled" in toks else "lcd"
        return _Hit(ProductKey("steam deck", model, (), _storage(toks, 64)), _tok_index(s, m.start()), "handheld")
    if (m := _ALLY_RE.search(s)):
        model = "x" if m.group(1) else "1"
        variant = ("extreme",) if model == "1" and "extreme" in toks else ()
        return _Hit(ProductKey("rog ally", model, variant, _storage(toks, 256)), _tok_index(s, m.start()),
                    "handheld")
    if (m := _LEGION_GO_RE.search(s)):
        return _Hit(ProductKey("legion go", m.group(1) or "1", (), _storage(toks, 256)), _tok_index(s, m.start()),
                    "handheld")
    if (m := _QUEST_RE.search(s)) and ("meta" in toks or "oculus" in toks or m.group(2)):
        model = (m.group(1) or m.group(2) or "").replace(" ", "")
        start = _tok_index(s, m.start())
        if not model:
            return _Hit(None, start, "handheld")
        return _Hit(ProductKey("meta quest", model, (), _storage(toks, 64)), start, "handheld")
    return None


# --- Laptops -------------------------------------------------------------

_THINKPAD_RE = re.compile(
    r"\bthinkpad ?(?:(x1) (carbon|yoga|extreme|nano|tablet|titanium)|yoga ([x]?\d{2,3})|([a-z]\d{1,3}[a-z]?))\b")
_LENOVO_TP_RE = re.compile(r"\blenovo ((?:t|x|l|e|p)\d{2,3}s?)\b")
_GEN_TOKEN_RE = re.compile(r"^(?:gen|g)(\d{1,2})$")
# (regex, family): model is group 1, optional variant group 2
_LAPTOP_LINES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bxps ?(\d{4})\b"), "dell xps"),
    (re.compile(r"\bxps ?(\d{2})(?: (\d{4}))?\b"), "dell xps"),
    (re.compile(r"\b(?:latitude|precision|vostro|inspiron) ([a-z]?\d{4})\b"), "dell"),
    (re.compile(r"\b(?:elitebook|probook|zbook) ?(\d{2,4}[a-z]?)\b"), "hp"),
    (re.compile(r"\b(?:omen|victus|envy|spectre|pavilion) (?:x360 )?(\d{2})\b"), "hp"),
    (re.compile(r"\bsurface (pro|laptop|book|go|studio)(?: (\d{1,2}|x))?\b"), "surface"),
    (re.compile(r"\blegion (?:pro |slim )?(\d)i?\b"), "lenovo legion"),
    (re.compile(r"\b(?:ideapad|yoga) (?:slim |pro |gaming |flex )?(\d{1,4}[a-z]?)\b"), "lenovo"),
    (re.compile(r"\bzephyrus ([gms]\d{2}|duo)\b"), "asus zephyrus"),
    (re.compile(r"\bstrix (g\d{2}|scar(?: \d{2})?)\b"), "asus strix"),
    (re.compile(r"\btuf (?:gaming )?([fa]\d{2}|dash)\b"), "asus tuf"),
    (re.compile(r"\b(?:zenbook|vivobook) (?:pro |flip |s |go )?(\d{2}|duo|[a-z]{1,2}\d{3,4}[a-z]{0,2})\b"), "asus"),
    (re.compile(r"\b(?:nitro|helios|triton|swift|aspire) ?(\d{1,3})\b"), "acer"),
    (re.compile(r"\b(?:katana|stealth|raider|cyborg|crosshair|prestige|modern|titan|sword) ?(\d{2}|gf\d{2})\b"), "msi"),
    (re.compile(r"\b(gf63|gf65|gl\d{2}|ge\d{2}|gs\d{2}|gp\d{2})\b"), "msi"),
    (re.compile(r"\brazer blade (\d{2})\b"), "razer blade"),
    (re.compile(r"\balienware ([mx]\d{2}|\d{2})\b"), "alienware"),
    (re.compile(r"\baero (\d{2})\b"), "gigabyte aero"),
)
# laptop-only line words: when one is present without a model, the title gets no key at all
# (so "Lenovo Legion Laptop RTX 3080" is not keyed as an RTX 3080)
_LAPTOP_LINE_WORDS = frozenset({"thinkpad", "legion", "zephyrus", "ideapad", "zenbook", "vivobook", "elitebook",
                                "probook", "zbook", "latitude", "inspiron", "vostro", "alienware", "omen",
                                "victus", "chromebook", "xps", "predator", "helios", "katana"})


def _laptop_family_name(fam: str, text: str) -> str:
    if fam in ("dell", "hp", "lenovo", "asus", "acer", "msi"):
        line = re.search(r"latitude|precision|vostro|inspiron|elitebook|probook|zbook|omen|victus|envy|spectre|"
                         r"pavilion|ideapad|yoga|zenbook|vivobook|nitro|helios|triton|swift|aspire|katana|stealth|"
                         r"raider|cyborg|crosshair|prestige|modern|titan|sword", text)
        return f"{fam} {line.group(0)}" if line else fam
    return fam


def _m_laptop(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _THINKPAD_RE.search(s) or _LENOVO_TP_RE.search(s)
    if m:
        start = _tok_index(s, m.start())
        if m.re is _THINKPAD_RE and m.group(1):
            model, variant = "x1", [m.group(2)]
        elif m.re is _THINKPAD_RE and m.group(3):
            model, variant = m.group(3), ["yoga"]
        else:
            model, variant = m.group(m.lastindex or 1), []
        for t in _clause(toks, _tok_index(s, m.end()) + 1, 4):
            g = _GEN_TOKEN_RE.match(t)
            if g:
                variant.append(f"gen{g.group(1)}")
                break
        return _Hit(ProductKey("thinkpad", model, _canon_variant(variant)), start, "laptop")
    if "thinkpad" in toks:
        return _Hit(None, toks.index("thinkpad"), "laptop")
    for rx, fam in _LAPTOP_LINES:
        m = rx.search(s)
        if not m:
            continue
        start = _tok_index(s, m.start())
        family = _laptop_family_name(fam, m.group(0))
        if fam == "surface":
            family = f"surface {m.group(1)}"
            if not m.group(2):
                return _Hit(None, start, "laptop")
            model = m.group(2)
            variant = _collect(toks, _tok_index(s, m.start(2)) + 1, {"plus"})
            return _Hit(ProductKey(family, model, variant), start, "laptop")
        model = m.group(2) if fam == "dell xps" and m.lastindex == 2 and m.group(2) else m.group(1)
        variant: list[str] = []
        after = _clause(toks, _tok_index(s, m.end()) + 1, 4)
        for t in after:
            g = _GEN_TOKEN_RE.match(t)
            if g:
                variant.append(f"gen{g.group(1)}")
            elif t in ("pro", "slim", "plus") and fam in ("lenovo legion", "asus", "lenovo"):
                variant.append(t)
            else:
                continue
            break
        return _Hit(ProductKey(family, model, _canon_variant(variant)), start, "laptop")
    line = next((i for i, t in enumerate(toks) if t in _LAPTOP_LINE_WORDS), None)
    if line is not None:
        return _Hit(None, line, "laptop")
    return None


# --- GPUs ----------------------------------------------------------------

_GPU_NV_RE = re.compile(r"\b(rtx|gtx|gt) ?(\d{3,4})\b")
_GPU_NV_BARE_RE = re.compile(r"\b(?:geforce|nvidia)(?: rtx| gtx)? ?(\d{3,4})\b")
_GPU_AMD_RE = re.compile(r"\b(?:rx|radeon(?: rx)?) ?(\d{3,4})\b")
_GPU_VEGA_RE = re.compile(r"\b(?:rx |radeon )?vega ?(56|64)\b")
_GPU_ARC_RE = re.compile(r"\barc ?([ab]\d{3})\b")
_GPU_KNOWN_NV = frozenset(
    "1050 1060 1070 1080 1650 1660 2060 2070 2080 3050 3060 3070 3080 3090 4060 4070 4080 4090 "
    "5060 5070 5080 5090".split())
_GPU_BARE_RE = re.compile(r"\b(" + "|".join(sorted(_GPU_KNOWN_NV)) + r")\b")
# words that make a bare "3080" a graphics card
_GPU_HINTS = frozenset({"grafikkarte", "gpu", "gddr6", "gddr6x", "gddr5", "evga", "zotac", "palit", "gainward",
                        "pny", "inno3d", "kfa2", "galax", "ventus", "trinity", "founders", "vram"})
_GPU_SUFFIX = frozenset({"ti", "super", "xt", "xtx", "gre"})
_GPU_IGNORE = _IGNORE_AFTER_MODEL | {"lhr", "fe", "founders", "nvidia", "geforce", "non"}
# models sold with several VRAM sizes -> the common one is the default (not written into the key)
_GPU_DEFAULT_VRAM: dict[tuple[str, str, tuple[str, ...]], int] = {
    ("rtx", "3080", ()): 10, ("rtx", "3060", ()): 12, ("rtx", "3050", ()): 8, ("rtx", "2060", ()): 6,
    ("rtx", "4060", ("ti",)): 8, ("rtx", "5060", ("ti",)): 8, ("gtx", "1060", ()): 6, ("gtx", "1050", ()): 2,
    ("rx", "580", ()): 8, ("rx", "570", ()): 8, ("rx", "480", ()): 8, ("rx", "470", ()): 4,
    ("rx", "6500", ("xt",)): 4, ("rx", "9060", ("xt",)): 16, ("arc", "a770", ()): 16,
}


def _nv_family(num: str, written: str | None) -> str:
    n = int(num)
    if 2000 <= n < 6000 and not 1600 <= n < 1700:
        return "rtx"
    if written == "gt" or n in (710, 730, 1030):
        return "gt"
    return "gtx"


def _vram(toks: tuple[str, ...]) -> int | None:
    for i, tok in enumerate(toks):
        v = _gb_value(tok)
        if v is None and (g := _VRAM_G_RE.match(tok)) and 2 <= int(g.group(1)) <= 48:
            return int(g.group(1))
        if v is not None and 2 <= v <= 48:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if nxt in _RAM_WORDS - {"memory"} and nxt not in _VRAM_WORDS:
                continue  # "32gb ram" in a PC title is not VRAM
            return int(v)
    return None


def _m_gpu(s: str, toks: tuple[str, ...]) -> _Hit | None:
    fam = None
    m = _GPU_NV_RE.search(s)
    if m:
        fam, num = _nv_family(m.group(2), m.group(1)), m.group(2)
    elif (m := _GPU_NV_BARE_RE.search(s)):
        fam, num = _nv_family(m.group(1), None), m.group(1)
    elif (m := _GPU_AMD_RE.search(s)):
        fam, num = "rx", m.group(1)
    elif (m := _GPU_VEGA_RE.search(s)):
        fam, num = "rx vega", m.group(1)
    elif (m := _GPU_ARC_RE.search(s)):
        fam, num = "arc", m.group(1)
    elif _GPU_HINTS.intersection(toks) and (m := _GPU_BARE_RE.search(s)):
        fam, num = _nv_family(m.group(1), None), m.group(1)
    if fam is None or m is None:
        return None
    i = _tok_index(s, m.end())  # index of the model token
    variant = _collect(toks, i + 1, _GPU_SUFFIX, _GPU_IGNORE)
    default = _GPU_DEFAULT_VRAM.get((fam, num, variant))
    memory = None
    if default is not None:
        v = _vram(toks)
        if v is not None and v != default:
            memory = f"{v}gb"
    return _Hit(ProductKey(fam, num, variant, None, memory), _tok_index(s, m.start()), "gpu")


# --- CPUs, RAM, SSDs -----------------------------------------------------

_INTEL_RE = re.compile(r"\b(?:core )?i[3579] ?(\d{3,5}[a-z]{0,3})\b")
_CORE_ULTRA_RE = re.compile(r"\bcore ultra ?[3579] ?(\d{3}[a-z]{0,2})\b")
_RYZEN_RE = re.compile(r"\bryzen (?:[3579] )?(?:pro )?(\d{4})(?: ?(x3d|xt|x|ge|g|gt|f|hs|hx|h|u))?\b")
_THREADRIPPER_RE = re.compile(r"\bthreadripper (?:pro )?(\d{4}[a-z]{0,3})\b")
_XEON_RE = re.compile(r"\bxeon (?:(e[357]|w|gold|silver|platinum|bronze) ?)?(\d{4}[a-z]?)(?: (v\d))?\b")


def _m_cpu(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if (m := _CORE_ULTRA_RE.search(s)):
        return _Hit(ProductKey("intel core ultra", m.group(1)), _tok_index(s, m.start()), "cpu")
    if (m := _INTEL_RE.search(s)):
        return _Hit(ProductKey("intel core", m.group(1)), _tok_index(s, m.start()), "cpu")
    if (m := _THREADRIPPER_RE.search(s)):
        return _Hit(ProductKey("threadripper", m.group(1)), _tok_index(s, m.start()), "cpu")
    if (m := _RYZEN_RE.search(s)):
        return _Hit(ProductKey("ryzen", m.group(1) + (m.group(2) or "")), _tok_index(s, m.start()), "cpu")
    if (m := _XEON_RE.search(s)):
        model = f"{m.group(1)} {m.group(2)}" if m.group(1) else m.group(2)
        variant = (m.group(3),) if m.group(3) else ()
        return _Hit(ProductKey("xeon", model, variant), _tok_index(s, m.start()), "cpu")
    if "ryzen" in toks:
        return _Hit(None, toks.index("ryzen"), "cpu")
    return None


_DDR_RE = re.compile(r"\b(?:lp)?ddr([2345])[lx]?\b|\bpc([34]) ?\d{4,5}\b")
_RAM_HINTS = frozenset({"ram", "arbeitsspeicher", "dimm", "sodimm", "kit", "memory", "vengeance", "fury",
                        "ripjaws", "trident", "ballistix", "ecc", "rdimm", "udimm", "speicher", "riegel"})
_KIT_SPACED_RE = re.compile(r"\b([1-8]) ?x ?(\d{1,3})gb\b")


def _m_ram(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _DDR_RE.search(s)
    if not m:
        return None
    kit = _KIT_SPACED_RE.search(s)
    if not (kit or _RAM_HINTS.intersection(toks)) or {"mainboard", "motherboard"}.intersection(toks):
        return None
    gen = m.group(1) or m.group(2)
    if kit:
        total = float(int(kit.group(1)) * int(kit.group(2)))
    else:
        vals = [v for _, v, _lab in _sizes(toks) if v <= 512]
        if not vals:
            return _Hit(None, _tok_index(s, m.start()), "ram")
        total = max(vals)
    variant = []
    if {"ecc", "registered", "rdimm", "reg", "lrdimm"}.intersection(toks):
        variant.append("ecc")
    if "sodimm" in toks or re.search(r"\bso ?dimm\b", s) or {"laptop", "notebook"}.intersection(toks):
        variant.append("sodimm")
    start = min(_tok_index(s, m.start()), _tok_index(s, kit.start()) if kit else 10**6)
    return _Hit(ProductKey(f"ram ddr{gen}", _fmt_gb(total), _canon_variant(variant)), start, "ram")


_SAMSUNG_SSD_RE = re.compile(r"\b(8[5-7]0|9[5-9]0) (evo|pro|qvo)(?: (plus))?\b")
_WD_SN_RE = re.compile(r"\bsn ?(\d{3}x?)\b")
_CRUCIAL_RE = re.compile(r"\b(mx\d{3}|bx\d{3}|p[1-5])(?: (plus))?\b")


def _m_ssd(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if (m := _SAMSUNG_SSD_RE.search(s)):
        key = ProductKey("samsung ssd", m.group(1), _canon_variant([m.group(2), m.group(3) or ""]),
                         _storage(toks, 100))
        return _Hit(key, _tok_index(s, m.start()), "storage")
    if (m := _WD_SN_RE.search(s)) and ({"wd", "western", "black", "blue", "red", "ssd", "nvme"} & set(toks)):
        return _Hit(ProductKey("wd sn", f"sn{m.group(1)}", (), _storage(toks, 100)), _tok_index(s, m.start()),
                    "storage")
    if "crucial" in toks and (m := _CRUCIAL_RE.search(s)):
        return _Hit(ProductKey("crucial ssd", m.group(1), (m.group(2),) if m.group(2) else (),
                               _storage(toks, 100)), _tok_index(s, m.start()), "storage")
    return None


# --- Audio, cameras, drones, household, tools ----------------------------

_SONY_1000X_RE = re.compile(r"\b(wh|wf)? ?1000 ?x ?m ?([1-6])\b")
_SONY_XM_RE = re.compile(r"\bxm([1-6])\b")
_BOSE_QC_RE = re.compile(r"\b(?:quiet ?comfort|qc) ?(\d{2}|ultra|earbuds|se|headphones)?\b")
_BOSE_700_RE = re.compile(r"\b(?:nc |noise cancelling headphones )?700\b")


def _m_audio(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _SONY_1000X_RE.search(s)
    if m or ("sony" in toks and (m := _SONY_XM_RE.search(s))):
        written = m.group(1) if m.re is _SONY_1000X_RE else None
        in_ear = "earbuds" in toks or "inear" in toks or re.search(r"\bin ear\b", s) is not None
        kind = written or ("wf" if in_ear else "wh")
        gen = m.group(2) if m.re is _SONY_1000X_RE else m.group(1)
        return _Hit(ProductKey(f"sony {kind}-1000x", f"xm{gen}"), _tok_index(s, m.start()), "audio")
    if "bose" in toks or "quietcomfort" in toks:
        m = _BOSE_QC_RE.search(s)
        if m:
            start = _tok_index(s, m.start())
            if not m.group(1):
                return _Hit(None, start, "audio")
            nxt = _clause(toks, _tok_index(s, m.end()) + 1, 2)
            variant = [w for w in nxt[:2] if w in ("ii", "earbuds", "headphones")]
            if nxt[:1] == ("2",):
                variant.append("ii")
            return _Hit(ProductKey("bose qc", m.group(1), _canon_variant(variant)), start, "audio")
        if "bose" in toks and (m := _BOSE_700_RE.search(s)):
            return _Hit(ProductKey("bose nc", "700"), _tok_index(s, m.start()), "audio")
    return None


_CAMERA_WORDS = frozenset({"kamera", "camera", "body", "gehaeuse", "objektiv", "systemkamera", "dslm", "dslr",
                           "spiegelreflex", "spiegelreflexkamera", "spiegellos", "vollformat", "kit"})
_SONY_A_RE = re.compile(r"\b(?:alpha ?|ilce ?|a)(7|9|1)(r|s|c|cr)?(?: ?(?:mk|m) ?([2-5]))?(?: ?(ii|iii|iv|v))?(?=\s|$)")
_SONY_APSC_RE = re.compile(r"\b(?:alpha ?|ilce ?|a)(5000|5100|6000|6100|6300|6400|6500|6600|6700)\b")
_CANON_RE = re.compile(r"\b(?:eos ?|canon )(r\d{0,2}|rp|\d{1,4}d|m\d{0,2})(?: ?(?:mk ?([2-4])|(ii|iii|iv)))?(?=\s|$)")
_NIKON_RE = re.compile(r"\bnikon (d\d{2,4}|z ?\d{1,2}|z ?fc|z ?f)(?: ?(?:mk ?([2-4])|(ii|iii)))?\b")
_FUJI_RE = re.compile(r"\bx ?(t\d{1,3}|s\d{1,2}|e\d|h\d|pro\d|m\d|100[a-z]{0,2})\b")
_GOPRO_RE = re.compile(r"\b(?:gopro (?:hero ?)?|hero ?)(\d{1,2})(?: (black|silver|white|mini|creator))?\b")
_FOCAL_RE = re.compile(r"\b\d{2,3} \d{2,3}mm\b|\b\d{2,3}mm f ?\d")


def _cam_mark(*parts: str | None) -> tuple[str, ...]:
    for p in parts:
        if p:
            return (f"mk{_ROMAN.get(p, p)}",)
    return ()


def _m_camera(s: str, toks: tuple[str, ...]) -> _Hit | None:
    kit = "kit" in toks or "objektiv" in toks or "objektive" in toks or bool(_FOCAL_RE.search(s))
    extra = ("kit",) if kit else ()
    sony = "sony" in toks or "alpha" in toks or "ilce" in toks or bool(_CAMERA_WORDS & set(toks))
    if sony and (m := _SONY_APSC_RE.search(s)):
        return _Hit(ProductKey("sony alpha", f"a{m.group(1)}", extra), _tok_index(s, m.start()), "camera")
    if sony and (m := _SONY_A_RE.search(s)):
        model = f"a{m.group(1)}{m.group(2) or ''}"
        return _Hit(ProductKey("sony alpha", model, _canon_variant(_cam_mark(m.group(3), m.group(4)) + extra)),
                    _tok_index(s, m.start()), "camera")
    if ("canon" in toks or "eos" in toks) and (m := _CANON_RE.search(s)):
        return _Hit(ProductKey("canon eos", m.group(1), _canon_variant(_cam_mark(m.group(2), m.group(3)) + extra)),
                    _tok_index(s, m.start()), "camera")
    if (m := _NIKON_RE.search(s)):
        return _Hit(ProductKey("nikon", m.group(1).replace(" ", ""),
                               _canon_variant(_cam_mark(m.group(2), m.group(3)) + extra)),
                    _tok_index(s, m.start()), "camera")
    if ("fujifilm" in toks or "fuji" in toks) and (m := _FUJI_RE.search(s)):
        return _Hit(ProductKey("fujifilm x", m.group(1), extra), _tok_index(s, m.start()), "camera")
    if (m := _GOPRO_RE.search(s)) and ("gopro" in toks or m.group(2)):
        return _Hit(ProductKey("gopro hero", m.group(1), (m.group(2),) if m.group(2) else ()),
                    _tok_index(s, m.start()), "camera")
    return None


_DJI_RE = re.compile(
    r"\b(mavic air|mavic mini|mavic|mini|air|avata|osmo pocket|osmo action|osmo mobile|pocket|action|neo|"
    r"flip|fpv|phantom|spark)(?: (\d[a-z]?))?\b")
_DJI_FAMILY = {"mavic air": "dji air", "mavic mini": "dji mini", "pocket": "dji osmo pocket",
               "action": "dji osmo action"}


def _m_dji(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if not ({"dji", "mavic", "avata", "osmo", "phantom"} & set(toks)):
        return None
    begin = s.find("dji ") + 4 if "dji" in toks else 0
    m = _DJI_RE.search(s, begin) or _DJI_RE.search(s)
    if not m:
        return _Hit(None, toks.index("dji") if "dji" in toks else 0, "drone")
    name = m.group(1)
    family = _DJI_FAMILY.get(name, f"dji {name}")
    model = m.group(2) or "1"
    i = _tok_index(s, m.end())
    variant = list(_collect(toks, i + 1, {"pro", "se", "classic", "cine", "plus"}))
    if name == "mavic" and not m.group(2) and i + 1 < len(toks) and toks[i + 1] == "pro":
        variant = ["pro"]
    if "fly" in toks and "more" in toks or "flymore" in toks:
        variant.append("combo")
    return _Hit(ProductKey(family, model, _canon_variant(variant)), _tok_index(s, m.start()), "drone")


_DYSON_RE = re.compile(r"\b(v(?:6|7|8|10|11|12|15|16)|gen5|sv\d{2}|supersonic|airwrap|corrale|airstrait)\b")


def _m_dyson(s: str, toks: tuple[str, ...]) -> _Hit | None:
    if "dyson" not in toks:
        return None
    m = _DYSON_RE.search(s)
    if not m:
        return _Hit(None, toks.index("dyson"), "vacuum")
    variant = [w for w in ("slim", "outsize") if w in toks]
    return _Hit(ProductKey("dyson", m.group(1), _canon_variant(variant)), min(toks.index("dyson"),
                _tok_index(s, m.start())), "vacuum")


_BOSCH_RE = re.compile(r"\b(g[a-z]{2}) ((?:\d{1,2}v )?\d{1,3}(?: \d{1,3})?)(?: (f|c|ec|b|e|k|h|d|re|dre))?\b")
_MAKITA_RE = re.compile(r"\b((?:d[a-z]{2}|[a-z]{2})\d{3,4})([a-z]{1,4})?\b")
_DEWALT_RE = re.compile(r"\b(dc[a-z]\d{3,4}|dw[a-z]?\d{3,4})([a-z]{1,2}\d?[a-z]?)?\b")
_SOLO_RE = re.compile(r"\b(?:solo|sologeraet|grundgeraet|body only|nur (?:das )?(?:geraet|werkzeug|maschine)|"
                      r"ohne (?:akkus?|ladegeraet|lader)(?: (?:und|oder|u) (?:akkus?|ladegeraet|lader))?|"
                      r"baremetal|tool only)\b")


def _m_tools(s: str, toks: tuple[str, ...]) -> _Hit | None:
    solo = bool(_SOLO_RE.search(s))
    if "bosch" in toks and (m := _BOSCH_RE.search(s)):
        variant = [m.group(3)] if m.group(3) else []
        if solo:
            variant.append("solo")
        return _Hit(ProductKey("bosch", f"{m.group(1)} {m.group(2)}", _canon_variant(variant)),
                    _tok_index(s, m.start()), "tool")
    m = _DEWALT_RE.search(s)
    if m and ("dewalt" in toks or m.group(1).startswith("dc")):
        is_solo = solo or (m.group(2) or "") in ("n", "nt")  # "DCD796N" = bare tool
        return _Hit(ProductKey("dewalt", m.group(1), ("solo",) if is_solo else ()), _tok_index(s, m.start()), "tool")
    if "makita" in toks and (m := _MAKITA_RE.search(s)):
        suffix = m.group(2) or ""
        is_solo = solo or suffix.startswith("z")
        return _Hit(ProductKey("makita", m.group(1), ("solo",) if is_solo else ()), _tok_index(s, m.start()), "tool")
    return None


_TOY_RE = re.compile(r"\b(lego|playmobil)\b")
_SET_NO_RE = re.compile(r"\b(\d{4,6})\b")


def _m_toys(s: str, toks: tuple[str, ...]) -> _Hit | None:
    m = _TOY_RE.search(s)
    if not m:
        return None
    n = next((x for x in _SET_NO_RE.findall(s) if not (len(x) == 4 and 1950 <= int(x) <= 2035)), None)
    start = _tok_index(s, m.start())
    return _Hit(ProductKey(m.group(1), n) if n else None, start, "other")


# --- Generic fallback ----------------------------------------------------

_UNIT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:gb|tb|mb|kb|ghz|mhz|hz|mah|ah|wh|zoll|inch|mm|cm|m|km|kg|g|l|ml|w|kw|v|k|p|mp|x|er|fach|"
    r"teilig|tlg|jahre|j|stk|st|min|h|%|qm|m2|ps|cc|ccm|t|kmh|lbs|mbit|gbit|db|nm|rpm|u|a|bit)$")
_DIM_RE = re.compile(r"^\d+x\d+")
_COUNT_AFTER = frozenset(
    "stueck stk sitzer sitze teilig tlg personen zimmer kinder jahre jahr monate monat wochen tage x mal paar er "
    "fach spiele spielen controller controllern games gaenge gang port ports kanal kanaele zoll teile set sets "
    "stuehle mann liter kg meter watt volt uhr".split())
_PREP_BEFORE = frozenset(
    "groesse gr size eu nr nummer ab fuer mit und inkl bis von ca circa je alle noch nur ueber unter um seit "
    "vor nach auf in im am an bei zu zum zur der die das den dem ein eine einen oder x".split())
_FILLER = frozenset(
    """verkaufe verkauf verkaufen biete bieten abzugeben gebe ab neu neue neuer neues neuem neuwertig neuwertige
    neuwertiger neuwertiges top topzustand zustand sehr gut gute guter gutes gutem super mega hammer wie
    nagelneu brandneu ovp originalverpackt originalverpackung verpackung karton unbenutzt ungeoeffnet ungebraucht
    versiegelt sealed boxed inkl inklusive incl mit und u oder fuer ohne von vom zum zur im in am an auf aus bei
    bis der die das den dem des ein eine einen einem einer ist sind zu ich wir sie gebraucht benutzt genutzt
    wenig kaum selten fast nur voll np vb fp festpreis preis verhandelbar angebot guenstig billig dringend
    schnell sofort rechnung garantie gewaehrleistung tausch versand abholung privat original orginal
    funktionsfaehig einwandfrei tadellos gepflegt euro eur stueck stk new used like mint condition sale for with
    the defekt defekte bastler suche kaufe ankauf gesucht set edition version modell model generation gen
    farbe color colour groesse size""".split())
_COLORS = frozenset(
    """schwarz weiss grau silber gold rot blau gruen gelb rosa pink lila orange braun beige tuerkis violett
    space spacegrau spacegray black white grey gray silver red blue green midnight starlight graphite graphit
    polarstern mitternacht titan natur sierrablau alpingruen""".split())
_BRANDS = frozenset(
    """apple samsung sony nintendo microsoft lenovo dell hp asus acer msi gigabyte zotac evga palit gainward inno3d
    pny sapphire powercolor xfx asrock nvidia amd intel corsair kingston crucial wd seagate synology qnap logitech
    razer steelseries bose jbl sennheiser beyerdynamic canon nikon fujifilm fuji olympus panasonic lumix gopro dji
    garmin fitbit huawei xiaomi oneplus google lg philips dyson bosch makita dewalt festool metabo hilti miele
    siemens vorwerk kitchenaid delonghi jura nespresso lego playmobil marshall fender gibson yamaha roland korg
    shimano raspberry ubiquiti avm fritz meta oculus valve noctua nzxt thermaltake seasonic teufel nubert denon
    marantz onkyo sonos nothing honor oppo motorola nokia framework amazon anker ecovacs roborock tp link
    netgear einhell ryobi milwaukee""".split())
_GENERIC_NOUNS = frozenset(
    """handy smartphone telefon tablet laptop notebook pc computer rechner konsole spielkonsole grafikkarte
    prozessor cpu gpu kopfhoerer headphones lautsprecher speaker kamera camera drohne uhr smartwatch fernseher tv
    monitor bildschirm maus tastatur drucker router staubsauger akkuschrauber bohrmaschine fahrrad ebike
    kinderwagen spiel spiele gaming gamer""".split())
_GENERIC_SUFFIX = frozenset({"pro", "max", "plus", "ultra", "mini", "lite", "se", "xl"})


def _is_model_tok(toks: tuple[str, ...], i: int) -> bool:
    t = toks[i]
    if not any(c.isdigit() for c in t) or _UNIT_RE.match(t) or _DIM_RE.match(t) or _GB_TOKEN_RE.match(t):
        return False
    if re.fullmatch(r"(?:gen|mk)\d+", t) or _KIT_TOKEN_RE.match(t):
        return False
    prv = toks[i - 1] if i else ""
    nxt = toks[i + 1] if i + 1 < len(toks) else ""
    if t.isdigit():
        if len(t) == 4 and 1950 <= int(t) <= 2035:
            return False  # a year
        if len(t) > 6:
            return False  # phone numbers, article numbers
        if nxt in _COUNT_AFTER or prv in _PREP_BEFORE or prv in _FILLER:
            return False
        if len(t) <= 2 and (not prv or not prv.isalpha() or prv in _GENERIC_NOUNS):
            return False
    return True


def _m_generic(s: str, toks: tuple[str, ...]) -> _Hit | None:
    idx = next((i for i in range(len(toks)) if _is_model_tok(toks, i)), None)
    if idx is None:  # "Kindle Paperwhite 11. Generation": the generation is the model
        idx = next((i for i, t in enumerate(toks) if re.fullmatch(r"gen\d{1,2}", t) and i
                    and toks[i - 1].isalpha() and toks[i - 1] not in _FILLER), None)
    if idx is None:
        return None
    before: list[str] = []
    j = idx - 1
    while j >= 0 and len(before) < 2:
        t = toks[j]
        if not t.isalpha() or t in _FILLER or t in _COLORS or t in _CONNECTORS:
            break
        before.insert(0, t)
        j -= 1
    specific = [t for t in before if t not in _BRANDS and t not in _GENERIC_NOUNS]
    brands = [t for t in before if t in _BRANDS]
    family_words = specific or brands or [t for t in before if t in _GENERIC_NOUNS and t not in ("spiel", "spiele")]
    if not family_words:
        nxt = next((t for t in toks[idx + 1:idx + 3] if t.isalpha() and t not in _FILLER and t not in _COLORS
                    and t not in _CONNECTORS and t not in _GENERIC_SUFFIX), None)
        if nxt is None:
            return None
        family_words = [nxt]
    variant = list(_collect(toks, idx + 1, _GENERIC_SUFFIX))
    for t in _clause(toks, idx + 1, 4) if not toks[idx].startswith("gen") else ():
        if re.fullmatch(r"(?:gen|mk)\d+", t):
            variant.append(t)
            break
    cap_vals = [v for _, v, lab in _sizes(toks) if lab != "ram"]
    cap = _fmt_gb(max(cap_vals)) if cap_vals else None
    start = idx - len(before)
    return _Hit(ProductKey(" ".join(family_words), toks[idx], _canon_variant(variant), cap), start, "other")


# words whose family matcher must succeed; if it cannot find a model the title gets no key
_CLAIMED = frozenset({"iphone", "ipad", "macbook", "imac", "airpods", "playstation", "ps4", "ps5", "ps3", "xbox",
                      "rtx", "gtx", "geforce", "radeon", "ryzen", "dyson", "mavic", "gopro", "galaxy"})

_MATCHERS = (
    _m_iphone, _m_ipad, _m_mac, _m_apple_watch, _m_airpods, _m_samsung, _m_pixel, _m_playstation, _m_xbox,
    _m_nintendo, _m_handheld, _m_laptop, _m_gpu, _m_cpu, _m_ram, _m_ssd, _m_audio, _m_camera, _m_dji,
    _m_dyson, _m_tools, _m_toys,
)


@lru_cache(maxsize=8192)
def _analyze(title: str) -> tuple[tuple[str, ...], _Hit | None]:
    toks = _tokens(title)
    if not toks:
        return toks, None
    s = " ".join(toks)
    for matcher in _MATCHERS:
        hit = matcher(s, toks)
        if hit is not None:
            return toks, hit
    if _CLAIMED.intersection(toks):
        return toks, _Hit(None, next(i for i, t in enumerate(toks) if t in _CLAIMED), "other")
    return toks, _m_generic(s, toks)


# ---------------------------------------------------------------------------
# Public: product_key / same_product
# ---------------------------------------------------------------------------


def product_key(title: str) -> ProductKey | None:
    """Identity of the product in an ad title, or None when the title names no model
    ("iPhone", "Handy", "Fahrrad 28 Zoll") — such ads get no history and no estimate."""
    if not title or not title.strip():
        return None
    _, hit = _analyze(title)
    return hit.key if hit else None


def product_category(title: str) -> str | None:
    """Coarse category of the product ("phone", "gpu", "laptop", ...), None if unknown."""
    _, hit = _analyze(title) if title else ((), None)
    return hit.category if hit else None


# memory that is always compared (None = default VRAM, so it is not "unknown")
def _strict_memory(family: str) -> bool:
    return family in _GPU_FAMILIES


def mismatch_reason(a: ProductKey, b: ProductKey, *, strict_capacity: bool = True) -> str | None:
    """Why a and b are different products (None when they are the same)."""
    if a.family != b.family:
        return f"family {a.family!r} != {b.family!r}"
    if a.model != b.model:
        return f"model {a.model!r} != {b.model!r}"
    if a.variant != b.variant:
        return f"variant {' '.join(a.variant) or '-'!r} != {' '.join(b.variant) or '-'!r}"
    if strict_capacity and a.capacity and b.capacity and a.capacity != b.capacity:
        return f"capacity {a.capacity} != {b.capacity}"
    if _strict_memory(a.family):
        if a.memory != b.memory:
            return f"memory {a.memory or 'default'} != {b.memory or 'default'}"
    elif strict_capacity and a.memory and b.memory and a.memory != b.memory:
        return f"memory {a.memory} != {b.memory}"
    if a.size and b.size and a.size != b.size:
        return f"size {a.size} != {b.size}"
    return None


def same_product(a: ProductKey, b: ProductKey, *, strict_capacity: bool = True) -> bool:
    """Same family, model and variant set; storage/RAM/size equal when both state them
    (strict_capacity=False ignores storage and RAM). GPU VRAM is always compared."""
    return mismatch_reason(a, b, strict_capacity=strict_capacity) is None


# ---------------------------------------------------------------------------
# Kind classification
# ---------------------------------------------------------------------------

_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _fold(ch: str) -> str:
    """Strip accents from Latin letters (é -> e); keep other characters."""
    base = unicodedata.normalize("NFKD", ch)[:1]
    return base if base.isascii() else ch


@lru_cache(maxsize=4096)
def _flag_tokens(text: str) -> str:
    """Lowercase words plus clause punctuation as tokens ("display gebrochen , sonst ok .")."""
    t = unicodedata.normalize("NFKC", text).lower().translate(_TRANSLIT).replace("\n", " . ")
    t = "".join(c if c.isascii() else _fold(c) for c in t)
    return " ".join(re.findall(r"[^\W_]+|[.!?;:,]", t))


def _rx(patterns: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<!\S)(?:" + "|".join(patterns) + r")(?!\S)")


_PUNCT = frozenset(".!?;:,")
_NEG_BEFORE = frozenset("nicht nichts kein keine keinen keiner keinem keines keinerlei ohne nie niemals weder null "
                        "frei no not never without".split())
_SKIP_BEFORE = frozenset("und oder bzw sowie noch auch u von an".split())
_NEG_AFTER_COLON = frozenset("keine kein keiner keinerlei nein none 0 ohne nicht".split())
_LEGAL = frozenset("garantie gewaehrleistung ruecknahme umtausch haftung reklamation sachmangelhaftung rueckgabe "
                   "sachmaengelhaftung".split())
_DISCLAIMER_PREV = frozenset(
    "bei etwaige etwaigen etwaiger eventuelle eventuellen eventueller moegliche moeglichen versteckte versteckten "
    "spaetere spaeteren zukuenftige auftretende auftretenden allfaellige".split())
_CONDITIONAL = frozenset("falls sollte sollten wenn sofern fall falle".split())
_PACKAGING = frozenset("ovp karton verpackung box schachtel packung originalverpackung huelle tasche case cover "
                       "folie schutzfolie panzerglas schutzglas displayschutz".split())

_DEFECT_WORD_RX = _rx([r"(?:teil)?defekt(?:e|er|es|en|em|s)?", r"kaputt(?:e|er|es|en|em)?"])
_DEFECT_RX = _rx([
    r"bastler\w*", r"bastel(?:projekt|objekt|ware|geraet)", r"fuer (?:ersatz)?teile", r"als ersatzteil\w*",
    r"ersatzteil(?:spender|lager)", r"teilespender", r"for parts", r"(?:zum )?ausschlachten",
    r"(?:wasser|feuchtigkeits|sturz|display|bildschirm)schaden",
    r"(?:display|bildschirm|glas|scheibe|screen|touchscreen) (?:(?!(?:nicht|kein\w*|ohne|nie|schutz\w*|panzer\w*|"
    r"folie)(?!\S))[^\W_]+ ){0,2}(?:gebrochen|gesprungen|zersprungen|gerissen|zerbrochen|broken|cracked|kaputt)",
    r"(?:display|bildschirm)(?:bruch|riss|sprung)", r"(?:riss|risse|sprung) im (?:display|bildschirm|glas)",
    r"geht nicht (?:mehr )?an", r"(?:startet|bootet) nicht(?: mehr)?",
    r"(?:funktioniert|funktionieren) nicht(?: mehr| richtig)?",
    r"laesst sich nicht (?:mehr )?(?:einschalten|anschalten|starten|laden)", r"kein (?:bild|signal)",
    r"ohne funktion", r"funktionslos", r"reparaturbeduerftig", r"(?:muss|sollte) repariert werden",
    r"not working", r"broken", r"faulty", r"defective",
    r"icloud (?:gesperrt|lock|locked|sperre|aktiv)", r"icloud\w*sperre", r"aktivierungssperre", r"activation lock",
    r"(?:passwort|code|pin|passcode) vergessen", r"blacklist\w*",
])
_BOX_RX = _rx([
    r"(?:nur|lediglich|ausschliesslich) (?:die |der |das |den )?(?:original ?)?(?:ovp|karton|verpackung|box|"
    r"schachtel|packung|originalverpackung|originalkarton|leerkarton)",
    r"leer(?:e|er|es|en)? (?:original ?)?(?:ovp|karton|verpackung|box|schachtel|originalverpackung|originalkarton)",
    r"(?:ovp|karton|verpackung|box|schachtel|originalverpackung|originalkarton) (?:ist )?leer",
    r"leerkarton",
    r"(?:ovp|karton|verpackung|box|originalverpackung|originalkarton|schachtel|packung) ohne "
    r"(?:inhalt|geraet|handy|konsole|telefon|iphone|smartphone|ipad|tablet|laptop|karte|grafikkarte)",
    r"ohne inhalt", r"box only", r"empty box", r"only (?:the )?box",
])
_BOX_POST_OK = frozenset("fehlt fehlen hat weist zeigt leicht etwas beschaedigt leider minimal eingedrueckt "
                         "eingerissen gebrauchsspuren geoeffnet vorhanden dabei".split())
_SWAP_DESC_RX = _rx([r"nur (?:gegen |zum )?tausch\w*", r"ausschliesslich (?:gegen )?tausch\w*", r"kein verkauf",
                     r"(?:swap|trade) only", r"only (?:swap|trade)"])
_SWAP_CANCEL = frozenset("verkauf verkaufe verkaufen vk moeglich evtl eventuell gerne alternativ auch".split())


def _negated_before(prev: list[str]) -> bool:
    words = 0
    for tok in reversed(prev):
        if tok in _PUNCT:
            return False
        if tok in _NEG_BEFORE:
            return True
        if tok in _SKIP_BEFORE:
            continue
        words += 1
        if words >= 3:
            return False
    return False


def _negated_after(nxt: list[str]) -> bool:
    if not nxt:
        return False
    if nxt[0] in ("frei", "free", "nein"):
        return True
    if nxt[0] in (":", "?") and len(nxt) > 1 and nxt[1] in _NEG_AFTER_COLON:
        return True
    return nxt[0] == "nicht" and len(nxt) > 1 and nxt[1] in ("vorhanden", "festgestellt", "bekannt")


def _clause_before(prev: list[str], n: int) -> list[str]:
    out: list[str] = []
    for tok in reversed(prev):
        if tok in _PUNCT or len(out) >= n:
            break
        out.append(tok)
    return out  # nearest first


def _is_disclaimer(prev: list[str], nxt: list[str]) -> bool:
    """'Rücknahme bei Defekt ausgeschlossen', 'keine Haftung für Defekte', 'falls ein Defekt auftritt'."""
    near = _clause_before(prev, 6)
    if near and near[0] in _DISCLAIMER_PREV:
        return True
    if near and near[0] in ("fuer", "auf", "wegen", "bzgl", "bezueglich", "gegen") and _LEGAL.intersection(near[1:5]):
        return True
    if near[:2] and near[0] in ("eines", "einem", "einen", "ein") and _CONDITIONAL.intersection(near[1:3]):
        return True
    if _CONDITIONAL.intersection(near[:4]):
        return True
    if near and near[0] in ("fuer", "bei", "auf", "gegen") and _LEGAL.intersection(_clause_before(nxt[::-1], 6)):
        return True  # "Für Defekte nach dem Kauf keine Haftung"
    return bool(nxt) and nxt[0] in ("ausgeschlossen", "uebernehme", "uebernehmen")


def _has_defect(ft: str) -> bool:
    for m in _DEFECT_WORD_RX.finditer(ft):
        prev, nxt = ft[:m.start()].split(), ft[m.end():].split()
        if _negated_before(prev) or _negated_after(nxt[:3]) or _is_disclaimer(prev, nxt):
            continue
        if _PACKAGING.intersection(_clause_before(prev, 3)):
            continue  # "Karton leicht kaputt", "Hülle kaputt"
        if m.group(0).startswith("defekt") and nxt[:1] and nxt[0] in _LEGAL:
            continue
        return True
    for m in _DEFECT_RX.finditer(ft):
        prev, nxt = ft[:m.start()].split(), ft[m.end():].split()
        if _negated_before(prev) or _negated_after(nxt[:3]):
            continue
        return True
    return False


def _has_box_only(ft: str) -> bool:
    for m in _BOX_RX.finditer(ft):
        prev, nxt = ft[:m.start()].split(), ft[m.end():].split()
        if _negated_before(prev) or set(nxt[:2]) & _BOX_POST_OK:
            continue
        return True
    return False


_WANTED_FIRST = frozenset({"suche", "suchen", "sucht", "kaufe", "kaufen", "ankauf", "gesucht", "wtb", "wanted",
                           "sofortankauf"})
_WANTED_ANY = frozenset({"gesucht", "ankauf", "wanted", "sofortankauf"})
_OFFER_WORDS = frozenset({"verkaufe", "verkauf", "biete", "tausche", "tausch"})


def _is_wanted(toks: tuple[str, ...]) -> bool:
    if not toks:
        return False
    if toks[0] in _WANTED_FIRST or _WANTED_ANY.intersection(toks):
        return True
    if list(toks[:2]) in (["ich", "suche"], ["wir", "suchen"], ["ich", "kaufe"], ["wir", "kaufen"]):
        return True
    if "suche" in toks:
        i = toks.index("suche")
        return not _OFFER_WORDS.intersection(toks[:i])
    return False


_SERVICE_TOKENS = frozenset({"reparaturservice", "handyreparatur", "displayreparatur", "repariere", "reparieren",
                             "vermiete", "vermietung", "verleih", "verleihe", "mietgeraet", "mieten",
                             "reinigungsservice", "installationsservice", "einrichtungsservice"})
_SERVICE_RE = re.compile(r"\b(?:reparatur service|service reparatur|biete reparatur|reparatur von|reparatur aller|"
                         r"reparatur fuer|zu vermieten|display reparatur|akku reparatur|display tausch service|"
                         r"akkutausch service|reparatur und)\b|^reparatur\b")


def _is_service(s: str, toks: tuple[str, ...]) -> bool:
    return bool(_SERVICE_TOKENS.intersection(toks) or _SERVICE_RE.search(s))


def _is_swap(s: str, toks: tuple[str, ...], desc_ft: str) -> bool:
    title_swap = bool(toks) and (toks[0] in ("tausche", "tausch") or bool(re.search(
        r"\b(?:tausche?|swap|trade) (?:\w+ ){0,4}gegen\b|\bnur tausch", s)))
    if title_swap and not _SWAP_CANCEL.intersection(toks):
        return True
    for m in _SWAP_DESC_RX.finditer(desc_ft):
        prev, nxt = desc_ft[:m.start()].split(), desc_ft[m.end():].split()
        if _negated_before(prev) or _SWAP_CANCEL.intersection(nxt[:4]) or "oder" in nxt[:2]:
            continue
        return True
    return False


_PC_WORDS = frozenset({"pc", "gamingpc", "rechner", "gamingrechner", "computer", "gamingcomputer",
                       "komplettsystem", "komplettpc", "tower", "workstation", "barebone", "setup", "server",
                       "aufruestpc"})
_LAPTOP_WORDS = frozenset({"laptop", "notebook", "ultrabook", "netbook", "chromebook", "gaminglaptop",
                           "gamingnotebook", "macbook"})
_NOT_ITSELF_BEFORE = frozenset({"fuer", "fuers", "for", "aus", "im", "in", "vom", "von", "zum", "ausgebaut",
                                "kompatibel", "passend", "dem", "den", "einem", "meinem", "midi", "big", "full",
                                "micro"})
_FROM_PC = frozenset({"fuer", "fuers", "for", "aus", "ausgebaut", "vom", "kompatibel", "passend"})
_COMPONENT_AFTER = frozenset(
    "gehaeuse case netzteil ram arbeitsspeicher ssd festplatte hdd kuehler cooler luefter tasche rucksack sleeve "
    "huelle ladegeraet ladekabel akku display tastatur maus spiel spiele game games kabel adapter lautsprecher "
    "monitor bildschirm mainboard cpu prozessor grafikkarte gpu halterung staender controller headset "
    "schreibtisch stuhl tisch dock dockingstation wasserkuehlung netzkabel fan fans mauspad scharnier".split())

_ACCESSORY = frozenset(
    """huelle schutzhuelle handyhuelle silikonhuelle lederhuelle klapphuelle case cover bumper backcover folio
    panzerglas panzerfolie schutzfolie displayschutz displayschutzfolie schutzglas folie ladekabel ladegeraet
    schnellladegeraet netzteil ladestation ladepad ladeschale ladedock dockingstation dock adapter kabel
    halterung staender standfuss wandhalterung tasche sleeve skin skins aufkleber sticker armband armbaender
    uhrenarmband strap ohrpolster earpads ladecase kuehler cooler wasserkuehler wasserkuehlung wasserblock
    waterblock eisblock eiswolf backplate riser bracket shroud controller gamepad joycon joycons headset lenkrad
    fernbedienung spiel spiele game games videospiel buerste elektrobuerste duese filter aufsatz keyboard
    tastaturhuelle stylus ersatzakku zusatzakku akkupack powerbank objektivdeckel gegenlichtblende grip
    abdeckung faceplate coverplatte""".split())
_PART = frozenset(
    """display bildschirm lcd digitizer touchscreen akku batterie mainboard platine logicboard rueckseite
    rueckglas backglass kameraglas kameralinse kameramodul rahmen gehaeuse flexkabel ladebuchse ladeport
    lautsprecher hoermuschel mikrofon tastatur scharnier laufwerk luefter ersatzteil ersatzteile ersatz
    ersatzdisplay displayeinheit simkartenhalter lesekopf topcase palmrest kamera""".split())
# categories where these part words are normal features of the device ("iPad Air 5 Display 10,9")
_BUILTIN_PARTS_OK = frozenset({"laptop", "tablet", "desktop", "console", "camera", "drone", "handheld", "other"})
_PART_STRONG_NEXT = frozenset({"fuer", "fuers", "passend", "kompatibel", "ersatz", "ersatzteil", "reparatur"})
_FEATURE_BEFORE = frozenset(
    """mit inkl inklusive incl und sowie samt zzgl ohne neu neue neuer neuem neuen neues neuwertig neuwertiges
    getauscht getauschter getauschtem gewechselt gewechseltem erneuert erneuertem original originale originaler
    originalem originalen orig top perfekt perfektem intakt intaktem einwandfreiem gutem guter retina liquid amoled
    super full hd 4k ips matt entspiegelt entspiegeltes touch beleuchtete beleuchteter beleuchtet deutsche
    deutscher deutsches qwertz qwerty de uk us magic dynamic promotion xdr oled 120hz 144hz 165hz 240hz 60hz
    grosses grosser starker starkem""".split())
_FEATURE_AFTER = frozenset("neu getauscht gewechselt erneuert top ok einwandfrei perfekt intakt kapazitaet zustand "
                           "health gesundheit maximale max bei".split())
# "Akku-Schlagschrauber", "Akku Staubsauger": cordless device, not a battery
_AKKU_DEVICE = frozenset(
    """schrauber schlagschrauber bohrschrauber bohrmaschine bohrhammer staubsauger sauger handstaubsauger
    stabstaubsauger rasenmaeher heckenschere saege kreissaege stichsaege saebelsaege kettensaege flex
    winkelschleifer schleifer geblaese laubblaeser trimmer rasentrimmer lampe leuchte multitool wischer
    schlagbohrschrauber kombihammer schrauberset set""".split())
_ADDON_CONNECTORS = frozenset({"und", "mit", "inkl", "inklusive", "incl", "sowie", "samt", "zzgl", "nebst", "dazu"})
_FOR_WORDS = frozenset({"fuer", "fuers", "for", "passend", "kompatibel"})
_BUNDLE_WORDS = frozenset({"bundle", "konvolut", "sammlung", "paket", "lot", "aufruestkit", "aufruestset",
                           "zubehoerpaket", "zubehoerset", "spielepaket", "starterpaket", "starterset",
                           "komplettpaket", "komplettset", "zubehoerbundle", "spielebundle"})
_MOBILE_CPU_RE = re.compile(r"\b\d{4,5}(?:h|hs|hx|hk|u)\b|\blaptop gpu\b|\bmobile gpu\b|\bmax q\b")


def _addon_kind(toks: tuple[str, ...], hit: _Hit | None) -> str | None:
    """'part' / 'accessory' when the title sells something for the product, not the product."""
    anchor = hit.start if hit else None
    category = hit.category if hit else "other"
    if anchor is not None:
        fi = next((i for i, t in enumerate(toks[:anchor]) if t in _FOR_WORDS), None)
        if fi is not None and fi > 0 and anchor - fi <= 4 and any(
                t not in _FILLER and t not in _BRANDS for t in toks[:fi]):
            return "part" if any(t in _PART for t in toks[:fi]) else "accessory"
    for i, w in enumerate(toks):
        is_part = w in _PART
        if not is_part and w not in _ACCESSORY:
            continue
        if w == "akku" and i + 1 < len(toks) and toks[i + 1] in _AKKU_DEVICE:
            continue
        if w == "kamera" and category not in ("phone", "tablet", "laptop"):
            continue
        prev2 = toks[max(0, i - 2):i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        after = anchor is not None and i > anchor
        if after:
            if _ADDON_CONNECTORS.intersection(toks[anchor:i]) or _FEATURE_BEFORE.intersection(prev2):
                continue
            if nxt in _FEATURE_AFTER or (nxt.isdigit() and w in ("akku", "batterie")):
                continue
            if is_part and category in _BUILTIN_PARTS_OK and nxt not in _PART_STRONG_NEXT and \
                    toks[i - 1] != "ersatz":
                continue
        else:
            if _ADDON_CONNECTORS.intersection(prev2) or {"ohne"}.intersection(prev2):
                continue
            if nxt in _FEATURE_AFTER or (nxt.isdigit() and w in ("akku", "batterie")):
                continue
        return "part" if is_part else "accessory"
    return None


def _is_complete_pc(s: str, toks: tuple[str, ...]) -> bool:
    for i, t in enumerate(toks):
        if t not in _PC_WORDS:
            continue
        prv = toks[i - 1] if i else ""
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if prv in _NOT_ITSELF_BEFORE or nxt in _COMPONENT_AFTER or (t == "tower" and nxt in _PC_WORDS):
            continue
        if _FROM_PC.intersection(toks[max(0, i - 2):i]) or nxt in ("ausgebaut", "entnommen"):
            continue  # "RTX 3080 aus Gaming PC ausgebaut", "Kühler für Gaming PC"
        return True
    # "Ryzen 7 5800X, RTX 3080, 32GB RAM" without the word PC
    return bool(_m_gpu(s, toks) and _m_cpu(s, toks) and not _BUNDLE_WORDS.intersection(toks))


def _is_laptop(s: str, toks: tuple[str, ...], hit: _Hit | None) -> bool:
    """Laptop words, or a GPU/CPU title that looks like a laptop (mobile CPU, 15.6" screen)."""
    for i, t in enumerate(toks):
        if t in _LAPTOP_WORDS:
            prv = toks[i - 1] if i else ""
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if prv in _NOT_ITSELF_BEFORE or nxt in _COMPONENT_AFTER:
                continue
            return True
    if hit and hit.category in ("gpu", "cpu"):
        if _MOBILE_CPU_RE.search(s):
            return True
        if any((v := _inch(t)) is not None and 13 <= v <= 18.4 and not t.isdigit() for t in toks):
            return True  # "15.6zoll" next to a GPU/CPU
    return False


def _is_bundle(toks: tuple[str, ...], hit: _Hit | None) -> bool:
    if _BUNDLE_WORDS.intersection(toks):
        return True
    anchor = hit.start if hit else 0
    if any(re.fullmatch(r"[2-9]x", t) for t in toks[:anchor + 1]) and anchor:
        return True
    if re.search(r"\b(?:[2-9]|1\d) (?:stueck|stk)\b", " ".join(toks)):
        return True
    if hit and hit.category in ("console", "handheld"):
        tail = toks[anchor:]
        for i, t in enumerate(tail):
            if t not in _ADDON_CONNECTORS:
                continue
            window = tail[i + 1:i + 6]
            if {"spiel", "spiele", "spielen", "games", "game"}.intersection(window):
                return True
            for j, w in enumerate(window):
                if w in ("controller", "controllern", "gamepads", "joycons") and j and (
                        window[j - 1] in ("2", "3", "4", "zwei", "drei", "vier", "zweiter", "zweitem", "extra")
                        or window[j - 1].startswith("zusaetzlich")):
                    return True
        if re.search(r"\b\d{1,2} (?:spiele|spielen|games)\b", " ".join(tail)):
            return True
    return False


# ---------------------------------------------------------------------------
# Missing parts ("ohne Akku", "SSD fehlt", "nur Tablet")
# ---------------------------------------------------------------------------

# component word -> canonical name
_MISSING_VOCAB = {
    "akku": "akku", "akkus": "akku", "batterie": "akku",
    "netzteil": "netzteil", "ladegeraet": "netzteil", "ladekabel": "netzteil", "lader": "netzteil",
    "netzkabel": "netzteil",
    "ssd": "ssd", "festplatte": "ssd", "hdd": "ssd",
    "ram": "ram", "arbeitsspeicher": "ram",
    "controller": "controller", "controllern": "controller", "gamepad": "controller",
    "joycons": "joycons", "joycon": "joycons",
    "dock": "dock", "dockingstation": "dock",
    "fernbedienung": "fernbedienung",
    "mainboard": "mainboard", "motherboard": "mainboard", "logicboard": "mainboard",
    "display": "display", "bildschirm": "display",
    "cpu": "cpu", "prozessor": "cpu",
    "grafikkarte": "gpu", "gpu": "gpu",
    "kuehler": "kuehler", "luefter": "kuehler",
    "tastatur": "tastatur",
    "kabel": "kabel",
    "ladestation": "ladestation",
    "ladecase": "ladecase",
}
_MISSING_AFTER = frozenset({"fehlt", "fehlen", "ausgebaut", "entfernt"})
_MISSING_AFTER_NICHT = frozenset({"dabei", "enthalten", "vorhanden", "inklusive", "inkl", "beiliegend"})
_NOT_MISSING_NEXT = frozenset(
    "problem probleme problemen schaden schaeden verschleiss tausch wechsel fehler defekt kratzer "
    "gebrauchsspuren mangel maengel".split())
_MISSING_SKIP = frozenset({"original", "originales", "originalen", "das", "den", "die", "der", "eigenes", "eigene",
                           "eigenen", "passendes", "passende", "passenden"})
# missing components that make the offer a part rather than the product, by category
_MAJOR_MISSING: dict[str, frozenset[str]] = {
    "phone": frozenset({"akku", "display", "mainboard"}),
    "tablet": frozenset({"akku", "display", "mainboard"}),
    "watch": frozenset({"akku", "display", "mainboard"}),
    "laptop": frozenset({"ssd", "ram", "display", "mainboard", "cpu", "tastatur"}),
    "desktop": frozenset({"ssd", "ram", "mainboard", "cpu"}),
    "console": frozenset({"ssd", "mainboard", "dock", "joycons"}),
    "handheld": frozenset({"ssd", "display", "mainboard"}),
    "gpu": frozenset({"kuehler"}),
    "vacuum": frozenset({"akku", "mainboard"}),
    "drone": frozenset({"fernbedienung", "controller", "akku"}),
    "audio": frozenset({"ladecase", "akku"}),
    "other": frozenset({"mainboard", "display"}),
}


def _missing_in(ft: str) -> list[str]:
    toks = ft.split()
    out: list[str] = []
    for i, t in enumerate(toks):
        if t == "ohne" or (t in ("kein", "keine", "keinen") and i + 1 < len(toks)):
            j = i + 1
            while j < len(toks) and toks[j] in _MISSING_SKIP:
                j += 1
            while j < len(toks) and toks[j] in _MISSING_VOCAB:
                if j + 1 < len(toks) and toks[j + 1] in _NOT_MISSING_NEXT:
                    break
                out.append(_MISSING_VOCAB[toks[j]])
                j += 1
                if j + 1 < len(toks) and toks[j] in ("und", "oder", "u", "bzw", "sowie") \
                        and toks[j + 1] in _MISSING_VOCAB:
                    j += 1
        elif t in _MISSING_VOCAB and i + 1 < len(toks):
            nxt = toks[i + 1]
            nxt2 = toks[i + 2] if i + 2 < len(toks) else ""
            if nxt in ("wurde", "ist", "sind", "wurden") and nxt2 in _MISSING_AFTER:
                nxt = nxt2
            if nxt in _MISSING_AFTER or (nxt == "nicht" and nxt2 in _MISSING_AFTER_NICHT):
                if not _negated_before(toks[:i]):
                    out.append(_MISSING_VOCAB[t])
    return out


def missing_parts(title: str, description: str = "") -> tuple[str, ...]:
    """Components the ad says are missing: 'MacBook Pro ohne SSD' -> ('ssd',),
    'Switch OLED nur Tablet' -> ('dock', 'joycons'), 'DeWalt DCD796 solo' -> ('akku',).
    Names: akku netzteil ssd ram controller joycons dock fernbedienung mainboard display cpu
    gpu kuehler tastatur kabel ladestation ladecase."""
    if not title:
        return ()
    _, hit = _analyze(title)
    found = _missing_in(_flag_tokens(title))
    if description:
        found += _missing_in(_flag_tokens(description))
    s = " ".join(_tokens(title))
    key = hit.key if hit else None
    switch = key is not None and key.family == "nintendo switch"
    if switch and key.model != "lite" and re.search(
            r"\bnur (?:das |die )?(?:tablet|display|konsole|geraet|handheld)\b", s):
        found += ["dock", "joycons"]  # the Switch without dock and Joy-Cons
    elif hit and hit.category == "console" and not switch and re.search(
            r"\bnur (?:die |das )?(?:konsole|geraet)\b", s):
        found.append("controller")
    if key and "solo" in key.variant:
        found.append("akku")
    return tuple(sorted(set(found)))


def _major_missing(missing: tuple[str, ...], hit: _Hit | None) -> bool:
    if not missing or hit is None:
        return False
    return bool(_MAJOR_MISSING.get(hit.category, frozenset()).intersection(missing))


_ADDON = ("part", "accessory")


def classify_kind(title: str, description: str = "", *, query_kind_hint: str | None = None) -> Kind:
    """What an ad offers. Title rules (priority order): wanted > service > box_only > swap >
    defect > complete_pc > part (major component missing, see missing_parts) > laptop >
    part/accessory > bundle > item. The description is only used for strong signals (defect,
    box only, swap only, missing components), with negations and legal disclaimers
    ("keine Rücknahme bei Defekten") ignored.

    query_kind_hint: the kind that is normal for the product being priced ("laptop" for a
    ThinkPad, "complete_pc" for a Mac mini, "part" when the search itself is for a part).
    A title of that kind is returned as "item" (part and accessory count as one)."""
    if not title or not title.strip():
        return "unknown"
    toks, hit = _analyze(title)
    if not toks:
        return "unknown"
    s = " ".join(toks)
    title_ft = _flag_tokens(title)
    desc_ft = _flag_tokens(description) if description else ""
    kind: str
    if _is_wanted(toks):
        kind = "wanted"
    elif _is_service(s, toks):
        kind = "service"
    elif _has_box_only(title_ft) or (desc_ft and _has_box_only(desc_ft)):
        kind = "box_only"
    elif _is_swap(s, toks, desc_ft):
        kind = "swap"
    elif _has_defect(title_ft) or (desc_ft and _has_defect(desc_ft)):
        kind = "defect"
    elif _is_complete_pc(s, toks) and not (hit and hit.category in ("laptop",)):
        kind = "complete_pc"
    elif _major_missing(missing_parts(title, description), hit):
        kind = "part"  # "MacBook Pro ohne SSD", "Dyson V11 ohne Akku", "Switch OLED nur Tablet"
    elif _is_laptop(s, toks, hit):
        kind = "laptop"
    elif (addon := _addon_kind(toks, hit)) is not None:
        kind = addon
    elif _is_bundle(toks, hit):
        kind = "bundle"
    elif hit and hit.category == "laptop":
        kind = "laptop"  # a ThinkPad is a laptop even without the word
    elif hit and hit.category == "desktop":
        kind = "complete_pc"  # Mac mini, iMac
    else:
        kind = "item"
    if query_kind_hint and (kind == query_kind_hint or (kind in _ADDON and query_kind_hint in _ADDON)):
        return "item"
    return kind  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Comparables filter
# ---------------------------------------------------------------------------

_LAPTOP_FAMILY_PREFIXES = ("macbook", "thinkpad", "dell", "hp", "lenovo", "asus", "acer", "msi", "surface laptop",
                           "surface book", "surface pro", "razer blade", "alienware", "gigabyte aero", "galaxy book")
_DESKTOP_FAMILIES = frozenset({"mac mini", "imac", "mac studio", "mac pro"})
_NEVER_COMPARABLE = frozenset({"wanted", "service", "box_only", "swap"})


def natural_kind(key: ProductKey | None, category: str | None = None) -> str | None:
    """Kind that is normal for this product ('laptop' for a ThinkPad, 'complete_pc' for a Mac mini)."""
    if category == "laptop" or (key and key.family.startswith(_LAPTOP_FAMILY_PREFIXES)):
        return "laptop"
    if category == "desktop" or (key and key.family in _DESKTOP_FAMILIES):
        return "complete_pc"
    return None


def _kind_class(kind: str) -> str:
    return "addon" if kind in _ADDON else ("item" if kind == "unknown" else kind)


def comparable_matches(
    query_title_or_key: str | ProductKey, candidate_title: str, candidate_description: str = ""
) -> tuple[bool, str]:
    """Is `candidate_title` the same product and the same kind of offer as the query
    (a title, a short query like "rtx 3080", or a ProductKey)? Returns (ok, reason).
    Drop-in replacement for estimator.comparable_is_relevant."""
    if isinstance(query_title_or_key, ProductKey):
        qk, q_title, q_cat = query_title_or_key, "", None
    else:
        q_title = query_title_or_key or ""
        _, q_hit = _analyze(q_title) if q_title else ((), None)
        qk, q_cat = (q_hit.key, q_hit.category) if q_hit else (None, None)
    hint = natural_kind(qk, q_cat)
    q_kind = classify_kind(q_title, query_kind_hint=hint) if q_title else "item"
    c_kind = classify_kind(candidate_title, candidate_description, query_kind_hint=hint)
    if c_kind in _NEVER_COMPARABLE:
        return False, f"candidate is {c_kind}"
    if c_kind == "defect" and q_kind != "defect":
        return False, "candidate is defect"
    if _kind_class(c_kind) != _kind_class(q_kind):
        return False, f"kind {c_kind} != {q_kind}"
    if qk is None:
        return _generic_word_match(q_title, candidate_title)
    ck = product_key(candidate_title)
    if ck is None:
        return False, "no model in candidate title"
    reason = mismatch_reason(qk, ck)
    if reason:
        return False, reason
    return True, f"same product {ck.key()}"


def _generic_word_match(query: str, title: str) -> tuple[bool, str]:
    """Fallback for queries without a model token: every meaningful query word must appear."""
    q_words = [t for t in _tokens(query) if t not in _FILLER and t not in _COLORS and t not in _BRANDS]
    if not q_words:
        return False, "query has no identity"
    t_words = _tokens(title)
    missing = [w for w in q_words if not any(t.startswith(w) for t in t_words)]
    if missing:
        return False, f"missing {' '.join(missing)!r}"
    return True, "generic query: all words present"


# ---------------------------------------------------------------------------
# One-call summary for the engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Everything the pricing engine needs to know about one ad."""

    key: ProductKey | None
    kind: str  # a Kind; the product's natural kind (laptop for a ThinkPad) is reported as "item"
    category: str | None
    missing: tuple[str, ...] = ()

    @property
    def priceable(self) -> bool:
        """Can this ad be compared with the market price of its product?"""
        return self.key is not None and self.kind == "item"

    def history_key(self) -> str | None:
        """Key to remember this ad's price under: the product key for plain items, a kind-prefixed
        key for bundles/PCs/parts/defects ('accessory:rtx|3080' never pollutes 'rtx|3080'), None
        when the ad is no price signal (wanted, swap, service, box only) or names no model."""
        if self.key is None or self.kind in _NEVER_COMPARABLE or self.kind == "unknown":
            return None
        k = self.key.key()
        return k if self.kind == "item" else f"{self.kind}:{k}"


def identify(title: str, description: str = "") -> Identity:
    """ProductKey + kind (natural kind folded into "item") + missing components of one ad."""
    _, hit = _analyze(title) if title else ((), None)
    key = hit.key if hit else None
    category = hit.category if hit else None
    kind = classify_kind(title, description, query_kind_hint=natural_kind(key, category))
    return Identity(key, kind, category, missing_parts(title, description))


def history_key(title: str, description: str = "") -> str | None:
    """Shortcut for identify(title, description).history_key()."""
    return identify(title, description).history_key()
