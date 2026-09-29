"""Prompt and JSON schema of the AI scout's triage (stage A, docs/design/AI_SCOUT.md).

Written for SMALL local models (1.5-4B on a CPU) as much as for a 7B:

* one flat object per ad, one-letter keys, enums instead of free text where possible;
* the answer is {"items": [...]} with the ad's index "i" in every item (never matched by order);
* facts first (kind, product, contents), the interest score and the reason last;
* NO prices: small models invent them. Money comes from our own price history (stage B);
* one short worked example (the system prompt is identical for every call, so llama.cpp /
  Ollama / LM Studio keep it in the prompt cache and it costs nothing after the first call).
"""

from __future__ import annotations

from ..models import Listing

KINDS = ("single", "bundle", "pc", "lot", "part", "acc", "box", "wanted", "swap", "service", "defect", "other")
# what the MODEL sees: "sale" is the explicit default (the research's measured prompt, AI_MODELS.md §5);
# the parser maps it back to the internal "single"
PROMPT_KINDS = ("sale",) + KINDS[1:]
CONDITIONS = ("new", "good", "used", "defect", "unknown")
HIDDEN_TAGS = ("typo", "vague", "unknown_model", "pc_parts", "lot", "bundle", "wrong_category", "cheap")
RISK_TAGS = ("scam", "defect", "locked", "fake", "missing", "reserved", "rent")
MAX_CONTENTS = 8
TITLE_LIMIT = 120
TEXT_LIMIT = 320  # characters of the ad text per ad (the search card shows ~150 anyway)

SYSTEM_PROMPT = """\
You read second-hand ads from Kleinanzeigen.de and eBay.de for a student in Berlin who buys \
cheap things to resell. For EVERY ad return one object. Report only what the ad says; never \
guess prices. Keys:
i: the ad number [i].
k: kind. Default: sale (one product for sale). Use another kind only when the ad says so:
wanted = Suche, Suche nach, Kaufe, Gesucht
swap = Tausche, Tausch, nur Tausch
defect = defekt, kaputt, für Bastler, iCloud gesperrt
part = Ersatzteil, als Ersatzteil (one component, not the whole device)
box = nur OVP, nur Karton, leere Verpackung
bundle = a product with extras: Paket, Set, Bundle, "mit 2 Controllern und Spielen"
lot = Konvolut, Sammlung, Nachlass, Kiste mit Technik (many different things)
pc = a whole computer: Gaming PC, Rechner, Tower (list its parts in c)
acc = only an accessory: Hülle, Kabel, Ladegerät
service = Reparatur, Dienstleistung
other = not electronics or tools: furniture, clothes, toys
p: the exact product: brand model variant storage, e.g. "Apple iPhone 13 Pro 256GB", \
"NVIDIA RTX 3080 10GB". Copy model numbers and sizes exactly as the ad writes them; fix typos \
("Iphne" -> "iPhone"). Use the text, not only the title. "" if no model is named.
n: how many of p (1 if one).
c: for bundle, pc and lot: the valuable items inside with model names, e.g. ["RTX 3070", \
"Ryzen 5 3600", "2x DualSense Controller"]. [] otherwise.
q: 2-5 German words to search this product on Kleinanzeigen, e.g. "iphone 13 pro 256gb".
z: condition: new | good | used | defect | unknown.
h: hidden value tags: typo (misspelled brand/model) | vague (title hides what it is) | \
unknown_model (seller does not know the model) | pc_parts (valuable parts inside a PC) | \
lot | bundle | wrong_category | cheap (price looks very low for this item). [] if none.
x: risk tags: scam (Vorkasse, only shipping, WhatsApp, Telegram, e-mail, link, too good) | defect | \
locked (iCloud/account lock) | fake (replica) | missing (important part missing) | reserved | rent. [] if none.
s: interest 0-10 for reselling: 9-10 valuable item hidden or far too cheap; 6-8 known \
valuable product, resellable; 3-5 ordinary; 0-2 junk, wanted, swap, service, broken, scam.
r: reason in Russian, at most 8 words, in your own words. Never copy German text from the ad.
Answer ONLY with minified JSON on one line (no line breaks, no indentation): {"items":[{...},...]}.

Example ads:
[0] Titel: Alter Rechner | Preis: 150 € VB | Text: PC von meinem Sohn, i7 8700k, Grafikkarte \
RTX 3070, 16GB RAM, läuft
[1] Titel: Iphne 12 64gb | Preis: 180 € | Text: Akku 86%, kleine Kratzer
[2] Titel: Suche PS5 | Preis: VB | Text: Suche PS5 Disc bis 300€
Example answer:
{"items":[{"i":0,"k":"pc","p":"","n":1,"c":["RTX 3070","Intel Core i7-8700K","16GB DDR4 RAM"],\
"q":"gaming pc rtx 3070","z":"used","h":["pc_parts","vague"],"x":[],"s":9,"r":"старый ПК, внутри RTX 3070"},\
{"i":1,"k":"sale","p":"Apple iPhone 12 64GB","n":1,"c":[],"q":"iphone 12 64gb","z":"used",\
"h":["typo"],"x":[],"s":6,"r":"iPhone 12, опечатка в названии"},\
{"i":2,"k":"wanted","p":"Sony PS5 Disc","n":1,"c":[],"q":"ps5 disc","z":"unknown","h":[],"x":[],\
"s":0,"r":"ищет, а не продаёт"}]}
"""

_ITEM_PROPERTIES: dict = {
    "i": {"type": "integer"},
    "k": {"type": "string", "enum": list(PROMPT_KINDS)},
    "p": {"type": "string"},
    "n": {"type": "integer"},
    "c": {"type": "array", "items": {"type": "string"}},
    "q": {"type": "string"},
    "z": {"type": "string", "enum": list(CONDITIONS)},
    "h": {"type": "array", "items": {"type": "string", "enum": list(HIDDEN_TAGS)}},
    "x": {"type": "array", "items": {"type": "string", "enum": list(RISK_TAGS)}},
    "s": {"type": "integer"},
    "r": {"type": "string"},
}

# Strict-schema compatible (llama.cpp grammar, LM Studio, vLLM, OpenAI strict): closed objects,
# every key required, no numeric/length limits (the parser enforces those).
TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _ITEM_PROPERTIES,
                "required": list(_ITEM_PROPERTIES),
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _eur(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".") + " €"


def price_text(listing: Listing) -> str:
    if listing.is_free:
        return "zu verschenken"
    if listing.price is None or listing.price <= 0:
        raw = " ".join((listing.price_text or "").split())
        return raw or "VB"
    text = _eur(listing.price)
    if listing.negotiable:
        text += " VB"
    if listing.buying_options and "AUCTION" in [o.upper() for o in listing.buying_options]:
        text += " (Auktion, aktuelles Gebot)"
    return text


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def ad_block(index: int, listing: Listing, *, category: str = "") -> str:
    """One ad, compact: '[3] Titel: … | Preis: … | Kategorie: … | Text: …'."""
    parts = [f"[{index}] Titel: {_clip(listing.title, TITLE_LIMIT)}", f"Preis: {price_text(listing)}"]
    if category:
        parts.append(f"Kategorie: {_clip(category, 40)}")
    if listing.source == "ebay":
        parts.append("eBay")
    text = _clip(listing.description, TEXT_LIMIT)
    if text:
        parts.append(f"Text: {text}")
    return " | ".join(parts)


def build_user_prompt(listings: list[Listing], *, categories: list[str] | None = None,
                      hints: str = "") -> str:
    """The batch: numbered ads (0..n-1), then the reminder of the answer format.
    `hints`: the user's preferences learned from feedback (see triage.feedback_hints)."""
    cats = categories or [""] * len(listings)
    lines = ["Ads:"]
    lines += [ad_block(i, listing, category=cats[i] if i < len(cats) else "") for i, listing in enumerate(listings)]
    if hints:
        lines += ["", hints.strip()]
    lines += ["", f'Return {{"items": [...]}} with exactly {len(listings)} objects, i = 0..{len(listings) - 1}.']
    return "\n".join(lines)
