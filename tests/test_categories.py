"""Category catalog, facet-link parsing, live discovery (offline) and the wizard's search builders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ebeyparser.config import GeneralConfig, SearchConfig
from ebeyparser.scraper.categories import (
    BUILTIN_CATEGORIES,
    RECOMMENDED_IDS,
    Category,
    CategoryLink,
    CategoryList,
    SetupAnswers,
    answers_from_searches,
    available_categories,
    build_setup_searches,
    builtin_categories,
    category_id_from_url,
    discover_categories,
    estimate_requests,
    load_cached_categories,
    merge_categories,
    merge_searches,
    parse_category_links,
    save_cached_categories,
    searches_from_answers,
    snap_radius,
    suggest_interval,
)
from ebeyparser.scraper.http import PoliteClient

# Region page in the redesigned markup: utility classes only, counts inside the link, in a
# following <span> with parentheses, as bare text; menus repeat links; filters look similar.
REGION_PAGE = """<!doctype html><html lang="de"><head><title>Kleinanzeigen Berlin</title></head><body>
<header class="flex items-center gap-4 px-4">
  <nav class="hidden lg:flex">
    <a class="px-3 py-2 text-sm" href="/s-autos/c216">Autos</a>
    <a class="px-3 py-2 text-sm" href="/s-multimedia-elektronik/c161">Elektronik</a>
    <a class="px-3 py-2 text-sm" href="https://themen.kleinanzeigen.de/c123/">Magazin</a>
    <a class="px-3 py-2 text-sm" href="/m-meine-anzeigen.html">Meine Anzeigen</a>
  </nav>
</header>
<main class="grid grid-cols-12 gap-6">
  <aside class="col-span-3 flex flex-col gap-2">
    <h2 class="text-base font-bold">Kategorien</h2>
    <ul class="flex flex-col gap-1">
      <li class="flex justify-between"><a class="truncate hover:underline" href="/s-berlin/l3331r30">Alle Kategorien</a></li>
      <li class="flex justify-between">
        <a class="flex w-full justify-between hover:underline" href="/s-berlin/auto-rad-boot/c210l3331r30">
          <span class="truncate">Auto, Rad &amp; Boot</span><span class="ml-2 text-gray-500 tabular-nums">24.512</span>
        </a>
      </li>
      <li class="flex flex-col">
        <a class="hover:underline" href="/s-berlin/multimedia-elektronik/c161l3331r30">Elektronik</a> <span class="text-sm text-gray-500">(98.765)</span>
        <ul class="ml-3 flex flex-col">
          <li><a class="hover:underline" href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a><span class="text-gray-500"> (12.345)</span></li>
          <li><a class="hover:underline" href="/s-berlin/notebooks/c278l3331r30">Notebooks (4.321)</a></li>
          <li><a class="hover:underline" href="/s-berlin/konsolen/c279l3331r30"><svg aria-hidden="true"><title>Pfeil</title></svg>Konsolen</a> 2.100</li>
          <li><a class="hover:underline" href="/s-berlin/anbieter:privat/c173l3331r30">Privat</a> <span>(9.000)</span></li>
          <li><a class="hover:underline" href="/s-berlin/c173l3331r30+handy_telefon.art_s:apple">Apple</a> (5.000)</li>
        </ul>
      </li>
      <li><a class="hover:underline" href="/s-berlin/haus-garten/c80l3331r30">Haus &amp; Garten</a></li>
    </ul>
  </aside>
  <section class="col-span-9">
    <article data-adid="3100000001">
      <a class="font-semibold" href="/s-anzeige/iphone-13-128gb/3100000001-173-3331">iPhone 13 128GB</a>
      <a href="/s-berlin/seite:2/c173l3331r30">2</a>
    </article>
  </section>
</main>
<footer><a href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a><a href="javascript:void(0)">x</a><a href="#top">↑</a></footer>
</body></html>"""

ELEKTRONIK_PAGE = """<html><body><ul>
<li><a href="/s-berlin/multimedia-elektronik/c161l3331r30">Elektronik</a> (98.765)
  <ul>
    <li><a href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a> <span>(12.345)</span></li>
    <li><a href="/s-berlin/pc-zubehoer-software/c225l3331r30">PC-Zubehör &amp; Software</a> <span>(6.001)</span></li>
    <li><a href="/s-berlin/foto/c245l3331r30">Foto</a> <span>(1.502)</span></li>
    <li><a href="/s-berlin/weitere/c9999l3331r30">Völlig neue Kategorie</a> <span>(77)</span></li>
  </ul>
</li></ul></body></html>"""


def _client(handler) -> PoliteClient:
    return PoliteClient(delay_range=(0, 0), max_retries=0, transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------- catalog
def test_builtin_catalog_is_honest_and_complete() -> None:
    ids = [c.id for c in BUILTIN_CATEGORIES]
    assert len(ids) == len(set(ids))
    names = {c.id: c.name_de for c in BUILTIN_CATEGORIES}
    expected = {161: "Elektronik", 172: "Audio & Hifi", 245: "Foto", 173: "Handy & Telefon", 176: "Haushaltsgeräte",
                279: "Konsolen", 278: "Notebooks", 228: "PCs", 225: "PC-Zubehör & Software", 285: "Tablets & Reader",
                175: "TV & Video", 227: "Videospiele", 168: "Weitere Elektronik", 217: "Fahrräder & Zubehör",
                74: "Musikinstrumente"}
    for cid, name in expected.items():
        assert names[cid] == name
    assert all(not c.verified for c in BUILTIN_CATEGORIES)  # from memory until a live page confirms
    assert all(c.name_ru for c in BUILTIN_CATEGORIES)
    assert set(RECOMMENDED_IDS) == {173, 278, 279, 225, 245, 172, 285}
    assert all(c.resale_friendly for c in BUILTIN_CATEGORIES if c.recommended)
    # copies: callers may mutate
    first = builtin_categories()
    first[0].verified = True
    assert not builtin_categories()[0].verified


@pytest.mark.parametrize(("href", "expected"), [
    ("/s-berlin/handy-telefon/c173l3331r30", 173),
    ("/s-handy-telefon/c173", 173),
    ("https://www.kleinanzeigen.de/s-notebooks/laptop/k0c278", 278),
    ("/s-berlin/rtx-3090/k0c225l3331r20", 225),
    ("/s-berlin/l3331r30", None),  # all categories
    ("/s-anzeige/iphone-13/3100000001-173-3331", None),  # an ad
    ("/s-berlin/anbieter:privat/c173l3331r30", None),  # filter facet
    ("/s-berlin/c173l3331r30+handy_telefon.art_s:apple", None),  # attribute facet
    ("/s-berlin/seite:2/c173l3331r30", None),  # pagination
    ("https://themen.kleinanzeigen.de/c123/", None),
    ("https://example.com/s-foo/c12", None),
    ("#top", None),
    ("javascript:void(0)", None),
])
def test_category_id_from_url(href: str, expected: int | None) -> None:
    assert category_id_from_url(href) == expected


# ------------------------------------------------------------------- parsing
def test_parse_category_links_utility_markup() -> None:
    links = {link.id: link for link in parse_category_links(REGION_PAGE)}
    assert set(links) == {216, 161, 210, 173, 278, 279, 80}
    assert links[210].name == "Auto, Rad & Boot" and links[210].count == 24512  # count in a span inside the link
    assert links[161].name == "Elektronik" and links[161].count == 98765  # (count) in the next span
    assert links[161].url == "https://www.kleinanzeigen.de/s-berlin/multimedia-elektronik/c161l3331r30"  # facet wins
    assert links[173].name == "Handy & Telefon" and links[173].count == 12345 and links[173].parent == 161
    assert links[278].name == "Notebooks" and links[278].count == 4321  # "(4.321)" inside the link text
    assert links[279].name == "Konsolen" and links[279].count == 2100  # bare number after, svg title ignored
    assert links[80].count is None and links[216].count is None
    assert links[173].url.startswith("https://www.kleinanzeigen.de/s-berlin/handy-telefon/c173")


def test_parse_category_links_tolerates_garbage() -> None:
    assert parse_category_links("") == []
    assert parse_category_links("<html><a href='/s-berlin/l3331'>Berlin</a></html>") == []
    only_count = '<a href="/s-foto/c245">(1.234)</a><a href="/s-foto/c245" title="Foto"></a>'
    assert [(c.id, c.name) for c in parse_category_links(only_count)] == [(245, "Foto")]


def test_merge_categories_marks_verified_and_keeps_extras() -> None:
    links = parse_category_links(REGION_PAGE) + [CategoryLink(245, "Kameras & Zubehör", 10, "u")]
    merged = merge_categories(links)
    by_id = {c.id: c for c in merged}
    assert by_id[173].verified and by_id[173].count == 12345 and by_id[173].recommended
    assert by_id[161].verified  # "Elektronik" vs slug multimedia-elektronik: same category
    assert not by_id[225].verified and by_id[225].count is None  # not on this page
    # remembered id with a different name on the site: trust the site, no advice
    assert by_id[245].verified and by_id[245].name_de == "Kameras & Zubehör"
    assert not by_id[245].recommended and not by_id[245].resale_friendly
    extras = [c for c in merged if c.id not in {b.id for b in BUILTIN_CATEGORIES}]
    assert [c.id for c in extras] == [210, 216, 80]  # by ad count, ties in page order
    assert all(c.verified and not c.resale_friendly for c in extras)
    assert merged.live and merged.by_id(210).name_de == "Auto, Rad & Boot"


# ----------------------------------------------------------------- discovery
async def test_discover_categories_live_and_expands_elektronik() -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True)
        seen.append(params)
        page = ELEKTRONIK_PAGE if params.get("categoryId") == ["161"] else REGION_PAGE
        return httpx.Response(200, text=page)

    client = _client(handler)
    try:
        result = await discover_categories(client, "Berlin", 30)
    finally:
        await client.aclose()
    assert len(seen) == 2
    assert seen[0]["locationStr"] == ["Berlin"] and seen[0]["radius"] == ["30"]
    assert seen[0]["keywords"] == [""] and seen[0]["categoryId"] == [""]
    assert result.live and not result.error and result.url.startswith("https://www.kleinanzeigen.de/s-suchanfrage.html")
    by_id = {c.id: c for c in result}
    assert by_id[225].verified and by_id[225].count == 6001 and by_id[225].parent == 161  # from the 2nd page
    assert by_id[245].verified and by_id[245].count == 1502
    assert by_id[9999].name_de == "Völlig neue Kategorie" and by_id[9999].parent == 161
    assert not by_id[285].verified  # Tablets not on either page


@pytest.mark.parametrize("failure", ["network", "blocked", "empty"])
async def test_discover_categories_falls_back_to_builtins(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError("no route to host")
        if failure == "blocked":
            return httpx.Response(403, text="Zugriff verweigert")
        return httpx.Response(200, text="<html><body><p>Keine Kategorien</p></body></html>")

    client = _client(handler)
    try:
        result = await discover_categories(client, "Berlin", 30)
    finally:
        await client.aclose()
    assert isinstance(result, CategoryList) and not result.live and result.error
    assert [c.id for c in result] == [c.id for c in BUILTIN_CATEGORIES]
    assert not any(c.verified for c in result)
    if failure == "blocked":
        assert "не пускает" in result.error or "HTTP 403" in result.error


async def test_discover_keeps_top_level_when_expansion_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "categoryId=161" in str(request.url):
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text=REGION_PAGE)

    client = _client(handler)
    try:
        result = await discover_categories(client, "10115", 10)
    finally:
        await client.aclose()
    assert result.live and result.by_id(173).verified and not result.by_id(225).verified


# --------------------------------------------------------------------- cache
def test_category_cache_roundtrip_and_expiry(tmp_path) -> None:
    live = merge_categories(parse_category_links(REGION_PAGE))
    live.url = "https://www.kleinanzeigen.de/s-berlin/l3331r30"
    assert not save_cached_categories(tmp_path, "Berlin", 30, builtin_categories())  # fallbacks are not cached
    assert save_cached_categories(tmp_path, "Berlin", 30, live)
    cached = load_cached_categories(tmp_path, " berlin ", 30)
    assert cached is not None and cached.live and cached.url == live.url
    assert cached.by_id(173).count == 12345 and cached.fetched_at is not None
    assert load_cached_categories(tmp_path, "Berlin", 50) is None  # other radius
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert load_cached_categories(tmp_path, "Berlin", 30, now=later) is None  # older than 7 days
    assert available_categories(tmp_path, "Hamburg", 30).live is False
    (tmp_path / "categories.json").write_text("{broken", encoding="utf-8")
    assert load_cached_categories(tmp_path, "Berlin", 30) is None


# ------------------------------------------------------------ search builders
def test_build_setup_searches() -> None:
    cats = [c for c in builtin_categories() if c.id in (173, 278)]
    searches = build_setup_searches(cats + cats[:1], location="Berlin", radius_km=30, max_price=400,
                                    min_profit=60, wishlist=[("RTX 3090", 550), ("  ", 10), ("RTX 3090", None)])
    names = [s.name for s in searches]
    assert names == ["Handy & Telefon · Berlin 30 км", "Notebooks · Berlin 30 км", "Для себя: RTX 3090",
                     "Для себя: RTX 3090 (2)"]
    phone = searches[0]
    assert phone.category_id == 173 and phone.category_name == "Handy & Telefon" and not phone.query
    assert phone.location == "Berlin" and phone.radius_km == 30 and phone.max_price == 400 and phone.min_price == 50
    assert phone.purpose == "resale" and phone.min_profit == 60
    assert phone.exclude_keywords == ["defekt", "bastler", "suche", "tausch", "ersatzteil"]
    wish = searches[2]
    assert wish.purpose == "personal" and wish.query == "RTX 3090" and wish.target_price == 550
    assert wish.max_price == 660 and wish.min_profit is None and wish.category_id is None
    assert searches[3].target_price is None and searches[3].max_price is None
    # budget below the category's junk threshold: no contradicting min_price
    cheap = build_setup_searches(cats[:1], location="10115", radius_km=0, max_price=40, purpose="personal")[0]
    assert cheap.min_price is None and cheap.radius_km is None and cheap.purpose == "personal"
    assert cheap.name == "Handy & Telefon · Mitte" and cheap.min_profit is None  # the district, not "10115"


def test_answers_roundtrip_and_merge() -> None:
    answers = SetupAnswers(location="Leipzig", radius_km=20, category_ids=[279, 4242], max_price=250,
                           min_profit=40, wishlist=[("Steam Deck", 300)])
    generated = searches_from_answers(answers, [Category(4242, "Sammeln")], global_min_profit=40)
    assert [s.name for s in generated] == ["Konsolen · Leipzig 20 км", "Sammeln · Leipzig 20 км",
                                           "Для себя: Steam Deck"]
    assert all(s.min_profit is None for s in generated)  # same as the global value
    assert searches_from_answers(answers, global_min_profit=30)[0].min_profit == 40
    back = answers_from_searches(generated, 40)
    assert (back.location, back.radius_km, back.category_ids, back.max_price) == ("Leipzig", 20, [279, 4242], 250)
    assert back.wishlist == [("Steam Deck", 300)] and back.search_count == 3

    manual = SearchConfig(name="Мой поиск", query="dyson")
    same_name = generated[0].model_copy(update={"max_price": 999})
    merged = merge_searches([manual, same_name], generated, replace_all=False)
    assert [s.name for s in merged] == ["Мой поиск", "Konsolen · Leipzig 20 км", "Sammeln · Leipzig 20 км",
                                        "Для себя: Steam Deck"]
    assert merged[1].max_price == 250  # updated in place
    assert merge_searches([manual], generated, replace_all=True) == generated


def test_radius_interval_and_request_estimate() -> None:
    assert [snap_radius(r) for r in (0, 3, 15, 25, 30, 40, 75, 500)] == [0, 5, 20, 30, 30, 50, 100, 200]
    general = GeneralConfig()  # 150 pages/hour, 40 ad pages + 40 comparables lookups per run
    # a category scan: up to 3 result pages + the search-form redirect; a keyword search: 1 + redirect
    assert suggest_interval(7, general) == 30 and suggest_interval(7, general, keyword_searches=1) == 30
    assert suggest_interval(3, general) == 15 and suggest_interval(0, general, keyword_searches=1) == 10
    assert suggest_interval(12, general) == 60
    est = estimate_requests(7, 30, general, keyword_searches=1)
    assert est.pages_per_run == 7 * 4 + 2 and est.pages_per_hour == 60
    assert est.extra_per_run == 40 + 100  # ad pages + comparables (≈ 2.5 pages per lookup)
    assert est.per_hour == 150 and est.evaluation_per_hour == 90 and not est.tight
    text = est.describe()
    assert "до 60 стр. выдачи в час" in text and "7 категорий × до 3 стр." in text and "остаётся ~90" in text
    busy = estimate_requests(7, 15, general, keyword_searches=1)
    assert busy.tight and busy.evaluation_per_hour == 30 and "поставь раз в 30 мин" in busy.describe()
    assert estimate_requests(0, 15, general).per_hour == 0
    answers = SetupAnswers(category_ids=[173, 278], wishlist=[("RTX 3090", 550), ("", None)])
    assert answers.keyword_count == 1 and answers.suggested_interval(general) == 10
    assert answers.estimate(15, general).pages_per_run == 10
