"""Offline tests for the scraper layer (HTML fixtures + httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ebeyparser.config import GeneralConfig, SearchConfig
from ebeyparser.models import Listing
from ebeyparser.scraper.ebay_sold import (
    EbaySoldScraper,
    build_sold_url,
    parse_ebay_price,
    parse_sold_results,
)
from ebeyparser.scraper.http import BlockedError, PoliteClient, looks_blocked
from ebeyparser.scraper.kleinanzeigen import (
    BASE_URL,
    KleinanzeigenScraper,
    build_search_url,
    large_image_url,
    next_page_url,
    page_url,
    parse_ad_detail,
    parse_price,
    parse_search_results,
)

FIXTURES = Path(__file__).parent / "fixtures"
KA_PRETTY_URL = "https://www.kleinanzeigen.de/s-berlin/rtx-3090/k0l3331r20"
KA_PAGE2_URL = "https://www.kleinanzeigen.de/s-berlin/seite:2/rtx-3090/k0l3331r20"
IMG = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/d5/d5e6f7a8-9b0c-4d1e-8f2a-555555555555"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def html_response(text: str, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, text=text, headers={"Content-Type": "text/html; charset=utf-8", **(headers or {})})


class SleepRecorder:
    """Replacement for PoliteClient._sleep that records instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(handler, *, delay_range=(0.0, 0.0), max_retries: int = 3, **kwargs) -> tuple[PoliteClient, SleepRecorder]:
    client = PoliteClient(
        delay_range=delay_range, max_retries=max_retries, transport=httpx.MockTransport(handler), **kwargs
    )
    recorder = SleepRecorder()
    client._sleep = recorder  # type: ignore[method-assign]
    return client, recorder


def query_of(url: str | httpx.URL) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(url)).query, keep_blank_values=True)


# ---------------------------------------------------------------------------
# Kleinanzeigen: pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.250 € VB", (1250.0, True, False)),
        ("35 €", (35.0, False, False)),
        ("1.234,50 €", (1234.5, False, False)),
        ("VB", (None, True, False)),
        ("Zu verschenken", (0.0, False, True)),
        ("", (None, False, False)),
        ("   ", (None, False, False)),
        ("\n    1.250\xa0€ VB\n  ", (1250.0, True, False)),
        ("720 € 649 € VB", (649.0, True, False)),  # old + new price as plain text: current wins
        ("12.500 €", (12500.0, False, False)),
        ("0 €", (0.0, False, False)),
        ("99,99 € VB", (99.99, True, False)),
        ("Verhandlungsbasis", (None, True, False)),
    ],
)
def test_parse_price(text: str, expected: tuple) -> None:
    assert parse_price(text) == expected


def test_build_search_url_from_fields() -> None:
    search = SearchConfig(
        name="rtx", query="rtx 3090", location="Berlin", radius_km=20, category_id=225,
        min_price=100, max_price=599.5,
    )
    url = build_search_url(search)
    assert url.startswith("https://www.kleinanzeigen.de/s-suchanfrage.html?")
    assert "keywords=rtx+3090" in url
    q = query_of(url)
    assert q == {
        "keywords": ["rtx 3090"],
        "categoryId": ["225"],
        "locationStr": ["Berlin"],
        "locationId": [""],
        "radius": ["20"],
        "sortingField": ["SORTING_DATE"],
        "adType": ["OFFER"],
        "posterType": [""],
        "pageNum": ["1"],
        "action": ["find"],
        "maxPrice": ["600"],  # rounded up so nothing is missed
        "minPrice": ["100"],
    }
    assert query_of(build_search_url(search, page=3))["pageNum"] == ["3"]


def test_build_search_url_defaults_and_encoding() -> None:
    search = SearchConfig(name="x", query="iphone 13 pro", location="München", location_id=6411)
    url = build_search_url(search)
    assert "locationStr=M%C3%BCnchen" in url
    q = query_of(url)
    assert q["radius"] == ["0"]
    assert q["categoryId"] == [""]
    assert q["locationId"] == ["6411"]
    assert q["minPrice"] == [""] and q["maxPrice"] == [""]


def test_build_search_url_uses_pasted_url() -> None:
    search = SearchConfig(name="x", url=KA_PRETTY_URL, query="ignored")
    assert build_search_url(search) == KA_PRETTY_URL
    assert build_search_url(search, page=2) == KA_PAGE2_URL
    pasted_page3 = SearchConfig(name="y", url="https://www.kleinanzeigen.de/s-seite:3/rtx-3090/k0")
    assert build_search_url(pasted_page3) == "https://www.kleinanzeigen.de/s-rtx-3090/k0"


@pytest.mark.parametrize(
    ("url", "page", "expected"),
    [
        ("https://www.kleinanzeigen.de/s-rtx-3090/k0", 2, "https://www.kleinanzeigen.de/s-seite:2/rtx-3090/k0"),
        (KA_PRETTY_URL, 2, KA_PAGE2_URL),
        (
            "https://www.kleinanzeigen.de/s-pc-zubehoer-software/c225",
            2,
            "https://www.kleinanzeigen.de/s-pc-zubehoer-software/seite:2/c225",
        ),
        (
            "https://www.kleinanzeigen.de/s-preis:100:500/rtx-3090/k0c225",
            4,
            "https://www.kleinanzeigen.de/s-preis:100:500/seite:4/rtx-3090/k0c225",
        ),
        (
            "https://www.kleinanzeigen.de/s-pc-zubehoer-software/rtx-3090/k0c225",
            2,
            "https://www.kleinanzeigen.de/s-pc-zubehoer-software/seite:2/rtx-3090/k0c225",
        ),
        # existing seite element is replaced / removed
        (KA_PAGE2_URL, 3, "https://www.kleinanzeigen.de/s-berlin/seite:3/rtx-3090/k0l3331r20"),
        (KA_PAGE2_URL, 1, KA_PRETTY_URL),
        ("https://www.kleinanzeigen.de/s-seite:5/rtx-3090/k0", 1, "https://www.kleinanzeigen.de/s-rtx-3090/k0"),
        # page 1 = unchanged
        (KA_PRETTY_URL, 1, KA_PRETTY_URL),
        # query strings are kept
        (
            "https://www.kleinanzeigen.de/s-rtx-3090/k0?shippingCarrier=dhl",
            2,
            "https://www.kleinanzeigen.de/s-seite:2/rtx-3090/k0?shippingCarrier=dhl",
        ),
        # unknown format: left alone
        ("https://www.kleinanzeigen.de/m-meine-anzeigen.html", 2, "https://www.kleinanzeigen.de/m-meine-anzeigen.html"),
    ],
)
def test_page_url(url: str, page: int, expected: str) -> None:
    assert page_url(url, page) == expected


def test_page_url_search_form_sets_page_num() -> None:
    url = build_search_url(SearchConfig(name="x", query="rtx 3090", location="Berlin"))
    url2 = page_url(url, 2)
    assert query_of(url2)["pageNum"] == ["2"]
    assert query_of(url2)["keywords"] == ["rtx 3090"]
    assert list(query_of(url2)) == list(query_of(url))  # parameter order preserved
    no_page = "https://www.kleinanzeigen.de/s-suchanfrage.html?keywords=foo"
    assert query_of(page_url(no_page, 3)) == {"keywords": ["foo"], "pageNum": ["3"]}


def test_large_image_url() -> None:
    assert large_image_url(f"{IMG}?rule=$_2.AUTO") == f"{IMG}?rule=$_59.JPG"
    assert large_image_url(f"{IMG}?rule=$_35.JPG") == f"{IMG}?rule=$_59.JPG"
    assert large_image_url(f"{IMG}?rule=%24_2.AUTO") == f"{IMG}?rule=$_59.JPG"
    assert large_image_url(f"{IMG}?foo=1&rule=$_57.AUTO") == f"{IMG}?foo=1&rule=$_59.JPG"
    assert large_image_url(IMG) == IMG
    assert large_image_url("https://example.com/pic.jpg") == "https://example.com/pic.jpg"


# ---------------------------------------------------------------------------
# Kleinanzeigen: search results page
# ---------------------------------------------------------------------------


def test_parse_search_results() -> None:
    listings = parse_search_results(fixture("ka_search_page1.html"), search_name="RTX 3090 Berlin")
    by_id = {item.ad_id: item for item in listings}
    # 7 regular ads; the "alternative" ad after the "Alternative Anzeigen" heading and the
    # banner <li> are ignored
    assert [item.ad_id for item in listings] == [f"289100000{i}" for i in range(1, 8)]
    assert all(item.search_name == "RTX 3090 Berlin" for item in listings)
    assert all(item.source == "kleinanzeigen" for item in listings)
    assert all(item.url.startswith(f"{BASE_URL}/s-anzeige/") for item in listings)
    assert all(not item.detail_loaded for item in listings)

    top = by_id["2891000001"]
    assert top.is_top_ad
    assert top.title == "MSI GeForce RTX 3090 Suprim X 24GB"
    assert (top.price, top.negotiable, top.is_free) == (699.0, True, False)
    assert top.price_text == "699 € VB"
    assert top.url == f"{BASE_URL}/s-anzeige/msi-geforce-rtx-3090-suprim-x-24gb/2891000001-225-3331"
    assert (top.location, top.postal_code, top.distance_km) == ("10115 Mitte", "10115", 3.0)
    assert top.posted_at_text == "Gestern, 18:40"
    assert top.tags == ["Versand möglich", "Direkt kaufen"]
    assert top.shipping_possible is True
    assert top.image_urls == [
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/3a/3a5e2c1d-7b8f-4c2a-9d3e-111111111111?rule=$_2.AUTO"
    ]
    assert top.description.startswith("Verkaufe meine MSI RTX 3090")
    assert sum(item.is_top_ad for item in listings) == 1

    pc = by_id["2891000002"]
    assert (pc.price, pc.negotiable) == (1250.0, True)
    assert pc.posted_at_text == "Heute, 14:05"
    assert pc.shipping_possible is False and pc.tags == ["Nur Abholung"]
    assert pc.image_urls == [  # lazy-loaded: data-imgsrc on the container, placeholder src ignored
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/b7/b71f00aa-1c2d-4e5f-8a9b-222222222222?rule=$_2.AUTO"
    ]

    free = by_id["2891000003"]
    assert (free.price, free.negotiable, free.is_free) == (0.0, False, True)
    assert free.image_urls == [] and free.shipping_possible is None

    vb = by_id["2891000004"]
    assert (vb.price, vb.negotiable, vb.is_free, vb.price_text) == (None, True, False, "VB")

    reduced = by_id["2891000005"]  # old price in a --old-price span
    assert (reduced.price, reduced.negotiable, reduced.price_text) == (649.0, True, "649 € VB")

    struck = by_id["2891000006"]  # old price in <s>
    assert (struck.price, struck.negotiable, struck.price_text) == (590.0, False, "590 €")
    assert struck.posted_at_text == "12.09.2025"

    assert "2891000008" not in by_id


def test_parse_search_results_empty_and_garbage() -> None:
    assert parse_search_results("") == []
    assert parse_search_results("<html><body><ul id='srchrslt-adtable'><li class='ad-listitem'></li></ul></body></html>") == []


def test_next_page_url() -> None:
    assert next_page_url(fixture("ka_search_page1.html")) == KA_PAGE2_URL
    assert next_page_url(fixture("ka_search_page2.html")) is None
    only_link = '<html><head><link rel="next" href="/s-seite:3/rtx-3090/k0"></head><body></body></html>'
    assert next_page_url(only_link) == "https://www.kleinanzeigen.de/s-seite:3/rtx-3090/k0"
    testid = '<nav><a data-testid="pagination-next" href="/s-seite:2/x/k0">Weiter</a></nav>'
    assert next_page_url(testid) == "https://www.kleinanzeigen.de/s-seite:2/x/k0"
    disabled = '<div class="pagination"><span class="pagination-next"></span></div>'
    assert next_page_url(disabled) is None


# ---------------------------------------------------------------------------
# Kleinanzeigen: ad detail page
# ---------------------------------------------------------------------------


def test_parse_ad_detail_merges_into_listing() -> None:
    card = next(i for i in parse_search_results(fixture("ka_search_page1.html"), "rtx") if i.ad_id == "2891000005")
    detail = parse_ad_detail(fixture("ka_ad_detail.html"), card, url=card.url)

    assert detail is not card and not card.detail_loaded  # original untouched
    assert detail.detail_loaded
    assert detail.title == "ASUS TUF RTX 3090 OC 24GB mit OVP"  # "Reserviert •" stripped
    assert "Reserviert" in detail.tags
    assert {"Versand möglich", "Direkt kaufen"} <= set(detail.tags)
    assert (detail.price, detail.negotiable, detail.price_text) == (649.0, True, "649 € VB")
    assert detail.description == (
        "Verkaufe meine ASUS TUF RTX 3090 OC.\n"
        "Karte lief nur zum Zocken, nie Mining.\n"
        "\n"
        "- OVP und Rechnung vorhanden\n"
        "- Keine Garantie, Privatverkauf\n"
        "\n"
        "Abholung in Friedrichshain oder Versand gegen Aufpreis."
    )
    assert detail.attributes == {
        "Art": "Grafikkarten", "Zustand": "Sehr Gut", "Versand": "Versand möglich",
        # seller trust signals from the contact box
        "Nutzertyp": "Privater Nutzer", "Aktiv seit": "03.04.2016", "Bewertung": "TOP Zufriedenheit, Sehr freundlich",
    }
    assert detail.condition == "Sehr Gut"
    assert detail.image_urls == [
        f"{IMG}?rule=$_59.AUTO",
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/7e/7e1d2c3b-4a59-4687-9a1b-5555aaaa0002?rule=$_59.AUTO",
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/8f/8f2e3d4c-5b6a-4798-8b2c-5555aaaa0003?rule=$_59.AUTO",
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/90/903f4e5d-6c7b-48a9-9c3d-5555aaaa0004?rule=$_59.AUTO",
    ]  # gallery order, thumbnails deduped
    assert (detail.seller_name, detail.seller_type) == ("Jonas", "private")
    assert detail.shipping_possible is True and detail.shipping_cost == 6.99
    assert detail.location == "10247 Berlin - Friedrichshain" and detail.postal_code == "10247"
    # search-only data is kept
    assert detail.search_name == "rtx"
    assert detail.distance_km == 5.0
    assert detail.posted_at_text == "Heute, 09:47"
    assert detail.ad_id == "2891000005" and detail.url == card.url
    assert detail.first_seen == card.first_seen


def test_parse_ad_detail_without_listing_commercial() -> None:
    detail = parse_ad_detail(fixture("ka_ad_detail_commercial.html"))
    assert detail.ad_id == "2890555123"
    assert detail.source == "kleinanzeigen"
    assert detail.url == (
        "https://www.kleinanzeigen.de/s-anzeige/rtx-3080-10gb-refurbished-12-monate-gewaehrleistung/2890555123-225-3378"
    )
    assert detail.title == "RTX 3080 10GB Refurbished - 12 Monate Gewährleistung"
    assert "Reserviert" not in detail.tags  # the hidden "Reserviert •" span is not a status
    assert (detail.price, detail.negotiable) == (529.0, False)
    assert (detail.seller_name, detail.seller_type) == ("PC-Outlet Berlin GmbH", "commercial")
    assert detail.shipping_possible is False and "Nur Abholung" in detail.tags
    assert {"Rechnung", "Gewährleistung"} <= set(detail.tags)
    assert detail.condition == "In Ordnung"
    assert detail.posted_at_text == "01.09.2025"
    assert detail.image_urls == [  # no gallery: og:image fallback
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/aa11bb22-cc33-4d44-8e55-f0f0f0f0f0f0?rule=$_59.JPG"
    ]
    assert detail.description.splitlines() == [
        "Geprüfte Grafikkarte aus unserem Ankauf.",
        "Mit Rechnung inkl. MwSt.",
        "Abholung im Laden, Mo-Fr 10-18 Uhr.",
    ]
    assert detail.detail_loaded


def test_parse_ad_detail_deleted_prefix_and_meta_price() -> None:
    html = """<html><body>
      <h1 id="viewad-title">Gelöscht • iPhone 13 Pro 128GB</h1>
      <h2 id="viewad-price"></h2><meta itemprop="price" content="450.00">
      <div id="viewad-ad-id-box"><ul><li>Anzeigen-ID</li><li>2877777777</li></ul></div>
    </body></html>"""
    detail = parse_ad_detail(html, url="https://www.kleinanzeigen.de/s-anzeige/iphone/2877777777-173-1")
    assert detail.title == "iPhone 13 Pro 128GB"
    assert detail.tags == ["Gelöscht"]
    assert detail.price == 450.0
    assert detail.ad_id == "2877777777"


def test_parse_ad_detail_non_ad_page() -> None:
    with pytest.raises(ValueError):
        parse_ad_detail("<html><body><p>Seite nicht gefunden</p></body></html>")
    listing = Listing(ad_id="1", url=f"{BASE_URL}/s-anzeige/x/1-2-3", title="X", price=10, description="snippet")
    merged = parse_ad_detail("<html><body></body></html>", listing)
    assert (merged.title, merged.price, merged.description) == ("X", 10, "snippet")
    assert merged.detail_loaded


# ---------------------------------------------------------------------------
# Kleinanzeigen: scraper with mocked HTTP
# ---------------------------------------------------------------------------


async def test_search_two_pages_follows_redirect_and_next_link() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if request.url.path == "/s-suchanfrage.html":
            q = query_of(url)
            assert q["keywords"] == ["rtx 3090"] and q["locationStr"] == ["Berlin"] and q["radius"] == ["20"]
            return httpx.Response(302, headers={"Location": "/s-berlin/rtx-3090/k0l3331r20"})
        if url == KA_PRETTY_URL:
            return html_response(fixture("ka_search_page1.html"))
        if url == KA_PAGE2_URL:
            return html_response(fixture("ka_search_page2.html"))
        return httpx.Response(404)

    client, sleeps = make_client(handler)
    async with client:
        scraper = KleinanzeigenScraper(client)
        search = SearchConfig(name="rtx", query="rtx 3090", location="Berlin", radius_km=20)
        listings = await scraper.search(search, max_pages=5)

    ids = [item.ad_id for item in listings]
    assert ids == [f"289100000{i}" for i in range(1, 8)] + ["2891000009", "2891000010"]
    assert len(set(ids)) == len(ids)
    assert all(item.search_name == "rtx" for item in listings)
    # form -> redirect -> page 1, then page 2; page 2 has no "next" link, so we stop
    assert [str(r.url) for r in requests[1:]] == [KA_PRETTY_URL, KA_PAGE2_URL]
    assert requests[-1].headers["Referer"].startswith("https://www.kleinanzeigen.de/")
    assert sleeps.calls == []


async def test_search_respects_max_pages() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return html_response(fixture("ka_search_page1.html"))

    client, _ = make_client(handler)
    async with client:
        listings = await KleinanzeigenScraper(client).search(SearchConfig(name="x", url=KA_PRETTY_URL), max_pages=1)
    assert calls == [KA_PRETTY_URL]
    assert len(listings) == 7


async def test_search_falls_back_to_page_url_and_stops_without_new_ads() -> None:
    no_pagination = fixture("ka_search_page1.html").split('<div class="srchresult-footer">')[0] + "</body></html>"
    no_pagination = no_pagination.replace('<link rel="next"', '<link rel="alternate"')
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return html_response(no_pagination)  # the site keeps serving the same ads

    client, _ = make_client(handler)
    async with client:
        listings = await KleinanzeigenScraper(client).search(SearchConfig(name="x", url=KA_PRETTY_URL), max_pages=5)
    assert calls == [KA_PRETTY_URL, KA_PAGE2_URL]  # page 2 built with page_url, then stop (no new ids)
    assert len(listings) == 7


async def test_fetch_detail() -> None:
    card = next(i for i in parse_search_results(fixture("ka_search_page1.html"), "rtx") if i.ad_id == "2891000005")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == card.url
        return html_response(fixture("ka_ad_detail.html"))

    client, _ = make_client(handler)
    async with client:
        detail = await KleinanzeigenScraper(client).fetch_detail(card)
    assert detail.detail_loaded and len(detail.image_urls) == 4 and detail.seller_type == "private"


async def test_comparables_filters_and_paginates() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/s-suchanfrage.html":
            q = query_of(request.url)
            assert q["keywords"] == ["rtx 3090"]
            assert q["sortingField"] == ["SORTING_RELEVANCE"]
            assert q["locationStr"] == [""] and q["radius"] == ["0"]  # nationwide
            assert q["minPrice"] == ["300"] and q["maxPrice"] == ["1000"]
            return html_response(fixture("ka_search_page1.html"))
        if str(request.url) == KA_PAGE2_URL:
            return html_response(fixture("ka_search_page2.html"))
        return httpx.Response(404)

    client, _ = make_client(handler)
    async with client:
        comps = await KleinanzeigenScraper(client).comparables(
            "rtx 3090", exclude_ad_id="2891000005", min_price=300, max_price=1000
        )
    # skipped: top ad (01), 1250 € > max (02), free (03), VB only (04), excluded (05), "Suche ..." (07),
    # "alternative" ad (08)
    assert [(c.title, c.price) for c in comps] == [
        ("Gigabyte RTX 3090 Gaming OC", 590.0),
        ("Palit RTX 3090 GameRock OC", 610.0),
        ("RTX 3090 Founders Edition", 575.0),
    ]
    assert all(c.source == "kleinanzeigen" and c.sold is False for c in comps)
    assert comps[0].url.endswith("/2891000006-225-3438") and comps[0].date_text == "12.09.2025"
    assert len(urls) == 2


async def test_comparables_limit_stops_early() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return html_response(fixture("ka_search_page1.html"))

    client, _ = make_client(handler)
    async with client:
        comps = await KleinanzeigenScraper(client).comparables("rtx 3090", limit=2)
    assert [c.price for c in comps] == [1250.0, 649.0]
    assert len(calls) == 1


async def test_download_images() -> None:
    requested: list[str] = []
    img2 = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/7e/7e1d2c3b"
    img3 = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/8f/8f2e3d4c"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        assert request.headers["Accept"].startswith("image/")
        if url.startswith(IMG):
            return httpx.Response(200, content=b"img1", headers={"Content-Type": "image/jpeg"})
        if url.startswith(img2):  # medium variant missing -> fallback to the original URL
            if "rule=$_57.JPG" in url or "rule=%24_57.JPG" in url:
                return httpx.Response(404)
            return httpx.Response(200, content=b"img2-small")
        return httpx.Response(500)  # third image broken: skipped silently

    listing = Listing(
        ad_id="2891000005", url=f"{BASE_URL}/s-anzeige/x/2891000005-225-3351", title="x",
        image_urls=[f"{IMG}?rule=$_2.AUTO", f"{img2}?rule=$_2.AUTO", f"{img3}?rule=$_2.AUTO", f"{IMG}-4?rule=$_2.AUTO"],
    )
    client, _ = make_client(handler, max_retries=0)
    async with client:
        scraper = KleinanzeigenScraper(client)
        images = await scraper.download_images(listing, max_images=3)
        assert images == [b"img1", b"img2-small"]
        assert "57.JPG" in requested[0]  # medium size for the AI, not the largest
        assert not any("-4?" in u for u in requested)  # only the first max_images photos

        requested.clear()
        assert await scraper.download_images(listing, max_images=1) == [b"img1"]
        assert len(requested) == 1


# ---------------------------------------------------------------------------
# eBay sold listings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("EUR 650,00", 650.0),
        ("650,00 €", 650.0),
        ("EUR 1.234,56", 1234.56),
        ("EUR\xa01.234,56", 1234.56),
        ("20,00 EUR", 20.0),
        ("EUR 20,00 bis EUR 30,00", None),
        ("EUR 20,00 - EUR 30,00", None),
        ("EUR 1.200", 1200.0),
        ("", None),
        ("Preis nicht verfügbar", None),
    ],
)
def test_parse_ebay_price(text: str, expected: float | None) -> None:
    assert parse_ebay_price(text) == expected


def test_build_sold_url() -> None:
    url = build_sold_url("rtx 3090")
    assert url.startswith("https://www.ebay.de/sch/i.html?_nkw=rtx+3090&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=60&LH_PrefLoc=1")
    assert "_pgn" not in url and "_udlo" not in url
    q = query_of(build_sold_url("rtx 3090", page=2, min_price=300, max_price=899.99))
    assert q["_pgn"] == ["2"] and q["_udlo"] == ["300"] and q["_udhi"] == ["899.99"]


def test_parse_sold_results_classic_layout() -> None:
    comps = parse_sold_results(fixture("ebay_sold_classic.html"))
    # placeholder "Shop on eBay", price range, struck-through best-offer price and
    # everything after the "fewer words" separator are skipped
    assert [(c.title, c.price) for c in comps] == [
        ("ASUS TUF Gaming GeForce RTX 3090 OC 24GB GDDR6X", 640.0),  # "Neues Angebot" stripped
        ("Gaming PC RTX 3090 Ryzen 9 5900X 64GB", 1234.56),
        ("Gigabyte GeForce RTX 3090 Vision OC 24G", 615.0),
    ]
    assert [c.url for c in comps] == [
        "https://www.ebay.de/itm/226512345678",
        "https://www.ebay.de/itm/186123456789",
        "https://www.ebay.de/itm/176512345672",
    ]
    assert [c.date_text for c in comps] == ["Verkauft 18. Sep 2025", "Verkauft 15. Sep 2025", "Verkauft 12. Sep 2025"]
    assert all(c.source == "ebay_sold" and c.sold for c in comps)


def test_parse_sold_results_card_layout() -> None:
    comps = parse_sold_results(fixture("ebay_sold_cards.html"))
    assert [(c.title, c.price, c.url, c.date_text) for c in comps] == [
        (
            "NVIDIA GeForce RTX 3090 Founders Edition 24GB",
            655.0,
            "https://www.ebay.de/itm/366012345678",
            "Verkauft 21. Sep 2025",
        ),
        (
            "EVGA GeForce RTX 3090 FTW3 Ultra Gaming 24GB",
            689.0,
            "https://www.ebay.de/itm/256612345679",
            "Verkauft 19. Sep 2025",
        ),
    ]
    assert all(c.source == "ebay_sold" and c.sold for c in comps)


def test_parse_sold_results_without_container() -> None:
    html = """<div><li class="s-item"><a class="s-item__link" href="https://www.ebay.de/itm/111111111111">
      <div class="s-item__title"><span>Neuer Artikel Steam Deck 512GB</span></div></a>
      <span class="s-item__price">EUR 380,00</span></li></div>"""
    comps = parse_sold_results(html)
    assert [(c.title, c.price) for c in comps] == [("Steam Deck 512GB", 380.0)]
    assert parse_sold_results("") == []


async def test_ebay_sold_comparables() -> None:
    urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(request.url)
        q = query_of(request.url)
        assert request.url.host == "www.ebay.de" and request.url.path == "/sch/i.html"
        assert q["_nkw"] == ["rtx 3090"] and q["LH_Sold"] == ["1"] and q["LH_Complete"] == ["1"]
        return html_response(fixture("ebay_sold_classic.html"))

    client, _ = make_client(handler)
    async with client:
        comps = await EbaySoldScraper(client).sold_comparables("rtx 3090", limit=2)
    assert [c.price for c in comps] == [640.0, 1234.56]
    assert len(urls) == 1


async def test_ebay_sold_comparables_second_page() -> None:
    page1 = fixture("ebay_sold_classic.html").replace(
        '<a class="pagination__next icon-link" aria-disabled="true" href="#">',
        '<a class="pagination__next icon-link" href="https://www.ebay.de/sch/i.html?_nkw=rtx+3090&amp;_pgn=2">',
    )
    pages: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pgn = query_of(request.url).get("_pgn", ["1"])
        pages.append(pgn)
        return html_response(page1 if pgn == ["1"] else fixture("ebay_sold_cards.html"))

    client, _ = make_client(handler)
    async with client:
        comps = await EbaySoldScraper(client).sold_comparables("rtx 3090", limit=100)
    assert pages == [["1"], ["2"]]
    assert [c.price for c in comps] == [640.0, 1234.56, 615.0, 655.0, 689.0]


# ---------------------------------------------------------------------------
# PoliteClient
# ---------------------------------------------------------------------------

NORMAL_PAGE = "<html><head><title>Ergebnisse</title></head><body><ul id='srchrslt-adtable'></ul></body></html>"


async def test_client_retries_on_429_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return html_response(NORMAL_PAGE)

    client, sleeps = make_client(handler)
    async with client:
        assert await client.get_text("https://www.kleinanzeigen.de/s-rtx/k0") == NORMAL_PAGE
    assert calls == 2
    assert len(sleeps.calls) == 1 and sleeps.calls[0] >= 7  # honours Retry-After


async def test_client_retries_5xx_and_transport_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection reset", request=request)
        if calls == 2:
            return httpx.Response(503)
        return html_response(NORMAL_PAGE)

    client, sleeps = make_client(handler)
    async with client:
        assert await client.get_text("https://www.kleinanzeigen.de/") == NORMAL_PAGE
    assert calls == 3
    assert len(sleeps.calls) == 2 and sleeps.calls[1] > sleeps.calls[0] - 1  # exponential backoff


async def test_client_gives_up_after_max_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500 if "fail" in request.url.path else 429)

    client, _ = make_client(handler, max_retries=2)
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_text("https://www.kleinanzeigen.de/fail")
        assert calls == 3
        with pytest.raises(BlockedError):  # persistent rate limiting = blocked
            await client.get_text("https://www.kleinanzeigen.de/limited")
        assert calls == 6


async def test_client_raises_blocked_on_403_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="<html><title>Access Denied</title></html>")

    client, _ = make_client(handler)
    async with client:
        with pytest.raises(BlockedError) as info:
            await client.get_text("https://www.kleinanzeigen.de/s-rtx/k0")
    assert calls == 1 and info.value.status_code == 403


DATADOME_PAGE = """<html><head><title>kleinanzeigen.de</title></head><body style="margin:0">
<p id="cmsg">Please enable JS and disable any ad blocker</p>
<script data-cfasync="false">var dd={'rt':'c','cid':'AHrlqAAAAAMA','hsh':'ABC','t':'fe','host':'geo.captcha-delivery.com'}</script>
<script data-cfasync="false" src="https://ct.captcha-delivery.com/c.js"></script></body></html>"""
CLOUDFLARE_PAGE = """<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>
<body><div class="main-wrapper"><noscript>Enable JavaScript and cookies to continue</noscript></div>
<script>(function(){window._cf_chl_opt={cvId: '3',cZone: 'www.ebay.de'};}());</script></body></html>"""
AKAMAI_PAGE = """<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>
You don't have permission to access "http://www.kleinanzeigen.de/s-rtx/k0" on this server.<P>
Reference&#32;&#35;18&#46;5f3c1702&#46;1727430000&#46;2a4b1c</BODY></HTML>"""
GERMAN_BLOCK_PAGE = """<html><head><title>Kleinanzeigen</title></head><body>
<h1>Zugriff verweigert</h1><p>Wir haben ungewöhnliche Aktivitäten festgestellt. Bitte bestätige, dass du kein Roboter bist.</p>
</body></html>"""


@pytest.mark.parametrize("page", [DATADOME_PAGE, CLOUDFLARE_PAGE, AKAMAI_PAGE, GERMAN_BLOCK_PAGE])
async def test_client_raises_blocked_on_captcha_page(page: str) -> None:
    assert looks_blocked(page)
    client, _ = make_client(lambda request: html_response(page))
    async with client:
        with pytest.raises(BlockedError):
            await client.get_text("https://www.kleinanzeigen.de/s-rtx/k0")


async def test_client_raises_blocked_on_captcha_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sch/i.html":
            return httpx.Response(302, headers={"Location": "https://www.ebay.de/splashui/captcha?ap=1&appName=orch"})
        return html_response("<html><body><div id='captcha'>Bitte bestätigen</div></body></html>")

    client, _ = make_client(handler)
    async with client:
        with pytest.raises(BlockedError):
            await client.get_text(build_sold_url("rtx 3090"))


@pytest.mark.parametrize(
    "name",
    [
        "ka_search_page1.html",
        "ka_search_page2.html",
        "ka_ad_detail.html",
        "ka_ad_detail_commercial.html",
        "ebay_sold_classic.html",
        "ebay_sold_cards.html",
    ],
)
def test_looks_blocked_no_false_positive_on_real_pages(name: str) -> None:
    assert not looks_blocked(fixture(name))  # the ad page even embeds a reCAPTCHA contact form


def test_looks_blocked_edge_cases() -> None:
    assert not looks_blocked("")
    assert not looks_blocked("<html><head><title>Ok</title></head><body>Hallo</body></html>")
    assert looks_blocked("<html><head><title>Pardon Our Interruption...</title></head><body>x</body></html>")


async def test_zero_delay_never_sleeps() -> None:
    client, sleeps = make_client(lambda request: html_response(NORMAL_PAGE), delay_range=(0.0, 0.0))
    started = time.monotonic()
    async with client:
        for _ in range(3):
            await client.get_text("https://www.kleinanzeigen.de/")
        await client.get_bytes("https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/bb?rule=$_59.JPG")
    assert sleeps.calls == []
    assert time.monotonic() - started < 1.0


async def test_delay_between_requests_to_same_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("img."):
            return httpx.Response(200, content=b"\xff\xd8")
        return html_response(NORMAL_PAGE)

    client, sleeps = make_client(handler, delay_range=(2.0, 2.0))
    async with client:
        await client.get_text("https://www.kleinanzeigen.de/a")  # first request: no wait
        assert sleeps.calls == []
        await client.get_text("https://www.ebay.de/b")  # other host: no wait
        assert sleeps.calls == []
        await client.get_text("https://www.kleinanzeigen.de/c")
        assert len(sleeps.calls) == 1 and 1.5 < sleeps.calls[0] <= 2.0
        await client.get_bytes("https://img.kleinanzeigen.de/1")
        await client.get_bytes("https://img.kleinanzeigen.de/2")  # image CDN: short pause only
        assert len(sleeps.calls) == 2 and 0 < sleeps.calls[1] <= 0.3


async def test_requests_to_one_host_are_serialized() -> None:
    in_flight: dict[str, int] = {}
    peak: dict[str, int] = {}
    overall = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        in_flight[host] = in_flight.get(host, 0) + 1
        overall["now"] += 1
        peak[host] = max(peak.get(host, 0), in_flight[host])
        overall["peak"] = max(overall["peak"], overall["now"])
        await asyncio.sleep(0.02)
        in_flight[host] -= 1
        overall["now"] -= 1
        return html_response(NORMAL_PAGE)

    client, _ = make_client(handler)
    async with client:
        await asyncio.gather(
            *(client.get_text(f"https://www.kleinanzeigen.de/{i}") for i in range(3)),
            *(client.get_text(f"https://www.ebay.de/{i}") for i in range(3)),
        )
    assert peak == {"www.kleinanzeigen.de": 1, "www.ebay.de": 1}
    assert overall["peak"] == 2  # different hosts still run in parallel


async def test_client_headers_referer_and_cookies() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cookie = {"Set-Cookie": "session=abc123; Path=/"} if len(seen) == 1 else None
        return html_response(NORMAL_PAGE, headers=cookie)

    client, _ = make_client(handler)
    async with client:
        await client.get_text("https://www.kleinanzeigen.de/")
        await client.get_text("https://www.kleinanzeigen.de/s-rtx/k0", referer="https://www.kleinanzeigen.de/")
    first, second = seen
    assert "Chrome/" in first.headers["User-Agent"] and "Windows NT 10.0" in first.headers["User-Agent"]
    assert first.headers["Accept-Language"] == "de-DE,de;q=0.9,en;q=0.8"
    assert first.headers["Accept"].startswith("text/html")
    assert first.headers["Sec-Fetch-Site"] == "none"
    assert "Referer" not in first.headers
    assert second.headers["Referer"] == "https://www.kleinanzeigen.de/"
    assert second.headers["Sec-Fetch-Site"] == "same-origin"
    assert "session=abc123" in second.headers.get("Cookie", "")


async def test_client_from_config() -> None:
    general = GeneralConfig(request_delay_seconds=2, request_timeout_seconds=10, user_agent="MyAgent/1.0")
    client = PoliteClient.from_config(general)
    async with client:
        assert client.delay_range == (2.0, 2.0)
        assert client.user_agent == "MyAgent/1.0"
        assert "sec-ch-ua" not in client._headers("https://www.kleinanzeigen.de/", None, "document")
    default = PoliteClient.from_config(GeneralConfig())
    assert default.delay_range == (3.0, 7.0)
    await default.aclose()
