from __future__ import annotations

import time

import pytest

from ebeyparser.pricing.identity import (
    KINDS,
    ProductKey,
    classify_kind,
    comparable_matches,
    history_key,
    identify,
    missing_parts,
    product_key,
    same_product,
)


def key_of(title: str) -> str | None:
    k = product_key(title)
    return k.key() if k else None


# --------------------------------------------------------------------------- product_key


@pytest.mark.parametrize(("title", "key"), [
    # phones
    ("Apple iPhone 13 128GB blau", "iphone|13||128gb"),
    ("iPhone 13 Pro Max 256GB Graphit", "iphone|13|pro max|256gb"),
    ("iphone13pro 128 GB", "iphone|13|pro|128gb"),
    ("iPhone 14 Plus 128GB", "iphone|14|plus|128gb"),
    ("iPhone 12 mini 64GB", "iphone|12|mini|64gb"),
    ("iPhone SE 2020 64GB", "iphone|se|gen2|64gb"),
    ("iPhone SE (3. Generation) 128GB", "iphone|se|gen3|128gb"),
    ("iPhone XS Max 256GB", "iphone|xs|max|256gb"),
    ("Samsung Galaxy S21 Ultra 5G 256GB", "galaxy s|s21|ultra|256gb"),
    ("Galaxy S21+ 128GB", "galaxy s|s21|plus|128gb"),
    ("Samsung S21FE 128GB", "galaxy s|s21|fe|128gb"),
    ("Samsung Galaxy S2", "galaxy s|s2"),
    ("Samsung Galaxy A54 5G 8/256GB", "galaxy a|a54||256gb"),
    ("Samsung Galaxy Z Fold 4 512GB", "galaxy z fold|4||512gb"),
    ("Galaxy Z Flip5 256GB", "galaxy z flip|5||256gb"),
    ("Samsung Galaxy Tab S8 Ultra 128GB", "galaxy tab|s8|ultra|128gb"),
    ("Samsung Galaxy Watch 4 Classic 46mm", "galaxy watch|4|classic|||46mm"),
    ("Samsung Galaxy Buds2 Pro", "galaxy buds|2|pro"),
    ("Google Pixel 7a 128GB", "pixel|7a||128gb"),
    ("Google Pixel 8 Pro 256GB", "pixel|8|pro|256gb"),
    # Apple computers, tablets, wearables
    ("MacBook Air M1 8GB 256GB", "macbook air|m1||256gb|8gb"),
    ("Apple MacBook Pro 14 M1 Pro 16GB 512GB", "macbook pro|m1|pro|512gb|16gb|14in"),
    ("MacBook Air 15 Zoll M2 8GB 512GB", "macbook air|m2||512gb|8gb|15in"),
    ("MacBook Pro 2019 16 Zoll i9 32GB 1TB", "macbook pro|2019||1tb|32gb|16in"),
    ("Mac mini M2 8GB 256GB", "mac mini|m2||256gb|8gb"),
    ("iPad 9. Generation 64GB WiFi", "ipad|2021||64gb"),
    ("iPad 2021 64GB", "ipad|2021||64gb"),
    ("iPad Air 5 64GB", "ipad air|2022||64gb"),
    ("iPad mini 6 64GB", "ipad mini|2021||64gb"),
    ("iPad Pro 11 M4 256GB", "ipad pro|2024||256gb||11in"),
    ("iPad Pro 12,9 Zoll 2021 M1 128GB", "ipad pro|2021||128gb||12.9in"),
    ("Apple Watch Series 7 45mm GPS", "apple watch|7||||45mm"),
    ("Apple Watch SE 2022 40mm", "apple watch|se|gen2|||40mm"),
    ("Apple Watch Ultra 2", "apple watch|ultra|gen2"),
    ("AirPods Pro 2. Generation", "airpods|pro|gen2"),
    ("AirPods Pro", "airpods|pro"),
    ("Apple AirPods 3", "airpods|3"),
    ("AirPods Max Space Grau", "airpods|max"),
    # consoles & handhelds
    ("Sony PS5 Digital Edition 825GB", "playstation|5|digital|825gb"),
    ("PlayStation 5 Slim Disc", "playstation|5|slim"),
    ("PS4 Pro 1TB", "playstation|4|pro|1tb"),
    ("PS VR2", "playstation vr|2"),
    ("Xbox Series X 1TB", "xbox|series x||1tb"),
    ("Xbox Series S 512GB", "xbox|series s||512gb"),
    ("Xbox One X", "xbox|one x"),
    ("Nintendo Switch OLED weiß", "nintendo switch|oled"),
    ("Nintendo Switch Lite türkis", "nintendo switch|lite"),
    ("Nintendo Switch V2 Konsole", "nintendo switch|standard"),
    ("Nintendo New 3DS XL", "nintendo ds|3ds|xl new"),
    ("Steam Deck OLED 512GB", "steam deck|oled||512gb"),
    ("Valve Steam Deck 64GB", "steam deck|lcd||64gb"),
    ("Meta Quest 3 128GB", "meta quest|3||128gb"),
    # PC components
    ("Gigabyte GeForce RTX 3080 Gaming OC 10G", "rtx|3080"),
    ("ZOTAC RTX 3080 Trinity OC LHR 10GB GDDR6X", "rtx|3080"),
    ("MSI RTX 3080 Ventus 3X 12G LHR", "rtx|3080|||12gb"),
    ("RTX3080Ti Founders Edition", "rtx|3080|ti"),
    ("MSI RTX 4070 Ti SUPER Gaming X Slim", "rtx|4070|ti super"),
    ("RTX 4060Ti 16GB", "rtx|4060|ti||16gb"),
    ("Nvidia GeForce GTX 1060 3GB", "gtx|1060|||3gb"),
    ("AMD Radeon RX 7900 XTX", "rx|7900|xtx"),
    ("Sapphire Pulse RX 6800 XT 16GB", "rx|6800|xt"),
    ("EVGA 3080 FTW3", "rtx|3080"),
    ("AMD Ryzen 5 5600", "ryzen|5600"),
    ("AMD Ryzen 5 5600X", "ryzen|5600x"),
    ("Ryzen 7 5800X3D", "ryzen|5800x3d"),
    ("Ryzen 5600 XT", "ryzen|5600xt"),
    ("Intel Core i7-8700K", "intel core|8700k"),
    ("Intel i5 13600KF", "intel core|13600kf"),
    ("Corsair Vengeance 32GB (2x16GB) DDR4 3200", "ram ddr4|32gb"),
    ("G.Skill 2x8GB DDR4 RAM", "ram ddr4|16gb"),
    ("Kingston 16GB DDR4 ECC Registered", "ram ddr4|16gb|ecc"),
    ("Samsung 970 Evo Plus 1TB NVMe", "samsung ssd|970|evo plus|1tb"),
    ("WD Black SN850X 2TB", "wd sn|sn850x||2tb"),
    # laptops
    ("Lenovo ThinkPad T480 i5 16GB 256GB", "thinkpad|t480"),
    ("ThinkPad X1 Carbon Gen 9", "thinkpad|x1|carbon gen9"),
    ("Lenovo ThinkPad T14 Gen 2 AMD", "thinkpad|t14|gen2"),
    ("Dell XPS 13 9310", "dell xps|9310"),
    ("HP EliteBook 840 G5", "hp elitebook|840|gen5"),
    ("Lenovo Legion 5 Pro RTX 3070", "lenovo legion|5|pro"),
    ("Microsoft Surface Pro 7", "surface pro|7"),
    # audio, cameras, drones, household, tools
    ("Sony WH-1000XM4 schwarz", "sony wh-1000x|xm4"),
    ("Sony WH1000XM5", "sony wh-1000x|xm5"),
    ("Sony WF-1000XM4", "sony wf-1000x|xm4"),
    ("Bose QuietComfort 35 II", "bose qc|35|ii"),
    ("Bose QC45", "bose qc|45"),
    ("Sony Alpha 7 III Body", "sony alpha|a7|mk3"),
    ("Sony A7 III + 28-70mm Kit", "sony alpha|a7|mk3 kit"),
    ("Sony A7R IV", "sony alpha|a7r|mk4"),
    ("Sony A6000 Kamera", "sony alpha|a6000"),
    ("Canon EOS 250D", "canon eos|250d"),
    ("Canon EOS 5D Mark IV Body", "canon eos|5d|mk4"),
    ("Nikon D750", "nikon|d750"),
    ("GoPro Hero 12 Black", "gopro hero|12|black"),
    ("DJI Mini 3 Pro", "dji mini|3|pro"),
    ("DJI Mini 3 Pro Fly More Combo", "dji mini|3|pro combo"),
    ("DJI Mavic Air 2", "dji air|2"),
    ("DJI Air 2S", "dji air|2s"),
    ("DJI Osmo Pocket 3", "dji osmo pocket|3"),
    ("Dyson V15 Detect Absolute", "dyson|v15"),
    ("Dyson V8 Slim", "dyson|v8|slim"),
    ("Makita DHP484Z", "makita|dhp484|solo"),
    ("Makita DHP484RTJ", "makita|dhp484"),
    ("DeWalt DCD796N", "dewalt|dcd796|solo"),
    ("Bosch Professional GSR 18V-55 mit 2 Akkus", "bosch|gsr 18v 55"),
    # generic fallback
    ("Lego Star Wars 75192", "lego|75192"),
    ("Kindle Paperwhite 11. Generation", "kindle paperwhite|gen11"),
    ("Canon Pixma TS5150", "pixma|ts5150"),
    ("Vorwerk Thermomix TM6", "thermomix|tm6"),
    ("Garmin Fenix 7 Pro Solar", "fenix|7|pro"),
    ("Kindle Paperwhite 5 16GB", "kindle paperwhite|5||16gb"),
])
def test_product_key(title, key):
    assert key_of(title) == key


@pytest.mark.parametrize("title", [
    "iPhone", "Handy", "Fahrrad 28 Zoll", "Apple iPhone 128GB", "Samsung Galaxy", "Nintendo Wii U",
    "Sofa 3 Sitzer", "Kinderwagen 2019", "Laptop", "Gaming PC", "Lenovo Legion Laptop RTX 3080",
    "MacBook Pro 13 Zoll", "Dyson Staubsauger", "AirPods", "Waschmaschine 7kg", "", "   ",
    "Tisch 120x80", "Schuhe Größe 42",
])
def test_generic_titles_have_no_key(title):
    assert product_key(title) is None


@pytest.mark.parametrize(("a", "b"), [
    ("Apple iPhone 13 128GB blau", "iPhone 13 128 GB Schwarz"),
    ("iPhone 13 Pro Max 256GB", "256GB Pro Max iPhone 13"),
    ("Samsung Galaxy S21 Ultra 256GB", "Galaxy S21 Ultra 5G 256 GB Phantom Black"),
    ("MSI RTX 3080 Ventus 10GB", "Gigabyte GeForce RTX3080 Gaming OC"),
    ("Sony PS5 Digital Edition", "PlayStation 5 Digital"),
    ("Nintendo Switch OLED Modell", "Switch OLED weiß Nintendo"),
    ("AMD Ryzen 5 5600X", "Ryzen 5600X Prozessor"),
    ("iPad 9. Generation 64GB", "Apple iPad 2021 64 GB"),
    ("iPad Air 5 64GB", "iPad Air M1 64GB"),
    ("Sony WH-1000XM4", "Sony WH 1000 XM4 Kopfhörer"),
    ("Sony A7 III", "Sony Alpha 7 Mark III"),
    ("AirPods Pro 2", "Apple AirPods Pro 2. Generation"),
    ("LEGO 75192", "Lego Star Wars 75192 Millennium Falcon"),
    ("Samsung Galaxy S23 Ultra 12/512GB", "Galaxy S23 Ultra 512GB"),
])
def test_key_is_stable_across_wording(a, b):
    assert key_of(a) is not None and key_of(a) == key_of(b)


def test_key_and_coarse_key_format():
    k = product_key("iPhone 13 Pro Max 128GB")
    assert k == ProductKey("iphone", "13", ("pro", "max"), "128gb")
    assert k.key() == "iphone|13|pro max|128gb"
    assert k.coarse_key() == "iphone|13|pro max"
    assert product_key("iPhone 13").coarse_key() == "iphone|13"
    assert k.query() == "iphone 13 pro max 128gb"
    assert product_key("MSI RTX 3080 Ti Suprim X").query() == "rtx 3080 ti"
    assert product_key("PS5 Digital Edition").query() == "ps5 digital"


# --------------------------------------------------------------------------- same_product


@pytest.mark.parametrize(("a", "b"), [
    ("iPhone 13", "iPhone 13 128GB"),
    ("iPhone 13 128GB", "Apple iPhone 13 128 GB Mitternacht"),
    ("RTX 3080", "RTX 3080 FE"),
    ("RTX 3080", "RTX 3080 LHR"),
    ("RTX 3080 10GB", "RTX 3080"),
    ("Galaxy S21 Ultra", "Samsung S21 Ultra 5G"),
    ("MacBook Air M1", "MacBook Air M1 8GB 256GB 13 Zoll"),
    ("PS5 Disc", "PlayStation 5"),
    ("Ryzen 5 5600X", "AMD Ryzen 5600X"),
    ("ThinkPad T480", "Lenovo ThinkPad T480 i5 8GB"),
])
def test_same_product(a, b):
    ka, kb = product_key(a), product_key(b)
    assert ka and kb and same_product(ka, kb), (ka, kb)


@pytest.mark.parametrize(("a", "b"), [
    ("iphone 13 pro", "iPhone 13 Pro Max"),
    ("iPhone 13", "iPhone 13 Pro"),
    ("iPhone 13", "iPhone 13 mini"),
    ("rtx 4070 ti", "RTX 4070 Ti Super"),
    ("rtx 3080", "RTX 3080 Ti"),
    ("rtx 3080", "RTX3080Ti"),
    ("rtx 3080", "RTX 3070"),
    ("rtx 3080", "RTX 3080 12GB"),
    ("rtx 3060", "RTX 3060 8GB"),
    ("rx 6800", "RX 6800 XT"),
    ("rx 7900 xt", "RX 7900 XTX"),
    ("galaxy s2", "Galaxy S23"),
    ("Galaxy S21", "Galaxy S21 Ultra"),
    ("Galaxy S21", "Galaxy S21 FE"),
    ("Galaxy S21 Ultra 256GB", "Galaxy S21 Ultra 128GB"),
    ("Galaxy S21 Ultra 5G 256GB", "Galaxy S21 Ultra 5G 128GB"),
    ("iPhone 13 128GB", "iPhone 13 256GB"),
    ("ryzen 5 5600", "Ryzen 5 5600X"),
    ("Ryzen 7 5800X", "Ryzen 7 5800X3D"),
    ("i7 8700", "i7 8700K"),
    ("PS5", "PS5 Digital Edition"),
    ("PS5", "PS5 Slim"),
    ("PS4", "PS4 Pro"),
    ("Xbox Series X", "Xbox Series S"),
    ("Nintendo Switch", "Nintendo Switch OLED"),
    ("Nintendo Switch OLED", "Nintendo Switch Lite"),
    ("MacBook Air M1 8GB", "MacBook Air M1 16GB"),
    ("MacBook Air M2 13 Zoll", "MacBook Air M2 15 Zoll"),
    ("MacBook Pro M1", "MacBook Pro M1 Pro"),
    ("MacBook Air M1", "MacBook Pro M1"),
    ("Apple Watch Series 7 41mm", "Apple Watch Series 7 45mm"),
    ("AirPods Pro", "AirPods Pro 2"),
    ("AirPods 2", "AirPods 3"),
    ("Sony WH-1000XM4", "Sony WH-1000XM5"),
    ("Sony A7 III", "Sony A7 IV"),
    ("Sony A7 III", "Sony A7R III"),
    ("DJI Mini 3", "DJI Mini 3 Pro"),
    ("Steam Deck 64GB", "Steam Deck OLED 512GB"),
    ("Steam Deck 256GB", "Steam Deck 512GB"),
    ("Makita DHP484Z", "Makita DHP484RTJ"),
    ("DeWalt DCD796N", "DeWalt DCD796P2"),
    ("iPad Air 4", "iPad Air 5"),
    ("iPad Pro 11 M4", "iPad Pro 13 M4"),
])
def test_different_products(a, b):
    ka, kb = product_key(a), product_key(b)
    assert ka and kb and not same_product(ka, kb), (ka, kb)
    assert not same_product(kb, ka)


def test_strict_capacity_can_be_relaxed():
    a, b = product_key("iPhone 13 128GB"), product_key("iPhone 13 256GB")
    assert not same_product(a, b)
    assert same_product(a, b, strict_capacity=False)
    # GPU VRAM defines the card and is always compared
    g1, g2 = product_key("RTX 3080 10GB"), product_key("RTX 3080 12GB")
    assert not same_product(g1, g2, strict_capacity=False)


# --------------------------------------------------------------------------- classify_kind


@pytest.mark.parametrize(("title", "kind"), [
    # plain items
    ("Apple iPhone 13 128GB blau", "item"),
    ("iPhone 13 128GB Akku 89%", "item"),
    ("iPhone 13 mit Hülle und Panzerglas", "item"),
    ("iPhone 13 Pro, neuer Akku, Display top", "item"),
    ("Gigabyte RTX 3080 Gaming OC 10G", "item"),
    ("RTX 3080 aus Gaming PC ausgebaut", "item"),
    ("PS5 Disc Edition inkl. Controller", "item"),
    ("Nintendo Switch OLED mit Dock", "item"),
    ("MacBook Air M1 deutsche Tastatur", "laptop"),
    ("iPad Air 5 Liquid Retina Display 64GB", "item"),
    ("Tausch möglich: iPhone 13", "item"),
    ("Verkaufe PS5, suche Xbox", "item"),
    ("Tausche oder verkaufe PS5", "item"),
    # parts and accessories
    ("Hülle für iPhone 13", "accessory"),
    ("iPhone 13 Display Ersatz", "part"),
    ("Ladekabel iPhone 13", "accessory"),
    ("Panzerglas iPhone 13 Pro 2 Stück", "accessory"),
    ("iPhone 13 Akku", "part"),
    ("Original Apple iPhone 13 Rückseite", "part"),
    ("PS5 Controller", "accessory"),
    ("PS5 Spiel FIFA 23", "accessory"),
    ("Controller für PS5 DualSense", "accessory"),
    ("Wasserkühler für RTX 3080", "accessory"),
    ("EVGA RTX 3080 FTW3 Kühler", "accessory"),
    ("Alphacool Eisblock RTX 3080 waterblock", "accessory"),
    ("Nintendo Switch Dock", "accessory"),
    ("Netzteil für MacBook Pro", "accessory"),
    ("Laptop Netzteil Lenovo 65W", "accessory"),
    ("AirPods Pro Ladecase", "accessory"),
    ("Akku für Dyson V8", "part"),
    ("Kühler für Gaming PC", "accessory"),
    ("Makita DTD153Z Akku-Schlagschrauber", "item"),
    ("Dyson V8 Akku Staubsauger", "item"),
    ("Akku für Makita DTD153", "part"),
    # whole computers and laptops
    ("Gaming PC mit RTX 3080, Ryzen 7 5800X, 32GB", "complete_pc"),
    ("Gaming-PC RTX 3080", "complete_pc"),
    ("Ryzen 7 5800X + RTX 3080 + 32GB RAM", "complete_pc"),
    ("Laptop Lenovo Legion RTX 3080", "laptop"),
    ("ASUS ROG Strix G15 RTX 3070", "laptop"),
    ("MSI GF63 RTX 3050 i5 10500H", "laptop"),
    ("Lenovo ThinkPad T480 Laptop", "laptop"),
    # boxes, defects, wanted, swaps, services, bundles
    ("Leere OVP RTX 3080 Karton", "box_only"),
    ("Nur OVP iPhone 14 Pro", "box_only"),
    ("iPhone 13 defekt", "defect"),
    ("iPhone 12 für Bastler", "defect"),
    ("RTX 3080 kein Bild", "defect"),
    ("PS5 geht nicht an", "defect"),
    ("iPhone 11 iCloud gesperrt", "defect"),
    ("Samsung S21 Display gebrochen", "defect"),
    ("iPhone 13 nicht defekt, top Zustand", "item"),
    ("MacBook Pro Karton leicht kaputt, Gerät top", "laptop"),
    ("Suche RTX 3080", "wanted"),
    ("Ankauf iPhone alle Modelle", "wanted"),
    ("RTX 3080 gesucht", "wanted"),
    ("Tausche PS5 gegen Xbox Series X", "swap"),
    ("iPhone Display Reparatur Service", "service"),
    ("Biete Reparatur für Konsolen", "service"),
    ("PS5 + 2 Controller + 5 Spiele", "bundle"),
    ("Nintendo Switch mit 3 Spielen", "bundle"),
    ("Konvolut 3x iPhone 8", "bundle"),
    ("", "unknown"),
])
def test_classify_kind(title, kind):
    assert classify_kind(title) == kind
    assert kind in KINDS


@pytest.mark.parametrize(("description", "kind"), [
    ("Privatverkauf, keine Garantie oder Rücknahme bei Defekten.", "item"),
    ("Rücknahme bei Defekt ausgeschlossen.", "item"),
    ("Keine Haftung für eventuelle Defekte.", "item"),
    ("Falls ein Defekt auftritt, keine Rücknahme.", "item"),
    ("Keine Defekte, keine Kratzer.", "item"),
    ("Defekte: keine", "item"),
    ("Keinerlei Kratzer oder Defekte", "item"),
    ("Das Gerät ist nicht defekt.", "item"),
    ("Kein Wasserschaden, alles funktioniert.", "item"),
    ("Hülle ist kaputt, Handy top.", "item"),
    ("Display hat einen Riss, Display gebrochen.", "defect"),
    ("Handy ist defekt, keine Rücknahme.", "defect"),
    ("Hatte einen Wasserschaden.", "defect"),
    ("Verkauft wird nur die OVP.", "box_only"),
    ("Nur Tausch, kein Verkauf!", "swap"),
    ("Kein Tausch, nur Verkauf.", "item"),
    ("Für Defekte nach dem Kauf keine Haftung.", "item"),
    ("Es funktioniert alles, nichts ist kaputt.", "item"),
    ("Defekt? Nein!", "item"),
    ("Face ID funktioniert nicht.", "defect"),
    ("Defekt - keine Rücknahme", "defect"),
])
def test_classify_kind_uses_description_carefully(description, kind):
    assert classify_kind("Apple iPhone 13 128GB", description) == kind


def test_kind_hint_turns_natural_kind_into_item():
    assert classify_kind("Lenovo ThinkPad T480 Laptop") == "laptop"
    assert classify_kind("Lenovo ThinkPad T480 Laptop", query_kind_hint="laptop") == "item"
    assert classify_kind("Hülle für iPhone 13", query_kind_hint="accessory") == "item"
    assert classify_kind("Gaming PC mit RTX 3080", query_kind_hint="complete_pc") == "item"
    assert classify_kind("iPhone 13 defekt", query_kind_hint="laptop") == "defect"


# --------------------------------------------------------------------------- comparable_matches


@pytest.mark.parametrize(("query", "title", "ok"), [
    # the critic's cases: must NOT match
    ("iPhone 13", "Hülle für iPhone 13", False),
    ("iPhone 13", "iPhone 13 Display Ersatz", False),
    ("iPhone 13", "Ladekabel iPhone 13", False),
    ("Sony PS5", "PS5 Controller", False),
    ("Sony PS5", "PS5 Spiel FIFA 23", False),
    ("iphone 13 pro", "iPhone 13 Pro Max", False),
    ("rtx 4070 ti", "RTX 4070 Ti Super", False),
    ("galaxy s2", "Galaxy S23", False),
    ("ryzen 5 5600", "Ryzen 5 5600X", False),
    ("Galaxy S21 Ultra 256GB", "Galaxy S21 Ultra 128GB", False),
    ("Galaxy S21 Ultra 5G 256GB", "Samsung Galaxy S21 Ultra 5G 128GB", False),
    ("rtx 3080", "Gaming PC RTX 3080 Ryzen 7 5800X 32GB", False),
    ("rtx 3080", "Wasserkühler für RTX 3080", False),
    ("rtx 3080", "Suche RTX 3080", False),
    ("rtx 3080", "Leere OVP RTX 3080 Karton", False),
    ("rtx 3080", "Gaming-PC RTX 3080", False),
    ("rtx 3080", "MSI RTX 3080 Ti Suprim X", False),
    ("rtx 3080", "RTX 3080Ti Founders Edition", False),
    ("rtx 3080", "EVGA RTX 3080 FTW3 Kühler", False),
    ("rtx 3080", "Alphacool Eisblock RTX 3080 waterblock", False),
    ("rtx 3080", "Laptop Lenovo Legion RTX 3080", False),
    ("rtx 3080", "Lenovo Legion 7 RTX 3080 16GB", False),
    ("rtx 3080", "RTX 3080 defekt", False),
    ("rtx 3080", "RTX 3070", False),
    ("rtx 3080", "RTX 3080 12GB", False),
    ("apple iphone 13", "Apple iPhone 13 Pro 128GB", False),
    ("Sony PS5", "Tausche PS5 gegen Switch", False),
    ("Sony PS5", "PS5 + 2 Controller + 5 Spiele", False),
    ("MacBook Air M1 8GB 256GB", "MacBook Air M1 16GB 256GB", False),
    # same kind of offer, same product: must match
    ("rtx 3080", "Gigabyte GeForce RTX 3080 Gaming OC 10G", True),
    ("rtx 3080", "RTX3080 MSI Ventus", True),
    ("rtx 3080", "ZOTAC RTX 3080 Trinity OC LHR 10GB", True),
    ("rtx 3080", "NVIDIA RTX 3080 Founders Edition", True),
    ("rtx 3080 ti", "MSI RTX 3080 Ti Suprim X", True),
    ("apple iphone 13 pro max", "iPhone 13 Pro Max 256GB Graphit", True),
    ("apple iphone 13", "iPhone 13 128GB", True),
    ("iPhone 13 128GB", "Apple iPhone 13 128 GB Schwarz, Akku 91%", True),
    ("lenovo thinkpad t480", "Lenovo ThinkPad T480 Laptop i5 16GB", True),
    ("sony ps5", "PS5 Disc Edition", True),
    ("Sony PS5 Digital Edition", "PlayStation 5 Digital", True),
    ("Galaxy S21 Ultra 256GB", "Samsung Galaxy S21 Ultra 5G 256GB", True),
    ("Mac mini M2 8GB 256GB", "Apple Mac mini M2 Desktop Computer 8GB 256GB", True),
    ("Hülle für iPhone 13", "iPhone 13 Silikon Hülle", True),
    ("iPhone 13 defekt", "iPhone 13 Display gebrochen", True),
])
def test_comparable_matches(query, title, ok):
    got, reason = comparable_matches(query, title)
    assert got is ok, reason
    assert reason


def test_comparable_matches_reasons_and_key_input():
    ok, reason = comparable_matches(product_key("RTX 3080"), "RTX 3080 Ti")
    assert not ok and "variant" in reason
    ok, reason = comparable_matches("rtx 3080", "Gaming PC mit RTX 3080")
    assert not ok and "complete_pc" in reason
    ok, reason = comparable_matches("iPhone 13", "iPhone 13 128GB", "Displayschaden, für Bastler")
    assert not ok and "defect" in reason
    ok, _ = comparable_matches("iPhone 13", "iPhone 13 128GB", "Keine Garantie oder Rücknahme bei Defekten")
    assert ok
    ok, reason = comparable_matches("iPhone 13", "Handy")
    assert not ok and "no model" in reason


def test_comparable_matches_generic_query_falls_back_to_words():
    assert comparable_matches("Fahrrad 28 Zoll", "Damen Fahrrad 28 Zoll Alu")[0]
    assert not comparable_matches("Fahrrad 28 Zoll", "Kinderwagen")[0]


def test_gaming_pcs_and_ti_cards_are_dropped_from_gpu_comparables():
    titles = ["RTX 3080 MSI", "RTX 3080 Zotac", "Asus TUF RTX 3080", "Palit RTX 3080 10GB",
              "Gaming PC RTX 3080 Ryzen 5", "RTX 3080 Ti", "Wasserkühler RTX 3080", "Suche RTX 3080",
              "RTX 3080 Karton leer", "Laptop RTX 3080", "RTX 3080 defekt"]
    kept = [t for t in titles if comparable_matches("rtx 3080", t)[0]]
    assert kept == titles[:4]


def test_fast_enough_for_every_ad():
    titles = [f"Verkaufe Apple iPhone {n} Pro Max 256GB blau top Zustand mit Hülle #{i}"
              for i in range(300) for n in (11, 12, 13)]
    t0 = time.perf_counter()
    for t in titles:
        product_key(t)
        classify_kind(t, "Privatverkauf, keine Garantie oder Rücknahme bei Defekten.")
        comparable_matches("iPhone 13 Pro Max 256GB", t)
    assert time.perf_counter() - t0 < 2.0


# --------------------------------------------------------------------------- benchmark findings


@pytest.mark.parametrize(("query", "title"), [
    ("Xbox Series S 512GB", "Xbox Series X 1TB"),
    ("Xbox Series X", "Xbox Series S"),
    ("Galaxy S23", "Galaxy S23+ 256GB"),
    ("Apple iPhone 12 64GB", "Apple iPhone 12 64GB + Zubehörpaket"),
    ("Xbox Series S 512GB", "Xbox Series S + 2 Controller + 5 Spiele"),
    ("PS5 Disc Edition", "PS5 Disc Edition Controller"),
    ("PS5 Digital Edition", "Spiele für PS5 Digital Edition (5 Stück)"),
    ("iPhone 13", "iPhone 13 OVP leer"),
    ("iPhone 13", "iPhone 13 OVP Originalverpackung ohne Gerät"),
    ("RTX 3080", "RTX 3080 Backplate"),
    ("RTX 3080", "EKWB Wasserkühler für RTX 3080 FE"),
    ("DeWalt DCD796 P2", "DeWalt DCD796 solo"),
    ("Makita DHP485", "Makita DHP485 nur das Grundgerät"),
    ("Nintendo Switch OLED", "Switch OLED nur Tablet"),
    ("Dyson V11", "Dyson V11 ohne Akku"),
    ("MacBook Pro 14 M1 Pro", "MacBook Pro 14 M1 Pro ohne SSD/Festplatte"),
])
def test_benchmark_non_matches(query, title):
    ok, reason = comparable_matches(query, title)
    assert not ok, reason


@pytest.mark.parametrize(("a", "b"), [
    ("iPhone 14 Pro 256GB Graphit", "iPhone 14 Pro 256GB Gold"),
    ("iPhone 13 Mitternacht 128GB", "iPhone 13 128GB Polarstern"),
    ("iPhone 13 Pro Sierrablau 256GB", "iPhone 13 Pro 256GB"),
    ("iPhone 14 Pro Max Dunkellila 1TB", "iPhone 14 Pro Max 1TB Space Schwarz"),
    ("iPhone 15 Pro Titan Blau 256GB", "iPhone 15 Pro 256GB Titan Natur"),
    ("Galaxy S21 Phantom Black 128GB", "Galaxy S21 128GB"),
    ("Pixel 8 Obsidian 128GB", "Google Pixel 8 Hazel 128GB"),
    ("Pixel 7 Lemongrass", "Pixel 7 Snow"),
    ("Galaxy S23 Cream 256GB", "Galaxy S23 Lavender 256GB"),
    ("iPhone 12 Rosé 64GB", "iPhone 12 Grün 64GB"),
    ("MacBook Air M2 2022 Space Grau 256GB", "MacBook Air M2 256GB Silber"),
    ("Samsung Galaxy S23 2023", "Galaxy S23"),
])
def test_colours_and_years_are_not_identity(a, b):
    assert key_of(a) is not None and key_of(a) == key_of(b)


@pytest.mark.parametrize("title", ["Tablet zu verkaufen", "Drohne zu verkaufen", "Handy", "iPhone", "Samsung Tablet",
                                   "Kopfhörer Bluetooth", "Grafikkarte", "Spielekonsole"])
def test_vague_titles_have_no_key(title):
    assert product_key(title) is None
    assert history_key(title) is None


@pytest.mark.parametrize(("title", "description", "missing", "kind"), [
    ("DeWalt DCD796 solo", "", ("akku",), "item"),
    ("Makita DHP485 nur das Grundgerät", "", ("akku",), "item"),
    ("Switch OLED nur Tablet", "", ("dock", "joycons"), "part"),
    ("Nintendo Switch Lite nur Konsole", "", (), "item"),
    ("Dyson V11 ohne Akku", "", ("akku",), "part"),
    ("MacBook Pro 14 M1 Pro ohne SSD/Festplatte", "", ("ssd",), "part"),
    ("MacBook Air M1 ohne Netzteil", "", ("netzteil",), "item"),
    ("MacBook Air M1 8GB", "Netzteil fehlt leider.", ("netzteil",), "item"),
    ("PS5 ohne Controller", "", ("controller",), "item"),
    ("PS5 Digital ohne Laufwerk", "", (), "item"),
    ("iPhone 13 ohne Akku", "", ("akku",), "part"),
    ("iPhone 13 128GB", "Kein Akku-Problem, keine Kratzer, ohne Mängel.", (), "item"),
    ("RTX 3080 ohne Kühler", "", ("kuehler",), "part"),
    ("DJI Mini 3 ohne Fernbedienung", "", ("fernbedienung",), "part"),
    ("AirPods Pro ohne Ladecase", "", ("ladecase",), "part"),
    ("Lenovo ThinkPad T480", "Festplatte wurde ausgebaut", ("ssd",), "part"),
])
def test_missing_parts(title, description, missing, kind):
    ident = identify(title, description)
    assert ident.missing == missing == missing_parts(title, description)
    assert ident.kind == kind


@pytest.mark.parametrize(("title", "hkey"), [
    ("iPhone 13 128GB Mitternacht", "iphone|13||128gb"),
    ("Lenovo ThinkPad T480 Laptop", "thinkpad|t480"),
    ("Apple Mac mini M2 Desktop Computer", "mac mini|m2"),
    ("RTX 3080 Backplate", "accessory:rtx|3080"),
    ("EKWB Wasserkühler für RTX 3080 FE", "accessory:rtx|3080"),
    ("Gaming PC RTX 3080 Ryzen 7", "complete_pc:rtx|3080"),
    ("Xbox Series S + 2 Controller + 5 Spiele", "bundle:xbox|series s"),
    ("RTX 3080 defekt", "defect:rtx|3080"),
    ("Switch OLED nur Tablet", "part:nintendo switch|oled"),
    ("Suche RTX 3080", None),
    ("Tausche PS5 gegen Xbox", None),
    ("Leere OVP RTX 3080 Karton", None),
    ("iPhone Display Reparatur Service", None),
])
def test_history_key_never_mixes_kinds(title, hkey):
    assert history_key(title) == hkey
    assert identify(title).priceable is (hkey is not None and ":" not in hkey)
