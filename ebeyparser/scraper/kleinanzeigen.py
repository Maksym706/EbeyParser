"""Kleinanzeigen.de scraper: search result pages, ad detail pages, images.

Pure parsing functions work on HTML strings (easy to test offline);
`KleinanzeigenScraper` ties them to a `PoliteClient`.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..config import SearchConfig
from ..models import Comparable, Listing
from .http import PoliteClient, RateBudgetExceeded

log = logging.getLogger(__name__)

BASE_URL = "https://www.kleinanzeigen.de"
SEARCH_FORM_URL = f"{BASE_URL}/s-suchanfrage.html"
LARGE_IMAGE_RULE = "$_59.JPG"  # largest CDN rendition (~1600 px)
# Rendition for the AI check. Assumption (not documented by the site): "$_57" is the
# ~800 px gallery size, well under the 1024 px a 7B vision model needs; bigger photos
# only cost tokens. The AI client downsizes anything larger anyway when Pillow is installed.
MEDIUM_IMAGE_RULE = "$_57.JPG"
COMPARABLE_MAX_PAGES = 2
# attributes added from the seller box of an ad page (see _seller_signals)
SELLER_ATTRIBUTE_KEYS = ("Nutzertyp", "Aktiv seit", "Bewertung", "Anzeigen des Verkäufers", "Antwortzeit")

_WS_RE = re.compile(r"\s+")
_AMOUNT_RE = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?")
_NEGOTIABLE_RE = re.compile(r"\bvb\b|verhandlungsbasis", re.IGNORECASE)
_POSTAL_RE = re.compile(r"\b(\d{5})\b")
_DISTANCE_RE = re.compile(r"\(?\s*(\d+(?:[.,]\d+)?)\s*km\s*\)?", re.IGNORECASE)
_AD_ID_IN_URL_RE = re.compile(r"/(\d{5,})-\d+-\d+")
_LONG_NUMBER_RE = re.compile(r"\d{6,}")
_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")
_SEITE_RE = re.compile(r"^seite:\d+$", re.IGNORECASE)
_CODE_RE = re.compile(r"^(?:k\d|c\d|l\d)")  # last path element: k0 / c225 / k0c225l3331r20 / ...
_IMAGE_RULE_RE = re.compile(r"rule=(?:\$|%24)_\d+\.[A-Za-z]+")
_STATUS_WORD_RE = re.compile(r"^\W*(reserviert|gelöscht|geloescht)\b", re.IGNORECASE)
_STATUS_PREFIX_RE = re.compile(r"^(reserviert|gelöscht|geloescht)\s*[•·|:\-–]\s*", re.IGNORECASE)
_WANTED_RE = re.compile(r"^\W*(?:suche|suchen|kaufe|ankauf|gesuch)\b", re.IGNORECASE)
_SHIPPING_COST_RE = re.compile(r"versand[^0-9]{0,20}?(\d+(?:[.,]\d{1,2})?)\s*€", re.IGNORECASE)
_SORT_ELEMENT_RE = re.compile(r"^sortierung:(.*)$", re.IGNORECASE)
_STRUCK_SELECTOR = "s, del, strike, [class*=old-price], [class*=strike], [class*=line-through], [style*=line-through]"


# -- small helpers -----------------------------------------------------------


def _clean(text: str | None) -> str:
    """Collapse whitespace (incl. nbsp / narrow nbsp) and strip."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text.replace("\xa0", " ").replace("\u202f", " ")).strip()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def _text(el: Tag | None, sep: str = " ") -> str:
    return _clean(el.get_text(sep)) if el is not None else ""


def _first(root: Tag, *selectors: str) -> Tag | None:
    """First match, trying selectors in priority order (not document order)."""
    for sel in selectors:
        el = root.select_one(sel)
        if el is not None:
            return el
    return None


def _german_amount(raw: str) -> float:
    return float(raw.replace(".", "").replace(",", "."))


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _absolute(href: str) -> str:
    return urljoin(BASE_URL + "/", href)


def _ad_id_from_url(url: str) -> str | None:
    m = _AD_ID_IN_URL_RE.search(url or "")
    return m.group(1) if m else None


def _image_from_element(el: Tag) -> str | None:
    """Best image URL of an <img> (or lazy-load container) element."""
    for attr in ("data-imgsrc", "data-src", "src"):
        value = (el.get(attr) or "").strip()
        if value and not value.startswith("data:"):
            return _absolute(value)
    srcset = (el.get("srcset") or el.get("data-srcset") or "").strip()
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return _absolute(first)
    return None


def _is_ad_image(url: str) -> bool:
    low = url.lower()
    return low.startswith("http") and "/static/" not in low and "placeholder" not in low


def _price_from_element(el: Tag | None) -> tuple[str, float | None, bool, bool]:
    """(price_text, price, negotiable, is_free), ignoring struck-through old prices."""
    if el is None:
        return "", None, False, False
    el = copy.copy(el)
    for old in el.select(_STRUCK_SELECTOR):
        old.decompose()
    text = _text(el)
    price, negotiable, is_free = parse_price(text)
    return text, price, negotiable, is_free


def _parse_location(text: str) -> tuple[str, str | None, float | None]:
    """'10115 Mitte (12 km)' -> ('10115 Mitte', '10115', 12.0)."""
    text = _clean(text)
    distance = None
    m = _DISTANCE_RE.search(text)
    if m:
        distance = float(m.group(1).replace(",", "."))
        text = _clean(_DISTANCE_RE.sub(" ", text))
    postal = _POSTAL_RE.search(text)
    return text, (postal.group(1) if postal else None), distance


def _shipping_info(texts: list[str]) -> tuple[bool | None, float | None]:
    """(shipping_possible, shipping_cost) from texts like 'Versand möglich', '+ Versand ab 6,99 €'."""
    joined = " | ".join(t.lower() for t in texts if t)
    if not joined:
        return None, None
    if "nur abholung" in joined:
        return False, None
    if "versand" in joined:
        m = _SHIPPING_COST_RE.search(joined)
        return True, (_german_amount(m.group(1)) if m else None)
    return None, None


def _multiline_text(el: Tag) -> str:
    """Text of an element keeping <br> / block boundaries as newlines (source
    whitespace is collapsed like a browser does)."""
    parts: list[str] = []
    for node in el.descendants:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if node.parent is not None and node.parent.name in ("script", "style"):
                continue
            parts.append(_WS_RE.sub(" ", str(node).replace("\xa0", " ")))
        elif isinstance(node, Tag) and node.name in ("br", "p", "div", "li"):
            parts.append("\n")
    lines = [line.strip() for line in "".join(parts).split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):  # keep at most one blank line in a row
            out.append(line)
    return "\n".join(out).strip()


# -- public parsing API ------------------------------------------------------


def parse_price(text: str) -> tuple[float | None, bool, bool]:
    """Parse a Kleinanzeigen price text -> (price, negotiable, is_free).

    "1.250 € VB" -> (1250.0, True, False); "VB" -> (None, True, False);
    "Zu verschenken" -> (0.0, False, True). If several amounts appear (old and
    new price in plain text), the last one (the current price) wins.
    """
    t = _clean(text)
    if not t:
        return None, False, False
    if "verschenken" in t.lower():
        return 0.0, False, True
    negotiable = bool(_NEGOTIABLE_RE.search(t))
    amounts = _AMOUNT_RE.findall(t)
    price = _german_amount(amounts[-1]) if amounts else None
    return price, negotiable, False


def _price_param(value: float | None, round_up: bool) -> str:
    if value is None:
        return ""
    return str(int(math.ceil(value) if round_up else math.floor(value)))


def _form_url(
    query: str,
    *,
    page: int = 1,
    category_id: int | None = None,
    location: str = "",
    location_id: int | None = None,
    radius_km: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    sorting: str = "SORTING_DATE",
) -> str:
    """URL of the search form endpoint; the site redirects it to the pretty URL."""
    params = {
        "keywords": query or "",
        "categoryId": "" if category_id is None else str(category_id),
        "locationStr": location or "",
        "locationId": "" if location_id is None else str(location_id),
        "radius": str(radius_km or 0),
        "sortingField": sorting,
        "adType": "OFFER",
        "posterType": "",
        "pageNum": str(max(1, page)),
        "action": "find",
        "maxPrice": _price_param(max_price, round_up=True),
        "minPrice": _price_param(min_price, round_up=False),
    }
    return f"{SEARCH_FORM_URL}?{urlencode(params)}"


def build_search_url(search: SearchConfig, page: int = 1) -> str:
    """Search URL for a configured search: the pasted `url` (newest first) or the search form."""
    if search.url:
        return page_url(with_date_sorting(search.url.strip()), page)
    return _form_url(
        search.query,
        page=page,
        category_id=search.category_id,
        location=search.location,
        location_id=search.location_id,
        radius_km=search.radius_km,
        min_price=search.min_price,
        max_price=search.max_price,
    )


def with_date_sorting(url: str) -> str:
    """Make a pasted search URL list the newest ads first (the monitor and "page until
    seen" rely on it).

    - s-suchanfrage.html form URLs: sets `sortingField=SORTING_DATE` (the form's own
      parameter, same as the URLs we build).
    - pretty /s-.../k0 URLs: removes an explicit non-date sort element such as
      `sortierung:preis`, so the site's default order (newest first) applies. Nothing is
      added: the exact spelling of a "newest" element is not verified, and an unknown
      element could break the URL. Other URLs are returned unchanged."""
    parts = urlsplit(url)
    if parts.path.endswith("s-suchanfrage.html"):
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if any(k == "sortingField" and v == "SORTING_DATE" for k, v in pairs):
            return url
        if any(k == "sortingField" for k, _ in pairs):
            pairs = [(k, "SORTING_DATE" if k == "sortingField" else v) for k, v in pairs]
        else:
            pairs.append(("sortingField", "SORTING_DATE"))
        return urlunsplit(parts._replace(query=urlencode(pairs)))
    path = parts.path.rstrip("/")
    if not path.startswith("/s-"):
        return url
    elements = path[len("/s-"):].split("/")
    kept = [e for e in elements
            if not ((m := _SORT_ELEMENT_RE.match(e)) and m.group(1).lower() not in ("neueste", "neu"))]
    if len(kept) == len(elements) or not kept:
        return url
    log.debug("Search URL %s: sort element removed (newest first)", url)
    return urlunsplit(parts._replace(path="/s-" + "/".join(kept)))


def page_url(url: str, page: int) -> str:
    """URL of result page `page` for a pretty search URL or a s-suchanfrage.html URL.

    /s-rtx-3090/k0 -> /s-seite:2/rtx-3090/k0
    /s-berlin/rtx-3090/k0l3331r20 -> /s-berlin/seite:2/rtx-3090/k0l3331r20
    /s-pc-zubehoer-software/c225 -> /s-pc-zubehoer-software/seite:2/c225
    """
    page = max(1, int(page))
    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if parts.path.endswith("s-suchanfrage.html") or any(k == "pageNum" for k, _ in query_pairs):
        if any(k == "pageNum" for k, _ in query_pairs):  # keep the parameter's position
            pairs = [(k, str(page) if k == "pageNum" else v) for k, v in query_pairs]
        else:
            pairs = [*query_pairs, ("pageNum", str(page))]
        return urlunsplit(parts._replace(query=urlencode(pairs)))

    path = parts.path.rstrip("/")
    if not path.startswith("/s-"):
        return url  # unknown format: leave it alone
    elements = [e for e in path[len("/s-"):].split("/") if e]
    elements = [e for e in elements if not _SEITE_RE.match(e)]
    if page > 1:
        if not elements or not _CODE_RE.match(elements[-1]):
            log.debug("Cannot paginate unknown search URL %s", url)
            return url
        insert_at = len(elements) - 1
        if elements[-1].lower().startswith("k") and insert_at > 0:
            insert_at -= 1  # keyword slug precedes the code: seite goes before it
        elements.insert(insert_at, f"seite:{page}")
    return urlunsplit(parts._replace(path="/s-" + "/".join(elements)))


def _is_top_ad(article: Tag) -> bool:
    nodes: list[Tag] = [article]
    if isinstance(article.parent, Tag) and article.parent.name == "li":
        nodes.append(article.parent)
    for node in nodes:
        if any("topad" in c.lower() for c in node.get("class") or []):
            return True
    if article.select_one("[class*=topad]") is not None:
        return True
    for badge in article.select("[class*=badge], .simpletag, [class*=label]"):
        if _text(badge).upper() == "TOP":
            return True
    return False


def _parse_card(article: Tag, search_name: str) -> Listing | None:
    href = article.get("data-href") or ""
    title_el = _first(article, "h2 a.ellipsis", "a.ellipsis", "h2 a", ".text-module-begin a", "h2")
    if not href and title_el is not None and title_el.name == "a":
        href = title_el.get("href") or ""
    ad_id = (article.get("data-adid") or "").strip() or _ad_id_from_url(href)
    title = _text(title_el)
    if not ad_id or not title:
        return None
    url = _absolute(href) if href else f"{BASE_URL}/s-anzeige/{ad_id}"

    image_urls: list[str] = []
    image_box = article.select_one(".aditem-image") or article
    for el in [*image_box.select("[data-imgsrc]"), *image_box.select("img")]:
        img = _image_from_element(el)
        if img and _is_ad_image(img):
            image_urls.append(img)
            break

    location, postal_code, distance = _parse_location(_text(article.select_one(".aditem-main--top--left")))
    price_el = _first(article, "p.aditem-main--middle--price-shipping--price", "[class*=price-shipping--price]")
    price_text, price, negotiable, is_free = _price_from_element(price_el)

    shipping_text = _text(article.select_one(".aditem-main--middle--price-shipping--shipping"))
    tags = [_text(t) for t in article.select(".simpletag")]
    tags = [t for t in tags if t.upper() != "TOP"]
    if shipping_text in ("Versand möglich", "Nur Abholung"):
        tags.append(shipping_text)
    shipping_possible, shipping_cost = _shipping_info([shipping_text, *tags])

    return Listing(
        ad_id=ad_id,
        source="kleinanzeigen",
        url=url,
        title=title,
        price=price,
        price_text=price_text,
        negotiable=negotiable,
        is_free=is_free,
        location=location,
        postal_code=postal_code,
        distance_km=distance,
        posted_at_text=_text(article.select_one(".aditem-main--top--right")),
        description=_text(article.select_one(".aditem-main--middle--description")),
        image_urls=image_urls,
        shipping_possible=shipping_possible,
        shipping_cost=shipping_cost,
        tags=_unique(tags),
        is_top_ad=_is_top_ad(article),
        search_name=search_name,
    )


def parse_search_results(html: str, search_name: str = "") -> list[Listing]:
    """All ads on a search result page (incl. top ads), deduped. Cards after a
    "similar / alternative ads" heading are dropped (they ignore the search filters)."""
    return _results_from_soup(_soup(html), search_name)


# Headings of sections with ads that do NOT match the search (other region / query).
_ALT_HINT_RE = re.compile(
    r"alternative|ähnliche|aehnliche|interessieren|in der nähe|in deiner nähe|umgebung|außerhalb|ausserhalb",
    re.IGNORECASE,
)
_ALT_HEADING_RE = re.compile(
    r"^\W*(?:weitere\s+|mehr\s+)?(?:"
    r"(?:alternative|ähnliche|aehnliche)\s+(?:anzeigen|angebote|ergebnisse)"
    r"|(?:das\s+)?(?:könnte|koennte)\s+dich\s+(?:auch\s+)?interessieren"
    r"|(?:anzeigen|angebote|ergebnisse)\s+(?:in\s+der\s+nähe|in\s+deiner\s+nähe|aus\s+der\s+umgebung)"
    r"|(?:anzeigen|angebote|ergebnisse)\b.{0,40}?\b(?:außerhalb|ausserhalb)\b"
    r")",
    re.IGNORECASE,
)
_ALT_SECTION_SELECTOR = ".srchrslt-alternatives-headline, [id*=altads], [class*=alternatives-headline]"
_NOT_HEADING_PARENTS = ["a", "nav", "header", "footer", "button", "select", "option", "script", "style", "title"]


def _alternative_markers(soup: BeautifulSoup) -> list[Tag]:
    markers: list[Tag] = list(soup.select(_ALT_SECTION_SELECTOR))
    for string in soup.find_all(string=_ALT_HINT_RE):
        el = string.parent
        if not isinstance(el, Tag) or el.name in _NOT_HEADING_PARENTS:
            continue
        text = _text(el)
        if len(text) > 100 or not _ALT_HEADING_RE.match(text):
            continue
        if el.find_parent(_NOT_HEADING_PARENTS) is not None:
            continue  # menu / footer link, not a section heading
        card = el.find_parent(["article", "li"])
        if card is not None and _ad_links(card):
            continue  # text inside an ad card (e.g. its description)
        markers.append(el)
    return markers


def _before_alternatives(soup: BeautifulSoup, cards: list[Tag]) -> list[Tag]:
    """Cards (in any order) that come before the first 'alternative / similar ads'
    section. A marker only counts when at least one card precedes it, so a genuine
    zero-results page (only alternatives) is left to is_empty_results_page()."""
    if not cards:
        return cards
    markers = _alternative_markers(soup)
    if not markers:
        return cards
    order = {id(el): i for i, el in enumerate(soup.find_all(True))}
    first_card = min(order.get(id(card), 0) for card in cards)
    cut = min((order[id(m)] for m in markers if order.get(id(m), -1) > first_card), default=None)
    if cut is None:
        return cards
    kept = [card for card in cards if order.get(id(card), -1) < cut]
    if len(kept) < len(cards):
        log.debug("Ignoring %d 'alternative' ads after the similar-ads heading", len(cards) - len(kept))
    return kept


def _results_from_soup(soup: BeautifulSoup, search_name: str = "") -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()
    for article in _before_alternatives(soup, soup.select("article.aditem")):
        try:
            listing = _parse_card(article, search_name)
        except Exception:  # one odd card must not kill the whole page
            log.exception("Failed to parse a Kleinanzeigen result card")
            continue
        if listing is not None and listing.ad_id not in seen:
            seen.add(listing.ad_id)
            listings.append(listing)
    if not listings:
        listings = _generic_cards(soup, search_name)
    return listings


_PRICE_IN_TEXT_RE = re.compile(
    r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?\s*€(?:\s*VB)?|zu verschenken|\bVB\b", re.IGNORECASE
)
_POSTED_RE = re.compile(r"(?:heute|gestern)(?:,\s*\d{1,2}:\d{2})?|\d{1,2}\.\d{1,2}\.\d{4}", re.IGNORECASE)
_TAG_WORDS = ("Versand möglich", "Nur Abholung", "Direkt kaufen", "Gesuch")


def _ad_links(el: Tag) -> dict[str, list[Tag]]:
    links: dict[str, list[Tag]] = {}
    for a in el.select('a[href*="/s-anzeige/"]'):
        ad_id = _ad_id_from_url(a.get("href") or "")
        if ad_id:
            links.setdefault(ad_id, []).append(a)
    return links


def _can_grow(box: Tag, ad_id: str) -> Tag | None:
    parent = box.parent
    if parent is None or parent.name in ("body", "html", "[document]", "main"):
        return None
    if any(other != ad_id for other in _ad_links(parent)):
        return None  # the parent already holds a neighbouring ad
    return parent


def _card_box(start: Tag, ad_id: str) -> Tag:
    """Grow from `start` (data-adid element or a link) to the whole card of `ad_id`:
    first until it shows the ad link and a price, then up to the card root (li/article)
    — never into a parent that also contains another ad."""
    box = start
    for _ in range(10):
        if ad_id in _ad_links(box) and _PRICE_IN_TEXT_RE.search(_text(box)):
            break
        parent = _can_grow(box, ad_id)
        if parent is None:
            return box
        box = parent
    for _ in range(4):  # include the image / meta columns of the same card
        if box.name in ("li", "article"):
            break
        parent = _can_grow(box, ad_id)
        if parent is None:
            break
        box = parent
    return box


def _lines(el: Tag) -> list[str]:
    return [line for raw in el.get_text("\n").split("\n") if (line := _clean(raw))]


# A whole element that is just the price: "449 €", "1.250 € VB", "VB", "Zu verschenken".
_FULL_PRICE_RE = re.compile(
    r"^(?:(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?\s*€(?:\s*VB)?|VB|Zu verschenken)$", re.IGNORECASE
)
# Amounts right after these words are not the asking price: "NP 1.200 €", "UVP: 999 €",
# "+ Versand ab 6,99 €", "inkl. Versand 460 €", "statt 500 €".
_NOT_ASKING_PRICE_RE = re.compile(
    r"(?:\bnp|\buvp|neupreis|neu\s*preis|versand(?:kosten)?|inkl\.?|zzgl\.?|statt)"
    r"\s*(?:ab|ca\.?|von|für|nur|nur\s+ab)?\s*[:=.\-]?\s*$",
    re.IGNORECASE,
)
_PRICE_CONTEXT_CHARS = 25


def _preceded_by_other_amount_word(text: str, start: int) -> bool:
    return _NOT_ASKING_PRICE_RE.search(text[max(0, start - _PRICE_CONTEXT_CHARS):start]) is not None


def _card_price(box: Tag) -> str:
    """Price text of a card: prefer an element whose whole text is a price, skip amounts
    labelled NP/UVP/Neupreis/Versand/inkl., ignore struck-through old prices."""
    clean = copy.copy(box)
    for old in clean.select(_STRUCK_SELECTOR):
        old.decompose()
    for el in clean.find_all(True):
        if el.name in ("a", "script", "style"):
            continue
        text = _text(el)
        if not text or len(text) > 30 or not _FULL_PRICE_RE.match(text):
            continue
        node = el  # climb while the text stays the same, then look at what precedes it
        while isinstance(node.parent, Tag) and _text(node.parent) == text:
            node = node.parent
        context = _text(node.parent) if isinstance(node.parent, Tag) else ""
        at = context.find(text)
        if at >= 0 and _preceded_by_other_amount_word(context, at):
            continue
        return text
    full = _text(clean)
    for match in _PRICE_IN_TEXT_RE.finditer(full):
        if not _preceded_by_other_amount_word(full, match.start()):
            return match.group(0)
    return ""


def _card_from_box(box: Tag, ad_id: str, search_name: str) -> Listing | None:
    anchors = _ad_links(box).get(ad_id, [])
    texts = [_text(a) or _clean(a.get("title")) for a in anchors]
    title = max(texts, key=len) if any(texts) else ""
    if not title:
        heading = box.select_one("h2, h3")
        img = box.select_one("img[alt]")
        title = _text(heading) or (_clean(img.get("alt")) if img is not None else "")
    if not title:
        return None
    href = anchors[0].get("href") if anchors else ""

    price_text = _card_price(box)
    price, negotiable, is_free = parse_price(price_text)

    lines = _lines(box)
    location = postal_code = posted = ""
    distance = None
    for line in lines:
        if not postal_code and _POSTAL_RE.search(line) and "€" not in line:
            cut = _POSTED_RE.search(line)
            location, postal_code, distance = _parse_location(line[: cut.start()] if cut else line)
        if not posted and (m := _POSTED_RE.search(line)):
            posted = m.group(0)
    body_lines = [ln for ln in lines if ln != title and len(ln) > 30 and "€" not in ln and not _POSTAL_RE.match(ln)]
    description = max(body_lines, key=len) if body_lines else ""
    full_text = " ".join(lines)
    tags = [w for w in _TAG_WORDS if w.lower() in full_text.lower()]
    shipping_possible, shipping_cost = _shipping_info(tags)
    image = next((u for el in box.select("img, [data-imgsrc]")
                  if (u := _image_from_element(el)) and _is_ad_image(u)), None)
    return Listing(
        ad_id=ad_id, source="kleinanzeigen",
        url=_absolute(href) if href else f"{BASE_URL}/s-anzeige/{ad_id}",
        title=title, price=price, price_text=price_text, negotiable=negotiable, is_free=is_free,
        location=location, postal_code=postal_code or None, distance_km=distance,
        posted_at_text=posted, description=description, image_urls=[image] if image else [],
        shipping_possible=shipping_possible, shipping_cost=shipping_cost, tags=tags,
        is_top_ad=box.select_one("[class*=topad], [class*=top-ad]") is not None,
        search_name=search_name,
    )


def _generic_cards(soup: BeautifulSoup, search_name: str = "") -> list[Listing]:
    """The classic card markup is gone (site redesign): find each ad by its data-adid
    element or its '/s-anzeige/<slug>/<id>-<cat>-<loc>' links and read the card text."""
    starts: dict[str, Tag] = {}
    for el in soup.select("[data-adid]"):
        ad_id = (el.get("data-adid") or "").strip()
        if ad_id.isdigit():
            starts.setdefault(ad_id, el)
    for ad_id, anchors in _ad_links(soup).items():
        starts.setdefault(ad_id, anchors[0])
    kept = {id(el) for el in _before_alternatives(soup, list(starts.values()))}
    listings: list[Listing] = []
    for ad_id, start in starts.items():
        if id(start) not in kept:
            continue
        try:
            listing = _card_from_box(_card_box(start, ad_id), ad_id, search_name)
        except Exception:
            log.exception("Failed to parse a Kleinanzeigen card %s", ad_id)
            continue
        if listing is not None:
            listings.append(listing)
    if listings:
        log.debug("Kleinanzeigen: generic card parser used (%d ads)", len(listings))
    return listings


class PageLayoutError(Exception):
    """The page downloaded fine, but no ads could be recognised in it. str(exc) is the
    technical text (log / CLI, with the debug hint); `message_ru` is what the web UI shows."""

    message_ru = ("Kleinanzeigen показал страницу, которую я не смог разобрать — возможно, сайт изменился. "
                  "Попробую снова на следующей проверке; если повторяется, обнови программу")
    code = "layout"


_EMPTY_RESULT_MARKERS = (
    "es wurden leider keine anzeigen", "keine ergebnisse", "keine anzeigen gefunden",
    "leider keine treffer", "wurden keine ergebnisse",
)
_ZERO_RESULTS_RE = re.compile(r"(?<![\d.])0 (?:ergebnisse|anzeigen)\b")
_CONSENT_MARKERS = ("gdpr-banner", "consent-banner", "cmp-", "didomi", "sp_message", "usercentrics",
                    "cookie-einstellungen", "datenschutzeinstellungen")


def is_empty_results_page(html: str) -> bool:
    """The site found nothing for the query/radius (it may still show 'similar' ads from
    all over Germany below — those must not be treated as results)."""
    low = (html or "").lower()
    return any(m in low for m in _EMPTY_RESULT_MARKERS) or bool(_ZERO_RESULTS_RE.search(low))


def page_diagnostics(html: str) -> dict[str, object]:
    """What the search page looks like to the parser — for `ebeyparser debug-search`."""
    from .http import looks_blocked

    soup = _soup(html)
    low = (html or "").lower()
    title = _text(soup.title) if soup.title else ""
    listings = _results_from_soup(soup)
    classes: dict[str, int] = {}
    for a in soup.select('a[href*="/s-anzeige/"]')[:5]:
        for parent in list(a.parents)[:4]:
            for cls in parent.get("class") or []:
                classes[cls] = classes.get(cls, 0) + 1
    return {
        "title": title,
        "size_kb": round(len(html or "") / 1024, 1),
        "article.aditem": len(soup.select("article.aditem")),
        "[data-adid]": len(soup.select("[data-adid]")),
        "li.ad-listitem": len(soup.select("li.ad-listitem")),
        "links /s-anzeige/": len(soup.select('a[href*="/s-anzeige/"]')),
        "parsed_ads": len(listings),
        "empty_results_text": is_empty_results_page(html),
        "blocked_markers": looks_blocked(html),
        "consent_markers": [m for m in _CONSENT_MARKERS if m in low],
        "link_parent_classes": sorted(classes, key=classes.get, reverse=True)[:12],  # type: ignore[arg-type]
        "sample": [(item.ad_id, item.title[:60], item.price_text, item.location) for item in listings[:3]],
    }


def next_page_url(html: str) -> str | None:
    """Absolute URL of the 'next page' link, or None on the last page."""
    return _next_link(_soup(html))


def _next_link(soup: BeautifulSoup) -> str | None:
    for sel in ("a.pagination-next", "[data-testid=pagination-next]", ".pagination-next", "a[rel=next]", "link[rel=next]"):
        for el in soup.select(sel):
            href = (el.get("href") or el.get("data-url") or el.get("data-href") or "").strip()
            if href and not href.startswith(("#", "javascript:")):
                return _absolute(href)
    return None


def _has_pagination(soup: BeautifulSoup) -> bool:
    return soup.select_one(".pagination, .pagination-nav, [data-testid=pagination]") is not None


def large_image_url(url: str) -> str:
    """Same CDN image in large size (rule=$_59.JPG); other URLs unchanged."""
    return _IMAGE_RULE_RE.sub(lambda _m: f"rule={LARGE_IMAGE_RULE}", url, count=1)


def medium_image_url(url: str) -> str:
    """Same CDN image in medium size for the AI check (rule=$_57.JPG); other URLs unchanged."""
    return _IMAGE_RULE_RE.sub(lambda _m: f"rule={MEDIUM_IMAGE_RULE}", url, count=1)


def _detail_images(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    elements = soup.select(
        ".galleryimage-element img, .galleryimage-element [data-imgsrc], img#viewad-image, "
        "#viewad-image img, #viewad-thumbnail-list img"
    )
    for el in elements:
        img = _image_from_element(el)
        if not img or not _is_ad_image(img):
            continue
        key = img.split("?", 1)[0]
        if key not in seen:
            seen.add(key)
            urls.append(img)
    if not urls:  # unknown gallery markup: any ad image on the page, in page order
        for el in soup.select("img, [data-imgsrc], source"):
            for img in [_image_from_element(el), *(u.strip().split(" ")[0] for u in (el.get("srcset") or "").split(","))]:
                if img and "/prod-ads/images/" in img and _is_ad_image(img):
                    key = img.split("?", 1)[0]
                    if key not in seen:
                        seen.add(key)
                        urls.append(img)
    if not urls:
        for meta in soup.select('meta[property="og:image"]'):
            content = (meta.get("content") or "").strip()
            if content and _is_ad_image(content) and content.split("?", 1)[0] not in seen:
                seen.add(content.split("?", 1)[0])
                urls.append(content)
    return urls


def _json_ld(soup: BeautifulSoup) -> dict[str, object]:
    """Product data from <script type="application/ld+json"> (name, description, images, price)."""
    found: dict[str, object] = {}

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = str(node.get("@type", "")).lower()
            if kind in ("product", "offer", "individualproduct", "vehicle", "car") or "offers" in node:
                for key in ("name", "description", "image", "price"):
                    if key in node and key not in found:
                        found[key] = node[key]
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return found


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        el = soup.select_one(f'meta[property="{name}"], meta[name="{name}"]')
        if el is not None and (el.get("content") or "").strip():
            return _clean(el.get("content"))
    return ""


def _detail_title(soup: BeautifulSoup) -> tuple[str, list[str]]:
    """Title without 'Reserviert •' / 'Gelöscht •' prefixes, plus status tags."""
    title_el = _first(soup, "#viewad-title", "h1.boxedarticle--title", "h1[itemprop=name]")
    if title_el is None:
        return "", []
    status: list[str] = []
    el = copy.copy(title_el)
    for span in el.select("[class*=reserved], [class*=deleted]"):
        classes = span.get("class") or []
        hidden = "is-hidden" in classes or "hidden" in classes or span.has_attr("hidden")
        label = _STATUS_WORD_RE.match(_text(span))
        if label and not hidden:
            status.append(label.group(1))
        span.decompose()
    title = _text(el)
    while (m := _STATUS_PREFIX_RE.match(title)) is not None:
        status.append(m.group(1))
        title = title[m.end():].strip()
    tags = []
    for word in status:
        low = word.lower()
        tags.append("Reserviert" if low.startswith("reserv") else "Gelöscht")
    return title, _unique(tags)


def _seller(soup: BeautifulSoup) -> tuple[str, str]:
    name_el = _first(
        soup,
        "#viewad-contact .userprofile-vip a",
        "#viewad-contact .userprofile-vip",
        ".userprofile-vip a",
        ".userprofile-vip",
        "#viewad-contact-box .text-body-regular-strong",
    )
    name = _text(name_el)
    texts = [_text(e) for e in soup.select(".userprofile-vip-details-text, #viewad-contact, #viewad-profile-box")]
    joined = " ".join(texts).lower()
    if "gewerblich" in joined:
        seller_type = "commercial"
    elif soup.select_one("#viewad-contact [class*=badge-hint-pro], #viewad-contact [class*=pro-badge]") is not None:
        seller_type = "commercial"
    elif "privat" in joined:
        seller_type = "private"
    else:
        seller_type = "unknown"
    return name, seller_type


_SELLER_BOX_SELECTOR = (
    "#viewad-contact, #viewad-profile-box, .userprofile-vip-details, [class*=userprofile], "
    "[class*=user-profile], [class*=userbadge], .badges-iconlist, [data-testid*=seller], "
    "[data-testid*=user-profile]"
)
_ACTIVE_SINCE_RE = re.compile(r"aktiv\s+seit\s*:?\s*(\d{1,2}\.\d{1,2}\.\d{4}|\d{4})", re.IGNORECASE)
_USER_TYPE_RE = re.compile(r"\b(privater|gewerblicher)\s+(nutzer|anbieter|verkäufer)\b", re.IGNORECASE)
_RATING_RE = re.compile(
    r"\b(?:(?:top|ok|na\s+ja)\s+zufriedenheit|(?:(?:sehr|besonders)\s+)?freundlich|"
    r"(?:(?:sehr|besonders)\s+)?zuverlässig)\b",
    re.IGNORECASE,
)
_OTHER_ADS_RE = re.compile(r"\b(\d{1,5})\s+(?:aktive\s+)?anzeigen\b|\banzeigen\s*\((\d{1,5})\)", re.IGNORECASE)
_REPLY_TIME_RE = re.compile(
    r"antwortet\s+(?:in\s+der\s+regel\s+)?(innerhalb\s+(?:von\s+)?[^.,;|()]{1,30}?)\s*(?:$|[.,;|()])", re.IGNORECASE
)


def _nice(text: str) -> str:
    """'top zufriedenheit' -> 'TOP Zufriedenheit', 'sehr freundlich' -> 'Sehr freundlich'."""
    text = _clean(text)
    head, _, tail = text.partition(" ")
    if head.lower() in ("top", "ok"):
        return f"{head.upper()} {tail[:1].upper()}{tail[1:]}"
    return text[:1].upper() + text[1:]


def _seller_signals(soup: BeautifulSoup) -> dict[str, str]:
    """Seller trust signals from the contact box of an ad page -> listing.attributes:
    Nutzertyp, Aktiv seit, Bewertung (badges), Anzeigen des Verkäufers, Antwortzeit."""
    boxes = soup.select(_SELLER_BOX_SELECTOR)
    box_ids = {id(b) for b in boxes}
    outer = [b for b in boxes if not any(id(parent) in box_ids for parent in b.parents)]  # no nested duplicates
    text = " | ".join(_text(b, " | ") for b in outer)
    fallback = False
    if not text:  # unknown markup: the page text without the ad description
        fallback = True
        body = copy.copy(soup.body or soup)
        for el in body.select("#viewad-description, #viewad-description-text, [itemprop=description], script, style"):
            el.decompose()
        text = _text(body, " | ")
    out: dict[str, str] = {}
    if m := _USER_TYPE_RE.search(text):
        out["Nutzertyp"] = f"{m.group(1).capitalize()} {m.group(2).capitalize()}"
    if m := _ACTIVE_SINCE_RE.search(text):
        out["Aktiv seit"] = m.group(1)
    if fallback:
        return out  # badges / counts only from a recognised seller box
    ratings = _unique([_nice(m.group(0)) for m in _RATING_RE.finditer(text)])
    if ratings:
        out["Bewertung"] = ", ".join(ratings)
    if m := _OTHER_ADS_RE.search(text):
        out["Anzeigen des Verkäufers"] = m.group(1) or m.group(2)
    if m := _REPLY_TIME_RE.search(text):
        out["Antwortzeit"] = _clean(m.group(1))
    return out


def parse_ad_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    """Parse an ad page and merge it into `listing` (a copy is returned, detail_loaded=True)."""
    soup = _soup(html)
    title, status_tags = _detail_title(soup)
    ld = _json_ld(soup)
    if not title:  # redesigned page: JSON-LD / <h1> / og:title
        title = _clean(str(ld.get("name") or "")) or _text(soup.select_one("h1")) or re.sub(
            r"\s*\|\s*kleinanzeigen.*$", "", _meta(soup, "og:title"), flags=re.IGNORECASE)

    price_text, price, negotiable, is_free = _price_from_element(
        _first(soup, "#viewad-price", ".boxedarticle--price")
    )
    if price is None and not is_free:
        meta = soup.select_one("meta[itemprop=price]")
        try:
            meta_price = float((meta.get("content") or "").strip()) if meta is not None else None
        except ValueError:
            meta_price = None
        if meta_price:  # 0 means "no number given"
            price = meta_price
            price_text = price_text or f"{meta_price:g} €"

    if price is None and not is_free and ld.get("price") not in (None, "", "0", 0):
        try:
            price = float(str(ld["price"]).replace(",", "."))
            price_text = price_text or f"{price:g} €"
        except ValueError:
            pass

    desc_el = _first(soup, "#viewad-description-text", "[itemprop=description]")
    description = _multiline_text(desc_el) if desc_el is not None else ""
    if not description:  # redesigned page: JSON-LD, then meta description (may be shortened)
        description = str(ld.get("description") or "").strip() or _meta(soup, "og:description", "description")
    location, postal_code, _ = _parse_location(_text(_first(soup, "#viewad-locality", "[itemprop=addressLocality]")))

    posted = ""
    extra = soup.select_one("#viewad-extra-info")
    if extra is not None:
        m = _DATE_RE.search(_text(extra))
        posted = m.group(0) if m else ""

    attributes: dict[str, str] = {}
    for li in soup.select("#viewad-details li.addetailslist--detail, li.addetailslist--detail"):
        value_el = li.select_one(".addetailslist--detail--value")
        if value_el is None:
            continue
        value = _text(value_el)
        key_el = copy.copy(li)
        key_el.select_one(".addetailslist--detail--value").decompose()
        key = _text(key_el).rstrip(":").strip()
        if key and value:
            attributes[key] = value

    feature_tags = [_text(t) for t in soup.select("#viewad-configuration .checktag, .checktaglist .checktag")]
    shipping_texts = [_text(e) for e in soup.select(".boxedarticle--details--shipping, [class*=details--shipping]")]
    shipping_texts.append(attributes.get("Versand", ""))
    shipping_possible, shipping_cost = _shipping_info(shipping_texts)
    shipping_tags = []
    if shipping_possible is True:
        shipping_tags.append("Versand möglich")
    elif shipping_possible is False:
        shipping_tags.append("Nur Abholung")

    seller_name, seller_type = _seller(soup)
    seller_attributes = _seller_signals(soup)
    images = _detail_images(soup)

    # Ad id / URL: explicit box, then the given data, then page metadata.
    ad_id = None
    id_box = soup.select_one("#viewad-ad-id-box")
    if id_box is not None:
        m = _LONG_NUMBER_RE.search(_text(id_box))
        ad_id = m.group(0) if m else None
    canonical_el = _first(soup, "link[rel=canonical]", 'meta[property="og:url"]')
    canonical = ""
    if canonical_el is not None:
        canonical = (canonical_el.get("href") or canonical_el.get("content") or "").strip()
    page_url_ = url or (listing.url if listing else "") or canonical
    if not ad_id:
        ad_id = (listing.ad_id if listing else None) or _ad_id_from_url(page_url_) or _ad_id_from_url(canonical)
    if not ad_id:
        holder = soup.select_one("[data-adid]")
        ad_id = (holder.get("data-adid") or "").strip() if holder is not None else None

    if listing is None:
        if not ad_id or not title:
            raise ValueError("Это не похоже на страницу объявления Kleinanzeigen — возможно, объявление уже удалено")
        listing = Listing(ad_id=ad_id, source="kleinanzeigen", url=page_url_ or f"{BASE_URL}/s-anzeige/{ad_id}", title=title)

    data = listing.model_dump()
    data["source"] = "kleinanzeigen"
    if title:
        data["title"] = title
    if price_text or price is not None or is_free:
        data.update(price=price, price_text=price_text, negotiable=negotiable, is_free=is_free)
    if description:
        data["description"] = description
    if location:
        data["location"] = location
        data["postal_code"] = postal_code or data.get("postal_code")
    if posted and not data.get("posted_at_text"):
        data["posted_at_text"] = posted
    if images:
        data["image_urls"] = images
    if shipping_possible is not None:
        data["shipping_possible"] = shipping_possible
    if shipping_cost is not None:
        data["shipping_cost"] = shipping_cost
    data["attributes"] = {**data.get("attributes", {}), **attributes, **seller_attributes}
    if attributes.get("Zustand"):
        data["condition"] = attributes["Zustand"]
    data["tags"] = _unique([*status_tags, *data.get("tags", []), *shipping_tags, *feature_tags])
    if seller_name:
        data["seller_name"] = seller_name
    if seller_type != "unknown":
        data["seller_type"] = seller_type
    if not data.get("url") and page_url_:
        data["url"] = page_url_
    data["detail_loaded"] = True
    return Listing.model_validate(data)


# -- scraper -----------------------------------------------------------------


class KleinanzeigenScraper:
    """Fetches and parses Kleinanzeigen pages through a PoliteClient."""

    def __init__(self, client: PoliteClient, *, debug_dir: str | Path | None = None) -> None:
        self.client = client
        self.debug_dir = Path(debug_dir) if debug_dir else None
        # search-form URL -> the pretty result URL the site redirected it to: page 1 of a
        # repeated search is then one request instead of two (form + redirect)
        self._resolved: dict[str, str] = {}

    def save_debug_page(self, html: str, name: str) -> Path | None:
        """Dump a page for layout debugging: one file per search per day (a failing search
        is retried every pass — later dumps of the same day overwrite it)."""
        if self.debug_dir is None:
            return None
        slug = re.sub(r"[^\w-]+", "-", name, flags=re.UNICODE).strip("-")[:40] or "page"
        path = self.debug_dir / f"{datetime.now():%Y%m%d}-{slug}.html"
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        except OSError as exc:  # a failed dump must not hide the real problem
            log.warning("Could not save debug page %s: %s", path, exc)
            return None
        return path

    async def _crawl(
        self,
        start_url: str,
        max_pages: int,
        search_name: str,
        seen: Callable[[str], bool] | None = None,
        resolve: str | None = None,
    ) -> list[Listing]:
        """Follow result pages; stop at max_pages, the last page, a page without new ads,
        or (with `seen`) the first page that shows an ad we already know. `resolve`: remember
        where this search-form URL redirected to (see search())."""
        found: dict[str, Listing] = {}
        visited: set[str] = set()
        url: str | None = start_url
        referer: str | None = None
        for page in range(1, max(1, max_pages) + 1):
            if url is None or url in visited:
                break
            visited.add(url)
            try:
                html = await self.client.get_text(url, referer=referer)
            except RateBudgetExceeded as exc:
                if page == 1:
                    raise
                log.info("%s: page %d deferred (%s), keeping %d ads", search_name or start_url, page, exc, len(found))
                break
            if page == 1 and resolve and "s-suchanfrage" in resolve:
                self._remember_resolved(resolve)
            soup = _soup(html)
            if page == 1 and is_empty_results_page(html):
                log.info("%s: no results for this search/radius (ignoring 'similar' ads)", search_name or start_url)
                return []
            items = _results_from_soup(soup, search_name)
            new = [item for item in items if item.ad_id not in found]
            if not new and page == 1:
                saved = self.save_debug_page(html, search_name or "search")
                raise PageLayoutError(
                    "страница поиска получена, но объявления на ней не распознаны"
                    + (f" (HTML сохранён: {saved})" if saved else "")
                    + ". Запусти `python -m ebeyparser debug-search` и пришли вывод."
                )
            if not new:
                break
            for item in new:
                found[item.ad_id] = item
            if page >= max_pages:
                break
            if seen is not None and any(not item.is_top_ad and seen(item.ad_id) for item in items):
                break  # newest first: the following pages only hold ads we already know
            nxt = _next_link(soup)
            if nxt is None:
                if _has_pagination(soup):
                    break  # pagination present but no "next": this was the last page
                nxt = page_url(start_url, page + 1)
            referer, url = url, nxt
        return list(found.values())

    async def search(
        self,
        search: SearchConfig,
        max_pages: int = 1,
        seen: Callable[[str], bool] | None = None,
    ) -> list[Listing]:
        """Ads of a search, newest first. Without `seen`: up to `max_pages` pages (stops
        early on the last page / a page without new ads). With `seen(ad_id) -> bool`:
        additionally stops after the first page that contains an already-known regular
        (non-top) ad — so pass a generous max_pages and only new ads cost requests.
        A RateBudgetExceeded on page 2+ ends the crawl with the pages fetched so far."""
        start = build_search_url(search, 1)
        cached = self._resolved.get(start)
        if cached:
            try:
                return await self._crawl(cached, max_pages, search.name, seen)
            except (PageLayoutError, httpx.HTTPStatusError) as exc:  # the site changed its URLs
                log.info("%s: cached result URL failed (%s) — using the search form again", search.name, exc)
                self._resolved.pop(start, None)
        listings = await self._crawl(start, max_pages, search.name, seen, resolve=start)
        return listings

    def _remember_resolved(self, form_url: str) -> None:
        final = getattr(self.client, "last_url", None)
        if (isinstance(final, str) and final.startswith(BASE_URL) and "s-suchanfrage" not in final
                and final != form_url):
            self._resolved[form_url] = final

    async def fetch_detail(self, listing: Listing) -> Listing:
        html = await self.client.get_text(listing.url, referer=BASE_URL + "/")
        return parse_ad_detail(html, listing, url=listing.url)

    async def comparables(
        self,
        query: str,
        *,
        exclude_ad_id: str | None = None,
        limit: int = 30,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> list[Comparable]:
        """Asking prices of similar offers, nationwide, sorted by relevance."""
        url = _form_url(query, sorting="SORTING_RELEVANCE", min_price=min_price, max_price=max_price)
        out: list[Comparable] = []
        seen: set[str] = set()
        referer: str | None = None
        for page in range(1, COMPARABLE_MAX_PAGES + 1):
            try:
                html = await self.client.get_text(url, referer=referer)
            except RateBudgetExceeded:
                if page == 1:
                    raise
                break  # the first page is enough for an estimate
            soup = _soup(html)
            items = _results_from_soup(soup)
            fresh = [i for i in items if i.ad_id not in seen]
            if not fresh:
                break
            for item in fresh:
                seen.add(item.ad_id)
                if item.ad_id == exclude_ad_id or item.is_top_ad or item.is_free:
                    continue
                if item.price is None or item.price <= 0:
                    continue
                if _WANTED_RE.match(item.title):
                    continue
                if min_price is not None and item.price < min_price:
                    continue
                if max_price is not None and item.price > max_price:
                    continue
                out.append(
                    Comparable(
                        title=item.title,
                        price=item.price,
                        url=item.url,
                        source="kleinanzeigen",
                        sold=False,
                        date_text=item.posted_at_text,
                    )
                )
                if len(out) >= limit:
                    return out
            nxt = _next_link(soup)
            if nxt is None:
                break
            referer, url = url, nxt
        return out

    async def download_images(self, listing: Listing, max_images: int = 3) -> list[bytes]:
        """Medium-size versions (enough for a vision model) of the first `max_images`
        photos, falling back to the URL as listed; failures are skipped."""
        images: list[bytes] = []
        for src in listing.image_urls[: max(0, max_images)]:
            for candidate in _unique([medium_image_url(src), src]):
                try:
                    data = await self.client.get_bytes(candidate)
                except RateBudgetExceeded as exc:  # hourly image cap: the AI works with what we have
                    log.info("Image downloads paused: %s", exc)
                    return images
                except Exception as exc:  # noqa: BLE001 - any failure: try next / skip
                    log.warning("Image download failed for %s: %s", candidate, exc)
                    continue
                if data:
                    images.append(data)
                    break
        return images
