"""Offline demo data: realistic Kleinanzeigen deals with evaluations.

Used to try the dashboard without scraping (`seed_demo`) and as sample
payload for test notifications (`demo_deals`). Images are generated SVG
data URIs, so everything works without internet access.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

from .config import PricingConfig
from .db import Database
from .models import (
    AIVerdict,
    Comparable,
    Evaluation,
    Listing,
    PriceEstimate,
    RunSummary,
    utcnow,
)

DEMO_AI_MODEL = "qwen2.5vl:7b"
SECOND_OPINION_MODEL = "claude-opus-5"

# (gradient start, gradient end) per demo item, picked to look distinct in a grid
_PALETTES: list[tuple[str, str]] = [
    ("#0f766e", "#164e63"),
    ("#4338ca", "#1e1b4b"),
    ("#475569", "#0f172a"),
    ("#b91c1c", "#450a0a"),
    ("#1d4ed8", "#0c4a6e"),
    ("#7c3aed", "#312e81"),
    ("#15803d", "#14532d"),
    ("#0369a1", "#082f49"),
    ("#a21caf", "#4a044e"),
    ("#c2410c", "#431407"),
    ("#b45309", "#422006"),
    ("#334155", "#020617"),
]


def _svg_image(emoji: str, label: str, sub: str, colors: tuple[str, str], index: int) -> str:
    """A 4:3 product "photo" as an SVG data URI (gradient, emoji, name)."""
    c1, c2 = colors
    # vary the composition a little between gallery images
    angle = (index * 55) % 360
    cx, cy, size = [(400, 215, 170), (300, 210, 140), (520, 210, 150)][index % 3]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600" width="800" height="600">
<defs>
<linearGradient id="g" gradientTransform="rotate({angle} .5 .5)"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>
<radialGradient id="l" cx=".3" cy=".25" r=".8"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
</defs>
<rect width="800" height="600" fill="url(#g)"/>
<rect width="800" height="600" fill="url(#l)"/>
<circle cx="{680 - index * 90}" cy="{110 + index * 40}" r="150" fill="#fff" fill-opacity=".05"/>
<circle cx="{120 + index * 60}" cy="520" r="210" fill="#000" fill-opacity=".12"/>
<ellipse cx="{cx}" cy="{cy + size * 0.62:.0f}" rx="{size * 0.75:.0f}" ry="{size * 0.12:.0f}" fill="#000" fill-opacity=".25"/>
<text x="{cx}" y="{cy}" font-size="{size}" text-anchor="middle" dominant-baseline="central" font-family="Noto Color Emoji, Apple Color Emoji, Segoe UI Emoji, sans-serif">{emoji}</text>
<text x="40" y="440" font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif" font-size="44" font-weight="700" fill="#fff">{escape(label)}</text>
<text x="40" y="480" font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif" font-size="26" fill="#fff" fill-opacity=".75">{escape(sub)}</text>
</svg>"""
    return "data:image/svg+xml;charset=utf-8," + quote(svg, safe="")


def _images(emoji: str, label: str, subs: list[str], palette: int) -> list[str]:
    colors = _PALETTES[palette % len(_PALETTES)]
    return [_svg_image(emoji, label, sub, colors, i) for i, sub in enumerate(subs)]


def _ka_url(slug: str, ad_id: str, cat: int) -> str:
    return f"https://www.kleinanzeigen.de/s-anzeige/{slug}/{ad_id}-{cat}-3331"


def _comps(rows: list[tuple[str, float, str, str]]) -> list[Comparable]:
    """rows: (title, price, source, date_text)."""
    out: list[Comparable] = []
    for i, (title, price, source, date_text) in enumerate(rows):
        if source == "ebay_sold":
            url = f"https://www.ebay.de/itm/{3867201145 + i * 7919}"
        elif source == "kleinanzeigen":
            url = f"https://www.kleinanzeigen.de/s-anzeige/{2893001200 + i * 3571}"
        else:
            url = ""
        out.append(
            Comparable(
                title=title,
                price=price,
                url=url,
                source=source,  # type: ignore[arg-type]
                sold=source == "ebay_sold",
                date_text=date_text,
            )
        )
    return out


def _estimate(comps: list[Comparable], market: float, query: str, source: str = "mixed", notes: str = "") -> PriceEstimate:
    prices = [c.price for c in comps] or [market]
    return PriceEstimate(
        market_price=market,
        low=min(prices),
        high=max(prices),
        sample_size=len(comps),
        source=source,  # type: ignore[arg-type]
        query=query,
        comparables=comps,
        notes=notes,
    )


def _money_eval(
    ad_id: str,
    purpose: str,
    buy_price: float,
    estimate: PriceEstimate,
    pricing: PricingConfig,
    *,
    buyer_shipping: float = 0.0,
    max_buy: float | None = None,
) -> dict[str, Any]:
    """Profit numbers the same way the dashboard explains them (default pricing).
    `buy_price` is the item price; `buyer_shipping` (eBay) is added to it."""
    market = estimate.market_price or 0.0
    total_buy = buy_price + buyer_shipping
    fees = 0.0
    shipping = 0.0
    if purpose == "personal":
        profit = market - total_buy  # savings vs buying at market price
    else:
        net = market * (1 - pricing.safety_margin_percent / 100)
        fees = round(net * (pricing.selling_fee_percent + pricing.payment_fee_percent) / 100, 2)
        shipping = pricing.default_shipping_cost
        profit = net - fees - shipping - total_buy
        if max_buy is None:
            # highest total price that still meets both min_profit and min_roi
            ceiling = min(net - fees - shipping - pricing.min_profit,
                          (net - fees - shipping) / (1 + pricing.min_roi))
            max_buy = float(int(ceiling - buyer_shipping)) if ceiling - buyer_shipping > 0 else None
    roi = profit / total_buy if total_buy > 0 else None
    return {
        "ad_id": ad_id,
        "purpose": purpose,
        "buy_price": round(total_buy, 2),
        "estimate": estimate,
        "fees": fees,
        "shipping_cost": shipping,
        "expected_profit": round(profit, 2),
        "roi": round(roi, 4) if roi is not None else None,
        "max_buy_price": max_buy,
    }


def demo_deals() -> list[tuple[Listing, Evaluation]]:
    """~12 realistic listings around Berlin with evaluations. Times are relative to now."""
    now = utcnow()
    pricing = PricingConfig()
    deals: list[tuple[Listing, Evaluation]] = []

    def add(listing: Listing, ev_kwargs: dict[str, Any]) -> None:
        deals.append((listing, Evaluation(**ev_kwargs)))

    def ago(minutes: float) -> datetime:
        return now - timedelta(minutes=minutes)

    # 1. Free monitor — "Zu verschenken"
    ad = "2894410057"
    comps = _comps([
        ("Dell UltraSharp U2415 24 Zoll IPS Monitor", 75, "ebay_sold", "Verkauft 21.09.2026"),
        ("Dell U2415 Monitor 1920x1200 mit Standfuß", 69, "ebay_sold", "Verkauft 17.09.2026"),
        ("Dell UltraSharp U2415 24\" gebraucht", 80, "kleinanzeigen", "Gestern"),
        ("Dell U2415b Monitor ohne Kabel", 60, "kleinanzeigen", "20.09.2026"),
    ])
    est = _estimate(comps, 70, "Dell U2415")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("zu-verschenken-dell-ultrasharp-u2415-monitor", ad, 225),
        title="Zu verschenken: Dell UltraSharp U2415 Monitor 24 Zoll",
        price=0,
        price_text="Zu verschenken",
        is_free=True,
        location="10115 Mitte",
        postal_code="10115",
        distance_km=2.1,
        posted_at_text="Heute, 19:48",
        description=(
            "Verschenke meinen alten Monitor, da ich auf 4K umgestiegen bin.\n"
            "Funktioniert einwandfrei, keine Pixelfehler.\n\n"
            "Stromkabel und DisplayPort-Kabel sind dabei.\n"
            "Nur Abholung in Mitte, bitte bis Sonntag."
        ),
        image_urls=_images("🖥️", "Dell U2415", ["24\" IPS, 1920×1200", "Anschlüsse", "Standfuß"], 11),
        shipping_possible=False,
        tags=["Nur Abholung"],
        attributes={"Art": "Monitore", "Zustand": "Gut"},
        seller_name="Jana",
        seller_type="private",
        search_name="Бесплатно рядом",
        detail_loaded=True,
        first_seen=ago(12),
    )
    add(listing, {
        **_money_eval(ad, "resale", 0, est, pricing),
        "ai": AIVerdict(
            product="Dell UltraSharp U2415 24\"",
            search_query="Dell U2415",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=70,
            verdict="buy",
            confidence=0.88,
            reasoning="Бесплатный монитор в рабочем состоянии: на фото тот же Dell U2415, кабели на месте. "
            "Продаётся за 60–80 €. Забрать нужно самому в Митте — окупится даже с дорогой.",
            model=DEMO_AI_MODEL,
        ),
        "score": 90,
        "verdict": "buy",
        "reasons": [
            "Отдают бесплатно — любая продажа = чистая прибыль",
            "4 похожих предложения: 60–80 €",
            "Фото совпадают с описанием, кабели в комплекте",
            "Всего 2 км от центра",
        ],
    })

    # 2. RTX 3090 — the big resale win
    ad = "2894398213"
    comps = _comps([
        ("Gigabyte GeForce RTX 3090 Gaming OC 24GB", 790, "ebay_sold", "Verkauft 24.09.2026"),
        ("NVIDIA RTX 3090 Founders Edition 24GB", 820, "ebay_sold", "Verkauft 22.09.2026"),
        ("ASUS TUF RTX 3090 24GB OC", 760, "ebay_sold", "Verkauft 20.09.2026"),
        ("MSI RTX 3090 Ventus 3X 24G", 745, "ebay_sold", "Verkauft 19.09.2026"),
        ("Zotac RTX 3090 Trinity 24GB", 720, "ebay_sold", "Verkauft 15.09.2026"),
        ("RTX 3090 Gigabyte Vision OC", 880, "kleinanzeigen", "Heute"),
        ("Gigabyte RTX 3090 Eagle 24GB VB", 850, "kleinanzeigen", "Gestern"),
        ("EVGA RTX 3090 FTW3 Ultra", 899, "kleinanzeigen", "23.09.2026"),
    ])
    est = _estimate(comps, 780, "RTX 3090 24GB", notes="Цены продаж eBay + объявления со скидкой 15 %")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("gigabyte-rtx-3090-gaming-oc-24gb", ad, 225),
        title="Gigabyte GeForce RTX 3090 Gaming OC 24GB – OVP + Rechnung",
        price=450,
        price_text="450 € VB",
        negotiable=True,
        location="10247 Friedrichshain",
        postal_code="10247",
        distance_km=4.8,
        posted_at_text="Heute, 19:35",
        description=(
            "Verkaufe meine Gigabyte RTX 3090 Gaming OC mit 24 GB.\n"
            "Die Karte lief nur zum Zocken, kein Mining. Temperaturen top, nie übertaktet.\n\n"
            "- Originalverpackung vorhanden\n"
            "- Rechnung von Mindfactory (03/2022)\n"
            "- Stützhalterung dabei\n\n"
            "Grund: Umstieg auf Laptop. Abholung in Friedrichshain, gerne mit Test vor Ort.\n"
            "Keine Tauschangebote!"
        ),
        image_urls=_images("🎮", "RTX 3090 24GB", ["Gigabyte Gaming OC", "OVP + Rechnung", "Backplate"], 0),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "Grafikkarten", "Zustand": "Gut", "Versand": "Versand möglich"},
        seller_name="Marco",
        seller_type="private",
        search_name="Видеокарты Берлин",
        detail_loaded=True,
        first_seen=ago(25),
    )
    add(listing, {
        **_money_eval(ad, "resale", 450, est, pricing),
        "ai": AIVerdict(
            product="NVIDIA GeForce RTX 3090 24GB (Gigabyte Gaming OC)",
            search_query="RTX 3090 24GB",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=760,
            verdict="buy",
            confidence=0.84,
            reasoning="На фото именно Gigabyte Gaming OC (три вентилятора, RGB-логотип), видны коробка и чек. "
            "Цена почти на 40 % ниже рынка — 24 ГБ VRAM сейчас в спросе из-за локальных нейросетей. "
            "При встрече проверь температуру памяти в FurMark/OCCT 10 минут.",
            model=DEMO_AI_MODEL,
        ),
        "ai_second": AIVerdict(
            product="Gigabyte GeForce RTX 3090 Gaming OC 24G (GV-N3090GAMING OC-24GD)",
            search_query="RTX 3090 24GB",
            photo_matches_description=True,
            condition="good",
            red_flags=["Проверь гарантию по серийному номеру на сайте Gigabyte"],
            estimated_market_price=770,
            verdict="buy",
            confidence=0.87,
            reasoning="Согласна с локальной моделью: ревизия и коробка совпадают, чек из Mindfactory даёт гарантию до 03/2025 "
            "(уже истекла — учти). 24 ГБ VRAM держат цену. Предложи 420 € и проверь карту под нагрузкой перед оплатой.",
            model=SECOND_OPINION_MODEL,
        ),
        "score": 94,
        "verdict": "buy",
        "reasons": [
            "Цена 450 € на ~42 % ниже рынка (~780 €)",
            "5 продаж на eBay за 720–820 € за последние 2 недели",
            "Фото совпадают с описанием, есть OVP и чек (гарантия)",
            "Торг уместен (VB) — можно предложить 420 €",
        ],
    })

    # 3. Mac mini M1
    ad = "2894311780"
    comps = _comps([
        ("Apple Mac mini M1 8GB 256GB", 430, "ebay_sold", "Verkauft 23.09.2026"),
        ("Mac mini M1 2020 8/256 wie neu", 455, "ebay_sold", "Verkauft 18.09.2026"),
        ("Apple Mac mini M1 8GB RAM 512GB", 480, "ebay_sold", "Verkauft 12.09.2026"),
        ("Mac Mini M1 256GB OVP", 470, "kleinanzeigen", "Heute"),
        ("Apple Mac mini (M1, 2020)", 440, "kleinanzeigen", "22.09.2026"),
    ])
    est = _estimate(comps, 420, "Mac mini M1 8GB 256GB")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("apple-mac-mini-m1-8gb-256gb", ad, 228),
        title="Apple Mac mini M1 (2020) 8GB / 256GB SSD",
        price=280,
        price_text="280 €",
        location="10437 Prenzlauer Berg",
        postal_code="10437",
        distance_km=3.9,
        posted_at_text="Heute, 18:22",
        description=(
            "Mac mini M1 aus 2020, 8 GB RAM, 256 GB SSD.\n"
            "Zurückgesetzt auf Werkseinstellungen, aus der iCloud abgemeldet.\n"
            "Kleine Kratzer auf der Oberseite, sonst tadellos.\n\n"
            "Nur das Gerät + Stromkabel, keine OVP."
        ),
        image_urls=_images("🍏", "Mac mini M1", ["8 GB · 256 GB SSD", "Rückseite", "Systeminfo"], 2),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "Desktop & Workstations", "Zustand": "Gut"},
        seller_name="Lukas B.",
        seller_type="private",
        search_name="Apple техника",
        detail_loaded=True,
        first_seen=ago(100),
    )
    add(listing, {
        **_money_eval(ad, "resale", 280, est, pricing),
        "ai": AIVerdict(
            product="Apple Mac mini M1 2020 8GB/256GB",
            search_query="Mac mini M1 8GB 256GB",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=430,
            verdict="buy",
            confidence=0.78,
            reasoning="Фото «Об этом Mac» подтверждает M1 8/256. Продавец пишет, что вышел из iCloud — "
            "при встрече проверь, что нет запроса Activation Lock после сброса. Мелкие царапины на цену почти не влияют.",
            model=DEMO_AI_MODEL,
        ),
        "score": 85,
        "verdict": "buy",
        "reasons": [
            "Цена 280 € — на ~33 % ниже рынка (~420 €)",
            "Стабильный спрос: 3 продажи на eBay за 430–480 €",
            "На скриншоте «Об этом Mac» видна конфигурация",
        ],
    })

    # 4. RTX 3060 — maybe
    ad = "2894102266"
    comps = _comps([
        ("MSI GeForce RTX 3060 Ventus 2X 12G OC", 215, "ebay_sold", "Verkauft 24.09.2026"),
        ("RTX 3060 12GB Gigabyte Eagle", 205, "ebay_sold", "Verkauft 21.09.2026"),
        ("Zotac RTX 3060 Twin Edge 12GB", 199, "ebay_sold", "Verkauft 16.09.2026"),
        ("RTX 3060 12GB ASUS Dual", 240, "kleinanzeigen", "Gestern"),
        ("MSI RTX 3060 12GB wie neu", 250, "kleinanzeigen", "24.09.2026"),
    ])
    est = _estimate(comps, 230, "RTX 3060 12GB")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("msi-rtx-3060-ventus-2x-12gb", ad, 225),
        title="MSI GeForce RTX 3060 Ventus 2X 12GB OC",
        price=190,
        price_text="190 € VB",
        negotiable=True,
        location="12043 Neukölln",
        postal_code="12043",
        distance_km=6.3,
        posted_at_text="Heute, 17:10",
        description=(
            "RTX 3060 mit 12GB, ca. 2 Jahre alt, funktioniert einwandfrei.\n"
            "Ein Lüfter macht beim Start kurz Geräusche, danach leise.\n"
            "Versand gegen Aufpreis möglich."
        ),
        image_urls=_images("🎮", "RTX 3060 12GB", ["MSI Ventus 2X", "Lüfter"], 1),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "Grafikkarten", "Zustand": "In Ordnung"},
        seller_name="gamer_nk",
        seller_type="private",
        search_name="Видеокарты Берлин",
        detail_loaded=True,
        first_seen=ago(185),
    )
    add(listing, {
        **_money_eval(ad, "resale", 190, est, pricing),
        "ai": AIVerdict(
            product="MSI GeForce RTX 3060 Ventus 2X 12GB",
            search_query="RTX 3060 12GB",
            photo_matches_description=True,
            condition="used",
            red_flags=["Шумит вентилятор при старте"],
            estimated_market_price=210,
            verdict="maybe",
            confidence=0.66,
            reasoning="Карта рабочая, но прибыль небольшая: после запаса на торг остаётся ~15–20 €. "
            "Шумящий вентилятор снижает цену при перепродаже. Имеет смысл, только если сторгуешься до 160 €.",
            model=DEMO_AI_MODEL,
        ),
        "score": 58,
        "verdict": "maybe",
        "reasons": [
            "Цена 190 € всего на ~17 % ниже рынка (~230 €)",
            "Прибыль ниже порога 40 € — стоит торговаться",
            "Упомянут шумный вентилятор",
        ],
        "red_flags": ["Шумит вентилятор при старте"],
    })

    # 5. ThinkPad T480 — bought
    ad = "2893987145"
    comps = _comps([
        ("Lenovo ThinkPad T480 i5 8th Gen 16GB 256GB", 265, "ebay_sold", "Verkauft 23.09.2026"),
        ("ThinkPad T480 i5-8350U 8GB 256GB FHD", 239, "ebay_sold", "Verkauft 19.09.2026"),
        ("Lenovo T480 i7 16GB 512GB", 310, "ebay_sold", "Verkauft 11.09.2026"),
        ("ThinkPad T480 Full HD 16GB", 280, "kleinanzeigen", "Gestern"),
        ("Lenovo ThinkPad T480 i5 Laptop", 250, "kleinanzeigen", "21.09.2026"),
        ("ThinkPad T480 Touch", 290, "kleinanzeigen", "20.09.2026"),
    ])
    est = _estimate(comps, 260, "ThinkPad T480 i5 16GB")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("lenovo-thinkpad-t480-i5-16gb-256gb-ssd", ad, 278),
        title="Lenovo ThinkPad T480 i5-8350U 16GB 256GB SSD Full HD",
        price=150,
        price_text="150 € VB",
        negotiable=True,
        location="10961 Kreuzberg",
        postal_code="10961",
        distance_km=3.2,
        posted_at_text="Heute, 15:02",
        description=(
            "ThinkPad T480 aus Firmenauflösung.\n"
            "i5-8350U, 16 GB RAM, 256 GB NVMe, 14\" Full HD IPS.\n"
            "Akku hält ca. 4 Stunden (beide Akkus).\n"
            "Windows 11 Pro frisch installiert. Netzteil dabei."
        ),
        image_urls=_images("💻", "ThinkPad T480", ["i5 · 16 GB · FHD", "Tastatur", "Anschlüsse"], 3),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "Notebooks", "Zustand": "Gut"},
        seller_name="IT-Resteposten Berlin",
        seller_type="commercial",
        search_name="Ноутбуки до 200 €",
        detail_loaded=True,
        first_seen=ago(300),
    )
    add(listing, {
        **_money_eval(ad, "resale", 150, est, pricing),
        "ai": AIVerdict(
            product="Lenovo ThinkPad T480 (i5-8350U, 16GB, 256GB)",
            search_query="ThinkPad T480 i5 16GB",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=255,
            verdict="buy",
            confidence=0.8,
            reasoning="Корпоративный ноутбук в хорошем состоянии, 16 ГБ и FHD-экран — самая ходовая конфигурация. "
            "Такие быстро уходят студентам за 240–280 €.",
            model=DEMO_AI_MODEL,
        ),
        "score": 82,
        "verdict": "buy",
        "reasons": [
            "Цена 150 € на ~42 % ниже рынка (~260 €)",
            "Ходовая конфигурация (16 ГБ, FHD IPS)",
            "Коммерческий продавец — есть право на возврат",
        ],
    })

    # 6. iPhone 13 — iCloud locked, skip
    ad = "2893876502"
    comps = _comps([
        ("iPhone 13 128GB iCloud gesperrt defekt Ersatzteile", 75, "ebay_sold", "Verkauft 22.09.2026"),
        ("iPhone 13 Mitternacht gesperrt für Bastler", 60, "ebay_sold", "Verkauft 18.09.2026"),
        ("iPhone 13 Aktivierungssperre Display ok", 85, "ebay_sold", "Verkauft 14.09.2026"),
    ])
    est = _estimate(comps, 70, "iPhone 13 iCloud gesperrt", source="ebay_sold",
                    notes="Заблокированный iPhone продаётся только на запчасти")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("iphone-13-128gb-mitternacht", ad, 173),
        title="iPhone 13 128GB Mitternacht – iCloud gesperrt, sonst top",
        price=180,
        price_text="180 €",
        location="13353 Wedding",
        postal_code="13353",
        distance_km=5.5,
        posted_at_text="Heute, 11:40",
        description=(
            "Verkaufe iPhone 13, 128 GB, Farbe Mitternacht.\n"
            "Handy ist iCloud gesperrt, ich habe das Passwort vergessen.\n"
            "Display und Gehäuse wie neu.\n"
            "Nur Barzahlung, kein Versand. Schnell sein!"
        ),
        image_urls=_images("📱", "iPhone 13 128GB", ["Mitternacht", "Sperrbildschirm"], 4),
        shipping_possible=False,
        tags=["Nur Abholung"],
        attributes={"Art": "Apple", "Zustand": "Defekt"},
        seller_name="Ali",
        seller_type="private",
        search_name="Apple техника",
        detail_loaded=True,
        first_seen=ago(540),
    )
    add(listing, {
        **_money_eval(ad, "resale", 180, est, pricing),
        "ai": AIVerdict(
            product="Apple iPhone 13 128GB (Activation Lock)",
            search_query="iPhone 13 iCloud gesperrt",
            photo_matches_description=True,
            condition="defective",
            red_flags=[
                "iCloud-блокировка — телефон нельзя активировать",
                "Возможно краденый",
                "Только наличные, «быстрее»",
            ],
            estimated_market_price=70,
            verdict="skip",
            confidence=0.95,
            reasoning="Телефон с блокировкой активации: снять её без владельца нельзя, продать можно только на запчасти "
            "за 60–85 €. «Забыл пароль» + только наличные — типичный признак краденого устройства. Не связываться.",
            model=DEMO_AI_MODEL,
        ),
        "score": 6,
        "verdict": "skip",
        "reasons": [
            "Заблокирован iCloud — рыночная цена только как запчасти (~70 €)",
            "Цена 180 € выше стоимости запчастей — убыток",
            "Высокий риск краденого устройства",
        ],
        "red_flags": ["iCloud gesperrt", "Риск кражи"],
    })

    # 7. 64GB DDR4 ECC — personal (AI server)
    ad = "2893790331"
    comps = _comps([
        ("Samsung 64GB (4x16GB) DDR4 ECC REG 2666", 115, "ebay_sold", "Verkauft 24.09.2026"),
        ("4x 16GB DDR4 2666V RDIMM ECC Server RAM", 105, "ebay_sold", "Verkauft 20.09.2026"),
        ("64GB DDR4 ECC Registered Kit SK Hynix", 130, "ebay_sold", "Verkauft 13.09.2026"),
        ("Server RAM 64GB DDR4 ECC 4x16", 140, "kleinanzeigen", "Gestern"),
    ])
    est = _estimate(comps, 120, "64GB DDR4 ECC REG")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("64gb-ddr4-ecc-registered-ram-4x16gb-2666", ad, 225),
        title="64GB (4x16GB) DDR4 ECC Registered RAM 2666 MHz Samsung",
        price=70,
        price_text="70 €",
        location="12099 Tempelhof",
        postal_code="12099",
        distance_km=7.4,
        posted_at_text="Heute, 06:58",
        description=(
            "4x 16GB Samsung M393A2K40CB2-CTD, DDR4-2666 ECC REG.\n"
            "Aus einem HP Z640 ausgebaut, lief stabil (Memtest 4 Durchläufe ohne Fehler).\n"
            "Versand als Brief möglich (+3 €)."
        ),
        image_urls=_images("🧠", "64 GB DDR4 ECC", ["4× 16 GB Samsung", "Etikett"], 5),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "PC-Zubehör", "Zustand": "Gut"},
        seller_name="Tobias",
        seller_type="private",
        search_name="AI-сервер (для себя)",
        detail_loaded=True,
        first_seen=ago(780),
    )
    add(listing, {
        **_money_eval(ad, "personal", 70, est, pricing, max_buy=90),
        "ai": AIVerdict(
            product="Samsung 64GB (4x16GB) DDR4-2666 ECC RDIMM",
            search_query="64GB DDR4 ECC REG",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=115,
            verdict="buy",
            confidence=0.8,
            reasoning="На этикетке видна маркировка M393A2K40CB2-CTD — это регистровая ECC-память, подходит к Xeon E5 v3/v4 "
            "(HP Z440/Z640, Dell T5810). Для локальной LLM-машины 64 ГБ за 70 € — отличная цена.",
            model=DEMO_AI_MODEL,
        ),
        "score": 87,
        "verdict": "buy",
        "reasons": [
            "Дешевле твоего лимита 90 € и на ~42 % ниже рынка",
            "Совместима с рабочими станциями на Xeon E5 v3/v4",
            "Протестирована Memtest (по словам продавца)",
        ],
    })

    # 8. HP Z440 workstation — personal (AI server)
    ad = "2893655120"
    comps = _comps([
        ("HP Z440 Xeon E5-1650 v4 32GB 512GB SSD", 250, "ebay_sold", "Verkauft 23.09.2026"),
        ("HP Z440 Workstation E5-2680 v4 64GB", 290, "ebay_sold", "Verkauft 17.09.2026"),
        ("HP Z440 Xeon 32GB ohne Grafikkarte", 230, "ebay_sold", "Verkauft 10.09.2026"),
        ("HP Z440 Workstation Xeon E5 Tower", 280, "kleinanzeigen", "Gestern"),
    ])
    est = _estimate(comps, 260, "HP Z440 Xeon 32GB")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("hp-z440-workstation-xeon-e5-1650-v4-32gb", ad, 228),
        title="HP Z440 Workstation Xeon E5-1650 v4, 32GB DDR4 ECC, 512GB SSD",
        price=180,
        price_text="180 € VB",
        negotiable=True,
        location="14482 Potsdam",
        postal_code="14482",
        distance_km=29.5,
        posted_at_text="Heute, 01:15",
        description=(
            "HP Z440 Workstation, lief im Büro als CAD-Rechner.\n"
            "Xeon E5-1650 v4 (6 Kerne, 3,6 GHz), 32 GB DDR4 ECC (4x8), 512 GB SSD.\n"
            "700W Netzteil, 2x PCIe x16 – passt für große Grafikkarten.\n"
            "Keine Grafikkarte enthalten. Windows 10 Pro Lizenz (COA-Aufkleber)."
        ),
        image_urls=_images("🖥️", "HP Z440 Xeon", ["E5-1650 v4 · 32 GB", "Innenraum", "700W Netzteil"], 6),
        shipping_possible=False,
        tags=["Nur Abholung"],
        attributes={"Art": "Desktop & Workstations", "Zustand": "Gut"},
        seller_name="Büro Potsdam GmbH",
        seller_type="commercial",
        search_name="AI-сервер (для себя)",
        detail_loaded=True,
        first_seen=ago(1170),
    )
    add(listing, {
        **_money_eval(ad, "personal", 180, est, pricing, max_buy=210),
        "ai": AIVerdict(
            product="HP Z440 Workstation (Xeon E5-1650 v4, 32GB ECC)",
            search_query="HP Z440 Xeon 32GB",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=255,
            verdict="buy",
            confidence=0.76,
            reasoning="Отличная база для домашнего AI-сервера: БП 700 Вт и два слота PCIe x16 — встанет RTX 3090. "
            "8 слотов под ECC-память, можно добавить 64 ГБ из соседнего объявления. Минус — забирать в Потсдаме.",
            model=DEMO_AI_MODEL,
        ),
        "score": 88,
        "verdict": "buy",
        "reasons": [
            "Экономия ~80 € относительно рынка (~260 €)",
            "БП 700 Вт + 2× PCIe x16 — подходит под RTX 3090",
            "8 слотов DDR4 ECC — расширяется до 256 ГБ",
            "Далеко: ~30 км (Потсдам), только самовывоз",
        ],
    })

    # 9. Dyson V11 — buy, contacted
    ad = "2893540987"
    comps = _comps([
        ("Dyson V11 Absolute Akku-Staubsauger", 255, "ebay_sold", "Verkauft 22.09.2026"),
        ("Dyson V11 Absolute Extra komplett", 280, "ebay_sold", "Verkauft 16.09.2026"),
        ("Dyson V11 Torque Drive", 240, "ebay_sold", "Verkauft 12.09.2026"),
        ("Dyson V11 Absolute wie neu mit Zubehör", 300, "kleinanzeigen", "Gestern"),
    ])
    est = _estimate(comps, 260, "Dyson V11 Absolute")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("dyson-v11-absolute-akkusauger", ad, 176),
        title="Dyson V11 Absolute Akkusauger mit Zubehör",
        price=160,
        price_text="160 € VB",
        negotiable=True,
        location="10585 Charlottenburg",
        postal_code="10585",
        distance_km=6.8,
        posted_at_text="Gestern, 22:30",
        description=(
            "Dyson V11 Absolute, gekauft 2022. Akku hält noch ca. 45 Minuten im Eco-Modus.\n"
            "Alle Aufsätze dabei + Wandhalterung.\n"
            "Filter vor 2 Monaten gewechselt."
        ),
        image_urls=_images("🧹", "Dyson V11", ["Absolute", "Zubehör", "Display"], 8),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "Staubsauger", "Zustand": "Gut"},
        seller_name="Claudia",
        seller_type="private",
        search_name="Бытовая техника",
        detail_loaded=True,
        first_seen=ago(1560),
    )
    add(listing, {
        **_money_eval(ad, "resale", 160, est, pricing),
        "ai": AIVerdict(
            product="Dyson V11 Absolute",
            search_query="Dyson V11 Absolute",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=255,
            verdict="buy",
            confidence=0.72,
            reasoning="Полный комплект насадок и LCD-экран — это действительно V11 Absolute. "
            "Состояние аккумулятора в норме (45 мин). Хорошо продаётся, запас по цене ~70 €.",
            model=DEMO_AI_MODEL,
        ),
        "score": 77,
        "verdict": "buy",
        "reasons": [
            "Цена 160 € на ~38 % ниже рынка (~260 €)",
            "Полный комплект насадок",
            "Аккумулятор в порядке (45 мин в Eco)",
        ],
    })

    # 10. Bosch drill — maybe, ignored by user
    ad = "2893402211"
    comps = _comps([
        ("Bosch Professional GSR 18V-60 C + 2x 5,0Ah + L-Boxx", 175, "ebay_sold", "Verkauft 21.09.2026"),
        ("Bosch GSR 18V-60 C Akkuschrauber Set", 160, "ebay_sold", "Verkauft 15.09.2026"),
        ("Bosch Blau GSR 18V-60 C mit 2 Akkus", 185, "kleinanzeigen", "23.09.2026"),
    ])
    est = _estimate(comps, 170, "Bosch GSR 18V-60 C")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("bosch-professional-gsr-18v-60-c-akkuschrauber", ad, 84),
        title="Bosch Professional GSR 18V-60 C Akkuschrauber + 2 Akkus + L-Boxx",
        price=120,
        price_text="120 €",
        location="12623 Mahlsdorf",
        postal_code="12623",
        distance_km=14.2,
        posted_at_text="Gestern, 16:05",
        description=(
            "Bosch Professional GSR 18V-60 C, bürstenlos.\n"
            "2x 4,0 Ah Akku, Ladegerät GAL 18V-40, L-Boxx 102.\n"
            "Gebrauchsspuren, voll funktionsfähig."
        ),
        image_urls=_images("🔧", "Bosch GSR 18V-60 C", ["2 Akkus + L-Boxx", "Ladegerät"], 9),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "Werkzeug", "Zustand": "In Ordnung"},
        seller_name="Handwerker_Marzahn",
        seller_type="private",
        search_name="Инструменты",
        detail_loaded=True,
        first_seen=ago(1860),
    )
    add(listing, {
        **_money_eval(ad, "resale", 120, est, pricing),
        "ai": AIVerdict(
            product="Bosch Professional GSR 18V-60 C (2x 4,0Ah)",
            search_query="Bosch GSR 18V-60 C",
            photo_matches_description=True,
            condition="used",
            estimated_market_price=165,
            verdict="maybe",
            confidence=0.61,
            reasoning="Набор полный, но аккумуляторы 4,0 А·ч, а в проданных комплектах чаще 5,0 А·ч. "
            "Прибыль ~30 € — терпимо, если по пути.",
            model=DEMO_AI_MODEL,
        ),
        "score": 61,
        "verdict": "maybe",
        "reasons": [
            "Цена 120 € на ~29 % ниже рынка (~170 €)",
            "Прибыль ~33 € — ниже порога 40 €",
            "Далеко: ~14 км (Мальсдорф)",
        ],
    })

    # 11. PS5 Disc — maybe
    ad = "2893280764"
    comps = _comps([
        ("Sony PlayStation 5 Disc Edition 825GB + Controller", 360, "ebay_sold", "Verkauft 24.09.2026"),
        ("PS5 Disc Version mit 2 Controllern", 395, "ebay_sold", "Verkauft 20.09.2026"),
        ("PlayStation 5 Standard Edition CFI-1216A", 370, "ebay_sold", "Verkauft 17.09.2026"),
        ("PS5 Disc + 2 Controller + Spiele", 420, "kleinanzeigen", "Heute"),
        ("Playstation 5 Disc wie neu", 400, "kleinanzeigen", "23.09.2026"),
    ])
    est = _estimate(comps, 380, "PS5 Disc Edition 2 Controller")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("playstation-5-disc-edition-2-controller", ad, 279),
        title="PlayStation 5 Disc Edition + 2 DualSense Controller",
        price=330,
        price_text="330 € VB",
        negotiable=True,
        location="13597 Spandau",
        postal_code="13597",
        distance_km=15.8,
        posted_at_text="Gestern, 09:12",
        description=(
            "PS5 mit Laufwerk, 2 Controller (einer mit leichtem Stick-Drift).\n"
            "Alle Kabel + OVP vorhanden.\n"
            "Nichtraucherhaushalt."
        ),
        image_urls=_images("🕹️", "PS5 Disc", ["+ 2 DualSense", "OVP"], 7),
        shipping_possible=True,
        tags=["Versand möglich"],
        attributes={"Art": "PlayStation", "Zustand": "Gut"},
        seller_name="Kevin",
        seller_type="private",
        search_name="Консоли",
        detail_loaded=True,
        first_seen=ago(2280),
    )
    add(listing, {
        **_money_eval(ad, "resale", 330, est, pricing),
        "ai": AIVerdict(
            product="Sony PlayStation 5 Disc Edition (CFI-1216A)",
            search_query="PS5 Disc Edition 2 Controller",
            photo_matches_description=True,
            condition="good",
            red_flags=["Дрифт стика у второго геймпада"],
            estimated_market_price=370,
            verdict="maybe",
            confidence=0.63,
            reasoning="Консоль в хорошем состоянии с коробкой, но маржа маленькая: ~10 € после запаса на торг. "
            "Интересно только при торге до 290–300 €.",
            model=DEMO_AI_MODEL,
        ),
        "score": 52,
        "verdict": "maybe",
        "reasons": [
            "Цена 330 € лишь на ~13 % ниже рынка (~380 €)",
            "Торг возможен (VB)",
            "Один геймпад с дрифтом",
        ],
        "red_flags": ["Дрифт стика у второго геймпада"],
    })

    # 12. Canyon road bike — overpriced, skip
    ad = "2893109876"
    comps = _comps([
        ("Canyon Endurace CF 7 Rahmengröße M 2021", 1650, "ebay_sold", "Verkauft 19.09.2026"),
        ("Canyon Endurace CF 7 105 Di2", 1750, "ebay_sold", "Verkauft 09.09.2026"),
        ("Canyon Endurace CF 7 Größe M", 1800, "kleinanzeigen", "22.09.2026"),
        ("Canyon Endurace CF7 Carbon Rennrad", 1690, "kleinanzeigen", "18.09.2026"),
    ])
    est = _estimate(comps, 1700, "Canyon Endurace CF 7")
    listing = Listing(
        ad_id=ad,
        url=_ka_url("canyon-endurace-cf-7-rennrad-carbon", ad, 217),
        title="Canyon Endurace CF 7 Rennrad Carbon, Größe M",
        price=1900,
        price_text="1.900 €",
        location="10827 Schöneberg",
        postal_code="10827",
        distance_km=5.1,
        posted_at_text="25.09.2026",
        description=(
            "Canyon Endurace CF 7, Baujahr 2022, ca. 3.500 km.\n"
            "Shimano 105, Carbon-Rahmen, Größe M.\n"
            "Preis ist fest!"
        ),
        image_urls=_images("🚴", "Canyon Endurace", ["CF 7 · Größe M", "Schaltwerk"], 10),
        shipping_possible=False,
        tags=["Nur Abholung"],
        attributes={"Art": "Herren", "Typ": "Rennräder", "Zustand": "Gut"},
        seller_name="Felix",
        seller_type="private",
        search_name="Велосипеды",
        detail_loaded=True,
        first_seen=ago(2760),
    )
    add(listing, {
        **_money_eval(ad, "resale", 1900, est, pricing),
        "ai": AIVerdict(
            product="Canyon Endurace CF 7 (2022, Shimano 105)",
            search_query="Canyon Endurace CF 7",
            photo_matches_description=True,
            condition="good",
            estimated_market_price=1700,
            verdict="skip",
            confidence=0.83,
            reasoning="Хороший велосипед, но цена выше рынка на ~200 € и «Festpreis» — перепродать с прибылью не выйдет.",
            model=DEMO_AI_MODEL,
        ),
        "score": 14,
        "verdict": "skip",
        "reasons": [
            "Цена 1 900 € выше рынка (~1 700 €)",
            "Фиксированная цена — торг не предполагается",
        ],
        "red_flags": ["Дороже рынка"],
    })

    # 13. eBay auction ending soon
    ad = "ebay-306512349871"
    comps = _comps([
        ("NVIDIA GeForce RTX 4070 Founders Edition 12GB", 515, "ebay_sold", "Verkauft 25.09.2026"),
        ("RTX 4070 FE 12GB GDDR6X", 530, "ebay_sold", "Verkauft 23.09.2026"),
        ("MSI RTX 4070 Ventus 2X 12G OC", 495, "ebay_sold", "Verkauft 21.09.2026"),
        ("ASUS Dual RTX 4070 12GB", 510, "ebay_sold", "Verkauft 18.09.2026"),
        ("RTX 4070 Founders Edition wie neu", 560, "ebay", "Sofort-Kaufen"),
        ("Nvidia RTX 4070 FE OVP", 540, "kleinanzeigen", "Gestern"),
    ])
    est = _estimate(comps, 520, "RTX 4070 12GB", source="ebay_sold")
    listing = Listing(
        ad_id=ad,
        source="ebay",
        url="https://www.ebay.de/itm/306512349871",
        title="NVIDIA GeForce RTX 4070 Founders Edition 12GB GDDR6X",
        price=310,
        price_text="310,00 EUR",
        location="04109 Leipzig",
        postal_code="04109",
        posted_at_text="",
        description=(
            "Verkaufe meine RTX 4070 Founders Edition.\n"
            "Gekauft 11/2023 bei NBB, Rechnung liegt bei (Restgarantie).\n"
            "Nie übertaktet, kein Mining. Wird sicher im Originalkarton verschickt.\n\n"
            "Privatverkauf, keine Rücknahme."
        ),
        image_urls=_images("🎮", "RTX 4070 FE", ["Founders Edition 12GB", "OVP", "Anschlüsse"], 1),
        shipping_possible=True,
        tags=["Auktion", "Versand 6,99 €"],
        attributes={"Marke": "NVIDIA", "Speichergröße": "12 GB", "Speichertyp": "GDDR6X"},
        seller_name="berlin_gpu_fan",
        seller_type="private",
        condition="Gebraucht",
        buying_options=["AUCTION"],
        bid_count=7,
        ends_at=now + timedelta(hours=2, minutes=15),
        shipping_cost=6.99,
        seller_feedback_percent=99.6,
        seller_feedback_score=1284,
        search_name="eBay: видеокарты (аукционы)",
        detail_loaded=True,
        first_seen=ago(40),
    )
    add(listing, {
        **_money_eval(ad, "resale", 310, est, pricing, buyer_shipping=6.99),
        "ai": AIVerdict(
            product="NVIDIA GeForce RTX 4070 Founders Edition 12GB",
            search_query="RTX 4070 12GB",
            photo_matches_description=True,
            condition="like_new",
            estimated_market_price=520,
            verdict="buy",
            confidence=0.79,
            reasoning="На фото оригинальная FE в коробке, без следов вскрытия. Текущая ставка 310 € сильно ниже продаж (495–530 €). "
            "Аукцион закончится через ~2 часа — ставь ближе к концу.",
            model=DEMO_AI_MODEL,
        ),
        "ai_second": AIVerdict(
            product="NVIDIA GeForce RTX 4070 Founders Edition",
            search_query="RTX 4070 Founders Edition",
            photo_matches_description=True,
            condition="like_new",
            red_flags=["Цена на аукционе вырастет в последние минуты"],
            estimated_market_price=510,
            verdict="maybe",
            confidence=0.7,
            reasoning="Карта выглядит честно, продавец с 99,6 % положительных отзывов. Но 310 € — это не финальная цена: "
            "такие лоты обычно уходят за 430–470 €. Выгодно только если финальная ставка не выше ~367 €.",
            model=SECOND_OPINION_MODEL,
        ),
        "score": 83,
        "verdict": "buy",
        "reasons": [
            "Текущая ставка 310 € + 6,99 € доставка — на ~40 % ниже рынка (~520 €)",
            "4 продажи на eBay за 495–530 €",
            "Продавец: 99,6 % положительных отзывов (1 284)",
            "Аукцион: ставь не выше 367 €, иначе прибыль ниже порога",
        ],
    })

    # 14. eBay Buy-It-Now with best offer
    ad = "ebay-205873410266"
    comps = _comps([
        ("Apple iPad Air 5. Gen M1 64GB WiFi Space Grau", 425, "ebay_sold", "Verkauft 24.09.2026"),
        ("iPad Air 5 64GB Wi-Fi Blau", 410, "ebay_sold", "Verkauft 22.09.2026"),
        ("iPad Air (5. Generation) 64 GB WLAN", 445, "ebay_sold", "Verkauft 19.09.2026"),
        ("iPad Air 5 M1 64GB wie neu", 460, "ebay", "Sofort-Kaufen"),
        ("iPad Air 5 64GB Polarstern", 450, "kleinanzeigen", "23.09.2026"),
    ])
    est = _estimate(comps, 430, "iPad Air 5 M1 64GB", source="ebay_sold")
    listing = Listing(
        ad_id=ad,
        source="ebay",
        url="https://www.ebay.de/itm/205873410266",
        title="Apple iPad Air 5. Gen (M1) 64GB WiFi Space Grau – Top Zustand",
        price=289,
        price_text="289,00 EUR",
        location="80331 München",
        postal_code="80331",
        description=(
            "iPad Air 5 mit M1 Chip, 64 GB, WiFi.\n"
            "Immer mit Hülle und Schutzfolie benutzt, keine Kratzer.\n"
            "Akkukapazität 94 %. Aus Apple-ID abgemeldet, zurückgesetzt.\n"
            "Mit Originalkarton und USB-C-Kabel."
        ),
        image_urls=_images("📲", "iPad Air 5 M1", ["64 GB · Space Grau", "Akku 94 %"], 4),
        shipping_possible=True,
        tags=["Sofort-Kaufen", "Preisvorschlag", "Versand 4,99 €"],
        attributes={"Marke": "Apple", "Speicherkapazität": "64 GB", "Farbe": "Space Grau"},
        seller_name="mia_tech_verkauf",
        seller_type="private",
        condition="Gebraucht – Sehr gut",
        buying_options=["FIXED_PRICE", "BEST_OFFER"],
        shipping_cost=4.99,
        seller_feedback_percent=100.0,
        seller_feedback_score=412,
        search_name="eBay: iPad",
        detail_loaded=True,
        first_seen=ago(420),
    )
    add(listing, {
        **_money_eval(ad, "resale", 289, est, pricing, buyer_shipping=4.99),
        "ai": AIVerdict(
            product="Apple iPad Air (5th gen, M1) 64GB Wi-Fi",
            search_query="iPad Air 5 M1 64GB",
            photo_matches_description=True,
            condition="like_new",
            estimated_market_price=430,
            verdict="buy",
            confidence=0.81,
            reasoning="Скриншот настроек подтверждает iPad Air 5-го поколения и ёмкость аккумулятора 94 %. "
            "Продавец отвязал Apple ID. Можно ещё предложить цену (Preisvorschlag) — 270 €.",
            model=DEMO_AI_MODEL,
        ),
        "score": 79,
        "verdict": "buy",
        "reasons": [
            "289 € + 4,99 € доставка — на ~32 % ниже рынка (~430 €)",
            "Купить сразу + можно предложить свою цену",
            "Продавец: 100 % положительных отзывов (412)",
        ],
    })

    return deals


# user state for a few demo deals: ad_id -> (status, note)
_DEMO_STATUS: dict[str, tuple[str, str]] = {
    "2893987145": ("bought", "Забрал за 140 €, выставить на eBay в пятницу"),
    "2894311780": ("starred", "Спросить про Activation Lock"),
    "2893540987": ("contacted", "Написал продавцу, жду ответ"),
    "2893402211": ("ignored", ""),
}


def seed_demo(db: Database) -> int:
    """Insert the demo deals (idempotent), set a few statuses and add sample
    monitor runs. Returns the number of newly inserted listings."""
    inserted = 0
    for listing, evaluation in demo_deals():
        if db.upsert_listing(listing):
            inserted += 1
            status = _DEMO_STATUS.get(listing.ad_id)
            if status:
                db.set_status(listing.ad_id, status[0], status[1])
        # evaluations are upserts; only write them once so user edits survive
        if db.get_evaluation(evaluation.ad_id) is None:
            db.save_evaluation(evaluation)

    if not db.list_runs(limit=1):
        now = utcnow()
        samples = [
            (95, 142, dict(searches=8, listings_seen=214, new_listings=9, evaluated=9, deals_found=3, notified=3)),
            (50, 118, dict(searches=8, listings_seen=198, new_listings=4, evaluated=4, deals_found=1, notified=1,
                           errors=["Инструменты: HTTP 429 Too Many Requests — повтор через 60 с"])),
            (5, 131, dict(searches=8, listings_seen=221, new_listings=6, evaluated=6, deals_found=2, notified=2)),
        ]
        for minutes_ago, duration_s, fields in samples:
            run = db.start_run()
            started = now - timedelta(minutes=minutes_ago)
            finished = started + timedelta(seconds=duration_s)
            summary = RunSummary(id=run.id, started_at=started, finished_at=finished, **fields)
            db.finish_run(summary)
    return inserted
