"""Kleinanzeigen.de scraper: search result pages, ad detail pages, images.

Pure parsing functions work on HTML strings (easy to test offline);
`KleinanzeigenScraper` ties them to a `PoliteClient`.
"""

from __future__ import annotations

import copy
import logging
import math
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..config import SearchConfig
from ..models import Comparable, Listing
from .http import PoliteClient

log = logging.getLogger(__name__)

BASE_URL = "https://www.kleinanzeigen.de"
SEARCH_FORM_URL = f"{BASE_URL}/s-suchanfrage.html"
LARGE_IMAGE_RULE = "$_59.JPG"
COMPARABLE_MAX_PAGES = 2

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
    for old in el.select("s, del, strike, [class*=old-price], [class*=strike]"):
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
    """Search URL for a configured search: the pasted `url` or the search form."""
    if search.url:
        return page_url(search.url.strip(), page)
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
    """All ads on a search result page (incl. top ads and 'alternative' ads), deduped."""
    return _results_from_soup(_soup(html), search_name)


def _results_from_soup(soup: BeautifulSoup, search_name: str = "") -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()
    for article in soup.select("article.aditem"):
        try:
            listing = _parse_card(article, search_name)
        except Exception:  # one odd card must not kill the whole page
            log.exception("Failed to parse a Kleinanzeigen result card")
            continue
        if listing is not None and listing.ad_id not in seen:
            seen.add(listing.ad_id)
            listings.append(listing)
    return listings


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
    if not urls:
        for meta in soup.select('meta[property="og:image"]'):
            content = (meta.get("content") or "").strip()
            if content and _is_ad_image(content) and content.split("?", 1)[0] not in seen:
                seen.add(content.split("?", 1)[0])
                urls.append(content)
    return urls


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


def parse_ad_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    """Parse an ad page and merge it into `listing` (a copy is returned, detail_loaded=True)."""
    soup = _soup(html)
    title, status_tags = _detail_title(soup)

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

    desc_el = _first(soup, "#viewad-description-text", "[itemprop=description]")
    description = _multiline_text(desc_el) if desc_el is not None else ""
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
            raise ValueError("page does not look like a Kleinanzeigen ad (no ad id / title)")
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
    data["attributes"] = {**data.get("attributes", {}), **attributes}
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

    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    async def _crawl(self, start_url: str, max_pages: int, search_name: str) -> list[Listing]:
        """Follow result pages; stop at max_pages, the last page or a page without new ads."""
        found: dict[str, Listing] = {}
        visited: set[str] = set()
        url: str | None = start_url
        referer: str | None = None
        for page in range(1, max(1, max_pages) + 1):
            if url is None or url in visited:
                break
            visited.add(url)
            html = await self.client.get_text(url, referer=referer)
            soup = _soup(html)
            new = [item for item in _results_from_soup(soup, search_name) if item.ad_id not in found]
            if not new:
                break
            for item in new:
                found[item.ad_id] = item
            if page >= max_pages:
                break
            nxt = _next_link(soup)
            if nxt is None:
                if _has_pagination(soup):
                    break  # pagination present but no "next": this was the last page
                nxt = page_url(start_url, page + 1)
            referer, url = url, nxt
        return list(found.values())

    async def search(self, search: SearchConfig, max_pages: int = 1) -> list[Listing]:
        return await self._crawl(build_search_url(search, 1), max_pages, search.name)

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
            html = await self.client.get_text(url, referer=referer)
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
        """Large versions of the first `max_images` photos; failures are skipped."""
        images: list[bytes] = []
        for src in listing.image_urls[: max(0, max_images)]:
            for candidate in _unique([large_image_url(src), src]):
                try:
                    data = await self.client.get_bytes(candidate)
                except Exception as exc:  # noqa: BLE001 - any failure: try next / skip
                    log.warning("Image download failed for %s: %s", candidate, exc)
                    continue
                if data:
                    images.append(data)
                    break
        return images
