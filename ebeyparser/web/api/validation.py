"""Semantic checks of a search on top of the pydantic model (same rules as the old form)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ...config import SearchConfig


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
    for key in ("min_price", "max_price", "reference_price", "target_price", "min_profit", "min_roi",
                "ending_within_hours"):
        value = getattr(search, key)
        if value is not None and value < 0:
            errors[key] = "Не может быть отрицательным"
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


def parse_search_url(url: str) -> dict[str, Any]:
    """What a pasted Kleinanzeigen search URL filters on: category, place, radius, price, words.
    '/s-berlin/preis:100:500/c278l3331r20' -> category 278, 'Berlin', l3331, 20 km, 100–500 €."""
    from ...scraper.categories import builtin_by_id

    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or "kleinanzeigen.de" not in parts.netloc.lower():
        raise ValueError("Нужна ссылка на страницу поиска kleinanzeigen.de")
    path = unquote(parts.path).rstrip("/")
    if "/s-anzeige/" in path:
        raise ValueError("Это ссылка на одно объявление, а нужна страница поиска (с фильтрами)")
    if not path.startswith("/s-"):
        raise ValueError("Не похоже на страницу поиска Kleinanzeigen (адрес должен начинаться с /s-)")
    elements = [e for e in path[len("/s-"):].split("/") if e]
    out: dict[str, Any] = {"category_id": None, "category_name": "", "location": "", "location_id": None,
                           "radius_km": None, "min_price": None, "max_price": None, "query": ""}
    code = _CODE_RE.match(elements[-1]) if elements else None
    if code and any(code.groupdict().values()):
        elements = elements[:-1]
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
        elif ":" not in element:
            rest.append(element)
    if out["location_id"] is not None and rest:
        out["location"] = rest.pop(0).replace("-", " ").title()
    if code and code["k"] is not None and rest:
        out["query"] = rest[-1].replace("-", " ")
    params = parse_qs(parts.query)
    if params.get("keywords"):
        out["query"] = params["keywords"][0]
    bits = []
    if out["category_id"]:
        bits.append(f"категория {out['category_name'] or out['category_id']}")
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
    out["summary_ru"] = ("Распознал: " + ", ".join(bits)) if bits else "Фильтры не распознаны — поиск пойдёт по ссылке как есть"
    name = out["category_name"] or out["query"] or "Поиск по ссылке"
    if out["location"]:
        name += f" · {out['location']}" + (f" {out['radius_km']} км" if out["radius_km"] else "")
    out["suggested_name"] = name
    return out
