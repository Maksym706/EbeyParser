"""Kleinanzeigen category catalog, category-facet parsing and live discovery.

The built-in list is curated for reselling. Its IDs are from memory
(`verified=False`) until a live search page for the user's region shows the same
id with the same name — `discover_categories` then marks it `verified=True`.
Also builds the SearchConfig entries the setup wizard (CLI and web) creates.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..config import GeneralConfig, SearchConfig

if TYPE_CHECKING:  # duck-typed at runtime: anything with `async get_text(url)` works
    from .http import PoliteClient

log = logging.getLogger(__name__)

BASE_URL = "https://www.kleinanzeigen.de"
ELEKTRONIK_ID = 161
# Radius values the Kleinanzeigen search form offers (0 = only the place itself).
RADIUS_CHOICES: tuple[int, ...] = (0, 5, 10, 20, 30, 50, 100, 150, 200)
DEFAULT_LOCATION = "Berlin"
DEFAULT_RADIUS_KM = 30
DEFAULT_BUDGET = 400.0
DEFAULT_MIN_PROFIT = 40.0
# Title/description words that almost always mean "not a real offer" or "broken".
SCAN_EXCLUDE_KEYWORDS: tuple[str, ...] = ("defekt", "bastler", "suche", "tausch", "ersatzteil")
WISHLIST_EXCLUDE_KEYWORDS: tuple[str, ...] = ("defekt", "bastler", "suche", "tausch")
WISHLIST_HAGGLE_FACTOR = 1.2  # wishlist searches also show ads up to 20 % above your limit (VB: haggle)
CACHE_FILE = "categories.json"  # live discovery results, in general.data_dir
CACHE_MAX_AGE_DAYS = 7
INTERVAL_STEPS: tuple[int, ...] = (10, 15, 20, 30, 45, 60, 90, 120)
DEFAULT_REQUESTS_PER_HOUR = 150  # general.max_requests_per_hour when the config has no such field


@dataclass
class Category:
    """A Kleinanzeigen category (`c<id>` in search URLs)."""

    id: int
    name_de: str
    name_ru: str = ""
    parent: int | None = None
    resale_friendly: bool = False  # worth scanning for resale
    verified: bool = False  # id + name confirmed on a live page
    recommended: bool = False  # preselected in the setup wizard
    min_price: float | None = None  # suggested lower price bound for scans (skips cables, cases, junk)
    count: int | None = None  # ads in the region (from discovery)
    url: str = ""

    @property
    def label(self) -> str:
        return f"{self.name_de} — {self.name_ru}" if self.name_ru else self.name_de


@dataclass
class CategoryLink:
    """A category facet link found on a search page."""

    id: int
    name: str
    count: int | None = None
    url: str = ""
    parent: int | None = None


class CategoryList(list["Category"]):
    """Categories to offer in the wizard, plus how they were obtained.

    `discovered` holds the raw links from the live page (empty when discovery
    failed and the list is the built-in fallback; `error` then says why)."""

    def __init__(
        self,
        items: Iterable[Category] = (),
        *,
        discovered: Iterable[CategoryLink] = (),
        error: str = "",
        url: str = "",
    ) -> None:
        super().__init__(items)
        self.discovered: list[CategoryLink] = list(discovered)
        self.error = error
        self.url = url

    @property
    def live(self) -> bool:
        return bool(self.discovered)

    def by_id(self, category_id: int) -> Category | None:
        return next((c for c in self if c.id == category_id), None)

    fetched_at: datetime | None = None  # when the live data was fetched (cache)


# Curated for reselling. IDs from memory — never marked verified here.
BUILTIN_CATEGORIES: tuple[Category, ...] = (
    Category(173, "Handy & Telefon", "смартфоны и телефоны", ELEKTRONIK_ID, True, recommended=True, min_price=50),
    Category(278, "Notebooks", "ноутбуки", ELEKTRONIK_ID, True, recommended=True, min_price=60),
    Category(279, "Konsolen", "игровые консоли", ELEKTRONIK_ID, True, recommended=True, min_price=40),
    Category(225, "PC-Zubehör & Software", "комплектующие: видеокарты, процессоры, мониторы", ELEKTRONIK_ID,
             True, recommended=True, min_price=30),
    Category(245, "Foto", "фотоаппараты и объективы", ELEKTRONIK_ID, True, recommended=True, min_price=40),
    Category(172, "Audio & Hifi", "наушники, колонки, усилители", ELEKTRONIK_ID, True, recommended=True, min_price=30),
    Category(285, "Tablets & Reader", "планшеты и электронные книги", ELEKTRONIK_ID, True, recommended=True,
             min_price=40),
    Category(228, "PCs", "компьютеры целиком", ELEKTRONIK_ID, True, min_price=80),
    Category(175, "TV & Video", "телевизоры, проекторы", ELEKTRONIK_ID, True, min_price=50),
    Category(227, "Videospiele", "видеоигры", ELEKTRONIK_ID, True, min_price=15),
    Category(176, "Haushaltsgeräte", "бытовая техника", ELEKTRONIK_ID, True, min_price=30),
    Category(217, "Fahrräder & Zubehör", "велосипеды", 210, True, min_price=60),
    Category(74, "Musikinstrumente", "музыкальные инструменты", 73, True, min_price=40),
    Category(84, "Heimwerken", "инструменты и ремонт", 80, True, min_price=30),
    Category(168, "Weitere Elektronik", "прочая электроника", ELEKTRONIK_ID, False, min_price=20),
    Category(ELEKTRONIK_ID, "Elektronik", "вся электроника сразу (очень много объявлений)", None, False,
             min_price=30),
)
RECOMMENDED_IDS: tuple[int, ...] = tuple(c.id for c in BUILTIN_CATEGORIES if c.recommended)


def builtin_categories() -> CategoryList:
    """Fresh copies of the built-in list (callers may mutate them)."""
    return CategoryList(replace(c) for c in BUILTIN_CATEGORIES)


def builtin_by_id(category_id: int) -> Category | None:
    return next((replace(c) for c in BUILTIN_CATEGORIES if c.id == category_id), None)


# ------------------------------------------------------------------ parsing
# Last path element of a category search: c173 / c173l3331 / c173l3331r30 / k0c225l3331r20
_CATEGORY_CODE_RE = re.compile(r"^(?:k\d+)?c(\d+)(?:-?l\d+)?(?:-?r\d+)?$", re.IGNORECASE)
_NUMBER = r"\d{1,3}(?:[.\s]\d{3})+|\d+"
_TRAILING_COUNT_RE = re.compile(rf"(?:(?:^|\s)\(?|\()\s*({_NUMBER})\s*\)?$")
_ONLY_COUNT_RE = re.compile(rf"^\(?\s*({_NUMBER})\s*\)?$")
_WS_RE = re.compile(r"\s+")
_SKIP_TEXT_IN = ("script", "style", "svg", "noscript", "template")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\xa0", " ").replace(" ", " ")).strip()


def _to_count(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def category_id_from_url(href: str) -> int | None:
    """173 for '/s-berlin/handy-telefon/c173l3331r30'; None for ads, filters and other links."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    parts = urlsplit(href)
    if parts.netloc and "kleinanzeigen" not in parts.netloc.lower():
        return None
    path = unquote(parts.path).rstrip("/")
    if not path.startswith("/s-"):
        return None
    elements = [e for e in path[len("/s-"):].split("/") if e]
    # filters (anbieter:privat, preis:..., seite:2) or attribute facets (c173+handy_telefon.art_s:apple)
    if not elements or any(":" in e or "+" in e for e in elements):
        return None
    m = _CATEGORY_CODE_RE.match(elements[-1])
    return int(m.group(1)) if m else None


def _visible_text(el: Tag) -> str:
    parts = [
        str(s) for s in el.find_all(string=True)
        if not any(p.name in _SKIP_TEXT_IN for p in s.parents if isinstance(p, Tag))
    ]
    return _clean(" ".join(parts))


def _name_and_count(a: Tag) -> tuple[str, int | None]:
    text = _visible_text(a)
    m = _TRAILING_COUNT_RE.search(text)
    if m:
        return text[: m.start()].strip(" ·|-–:"), _to_count(m.group(1))
    return text, None


def _count_after(a: Tag) -> int | None:
    """Count right after the link: '<a>Foto</a> <span>(1.234)</span>' or '<a>Foto</a> (1.234)'."""
    for sib in a.next_siblings:
        if isinstance(sib, NavigableString):
            text = _clean(str(sib))
            if not text:
                continue
        elif isinstance(sib, Tag):
            if sib.name in ("ul", "ol", "a", "li", "div") and sib.find("a") is not None:
                return None
            text = _visible_text(sib)
            if not text:
                continue
        else:
            continue
        m = _ONLY_COUNT_RE.match(text)
        return _to_count(m.group(1)) if m else None
    return None


def _parent_id(a: Tag, own_id: int) -> int | None:
    """Nested facet lists: the category link of the enclosing <li> is the parent."""
    li = a.find_parent("li")
    outer = li.find_parent("li") if li is not None else None
    if outer is None:
        return None
    for cand in outer.find_all("a", href=True):
        cid = category_id_from_url(cand["href"])
        if cid is not None:
            return cid if cid != own_id else None
    return None


def parse_category_links(html: str) -> list[CategoryLink]:
    """Category facet links of a Kleinanzeigen search page, deduplicated by id.

    Works on links only (no CSS classes): any <a href> whose last path element is
    a `c<digits>` code. Name = link text without a trailing count; count = number
    in parentheses or in a trailing / following <span> when present."""
    soup = BeautifulSoup(html or "", "lxml")
    found: dict[int, CategoryLink] = {}
    for a in soup.find_all("a", href=True):
        cid = category_id_from_url(a["href"])
        if cid is None:
            continue
        name, count = _name_and_count(a)
        if count is None:
            count = _count_after(a)
        name = name or _clean(a.get("title") or "")
        if not name:
            continue
        link = CategoryLink(cid, name, count, urljoin(BASE_URL + "/", a["href"]), _parent_id(a, cid))
        known = found.get(cid)
        if known is None:
            found[cid] = link
            continue
        if known.count is None and count is not None:  # prefer the facet link (with count) over menus
            known.count, known.url = count, link.url
        if known.parent is None and link.parent is not None:
            known.parent = link.parent
    return list(found.values())


# ---------------------------------------------------------------- discovery
def _norm_name(name: str) -> str:
    low = name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    low = re.sub(r"\bund\b", " ", unicodedata.normalize("NFKD", low))
    return re.sub(r"[^a-z0-9]", "", low)


def _same_name(a: str, b: str) -> bool:
    x, y = _norm_name(a), _norm_name(b)
    return bool(x and y) and (x == y or x in y or y in x)


def merge_categories(links: Iterable[CategoryLink]) -> CategoryList:
    """Built-ins first (confirmed ones get verified/count/url), then other categories from the page."""
    links = list(links)
    by_id = {link.id: link for link in links}
    out = CategoryList(discovered=links)
    for cat in BUILTIN_CATEGORIES:
        link = by_id.get(cat.id)
        if link is None:
            out.append(replace(cat))
        elif _same_name(cat.name_de, link.name):
            out.append(replace(cat, verified=True, count=link.count, url=link.url))
        else:
            # our remembered id belongs to another category: trust the site, drop our advice
            log.warning("Kleinanzeigen category %s is %r, not %r", cat.id, link.name, cat.name_de)
            out.append(Category(cat.id, link.name, parent=link.parent, verified=True, count=link.count, url=link.url))
    builtin_ids = {c.id for c in BUILTIN_CATEGORIES}
    extras = [link for link in links if link.id not in builtin_ids]
    extras.sort(key=lambda link: -(link.count or 0))
    out.extend(
        Category(link.id, link.name, parent=link.parent, verified=True, count=link.count, url=link.url)
        for link in extras
    )
    return out


def discovery_url(location: str, radius_km: int, category_id: int | None = None) -> str:
    """Search-form URL with only region (+ category), no keywords or prices."""
    from .kleinanzeigen import (
        build_search_url,  # lazy: keeps this module light for the web app
    )

    probe = SearchConfig(name="categories", location=location.strip(), radius_km=radius_km or 0,
                         category_id=category_id)
    return build_search_url(probe, 1)


def describe_error(exc: BaseException) -> str:
    """Short Russian reason for the fallback notice."""
    kind = type(exc).__name__
    until = getattr(exc, "cooldown_until", None) or getattr(exc, "retry_at", None)
    when = f" до {until.astimezone():%H:%M}" if isinstance(until, datetime) else ""
    if kind == "RateBudgetExceeded":
        return f"исчерпан лимит запросов к сайту в этот час — попробуй{when or ' позже'}"
    if kind == "BlockedError":
        if getattr(exc, "cooling_down", False):
            return f"Kleinanzeigen на паузе после блокировки{when}"
        return "Kleinanzeigen временно не пускает (защита от ботов) — попробуй позже"
    status = getattr(exc, "status_code", None)
    if status:
        return f"сайт ответил HTTP {status}"
    if isinstance(exc, TimeoutError):
        return "kleinanzeigen.de не ответил вовремя"
    return f"нет связи с kleinanzeigen.de ({(str(exc) or kind)[:160]})"


def discovery_client(general: GeneralConfig | None = None, **kwargs: Any) -> PoliteClient:
    """PoliteClient for 1–2 discovery pages: the site's hourly cap and block cooldowns
    apply (from_config), but with a short pause and a single retry."""
    from .http import PoliteClient

    client = PoliteClient.from_config(general or GeneralConfig(), **kwargs)
    client.delay_range = (1.0, 2.5)
    client.max_retries = 1
    return client


async def discover_categories(
    client: PoliteClient,
    location: str,
    radius_km: int,
    *,
    expand: tuple[int, ...] = (ELEKTRONIK_ID,),
) -> CategoryList:
    """Categories that exist around `location`, merged with the built-in list.

    Fetches the region's search page (top-level facets) and then the pages of the
    `expand` categories (their sub-categories, e.g. Elektronik -> Handy & Telefon).
    Never raises (except cancellation): on any error the built-in list comes back
    with `.error` set."""
    url = discovery_url(location, radius_km)
    try:
        html = await client.get_text(url)
        links = parse_category_links(html)
    except Exception as exc:  # network, block page, parser surprise: fall back
        log.warning("Category discovery for %s failed: %s", location, exc)
        return CategoryList(builtin_categories(), error=describe_error(exc), url=url)
    if not links:
        return CategoryList(builtin_categories(), error="на странице сайта не нашлось списка категорий", url=url)
    final_url = getattr(client, "last_url", None) or url
    known = {link.id for link in links}
    for parent_id in expand:
        try:
            sub_links = parse_category_links(await client.get_text(discovery_url(location, radius_km, parent_id)))
        except Exception as exc:
            log.info("Sub-categories of %s not loaded: %s", parent_id, exc)
            continue
        for link in sub_links:
            if link.id not in known:
                if link.parent is None and link.id != parent_id:
                    link.parent = parent_id
                links.append(link)
                known.add(link.id)
            else:
                old = next(x for x in links if x.id == link.id)
                if old.count is None:
                    old.count = link.count
    result = merge_categories(links)
    result.url = final_url
    return result


# -------------------------------------------------------------------- cache
def _cache_key(location: str, radius_km: int) -> str:
    return f"{' '.join(location.lower().split())}|{int(radius_km or 0)}"


def load_cached_categories(
    data_dir: str | Path, location: str, radius_km: int, *, max_age_days: float = CACHE_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> CategoryList | None:
    """Live discovery result saved by `save_cached_categories`, if younger than `max_age_days`."""
    path = Path(data_dir) / CACHE_FILE
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))["entries"][_cache_key(location, radius_km)]
        fetched = datetime.fromisoformat(entry["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        links = [CategoryLink(**link) for link in entry["links"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (now or datetime.now(timezone.utc)) - fetched > timedelta(days=max_age_days) or not links:
        return None
    result = merge_categories(links)
    result.url = str(entry.get("url") or "")
    result.fetched_at = fetched
    return result


def save_cached_categories(data_dir: str | Path, location: str, radius_km: int, result: CategoryList) -> bool:
    """Remember a live discovery result (fallback lists are not cached). Never raises."""
    if not result.live:
        return False
    path = Path(data_dir) / CACHE_FILE
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data.get("entries"), dict):
            data = {"entries": {}}
    except (OSError, ValueError):
        data = {"entries": {}}
    fetched = result.fetched_at or datetime.now(timezone.utc)
    data["entries"][_cache_key(location, radius_km)] = {
        "fetched_at": fetched.isoformat(),
        "location": location.strip(),
        "radius_km": int(radius_km or 0),
        "url": result.url,
        "links": [asdict(link) for link in result.discovered],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("Cannot write %s: %s", path, exc)
        return False
    return True


def available_categories(data_dir: str | Path, location: str, radius_km: int) -> CategoryList:
    """Fast path, no requests: the cached live list for this region, else the built-in list."""
    return load_cached_categories(data_dir, location, radius_km) or builtin_categories()


# ---------------------------------------------------------- request estimate
SCAN_RESULT_PAGES = 3  # a category scan reads up to 3 result pages per pass (until it meets known ads)
REDIRECT_REQUESTS = 1  # the search form answers with a redirect to the real result page
COMPS_PAGES_PER_LOOKUP = 2.5  # one comparables lookup ≈ 2–3 result pages
RESULT_PAGES_SHARE = 0.4  # result pages may use at most 40 % of the hourly cap: the rest evaluates ads


@dataclass
class RequestEstimate:
    """Upper bound of Kleinanzeigen page requests a config makes, vs. general.max_requests_per_hour."""

    category_scans: int
    keyword_searches: int
    interval_minutes: float
    pages_per_run: int  # result pages (+ redirects) of all searches: always made
    extra_per_run: int  # ad pages + comparables pages, only for promising ads (per-run budgets)
    cap_per_hour: int  # general.max_requests_per_hour (0 = no cap)
    suggested_interval: int = 0

    @property
    def searches(self) -> int:
        return self.category_scans + self.keyword_searches

    @property
    def per_run(self) -> int:
        return self.pages_per_run + self.extra_per_run

    @property
    def runs_per_hour(self) -> float:
        return 60 / max(1.0, self.interval_minutes)

    @property
    def pages_per_hour(self) -> int:
        return int(round(self.pages_per_run * self.runs_per_hour))

    @property
    def per_hour(self) -> int:
        """Upper bound incl. evaluation of promising ads, capped by the hourly limit."""
        raw = int(round(self.per_run * self.runs_per_hour))
        return max(self.pages_per_hour, min(raw, self.cap_per_hour)) if self.cap_per_hour else raw

    @property
    def per_day(self) -> int:
        return self.per_hour * 24

    @property
    def evaluation_per_hour(self) -> int | None:
        """Requests per hour left for ad pages and comparables (None = no cap)."""
        return max(0, self.cap_per_hour - self.pages_per_hour) if self.cap_per_hour else None

    @property
    def tight(self) -> bool:
        """Result pages alone use over half the hourly cap: evaluation of ads would starve."""
        return bool(self.cap_per_hour) and self.pages_per_hour > self.cap_per_hour / 2

    def describe(self) -> str:
        parts = []
        if self.category_scans:
            parts.append(f"{self.category_scans} {_plural(self.category_scans, 'категория', 'категории', 'категорий')}"
                         f" × до {SCAN_RESULT_PAGES} стр.")
        if self.keyword_searches:
            parts.append(f"{self.keyword_searches} {_plural(self.keyword_searches, 'поиск', 'поиска', 'поисков')}"
                         " по словам")
        what = f" ({', '.join(parts)})" if parts else ""
        text = (f"Раз в {self.interval_minutes:g} мин: до {self.pages_per_hour} стр. выдачи в час{what}, "
                f"всего с оценкой объявлений — не больше {self.per_hour} запросов в час (~{self.per_day} в сутки).")
        if self.cap_per_hour:
            room = self.evaluation_per_hour
            text += (f" На оценку объявлений (страница объявления + 2–3 стр. цен аналогов) остаётся ~{room}"
                     f" в час из лимита {self.cap_per_hour}.")
        if self.tight:
            better = f" — поставь раз в {self.suggested_interval} мин" if self.suggested_interval else ""
            text += f" ⚠ Слишком часто: на оценку объявлений почти не останется запросов{better}."
        return text


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def request_budgets(general: GeneralConfig | None) -> tuple[int, int, int, int]:
    """(pages per category scan, pages per keyword search, evaluation requests per run, hourly cap)."""
    general = general or GeneralConfig()
    max_pages = max(1, int(getattr(general, "max_pages", 1) or 1))
    scan_pages = max(SCAN_RESULT_PAGES, max_pages) + REDIRECT_REQUESTS
    keyword_pages = max_pages + REDIRECT_REQUESTS
    details = int(getattr(general, "max_details_per_run", 40) or 0)
    comps = int(getattr(general, "max_comps_lookups_per_run", 40) or 0)
    extra = details + int(math.ceil(comps * COMPS_PAGES_PER_LOOKUP))
    cap = int(getattr(general, "max_requests_per_hour", DEFAULT_REQUESTS_PER_HOUR) or 0)
    return scan_pages, keyword_pages, extra, cap


def suggest_interval(category_scans: int, general: GeneralConfig | None = None, *, keyword_searches: int = 0) -> int:
    """Safe check interval: result pages (+ redirects) may use at most 40 % of the hourly
    request cap, never more often than every 10 min. ~7 category scans -> 30 min."""
    scan_pages, keyword_pages, _, cap = request_budgets(general)
    pages = category_scans * scan_pages + keyword_searches * keyword_pages
    wanted = 10.0
    if cap:
        wanted = max(wanted, pages * 60 / (cap * RESULT_PAGES_SHARE))
    else:
        wanted = max(wanted, 2.0 * (category_scans + keyword_searches))
    return next((step for step in INTERVAL_STEPS if step >= wanted - 1e-9), int(math.ceil(wanted / 30) * 30))


def estimate_requests(
    category_scans: int, interval_minutes: float, general: GeneralConfig | None = None, *, keyword_searches: int = 0
) -> RequestEstimate:
    scan_pages, keyword_pages, extra, cap = request_budgets(general)
    pages = category_scans * scan_pages + keyword_searches * keyword_pages
    return RequestEstimate(
        category_scans, keyword_searches, float(interval_minutes), pages, extra if pages else 0, cap,
        suggest_interval(category_scans, general, keyword_searches=keyword_searches),
    )


# ------------------------------------------------------------ search builders
def scan_name(category_name: str, location: str, radius_km: int | None) -> str:
    """'Handy & Telefon · Berlin 30 км'."""
    where = location.strip() or "вся Германия"
    if radius_km:
        where = f"{where} {radius_km} км"
    return f"{category_name} · {where}"


def category_search(
    category: Category,
    *,
    location: str,
    radius_km: int | None,
    max_price: float | None,
    purpose: str = "resale",
    min_profit: float | None = None,
) -> SearchConfig:
    """A category scan: every new ad of the category in the region, within the budget."""
    min_price = category.min_price
    if min_price is not None and max_price is not None and min_price >= max_price:
        min_price = None
    return SearchConfig(
        name=scan_name(category.name_de, location, radius_km),
        source="kleinanzeigen",
        category_id=category.id,
        category_name=category.name_de,
        location=location.strip(),
        radius_km=radius_km or None,
        min_price=min_price,
        max_price=max_price,
        purpose="personal" if purpose == "personal" else "resale",
        exclude_keywords=list(SCAN_EXCLUDE_KEYWORDS),
        min_profit=min_profit if purpose != "personal" else None,
    )


def wishlist_search(item: str, *, max_price: float | None, location: str, radius_km: int | None) -> SearchConfig:
    """A personal search: 'RTX 3090 up to 550 €' -> keyword search, savings vs market."""
    item = _clean(item)
    return SearchConfig(
        name=f"Для себя: {item}",
        source="kleinanzeigen",
        query=item,
        location=location.strip(),
        radius_km=radius_km or None,
        max_price=round(max_price * WISHLIST_HAGGLE_FACTOR) if max_price else None,
        purpose="personal",
        target_price=max_price or None,
        exclude_keywords=list(WISHLIST_EXCLUDE_KEYWORDS),
    )


def build_setup_searches(
    categories: Iterable[Category],
    *,
    location: str,
    radius_km: int | None,
    max_price: float | None,
    purpose: str = "resale",
    min_profit: float | None = None,
    wishlist: Iterable[tuple[str, float | None]] = (),
) -> list[SearchConfig]:
    """Category scans + wishlist searches with unique names."""
    out: list[SearchConfig] = []
    seen_cats: set[int] = set()
    for cat in categories:
        if cat.id in seen_cats:
            continue
        seen_cats.add(cat.id)
        out.append(category_search(cat, location=location, radius_km=radius_km, max_price=max_price,
                                   purpose=purpose, min_profit=min_profit))
    for item, price in wishlist:
        if _clean(item or ""):
            out.append(wishlist_search(item, max_price=price, location=location, radius_km=radius_km))
    names: set[str] = set()
    for i, search in enumerate(out):
        name, n = search.name, 2
        while name in names:
            name = f"{search.name} ({n})"
            n += 1
        names.add(name)
        if name != search.name:
            out[i] = search.model_copy(update={"name": name})
    return out


@dataclass
class SetupAnswers:
    """What the setup wizard (CLI or web) asks; `searches_from_answers` turns it into searches."""

    location: str = DEFAULT_LOCATION
    radius_km: int = DEFAULT_RADIUS_KM
    category_ids: list[int] = field(default_factory=lambda: list(RECOMMENDED_IDS))
    purpose: str = "resale"
    max_price: float | None = DEFAULT_BUDGET
    min_profit: float = DEFAULT_MIN_PROFIT
    wishlist: list[tuple[str, float | None]] = field(default_factory=list)
    interval_minutes: float | None = None  # general.interval_minutes; None = keep / suggest

    @property
    def keyword_count(self) -> int:
        return sum(1 for item, _ in self.wishlist if item.strip())

    @property
    def search_count(self) -> int:
        return len(self.category_ids) + self.keyword_count

    def suggested_interval(self, general: GeneralConfig | None = None) -> int:
        return suggest_interval(len(self.category_ids), general, keyword_searches=self.keyword_count)

    def estimate(self, interval_minutes: float, general: GeneralConfig | None = None) -> RequestEstimate:
        return estimate_requests(len(self.category_ids), interval_minutes, general,
                                 keyword_searches=self.keyword_count)


def is_category_scan(search: SearchConfig) -> bool:
    return search.source == "kleinanzeigen" and bool(search.category_id) and not search.query and not search.url


def answers_from_searches(searches: Iterable[SearchConfig], min_profit: float = DEFAULT_MIN_PROFIT) -> SetupAnswers:
    """Pre-fill the wizard from the current config (so re-running it changes, not resets)."""
    searches = list(searches)
    scans = [s for s in searches if is_category_scan(s)]
    answers = SetupAnswers(min_profit=min_profit)
    base = scans[0] if scans else next((s for s in searches if s.source == "kleinanzeigen" and s.location), None)
    if base is not None:
        answers.location = base.location or answers.location
        if base.radius_km is not None:
            answers.radius_km = base.radius_km
    if scans:
        answers.category_ids = list(dict.fromkeys(s.category_id for s in scans if s.category_id))
        answers.max_price = scans[0].max_price
        answers.purpose = scans[0].purpose
        if scans[0].min_profit is not None:
            answers.min_profit = scans[0].min_profit
    answers.wishlist = [
        (s.query, s.target_price) for s in searches
        if s.purpose == "personal" and s.source == "kleinanzeigen" and s.query and not s.url
    ]
    return answers


def searches_from_answers(
    answers: SetupAnswers, categories: Iterable[Category] = (), *, global_min_profit: float | None = None
) -> list[SearchConfig]:
    """Wizard answers -> SearchConfig list. `min_profit` is stored per search only when it
    differs from the global pricing.min_profit."""
    known = {c.id: c for c in categories}
    chosen = [
        known.get(cid) or builtin_by_id(cid) or Category(cid, f"Категория {cid}")
        for cid in answers.category_ids
    ]
    min_profit = None if global_min_profit is not None and answers.min_profit == global_min_profit else answers.min_profit
    return build_setup_searches(
        chosen,
        location=answers.location,
        radius_km=answers.radius_km,
        max_price=answers.max_price,
        purpose=answers.purpose,
        min_profit=min_profit,
        wishlist=answers.wishlist,
    )


def merge_searches(
    existing: Iterable[SearchConfig], generated: Iterable[SearchConfig], *, replace_all: bool
) -> list[SearchConfig]:
    """replace_all: only the generated ones. Otherwise same-name searches are updated in
    place, other existing ones kept, new ones appended."""
    generated = list(generated)
    if replace_all:
        return generated
    pending = {s.name: s for s in generated}
    out = [pending.pop(s.name, s) for s in existing]
    out.extend(s for s in generated if s.name in pending)
    return out


def snap_radius(radius_km: int) -> int:
    """Nearest radius the site offers: 15 -> 20, 25 -> 30 (ties go up), 500 -> 200."""
    radius_km = max(0, int(radius_km))
    return min(RADIUS_CHOICES, key=lambda r: (abs(r - radius_km), -r))


__all__ = [
    "BUILTIN_CATEGORIES",
    "Category",
    "CategoryLink",
    "CategoryList",
    "RADIUS_CHOICES",
    "RECOMMENDED_IDS",
    "RequestEstimate",
    "request_budgets",
    "SetupAnswers",
    "answers_from_searches",
    "available_categories",
    "build_setup_searches",
    "builtin_by_id",
    "builtin_categories",
    "category_id_from_url",
    "category_search",
    "discover_categories",
    "discovery_client",
    "discovery_url",
    "estimate_requests",
    "is_category_scan",
    "load_cached_categories",
    "merge_categories",
    "merge_searches",
    "parse_category_links",
    "save_cached_categories",
    "scan_name",
    "searches_from_answers",
    "snap_radius",
    "suggest_interval",
    "wishlist_search",
]

