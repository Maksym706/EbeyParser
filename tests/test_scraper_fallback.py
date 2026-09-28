"""Redesigned / unexpected Kleinanzeigen markup: fallback parsing and diagnostics."""

from __future__ import annotations

import httpx
import pytest

from ebeyparser.config import SearchConfig
from ebeyparser.scraper.http import PoliteClient
from ebeyparser.scraper.kleinanzeigen import (
    KleinanzeigenScraper,
    PageLayoutError,
    page_diagnostics,
    parse_search_results,
)

REDESIGNED = """<html><head><title>Rtx 3080 in Berlin | kleinanzeigen.de</title></head><body>
<div class="results-grid">
  <div class="card-x">
    <a href="/s-anzeige/rtx-3080-gaming/2911112222-225-3331"><img src="https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/bb?rule=$_2.AUTO" alt="RTX 3080"></a>
    <div class="card-x__body">
      <a href="/s-anzeige/rtx-3080-gaming/2911112222-225-3331">MSI RTX 3080 Gaming X Trio 10GB</a>
      <span class="p">450 € VB</span><span class="loc">10115 Mitte (3 km)</span>
    </div>
  </div>
  <div class="card-x">
    <a href="/s-anzeige/monitor/2933334444-225-3331">Alter Monitor zu verschenken</a>
    <span>Zu verschenken</span><span>12043 Neukölln</span>
  </div>
</div></body></html>"""

EMPTY = "<html><body><h1>Es wurden leider keine Anzeigen für «xyz» gefunden.</h1></body></html>"
CONSENT_ONLY = "<html><head><title>kleinanzeigen.de</title></head><body><div id='gdpr-banner'>Cookies</div></body></html>"


def test_fallback_parses_links_when_card_markup_changed():
    ads = parse_search_results(REDESIGNED, "gpu")
    assert [a.ad_id for a in ads] == ["2911112222", "2933334444"]
    gpu, monitor = ads
    assert gpu.title == "MSI RTX 3080 Gaming X Trio 10GB"
    assert gpu.price == 450.0 and gpu.negotiable
    assert gpu.postal_code == "10115"
    assert gpu.image_urls and gpu.image_urls[0].startswith("https://img.kleinanzeigen.de/")
    assert gpu.url == "https://www.kleinanzeigen.de/s-anzeige/rtx-3080-gaming/2911112222-225-3331"
    assert monitor.is_free and monitor.price == 0.0
    assert all(a.search_name == "gpu" and a.source == "kleinanzeigen" for a in ads)


def test_page_diagnostics():
    info = page_diagnostics(REDESIGNED)
    assert info["title"].startswith("Rtx 3080")
    assert info["article.aditem"] == 0 and info["links /s-anzeige/"] == 3 and info["parsed_ads"] == 2
    assert "card-x" in info["link_parent_classes"]
    assert page_diagnostics(CONSENT_ONLY)["consent_markers"] == ["gdpr-banner"]
    assert page_diagnostics(EMPTY)["empty_results_text"] is True


def _scraper(html: str, tmp_path) -> KleinanzeigenScraper:
    client = PoliteClient(delay_range=(0, 0), transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    return KleinanzeigenScraper(client, debug_dir=tmp_path / "debug")


async def test_unrecognised_page_raises_and_saves_html(tmp_path):
    scraper = _scraper(CONSENT_ONLY, tmp_path)
    with pytest.raises(PageLayoutError, match="не распознаны") as err:
        await scraper.search(SearchConfig(name="Видеокарты рядом", query="rtx 3080"))
    saved = list((tmp_path / "debug").glob("*.html"))
    assert len(saved) == 1 and str(saved[0]) in str(err.value)
    assert saved[0].read_text(encoding="utf-8") == CONSENT_ONLY
    await scraper.client.aclose()


async def test_genuinely_empty_search_is_not_an_error(tmp_path):
    scraper = _scraper(EMPTY, tmp_path)
    assert await scraper.search(SearchConfig(name="x", query="xyz")) == []
    assert not (tmp_path / "debug").exists()
    await scraper.client.aclose()


def test_debug_search_command(tmp_path, monkeypatch, capsys):
    from ebeyparser import cli
    from ebeyparser.scraper import http

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"general:\n  data_dir: {tmp_path / 'data'}\nsearches:\n  - name: gpu\n    query: rtx 3080\n",
                   encoding="utf-8")
    monkeypatch.setattr(http.PoliteClient, "from_config", classmethod(
        lambda cls, general, **_kw: cls(delay_range=(0, 0),
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, text=REDESIGNED)))))
    assert cli.main(["-c", str(cfg), "debug-search"]) == 0
    out = capsys.readouterr().out
    assert "parsed_ads:" in out and "2911112222 | MSI RTX 3080" in out and "HTML сохранён" in out


# Closer to the 2026 Kleinanzeigen layout: utility CSS classes, data-adid on an inner
# element, location and time in separate elements, struck-through old price.
TAILWIND = """<html><head><title>Rtx 3080 in Berlin | kleinanzeigen.de</title></head><body>
<p>15 Ergebnisse</p>
<ul class="flex flex-col">
 <li class="relative">
  <div class="flex justify-between p-medium">
   <a class="basis-[200px]" href="/s-anzeige/gigabyte-rtx-3080/3525778616-225-3331">
     <img src="https://img.kleinanzeigen.de/api/v1/prod-ads/images/12/abc?rule=$_2.AUTO" alt="Gigabyte"></a>
   <div class="flex-col">
     <div class="mb-xsmall"><span>10178 Mitte</span><span>Heute, 16:14</span></div>
     <h2><a href="/s-anzeige/gigabyte-rtx-3080/3525778616-225-3331">Gigabyte GeForce RTX 3080 Gaming OC 10G</a></h2>
     <p>Verkaufe meine Grafikkarte, läuft einwandfrei, nie übertaktet, mit OVP und Rechnung.</p>
     <p><span class="line-through">399 €</span> <span>349 € VB</span></p>
     <span>Versand möglich</span>
     <button data-adid="3525778616" aria-label="Merken">♡</button>
   </div>
  </div>
 </li>
 <li class="relative">
  <div class="flex justify-between p-medium">
   <div class="flex-col">
     <div><span>16515 Oranienburg</span><span>Heute, 00:53</span></div>
     <h2><a href="/s-anzeige/asus-tuf-3080/3525304253-225-3354">ASUS TUF Gaming GeForce RTX 3080 12GB</a></h2>
     <p>450 € VB</p>
     <button data-adid="3525304253">♡</button>
   </div>
  </div>
 </li>
</ul></body></html>"""

ALTERNATIVES = """<html><body><h1>Leider keine Ergebnisse für «rtx 3090» in Berlin</h1>
<h2>Ähnliche Anzeigen</h2><ul>
 <li><a href="/s-anzeige/eisblock/3511251577-225-1">Alphacool Eisblock RTX 3090 waterblock</a> 90 € VB 24211 Wahlstorf</li>
</ul></body></html>"""


def test_tailwind_layout_cards():
    ads = parse_search_results(TAILWIND, "gpu")
    assert [a.ad_id for a in ads] == ["3525778616", "3525304253"]
    gpu = ads[0]
    assert gpu.title == "Gigabyte GeForce RTX 3080 Gaming OC 10G"
    assert gpu.price == 349.0 and gpu.negotiable  # not the struck-through 399
    assert gpu.location == "10178 Mitte" and gpu.postal_code == "10178"
    assert gpu.posted_at_text == "Heute, 16:14"
    assert gpu.description.startswith("Verkaufe meine Grafikkarte")
    assert gpu.shipping_possible is True and "Versand möglich" in gpu.tags
    assert gpu.image_urls and "/prod-ads/images/" in gpu.image_urls[0]
    assert ads[1].location == "16515 Oranienburg" and ads[1].price == 450.0


async def test_no_results_page_ignores_similar_ads(tmp_path):
    from ebeyparser.scraper.kleinanzeigen import is_empty_results_page

    assert is_empty_results_page(ALTERNATIVES)
    assert not is_empty_results_page(TAILWIND)  # "15 Ergebnisse" is not "0 Ergebnisse"
    assert not is_empty_results_page("<p>10 Ergebnisse</p>")
    assert is_empty_results_page("<p>0 Ergebnisse</p>")
    scraper = _scraper(ALTERNATIVES, tmp_path)
    assert await scraper.search(SearchConfig(name="3090", query="rtx 3090", location="Berlin")) == []
    await scraper.client.aclose()


def test_detail_page_json_ld_and_meta_fallbacks():
    from ebeyparser.scraper.kleinanzeigen import parse_ad_detail

    html = """<html><head>
    <meta property="og:title" content="Gigabyte RTX 3080 | kleinanzeigen.de">
    <meta property="og:description" content="Kurz...">
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
      "name":"Gigabyte GeForce RTX 3080 Gaming OC","description":"Läuft einwandfrei.\\nMit OVP.",
      "offers":{"@type":"Offer","price":"349","priceCurrency":"EUR"}}</script>
    </head><body><div class="gallery">
      <img src="https://img.kleinanzeigen.de/api/v1/prod-ads/images/12/abc?rule=$_59.AUTO">
      <img src="https://img.kleinanzeigen.de/api/v1/prod-ads/images/34/def?rule=$_59.AUTO">
      <img src="https://static.kleinanzeigen.de/logo.png">
    </div></body></html>"""
    ad = parse_ad_detail(html, url="https://www.kleinanzeigen.de/s-anzeige/x/3525778616-225-3331")
    assert ad.ad_id == "3525778616" and ad.title == "Gigabyte GeForce RTX 3080 Gaming OC"
    assert ad.price == 349.0 and ad.description == "Läuft einwandfrei.\nMit OVP."
    assert len(ad.image_urls) == 2 and ad.detail_loaded
