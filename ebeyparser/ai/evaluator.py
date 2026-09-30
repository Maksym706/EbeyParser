"""Ask the vision LLM about a listing and turn its (possibly messy) answer into an AIVerdict.

The answer is a structured extraction (see prompts.VERDICT_SCHEMA). Small local models
break the format in creative ways, so every field is coerced tolerantly and
parse_verdict() never raises.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

from ..config import LLMSettings
from ..models import AIVerdict, Comparable, Listing, PriceEstimate
from ..pricing.text import normalize
from .client import IMAGE_MAX_SIDE, ChatModel, LLMError, prepare_images
from .prompts import (
    DEFECTS,
    ITEM_TYPES,
    MAX_DEFECTS,
    MAX_RED_FLAGS,
    SYSTEM_PROMPT,
    VARIANT_FIELDS,
    VERDICT_SCHEMA,
    build_user_prompt,
    prompt_comparables,
)

log = logging.getLogger(__name__)

PARSE_FAILED = "Не удалось разобрать ответ модели"
# AIVerdict.variant key holding the model's matches among the shown comparables:
# "0,3,5" (indexes into prompts.prompt_comparables()), "" = none match, absent = unknown.
SAME_VARIANT_KEY = "same_variant_indexes"

# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _balanced_objects(text: str) -> list[str]:
    """Top-level {...} substrings (string-aware brace matching), outermost first."""
    out: list[str] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            return out
        depth, in_str, esc, quote = 0, False, False, ""
        end = -1
        for j in range(start, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    in_str = False
            elif ch in "\"'":
                in_str, quote = True, ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:  # unbalanced: try the rest anyway (e.g. truncated output)
            out.append(text[start:])
            return out
        out.append(text[start : end + 1])
        i = end + 1


def _loads(candidate: str) -> dict | None:
    attempts = [candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)]
    for a in attempts:
        try:
            obj = json.loads(a)
            if isinstance(obj, dict):
                return obj
        except (ValueError, RecursionError):
            pass
    # Python-ish dicts: single quotes, True/False/None
    py = re.sub(r"\btrue\b", "True", attempts[1])
    py = re.sub(r"\bfalse\b", "False", py)
    py = re.sub(r"\bnull\b", "None", py)
    try:
        obj = ast.literal_eval(py)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        pass
    return None


_FIELD_RES = {
    key: re.compile(rf'["\']?{key}["\']?\s*[:=]\s*("(?:[^"\\]|\\.)*"|[^,\n}}]+)', re.IGNORECASE)
    for key in (
        "product",
        "item_type",
        "search_query",
        "photo_matches_description",
        "locked",
        "stock_photos",
        "condition",
        "estimated_market_price",
        "verdict",
        "confidence",
        "reasoning",
    )
}


def _salvage(text: str) -> dict | None:
    """Last resort for truncated/broken JSON: pick "key": value pairs with regexes."""
    found: dict[str, Any] = {}
    for key, rx in _FIELD_RES.items():
        m = rx.search(text)
        if m:
            raw = m.group(1).strip()
            if raw.startswith('"'):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = raw.strip('"')
            found[key] = raw
    return found if ("verdict" in found or "confidence" in found) else None


def _extract_object(text: str) -> dict | None:
    text = _THINK_RE.sub("", text).strip()
    fences = _FENCE_RE.findall(text)
    sources = [f.strip() for f in fences] + [text]
    for src in sources:
        obj = _loads(src)
        if obj is not None:
            return obj
        for cand in _balanced_objects(src):
            obj = _loads(cand)
            if obj is not None:
                return obj
    return _salvage(text)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

_KEY_ALIASES = {
    "product": "product",
    "produkt": "product",
    "item": "product",
    "product name": "product",
    "search query": "search_query",
    "query": "search_query",
    "suchbegriff": "search_query",
    "photo matches description": "photo_matches_description",
    "photos match description": "photo_matches_description",
    "photo matches": "photo_matches_description",
    "photos match": "photo_matches_description",
    "photo match": "photo_matches_description",
    "condition": "condition",
    "zustand": "condition",
    "состояние": "condition",
    "red flags": "red_flags",
    "flags": "red_flags",
    "warnings": "red_flags",
    "estimated market price": "estimated_market_price",
    "market price": "estimated_market_price",
    "estimated price": "estimated_market_price",
    "price estimate": "estimated_market_price",
    "marktpreis": "estimated_market_price",
    "verdict": "verdict",
    "decision": "verdict",
    "recommendation": "verdict",
    "вердикт": "verdict",
    "confidence": "confidence",
    "уверенность": "confidence",
    "reasoning": "reasoning",
    "reason": "reasoning",
    "explanation": "reasoning",
    "begruendung": "reasoning",
    "обоснование": "reasoning",
    "пояснение": "reasoning",
    # v0.2 extraction fields
    "item type": "item_type",
    "itemtype": "item_type",
    "listing type": "item_type",
    "ad type": "item_type",
    "angebotstyp": "item_type",
    "variant": "variant",
    "variante": "variant",
    "variant info": "variant",
    "defects": "defects",
    "defect": "defects",
    "defekte": "defects",
    "maengel": "defects",
    "damage": "defects",
    "damages": "defects",
    "дефекты": "defects",
    "locked": "locked",
    "is locked": "locked",
    "lock": "locked",
    "gesperrt": "locked",
    "stock photos": "stock_photos",
    "stock photo": "stock_photos",
    "stock images": "stock_photos",
    "stockfotos": "stock_photos",
    "same variant indexes": "same_variant_indexes",
    "same variant index": "same_variant_indexes",
    "same variant": "same_variant_indexes",
    "matching comparables": "same_variant_indexes",
    "matching indexes": "same_variant_indexes",
}


def _canonical_keys(obj: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        canon = _KEY_ALIASES.get(normalize(str(key)))
        if canon and canon not in out:
            out[canon] = value
    # models sometimes nest the answer, e.g. {"verdict": {...all fields...}}
    if len(out) <= 1:
        for value in obj.values():
            if isinstance(value, dict):
                nested = _canonical_keys(value)
                if len(nested) > len(out):
                    return nested
    return out


_NUM_RE = re.compile(r"\d[\d.,]*")


def _parse_number(token: str) -> float | None:
    tok = token.strip(".,")
    if not tok:
        return None
    if "." in tok and "," in tok:
        dec = "." if tok.rfind(".") > tok.rfind(",") else ","
        tok = tok.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "," in tok or "." in tok:
        sep = "," if "," in tok else "."
        head, _, tail = tok.rpartition(sep)
        if tok.count(sep) > 1 or len(tail) == 3:  # 1.200 / 1,200 / 1.200.000 = thousands
            tok = tok.replace(sep, "")
        else:
            tok = tok.replace(sep, ".")
    try:
        return float(tok)
    except ValueError:
        return None


def _to_price(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, dict):
        for k in ("value", "amount", "price", "eur"):
            if k in value:
                return _to_price(value[k])
        return None
    text = str(value)
    nums = [n for n in (_parse_number(t) for t in _NUM_RE.findall(text)) if n is not None]
    if not nums:
        return None
    if len(nums) >= 2 and re.search(r"\d\s*(?:-|–|—|bis|to|до)\s*\d", text):
        price = (nums[0] + nums[1]) / 2  # a range "400-500 €" -> middle
    else:
        price = nums[0]
    if re.search(r"\d\s*k\b", text, re.IGNORECASE) and price < 100:
        price *= 1000  # "1.2k"
    return round(price, 2) if price > 0 else None


_CONF_WORDS = (
    (("sehr hoch", "very high", "очень высок"), 0.9),
    (("hoch", "high", "высок"), 0.8),
    (("mittel", "medium", "moderate", "средн"), 0.5),
    (("niedrig", "gering", "low", "низк"), 0.25),
)


def _to_confidence(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip().lower()
        m = _NUM_RE.search(text)
        if m:
            num = _parse_number(m.group(0)) or 0.0
            if "%" in text:
                num /= 100
        else:
            for words, score in _CONF_WORDS:
                if any(w in text for w in words):
                    return score
            return 0.0
    if num > 1:
        num /= 100  # 80 -> 0.8
    return max(0.0, min(1.0, num))


_NEGATED_WORD_RE = re.compile(r"\b(?:без|ohne|kein\w*|nicht|no|not|non|without)\s+\S+")


def _to_condition(value: Any) -> str:
    text = normalize(str(value or ""))
    if not text:
        return "unclear"
    compact = text.replace(" ", "_")
    if compact in ("new", "like_new", "good", "used", "defective", "unclear"):
        return compact
    text = _NEGATED_WORD_RE.sub(" ", text)  # "без дефектов", "keine Mängel"
    if any(w in text for w in ("wie neu", "neuwertig", "like new", "как нов", "почти нов", "mint", "sehr gut", "отличн")):
        return "like_new"
    if any(w in text for w in ("defekt", "kaputt", "broken", "defective", "for parts", "faulty", "сломан", "неисправ", "дефект", "на запчаст", "bastler")):
        return "defective"
    if any(w in text for w in ("neu", "new", "нов", "sealed", "ovp", "unbenutzt")):
        return "new"
    if any(w in text for w in ("gut", "good", "хорош", "very good")):
        return "good"
    if {"бу", "ok", "okay"} & set(text.split()) or any(
        w in text
        for w in ("gebraucht", "used", "б у", "fair", "acceptable", "akzeptabel", "in ordnung", "удовлетвор", "поношен", "worn")
    ):
        return "used"
    return "unclear"


def _to_verdict(value: Any) -> str:
    text = normalize(str(value or ""))
    if text in ("buy", "maybe", "skip"):
        return text
    tokens = set(text.split())
    if any(p in text for p in ("не уверен", "not sure", "nicht sicher", "unsicher", "unsure")):
        return "maybe"
    if tokens & {"skip", "no", "nein", "нет", "pass", "avoid", "пропустить", "пропуск", "reject"} or any(
        p in text
        for p in ("nicht kaufen", "don t buy", "do not buy", "не покупать", "не брать", "не стоит", "finger weg", "nicht empfehlen")
    ):
        return "skip"
    if any(
        p in text
        for p in ("maybe", "vielleicht", "возможно", "может быть", "mittel", "abwarten", "uncertain", "neutral", "проверить", "unclear", "check")
    ):
        return "maybe"
    if tokens & {"yes", "ja", "да", "deal"} or any(
        p in text for p in ("kaufen", "купить", "покупать", "брать", "buy", "empfehl", "рекоменд")
    ):
        return "buy"
    return "maybe"


def _to_photo_match(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else None
    text = normalize(str(value))
    if not text or text in ("null", "none", "unclear", "unknown", "unbekannt", "неясно", "n a", "na"):
        return None
    tokens = set(text.split())
    if tokens & {"false", "no", "nein", "нет", "mismatch", "nicht"} or text.startswith("не "):
        return False
    if tokens & {"true", "yes", "ja", "да", "y", "match", "matches", "stimmt", "совпадает", "совпадают"}:
        return True
    return None


_NO_FLAG = {"", "нет", "none", "keine", "kein", "no", "n a", "na", "null", "ничего", "нету"}


def _to_flags(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        items: list[Any] = re.split(r"[,;\n•]+", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = next((item[k] for k in ("flag", "text", "description", "label") if k in item), None) or next(
                iter(item.values()), ""
            )
        text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", str(item)).strip().strip("\"'").strip()
        if normalize(text) in _NO_FLAG or not text:
            continue
        if text not in out:
            out.append(text[:200])
    return out[:MAX_RED_FLAGS]


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    """Short words match whole tokens ("set" not in "headset"), longer ones as substrings."""
    tokens = set(text.split())
    return any((w in text) if (" " in w or len(w) > 4) else (w in tokens) for w in words)


_ITEM_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("wanted", ("suche", "gesuch", "wanted", "kaufe", "ankauf", "looking for", "ищу", "куплю")),
    ("box_only", ("box only", "only box", "empty box", "nur ovp", "nur karton", "leerkarton", "nur verpackung",
                  "packaging", "коробка")),
    ("complete_pc", ("complete pc", "komplett pc", "gaming pc", "desktop", "komplettsystem", "rechner",
                     "компьютер", "системный блок")),
    ("laptop", ("laptop", "notebook", "macbook", "ноутбук")),
    ("bundle", ("bundle", "set", "konvolut", "paket", "lot", "набор", "комплект")),
    ("accessory", ("accessory", "accessories", "zubehoer", "case", "huelle", "charger", "ladegeraet", "kabel",
                   "cable", "controller", "аксессуар")),
    ("part", ("part", "parts", "ersatzteil", "spare", "component", "komponente", "запчаст", "деталь")),
    ("single", ("single", "einzel", "einzeln", "one item", "item", "один", "одиночн")),
)


def _to_item_type(value: Any) -> str:
    text = normalize(str(value or ""))
    compact = text.replace(" ", "_")
    if compact in ITEM_TYPES:
        return compact
    for item_type, words in _ITEM_TYPE_RULES:
        if _has_any(text, words):
            return item_type
    return "unclear"


_VARIANT_ALIASES = {
    "model": "model", "modell": "model", "name": "model", "product": "model",
    "storage": "storage_gb", "storage gb": "storage_gb", "speicher": "storage_gb", "capacity": "storage_gb",
    "ram": "ram_gb", "ram gb": "ram_gb", "memory": "ram_gb", "arbeitsspeicher": "ram_gb",
    "vram": "vram_gb", "vram gb": "vram_gb", "gpu memory": "vram_gb",
    "edition": "edition", "version": "edition",
}
_EMPTY_VALUES = {"", "null", "none", "unknown", "unbekannt", "n a", "na", "unclear", "неизвестно", "нет", "keine"}
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(tb|gb|mb)?", re.IGNORECASE)


def _gb(value: Any) -> str | None:
    """'128GB' -> '128', '1 TB' -> '1024', 16 -> '16'; None when no number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"{value:g}" if value > 0 else None
    m = _SIZE_RE.search(str(value))
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "gb").lower()
    number = number * 1024 if unit == "tb" else number / 1024 if unit == "mb" else number
    return f"{number:g}" if number > 0 else None


def _to_variant(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = _to_text(value, 80)
        return {"model": text} if normalize(text) not in _EMPTY_VALUES else {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        field = key if key in VARIANT_FIELDS else _VARIANT_ALIASES.get(normalize(str(key)))
        if field is None or field in out or raw is None or isinstance(raw, (dict, list)):
            continue
        if field.endswith("_gb"):
            gb = _gb(raw)
            if gb is not None:
                out[field] = gb
            continue
        text = _to_text(raw, 80)
        if normalize(text) not in _EMPTY_VALUES:
            out[field] = text
    return out


_DEFECT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("locked", ("icloud", "gesperrt", "locked", "lock", "sperre", "frp", "blacklist", "simlock", "заблок",
                "блокир")),
    ("water_damage", ("water", "wasser", "liquid", "feucht", "вод", "влаг")),
    ("screen_broken", ("screen", "display", "bildschirm", "glas", "riss", "crack", "cracked", "gebrochen",
                       "sprung", "экран", "дисплей", "трещин")),
    ("battery_bad", ("battery", "akku", "batterie", "аккумулятор", "батаре", "акб")),
    ("missing_parts", ("missing", "fehlt", "fehlen", "fehlend", "ohne", "unvollstaendig", "incomplete",
                       "отсутств", "без", "нет")),
    ("not_working", ("not working", "defekt", "kaputt", "broken", "geht nicht", "funktioniert nicht",
                     "startet nicht", "dead", "no power", "не работает", "не включ", "неисправ", "сломан")),
)


# "unlocked", "keine Mängel", "без дефектов": statements that there is NO defect
_NOT_A_DEFECT_RE = re.compile(
    r"\bunlocked\b|\bnot locked\b|\bentsperrt\b|simlockfrei|ohne simlock|\bno (?:defects?|damage|issues?)\b|"
    r"\bkeine? (?:defekte?|maengel|schaeden|kratzer)\b|без (?:дефектов|повреждений)|нет дефектов"
)


def _to_defects(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    items: list[Any] = re.split(r"[,;\n•]+", value) if isinstance(value, str) else (
        list(value) if isinstance(value, (list, tuple)) else [value])
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = next(iter(item.values()), "")
        text = normalize(str(item))
        if text in _NO_FLAG or text in _EMPTY_VALUES or _NOT_A_DEFECT_RE.search(text):
            continue
        compact = text.replace(" ", "_")
        defect = compact if compact in DEFECTS else next(
            (name for name, words in _DEFECT_RULES if _has_any(text, words)), "other")
        if defect not in out:
            out.append(defect)
    return out[:MAX_DEFECTS]


def _to_indexes(value: Any, n: int | None) -> list[int]:
    """Comparable numbers the model picked: [0, 2] / "0, 2" / ["[1]"] -> valid, unique, sorted."""
    raw: list[Any] = value if isinstance(value, (list, tuple)) else [value]
    found: set[int] = set()
    for item in raw:
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, (int, float)):
            numbers = [int(item)] if float(item).is_integer() else []
        else:
            numbers = [int(x) for x in re.findall(r"\d+", str(item))]
        found.update(i for i in numbers if i >= 0 and (n is None or i < n))
    return sorted(found)


def _to_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return " ".join(str(value).split())[:limit]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_verdict(text: str, model: str = "", n_comparables: int | None = None) -> AIVerdict:
    """Turn the model's answer into an AIVerdict. Never raises.

    `n_comparables` = how many comparable offers the prompt listed; picked indexes outside
    that range are dropped. The picks go to variant["same_variant_indexes"] as "0,2"
    ("" = the model matched none; absent = not asked / not answered)."""
    try:
        obj = _extract_object(text or "")
        data = _canonical_keys(obj) if obj else {}
        if not data:
            return AIVerdict(verdict="maybe", confidence=0.0, reasoning=PARSE_FAILED, model=model)
        query = _to_text(data.get("search_query"), 80)
        query = " ".join(query.split()[:8])
        variant = _to_variant(data.get("variant"))
        if "same_variant_indexes" in data and n_comparables != 0:
            indexes = _to_indexes(data.get("same_variant_indexes"), n_comparables)
            variant[SAME_VARIANT_KEY] = ",".join(str(i) for i in indexes)
        defects = _to_defects(data.get("defects"))
        locked = _to_photo_match(data.get("locked"))
        if locked is None and "locked" in defects:
            locked = True
        if locked is True and "locked" not in defects and len(defects) < MAX_DEFECTS:
            defects.append("locked")
        return AIVerdict(
            product=_to_text(data.get("product"), 200),
            search_query=query,
            photo_matches_description=_to_photo_match(data.get("photo_matches_description")),
            condition=_to_condition(data.get("condition")),  # type: ignore[arg-type]
            red_flags=_to_flags(data.get("red_flags")),
            estimated_market_price=_to_price(data.get("estimated_market_price")),
            verdict=_to_verdict(data.get("verdict")),  # type: ignore[arg-type]
            confidence=_to_confidence(data.get("confidence")),
            reasoning=_to_text(data.get("reasoning"), 1500),
            model=model,
            item_type=_to_item_type(data.get("item_type")),  # type: ignore[arg-type]
            variant=variant,
            defects=defects,
            locked=locked,
            stock_photos=_to_photo_match(data.get("stock_photos")),
        )
    except Exception:  # noqa: BLE001 - parsing must never break the pipeline
        log.exception("parse_verdict failed")
        return AIVerdict(verdict="maybe", confidence=0.0, reasoning=PARSE_FAILED, model=model)


def same_variant_comparables(verdict: AIVerdict | None, comparables: list[Comparable]) -> list[Comparable] | None:
    """The comparables the model called the same variant. `comparables` must be the list
    shown in the prompt (prompts.prompt_comparables(estimate)). None = the model gave no
    answer (use your own matching); [] = it matched none of them."""
    if verdict is None or SAME_VARIANT_KEY not in verdict.variant:
        return None
    picked = [int(x) for x in verdict.variant[SAME_VARIANT_KEY].split(",") if x.strip().isdigit()]
    return [comparables[i] for i in picked if 0 <= i < len(comparables)]


class AIEvaluator:
    """Runs the listing check on any client with chat_json() (VisionLLM, ClaudeVision)."""

    def __init__(self, llm: ChatModel, cfg: LLMSettings):
        self.llm = llm
        self.cfg = cfg

    async def evaluate(
        self,
        listing: Listing,
        images: list[bytes],
        *,
        purpose: str = "resale",
        estimate: PriceEstimate | None = None,
        target_price: float | None = None,
        comparables: list[Comparable] | None = None,
    ) -> AIVerdict:
        """Check one listing. The estimate's market price is never shown to the model; only
        up to 8 of its comparables (or `comparables`), see prompts.prompt_comparables()."""
        imgs = [img for img in (images or []) if img][: max(0, self.cfg.max_images)]
        imgs = prepare_images(imgs, max_side=int(getattr(self.cfg, "image_max_side", None) or IMAGE_MAX_SIDE))
        comps = prompt_comparables(estimate, comparables)
        shown = listing
        if getattr(self.llm, "cloud", ""):
            # a cloud model sees the ad only: no seller name / ratings, no postal code, no contacts
            from .cloud import cloud_listing

            shown = cloud_listing(listing)
        prompt = build_user_prompt(
            shown,
            purpose=purpose,
            target_price=target_price,
            n_images=len(imgs),
            comparables=comps,
        )
        try:
            answer = await self.llm.chat_json(SYSTEM_PROMPT, prompt, imgs, VERDICT_SCHEMA)
        except LLMError as exc:
            log.warning("AI check failed for %s (%s): %s", listing.ad_id, self.cfg.model, exc)
            message_ru = getattr(exc, "message_ru", "")
            if message_ru:  # the cloud's free quota / the eBay rule: already plain Russian
                return AIVerdict(verdict="maybe", confidence=0.0, reasoning=f"Нейросеть не проверила фото: {message_ru}",
                                 model=self.cfg.model)
            return self._unavailable(str(exc))
        except Exception as exc:  # noqa: BLE001 - never let the model break a monitoring run
            log.exception("AI check crashed for %s", listing.ad_id)
            return self._unavailable(f"{type(exc).__name__}: {exc}")
        verdict = parse_verdict(answer, model=self.cfg.model, n_comparables=len(comps))
        if not imgs:  # nothing to look at
            verdict.photo_matches_description = None
            verdict.stock_photos = None
        return verdict

    def _unavailable(self, error: str) -> AIVerdict:
        """The model did not answer: the deal view shows this reasoning, so it is plain Russian
        with the fix (the technical error is in the log)."""
        from ..errors_ru import ai_problem

        why = ai_problem(self.cfg.provider, getattr(self.cfg, "base_url", ""), self.cfg.model, error, server_ok=None)
        return AIVerdict(
            verdict="maybe",
            confidence=0.0,
            reasoning=f"Нейросеть не проверила фото: {why}",
            model=self.cfg.model,
        )
