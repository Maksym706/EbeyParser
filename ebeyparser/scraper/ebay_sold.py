"""eBay.de SOLD listings (completed sales) as price comparables.

Parses both the classic result layout (`li.s-item`) and the 2025 card layout
(`li.s-card`). Only real sale prices are returned: items whose only price is
struck through ("Preisvorschlag angenommen" - the accepted offer is hidden) are
skipped, as are price ranges and results from the "fewer words" section.
"""

from __future__ import annotations

import copy
import logging
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Comparable
from .http import PoliteClient

log = logging.getLogger(__name__)

EBAY_BASE = "https://www.ebay.de"
SOLD_SEARCH_URL = f"{EBAY_BASE}/sch/i.html"
RESULTS_PER_PAGE = 60
MAX_PAGES = 3

_WS_RE = re.compile(r"\s+")
_AMOUNT_RE = re.compile(r"\d[\d.,]*\d|\d")
_RANGE_RE = re.compile(r"\bbis\b|\bto\b|\s[-–]\s", re.IGNORECASE)
_ITEM_ID_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d+)")
_TITLE_PREFIX_RE = re.compile(r"^(?:neues angebot|neuer artikel|new listing)\s*[:\-]?\s*", re.IGNORECASE)
_TITLE_NOISE = (
    "wird in neuem fenster oder tab geöffnet",
    "opens in a new window or tab",
)
_PLACEHOLDER_TITLES = ("shop on ebay", "neue anzeige")
_PLACEHOLDER_ITEM_ID = "123456"
_FEWER_WORDS_MARKERS = (
    "weniger suchbegriffe",
    "weniger treffer",
    "ergebnisse für weniger",
    "fewer words",
)
_STRUCK_SELECTOR = "s, del, strike, [class*=STRIKETHROUGH], [class*=strikethrough]"


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", text.replace("\xa0", " ").replace("\u202f", " ")).strip()


def _text(el: Tag | None) -> str:
    return _clean(el.get_text(" ")) if el is not None else ""


def _first(root: Tag, *selectors: str) -> Tag | None:
    for sel in selectors:
        el = root.select_one(sel)
        if el is not None:
            return el
    return None


def _fmt_price(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def build_sold_url(
    query: str, page: int = 1, min_price: float | None = None, max_price: float | None = None
) -> str:
    """eBay.de search URL for sold items in Germany, newest first, 60 per page."""
    params: list[tuple[str, str]] = [
        ("_nkw", query),
        ("LH_Sold", "1"),
        ("LH_Complete", "1"),
        ("_sop", "13"),
        ("_ipg", str(RESULTS_PER_PAGE)),
        ("LH_PrefLoc", "1"),
    ]
    if page > 1:
        params.append(("_pgn", str(page)))
    if min_price is not None:
        params.append(("_udlo", _fmt_price(min_price)))
    if max_price is not None:
        params.append(("_udhi", _fmt_price(max_price)))
    return f"{SOLD_SEARCH_URL}?{urlencode(params)}"


def _to_float(token: str) -> float | None:
    """'1.234,56' / '650,00' / '1,234.56' / '650' -> float."""
    tok = token.replace(" ", "")
    if "," in tok and "." in tok:
        if tok.rfind(",") > tok.rfind("."):
            tok = tok.replace(".", "").replace(",", ".")  # German
        else:
            tok = tok.replace(",", "")  # English
    elif "," in tok:
        head, _, tail = tok.rpartition(",")
        tok = f"{head.replace(',', '')}{tail}" if len(tail) == 3 else f"{head.replace(',', '')}.{tail}"
    elif "." in tok:
        head, _, tail = tok.rpartition(".")
        tok = f"{head.replace('.', '')}{tail}" if len(tail) == 3 else f"{head.replace('.', '')}.{tail}"
    try:
        return float(tok)
    except ValueError:
        return None


def parse_ebay_price(text: str) -> float | None:
    """'EUR 650,00' / '650,00 €' / 'EUR 1.234,56' -> float; ranges ('... bis ...') -> None."""
    t = _clean(text)
    if not t:
        return None
    amounts = _AMOUNT_RE.findall(t)
    if not amounts:
        return None
    if len(amounts) > 1 and _RANGE_RE.search(t):
        return None
    return _to_float(amounts[0].strip())


def _clean_title(el: Tag | None) -> str:
    if el is None:
        return ""
    el = copy.copy(el)
    for noise in el.select(".clipped"):
        noise.decompose()
    for span in el.find_all("span"):
        if _TITLE_PREFIX_RE.fullmatch(_text(span) + " ") is not None:
            span.decompose()  # "Neues Angebot" badge inside the title
    title = _text(el)
    for noise in _TITLE_NOISE:
        idx = title.lower().find(noise)
        if idx >= 0:
            title = _clean(title[:idx] + title[idx + len(noise):])
    while (m := _TITLE_PREFIX_RE.match(title)) is not None and m.end() > 0:
        title = title[m.end():].strip()
    return title


def _item_price(item: Tag) -> float | None:
    """First non-struck numeric price of a result item."""
    for el in item.select(".s-item__price, .s-card__price"):
        classes = " ".join(el.get("class") or []).lower()
        if "strikethrough" in classes or el.find_parent(["s", "del", "strike"]) is not None:
            continue
        el = copy.copy(el)
        for struck in el.select(_STRUCK_SELECTOR):
            struck.decompose()
        price = parse_ebay_price(_text(el))
        if price is not None and price > 0:
            return price
    return None


def _item_url(item: Tag) -> tuple[str, str | None]:
    """(clean item URL, item id)."""
    link = _first(item, "a.s-item__link", "a.s-card__link", "a.su-link[href*='/itm/']", "a[href*='/itm/']")
    href = (link.get("href") or "").strip() if link is not None else ""
    m = _ITEM_ID_RE.search(href)
    item_id = m.group(1) if m else (item.get("data-listingid") or None)
    if item_id and item_id != _PLACEHOLDER_ITEM_ID:
        return f"{EBAY_BASE}/itm/{item_id}", item_id
    return (urljoin(EBAY_BASE, href.split("?", 1)[0]) if href else ""), item_id


def _is_fewer_words_separator(li: Tag) -> bool:
    classes = " ".join(li.get("class") or [])
    if "srp-river-answer" not in classes:
        return False
    if "REWRITE_START" in classes:
        return True
    text = _text(li).lower()
    return any(m in text for m in _FEWER_WORDS_MARKERS)


def _result_items(soup: BeautifulSoup) -> list[Tag]:
    """Result <li> elements in page order, up to the 'fewer words' separator."""
    container = soup.select_one("ul.srp-results")
    if container is not None:
        candidates = container.find_all("li", recursive=False)
    else:
        candidates = soup.select("li.s-item, li.s-card, li.srp-river-answer")
    items: list[Tag] = []
    for li in candidates:
        if _is_fewer_words_separator(li):
            break
        classes = li.get("class") or []
        if "s-item" in classes or "s-card" in classes:
            items.append(li)
    return items


def _parse_item(item: Tag) -> Comparable | None:
    title = _clean_title(_first(item, ".s-item__title", ".s-card__title", "[role=heading]"))
    url, item_id = _item_url(item)
    if not title or title.lower() in _PLACEHOLDER_TITLES or item_id == _PLACEHOLDER_ITEM_ID:
        return None
    date_text = _text(
        _first(
            item,
            ".s-item__caption--signal",
            ".s-item__title--tagblock .POSITIVE",
            ".s-card__caption",
            ".s-item__ended-date",
        )
    )
    if date_text and "verkauft" not in date_text.lower() and "sold" not in date_text.lower():
        if "beendet" in date_text.lower() or "ended" in date_text.lower():
            return None  # ended without a sale
    price = _item_price(item)
    if price is None:
        return None
    return Comparable(title=title, price=price, url=url, source="ebay_sold", sold=True, date_text=date_text)


def parse_sold_results(html: str) -> list[Comparable]:
    """Sold items from an eBay.de search page (classic `s-item` or new `s-card` layout)."""
    soup = BeautifulSoup(html or "", "lxml")
    out: list[Comparable] = []
    seen: set[str] = set()
    for item in _result_items(soup):
        try:
            comp = _parse_item(item)
        except Exception:  # one odd item must not kill the page
            log.exception("Failed to parse an eBay result item")
            continue
        if comp is None:
            continue
        key = comp.url or f"{comp.title}|{comp.price}"
        if key not in seen:
            seen.add(key)
            out.append(comp)
    return out


def _has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html or "", "lxml")
    nxt = soup.select_one("a.pagination__next, .pagination__next[href]")
    return nxt is not None and nxt.get("aria-disabled") != "true" and bool(nxt.get("href"))


class EbaySoldScraper:
    """Fetches recently sold items on eBay.de."""

    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    async def sold_comparables(self, query: str, limit: int = 30) -> list[Comparable]:
        out: list[Comparable] = []
        seen: set[str] = set()
        referer: str | None = EBAY_BASE + "/"
        pages = min(MAX_PAGES, max(1, -(-limit // RESULTS_PER_PAGE)))
        for page in range(1, pages + 1):
            url = build_sold_url(query, page)
            html = await self.client.get_text(url, referer=referer)
            fresh = 0
            for comp in parse_sold_results(html):
                key = comp.url or f"{comp.title}|{comp.price}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(comp)
                fresh += 1
            if len(out) >= limit or fresh == 0 or not _has_next_page(html):
                break
            referer = url
        return out[:limit]
