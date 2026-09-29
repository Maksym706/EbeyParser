"""v0.2 scraping: card prices, 'similar ads' cutoff, page-until-seen, date sorting,
debug dumps, seller trust signals, medium images, eBay comparables."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ebeyparser.config import EbayConfig, SearchConfig
from ebeyparser.scraper.ebay_api import EbayBrowseClient
from ebeyparser.scraper.http import PoliteClient, RateBudgetExceeded
from ebeyparser.scraper.kleinanzeigen import (
    SELLER_ATTRIBUTE_KEYS,
    KleinanzeigenScraper,
    build_search_url,
    medium_image_url,
    parse_ad_detail,
    parse_search_results,
    with_date_sorting,
)

FIXTURES = Path(__file__).parent / "fixtures"
KA_PRETTY_URL = "https://www.kleinanzeigen.de/s-berlin/rtx-3090/k0l3331r20"
KA_PAGE2_URL = "https://www.kleinanzeigen.de/s-berlin/seite:2/rtx-3090/k0l3331r20"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def card(ad_id: str, title: str, body: str) -> str:
    return (f'<li class="relative"><div><h2><a href="/s-anzeige/x/{ad_id}-225-3331">{title}</a></h2>'
            f"{body}<span>10115 Mitte</span></div></li>")


def page(*cards: str, between: str = "") -> str:
    head, tail = cards[:1], cards[1:]
    return ("<html><head><title>x | kleinanzeigen.de</title></head><body><p>25 Ergebnisse</p><ul>"
            + "".join(head) + between + "".join(tail) + "</ul></body></html>")


# ---------------------------------------------------------------- card prices


@pytest.mark.parametrize(
    ("body", "price", "text"),
    [
        # a "NP" amount in the description comes first in the text: the price element wins
        ("<p>Top Zustand, NP 1.200 € laut Rechnung</p><p>450 € VB</p>", 450.0, "450 € VB"),
        # shipping cost before the price
        ("<p>+ Versand ab 6,99 €</p><p><strong>80 €</strong></p>", 80.0, "80 €"),
        ("<p><span>Versand</span> <span>5 €</span></p><p>35 €</p>", 35.0, "35 €"),
        # struck-through old price via inline style
        ('<p><span style="text-decoration: line-through">399 €</span> <span>349 €</span></p>', 349.0, "349 €"),
        ("<p>Zu verschenken</p>", 0.0, "Zu verschenken"),
        ("<p>VB</p>", None, "VB"),
        # no price element: first amount in the text that is not labelled UVP / inkl.
        ("<p>UVP: 999 € — jetzt nur 650 € VB, inkl. Versand 660 €</p>", 650.0, "650 € VB"),
    ],
)
def test_generic_card_price(body: str, price: float | None, text: str) -> None:
    ads = parse_search_results(page(card("3500000001", "Artikel", body)))
    assert len(ads) == 1
    assert (ads[0].price, ads[0].price_text) == (price, text)


# ---------------------------------------------------------------- similar ads


@pytest.mark.parametrize(
    "heading",
    [
        "<h2>Das könnte dich auch interessieren</h2>",
        "<li><h3>Ähnliche Anzeigen</h3></li>",
        '<div class="x"><span>Anzeigen außerhalb deines Suchradius</span></div>',
        "<h2>Weitere Anzeigen in der Nähe</h2>",
    ],
)
def test_cards_after_similar_ads_heading_are_ignored(heading: str) -> None:
    html = page(card("3500000001", "RTX 3080", "<p>450 €</p>"), card("3500000002", "RTX 3070", "<p>300 €</p>"),
                card("3500000009", "Alte Karte woanders", "<p>100 €</p>"))
    html = html.replace('<li class="relative"><div><h2><a href="/s-anzeige/x/3500000009',
                        heading + '<li class="relative"><div><h2><a href="/s-anzeige/x/3500000009')
    assert [a.ad_id for a in parse_search_results(html)] == ["3500000001", "3500000002"]


def test_similar_ads_markers_that_are_not_headings() -> None:
    # a menu link before the results and an ad description mentioning the words do not cut
    html = page(
        card("3500000001", "RTX 3080", "<p>Ähnliche Anzeigen findest du in meinem Profil</p><p>450 €</p>"),
        card("3500000002", "RTX 3070", "<p>300 €</p>"),
    ).replace("<ul>", '<nav><a href="/s-umgebung">Anzeigen in der Nähe</a></nav><ul>', 1)
    assert [a.ad_id for a in parse_search_results(html)] == ["3500000001", "3500000002"]


# ---------------------------------------------------------------- page until seen


def two_page_client(calls: list[str], page2_budget: bool = False) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == KA_PRETTY_URL:
            return httpx.Response(200, text=fixture("ka_search_page1.html"))
        if url == KA_PAGE2_URL:
            return httpx.Response(200, text=fixture("ka_search_page2.html"))
        return httpx.Response(404)

    return PoliteClient(delay_range=(0, 0), transport=httpx.MockTransport(handler),
                        max_requests_per_hour=1 if page2_budget else None)


@pytest.mark.parametrize(
    ("known", "expected_calls"),
    [
        (set(), 2),  # nothing known yet: keep paging (up to max_pages / the last page)
        ({"2891000001"}, 2),  # only the TOP ad is known: top ads are pinned, keep paging
        ({"2891000006"}, 1),  # a regular ad on page 1 is known: page 2 holds only older ads
    ],
)
async def test_search_pages_until_a_known_ad(known: set[str], expected_calls: int) -> None:
    calls: list[str] = []
    async with two_page_client(calls) as client:
        scraper = KleinanzeigenScraper(client)
        ads = await scraper.search(SearchConfig(name="x", url=KA_PRETTY_URL), max_pages=5, seen=known.__contains__)
    assert len(calls) == expected_calls
    assert len(ads) == (7 if expected_calls == 1 else 9)


async def test_search_old_signature_still_works() -> None:
    calls: list[str] = []
    async with two_page_client(calls) as client:
        ads = await KleinanzeigenScraper(client).search(SearchConfig(name="x", url=KA_PRETTY_URL), 1)
    assert calls == [KA_PRETTY_URL] and len(ads) == 7


async def test_budget_on_page_two_keeps_page_one() -> None:
    calls: list[str] = []
    async with two_page_client(calls, page2_budget=True) as client:
        scraper = KleinanzeigenScraper(client)
        ads = await scraper.search(SearchConfig(name="x", url=KA_PRETTY_URL), max_pages=3)
        assert calls == [KA_PRETTY_URL] and len(ads) == 7
        with pytest.raises(RateBudgetExceeded):  # page 1 itself: the caller must defer
            await scraper.search(SearchConfig(name="x", url=KA_PRETTY_URL), max_pages=3)


# ---------------------------------------------------------------- date sorting


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.kleinanzeigen.de/s-sortierung:preis/rtx-3090/k0", "https://www.kleinanzeigen.de/s-rtx-3090/k0"),
        ("https://www.kleinanzeigen.de/s-berlin/sortierung:entfernung/rtx/k0l3331r20?x=1",
         "https://www.kleinanzeigen.de/s-berlin/rtx/k0l3331r20?x=1"),
        ("https://www.kleinanzeigen.de/s-sortierung:neueste/rtx-3090/k0", "https://www.kleinanzeigen.de/s-sortierung:neueste/rtx-3090/k0"),
        (KA_PRETTY_URL, KA_PRETTY_URL),
        ("https://www.kleinanzeigen.de/m-meine-anzeigen.html", "https://www.kleinanzeigen.de/m-meine-anzeigen.html"),
    ],
)
def test_with_date_sorting_pretty_urls(url: str, expected: str) -> None:
    assert with_date_sorting(url) == expected


def test_with_date_sorting_form_urls() -> None:
    form = "https://www.kleinanzeigen.de/s-suchanfrage.html?keywords=rtx&sortingField=SORTING_PRICE&pageNum=1"
    q = parse_qs(urlsplit(with_date_sorting(form)).query)
    assert q["sortingField"] == ["SORTING_DATE"] and q["keywords"] == ["rtx"]
    bare = "https://www.kleinanzeigen.de/s-suchanfrage.html?keywords=rtx"
    assert parse_qs(urlsplit(with_date_sorting(bare)).query)["sortingField"] == ["SORTING_DATE"]
    already = "https://www.kleinanzeigen.de/s-suchanfrage.html?keywords=rtx&sortingField=SORTING_DATE"
    assert with_date_sorting(already) == already
    pasted = SearchConfig(name="x", url="https://www.kleinanzeigen.de/s-sortierung:preis/rtx-3090/k0")
    assert build_search_url(pasted, 2) == "https://www.kleinanzeigen.de/s-seite:2/rtx-3090/k0"


# ---------------------------------------------------------------- debug dumps


def test_debug_page_once_per_search_per_day(tmp_path) -> None:
    scraper = KleinanzeigenScraper(client=None, debug_dir=tmp_path)  # type: ignore[arg-type]
    first = scraper.save_debug_page("<html>1</html>", "Видеокарты Берлин")
    second = scraper.save_debug_page("<html>2</html>", "Видеокарты Берлин")
    other = scraper.save_debug_page("<html>3</html>", "Handys")
    assert first == second and first != other
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([first.name, other.name])
    assert first.read_text(encoding="utf-8") == "<html>2</html>"  # the latest page of the day
    assert KleinanzeigenScraper(client=None).save_debug_page("x", "y") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------- seller trust signals


def test_seller_signals_from_contact_box() -> None:
    html = """<html><body><h1 id="viewad-title">iPhone 13</h1>
      <div id="viewad-description-text">Sehr freundlich verpackt. 99 Anzeigen gesehen?</div>
      <div id="viewad-contact">
        <span class="userprofile-vip"><a href="/s-bestandsliste.html?userId=1">Mia</a></span>
        <span class="userprofile-vip-details-text">Gewerblicher Nutzer</span>
        <span class="userprofile-vip-details-text">Aktiv seit 01.02.2023</span>
        <ul class="badges-iconlist"><li>OK Zufriedenheit</li><li>Besonders zuverlässig</li></ul>
        <p>Antwortet in der Regel innerhalb von 10 Minuten</p>
        <a href="/s-bestandsliste.html?userId=1">14 Anzeigen online</a>
      </div></body></html>"""
    ad = parse_ad_detail(html, url="https://www.kleinanzeigen.de/s-anzeige/x/3511111111-173-1")
    assert {k: ad.attributes[k] for k in SELLER_ATTRIBUTE_KEYS} == {
        "Nutzertyp": "Gewerblicher Nutzer",
        "Aktiv seit": "01.02.2023",
        "Bewertung": "OK Zufriedenheit, Besonders zuverlässig",
        "Anzeigen des Verkäufers": "14",
        "Antwortzeit": "innerhalb von 10 Minuten",
    }
    assert ad.seller_type == "commercial"


def test_seller_signals_without_seller_box() -> None:
    html = """<html><body><h1>Sofa</h1><div itemprop="description">Sehr freundlich, TOP Zufriedenheit!</div>
      <section><p>Privater Nutzer · Aktiv seit 2019</p></section></body></html>"""
    ad = parse_ad_detail(html, url="https://www.kleinanzeigen.de/s-anzeige/x/3522222222-88-1")
    assert ad.attributes == {"Nutzertyp": "Privater Nutzer", "Aktiv seit": "2019"}  # no badges from the text


def test_prompt_and_scraper_agree_on_seller_keys() -> None:
    from ebeyparser.ai import prompts

    assert prompts.SELLER_ATTRIBUTE_KEYS == SELLER_ATTRIBUTE_KEYS


def test_medium_image_url() -> None:
    img = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/bb"
    assert medium_image_url(f"{img}?rule=$_59.AUTO") == f"{img}?rule=$_57.JPG"
    assert medium_image_url(f"{img}?rule=%24_2.AUTO") == f"{img}?rule=$_57.JPG"
    assert medium_image_url("https://example.com/a.jpg") == "https://example.com/a.jpg"


# ---------------------------------------------------------------- eBay API


def _item(item_id: str, price: str, **extra) -> dict:
    return {"itemId": f"v1|{item_id}|0", "title": f"Item {item_id}", "price": {"value": price},
            "buyingOptions": ["FIXED_PRICE"], "condition": "Gebraucht",
            "itemWebUrl": f"https://www.ebay.de/itm/{item_id}", **extra}


async def test_ebay_comparables_used_private_only() -> None:
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        filters.append(request.url.params["filter"])
        return httpx.Response(200, json={"itemSummaries": [
            _item("1", "300"),
            _item("2", "250", sellerAccountType="BUSINESS"),
            _item("3", "280", seller={"username": "shop", "sellerAccountType": "BUSINESS"}),
            _item("4", "310", seller={"username": "max", "sellerAccountType": "INDIVIDUAL"}),
        ]})

    client = EbayBrowseClient(EbayConfig(client_id="id", client_secret="s"), transport=httpx.MockTransport(handler))
    comps = await client.comparables("rtx 3080")
    await client.aclose()
    assert [c.price for c in comps] == [300.0, 310.0]
    assert "conditions:{USED}" in filters[0] and "buyingOptions:{FIXED_PRICE}" in filters[0]
    assert filters[0].endswith("sellerAccountTypes:{INDIVIDUAL}")


async def test_ebay_comparables_retry_without_seller_filter_on_400() -> None:
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        filters.append(request.url.params["filter"])
        if "sellerAccountTypes" in request.url.params["filter"]:
            return httpx.Response(400, json={"errors": [{"message": "Invalid filter sellerAccountTypes"}]})
        return httpx.Response(200, json={"itemSummaries": [_item("1", "300"), _item("2", "250", sellerAccountType="BUSINESS")]})

    client = EbayBrowseClient(EbayConfig(client_id="id", client_secret="s"), transport=httpx.MockTransport(handler))
    comps = await client.comparables("rtx 3080")
    await client.aclose()
    assert [c.price for c in comps] == [300.0] and len(filters) == 2
    assert "sellerAccountTypes" not in filters[1] and "conditions:{USED}" in filters[1]


async def test_ebay_search_pages_until_seen() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(200, json={"total": 500, "itemSummaries": [
            _item(str(900000000000 + offset + i), "100") for i in range(50)]})

    client = EbayBrowseClient(EbayConfig(client_id="id", client_secret="s"), transport=httpx.MockTransport(handler))
    search = SearchConfig(name="t", source="ebay", query="thinkpad")
    ads = await client.search(search, max_pages=5, seen=lambda ad_id: ad_id == "ebay-900000000060")
    assert offsets == [0, 50] and len(ads) == 100  # page 2 contains a known item: stop
    offsets.clear()
    await client.search(search, max_pages=3)
    assert offsets == [0, 50, 100]
    await client.aclose()
