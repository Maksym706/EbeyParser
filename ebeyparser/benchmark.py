"""Offline benchmark: a synthetic Berlin second-hand market with hidden ground truth.

The real `Monitor` pipeline runs against fake Kleinanzeigen / eBay (sold scraper and Browse
API) sources and a fake "oracle" vision model. Every listing carries the truth (product,
true market price, planted kind: normal / deal / trap), so the verdicts stored in the DB
can be scored: precision and recall of "buy" (raw and at the notification threshold),
traps that slipped through, market-estimate error, product-identity collisions among the
comparables, special cases (auctions, VB haggling, AI down, private-sale disclaimers) and
the request cost per ad.

Everything is deterministic for a given seed, and the fakes draw their randomness from
(seed, ad id) rather than from call order, so engine changes don't reshuffle the world.
Trap texts are written as sellers write them (plain wording, paraphrases, typos, subtle
hints) — deliberately not derived from the regexes in pricing/text.py.

    python -m ebeyparser.benchmark --seed 1 --n 600 --ai oracle
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import logging
import math
import random
import re
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from statistics import median
from typing import Any, Callable, Iterator, Sequence

from .config import AppConfig, PricingConfig, parse_config
from .db import Database
from .models import AIVerdict, Comparable, Evaluation, Listing, utcnow
from .monitor import Monitor
from .pricing import estimator as _estimator
from .pricing import text as _text
from .pricing.text import SEVERE_FLAGS, normalize
from .scraper.http import BlockedError

try:  # the real scraper drops "Suche ..." titles from comparables; mimic it exactly
    from .scraper.kleinanzeigen import _WANTED_RE as _SCRAPER_WANTED_RE
except ImportError:  # pragma: no cover - engine refactor
    _SCRAPER_WANTED_RE = re.compile(r"^\W*(?:suche|suchen|kaufe|ankauf|gesuch)\b", re.IGNORECASE)
try:
    from .scraper.kleinanzeigen import COMPARABLE_MAX_PAGES as _COMPS_PAGES
except ImportError:  # pragma: no cover
    _COMPS_PAGES = 2

log = logging.getLogger(__name__)

RESULTS_PER_PAGE = 25
EBAY_SHARE = 0.08  # share of the stream listed on eBay (Browse API searches)
AI_MODES = ("oracle", "noisy", "down", "off")
# (trap recall, market-price noise, false-alarm rate on clean ads)
AI_PRESETS: dict[str, tuple[float, float, float]] = {"oracle": (0.85, 0.15, 0.03), "noisy": (0.6, 0.30, 0.10)}
# Quality targets (documented by the xfail tests; the report marks them ✔/✖).
TARGET_PRECISION = 0.8
TARGET_RECALL = 0.6
TARGET_MEDIAN_ERROR = 0.2

# ---------------------------------------------------------------------------
# Catalog: categories and products with their TRUE used-market price (EUR, DE, 2025-26)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Category:
    key: str
    name_ru: str
    category_id: int  # Kleinanzeigen-like category id (used by category scans)
    noun: str  # generic German word for vague titles: "Handy zu verkaufen"
    weight: float  # share of ads in the stream


CATEGORIES: tuple[Category, ...] = (
    Category("smartphone", "Смартфоны", 173, "Handy", 3.0),
    Category("laptop", "Ноутбуки", 278, "Laptop", 1.2),
    Category("console", "Консоли", 279, "Konsole", 1.5),
    Category("gpu", "Видеокарты", 225, "Grafikkarte", 1.5),
    Category("tablet", "Планшеты", 285, "Tablet", 1.0),
    Category("audio", "Аудио", 172, "Kopfhörer", 1.0),
    Category("camera", "Фото", 245, "Kamera", 0.7),
    Category("smartwatch", "Смарт-часы", 156, "Smartwatch", 0.8),
    Category("tools", "Инструменты", 84, "Werkzeug", 0.8),
    Category("household", "Бытовая техника", 176, "Haushaltsgerät", 0.8),
    Category("escooter", "Электросамокаты", 217, "E-Scooter", 0.5),
    Category("vr", "VR-очки", 227, "VR-Brille", 0.5),
    Category("drone", "Дроны", 161, "Drohne", 0.5),
)
CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}


@dataclass(frozen=True)
class Product:
    pid: str
    category: str
    brand: str
    model: str  # as sellers write it, without brand: "iPhone 13 Pro"
    price: float  # TRUE used-market (resale) price in EUR
    spec: str = ""  # price-relevant variant detail: "128GB"
    forms: tuple[str, ...] = ()  # alternative spellings of `model`
    colors: tuple[str, ...] = ()
    brand_rate: float = 0.5  # how often sellers put the brand into the title
    premium: bool = False  # hot item: used for "too good to be true" traps

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.brand, self.model, self.spec) if p)

    @property
    def storage_gb(self) -> str:
        m = re.fullmatch(r"(\d+)\s?GB", self.spec)
        return m.group(1) if m else ""

    @property
    def ai_query(self) -> str:
        """What a good vision model would type to find comparables."""
        storage = f"{self.storage_gb}GB" if self.storage_gb else ""
        if self.category == "gpu":
            return normalize(self.model)
        brand = "" if self.brand == "Apple" else self.brand.split()[0]
        return normalize(f"{brand} {self.model} {storage}")


_IPHONE = ("Schwarz", "Blau", "Mitternacht", "Polarstern", "Rot", "Grün", "Weiß")
_IPHONE_PRO = ("Graphit", "Silber", "Gold", "Sierrablau", "Space Schwarz", "Dunkellila", "Titan Natur")
_SAMSUNG = ("Phantom Black", "Schwarz", "Lavender", "Cream", "Grün")


def _p(pid: str, cat: str, brand: str, model: str, price: float, spec: str = "", forms: Sequence[str] = (),
       colors: Sequence[str] = (), brand_rate: float = 0.5, premium: bool = False) -> Product:
    return Product(pid, cat, brand, model, float(price), spec, tuple(forms), tuple(colors), brand_rate, premium)


PRODUCTS: tuple[Product, ...] = (
    # smartphones
    _p("iphone12_64", "smartphone", "Apple", "iPhone 12", 230, "64GB", colors=_IPHONE, brand_rate=0.35),
    _p("iphone12_128", "smartphone", "Apple", "iPhone 12", 260, "128GB", colors=_IPHONE, brand_rate=0.35),
    _p("iphone12pro_128", "smartphone", "Apple", "iPhone 12 Pro", 320, "128GB", colors=_IPHONE_PRO, brand_rate=0.35),
    _p("iphone13_128", "smartphone", "Apple", "iPhone 13", 340, "128GB", colors=_IPHONE, brand_rate=0.35),
    _p("iphone13_256", "smartphone", "Apple", "iPhone 13", 390, "256GB", colors=_IPHONE, brand_rate=0.35),
    _p("iphone13mini_128", "smartphone", "Apple", "iPhone 13 mini", 270, "128GB", colors=_IPHONE, brand_rate=0.35),
    _p("iphone13pro_128", "smartphone", "Apple", "iPhone 13 Pro", 440, "128GB", colors=_IPHONE_PRO, brand_rate=0.35),
    _p("iphone13promax_256", "smartphone", "Apple", "iPhone 13 Pro Max", 540, "256GB", colors=_IPHONE_PRO,
       brand_rate=0.35),
    _p("iphone14_128", "smartphone", "Apple", "iPhone 14", 410, "128GB", colors=_IPHONE, brand_rate=0.35),
    _p("iphone14pro_128", "smartphone", "Apple", "iPhone 14 Pro", 560, "128GB", colors=_IPHONE_PRO, brand_rate=0.35),
    _p("iphone14pro_256", "smartphone", "Apple", "iPhone 14 Pro", 620, "256GB", colors=_IPHONE_PRO, brand_rate=0.35),
    _p("iphone14promax_256", "smartphone", "Apple", "iPhone 14 Pro Max", 700, "256GB", colors=_IPHONE_PRO,
       brand_rate=0.35),
    _p("iphone15_128", "smartphone", "Apple", "iPhone 15", 520, "128GB", colors=_IPHONE, brand_rate=0.35,
       premium=True),
    _p("iphone15pro_128", "smartphone", "Apple", "iPhone 15 Pro", 700, "128GB", colors=_IPHONE_PRO, brand_rate=0.35,
       premium=True),
    _p("iphone15promax_256", "smartphone", "Apple", "iPhone 15 Pro Max", 860, "256GB", colors=_IPHONE_PRO,
       brand_rate=0.35, premium=True),
    _p("galaxys21_128", "smartphone", "Samsung", "Galaxy S21", 170, "128GB", ("Galaxy S21 5G", "S21 5G"), _SAMSUNG, 0.6),
    _p("galaxys22_128", "smartphone", "Samsung", "Galaxy S22", 240, "128GB", ("Galaxy S22 5G",), _SAMSUNG, 0.6),
    _p("galaxys22ultra_128", "smartphone", "Samsung", "Galaxy S22 Ultra", 420, "128GB", (), _SAMSUNG, 0.6),
    _p("galaxys23fe_128", "smartphone", "Samsung", "Galaxy S23 FE", 270, "128GB", (), _SAMSUNG, 0.6),
    _p("galaxys23_128", "smartphone", "Samsung", "Galaxy S23", 370, "128GB", ("Galaxy S23 5G",), _SAMSUNG, 0.6),
    _p("galaxys23plus_256", "smartphone", "Samsung", "Galaxy S23+", 450, "256GB", ("Galaxy S23 Plus",), _SAMSUNG, 0.6),
    _p("galaxys23ultra_256", "smartphone", "Samsung", "Galaxy S23 Ultra", 620, "256GB", (), _SAMSUNG, 0.6,
       premium=True),
    _p("pixel7_128", "smartphone", "Google", "Pixel 7", 220, "128GB", (), ("Obsidian", "Snow", "Lemongrass"), 0.5),
    _p("pixel7pro_128", "smartphone", "Google", "Pixel 7 Pro", 310, "128GB", (), ("Obsidian", "Hazel"), 0.5),
    _p("pixel8_128", "smartphone", "Google", "Pixel 8", 340, "128GB", (), ("Obsidian", "Hazel", "Rose"), 0.5),
    # laptops
    _p("thinkpad_t480", "laptop", "Lenovo", "ThinkPad T480", 250, "i5 8GB 256GB", ("Thinkpad T480 i5",), (), 0.5),
    _p("thinkpad_t14g2", "laptop", "Lenovo", "ThinkPad T14 Gen 2", 470, "i5 16GB", (), (), 0.5),
    _p("mba_m1", "laptop", "Apple", "MacBook Air M1", 520, "8GB 256GB", ("MacBook Air 13 M1", "MacBook Air M1 2020"),
       ("Space Grau", "Silber", "Gold"), 0.4),
    _p("mba_m2", "laptop", "Apple", "MacBook Air M2", 760, "8GB 256GB", ("MacBook Air M2 2022",),
       ("Mitternacht", "Polarstern", "Space Grau"), 0.4, premium=True),
    _p("mbp14_m1pro", "laptop", "Apple", "MacBook Pro 14 M1 Pro", 1250, "16GB 512GB", ("MacBook Pro 14 Zoll M1 Pro",),
       ("Space Grau", "Silber"), 0.4, premium=True),
    _p("xps13", "laptop", "Dell", "XPS 13 9310", 560, "i7 16GB", ("XPS 13 i7",), (), 0.7),
    # consoles
    _p("ps5_disc", "console", "Sony", "PS5 Disc Edition", 380, "",
       ("PlayStation 5 Disc", "PS5 mit Laufwerk", "Playstation 5 Standard Edition", "PS5 Konsole Disc"), (), 0.2,
       premium=True),
    _p("ps5_digital", "console", "Sony", "PS5 Digital Edition", 320, "",
       ("PlayStation 5 Digital Edition", "PS5 Digital"), (), 0.2),
    _p("switch_oled", "console", "Nintendo", "Switch OLED", 240, "", ("Switch OLED Modell", "Switch OLED Weiß"), (), 0.6),
    _p("switch_v2", "console", "Nintendo", "Switch", 170, "", ("Switch Konsole", "Switch V2 rot/blau"), (), 0.8),
    _p("switch_lite", "console", "Nintendo", "Switch Lite", 120, "", ("Switch Lite Türkis", "Switch Lite Koralle"), (),
       0.6),
    _p("xbox_sx", "console", "Microsoft", "Xbox Series X", 330, "", ("Xbox Series X 1TB",), (), 0.2),
    _p("xbox_ss", "console", "Microsoft", "Xbox Series S", 190, "", ("Xbox Series S 512GB",), (), 0.2),
    _p("steamdeck_lcd", "console", "Valve", "Steam Deck", 320, "512GB", ("Steam Deck LCD",), (), 0.3),
    _p("steamdeck_oled", "console", "Valve", "Steam Deck OLED", 450, "512GB", (), (), 0.3, premium=True),
    # graphics cards (brand = board partner, see _gpu_title)
    _p("rtx3060", "gpu", "", "RTX 3060", 210, "12GB"),
    _p("rtx3060ti", "gpu", "", "RTX 3060 Ti", 240, "8GB"),
    _p("rtx3070", "gpu", "", "RTX 3070", 270, "8GB"),
    _p("rtx3070ti", "gpu", "", "RTX 3070 Ti", 300, "8GB"),
    _p("rtx3080", "gpu", "", "RTX 3080", 390, "10GB"),
    _p("rtx3080ti", "gpu", "", "RTX 3080 Ti", 480, "12GB"),
    _p("rtx4070", "gpu", "", "RTX 4070", 460, "12GB", premium=True),
    _p("rtx4070ti", "gpu", "", "RTX 4070 Ti", 580, "12GB"),
    _p("rx6800", "gpu", "", "RX 6800", 320, "16GB"),
    _p("rx6800xt", "gpu", "", "RX 6800 XT", 370, "16GB"),
    # tablets
    _p("ipad9_64", "tablet", "Apple", "iPad 9", 200, "64GB", ("iPad 9. Generation", "iPad (9. Gen) WiFi"),
       ("Space Grau", "Silber"), 0.4),
    _p("ipad10_64", "tablet", "Apple", "iPad 10", 290, "64GB", ("iPad 10. Generation", "iPad (10. Gen) WiFi"),
       ("Blau", "Silber", "Rosé"), 0.4),
    _p("ipadair5_64", "tablet", "Apple", "iPad Air 5", 410, "64GB", ("iPad Air 5. Generation M1", "iPad Air (5. Gen)"),
       ("Space Grau", "Blau", "Violett"), 0.4),
    _p("ipadpro11_m1", "tablet", "Apple", "iPad Pro 11 M1", 560, "128GB", ("iPad Pro 11 Zoll M1 2021",),
       ("Space Grau", "Silber"), 0.4, premium=True),
    _p("tabs8", "tablet", "Samsung", "Galaxy Tab S8", 330, "128GB", (), ("Graphite", "Silver"), 0.6),
    # audio
    _p("airpodspro2", "audio", "Apple", "AirPods Pro 2", 130, "",
       ("AirPods Pro 2. Generation", "AirPods Pro (2. Gen) USB-C", "Airpods Pro 2 MagSafe"), (), 0.3, premium=True),
    _p("airpodspro1", "audio", "Apple", "AirPods Pro", 75, "", ("AirPods Pro 1. Generation", "AirPods Pro (1. Gen)"),
       (), 0.3),
    _p("wh1000xm4", "audio", "Sony", "WH-1000XM4", 150, "", ("WH-1000XM4 Kopfhörer", "WH1000XM4"), ("Schwarz", "Silber"),
       0.7),
    _p("wh1000xm5", "audio", "Sony", "WH-1000XM5", 210, "", ("WH-1000XM5 Kopfhörer", "WH1000XM5"), ("Schwarz", "Silber"),
       0.7),
    _p("boseqc45", "audio", "Bose", "QuietComfort 45", 140, "", ("QC45 Kopfhörer", "QC 45"), ("Schwarz", "Weiß"), 0.8),
    _p("boseqcultra", "audio", "Bose", "QuietComfort Ultra Headphones", 230, "", ("QC Ultra Kopfhörer",), (), 0.8),
    _p("jblcharge5", "audio", "JBL", "Charge 5", 95, "", ("Charge 5 Bluetooth Lautsprecher",), ("Blau", "Schwarz"),
       0.9),
    # cameras
    _p("a6000", "camera", "Sony", "Alpha 6000", 300, "16-50mm Kit", ("A6000 Kit 16-50", "ILCE-6000 + 16-50mm"), (),
       0.7),
    _p("a7iii", "camera", "Sony", "Alpha 7 III", 1000, "Body", ("A7 III Gehäuse", "A7III Body"), (), 0.7,
       premium=True),
    _p("eos250d", "camera", "Canon", "EOS 250D", 400, "18-55mm Kit", ("EOS 250D Spiegelreflex", "250D + 18-55"), (),
       0.8),
    _p("gopro11", "camera", "GoPro", "Hero 11 Black", 220, "", ("HERO11 Black",), (), 0.9),
    # smartwatches
    _p("aw_s7", "smartwatch", "Apple", "Watch Series 7", 190, "45mm", ("Watch S7", "Watch Series 7 GPS"),
       ("Mitternacht", "Polarstern", "Grün"), 0.9),
    _p("aw_s8", "smartwatch", "Apple", "Watch Series 8", 240, "45mm", ("Watch S8", "Watch Series 8 GPS"),
       ("Mitternacht", "Polarstern", "Silber"), 0.9),
    _p("aw_ultra", "smartwatch", "Apple", "Watch Ultra", 450, "49mm", ("Watch Ultra 1",), (), 0.9, premium=True),
    _p("fenix6pro", "smartwatch", "Garmin", "Fenix 6 Pro", 220, "", ("fenix 6 Pro Solar",), (), 0.8),
    _p("fenix7", "smartwatch", "Garmin", "Fenix 7", 350, "", ("fenix 7 GPS",), (), 0.8),
    # tools
    _p("bosch_gsr18v55", "tools", "Bosch Professional", "GSR 18V-55", 170, "Set 2x 2,0Ah",
       ("GSR 18V-55 Akkuschrauber",), (), 0.8),
    _p("makita_dhp485", "tools", "Makita", "DHP485", 190, "Set 2x 5,0Ah + Lader",
       ("DHP485 Akku-Schlagbohrschrauber", "DHP485RTJ"), (), 0.9),
    _p("bosch_gbh228", "tools", "Bosch Professional", "GBH 2-28 F", 160, "Bohrhammer", ("GBH 2-28 Bohrhammer",), (),
       0.8),
    _p("dewalt_dcd796", "tools", "DeWalt", "DCD796", 160, "Set mit 2 Akkus", ("DCD796 Schlagbohrschrauber",), (), 0.9),
    # household
    _p("dyson_v11", "household", "Dyson", "V11 Absolute", 250, "", ("V11 Akkusauger", "V11"), (), 0.9),
    _p("dyson_v15", "household", "Dyson", "V15 Detect", 380, "", ("V15 Detect Absolute", "V15 Akkusauger"), (), 0.9,
       premium=True),
    _p("thermomix_tm6", "household", "Vorwerk", "Thermomix TM6", 780, "", ("TM6",), (), 0.4, premium=True),
    _p("kitchenaid_artisan", "household", "KitchenAid", "Artisan 5KSM175", 280, "",
       ("Artisan Küchenmaschine", "5KSM175PS Artisan"), ("Rot", "Creme"), 0.8),
    _p("roborock_s7", "household", "Roborock", "S7", 230, "", ("S7 Saugroboter",), (), 0.95),
    # e-scooters
    _p("xiaomi_4pro", "escooter", "Xiaomi", "Electric Scooter 4 Pro", 330, "", ("Mi Scooter 4 Pro", "E-Scooter 4 Pro"),
       (), 0.8),
    _p("ninebot_g30", "escooter", "Segway", "Ninebot Max G30D", 360, "", ("Ninebot Max G30D II", "Ninebot G30D"), (),
       0.5),
    # VR
    _p("quest2", "vr", "Meta", "Quest 2", 170, "128GB", ("Oculus Quest 2", "Quest 2 VR Brille"), (), 0.6),
    _p("quest3", "vr", "Meta", "Quest 3", 390, "128GB", ("Quest 3 VR Brille",), (), 0.6, premium=True),
    # drones
    _p("djimini3", "drone", "DJI", "Mini 3", 330, "", ("Mini 3 Drohne",), (), 0.9),
    _p("djimini2se", "drone", "DJI", "Mini 2 SE", 190, "", ("Mini 2 SE Drohne",), (), 0.9),
    _p("djiair2s", "drone", "DJI", "Air 2S", 480, "", ("Air 2S Drohne", "Mavic Air 2S"), (), 0.9, premium=True),
)
PRODUCT_BY_ID = {p.pid: p for p in PRODUCTS}

# (pricier, cheaper): variants sellers mix up or that search engines confuse
VARIANT_PAIRS: tuple[tuple[str, str], ...] = (
    ("iphone13_128", "iphone13mini_128"), ("iphone13pro_128", "iphone13_128"), ("iphone13_256", "iphone13_128"),
    ("iphone13promax_256", "iphone13pro_128"), ("iphone14pro_256", "iphone14pro_128"),
    ("iphone12_128", "iphone12_64"), ("galaxys23_128", "galaxys23fe_128"), ("galaxys23plus_256", "galaxys23_128"),
    ("galaxys23ultra_256", "galaxys23_128"), ("galaxys22ultra_128", "galaxys22_128"), ("pixel7pro_128", "pixel7_128"),
    ("mba_m2", "mba_m1"), ("ps5_disc", "ps5_digital"), ("switch_oled", "switch_lite"), ("switch_oled", "switch_v2"),
    ("xbox_sx", "xbox_ss"), ("steamdeck_oled", "steamdeck_lcd"), ("rtx3080ti", "rtx3080"), ("rtx3070ti", "rtx3070"),
    ("rtx3060ti", "rtx3060"), ("rtx4070ti", "rtx4070"), ("rx6800xt", "rx6800"), ("airpodspro2", "airpodspro1"),
    ("wh1000xm5", "wh1000xm4"), ("aw_s8", "aw_s7"), ("fenix7", "fenix6pro"), ("dyson_v15", "dyson_v11"),
    ("quest3", "quest2"), ("djimini3", "djimini2se"),
)
EBAY_QUERIES: tuple[str, ...] = ("iphone", "rtx", "ps5", "switch", "macbook", "airpods")

# ---------------------------------------------------------------------------
# Wording
# ---------------------------------------------------------------------------

BERLIN = (
    ("10115", "Mitte"), ("10245", "Friedrichshain"), ("10967", "Kreuzberg"), ("12043", "Neukölln"),
    ("10437", "Prenzlauer Berg"), ("13353", "Wedding"), ("12163", "Steglitz"), ("10585", "Charlottenburg"),
    ("12555", "Köpenick"), ("13597", "Spandau"), ("10827", "Schöneberg"), ("13086", "Weißensee"),
    ("12489", "Adlershof"), ("14163", "Zehlendorf"), ("13407", "Reinickendorf"), ("10317", "Lichtenberg"),
    ("12681", "Marzahn"), ("12099", "Tempelhof"), ("13187", "Pankow"), ("14482", "Potsdam"),
)
GERMANY = (
    ("80331", "München"), ("20095", "Hamburg"), ("50667", "Köln"), ("60311", "Frankfurt am Main"),
    ("04109", "Leipzig"), ("01067", "Dresden"), ("70173", "Stuttgart"), ("90402", "Nürnberg"), ("28195", "Bremen"),
    ("30159", "Hannover"), ("44135", "Dortmund"), ("24103", "Kiel"),
)
_PRE = ("", "", "", "", "", "Verkaufe ", "Biete ", "✅ ", "🔥 ")
_POST = ("", "", "", "", " Top Zustand", " wie neu", " OVP", " VB", " !!!", " – TOP", " neuwertig", " mit Rechnung",
         " 🔥", " Zustand sehr gut", " 💯")
_POST_CAT = {
    "smartphone": (" inkl. Hülle", " Akku 90%", " 📱"), "gpu": (" kein Mining",), "console": (" + Controller", " 🎮"),
    "tools": (" im Koffer",), "laptop": (" + Ladekabel",), "tablet": (" + Hülle",),
}
_OPENERS = (
    "Verkaufe mein {name}.", "Ich verkaufe hier mein {model}, da ich umgestiegen bin.", "Biete {name} an.",
    "Hallo, ich verkaufe mein {model}, weil ich es kaum nutze.", "Verkaufe {name} aus zweiter Hand.",
    "Zum Verkauf steht mein {model}.",
)
_CONDITION = (
    "Das Gerät ist in einem sehr guten Zustand.", "Leichte Gebrauchsspuren, siehe Bilder.",
    "Keine Kratzer, alles funktioniert einwandfrei.", "Zustand wie neu, kaum benutzt.",
    "Normale Gebrauchsspuren, technisch einwandfrei.", "Wurde immer pfleglich behandelt.",
)
_DETAILS: dict[str, tuple[str, ...]] = {
    "smartphone": ("Akkukapazität {pct} %.", "Immer mit Hülle und Panzerglas benutzt.", "{account}",
                   "Ohne Simlock, für alle Netze.", "Mit Ladekabel, ohne Netzteil.", "Dual-SIM, eSIM fähig."),
    "laptop": ("Akku hält ca. {hours} Stunden.", "Frisch aufgesetzt, Windows 11 aktiviert.",
               "Tastatur und Display ohne Mängel.", "Mit Original-Netzteil.", "Ladezyklen: {cycles}."),
    "console": ("Mit einem Controller und allen Kabeln.", "Lüfter leise, nie geöffnet.",
                "Läuft einwandfrei, Firmware aktuell.", "Nur wenig gespielt."),
    "gpu": ("Nie für Mining genutzt.", "Lief nur im Gaming-PC, nie übertaktet.",
            "Temperaturen unter Last um die 70 Grad.", "Originalverpackung vorhanden.", "Rechnung vorhanden."),
    "tablet": ("Immer mit Hülle benutzt.", "Akku hält lange.", "Displayschutzfolie seit Tag 1.", "WiFi-Modell."),
    "audio": ("Klang und ANC einwandfrei.", "Polster sauber.", "Mit Ladekabel und Tasche.", "Akku hält noch lange."),
    "camera": ("Auslösungen ca. {shots}.", "Sensor sauber.", "Mit Akku, Ladegerät und Gurt.",
               "Objektiv ohne Kratzer."),
    "smartwatch": ("Akku {pct} %.", "Mit Ladekabel und Originalarmband.", "Display ohne Kratzer.",
                   "Immer mit Schutzfolie getragen."),
    "tools": ("Mit Koffer.", "Wenig benutzt, nur für ein paar Regale.", "Alles funktioniert einwandfrei.",
              "Akkus halten gut."),
    "household": ("Regelmäßig gereinigt, Filter neu.", "Mit allen Aufsätzen.", "Funktioniert einwandfrei.",
                  "Wenig genutzt."),
    "escooter": ("Ca. {km} km gefahren.", "Reifen gut, Bremsen neu eingestellt.", "Mit Ladegerät.",
                 "Straßenzulassung (ABE) vorhanden."),
    "vr": ("Linsen ohne Kratzer.", "Mit beiden Controllern.", "Wenig genutzt.", "Auf Werkseinstellungen zurückgesetzt."),
    "drone": ("Nie abgestürzt.", "Mit 3 Akkus.", "Registriert, alle Updates drauf.", "Gimbal einwandfrei."),
}
_CLOSERS = (
    "Privatverkauf, keine Garantie, keine Rücknahme.", "Privatverkauf, daher keine Garantie und kein Umtausch.",
    "Abholung in Berlin-{district} oder Versand gegen Aufpreis.", "Nichtraucherhaushalt, tierfrei.",
    "Bei Interesse einfach melden.", "Nur Abholung.", "Versand möglich, PayPal Waren & Dienstleistungen.",
    "Rechnung vorhanden.", "Preis ist VB, keine unrealistischen Angebote.",
)
# The standard private-sale disclaimer: legal boilerplate, NOT a defect report.
DISCLAIMERS = (
    "Privatverkauf, keine Garantie oder Rücknahme bei Defekten.",
    "Da Privatverkauf: keine Gewährleistung, keine Rücknahme, auch nicht bei Defekten.",
    "Privatverkauf unter Ausschluss jeglicher Gewährleistung – keine Haftung für Defekte.",
    "Keine Garantie, keine Rücknahme. Für eventuelle Defekte wird nicht gehaftet.",
    "Privatverkauf. Keine Garantie, kein Umtausch, keine Rücknahme bei späteren Defekten.",
)
# Legit ads full of negations: a naive red-flag matcher kills these real deals.
_NEGATED = (
    "Keine Defekte, kein Wasserschaden.", "Display ohne Risse, nie gebrochen.", "Kein Tausch, nur Verkauf.",
    "Keine Vorkasse, nur Abholung oder PayPal.", "Funktioniert einwandfrei, nichts kaputt.",
)
_NEGATED_CAT = {
    "gpu": ("Nie für Mining genutzt, keine Bildfehler.",),
    "smartphone": ("Nicht gesperrt, iCloud ist abgemeldet.",),
}
_GPU_BOARDS_NV = ("MSI", "ASUS", "Gigabyte", "Zotac", "Palit", "EVGA", "Gainward", "PNY", "NVIDIA")
_GPU_LINES_NV = ("Gaming X Trio", "TUF Gaming OC", "Eagle OC", "Founders Edition", "Ventus 3X", "Twin Edge",
                 "GamingPro", "FTW3 Ultra", "Gaming OC")
_GPU_BOARDS_AMD = ("Sapphire", "PowerColor", "XFX", "ASRock", "AMD")
_GPU_LINES_AMD = ("Nitro+", "Red Devil", "Pulse", "MERC 319", "Reference", "Phantom Gaming")

CLARITIES = ("plain", "paraphrase", "subtle")


@dataclass(frozen=True)
class TrapText:
    title: str  # template: {name} {model} {brand} {noun} {other} {digits}
    text: str  # sentence planted in the description
    clarity: str  # plain = standard wording; paraphrase = natural rewording / typo; subtle = only implied
    value: float  # true value as a share of the product market (absolute EUR if `absolute`)
    price: tuple[float, float]  # asking price range, same unit as `value`
    cats: frozenset[str] | None = None
    brands: frozenset[str] | None = None
    pids: frozenset[str] | None = None
    absolute: bool = False
    attrs: tuple[tuple[str, str], ...] = ()


def _t(title: str, text: str, clarity: str, value: float, price: tuple[float, float], *, cats: str = "",
       brands: str = "", pids: str = "", absolute: bool = False, attrs: dict[str, str] | None = None) -> TrapText:
    assert clarity in CLARITIES, clarity
    return TrapText(title, text, clarity, value, price,
                    frozenset(cats.split()) or None, frozenset(brands.split()) or None,
                    frozenset(pids.split()) or None, absolute, tuple((attrs or {}).items()))


_PHONEY = "smartphone tablet smartwatch"
P, R, S = "plain", "paraphrase", "subtle"
TRAP_TEXTS: dict[str, tuple[TrapText, ...]] = {
    "defect": (
        _t("{name} defekt", "Das Gerät ist defekt und geht nicht mehr an, deshalb für Bastler.", P, 0.25, (0.15, 0.4)),
        _t("{name} – Display gebrochen", "Display ist gebrochen, Touch funktioniert nur teilweise.", P, 0.35,
           (0.2, 0.45), cats=_PHONEY + " laptop"),
        _t("{name} für Bastler", "Startet nicht mehr, als Ersatzteilspender.", P, 0.2, (0.1, 0.35)),
        _t("{name}", "Leider Wasserschaden, schaltet sich nicht mehr ein.", P, 0.2, (0.15, 0.4),
           cats=_PHONEY + " audio laptop"),
        _t("{name}", "Siehe Bilder, Preis ist fest.", P, 0.3, (0.2, 0.45), attrs={"Zustand": "Defekt"}),
        _t("{name} defeckt", "Ist leider defeckt, geht nicht an.", R, 0.25, (0.15, 0.4)),
        _t("{name} kaputtes Display", "Das Display hat einen Sprung quer über den ganzen Bildschirm.", R, 0.35,
           (0.2, 0.45), cats=_PHONEY),
        _t("{name}", "Ist mir runtergefallen, seitdem bleibt der Bildschirm schwarz.", R, 0.25, (0.15, 0.4),
           cats=_PHONEY + " laptop console"),
        _t("{name}", "Lädt nicht mehr, Ladebuchse vermutlich hinüber.", R, 0.35, (0.2, 0.45),
           cats=_PHONEY + " audio escooter drone"),
        _t("{name}", "Hatte Wasserkontakt, geht seitdem nicht mehr.", R, 0.2, (0.15, 0.4), cats=_PHONEY + " audio"),
        _t("{name} Bastlerware", "Als Bastlerware, ohne Gewähr auf Funktion.", R, 0.25, (0.15, 0.4)),
        _t("{name}", "Karte gibt kein Signal mehr aus, Lüfter drehen aber.", R, 0.25, (0.15, 0.4), cats="gpu"),
        _t("{name}", "Schaltet sich nach ein paar Minuten ab, wird zu heiß.", R, 0.3, (0.2, 0.45),
           cats="console laptop gpu"),
        _t("{name}", "Akku ist leicht aufgebläht, sonst alles gut.", S, 0.35, (0.3, 0.5),
           cats="smartphone laptop tablet"),
        _t("{name}", "Face ID geht nicht, ansonsten top.", S, 0.5, (0.35, 0.55), cats="smartphone", brands="Apple"),
        _t("{name}", "Unter Last gibt es manchmal Artefakte und Bildfehler.", S, 0.3, (0.3, 0.5), cats="gpu"),
        _t("{name}", "Geht ab und zu einfach aus, vermutlich der Akku.", S, 0.4, (0.3, 0.5),
           cats="smartphone escooter tools household drone"),
        _t("{name}", "Grüne Linie im Display, stört aber kaum.", S, 0.45, (0.35, 0.55), cats="smartphone tablet"),
        _t("{name}", "Touch reagiert an manchen Stellen nicht mehr.", S, 0.35, (0.3, 0.5), cats=_PHONEY),
        _t("{name}", "Lüfter schleift laut, müsste man mal machen lassen.", S, 0.5, (0.35, 0.5),
           cats="gpu console laptop"),
        _t("{name}", "Linker Ohrhörer ist sehr leise, rechter geht normal.", S, 0.35, (0.3, 0.45), cats="audio"),
        _t("{name}", "Gimbal-Fehler nach Absturz, fliegt aber noch.", S, 0.35, (0.3, 0.5), cats="drone"),
        _t("{name}", "Autofokus spinnt manchmal, Bilder oft unscharf.", S, 0.45, (0.35, 0.5), cats="camera"),
    ),
    "box_only": (
        _t("{name} nur OVP", "Verkauft wird nur die Originalverpackung, ohne Inhalt!", P, 8, (5, 25), absolute=True),
        _t("Leerkarton {name}", "Leerer Originalkarton, kein Gerät enthalten.", P, 8, (5, 20), absolute=True),
        _t("{name} Verpackung", "Nur der Karton mit Anleitung, ohne Gerät.", R, 8, (5, 20), absolute=True),
        _t("{name} Schachtel", "Es geht NUR um die leere Schachtel!", R, 8, (5, 20), absolute=True),
        _t("{name} OVP leer", "Originalverpackung, leer, top erhalten.", R, 8, (5, 20), absolute=True),
        _t("Originalkarton {name}", "Originalkarton in top Zustand mit Anleitung und Sticker.", S, 8, (10, 25),
           absolute=True),
        _t("{name} OVP Box", "Box in sehr gutem Zustand, ideal zum Weiterverkauf.", S, 8, (10, 30), absolute=True),
    ),
    "locked": (
        _t("{name} iCloud gesperrt", "iCloud gesperrt, Vorbesitzer nicht erreichbar, deshalb so günstig.", P, 0.15,
           (0.15, 0.35), cats=_PHONEY + " laptop", brands="Apple"),
        _t("{name}", "Hat eine Aktivierungssperre, Passwort vergessen.", P, 0.15, (0.2, 0.4),
           cats=_PHONEY + " laptop", brands="Apple"),
        _t("{name}", "Google-Konto gesperrt (FRP), lässt sich nicht einrichten.", P, 0.2, (0.2, 0.4),
           cats="smartphone tablet", brands="Samsung Google"),
        _t("{name} icloud gespert", "Icloud gespert, kann man bestimmt entsperren lassen.", R, 0.15, (0.15, 0.35),
           cats=_PHONEY, brands="Apple"),
        _t("{name}", "Hängt in der Aktivierung fest, Apple-ID unbekannt.", R, 0.15, (0.2, 0.4),
           cats=_PHONEY + " laptop", brands="Apple"),
        _t("{name}", "Ist mit einem Code gesichert, den ich leider nicht mehr weiß.", R, 0.2, (0.2, 0.4),
           cats="smartphone tablet"),
        _t("{name}", "Apple-ID vom Vorbesitzer ist noch angemeldet, sonst top.", S, 0.15, (0.25, 0.45),
           cats=_PHONEY + " laptop", brands="Apple"),
        _t("{name}", "Ist noch mit dem Konto meines Bruders verbunden, er meldet sich aber nicht.", S, 0.15,
           (0.25, 0.45), cats=_PHONEY),
    ),
    "wanted": (
        _t("Suche {model}", "Suche {model} in gutem Zustand, zahle bar bei Abholung.", P, 0, (0.5, 0.75)),
        _t("{model} gesucht", "Ich suche ein {model}, gerne mit OVP.", P, 0, (0.5, 0.75)),
        _t("Kaufe {model} – faire Preise", "Ankauf von {noun}s aller Art, sofortige Barzahlung.", P, 0, (0.4, 0.6)),
        _t("SUCHE {model} !!!", "Hallo, ich brauche ein {model}, bitte melden.", R, 0, (0.5, 0.75)),
        _t("Ankauf {model}", "Wir kaufen dein {model} – sofort Bargeld.", R, 0, (0.4, 0.6)),
        _t("Wer verkauft {model}?", "Bin auf der Suche nach {model}, bitte alles anbieten.", S, 0, (0.5, 0.7)),
        _t("{model} – zahle gut", "Brauche dringend ein {model}, melde dich!", S, 0, (0.5, 0.7)),
    ),
    "swap": (
        _t("{name} – nur Tausch", "Nur Tausch gegen {other}, kein Verkauf!", P, 0, (0.5, 0.8)),
        _t("Tausche {name} gegen {other}", "Tausche gegen {other}, gerne mit Zuzahlung.", P, 0, (0.5, 0.8)),
        _t("{name} Tausch", "Nur gegen {other}, Verkauf nicht gewünscht.", R, 0, (0.5, 0.8)),
        _t("{name} tausch gg. {other}", "Tausch gg. {other}, kein Geld.", R, 0, (0.5, 0.8)),
        _t("{name}", "Hätte lieber eine {other} dafür, Geld interessiert mich eher nicht.", S, 0, (0.5, 0.7)),
    ),
    "scam": (
        _t("{name}", "Zahlung nur per Vorkasse (Überweisung), dann Versand mit DHL.", P, 0, (0.35, 0.6)),
        _t("{name}", "Bin zurzeit beruflich im Ausland, Versand nach Zahlung per Western Union.", P, 0, (0.35, 0.6)),
        _t("{name}", "Kontakt bitte nur per WhatsApp: 0157 {digits}.", P, 0, (0.35, 0.6)),
        _t("{name}", "Bezahlung nur über PayPal Freunde und Familie.", P, 0, (0.35, 0.6)),
        _t("{name}", "Bezahlung vorab per Überweisung, danach verschicke ich sofort.", R, 0, (0.35, 0.6)),
        _t("{name}", "Ich bin gerade auf Montage in Polen, Versand über DHL.", R, 0, (0.35, 0.6)),
        _t("{name}", "Bitte nur über WhatsApp melden, hier bin ich selten: 0176 {digits}.", R, 0, (0.35, 0.6)),
        _t("{name}", "Zahlung per PayPal an Freunde, sonst zu viele Gebühren.", R, 0, (0.35, 0.6)),
        _t("{name}", "Vorrauskasse, Versand versichert.", R, 0, (0.35, 0.6)),
        _t("{name}", "Schreib mir gern direkt auf WhatsApp, hier bin ich selten online.", S, 0, (0.35, 0.6)),
        _t("{name} NEU", "Neu und unbenutzt, Versand erfolgt direkt nach Zahlungseingang.", S, 0, (0.35, 0.6)),
    ),
    "scam_cheap": (  # far too cheap + shipping only / pay first: must be "skip"
        _t("{name}", "Nur Versand, Zahlung per Vorkasse.", P, 0, (0.15, 0.35)),
        _t("{name} günstig", "Versand nur nach Überweisung, Abholung leider nicht möglich (wohne in Spanien).", R, 0,
           (0.15, 0.35)),
        _t("{name}", "Nur Versand! Bezahlung vorab, dann geht das Paket raus.", R, 0, (0.15, 0.35)),
        _t("{name} wie neu", "Wegen Umzug günstig abzugeben, nur Versand per DHL.", S, 0, (0.15, 0.35)),
        _t("{name}", "Nur Versand, keine Abholung möglich. Zahlung per Überweisung.", S, 0, (0.15, 0.35)),
    ),
    "fake": (
        _t("{name} Replika", "1:1 Kopie in Top Qualität, kaum vom Original zu unterscheiden.", P, 0.15, (0.2, 0.35),
           cats="audio smartwatch"),
        _t("{name}", "Es handelt sich um einen Nachbau, funktioniert aber super.", P, 0.15, (0.2, 0.35),
           cats="audio smartwatch"),
        _t("{name} (nicht original)", "Nicht original, China-Version, klingt aber gut.", R, 0.15, (0.2, 0.35),
           cats="audio smartwatch"),
        _t("{name} 1zu1", "1zu1 wie das Original, keiner merkt den Unterschied.", R, 0.15, (0.2, 0.35),
           cats="audio smartwatch"),
        _t("{name}", "Kein Original, aber gleiche Funktionen und Klang.", S, 0.15, (0.2, 0.35),
           cats="audio smartwatch"),
        _t("{name} NEU versiegelt", "Neu, versiegelt, aus Restposten. Mehrere Stück verfügbar.", S, 0.1,
           (0.2, 0.3), cats="audio smartwatch"),
    ),
    "rent": (
        _t("{name} zu vermieten", "Vermiete meine {model} tageweise, Preis pro Tag.", P, 0, (10, 30), absolute=True),
        _t("{name} Mietkauf", "Mietkauf möglich, Preis ist die monatliche Rate.", P, 0, (0.08, 0.15)),
        _t("{name} leihen", "Verleihe meine {model} fürs Wochenende, Preis pro Tag.", R, 0, (10, 30),
           absolute=True),
    ),
    "too_good": (
        _t("{name} NEU OVP versiegelt", "Brandneu und originalverpackt. Bilder vom Hersteller. Versand nach Zahlung.",
           S, 0, (0.15, 0.35)),
        _t("{name} neu", "Nie benutzt, Geschenk bekommen. Rechnung vorhanden. Nur Versand.", S, 0, (0.15, 0.35)),
        _t("{name} – wie neu", "Muss dringend weg. Foto aus dem Internet, Gerät ist aber identisch.", S, 0,
           (0.15, 0.35)),
    ),
    "partial": (
        _t("{name} solo", "Nur das Gerät, ohne Akku und ohne Ladegerät.", P, 0.45, (0.4, 0.55), cats="tools"),
        _t("{name}", "Verkaufe nur das Grundgerät, Akkus und Lader behalte ich.", R, 0.45, (0.4, 0.55),
           cats="tools"),
        _t("{name} – nur Ladecase", "Nur das Ladecase, ohne Ohrhörer.", P, 0.25, (0.25, 0.4),
           pids="airpodspro2 airpodspro1"),
        _t("{name}", "Nur der linke Ohrhörer, den rechten habe ich verloren.", S, 0.3, (0.25, 0.4),
           pids="airpodspro2 airpodspro1"),
        _t("{name} nur Tablet", "Nur das Tablet, ohne Dock und ohne Joy-Cons.", P, 0.55, (0.45, 0.55),
           pids="switch_oled switch_v2"),
        _t("{name} ohne SSD", "Ohne Festplatte und ohne Netzteil, Akku hält kaum noch.", P, 0.55, (0.45, 0.55),
           cats="laptop"),
        _t("{name} ohne Fernbedienung", "Nur die Drohne, Fernbedienung und Akkus fehlen.", P, 0.45, (0.4, 0.5),
           cats="drone"),
        _t("{name}", "Nur das Gehäuse, das Objektiv ist nicht dabei.", S, 0.65, (0.45, 0.55), pids="a6000 eos250d"),
        _t("{name}", "Ohne Ladegerät, Akku schafft nur noch 5 km.", R, 0.5, (0.4, 0.5), cats="escooter"),
        _t("{name}", "Nur die Brille, Controller sind leider kaputt gegangen und entsorgt.", S, 0.5, (0.4, 0.5),
           cats="vr"),
        _t("{name}", "Nur der Sauger ohne Akku, Akku ist hinüber.", R, 0.45, (0.35, 0.5),
           pids="dyson_v11 dyson_v15"),
    ),
}
# (title, description, price range EUR, collision class) — ads that mention the product
ACCESSORIES: dict[str, tuple[tuple[str, str, tuple[float, float], str], ...]] = {
    "smartphone": (("Hülle für {model}", "Original Silikon Case, kaum benutzt.", (8, 25), "accessory"),
                   ("{model} Case + Panzerglas", "Case und 2x Panzerglas, neu.", (8, 20), "accessory"),
                   ("Ladegerät 20W für {model}", "Original Netzteil mit Kabel.", (10, 20), "accessory"),
                   ("{model} Display Original ausgebaut", "Ausgebautes Originaldisplay, funktioniert.", (50, 110),
                    "part")),
    "gpu": (("Wasserkühler für {model}", "EKWB Wasserkühler, passt auf Referenzdesign.", (40, 90), "part"),
            ("{model} Backplate", "Passive Backplate, neu.", (15, 35), "part"),
            ("Grafikkarten Halterung für {model}", "Stütze gegen GPU-Sag.", (10, 20), "part"),
            ("Kühler für {model} Founders Edition", "Originalkühler, ausgebaut.", (25, 60), "part")),
    "console": (("{model} Controller", "Original Controller, funktioniert.", (30, 50), "controller"),
                ("Ladestation für {model} Controller", "Für zwei Controller.", (12, 25), "controller"),
                ("Spiele für {model} (5 Stück)", "FIFA, GTA, Spider-Man und mehr.", (40, 80), "games")),
    "laptop": (("Netzteil für {model}", "Original Netzteil.", (15, 35), "accessory"),
               ("Tasche für {model}", "Laptoptasche, wie neu.", (10, 20), "accessory")),
    "tablet": (("Stift für {model}", "Eingabestift, kompatibel.", (20, 60), "accessory"),
               ("Tastatur-Case für {model}", "Mit Bluetooth-Tastatur.", (30, 60), "accessory")),
    "audio": (("Ersatz-Ohrpolster für {model}", "Neue Polster.", (10, 20), "accessory"),
              ("Tasche für {model}", "Transporttasche.", (8, 15), "accessory")),
    "camera": (("Akku für {model} (2 Stück)", "Zwei Ersatzakkus.", (15, 30), "accessory"),
               ("Kameratasche für {model}", "Passt perfekt.", (15, 30), "accessory")),
    "smartwatch": (("Armband für {model}", "Sportarmband, neu.", (8, 20), "accessory"),
                   ("Ladekabel {model}", "Magnetisches Ladekabel.", (8, 15), "accessory")),
    "tools": (("Akku 18V 5,0Ah für {brand} {model}", "Originalakku, wenig Zyklen.", (30, 60), "accessory"),
              ("Koffer für {model}", "Leerer Koffer mit Einlage.", (15, 30), "accessory")),
    "household": (("Akku für {model}", "Ersatzakku.", (30, 60), "accessory"),
                  ("Wandhalterung für {model}", "Ladestation Wandhalterung.", (10, 20), "accessory")),
    "escooter": (("Ladegerät für {model}", "Original Ladegerät.", (15, 30), "accessory"),
                 ("Ersatzreifen für {model}", "Zwei neue Reifen.", (15, 30), "accessory")),
    "vr": (("Elite Strap für {model}", "Bequemer Kopfgurt.", (20, 40), "accessory"),
           ("Controller für {model} links", "Linker Controller.", (30, 50), "controller")),
    "drone": (("Akku für {model}", "Intelligent Flight Battery.", (25, 50), "accessory"),
              ("Propeller Set für {model}", "8 Stück, neu.", (8, 15), "accessory")),
}
# Short family names that console accessories/games use ("PS5 Controller", "Switch Spiele").
_CONSOLE_FAMILY = {"ps5_disc": "PS5", "ps5_digital": "PS5", "switch_oled": "Switch", "switch_v2": "Switch",
                   "switch_lite": "Switch", "xbox_sx": "Xbox Series X", "xbox_ss": "Xbox Series S",
                   "steamdeck_lcd": "Steam Deck", "steamdeck_oled": "Steam Deck"}
# (title, description, price as multiple of the product market, extra EUR)
BUNDLES: dict[str, tuple[str, str, float, float]] = {
    "gpu": ("Gaming PC Ryzen 7 5800X + {model} 32GB RAM", "Kompletter Gaming-PC mit {model}, 1TB NVMe, 750W Netzteil.",
            1.3, 380),
    "console": ("{name} + 2 Controller + 5 Spiele", "Alles zusammen, nur komplett.", 1.35, 0),
    "laptop": ("{name} mit Monitor, Tastatur und Maus", "Komplettes Setup, nur zusammen.", 1.3, 0),
    "camera": ("{name} + 3 Objektive + Tasche", "Komplettes Set, nur zusammen.", 1.6, 0),
    "tools": ("{brand} Werkzeug Set: {model} + Flex + Stichsäge", "Alles mit Akkus, nur komplett.", 1.8, 0),
    "smartphone": ("{name} + Smartwatch Bundle", "Nur zusammen abzugeben.", 1.4, 0),
    "vr": ("{name} + Link Kabel + Elite Strap + 10 Spiele", "Komplettpaket.", 1.3, 0),
}
BUNDLE_DEFAULT = ("{name} mit viel Zubehör", "Mit allem Zubehör, nur komplett.", 1.25, 0.0)

TRAP_RU: dict[str, str] = {
    "defect": "дефект", "box_only": "только коробка", "locked": "блокировка аккаунта", "wanted": "поиск, не продажа",
    "swap": "только обмен", "scam": "мошенничество", "scam_cheap": "дёшево + только пересылка/предоплата",
    "fake": "подделка", "rent": "аренда", "too_good": "слишком дёшево (фейк)", "placeholder": "цена-заглушка 1 €",
    "accessory": "аксессуар", "bundle": "комплект / ПК", "partial": "некомплект", "variant": "дешёвая модификация",
    "auction": "аукцион eBay (ставка ≠ цена)",
}
CLARITY_RU = {"plain": "прямо", "paraphrase": "перефразировано/с опечаткой", "subtle": "намёком"}
AI_FLAG_RU: dict[str, str] = {
    "defect": "По описанию есть неисправность", "box_only": "Продаётся только коробка",
    "locked": "Устройство привязано к чужому аккаунту", "wanted": "Автор ищет товар, а не продаёт",
    "swap": "Продавец хочет только обмен", "scam": "Признаки мошенничества: предоплата / уход в мессенджер",
    "scam_cheap": "Слишком дёшево и только пересылка с предоплатой", "fake": "Похоже на копию, не оригинал",
    "rent": "Это аренда, а не продажа", "too_good": "Цена слишком хороша, фото из интернета",
    "partial": "Не хватает важных частей комплекта",
}
# Trap types a correct engine must never call "buy" when the text states them plainly.
SEVERE_TRAPS: tuple[str, ...] = ("defect", "box_only", "locked", "wanted", "swap", "scam", "scam_cheap", "fake", "rent",
                                 "placeholder")
# Planted mix of the Kleinanzeigen stream (kind, flavour, share); the rest are plain normal ads.
STREAM_MIX: tuple[tuple[str, str, float], ...] = (
    ("deal", "clean", 0.05), ("deal", "urgent", 0.015), ("deal", "vague", 0.02), ("deal", "glued", 0.015),
    ("deal", "negated", 0.02), ("deal", "variant", 0.015), ("deal", "price_drop", 0.015), ("deal", "disclaimer", 0.02),
    ("trap", "defect", 0.05), ("trap", "box_only", 0.015), ("trap", "locked", 0.015), ("trap", "wanted", 0.02),
    ("trap", "swap", 0.015), ("trap", "scam", 0.025), ("trap", "scam_cheap", 0.012), ("trap", "fake", 0.01),
    ("trap", "rent", 0.005), ("trap", "accessory", 0.03), ("trap", "bundle", 0.015), ("trap", "partial", 0.015),
    ("trap", "too_good", 0.01), ("trap", "placeholder", 0.01), ("trap", "variant", 0.02),
    ("normal", "shop", 0.03), ("normal", "overpriced", 0.03), ("normal", "no_price", 0.015),
    ("normal", "haggle", 0.03),
)
# eBay part of the stream: (kind, flavour, share of the eBay ads)
EBAY_MIX: tuple[tuple[str, str, float], ...] = (
    ("trap", "auction_soon", 0.35), ("trap", "auction_later", 0.15), ("deal", "ebay", 0.15),
)
# Collision classes of comparables (what a keyword search returns besides the product itself)
CLASS_RU: dict[str, str] = {
    "ok": "тот же товар", "storage": "другой объём памяти", "variant": "другая модификация (Pro/Max/mini/Ti/FE/+)",
    "other": "другой товар", "pc": "ПК с этой картой", "laptop": "ноутбук с этой картой", "part": "кулер/запчасть",
    "box": "пустая коробка", "wanted": "поиск", "defect": "неисправный", "bundle": "комплект",
    "accessory": "чехол/зарядка/аксессуар", "controller": "контроллер", "games": "игры", "unknown": "не опознан",
}
BAD_CLASSES = tuple(k for k in CLASS_RU if k not in ("ok", "unknown"))

# ---------------------------------------------------------------------------
# Synthetic market
# ---------------------------------------------------------------------------


@dataclass
class Truth:
    pid: str  # product the ad is about (for accessories: the product it fits)
    category: str
    kind: str  # "normal" | "deal" | "trap"
    flavour: str  # deal/normal flavour, or the trap type
    product_market: float  # market price of the referenced product (working, complete)
    value: float  # true resale value of exactly what is offered (0 = nothing to buy)
    lure: float  # what a fooled buyer/AI thinks it is worth
    clarity: str = "plain"  # how plainly the trap is stated (see CLARITIES)
    wording: str = ""
    klass: str = "ok"  # collision class when this ad shows up as someone else's comparable

    @property
    def trap(self) -> str:
        return self.flavour if self.kind == "trap" else ""


@dataclass
class SiteAd:
    ad_id: str
    title: str
    description: str
    category_id: int
    price: float | None
    negotiable: bool
    is_free: bool
    postal_code: str
    district: str
    images: int
    truth: Truth
    arrival: int = 0  # pass when the ad goes online (0 = already online: background)
    stream: bool = False  # Berlin/eBay ad in the monitored stream (scored) vs nationwide background
    source: str = "kleinanzeigen"
    attributes: dict[str, str] = field(default_factory=dict)
    seller_type: str = "private"
    seller_name: str = ""
    shipping: bool = True
    distance_km: float = 0.0
    posted: str = ""
    order: int = 0  # higher = newer
    drop_pass: int | None = None
    drop_price: float | None = None
    # eBay only
    buying_options: list[str] = field(default_factory=list)
    bid_count: int | None = None
    ends_in_hours: float | None = None
    shipping_cost: float | None = None
    feedback: tuple[float, int] | None = None
    _ends_at: Any = None

    @property
    def url(self) -> str:
        if self.source == "ebay":
            return f"https://www.ebay.de/itm/{self.ad_id.removeprefix('ebay-')}"
        slug = re.sub(r"[^a-z0-9]+", "-", normalize(self.title))[:50].strip("-") or "anzeige"
        return f"https://www.kleinanzeigen.de/s-anzeige/{slug}/{self.ad_id}-{self.category_id}-3331"

    @property
    def final_price(self) -> float | None:
        return self.drop_price if self.drop_pass is not None else self.price

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options and "FIXED_PRICE" not in self.buying_options

    def price_at(self, pass_no: int) -> float | None:
        if self.drop_pass is not None and pass_no >= self.drop_pass:
            return self.drop_price
        return self.price

    def price_text_at(self, pass_no: int) -> str:
        if self.is_free:
            return "Zu verschenken"
        price = self.price_at(pass_no)
        if price is None:
            return "VB"
        return f"{price:.0f} €" + (" VB" if self.negotiable else "")

    def image_urls(self) -> list[str]:
        host = "i.ebayimg.com/images/g" if self.source == "ebay" else "img.kleinanzeigen.de/api/v1/prod-ads/images"
        return [f"https://{host}/{self.ad_id[-6:]}{i:02d}/s-l500.jpg" for i in range(self.images)]

    def card(self, pass_no: int, search_name: str = "") -> Listing:
        """What a search-result card shows: title, price, place, a description snippet."""
        snippet = self.description if len(self.description) <= 150 else self.description[:150].rsplit(" ", 1)[0] + " …"
        extra: dict[str, Any] = {}
        if self.source == "ebay":
            if self._ends_at is None and self.ends_in_hours is not None:
                self._ends_at = utcnow() + timedelta(hours=self.ends_in_hours)
            extra = {"source": "ebay", "condition": "Gebraucht", "buying_options": list(self.buying_options),
                     "bid_count": self.bid_count, "ends_at": self._ends_at, "shipping_cost": self.shipping_cost,
                     "seller_feedback_percent": self.feedback[0] if self.feedback else None,
                     "seller_feedback_score": self.feedback[1] if self.feedback else None, "seller_type": "private"}
            snippet = ""
        return Listing(
            ad_id=self.ad_id, url=self.url, title=self.title, price=self.price_at(pass_no),
            price_text=self.price_text_at(pass_no), negotiable=self.negotiable, is_free=self.is_free,
            location=f"{self.postal_code} {self.district}", postal_code=self.postal_code,
            distance_km=self.distance_km, posted_at_text=self.posted, description=snippet,
            image_urls=self.image_urls()[:1], shipping_possible=self.shipping,
            tags=["Versand möglich"] if self.shipping else [], search_name=search_name, **extra,
        )

    def detail(self, listing: Listing, pass_no: int) -> Listing:
        """The ad page: full text, all photos, attributes, seller."""
        return listing.model_copy(update={
            "title": self.title, "price": self.price_at(pass_no), "price_text": self.price_text_at(pass_no),
            "negotiable": self.negotiable, "is_free": self.is_free, "description": self.description,
            "image_urls": self.image_urls(), "attributes": dict(self.attributes), "seller_name": self.seller_name,
            "seller_type": self.seller_type, "shipping_possible": self.shipping,
            "tags": ["Versand möglich"] if self.shipping else ["Nur Abholung"], "detail_loaded": True,
        })


@dataclass
class PoolItem:
    """eBay sold item or eBay fixed-price offer (comparables only)."""

    title: str
    price: float
    url: str
    pid: str
    klass: str = "ok"
    condition: str = "Gebraucht"


class _Index:
    """Word-prefix search like the sites': every query word must start a word of the ad
    ("3080" finds "3080" and "3080ti", not "30800"). Title hits rank before text-only hits."""

    def __init__(self, docs: Sequence[tuple[str, str]]):
        self._title: dict[str, set[int]] = defaultdict(set)
        self._all: dict[str, set[int]] = defaultdict(set)
        for i, (title, text) in enumerate(docs):
            for w in normalize(title).split():
                self._title[w].add(i)
                self._all[w].add(i)
            for w in normalize(text).split():
                self._all[w].add(i)
        self._title_vocab = sorted(self._title)
        self._all_vocab = sorted(self._all)

    @staticmethod
    def _prefix(q: str, post: dict[str, set[int]], vocab: list[str]) -> set[int]:
        out: set[int] = set()
        i = bisect.bisect_left(vocab, q)
        while i < len(vocab) and vocab[i].startswith(q):
            w = vocab[i]
            if not (q[-1].isdigit() and len(w) > len(q) and w[len(q)].isdigit()):
                out |= post[w]
            i += 1
        return out

    def search(self, query: str, *, title_only: bool = False) -> tuple[set[int], set[int]]:
        words = normalize(query).split()
        if not words:
            return set(), set()
        title_hits = set.intersection(*(self._prefix(w, self._title, self._title_vocab) for w in words))
        if title_only:
            return title_hits, set()
        all_hits = set.intersection(*(self._prefix(w, self._all, self._all_vocab) for w in words))
        return title_hits, all_hits - title_hits


@dataclass
class Market:
    seed: int
    passes: int
    stream: list[SiteAd]  # scored: new Kleinanzeigen Berlin ads + eBay ads (arrival >= 1)
    background: list[SiteAd]  # nationwide Kleinanzeigen ads (comparables only)
    sold: list[PoolItem]  # eBay sold
    offers: list[PoolItem]  # eBay fixed-price offers (Browse API comparables)
    warmup: list[SiteAd] = field(default_factory=list)  # Berlin ads online before monitoring starts
    by_id: dict[str, SiteAd] = field(default_factory=dict)
    by_url: dict[str, tuple[str, str]] = field(default_factory=dict)  # url -> (pid, collision class)
    site: list[SiteAd] = field(default_factory=list)
    ebay_stream: list[SiteAd] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.site = self.background + self.warmup + [a for a in self.stream if a.source == "kleinanzeigen"]
        self.ebay_stream = [a for a in self.stream if a.source == "ebay"]
        self.by_id = {a.ad_id: a for a in self.background + self.warmup + self.stream}
        self._site_index = _Index([(a.title, a.description) for a in self.site])
        self._ebay_index = _Index([(a.title, "") for a in self.ebay_stream])
        self._sold_index = _Index([(s.title, "") for s in self.sold])
        self._offers_index = _Index([(s.title, "") for s in self.offers])
        for a in self.background + self.warmup + self.stream:
            self.by_url[a.url] = (a.truth.pid, a.truth.klass)
        for s in self.sold + self.offers:
            self.by_url[s.url] = (s.pid, s.klass)

    def _rank(self, key: str, salt: str) -> float:
        return random.Random(f"{self.seed}:{salt}:{key}").random()

    def search_site(self, query: str, pass_no: int, *, title_only: bool = False) -> list[SiteAd]:
        """Online Kleinanzeigen ads matching the query, most relevant first."""
        title_hits, text_hits = self._site_index.search(query, title_only=title_only)
        out: list[SiteAd] = []
        for group in (title_hits, text_hits):
            ads = [self.site[i] for i in group if self.site[i].arrival <= pass_no]
            ads.sort(key=lambda a: self._rank(a.ad_id, normalize(query)))
            out += ads
        return out

    def search_ebay(self, query: str, pass_no: int) -> list[SiteAd]:
        hits, _ = self._ebay_index.search(query, title_only=True)
        return [self.ebay_stream[i] for i in sorted(hits) if self.ebay_stream[i].arrival <= pass_no]

    def search_sold(self, query: str) -> list[PoolItem]:
        hits, _ = self._sold_index.search(query, title_only=True)
        return sorted((self.sold[i] for i in hits), key=lambda s: self._rank(s.url, "sold"))

    def search_offers(self, query: str) -> list[PoolItem]:
        hits, _ = self._offers_index.search(query, title_only=True)
        return sorted((self.offers[i] for i in hits), key=lambda s: self._rank(s.url, "offers"))

    def stats(self) -> dict[str, int]:
        return {"categories": len(CATEGORIES), "products": len(PRODUCTS), "stream": len(self.stream),
                "ebay_stream": len(self.ebay_stream), "warmup": len(self.warmup), "background": len(self.background),
                "sold": len(self.sold),
                "ebay_offers": len(self.offers)}


def _nice_price(rng: random.Random, value: float) -> float:
    if value < 20:
        return float(max(1, round(value)))
    step = 1 if value < 50 else 5 if value < 300 else 10
    price = round(value / step) * step
    if price >= 100 and rng.random() < 0.3:
        price -= 1  # "249 €"
    return float(price)


def _true_profit(value: float, cost: float, pricing: PricingConfig) -> tuple[float, float]:
    """Net profit and ROI when reselling at the TRUE market value, with the configured margins."""
    resale = value * (1 - pricing.safety_margin_percent / 100)
    fees = resale * (pricing.selling_fee_percent + pricing.payment_fee_percent) / 100
    profit = resale - fees - pricing.default_shipping_cost - cost
    return profit, profit / max(cost, 1.0)


def _deal_at(ad: SiteAd, price: float, pricing: PricingConfig) -> bool:
    profit, roi = _true_profit(ad.truth.value, price + (ad.shipping_cost or 0.0), pricing)
    return profit >= pricing.min_profit and roi >= pricing.min_roi


def is_true_deal(ad: SiteAd, pricing: PricingConfig) -> bool:
    """A real deal: not a trap, a real price, and the configured profit + ROI at the TRUE market."""
    price = ad.final_price
    if ad.truth.kind == "trap" or ad.is_free or price is None or price <= 1:
        return False
    return _deal_at(ad, price, pricing)


def is_haggle_deal(ad: SiteAd, pricing: PricingConfig) -> bool:
    """A VB ad that is no deal at the asking price but becomes one after a ~10 % haggle."""
    price = ad.final_price
    return (ad.truth.flavour == "haggle" and ad.negotiable and price is not None and price > 1
            and not _deal_at(ad, price, pricing) and _deal_at(ad, price * 0.9, pricing))


def _related(a: Product, b: Product) -> bool:
    if a.category != b.category:
        return False
    if (a.pid, b.pid) in VARIANT_PAIRS or (b.pid, a.pid) in VARIANT_PAIRS:
        return True
    na, nb = normalize(a.model), normalize(b.model)
    return na.startswith(nb) or nb.startswith(na)


def comp_class(listing_pid: str, comp_pid: str, comp_klass: str) -> str:
    """Collision class of a comparable relative to the product actually being sold."""
    if comp_klass != "ok":
        return comp_klass
    if comp_pid == listing_pid:
        return "ok"
    a, b = PRODUCT_BY_ID.get(listing_pid), PRODUCT_BY_ID.get(comp_pid)
    if a is None or b is None:
        return "unknown"
    if a.category == b.category and a.model == b.model:
        return "storage"
    return "variant" if _related(a, b) else "other"


class _MarketBuilder:
    def __init__(self, seed: int, n: int, passes: int | None, salt: str = ""):
        self.seed = seed
        self.n = max(1, int(n))
        self.passes = passes or max(3, min(10, math.ceil(self.n / 80)))
        self.rng = random.Random(f"ebeyparser-benchmark:{seed}{salt}")
        self.by_cat: dict[str, list[Product]] = defaultdict(list)
        for p in PRODUCTS:
            self.by_cat[p.category].append(p)
        self._stream_id = 2_950_000_000 + (seed % 50) * 1_000_000
        self._bg_id = 2_700_000_000 + (seed % 50) * 1_000_000
        self._ebay_id = 356_000_000_000 + (seed % 50) * 1_000_000
        self._pool_id = 357_000_000_000 + (seed % 50) * 1_000_000
        # per-product systematic bias: eBay sold vs. the true market, typical asking markup
        self.bias: dict[str, tuple[float, float]] = {}
        for p in PRODUCTS:
            b = random.Random(f"{seed}:bias:{p.pid}")
            self.bias[p.pid] = (b.uniform(0.94, 1.07), b.uniform(1.0, 1.15))

    # ------------------------------------------------------------- helpers
    def _next_id(self, stream: bool) -> str:
        if stream:
            self._stream_id += self.rng.randint(3, 97)
            return str(self._stream_id)
        self._bg_id += self.rng.randint(3, 97)
        return str(self._bg_id)

    def _pick_product(self, ok: Callable[[Product], bool] | None = None) -> Product:
        cats = [c for c in CATEGORIES if any(ok is None or ok(p) for p in self.by_cat[c.key])]
        cat = self.rng.choices(cats, weights=[c.weight for c in cats])[0]
        return self.rng.choice([p for p in self.by_cat[cat.key] if ok is None or ok(p)])

    def _spec(self, spec: str) -> str:
        m = re.fullmatch(r"(\d+)\s?GB", spec)
        if m:
            return self.rng.choice((f"{m.group(1)}GB", f"{m.group(1)} GB", f"{m.group(1)}gb", f"{m.group(1)} Gb"))
        return spec

    def _core(self, p: Product, *, spec_rate: float = 0.8, color_rate: float = 0.3, glued: bool = False) -> str:
        """Brand + model (+ spec, colour) the way private sellers write it."""
        if p.category == "gpu":
            return self._gpu_title(p, glued=glued)
        rng = self.rng
        model = rng.choice(p.forms) if p.forms and rng.random() < 0.45 else p.model
        if glued:
            model = model.replace(" ", "", 2) if rng.random() < 0.5 else model.replace(" ", "", 1)
        parts = [p.brand] if p.brand and rng.random() < p.brand_rate else []
        parts.append(model)
        if p.spec and rng.random() < spec_rate and not re.search(r"\d\s?(?:GB|TB)\b", model):
            parts.append(self._spec(p.spec))
        if p.colors and rng.random() < color_rate:
            parts.append(rng.choice(p.colors))
        core = " ".join(parts)
        return core.upper() if rng.random() < 0.06 else core

    def _gpu_title(self, p: Product, *, glued: bool = False) -> str:
        rng = self.rng
        amd = p.model.startswith("RX")
        board = rng.choice(_GPU_BOARDS_AMD if amd else _GPU_BOARDS_NV)
        line = rng.choice(_GPU_LINES_AMD if amd else _GPU_LINES_NV)
        model = p.model.replace(" ", "") if glued else p.model
        family = "" if rng.random() < 0.6 else ("Radeon " if amd else "GeForce ")
        mem = rng.choice((p.spec, p.spec.replace("GB", "G"), p.spec.replace("GB", " GB"), ""))
        parts = [board if rng.random() < 0.75 else "", f"{family}{model}", line if rng.random() < 0.6 else "", mem]
        return " ".join(x for x in parts if x)

    def _title(self, p: Product, **kw: Any) -> str:
        post = _POST + _POST_CAT.get(p.category, ())
        return (self.rng.choice(_PRE) + self._core(p, **kw) + self.rng.choice(post)).strip()

    def _fill(self, template: str, p: Product) -> str:
        other = self.rng.choice([q for q in PRODUCTS if q.pid != p.pid and q.premium])
        return template.format(
            name=" ".join(x for x in (p.brand, p.model) if x), model=p.model, brand=p.brand or "Marke",
            noun=CATEGORY_BY_KEY[p.category].noun, other=other.model, family=_CONSOLE_FAMILY.get(p.pid, p.model),
            digits=f"{self.rng.randint(1000000, 9999999)}",
        )

    def _description(self, p: Product, district: str, extra: Sequence[str] = (), early: Sequence[str] = (),
                     disclaimer: str = "") -> str:
        rng = self.rng
        name = " ".join(x for x in (p.brand, p.model, p.spec) if x)
        details = list(_DETAILS.get(p.category, ()))
        rng.shuffle(details)
        sentences = [*early, rng.choice(_OPENERS).format(name=name, model=p.model), rng.choice(_CONDITION)]
        sentences += details[: rng.randint(1, 3)]
        sentences += list(extra)
        closers = list(_CLOSERS)
        rng.shuffle(closers)
        sentences += closers[: rng.randint(1, 3)]
        if disclaimer:
            sentences.append(disclaimer)
        account = ("Kein iCloud-Lock, auf Werkseinstellungen zurückgesetzt." if p.brand == "Apple"
                   else "Google-Konto abgemeldet, auf Werkseinstellungen zurückgesetzt.")
        text = " ".join(sentences)
        return text.format(pct=rng.randint(82, 100), hours=rng.randint(3, 9), cycles=rng.randint(40, 600),
                           shots=rng.randint(3, 40) * 1000, km=rng.randint(300, 4000), account=account,
                           district=district)

    def _asking(self, market: float, lo: float = 0.85, hi: float = 1.25, mode: float = 1.02) -> float:
        return _nice_price(self.rng, market * self.rng.triangular(lo, hi, mode))

    def _ad(self, p: Product, title: str, desc: str, price: float | None, truth: Truth, *, stream: bool,
            **kw: Any) -> SiteAd:
        rng = self.rng
        plz, district = rng.choice(BERLIN if stream else GERMANY)
        ad = SiteAd(
            ad_id=self._next_id(stream), title=title, description=desc,
            category_id=CATEGORY_BY_KEY[p.category].category_id, price=price,
            negotiable=price is not None and rng.random() < 0.5, is_free=False, postal_code=plz, district=district,
            images=rng.randint(2, 7), truth=truth, stream=stream, seller_name=rng.choice(
                ("Max", "Anna", "Leon", "Mia", "Paul", "Lena", "Jonas", "Sofia", "Tim", "Marie", "Ali", "Olga")),
            shipping=rng.random() < 0.6, distance_km=round(rng.uniform(1, 25), 1),
            posted=f"Heute, {rng.randint(7, 23):02d}:{rng.randint(0, 59):02d}",
            attributes={"Zustand": rng.choice(("Sehr gut", "Gut", "In Ordnung", "Wie neu"))},
        )
        for key, value in kw.items():
            setattr(ad, key, value)
        return ad

    # -------------------------------------------------------------- stream
    def _normal(self, flavour: str) -> SiteAd:
        p = self._pick_product()
        rng = self.rng
        district = rng.choice(BERLIN)[1]
        truth = Truth(p.pid, p.category, "normal", flavour, p.price, p.price, p.price)
        if flavour == "shop":
            title = rng.choice(("Refurbished ", "")) + self._core(p) + rng.choice((" – 12 Monate Gewährleistung",
                                                                                   " vom Händler", ""))
            desc = self._description(p, district, extra=("Gewerblicher Verkäufer, Rechnung mit ausgewiesener "
                                                         "MwSt., 12 Monate Gewährleistung.",))
            return self._ad(p, title, desc, self._asking(p.price, 1.1, 1.4, 1.2), truth, stream=True,
                            seller_type="commercial", seller_name="Handy-Service Berlin GmbH")
        if flavour == "overpriced":
            return self._ad(p, self._title(p), self._description(p, district),
                            self._asking(p.price, 1.25, 1.7, 1.35), truth, stream=True)
        if flavour == "no_price":
            ad = self._ad(p, self._title(p), self._description(p, district), None, truth, stream=True)
            ad.negotiable = True
            return ad
        if flavour == "haggle":  # VB, ~10 % above a deal price
            price = _nice_price(rng, p.price * rng.uniform(0.73, 0.79))
            return self._ad(p, self._title(p) + ("" if rng.random() < 0.5 else " VB"), self._description(p, district),
                            price, truth, stream=True, negotiable=True)
        disclaimer = rng.choice(DISCLAIMERS) if rng.random() < 0.1 else ""
        return self._ad(p, self._title(p), self._description(p, district, disclaimer=disclaimer),
                        self._asking(p.price), truth, stream=True)

    def _deal(self, flavour: str) -> SiteAd:
        rng = self.rng
        if flavour == "variant":
            pricier, cheaper = rng.choice(VARIANT_PAIRS)
            p, c = PRODUCT_BY_ID[pricier], PRODUCT_BY_ID[cheaper]
            price = _nice_price(rng, min(c.price * rng.uniform(0.8, 0.95), p.price * rng.uniform(0.55, 0.68)))
        else:
            p = self._pick_product()
            price = _nice_price(rng, p.price * rng.uniform(0.4, 0.68))
        district = rng.choice(BERLIN)[1]
        truth = Truth(p.pid, p.category, "deal", flavour, p.price, p.price, p.price)
        extra: list[str] = []
        early: list[str] = []
        disclaimer = ""
        title = self._title(p)
        if flavour == "urgent":
            extra.append("Muss schnell weg wegen Umzug, daher der günstige Preis.")
            title = self._core(p) + rng.choice((" – muss schnell weg", " Notverkauf", " dringend"))
        elif flavour == "vague":
            noun = CATEGORY_BY_KEY[p.category].noun
            title = rng.choice((f"{noun} zu verkaufen", f"{p.brand or 'Marken'} {noun}", f"{noun} – Top Zustand",
                                f"Verkaufe mein {noun}"))
            early.append(f"Es handelt sich um ein {p.name}.")
        elif flavour == "glued":
            title = self._core(p, glued=True) + rng.choice(("", " VB", " Top"))
        elif flavour == "negated":
            options = list(_NEGATED) + list(_NEGATED_CAT.get(p.category, ()))
            extra += rng.sample(options, 2)
        elif flavour == "disclaimer":
            disclaimer = rng.choice(DISCLAIMERS)
            truth.wording = disclaimer
        ad = self._ad(p, title, self._description(p, district, extra=extra, early=early, disclaimer=disclaimer),
                      price, truth, stream=True)
        if flavour == "price_drop":  # starts at market level, drops later in the run
            ad.drop_price = price
            ad.price = self._asking(p.price, 0.95, 1.15, 1.02)
            ad.negotiable = True
        return ad

    def _trap(self, trap: str) -> SiteAd:
        rng = self.rng
        district = rng.choice(BERLIN)[1]
        if trap == "accessory":
            p = self._pick_product(lambda q: q.category in ACCESSORIES)
            title_t, text, (lo, hi), klass = rng.choice(ACCESSORIES[p.category])
            price = _nice_price(rng, rng.uniform(lo, hi))
            title = self._fill(title_t, p)
            truth = Truth(p.pid, p.category, "trap", trap, p.price, round(price * rng.uniform(1.0, 1.25), 2),
                          round(price * 1.2, 2), wording=title, klass=klass)
            return self._ad(p, title, text + " " + rng.choice(_CLOSERS).format(district=district), price, truth,
                            stream=True)
        if trap == "bundle":
            p = self._pick_product()
            title_t, text, factor, extra = BUNDLES.get(p.category, BUNDLE_DEFAULT)
            value = p.price * factor + extra
            price = _nice_price(rng, value * rng.uniform(0.9, 1.05))
            title = self._fill(title_t, p)
            klass = "pc" if p.category == "gpu" else "bundle"
            truth = Truth(p.pid, p.category, "trap", trap, p.price, value, value, wording=title, klass=klass)
            return self._ad(p, title, self._fill(text, p) + " " + self._description(p, district), price, truth,
                            stream=True)
        if trap == "placeholder":
            p = self._pick_product()
            truth = Truth(p.pid, p.category, "trap", trap, p.price, p.price, p.price, wording="1 € VB")
            ad = self._ad(p, self._title(p), self._description(p, district), 1.0, truth, stream=True)
            ad.negotiable = True
            return ad
        if trap == "variant":
            pricier, cheaper = rng.choice(VARIANT_PAIRS)
            e, p = PRODUCT_BY_ID[pricier], PRODUCT_BY_ID[cheaper]
            price = e.price * rng.uniform(0.58, 0.7)
            if price < p.price * 0.8:
                price = p.price * rng.uniform(0.82, 0.95)
            truth = Truth(p.pid, p.category, "trap", trap, p.price, p.price, e.price, wording=f"≠ {e.name}")
            return self._ad(p, self._title(p, spec_rate=0.9), self._description(p, district), _nice_price(rng, price),
                            truth, stream=True)

        def fits(q: Product, t: TrapText) -> bool:
            return ((t.cats is None or q.category in t.cats) and (t.brands is None or q.brand in t.brands)
                    and (t.pids is None or q.pid in t.pids))

        texts = TRAP_TEXTS[trap]
        if trap in ("too_good", "scam_cheap"):
            p = self._pick_product(lambda q: q.premium)
        else:
            p = self._pick_product(lambda q: any(fits(q, t) for t in texts))
        options = [t for t in texts if fits(p, t)]
        by_clarity = {c: [t for t in options if t.clarity == c] for c in CLARITIES}
        weights = [0.45 if by_clarity["plain"] else 0, 0.3 if by_clarity["paraphrase"] else 0,
                   0.25 if by_clarity["subtle"] else 0]
        clarity = rng.choices(CLARITIES, weights=weights)[0]
        tt = rng.choice(by_clarity[clarity])
        title = self._fill(tt.title, p)
        if "{name}" in tt.title and rng.random() < 0.5:  # sellers' own spelling of the product
            title = self._fill(tt.title.replace("{name}", self._core(p, color_rate=0.1)), p)
        sentence = self._fill(tt.text, p)
        early = rng.random() < 0.5  # else only visible on the ad page (needs the detail fetch)
        desc = self._description(p, district, extra=() if early else (sentence,), early=(sentence,) if early else ())
        if tt.absolute:
            value, price = tt.value, _nice_price(rng, rng.uniform(*tt.price))
        else:
            value = p.price * tt.value
            price = _nice_price(rng, p.price * rng.uniform(*tt.price))
        klass = {"defect": "defect", "box_only": "box", "wanted": "wanted", "partial": "part"}.get(trap, "ok")
        truth = Truth(p.pid, p.category, "trap", trap, p.price, round(value, 2), p.price, tt.clarity, sentence,
                      klass=klass)
        ad = self._ad(p, title, desc, price, truth, stream=True)
        if tt.attrs:
            ad.attributes.update(dict(tt.attrs))
        if trap in ("too_good", "fake", "scam_cheap"):
            ad.images = 1
        if trap in ("scam_cheap", "too_good"):
            ad.shipping = True
        if trap == "defect" and rng.random() < 0.15:  # "Defekter Dyson zu verschenken"
            ad.is_free, ad.price, ad.negotiable = True, 0.0, False
        return ad

    def _ebay(self, kind: str, flavour: str) -> SiteAd:
        """eBay.de listing (Browse API): auctions with a low current bid, fixed-price offers."""
        rng = self.rng
        p = self._pick_product(lambda q: any(k in normalize(q.model) for k in EBAY_QUERIES))
        color = f" {rng.choice(p.colors)}" if p.colors and rng.random() < 0.4 else ""
        brand = p.brand or ("NVIDIA GeForce" if "RTX" in p.model else "AMD Radeon")
        title = f"{brand} {p.model} {p.spec}{color}".replace("  ", " ").strip() + rng.choice(
            ("", " - Gut", " - Sehr gut", " Top Zustand", " gebraucht"))
        self._ebay_id += rng.randint(101, 9999)
        ad_id = f"ebay-{self._ebay_id}"
        truth = Truth(p.pid, p.category, kind, flavour if kind != "trap" else "auction", p.price, p.price, p.price)
        ship = rng.choice((0.0, 4.99, 5.49, 6.99))
        options = ["FIXED_PRICE"] + (["BEST_OFFER"] if rng.random() < 0.3 else [])
        bids: int | None = None
        hours: float | None = None
        if flavour == "auction_soon":
            price, bids, hours = p.price * rng.uniform(0.15, 0.4), rng.randint(3, 15), rng.uniform(0.5, 2.5)
            options = ["AUCTION"]
            truth.wording = f"аукцион, ставка {price / p.price:.0%} рынка, до конца ~{hours:.1f} ч"
        elif flavour == "auction_later":
            price, bids, hours = p.price * rng.uniform(0.1, 0.35), rng.randint(0, 4), rng.uniform(30, 120)
            options = ["AUCTION"]
            truth.wording = f"аукцион, ставка {price / p.price:.0%} рынка, до конца ~{hours:.0f} ч"
        elif kind == "deal":
            price = p.price * rng.uniform(0.45, 0.65)
        else:
            price = p.price * rng.triangular(0.9, 1.3, 1.05)
        plz, district = rng.choice(BERLIN + GERMANY)
        return SiteAd(
            ad_id=ad_id, title=title, description=f"{p.name}. Gebraucht, voll funktionsfähig. Versand mit DHL.",
            category_id=CATEGORY_BY_KEY[p.category].category_id, price=round(price, 2), negotiable=False,
            is_free=False, postal_code=plz, district=district, images=rng.randint(3, 8), truth=truth, stream=True,
            source="ebay", shipping=True, buying_options=options, bid_count=bids, ends_in_hours=hours,
            shipping_cost=ship, feedback=(round(rng.uniform(98.5, 100), 1), rng.randint(15, 3000)),
            seller_name=f"user{rng.randint(1000, 99999)}",
        )

    def _plan(self, n: int, mix: Sequence[tuple[str, str, float]], rest: tuple[str, str]) -> list[tuple[str, str]]:
        plan: list[tuple[str, str]] = []
        for kind, flavour, share in mix:
            count = round(share * n)
            if n >= 100 or (n >= 10 and mix is EBAY_MIX):
                count = max(1, count)
            plan += [(kind, flavour)] * count
        plan = plan[:n]
        plan += [rest] * (n - len(plan))
        self.rng.shuffle(plan)
        return plan

    def _ka_ads(self, n: int) -> list[SiteAd]:
        ads: list[SiteAd] = []
        for kind, flavour in self._plan(n, STREAM_MIX, ("normal", "plain")):
            if kind == "deal":
                ads.append(self._deal(flavour))
            elif kind == "trap":
                ads.append(self._trap(flavour))
            else:
                ads.append(self._normal(flavour))
        return ads

    def build_warmup(self) -> list[SiteAd]:
        """Berlin ads already online when monitoring starts: the first (learning) pass sees
        them; they are not scored."""
        ads = self._ka_ads(max(20, self.n // 4))
        for i, ad in enumerate(ads):
            ad.arrival, ad.order = 0, -len(ads) + i
            ad.drop_price = ad.drop_pass = None
        return ads

    def build_stream(self) -> list[SiteAd]:
        n_ebay = round(self.n * EBAY_SHARE) if self.n >= 20 else 0
        ads = self._ka_ads(self.n - n_ebay)
        for kind, flavour in self._plan(n_ebay, EBAY_MIX, ("normal", "ebay")):
            ads.append(self._ebay(kind, flavour))
        order = list(range(len(ads)))
        self.rng.shuffle(order)
        ads = [ads[i] for i in order]
        for i, ad in enumerate(ads):
            ad.arrival = 1 + i * self.passes // len(ads)
            ad.order = i
            if ad.drop_price is not None:
                if self.passes < 2:
                    ad.price, ad.drop_price = ad.drop_price, None
                    continue
                ad.arrival = min(ad.arrival, self.passes - 1)
                ad.drop_pass = min(self.passes, ad.arrival + self.rng.choice((1, 2)))
        return ads

    # ----------------------------------------------------------- background
    def build_background(self) -> list[SiteAd]:
        """Nationwide Kleinanzeigen ads that only show up in comparables searches: honest asking
        prices plus what a real keyword search also returns — other variants, parts, PCs and
        laptops with the card, empty boxes, wanted ads, broken units, bundles, games."""
        rng = self.rng
        out: list[SiteAd] = []

        def add(p: Product, title: str, text: str, price: float, klass: str, kind: str = "trap") -> None:
            value = p.price if klass == "ok" else price
            truth = Truth(p.pid, p.category, kind, klass, p.price, value, value, klass=klass)
            out.append(self._ad(p, title, text, _nice_price(rng, price), truth, stream=False))

        for p in PRODUCTS:
            ask_mode = self.bias[p.pid][1]
            for _ in range(16):
                add(p, self._title(p), self._description(p, "Mitte"), p.price * rng.triangular(0.88, 1.4, ask_mode),
                    "ok", "normal")
            acc = ACCESSORIES.get(p.category, ())
            for title_t, text, (lo, hi), klass in rng.sample(acc, min(2, len(acc))):
                add(p, self._fill(title_t, p), text, rng.uniform(lo, hi), klass)
            add(p, self._fill(rng.choice(("{name} defekt", "{name} Display defekt", "{name} für Bastler")), p),
                "Defekt, für Bastler.", p.price * 0.3, "defect")
            title_t, text, factor, extra = BUNDLES.get(p.category, BUNDLE_DEFAULT)
            add(p, self._fill(title_t, p), self._fill(text, p), p.price * factor + extra,
                "pc" if p.category == "gpu" else "bundle")
            add(p, self._fill("Suche {model}", p), "Zahle bar.", p.price * 0.6, "wanted")
            add(p, self._fill(rng.choice(("{model} gesucht", "Kaufe {model}")), p), "Zahle fair, melde dich.",
                p.price * 0.6, "wanted")
            add(p, self._fill(rng.choice(("{name} OVP leer", "Leerkarton {name}", "{name} Originalkarton")), p),
                "Nur die Verpackung.", rng.uniform(5, 20), "box")
            if p.category == "gpu":  # computers that merely contain the card
                for cpu in ("i7 12700K", "Ryzen 5 5600X"):
                    add(p, f"Gaming PC {cpu} {p.model} 16GB", f"Gaming-PC mit {p.model}, {cpu}, 16GB RAM.",
                        p.price * 1.4 + rng.uniform(250, 500), "pc")
                add(p, f"Gaming Laptop 17 Zoll {p.model} i7 16GB", f"Notebook mit {p.model} Laptop GPU.",
                    p.price * 1.5 + rng.uniform(200, 400), "laptop")
            if p.pid in _CONSOLE_FAMILY:
                fam = _CONSOLE_FAMILY[p.pid]
                add(p, f"{fam} Controller", "Original Controller.", rng.uniform(30, 50), "controller")
                add(p, f"{fam} Spiele Sammlung", "Zelda, Mario Kart, FIFA …", rng.uniform(40, 90), "games")
        for i, ad in enumerate(out):
            ad.arrival, ad.order = 0, -len(out) + i
        return out

    def _pool(self, p: Product, title: str, price: float, klass: str = "ok", condition: str = "Gebraucht",
              ) -> PoolItem:
        self._pool_id += self.rng.randint(11, 999)
        return PoolItem(title, round(price, 2), f"https://www.ebay.de/itm/{self._pool_id}", p.pid, klass, condition)

    def _ebay_name(self, p: Product) -> str:
        brand = p.brand or ("NVIDIA GeForce" if "RTX" in p.model else "AMD Radeon")
        return " ".join(x for x in (brand, p.model, p.spec) if x)

    def build_sold(self) -> list[PoolItem]:
        """eBay.de sold listings: real sale prices around the true market, plus noise."""
        rng = self.rng
        out: list[PoolItem] = []
        for p in PRODUCTS:
            name = self._ebay_name(p)
            for _ in range(14):
                cond = rng.choice(("", " - Gut", " - Sehr gut", " - Hervorragend", " Refurbished", " gebraucht"))
                color = f" {rng.choice(p.colors)}" if p.colors and rng.random() < 0.4 else ""
                sold_bias = self.bias[p.pid][0]
                out.append(self._pool(p, f"{name}{color}{cond}",
                                      p.price * min(1.35, max(0.7, rng.gauss(sold_bias, 0.08)))))
            out.append(self._pool(p, f"{name} defekt", p.price * rng.uniform(0.25, 0.4), "defect"))
            out.append(self._pool(p, f"{name} für Bastler Display defekt", p.price * rng.uniform(0.2, 0.35), "defect"))
            out.append(self._pool(p, f"Hülle / Zubehör für {name}", rng.uniform(8, 25), "accessory"))
            out.append(self._pool(p, f"{name} + Zubehörpaket", p.price * rng.uniform(1.2, 1.4), "bundle"))
            out.append(self._pool(p, f"{name} OVP Originalverpackung ohne Gerät", rng.uniform(5, 20), "box"))
            if p.category == "gpu":
                out.append(self._pool(p, f"Gaming PC mit {p.model} und Ryzen 7", p.price * 1.4 + rng.uniform(300, 500),
                                      "pc"))
        return out

    def build_offers(self) -> list[PoolItem]:
        """eBay.de fixed-price offers (asking prices; shops ask more), for the Browse API."""
        rng = self.rng
        out: list[PoolItem] = []
        for p in PRODUCTS:
            name = self._ebay_name(p)
            for _ in range(8):
                out.append(self._pool(p, f"{name}{rng.choice(('', ' - Gut', ' Refurbished', ' TOP'))}",
                                      p.price * rng.triangular(0.95, 1.4, 1.12)))
            out.append(self._pool(p, f"{name} defekt", p.price * 0.3, "defect", "Als Ersatzteil / defekt"))
            out.append(self._pool(p, f"Schutzhülle für {name}", rng.uniform(8, 20), "accessory", "Neu"))
            if p.category == "gpu":
                out.append(self._pool(p, f"Gaming PC {p.model} i5 16GB", p.price * 1.5 + 300, "pc"))
        return out

    def build(self) -> Market:
        stream = self.build_stream()
        warmup = self.build_warmup()
        background = self.build_background()
        return Market(self.seed, self.passes, stream, background, self.build_sold(), self.build_offers(), warmup)


def build_market(seed: int = 1, n_listings: int = 600, passes: int | None = None) -> Market:
    """The deterministic synthetic world for `seed`: stream (scored), background, eBay pools."""
    return _MarketBuilder(seed, n_listings, passes).build()


# ---------------------------------------------------------------------------
# Fake sources and AI
# ---------------------------------------------------------------------------


class FakeKleinanzeigen:
    """Stands in for KleinanzeigenScraper. Searches behave like the site: category scans list the
    newest ads, keyword searches (and comparables) return ANY ad containing the query words —
    PCs, coolers, Ti variants included — so the engine's own relevance filtering is exercised."""

    def __init__(self, market: Market):
        self.market = market
        self.current_pass = 1
        self.calls: Counter[str] = Counter()
        self.comparable_queries: list[str] = []

    async def search(self, search: Any, max_pages: int = 1, **_: Any) -> list[Listing]:
        self.calls["search"] += 1
        p = self.current_pass
        query = (getattr(search, "query", "") or "").strip()
        if query:
            ads = [a for a in self.market.search_site(query, p) if a.stream]
        else:
            ads = [a for a in self.market.site if a.stream and a.arrival <= p]
        cat = getattr(search, "category_id", None)
        if cat is not None:
            ads = [a for a in ads if a.category_id == cat]
        lo, hi = getattr(search, "min_price", None), getattr(search, "max_price", None)
        if lo is not None or hi is not None:
            ads = [a for a in ads if (price := a.price_at(p)) is not None
                   and (lo is None or price >= lo) and (hi is None or price <= hi)]
        ads.sort(key=lambda a: (a.arrival, a.order), reverse=True)  # newest first
        ads = ads[: RESULTS_PER_PAGE * max(1, int(max_pages or 1))]
        self.calls["search_pages"] += max(1, math.ceil(len(ads) / RESULTS_PER_PAGE))
        return [a.card(p, getattr(search, "name", "")) for a in ads]

    async def fetch_detail(self, listing: Listing, **_: Any) -> Listing:
        self.calls["detail"] += 1
        ad = self.market.by_id.get(listing.ad_id)
        if ad is None:
            return listing.model_copy(update={"detail_loaded": True})
        return ad.detail(listing, self.current_pass)

    async def comparables(self, query: str, *, exclude_ad_id: str | None = None, limit: int = 30,
                          min_price: float | None = None, max_price: float | None = None,
                          **_: Any) -> list[Comparable]:
        self.calls["comparables"] += 1
        self.comparable_queries.append(query)
        out: list[Comparable] = []
        # the real scraper reads at most COMPARABLE_MAX_PAGES result pages
        for ad in self.market.search_site(query, self.current_pass)[: RESULTS_PER_PAGE * _COMPS_PAGES]:
            price = ad.price_at(self.current_pass)
            if ad.ad_id == exclude_ad_id or ad.is_free or price is None or price <= 0:
                continue
            if _SCRAPER_WANTED_RE.match(ad.title):
                continue
            if (min_price is not None and price < min_price) or (max_price is not None and price > max_price):
                continue
            out.append(Comparable(title=ad.title, price=price, url=ad.url, source="kleinanzeigen", sold=False,
                                  date_text=ad.posted))
            if len(out) >= limit:
                break
        return out

    async def download_images(self, listing: Listing, max_images: int = 3, **_: Any) -> list[bytes]:
        self.calls["image_requests"] += 1
        n = min(max(0, max_images), len(listing.image_urls))
        self.calls["images"] += n
        return [b"\xff\xd8\xff\xe0" + listing.ad_id.encode() + bytes([i]) for i in range(n)]

    async def aclose(self) -> None:
        return None


class FakeEbaySold:
    """Stands in for EbaySoldScraper. enabled=False mimics the live HTTP 403."""

    def __init__(self, market: Market, *, enabled: bool = True):
        self.market = market
        self.enabled = enabled
        self.calls = 0

    async def sold_comparables(self, query: str, limit: int = 30, **_: Any) -> list[Comparable]:
        self.calls += 1
        if not self.enabled:
            raise BlockedError("HTTP 403 от eBay (как на живом сайте)", status_code=403)
        return [Comparable(title=s.title, price=s.price, url=s.url, source="ebay_sold", sold=True,
                           date_text="Verkauft Sep 2026") for s in self.market.search_sold(query)[:limit]]

    async def aclose(self) -> None:
        return None


class FakeEbayAPI:
    """Stands in for EbayBrowseClient: eBay searches (auctions + fixed price) and fixed-price
    comparables (asking prices; items whose condition says defect are dropped, like the real one)."""

    def __init__(self, market: Market):
        self.market = market
        self.current_pass = 1
        self.calls: Counter[str] = Counter()

    async def search(self, search: Any, max_pages: int = 1, **_: Any) -> list[Listing]:
        self.calls["search"] += 1
        query = (getattr(search, "query", "") or "").strip()
        ads = self.market.search_ebay(query, self.current_pass) if query else []
        return [a.card(self.current_pass, getattr(search, "name", "")) for a in ads[: 50 * max(1, max_pages)]]

    async def fetch_detail(self, listing: Listing, **_: Any) -> Listing:
        self.calls["detail"] += 1
        ad = self.market.by_id.get(listing.ad_id)
        if ad is None:
            return listing.model_copy(update={"detail_loaded": True})
        return listing.model_copy(update={"description": ad.description, "image_urls": ad.image_urls(),
                                          "detail_loaded": True})

    async def comparables(self, query: str, *, exclude_ad_id: str | None = None, limit: int = 30,
                          min_price: float | None = None, max_price: float | None = None,
                          **_: Any) -> list[Comparable]:
        self.calls["comparables"] += 1
        out: list[Comparable] = []
        for item in self.market.search_offers(query):
            cond = item.condition.lower()
            if "defekt" in cond or "ersatzteil" in cond:
                continue
            if (min_price is not None and item.price < min_price) or (max_price is not None and item.price > max_price):
                continue
            out.append(Comparable(title=item.title, price=item.price, url=item.url, source="ebay", sold=False))
            if len(out) >= limit:
                break
        return out

    async def download_images(self, listing: Listing, max_images: int = 3, **_: Any) -> list[bytes]:
        self.calls["image_requests"] += 1
        n = min(max(0, max_images), len(listing.image_urls))
        return [b"\xff\xd8\xff\xe0" + listing.ad_id.encode() + bytes([i]) for i in range(n)]

    async def aclose(self) -> None:
        return None


def _ai_verdict(**kw: Any) -> AIVerdict:
    """AIVerdict with the structured fields the current model supports (older ones lack some)."""
    fields = getattr(AIVerdict, "model_fields", {})
    return AIVerdict(**{k: v for k, v in kw.items() if k in fields})


class OracleEvaluator:
    """Fake vision LLM that knows the truth: identifies the product, estimates its market
    price with noise and flags traps with a given recall. Deterministic per (seed, ad id)."""

    def __init__(self, market: Market, mode: str = "oracle", *, seed: int = 1, recall: float | None = None,
                 noise: float | None = None, false_alarm: float | None = None):
        preset = AI_PRESETS.get(mode, AI_PRESETS["oracle"])
        self.market = market
        self.mode = mode
        self.seed = seed
        self.recall = preset[0] if recall is None else recall
        self.noise = preset[1] if noise is None else noise
        self.false_alarm = preset[2] if false_alarm is None else false_alarm
        self.calls = 0
        self.called: list[str] = []

    async def evaluate(self, listing: Listing, images: list[bytes], *, purpose: str = "resale",
                       estimate: Any = None, target_price: float | None = None, **_: Any) -> AIVerdict:
        self.calls += 1
        self.called.append(listing.ad_id)
        model = f"oracle-{self.mode}"
        ad = self.market.by_id.get(listing.ad_id)
        if ad is None:
            return _ai_verdict(product=listing.title, search_query=normalize(listing.title), verdict="maybe",
                               confidence=0.3, reasoning="Не удалось опознать товар.", model=model)
        rng = random.Random(f"{self.seed}:{ad.ad_id}:{self.mode}:ai")
        t = ad.truth
        p = PRODUCT_BY_ID[t.pid]
        noise = 1 + rng.uniform(-self.noise, self.noise)
        caught = rng.random() < (max(self.recall, 0.97) if t.trap in ("accessory", "bundle") else self.recall)
        alarm = rng.random() < self.false_alarm
        conf = rng.uniform(0.75, 0.95)
        price = listing.price
        variant = {"model": p.model, **({"storage_gb": p.storage_gb} if p.storage_gb else {})}
        base: dict[str, Any] = {"model": model, "variant": variant, "item_type": "single", "locked": False,
                                "stock_photos": False, "defects": []}
        if t.kind == "trap" and caught and t.trap in AI_FLAG_RU:
            est = round(t.value * noise, 2) if t.value > 0 else None
            item_type = {"box_only": "box_only", "wanted": "wanted", "partial": "part"}.get(t.trap, "single")
            return _ai_verdict(**{
                **base, "product": f"{p.name} ({TRAP_RU[t.trap]})", "search_query": p.ai_query,
                "photo_matches_description": False if t.trap in ("too_good", "box_only", "fake") else True,
                "condition": "defective" if t.trap == "defect" else "unclear", "red_flags": [AI_FLAG_RU[t.trap]],
                "estimated_market_price": est, "verdict": "skip", "confidence": round(conf, 2),
                "reasoning": f"Ловушка: {TRAP_RU[t.trap]}. {AI_FLAG_RU[t.trap]}.", "item_type": item_type,
                "defects": [t.wording] if t.trap == "defect" else [], "locked": t.trap == "locked",
                "stock_photos": t.trap in ("too_good", "scam_cheap")})
        if t.kind == "trap" and caught and t.trap == "auction":
            return _ai_verdict(**{
                **base, "product": p.name, "search_query": p.ai_query, "photo_matches_description": True,
                "condition": "good", "estimated_market_price": round(p.price * noise, 2), "verdict": "maybe",
                "confidence": 0.6, "reasoning": "Аукцион: текущая ставка — не итоговая цена, она вырастет."})
        product, query, value = p.name, p.ai_query, t.value
        if t.kind == "trap" and caught and t.trap in ("accessory", "bundle"):
            product, query = ad.title, normalize(ad.title)
            base["item_type"] = ("complete_pc" if t.klass == "pc" else "bundle") if t.trap == "bundle" else (
                "part" if t.klass == "part" else "accessory")
        elif t.kind == "trap" and not caught:  # fooled: takes it for the lure product
            value = t.lure
            if t.trap == "variant":
                pricier = next((PRODUCT_BY_ID[a] for a, b in VARIANT_PAIRS if b == p.pid), p)
                product, query = pricier.name, pricier.ai_query
        est = round(value * noise, 2)
        if alarm and t.kind != "trap":
            return _ai_verdict(**{
                **base, "product": product, "search_query": query, "photo_matches_description": False,
                "condition": "unclear", "red_flags": ["Фото похожи на картинки из интернета"],
                "estimated_market_price": est, "verdict": "skip", "confidence": 0.65, "stock_photos": True,
                "reasoning": "Фото выглядят как стоковые, не уверен в продавце."})
        cost = (price or 0.0) + (listing.shipping_cost or 0.0)
        if ad.is_free:
            verdict, vconf = "buy", 0.8
        elif price is None or price <= 1:
            verdict, vconf = "maybe", 0.4
        else:
            ratio = cost / max(est, 1.0)
            if ratio <= 0.72:
                verdict, vconf = "buy", rng.uniform(0.7, 0.9)
            elif ratio <= 0.9:
                verdict, vconf = "maybe", rng.uniform(0.45, 0.65)
            else:
                verdict, vconf = "skip", rng.uniform(0.6, 0.8)
        return _ai_verdict(**{
            **base, "product": product, "search_query": query, "photo_matches_description": True,
            "condition": rng.choice(("good", "like_new", "used")), "red_flags": [], "estimated_market_price": est,
            "verdict": verdict, "confidence": round(vconf, 2),
            "reasoning": f"Похоже на {product}; рынок ~{est:.0f} €, цена {price if price is not None else '?'} €."})


class DownEvaluator:
    """The local model is unreachable: every answer is the "no real answer" verdict."""

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, listing: Listing, images: list[bytes], **_: Any) -> AIVerdict:
        self.calls += 1
        return _ai_verdict(verdict="maybe", confidence=0.0, model="down",
                           reasoning="ИИ-проверка не выполнена: модель недоступна (Connection refused)")


class CountingNotifier:
    name = "benchmark"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, deals: Sequence[Any], **_: Any) -> None:
        self.sent += [d.listing.ad_id for d in deals]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

KEYWORD_SEARCHES: tuple[dict[str, Any], ...] = (
    {"name": "iPhone", "query": "iphone", "category_id": 173, "exclude_keywords": ["hülle", "case", "panzerglas"]},
    {"name": "RTX", "query": "rtx", "category_id": 225},
    {"name": "PS5", "query": "ps5", "category_id": 279},
    {"name": "MacBook", "query": "macbook", "category_id": 278},
    {"name": "AirPods", "query": "airpods", "category_id": 172},
    {"name": "Steam Deck", "query": "steam deck", "category_id": 279},
)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def benchmark_config(*, category_scan: bool = True, ai_enabled: bool = True, ebay_api: bool = True,
                     overrides: dict[str, Any] | None = None) -> AppConfig:
    """Keyword searches a student would set up, eBay Browse API searches (auctions!) and
    (optionally) broad Berlin category scans. Everything else = the app defaults."""
    searches = [dict(s, location="Berlin", radius_km=30) for s in KEYWORD_SEARCHES]
    if ebay_api:
        searches += [{"name": f"eBay: {q}", "source": "ebay", "query": q} for q in EBAY_QUERIES]
    if category_scan:
        searches += [{"name": f"Категория: {c.name_ru}", "query": "", "category_id": c.category_id,
                      "category_name": c.name_ru, "location": "Berlin", "radius_km": 30} for c in CATEGORIES]
    data: dict[str, Any] = {
        "general": {"max_pages": 2, "fetch_details": True, "request_delay_seconds": 0},
        "searches": searches,
        "pricing": {"min_profit": 40, "min_roi": 0.25, "use_ebay_sold_comps": True},
        "ai": {"enabled": ai_enabled, "max_images": 3},
        "notifications": {"min_score": 70, "verdicts": ["buy"], "mode": "instant"},
    }
    return parse_config(_deep_merge(data, overrides or {}))


@contextmanager
def _quiet_logs(level: int = logging.CRITICAL) -> Iterator[None]:
    logger = logging.getLogger("ebeyparser")
    old = logger.level
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.setLevel(old)


@dataclass
class Mistake:
    kind: str  # trap_buy | false_buy | missed_deal | bad_estimate
    cause_key: str  # short root-cause bucket (Russian)
    cause: str  # specifics
    ad_id: str
    title: str
    category: str
    product: str
    planted: str  # e.g. "deal/clean", "ловушка/defect (намёком)"
    price: float | None
    true_value: float
    product_market: float
    estimate: float | None
    estimate_source: str
    estimate_query: str
    sample_size: int
    verdict: str
    action: str
    score: float | None
    expected_profit: float | None
    true_profit: float | None
    reasons: list[str]
    red_flags: list[str]
    ai: str
    comps: list[str]
    wording: str
    severity: float


@dataclass
class BenchmarkResult:
    seed: int
    n_listings: int
    n_scored: int
    ai_mode: str
    ebay_sold: bool
    ebay_api: bool
    category_scan: bool
    passes: int
    passes_run: int
    market: dict[str, int]
    verdicts: dict[str, int]
    true_deals: int
    buys: int
    correct_buys: int
    precision: float | None
    recall: float | None
    recall_buy_or_maybe: float | None
    notify_n: int
    notify_correct: int
    notify_traps: int
    notify_precision: float | None
    traps: int
    trap_buys: int
    trap_maybes: int
    severe_plain_trap_buys: int  # the invariant
    severe_explicit_trap_buys: int  # plain + paraphrased wording
    trap_by_type: dict[str, dict[str, int]]
    trap_by_clarity: dict[str, dict[str, int]]
    false_buys: int
    est_n: int
    est_coverage: float | None
    est_median_error: float | None
    est_over30_share: float | None
    est_by_category: dict[str, dict[str, Any]]
    est_by_source: dict[str, dict[str, Any]]
    categories: dict[str, dict[str, Any]]
    checks: dict[str, dict[str, Any]]
    collisions_probe: dict[str, dict[str, Any]]
    collisions_stored: dict[str, Any]
    calls: dict[str, int]
    per_100: dict[str, float]
    summary_totals: dict[str, float]
    errors: list[str]
    never_seen: int
    never_evaluated: int
    deferred: int
    notified: int
    notified_traps: int
    mistakes: list[Mistake]
    bad_estimates: list[Mistake]
    elapsed_s: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def invariants_ok(self) -> bool:
        return self.severe_plain_trap_buys == 0


async def run_benchmark_async(
    seed: int = 1,
    n_listings: int = 600,
    ai_mode: str = "oracle",
    ebay_sold: bool = True,
    category_scan: bool = True,
    *,
    ebay_api: bool = True,
    passes: int | None = None,
    drain_passes: int = 3,
    config_overrides: dict[str, Any] | None = None,
    ai_recall: float | None = None,
    ai_noise: float | None = None,
    probe: bool = True,
) -> BenchmarkResult:
    """Build the market, run Monitor.run_once() pass by pass (new ads arrive each pass; extra
    passes drain ads deferred by budgets) and score what the DB stored."""
    if ai_mode not in AI_MODES:
        raise ValueError(f"ai_mode must be one of {AI_MODES}, got {ai_mode!r}")
    started = time.perf_counter()
    market = build_market(seed, n_listings, passes)
    config = benchmark_config(category_scan=category_scan, ai_enabled=ai_mode != "off", ebay_api=ebay_api,
                              overrides=config_overrides)
    db = Database()
    ka = FakeKleinanzeigen(market)
    sold = FakeEbaySold(market, enabled=ebay_sold)
    api = FakeEbayAPI(market) if ebay_api else None
    ai: OracleEvaluator | DownEvaluator | None = None
    if ai_mode == "down":
        ai = DownEvaluator()
    elif ai_mode != "off":
        ai = OracleEvaluator(market, ai_mode, seed=seed, recall=ai_recall, noise=ai_noise)
    notifier = CountingNotifier()
    monitor = Monitor(config, db, scraper=ka, ebay=sold, ebay_api=api, evaluator=ai,  # type: ignore[arg-type]
                      notifiers=[notifier])
    scored = [a for a in market.stream if a.source == "kleinanzeigen" or ebay_api]

    seen_pass: dict[str, int] = {}
    eval_pass: dict[str, int] = {}
    summaries: list[Any] = []
    pass_no = -1
    with _quiet_logs():
        while True:  # pass 0 sees only the warm-up ads (the engine's learning pass)
            pass_no += 1
            if pass_no > market.passes:
                backlog = [a for a in seen_pass if a not in eval_pass]
                if not backlog or pass_no > market.passes + max(0, drain_passes):
                    pass_no -= 1
                    break
            ka.current_pass = pass_no
            if api is not None:
                api.current_pass = pass_no
            summaries.append(await monitor.run_once())
            for ad in scored:
                if ad.arrival > pass_no:
                    continue
                if ad.ad_id not in seen_pass and db.get_listing(ad.ad_id) is not None:
                    seen_pass[ad.ad_id] = pass_no
                if ad.ad_id not in eval_pass and db.get_evaluation(ad.ad_id) is not None:
                    eval_pass[ad.ad_id] = pass_no
        try:
            await monitor.aclose()
        except Exception:  # pragma: no cover - closing fakes must not fail the benchmark
            pass
        collisions_probe = _collision_probe(market, seed) if probe else {}
    calls = {
        "searches": ka.calls["search"] + (api.calls["search"] if api else 0),
        "search_pages": ka.calls["search_pages"],
        "comparables": ka.calls["comparables"], "ebay_sold": sold.calls,
        "ebay_api_comparables": api.calls["comparables"] if api else 0,
        "details": ka.calls["detail"] + (api.calls["detail"] if api else 0),
        "image_requests": ka.calls["image_requests"] + (api.calls["image_requests"] if api else 0),
        "ai": ai.calls if ai is not None else 0,
    }
    return _score(market, scored, db, config, calls, notifier, summaries, seen_pass, eval_pass, pass_no,
                  dict(seed=seed, n=n_listings, ai=ai_mode, ebay=ebay_sold, api=ebay_api, cats=category_scan),
                  collisions_probe, time.perf_counter() - started)


def run_benchmark(seed: int = 1, n_listings: int = 600, ai_mode: str = "oracle", ebay_sold: bool = True,
                  category_scan: bool = True, **kwargs: Any) -> BenchmarkResult:
    """Synchronous wrapper around run_benchmark_async (don't call from a running event loop)."""
    return asyncio.run(run_benchmark_async(seed, n_listings, ai_mode, ebay_sold, category_scan, **kwargs))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _share(a: int, b: int) -> float | None:
    return a / b if b else None


def _err_stats(errors: list[float]) -> dict[str, Any]:
    return {"n": len(errors), "median": median(errors) if errors else None,
            "over30": _share(sum(e > 0.3 for e in errors), len(errors))}


def _planted(ad: SiteAd) -> str:
    t = ad.truth
    if t.kind == "trap":
        return f"ловушка/{t.trap}" + ("" if t.clarity == "plain" else f" ({CLARITY_RU[t.clarity]})")
    return f"{t.kind}/{t.flavour}" + (" (eBay)" if ad.source == "ebay" else "")


def _make_mistake(ad: SiteAd, ev: Evaluation | None, kind: str, cause_key: str, cause: str, severity: float,
                  pricing: PricingConfig) -> Mistake:
    est = ev.estimate if ev is not None else None
    price = ad.final_price
    true_profit = None
    if price is not None:
        true_profit = round(_true_profit(ad.truth.value, price + (ad.shipping_cost or 0.0), pricing)[0], 2)
    ai = ""
    if ev is not None and ev.ai is not None:
        ai = f"{ev.ai.verdict} {ev.ai.confidence:.2f}" + (f" ~{ev.ai.estimated_market_price:.0f} €"
                                                          if ev.ai.estimated_market_price else "")
    comps_raw = list(est.comparables) if est is not None else []
    if est is not None and est.market_price and len(comps_raw) > 4:  # the ones around the median
        comps_raw = sorted(comps_raw, key=lambda c: abs(c.price - est.market_price))[:4]  # type: ignore[operator]
    comps = [f"{c.title} — {c.price:.0f} €{' (sold)' if c.sold else ''}" for c in comps_raw]
    return Mistake(
        kind=kind, cause_key=cause_key, cause=cause, ad_id=ad.ad_id, title=ad.title,
        category=CATEGORY_BY_KEY[ad.truth.category].name_ru, product=PRODUCT_BY_ID[ad.truth.pid].name,
        planted=_planted(ad), price=price, true_value=round(ad.truth.value, 2),
        product_market=ad.truth.product_market,
        estimate=est.market_price if est is not None else None, estimate_source=est.source if est else "",
        estimate_query=est.query if est else "", sample_size=est.sample_size if est else 0,
        verdict=ev.verdict if ev is not None else "—", action=str(getattr(ev, "action", "") or ""),
        score=ev.score if ev is not None else None,
        expected_profit=ev.expected_profit if ev is not None else None, true_profit=true_profit,
        reasons=list(ev.reasons[:5]) if ev is not None else [], red_flags=list(ev.red_flags) if ev else [],
        ai=ai, comps=comps, wording=ad.truth.wording, severity=round(severity, 2),
    )


def _est_txt(ev: Evaluation) -> str:
    e = ev.estimate
    if e.market_price is None:
        return f"нет оценки (запрос «{e.query}»)"
    return f"оценка {e.market_price:.0f} € [{e.source}, n={e.sample_size}, «{e.query}»]"


def _warn_key(text: str) -> str:
    """'⚠ Цены похожих сильно разбросаны (391 €–824 €) — рынок неясен' -> 'Цены похожих сильно разбросаны'."""
    t = text.lstrip("⚠").strip()
    t = re.split(r"[(:]", t, maxsplit=1)[0]
    t = re.sub(r"~?[\d.,]+\s*€?", "", t)
    return re.sub(r"\s+", " ", t).strip(" —-,;")[:60]


def _classify_missed(ad: SiteAd, ev: Evaluation | None) -> tuple[str, str]:
    if ev is None:
        return "не оценено", "объявление не дошло до оценки (не найдено поиском или отложено бюджетом)"
    severe = [f for f in ev.red_flags if f in SEVERE_FLAGS]
    if severe:
        return "ложный красный флаг", "сработали флаги: " + ", ".join(severe)
    prefilter = [r for r in ev.reasons if r.startswith(("Стоп-слова", "Нет ключевых слов", "Это объявление о поиске",
                                                        "Цена"))]
    est = ev.estimate.market_price
    if prefilter and est is None and ev.ai is None:
        return "префильтр", prefilter[0]
    if est is None:
        return "нет оценки рынка", _est_txt(ev)
    if est < 0.85 * ad.truth.value:
        return "рынок занижен", f"{_est_txt(ev)} при реальных {ad.truth.value:.0f} €"
    if ev.ai is not None and ev.ai.verdict == "skip" and ev.ai.confidence >= 0.6:
        return "вето ИИ", f"ИИ: skip {ev.ai.confidence:.2f} — {ev.ai.reasoning[:80]}"
    warn = next((r for r in ev.reasons if r.startswith("⚠")), None)
    if warn:
        return f"осторожный вердикт: {_warn_key(warn)}", f"{warn}; {_est_txt(ev)}"
    return "осторожный вердикт", (f"вердикт {ev.verdict}, балл {ev.score:.0f}, прибыль по движку "
                                  f"{ev.expected_profit if ev.expected_profit is not None else '—'} €; {_est_txt(ev)}")


def _collision_probe(market: Market, seed: int) -> dict[str, dict[str, Any]]:
    """Direct test of the engine's relevance filter: for every product, look up comparables
    the way the engine would (queries from typical titles + the AI query) in all three fake
    sources, then count per collision class how many the filter keeps."""
    relevant = getattr(_estimator, "relevant_comparables", None)
    make_query = getattr(_text, "make_search_query", None)
    if relevant is None or make_query is None:
        return {}
    estimate = getattr(_estimator, "estimate_from_comparables", None)
    builder = _MarketBuilder(seed, 1, 1, salt=":probe")
    stats: dict[str, dict[str, Any]] = {k: {"returned": 0, "kept": 0, "examples": []} for k in CLASS_RU}
    errors: list[tuple[float, str]] = []
    for p in PRODUCTS:
        queries = {make_query(builder._title(p)) for _ in range(3)} | {p.ai_query}
        for q in sorted(x for x in queries if x):
            items: list[tuple[Comparable, str]] = []
            # background only: stream ads with hidden (text-only) problems can't be judged by title
            hits = [a for a in market.search_site(q, 0) if not a.stream]
            for ad in hits[: RESULTS_PER_PAGE * _COMPS_PAGES]:
                if ad.price and not _SCRAPER_WANTED_RE.match(ad.title):
                    items.append((Comparable(title=ad.title, price=ad.price, url=ad.url, source="kleinanzeigen"),
                                  comp_class(p.pid, ad.truth.pid, ad.truth.klass)))
            for s in market.search_sold(q)[:30]:
                items.append((Comparable(title=s.title, price=s.price, url=s.url, source="ebay_sold", sold=True),
                              comp_class(p.pid, s.pid, s.klass)))
            for s in market.search_offers(q)[:30]:
                if "defekt" not in s.condition.lower():
                    items.append((Comparable(title=s.title, price=s.price, url=s.url, source="ebay"),
                                  comp_class(p.pid, s.pid, s.klass)))
            try:
                kept_list = relevant(q, [c for c, _ in items])
            except Exception:  # pragma: no cover - engine refactor
                return {}
            kept = {id(c) for c in kept_list}
            if estimate is not None:
                est = estimate(kept_list, asking_price_discount=0.85, query=q).market_price
                if est:
                    bad = Counter(k for c, k in items if id(c) in kept and k != "ok")
                    why = ", ".join(f"{CLASS_RU[k]} ×{v}" for k, v in bad.most_common(2)) or "—"
                    errors.append((abs(est - p.price) / p.price,
                                   f"«{q}» ({p.name}, рынок {p.price:.0f} €) → {est:.0f} €; чужие: {why}"))
            for comp, klass in items:
                row = stats[klass]
                row["returned"] += 1
                if id(comp) in kept:
                    row["kept"] += 1
                    if klass != "ok" and len(row["examples"]) < 3:
                        row["examples"].append(f"«{q}» → {comp.title} ({comp.price:.0f} €)")
    out: dict[str, dict[str, Any]] = {}
    for k, row in stats.items():
        if row["returned"]:
            out[k] = {**row, "kept_share": row["kept"] / row["returned"]}
    if errors:
        errs = [e for e, _ in errors]
        out["_estimate"] = {"n": len(errs), "median": median(errs), "over30": sum(e > 0.3 for e in errs) / len(errs),
                            "worst": [txt for _, txt in sorted(errors, key=lambda t: -t[0])[:6]]}
    return out


def _score(market: Market, scored: list[SiteAd], db: Database, config: AppConfig, calls: dict[str, int],
           notifier: CountingNotifier, summaries: list[Any], seen_pass: dict[str, int], eval_pass: dict[str, int],
           passes_run: int, params: dict[str, Any], collisions_probe: dict[str, dict[str, Any]],
           elapsed: float) -> BenchmarkResult:
    pricing = config.pricing
    notify_cfg = config.notifications
    verdicts: Counter[str] = Counter()
    trap_by_type: dict[str, dict[str, int]] = {}
    trap_by_clarity: dict[str, dict[str, int]] = {c: {"total": 0, "buy": 0, "maybe": 0} for c in CLARITIES}
    cat_stats: dict[str, dict[str, Any]] = {c.key: {"name": c.name_ru, "listings": 0, "deals": 0, "buys": 0,
                                                    "correct_buys": 0, "trap_buys": 0} for c in CATEGORIES}
    checks: dict[str, dict[str, Any]] = {
        "auction": Counter(), "haggle": Counter(), "scam_cheap": Counter(), "disclaimer": Counter(),
        "ai_down": Counter(),
    }
    errors_all: list[float] = []
    errors_cat: dict[str, list[float]] = defaultdict(list)
    errors_src: dict[str, list[float]] = defaultdict(list)
    stored_classes: Counter[str] = Counter()
    polluted = with_comps = 0
    est_eligible = 0
    mistakes: list[Mistake] = []
    bad_estimates: list[Mistake] = []
    true_deals = buys = correct = deal_buys = maybe_deals = traps = trap_buys = trap_maybes = 0
    severe_plain = severe_explicit = false_buys = notify_n = notify_correct = notify_traps = 0
    buy_ids = {d.listing.ad_id for d in db.list_deals(verdict="buy", include_ignored=True, limit=100_000)}

    for ad in scored:
        t = ad.truth
        ev = db.get_evaluation(ad.ad_id)
        verdict = ev.verdict if ev is not None else "none"
        action = str(getattr(ev, "action", "") or "") if ev is not None else ""
        is_buy = verdict == "buy" or ad.ad_id in buy_ids
        verdicts[verdict] += 1
        deal = is_true_deal(ad, pricing)
        haggle = is_haggle_deal(ad, pricing)
        acceptable = deal or (haggle and action == "haggle")  # "buy — but only after haggling" is right
        cs = cat_stats[t.category]
        cs["listings"] += 1
        cs["deals"] += deal
        cs["buys"] += is_buy
        true_deals += deal
        if is_buy:
            buys += 1
            correct += acceptable
            deal_buys += deal
            cs["correct_buys"] += acceptable
        if deal and verdict in ("buy", "maybe"):
            maybe_deals += 1
        if ev is not None and verdict in notify_cfg.verdicts and ev.score >= notify_cfg.min_score:
            notify_n += 1
            notify_correct += acceptable
            notify_traps += t.kind == "trap"
        price = ad.final_price
        cost = (price or 0.0) + (ad.shipping_cost or 0.0)

        # ---- special checks
        if t.trap == "auction":
            row = checks["auction"]
            row["total"] += 1
            row[verdict] += 1
            row["action_bid"] += action == "bid"
            row["ending_soon"] += (ad.ends_in_hours or 99) <= 3
            row["ending_soon_buy"] += is_buy and (ad.ends_in_hours or 99) <= 3
        if haggle:
            row = checks["haggle"]
            row["total"] += 1
            row[verdict] += 1
            row["action_haggle"] += action == "haggle"
            row["buy_haggle"] += is_buy and action == "haggle"
            offer = getattr(ev, "offer_price", None) if ev is not None else None
            row["offer_ok"] += offer is not None and offer <= (price or 0) * 0.95
            max_buy = ev.max_buy_price if ev is not None else None
            row["max_buy_ok"] += max_buy is not None and max_buy >= (price or 0) * 0.9
        if t.trap == "scam_cheap":
            row = checks["scam_cheap"]
            row["total"] += 1
            row[verdict] += 1
        if t.flavour == "disclaimer":
            row = checks["disclaimer"]
            row["total"] += 1
            row[verdict] += 1
            row["deal"] += deal
            row["defect_flag"] += ev is not None and any(
                f == getattr(_text, "FLAG_DEFECT", "Дефект / для мастера") for f in ev.red_flags)
        if params["ai"] == "down" and ev is not None:
            checks["ai_down"]["evaluated"] += 1
            checks["ai_down"]["buy"] += is_buy
            checks["ai_down"]["ai_checked_false"] += getattr(ev, "ai_checked", None) is False

        # ---- traps / false buys / missed deals
        if t.kind == "trap":
            traps += 1
            row = trap_by_type.setdefault(t.trap, {"total": 0, "plain": 0, "buy": 0, "plain_buy": 0, "maybe": 0})
            row["total"] += 1
            row["plain"] += t.clarity == "plain"
            row["maybe"] += verdict == "maybe"
            cl = trap_by_clarity[t.clarity]
            cl["total"] += 1
            cl["maybe"] += verdict == "maybe"
            trap_maybes += verdict == "maybe"
            if is_buy:
                trap_buys += 1
                cs["trap_buys"] += 1
                row["buy"] += 1
                row["plain_buy"] += t.clarity == "plain"
                cl["buy"] += 1
                if t.trap in SEVERE_TRAPS and t.clarity == "plain":
                    severe_plain += 1
                if t.trap in SEVERE_TRAPS and t.clarity != "subtle":
                    severe_explicit += 1
                mistakes.append(_make_mistake(
                    ad, ev, "trap_buy", f"ловушка «{TRAP_RU[t.trap]}» ({CLARITY_RU[t.clarity]}) не распознана",
                    (f"в тексте: «{t.wording}»; " if t.wording else "") + (_est_txt(ev) if ev else ""),
                    1000 + cost, pricing))
        elif is_buy and not acceptable:
            false_buys += 1
            assert ev is not None
            est = ev.estimate.market_price
            if haggle:
                key, cause = "«buy» по цене VB без торга", (f"сделка только после торга, а action={action or '—'}; "
                                                           f"{_est_txt(ev)}")
            elif est is not None and est > t.value * 1.15:
                key, cause = "рынок завышен", f"{_est_txt(ev)} при реальных {t.value:.0f} €"
            else:
                tp, roi = _true_profit(t.value, cost, pricing)
                key, cause = "граничный случай", f"истинная прибыль {tp:.0f} € (ROI {roi:.0%}) ниже порогов"
            loss = max(0.0, cost - t.value * (1 - pricing.safety_margin_percent / 100))
            mistakes.append(_make_mistake(ad, ev, "false_buy", key, cause, 500 + loss, pricing))
        elif deal and not is_buy:
            key, cause = _classify_missed(ad, ev)
            tp = _true_profit(t.value, cost, pricing)[0]
            mistakes.append(_make_mistake(ad, ev, "missed_deal", key, cause, tp, pricing))

        # ---- comparables the stored estimate is made of: which ones are another product?
        if ev is not None and ev.estimate.comparables and ev.estimate.source not in ("reference", "ai"):
            with_comps += 1
            bad = 0
            for c in ev.estimate.comparables:
                pid, klass = market.by_url.get(c.url, ("", "unknown"))
                k = comp_class(t.pid, pid, klass) if pid else "unknown"
                stored_classes[k] += 1
                bad += k in BAD_CLASSES
            polluted += bad > 0

        # ---- market-estimate accuracy: only ads whose true market is well defined
        if t.kind in ("normal", "deal") or t.trap in ("variant", "placeholder", "auction"):
            est_eligible += 1
            if ev is not None and ev.estimate.market_price:
                err = abs(ev.estimate.market_price - t.value) / t.value
                errors_all.append(err)
                errors_cat[t.category].append(err)
                errors_src[ev.estimate.source].append(err)
                if err > 0.3:
                    direction = "завышена" if ev.estimate.market_price > t.value else "занижена"
                    bad_estimates.append(_make_mistake(
                        ad, ev, "bad_estimate", f"оценка {direction}",
                        f"{_est_txt(ev)} при реальных {t.value:.0f} € (ошибка {err:.0%})", err, pricing))

    notified_ids = set(notifier.sent)
    notified_traps = sum(1 for a in scored if a.ad_id in notified_ids and a.truth.kind == "trap")
    totals: dict[str, float] = defaultdict(float)
    errors: list[str] = []
    for s in summaries:
        data = s.model_dump() if hasattr(s, "model_dump") else dict(vars(s))
        for key, value in data.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and key != "id":
                totals[key] += value
        for e in getattr(s, "errors", []) or []:
            if e not in errors:
                errors.append(e)
    n = len(scored)
    arrived = [a for a in scored if a.arrival <= passes_run]
    never_seen = sum(1 for a in arrived if a.ad_id not in seen_pass)
    never_eval = sum(1 for a in arrived if a.ad_id not in eval_pass)
    deferred = sum(1 for a, p in seen_pass.items() if eval_pass.get(a, 10**6) > p)
    per_100 = {k: round(v * 100 / max(n, 1), 1) for k, v in calls.items()}
    per_100["deferred"] = round(deferred * 100 / max(n, 1), 1)
    for c in cat_stats.values():
        c["precision"] = _share(c["correct_buys"], c["buys"])
        c["recall"] = _share(c["correct_buys"], c["deals"])
    total_stored = sum(stored_classes.values())
    collisions_stored = {
        "comps": total_stored, "estimates": with_comps, "polluted_estimates": polluted,
        "polluted_share": _share(polluted, with_comps),
        "by_class": {k: {"n": v, "share": v / total_stored} for k, v in stored_classes.most_common()},
    }
    mistakes.sort(key=lambda m: -m.severity)
    bad_estimates.sort(key=lambda m: -m.severity)
    return BenchmarkResult(
        seed=params["seed"], n_listings=len(market.stream), n_scored=n, ai_mode=params["ai"],
        ebay_sold=params["ebay"], ebay_api=params["api"], category_scan=params["cats"], passes=market.passes,
        passes_run=passes_run, market=market.stats(),
        verdicts={k: verdicts.get(k, 0) for k in ("buy", "maybe", "skip", "none")}, true_deals=true_deals,
        buys=buys, correct_buys=correct, precision=_share(correct, buys), recall=_share(deal_buys, true_deals),
        recall_buy_or_maybe=_share(maybe_deals, true_deals), notify_n=notify_n, notify_correct=notify_correct,
        notify_traps=notify_traps, notify_precision=_share(notify_correct, notify_n), traps=traps,
        trap_buys=trap_buys, trap_maybes=trap_maybes, severe_plain_trap_buys=severe_plain,
        severe_explicit_trap_buys=severe_explicit,
        trap_by_type=dict(sorted(trap_by_type.items(), key=lambda kv: -kv[1]["total"])),
        trap_by_clarity=trap_by_clarity, false_buys=false_buys,
        est_n=len(errors_all), est_coverage=_share(len(errors_all), est_eligible),
        est_median_error=median(errors_all) if errors_all else None,
        est_over30_share=_share(sum(e > 0.3 for e in errors_all), len(errors_all)),
        est_by_category={CATEGORY_BY_KEY[k].name_ru: _err_stats(v) for k, v in errors_cat.items()},
        est_by_source={k: _err_stats(v) for k, v in sorted(errors_src.items())},
        categories={v["name"]: v for v in cat_stats.values()},
        checks={k: dict(v) for k, v in checks.items()}, collisions_probe=collisions_probe,
        collisions_stored=collisions_stored, calls=calls, per_100=per_100, summary_totals=dict(totals),
        errors=errors[:10], never_seen=never_seen, never_evaluated=never_eval, deferred=deferred,
        notified=len(notified_ids), notified_traps=notified_traps, mistakes=mistakes[:60],
        bad_estimates=bad_estimates[:20], elapsed_s=round(elapsed, 2),
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _pct(x: float | None, digits: int = 0) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _eur(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f} €".replace(",", ".")


def _mark(ok: bool | None) -> str:
    return "" if ok is None else (" ✔" if ok else " ✖")


def _mistake_lines(i: int, m: Mistake) -> list[str]:
    lines = [f"  {i:>2}. «{m.title}» [{m.category}; {m.planted}]",
             f"      цена {_eur(m.price)} · реальная стоимость {_eur(m.true_value)} (рынок товара {_eur(m.product_market)})"
             f" · оценка движка {_eur(m.estimate)} [{m.estimate_source or '—'}, n={m.sample_size},"
             f" «{m.estimate_query}»] · вердикт {m.verdict}" + (f"/{m.action}" if m.action else "")
             + (f" (балл {m.score:.0f})" if m.score is not None else ""),
             f"      причина: {m.cause}"]
    if m.comps and m.kind in ("false_buy", "trap_buy", "bad_estimate"):
        lines.append("      аналоги: " + " | ".join(m.comps[:3]))
    if m.reasons:
        lines.append("      движок: " + " / ".join(r[:90] for r in m.reasons[:3]))
    if m.ai:
        lines.append(f"      ИИ: {m.ai}")
    return lines


def _checks_lines(r: BenchmarkResult) -> list[str]:
    c = r.checks
    out = ["", "▶ Особые случаи"]
    a = c.get("auction", {})
    if a.get("total"):
        out.append(f"  Аукционы eBay (ставка 10–40% рынка): {a['total']} · buy {a.get('buy', 0)}"
                   f"{_mark(a.get('buy', 0) == 0)} (из них ≤3 ч до конца: {a.get('ending_soon_buy', 0)} из "
                   f"{a.get('ending_soon', 0)}) · maybe {a.get('maybe', 0)} · skip {a.get('skip', 0)} · "
                   f"без оценки {a.get('none', 0)} · action=bid {a.get('action_bid', 0)}")
    elif not r.ebay_api:
        out.append("  Аукционы eBay: eBay API выключен — не проверялись")
    h = c.get("haggle", {})
    if h.get("total"):
        out.append(f"  VB, где торг −10% даёт сделку: {h['total']} · action=haggle {h.get('action_haggle', 0)} · "
                   f"предложение ≤95% цены {h.get('offer_ok', 0)} · max_buy ≥ цена−10% {h.get('max_buy_ok', 0)} · "
                   f"buy {h.get('buy', 0)} · maybe {h.get('maybe', 0)} · skip {h.get('skip', 0)} · без оценки {h.get('none', 0)}")
    s = c.get("scam_cheap", {})
    if s.get("total"):
        ok = s.get("skip", 0) == s["total"]
        out.append(f"  Дёшево + только пересылка/предоплата: {s['total']} · skip {s.get('skip', 0)}{_mark(ok)} · "
                   f"maybe {s.get('maybe', 0)} · buy {s.get('buy', 0)} · без оценки {s.get('none', 0)}")
    d = c.get("disclaimer", {})
    if d.get("total"):
        out.append(f"  Сделки с оговоркой «Privatverkauf … keine Rücknahme bei Defekten»: {d['total']} · "
                   f"помечены как дефект {d.get('defect_flag', 0)}{_mark(d.get('defect_flag', 0) == 0)} · "
                   f"buy {d.get('buy', 0)} (истинных сделок {d.get('deal', 0)})")
    down = c.get("ai_down", {})
    if r.ai_mode == "down":
        out.append(f"  ИИ недоступен (confidence 0): оценено {down.get('evaluated', 0)} · buy {down.get('buy', 0)}"
                   f"{_mark(down.get('buy', 0) == 0)} · ai_checked=False у {down.get('ai_checked_false', 0)}")
    return out


def _collision_lines(r: BenchmarkResult) -> list[str]:
    out = ["", "▶ Коллизии товаров в аналогах"]
    probe = r.collisions_probe
    if probe:
        out.append("  Проба фильтра relevant_comparables (все товары × типичные запросы, 3 источника):")
        out.append(f"  {'класс':<44}{'найдено':>9}{'оставлено':>11}{'доля':>7}")
        for k in ("ok",) + BAD_CLASSES:
            row = probe.get(k)
            if not row:
                continue
            flag = "" if k == "ok" else (" ✖" if row["kept_share"] > 0.05 else " ✔")
            out.append(f"  {CLASS_RU[k]:<44}{row['returned']:>9}{row['kept']:>11}{_pct(row['kept_share']):>7}{flag}")
        for k in BAD_CLASSES:
            row = probe.get(k)
            if row and row["examples"]:
                out.append(f"    утечки «{CLASS_RU[k]}»: " + " | ".join(row["examples"][:2]))
        pe = probe.get("_estimate")
        if pe:
            out.append(f"  Оценка по оставленным аналогам (медиана ×0.85 для объявлений): медианная ошибка "
                       f"{_pct(pe['median'], 1)} · >30%: {_pct(pe['over30'])} (запросов {pe['n']}); худшие:")
            out += [f"    {txt}" for txt in pe["worst"][:5]]
    st = r.collisions_stored
    if st.get("comps"):
        by = st["by_class"]
        out.append(f"  В сохранённых оценках: аналогов {st['comps']} в {st['estimates']} оценках; "
                   f"оценок с чужими товарами {st['polluted_estimates']} ({_pct(st['polluted_share'])})")
        out.append("  по классам: " + " · ".join(f"{CLASS_RU.get(k, k)} {_pct(v['share'], 1)}"
                                                 for k, v in by.items()))
    return out


def format_report(result: BenchmarkResult, *, top: int = 15) -> str:
    """Human-readable Russian report."""
    r = result
    ai_txt = {"oracle": "оракул (ловушки 85%, шум цены ±15%)", "noisy": "шумный (ловушки 60%, шум ±30%)",
              "down": "недоступен (confidence 0)", "off": "выключен"}.get(r.ai_mode, r.ai_mode)
    m = r.market
    out: list[str] = [
        f"══ Бенчмарк EbeyParser · seed {r.seed} · объявлений {r.n_listings} (оценивается {r.n_scored}) ══",
        f"ИИ: {ai_txt} · eBay sold: {'вкл' if r.ebay_sold else 'выкл (403)'} · eBay API: "
        f"{'вкл' if r.ebay_api else 'выкл'} · категории: {'вкл' if r.category_scan else 'выкл'} · "
        f"проходов {r.passes_run} (новые объявления в первых {r.passes})",
        f"Рынок: {m['categories']} категорий, {m['products']} товаров · eBay-объявлений {m['ebay_stream']} · "
        f"фон для аналогов {m['background']} · eBay sold {m['sold']} · eBay предложения {m['ebay_offers']} · "
        f"{r.elapsed_s:.1f} с",
        "",
        "▶ Сделки",
        f"  Истинных сделок: {r.true_deals} · «buy»: {r.buys} (верных {r.correct_buys}, из них «buy + торг» по VB "
        f"{r.checks.get('haggle', {}).get('buy_haggle', 0)}; ошибочных {r.buys - r.correct_buys}: ловушек "
        f"{r.trap_buys}, обычных {r.false_buys})",
        f"  Точность «buy»: {_pct(r.precision, 1)}"
        f"{_mark(None if r.precision is None else r.precision >= TARGET_PRECISION)} (цель ≥{TARGET_PRECISION:.0%})"
        f" · Полнота: {_pct(r.recall, 1)}{_mark(None if r.recall is None else r.recall >= TARGET_RECALL)}"
        f" (цель ≥{TARGET_RECALL:.0%}) · Полнота «buy»+«maybe»: {_pct(r.recall_buy_or_maybe, 1)}",
        f"  То, что получает пользователь (уведомления: вердикт ∈ notifications.verdicts, балл ≥ порога): "
        f"{r.notify_n} · верных {r.notify_correct} · точность {_pct(r.notify_precision, 1)} · ловушек {r.notify_traps}",
        f"  Вердикты: buy {r.verdicts['buy']} · maybe {r.verdicts['maybe']} · skip {r.verdicts['skip']} · "
        f"без оценки {r.verdicts['none']} · реально отправлено уведомлений {r.notified} (ловушек {r.notified_traps})",
        "",
        "▶ Ловушки (никогда не должны быть «buy»)",
        f"  Всего {r.traps} · «buy»: {r.trap_buys}{_mark(r.trap_buys == 0)} · «maybe»: {r.trap_maybes} · "
        f"серьёзных, сказанных прямо, в «buy»: {r.severe_plain_trap_buys}{_mark(r.severe_plain_trap_buys == 0)} · "
        f"+ перефразированных: {r.severe_explicit_trap_buys}",
        f"  {'тип':<36}{'всего':>6}{'прямо':>7}{'buy':>5}{'прямо buy':>11}{'maybe':>7}",
    ]
    for trap, row in r.trap_by_type.items():
        out.append(f"  {TRAP_RU.get(trap, trap):<36}{row['total']:>6}{row['plain']:>7}{row['buy']:>5}"
                   f"{row['plain_buy']:>11}{row['maybe']:>7}" + ("  ✖" if row["buy"] else ""))
    out.append("  по формулировке: " + " · ".join(
        f"{CLARITY_RU[c]}: {v['buy']}/{v['total']} buy, {v['maybe']} maybe" for c, v in r.trap_by_clarity.items()))
    ok_err = None if r.est_median_error is None else r.est_median_error <= TARGET_MEDIAN_ERROR
    out += [
        "",
        "▶ Оценка рынка (обычные объявления, сделки, модификации, аукционы)",
        f"  Медианная ошибка {_pct(r.est_median_error, 1)}{_mark(ok_err)} (цель ≤{TARGET_MEDIAN_ERROR:.0%}) · "
        f"ошибок >30%: {_pct(r.est_over30_share)} · покрытие {_pct(r.est_coverage)} (n={r.est_n})",
        f"  {'категория':<18}{'n':>5}{'медиана':>9}{'>30%':>7}{'сделок':>8}{'buy':>5}{'верных':>8}{'ловушек':>9}",
    ]
    for name, c in r.categories.items():
        e = r.est_by_category.get(name, {"n": 0, "median": None, "over30": None})
        out.append(f"  {name:<18}{e['n']:>5}{_pct(e['median'], 1):>9}{_pct(e['over30']):>7}{c['deals']:>8}"
                   f"{c['buys']:>5}{c['correct_buys']:>8}{c['trap_buys']:>9}")
    out.append("  по источнику оценки: " + " · ".join(
        f"{src} {_pct(s['median'], 1)} (n={s['n']}, >30%: {_pct(s['over30'])})" for src, s in r.est_by_source.items()))
    out += _checks_lines(r)
    out += _collision_lines(r)
    p = r.per_100
    out += [
        "",
        "▶ Воронка и стоимость (на 100 объявлений)",
        f"  поиск аналогов Kleinanzeigen {p.get('comparables', 0):g} · eBay sold {p.get('ebay_sold', 0):g} · "
        f"eBay API аналоги {p.get('ebay_api_comparables', 0):g} · страниц объявлений {p.get('details', 0):g} · "
        f"загрузок фото {p.get('image_requests', 0):g} · вызовов ИИ {p.get('ai', 0):g} · отложено {p.get('deferred', 0):g}",
        f"  всего: поисков {r.calls.get('searches', 0)} · аналогов {r.calls.get('comparables', 0)} · "
        f"eBay sold {r.calls.get('ebay_sold', 0)} · страниц {r.calls.get('details', 0)} · ИИ {r.calls.get('ai', 0)}"
        f" · не найдено поиском {r.never_seen} · не оценено {r.never_evaluated}",
        "  RunSummary (сумма): " + ", ".join(f"{k}={v:g}" for k, v in r.summary_totals.items()),
    ]
    if r.errors:
        out.append("  ошибки/предупреждения прогонов: " + " | ".join(e[:120] for e in r.errors[:4]))
    worst = r.mistakes[:top]
    out += ["", f"▶ Худшие ошибки (топ {len(worst)}, по причинам)"]
    groups: dict[str, list[Mistake]] = {}
    for mk in worst:
        groups.setdefault(mk.cause_key, []).append(mk)
    i = 0
    for key, items in groups.items():
        out.append(f" ■ {key} — {len(items)}")
        for mk in items:
            i += 1
            out += _mistake_lines(i, mk)
    if not worst:
        out.append("  нет ошибок 🎉")
    counts = Counter(mk.cause_key for mk in r.mistakes)
    if counts:
        out.append("  все ошибки по причинам: " + ", ".join(f"{k} ×{v}" for k, v in counts.most_common()))
    if r.bad_estimates:
        out += ["", "▶ Худшие оценки рынка (>30%)"]
        for j, mk in enumerate(r.bad_estimates[:8], 1):
            out.append(f"  {j}. «{mk.title}» — {mk.cause}")
            if mk.comps:
                out.append("      аналоги: " + " | ".join(mk.comps[:3]))
    out += ["", "Инвариант: " + ("✔ ни одна серьёзная ловушка, сказанная прямо, не получила «buy»" if r.invariants_ok
                                 else f"✖ серьёзных ловушек (сказано прямо) в «buy»: {r.severe_plain_trap_buys}")]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments of the `benchmark` subcommand (shared with `python -m ebeyparser.benchmark`)."""
    parser.add_argument("--seed", type=int, default=1, help="seed синтетического рынка")
    parser.add_argument("-n", "--n", type=int, default=600, help="сколько объявлений в потоке")
    parser.add_argument("--ai", choices=AI_MODES, default="oracle", help="фальшивый ИИ: oracle / noisy / down / off")
    parser.add_argument("--no-ebay", action="store_true", help="eBay sold недоступен (как живой 403)")
    parser.add_argument("--no-ebay-api", action="store_true", help="без eBay Browse API (без аукционов)")
    parser.add_argument("--no-categories", action="store_true", help="без сканирования категорий")
    parser.add_argument("--passes", type=int, default=None, help="сколько проходов с новыми объявлениями")
    parser.add_argument("--top", type=int, default=15, help="сколько худших ошибок показать")
    parser.add_argument("--json", default=None, help="сохранить полный результат в JSON-файл")


def run_cli(args: Any = None) -> int:
    """Entry point for `python -m ebeyparser benchmark`. Prints the report; exit code 1 when a
    plainly stated severe trap was judged "buy" (the invariant), else 0."""
    seed = int(getattr(args, "seed", 1) or 1)
    n = int(getattr(args, "n", None) or getattr(args, "n_listings", None) or 600)
    ai = getattr(args, "ai", None) or "oracle"
    result = run_benchmark(
        seed=seed, n_listings=n, ai_mode=ai, ebay_sold=not getattr(args, "no_ebay", False),
        category_scan=not getattr(args, "no_categories", False), passes=getattr(args, "passes", None),
        ebay_api=not getattr(args, "no_ebay_api", False),
    )
    print(format_report(result, top=int(getattr(args, "top", 15) or 15)))
    path = getattr(args, "json", None)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, ensure_ascii=False, indent=1, default=str)
        print(f"\nJSON: {path}")
    return 0 if result.invariants_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ebeyparser.benchmark",
                                     description="Бенчмарк качества на синтетическом рынке")
    add_cli_arguments(parser)
    return run_cli(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
