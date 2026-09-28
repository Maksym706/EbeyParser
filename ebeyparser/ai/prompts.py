"""Prompts and the JSON schema for the vision-LLM listing check.

v0.2: the local 7B model is used as a structured EXTRACTOR (item type, variant,
defects, lock, stock photos) whose fields drive hard checks in code. It is never
shown our own market estimate (no anchoring); it only sees up to 8 comparable
offers (titles + prices) and says which of them are the same variant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import Comparable, Listing, PriceEstimate

DESCRIPTION_LIMIT = 2500
MAX_PROMPT_COMPARABLES = 8
COMPARABLE_TITLE_LIMIT = 90

ITEM_TYPES = ("single", "bundle", "complete_pc", "laptop", "part", "accessory", "box_only", "wanted", "unclear")
CONDITIONS = ("new", "like_new", "good", "used", "defective", "unclear")
DEFECTS = ("screen_broken", "water_damage", "not_working", "missing_parts", "battery_bad", "locked", "other")
VARIANT_FIELDS = ("model", "storage_gb", "ram_gb", "vram_gb", "edition")
MAX_DEFECTS = 6
MAX_RED_FLAGS = 5

# Seller trust signals the Kleinanzeigen scraper stores in listing.attributes; shown on
# the seller line instead of among the item's features (same keys as SELLER_ATTRIBUTE_KEYS
# in scraper/kleinanzeigen.py, duplicated to keep this module free of scraper imports).
SELLER_ATTRIBUTE_KEYS = ("Nutzertyp", "Aktiv seit", "Bewertung", "Anzeigen des Verkäufers", "Antwortzeit")

SYSTEM_PROMPT = """\
You extract facts from ONE second-hand ad (Kleinanzeigen.de or eBay.de) for a student in Berlin \
who buys cheap items to resell or to use. Read the title and description and look at the photos. \
Report only what is written or clearly visible; when unsure use null, [] or "unclear". \
Code does the price math later, so be precise, not optimistic.

Fields:
- product: brand + exact model + key variant, e.g. "Apple iPhone 13 Pro 128GB".
- item_type: single = one complete product | bundle = several products sold together | \
complete_pc = whole desktop PC | laptop | part = spare/repair part (display, mainboard, housing) | \
accessory = case, charger, cable, controller, game | box_only = only box/packaging/invoice | \
wanted = the author wants to BUY ("Suche", "Gesuch", "Kaufe") | unclear.
- variant: model, storage_gb, ram_gb, vram_gb, edition as short strings (numbers without "GB"); \
null when unknown or not applicable.
- condition: new | like_new | good | used | defective | unclear.
- defects: only if written or clearly visible: screen_broken, water_damage, not_working, \
missing_parts, battery_bad, locked, other. At most 6, [] if none.
- locked: true if an iCloud/Google/account/activation/SIM lock or blacklist is mentioned or shown; \
false if the ad says it is unlocked/free; otherwise null.
- stock_photos: true if the photos are catalogue/press/internet pictures, not the real item; \
false for real photos; null without photos.
- photo_matches_description: true or false; null without photos.
- red_flags: at most 5 short phrases in Russian (e.g. "только предоплата", "PayPal Freunde", \
"продавец за границей", "цена подозрительно низкая", "только коробка"). [] if none.
- same_variant_indexes: numbers of the listed comparable offers that are the SAME product and \
variant (same model, storage, edition, complete item). [] if none or no list.
- estimated_market_price: typical used price in Germany in EUR for this exact item in this \
condition, or null if you do not know.
- search_query: 2-5 words a German seller would type for the same item (brand, model, variant), \
no filler words like "top", "neu", "OVP".
- reasoning: at most 2 short sentences in Russian.
- verdict: buy | maybe | skip, with confidence 0..1.

PURPOSE resale: buy only if it can be resold with a clear profit after haggling. \
PURPOSE personal: buy if the price is fair for own use. If unsure: maybe with low confidence.
Answer with ONLY one JSON object matching the schema, no markdown.
"""

_NULLABLE_STRING = {"type": ["string", "null"]}

# Strict-JSON-schema compatible (LM Studio / llama.cpp grammars, OpenAI strict mode, Claude
# structured outputs): every object closed and fully required, nullable via type arrays,
# no numeric/length/array-size constraints (limits are in the prompt and enforced by the parser).
# Property order = generation order: facts first, the verdict last.
VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "product": {"type": "string"},
        "item_type": {"type": "string", "enum": list(ITEM_TYPES)},
        "variant": {
            "type": "object",
            "properties": {name: _NULLABLE_STRING for name in VARIANT_FIELDS},
            "required": list(VARIANT_FIELDS),
            "additionalProperties": False,
        },
        "condition": {"type": "string", "enum": list(CONDITIONS)},
        "defects": {"type": "array", "items": {"type": "string", "enum": list(DEFECTS)}},
        "locked": {"type": ["boolean", "null"]},
        "stock_photos": {"type": ["boolean", "null"]},
        "photo_matches_description": {"type": ["boolean", "null"]},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "same_variant_indexes": {"type": "array", "items": {"type": "integer"}},
        "estimated_market_price": {"type": ["number", "null"]},
        "search_query": {"type": "string"},
        "reasoning": {"type": "string"},  # before the verdict: think first, decide after
        "verdict": {"type": "string", "enum": ["buy", "maybe", "skip"]},
        "confidence": {"type": "number"},
    },
    "required": [
        "product",
        "item_type",
        "variant",
        "condition",
        "defects",
        "locked",
        "stock_photos",
        "photo_matches_description",
        "red_flags",
        "same_variant_indexes",
        "estimated_market_price",
        "search_query",
        "reasoning",
        "verdict",
        "confidence",
    ],
    "additionalProperties": False,
}


def _eur(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").removesuffix(",00") + " €"


def _price_line(listing: Listing) -> str:
    if listing.is_free:
        return "Zu verschenken (0 €)"
    if listing.price is None:
        raw = listing.price_text.strip()
        return f"keine Zahl angegeben ({raw})" if raw else "nicht angegeben"
    text = _eur(listing.price)
    if listing.negotiable:
        text += " VB (verhandelbar)"
    return text


def _shipping_line(listing: Listing) -> str | None:
    if listing.shipping_cost is not None:
        return "Versand: " + ("kostenlos" if listing.shipping_cost == 0 else _eur(listing.shipping_cost))
    if listing.shipping_possible is True:
        return "Versand: möglich"
    if listing.shipping_possible is False:
        return "Versand: nur Abholung"
    return None


def _auction_lines(listing: Listing) -> list[str]:
    opts = [o.upper() for o in listing.buying_options]
    if not opts:
        return []
    lines = ["Kaufoptionen: " + ", ".join(opts)]
    if "AUCTION" in opts:
        parts = []
        if listing.bid_count is not None:
            parts.append(f"{listing.bid_count} Gebote")
        if listing.ends_at is not None:
            ends = listing.ends_at if listing.ends_at.tzinfo else listing.ends_at.replace(tzinfo=timezone.utc)
            hours = (ends - datetime.now(timezone.utc)).total_seconds() / 3600
            parts.append(f"endet {ends:%Y-%m-%d %H:%M} UTC (in ~{max(hours, 0):.0f} h)")
        lines.append(
            "Auktion: der Preis ist nur das aktuelle Gebot"
            + (" — " + ", ".join(parts) if parts else "")
        )
    return lines


def _seller_line(listing: Listing) -> str | None:
    kind = {"private": "privat", "commercial": "gewerblich"}.get(listing.seller_type)
    bits = [b for b in (listing.seller_name, kind) if b]
    if listing.seller_feedback_score is not None:
        fb = f"{listing.seller_feedback_score} Bewertungen"
        if listing.seller_feedback_percent is not None:
            fb += f", {listing.seller_feedback_percent:g}% positiv"
        bits.append(fb)
    elif listing.seller_feedback_percent is not None:
        bits.append(f"{listing.seller_feedback_percent:g}% positiv")
    for key in SELLER_ATTRIBUTE_KEYS:
        value = listing.attributes.get(key)
        if value and not (key == "Nutzertyp" and kind):
            bits.append(f"{key}: {value}")
    return "Verkäufer: " + ", ".join(bits) if bits else None


def prompt_comparables(
    estimate: PriceEstimate | None = None, comparables: list[Comparable] | None = None
) -> list[Comparable]:
    """The comparable offers shown to the model, in prompt order: `same_variant_indexes`
    in the answer are indexes into THIS list. Skips user reference prices (they would
    anchor the model), zero prices and duplicates; at most MAX_PROMPT_COMPARABLES."""
    source = comparables if comparables is not None else (estimate.comparables if estimate else [])
    out: list[Comparable] = []
    keys: set[tuple[str, float]] = set()
    for comp in source:
        if comp.source == "reference" or not comp.price or comp.price <= 0 or not comp.title.strip():
            continue
        key = (" ".join(comp.title.lower().split()), round(comp.price, 2))
        if key in keys:
            continue
        keys.add(key)
        out.append(comp)
        if len(out) >= MAX_PROMPT_COMPARABLES:
            break
    return out


def _comparable_lines(comps: list[Comparable]) -> list[str]:
    if not comps:
        return []
    lines = ["", "VERGLEICHSANGEBOTE (automatisch gefunden, oft andere Varianten, defekt oder Zubehör):"]
    for i, comp in enumerate(comps):
        title = " ".join(comp.title.split())
        if len(title) > COMPARABLE_TITLE_LIMIT:
            title = title[: COMPARABLE_TITLE_LIMIT - 1].rstrip() + "…"
        kind = "verkauft" if comp.sold or comp.source == "ebay_sold" else "Angebot"
        lines.append(f"[{i}] {title} — {_eur(comp.price)} ({kind})")
    lines.append("Put the numbers of offers that are exactly the same product and variant into same_variant_indexes.")
    return lines


def build_user_prompt(
    listing: Listing,
    *,
    purpose: str = "resale",
    estimate: PriceEstimate | None = None,
    target_price: float | None = None,
    n_images: int | None = None,
    comparables: list[Comparable] | None = None,
) -> str:
    """The listing as-is (German), numbered comparable offers and the buyer's goal.

    Our own market estimate is deliberately NOT shown (no anchoring). `comparables`
    must be the list from prompt_comparables(); without it the first comparables of
    `estimate` are used the same way."""
    source = "eBay.de" if listing.source == "ebay" else "Kleinanzeigen.de"
    lines = [f"ANZEIGE ({source})", f"Titel: {listing.title}", f"Preis: {_price_line(listing)}"]
    if listing.condition:
        lines.append(f"Zustand (laut Anzeige): {listing.condition}")
    shipping = _shipping_line(listing)
    if shipping:
        lines.append(shipping)
    lines += _auction_lines(listing)
    if listing.location:
        lines.append(f"Ort: {listing.location}")
    features = {k: v for k, v in listing.attributes.items() if k not in SELLER_ATTRIBUTE_KEYS}
    if features:
        lines.append("Merkmale: " + "; ".join(f"{k}: {v}" for k, v in features.items()))
    if listing.tags:
        lines.append("Tags: " + ", ".join(listing.tags))
    seller = _seller_line(listing)
    if seller:
        lines.append(seller)
    desc = " ".join(listing.description.split()) if listing.description else ""
    if len(desc) > DESCRIPTION_LIMIT:
        desc = desc[:DESCRIPTION_LIMIT].rstrip() + " […gekürzt]"
    lines += ["Beschreibung:", f'"""{desc or "(keine Beschreibung)"}"""']
    count = len(listing.image_urls) if n_images is None else n_images
    total = len(listing.image_urls)
    photos = f"Fotos: {count} angehängt"
    if total > count:
        photos += f" (von {total} in der Anzeige)"
    if count == 0:
        photos = ("Fotos: keine angehängt — Fotos nicht beurteilbar "
                  "(photo_matches_description = null, stock_photos = null)")
    lines.append(photos)

    comps = comparables if comparables is not None else prompt_comparables(estimate)
    lines += _comparable_lines(comps)

    lines.append("")
    if purpose == "personal":
        goal = "PURPOSE: personal — the buyer wants this item for their OWN use, not for resale."
        if target_price:
            goal += f" Target price: at most {_eur(target_price)} (including shipping)."
        lines.append(goal)
    else:
        lines.append("PURPOSE: resale — the buyer wants to resell it in Germany for a net profit.")
    lines.append("Answer with ONLY the JSON object (reasoning and red_flags in Russian).")
    return "\n".join(lines)
