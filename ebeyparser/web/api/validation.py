"""Semantic checks of a search on top of the pydantic model (same rules as the old form)."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ...config import SearchConfig


MAX_PRICE = 1_000_000.0


def search_field_problems(search: SearchConfig) -> dict[str, str]:
    """{field: Russian message} — empty when the search can run."""
    errors: dict[str, str] = {}
    if not search.name.strip():
        errors["name"] = "Укажи название поиска"
    source = search.source
    if search.url:
        parts = urlsplit(search.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            errors["url"] = "Ссылка должна начинаться с https://"
        elif source == "kleinanzeigen" and "kleinanzeigen.de" not in parts.netloc:
            errors["url"] = "Ссылка должна вести на kleinanzeigen.de"
    if source == "ebay":
        if not search.query and not search.ebay_category_ids:
            errors["query"] = "Для eBay укажи запрос или категорию eBay"
        if search.local_pickup_only and not (search.location and search.radius_km):
            errors["location"] = "Для самовывоза укажи почтовый индекс и радиус"
    elif not (search.url or search.query or search.category_id):
        errors["query"] = "Укажи ссылку с kleinanzeigen.de, слова для поиска или категорию"
    if search.min_price is not None and search.max_price is not None and search.min_price > search.max_price:
        errors["min_price"] = "«Цена от» больше, чем «Цена до»"
    if search.purpose == "personal" and search.target_price is not None and search.max_price is not None \
            and search.target_price > search.max_price:
        errors["max_price"] = "«Показывать до» меньше, чем «Готов заплатить» — подними его или снизь цель"
    for key in ("min_price", "max_price", "reference_price", "target_price", "min_profit", "min_roi",
                "ending_within_hours"):
        value = getattr(search, key)
        if value is not None and not math.isfinite(value):
            errors[key] = "Нужно число"
        elif value is not None and value < 0:
            errors[key] = "Не может быть меньше нуля"
        elif value is not None and key != "min_roi" and value > MAX_PRICE:
            errors[key] = "Слишком большое число — проверь, нет ли лишних нулей"
    if search.min_roi is not None and search.min_roi > 10:
        errors["min_roi"] = "ROI — от 0 до 1000 %"
    if search.radius_km is not None and not 0 <= search.radius_km <= 500:
        errors["radius_km"] = "Радиус от 0 до 500 км"
    if search.max_pages is not None and not 1 <= search.max_pages <= 20:
        errors["max_pages"] = "От 1 до 20 страниц"
    return errors


def search_problems(search: SearchConfig) -> list[str]:
    return list(search_field_problems(search).values())


__all__ = ["parse_search_url", "search_field_problems", "search_problems"]


_CODE_RE = re.compile(r"^(?:k(?P<k>\d+))?(?:c(?P<c>\d+))?(?:-?l(?P<l>\d+))?(?:-?r(?P<r>\d+))?$", re.IGNORECASE)
_PRICE_RE = re.compile(r"^preis:(?P<min>\d*):(?P<max>\d*)$", re.IGNORECASE)
NOT_KLEINANZEIGEN_RU = "Это не ссылка Kleinanzeigen — открой поиск на kleinanzeigen.de и скопируй адрес из браузера"
# Kleinanzeigen location ids of the biggest cities (the slug in the URL is enough elsewhere)
KA_LOCATION_IDS = {3331: "Berlin", 9409: "Hamburg", 6411: "München", 945: "Köln", 4292: "Frankfurt am Main",
                   9280: "Stuttgart", 2068: "Düsseldorf", 4233: "Leipzig", 1085: "Dortmund", 1921: "Essen",
                   1: "Bremen", 3820: "Dresden", 3155: "Hannover", 5967: "Nürnberg"}


def _place_name(slug: str) -> str:
    """'berlin' -> 'Berlin', 'neukoelln' -> 'Neukölln' (the built-in places), else Title Case."""
    from .locations import _fold, search_places

    words = slug.replace("-", " ").replace("_", " ").strip()
    if not words:
        return ""
    folded = _fold(words, "ae")
    for place in search_places(words, 5):
        if _fold(place["name"], "ae") == folded or _fold(place["name"], "a") == _fold(words, "a"):
            return str(place["name"])
    return words.title()


def parse_search_url(url: str) -> dict[str, Any]:
    """What a pasted search URL filters on: category, place, radius, price, words.

    Kleinanzeigen: /s-<category>/<place>/<price>/<words>/k0c<cat>l<place id>r<km> — every
    part optional; the code at the end says which ones are there, so they are read from the
    right: words (k), place (l), then the category slug.
    '/s-notebooks/berlin/preis:100:500/c278l3331r20' -> category 278, 'Berlin', 20 km, 100–500 €.
    An eBay search link (ebay.de/sch/i.html?_nkw=...) becomes an eBay search. Anything else:
    ValueError with a plain Russian message."""
    from ...scraper.categories import builtin_by_id

    raw = (url or "").strip()
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = parts.netloc.lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(NOT_KLEINANZEIGEN_RU)
    if "ebay." in host:
        return _parse_ebay_url(parts)
    if "kleinanzeigen.de" not in host:
        raise ValueError(NOT_KLEINANZEIGEN_RU)
    path = unquote(parts.path).rstrip("/")
    if "/s-anzeige/" in path:
        raise ValueError("Это ссылка на одно объявление, а нужна страница поиска (с фильтрами) — открой поиск и "
                         "скопируй адрес. Одно объявление можно проверить через «Проверить объявление»")
    if not path.startswith("/s-"):
        raise ValueError("Это не страница поиска Kleinanzeigen — открой поиск с нужными фильтрами и скопируй адрес")
    elements = [e for e in path[len("/s-"):].split("/") if e]
    out: dict[str, Any] = {"source": "kleinanzeigen", "category_id": None, "category_name": "", "location": "",
                           "location_id": None, "radius_km": None, "min_price": None, "max_price": None, "query": ""}
    code = _CODE_RE.match(elements[-1]) if elements else None
    has_words = False
    if code and any(code.groupdict().values()):
        elements = elements[:-1]
        has_words = code["k"] is not None
        if code["c"]:
            out["category_id"] = int(code["c"])
            cat = builtin_by_id(out["category_id"])
            out["category_name"] = cat.name_de if cat else ""
        if code["l"]:
            out["location_id"] = int(code["l"])
        if code["r"]:
            out["radius_km"] = int(code["r"])
    rest: list[str] = []
    for element in elements:
        price = _PRICE_RE.match(element)
        if price:
            out["min_price"] = float(price["min"]) if price["min"] else None
            out["max_price"] = float(price["max"]) if price["max"] else None
        elif ":" not in element and not element.startswith("seite:"):
            rest.append(element)
    if has_words and rest:
        out["query"] = rest.pop().replace("-", " ")
    if out["location_id"] is not None:
        slug = rest.pop() if rest else ""
        out["location"] = _place_name(slug) if slug else KA_LOCATION_IDS.get(out["location_id"], "")
    if rest and not out["category_name"]:
        out["category_name"] = rest[0].replace("-", " ").title()  # the category slug of a live-only category
    params = parse_qs(parts.query)
    if params.get("keywords"):
        out["query"] = params["keywords"][0]
    return _described(out)


def _parse_ebay_url(parts: Any) -> dict[str, Any]:
    """ebay.de/sch/i.html?_nkw=rtx+3090&_udlo=100&_udhi=500&_sacat=27386&LH_Auction=1."""
    if "/itm/" in parts.path:
        raise ValueError("Это ссылка на одно объявление eBay, а нужна страница поиска — открой поиск на ebay.de "
                         "и скопируй адрес. Одно объявление можно проверить через «Проверить объявление»")
    params = parse_qs(parts.query)

    def first(key: str) -> str:
        return (params.get(key) or [""])[0].strip()

    def number(key: str) -> float | None:
        try:
            return float(first(key).replace(",", ".")) if first(key) else None
        except ValueError:
            return None

    query = first("_nkw")
    category = first("_sacat")
    if not query and (not category or category == "0"):
        raise ValueError("Не нашёл в ссылке eBay, что искать — сначала введи запрос в поиске eBay, потом скопируй адрес")
    options = []
    if first("LH_BIN") == "1":
        options.append("FIXED_PRICE")
    if first("LH_Auction") == "1":
        options.append("AUCTION")
    if first("LH_BO") == "1":
        options.append("BEST_OFFER")
    out: dict[str, Any] = {
        "source": "ebay", "category_id": None, "category_name": "", "location": first("_stpos"), "location_id": None,
        "radius_km": int(number("_sadis") or 0) or None, "min_price": number("_udlo"), "max_price": number("_udhi"),
        "query": query, "ebay_category_ids": [category] if category and category != "0" else [],
        "buying_options": options,
    }
    return _described(out)


def _described(out: dict[str, Any]) -> dict[str, Any]:
    bits = []
    if out.get("source") == "ebay":
        bits.append("eBay")
    if out["category_id"]:
        bits.append(f"категория {out['category_name'] or out['category_id']}")
    elif out["category_name"]:
        bits.append(f"категория {out['category_name']}")
    if out["query"]:
        bits.append(f"«{out['query']}»")
    if out["location"]:
        bits.append(out["location"])
    if out["radius_km"]:
        bits.append(f"{out['radius_km']} км")
    if out["min_price"] is not None or out["max_price"] is not None:
        lo = f"{out['min_price']:g}" if out["min_price"] is not None else "0"
        hi = f"{out['max_price']:g}" if out["max_price"] is not None else "…"
        bits.append(f"{lo}–{hi} €")
    if "AUCTION" in (out.get("buying_options") or []):
        bits.append("аукционы")
    out["summary_ru"] = ("Распознал: " + ", ".join(bits)) if bits else "Фильтры не распознаны — поиск пойдёт по ссылке как есть"
    name = out["category_name"] or out["query"] or "Поиск по ссылке"
    if out.get("source") == "ebay":
        name = f"eBay: {out['query'] or name}"
    if out["location"]:
        name += f" · {out['location']}" + (f" {out['radius_km']} км" if out["radius_km"] else "")
    out["suggested_name"] = name
    return out
