"""Prompts and the JSON schema for the vision-LLM listing check."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import Listing, PriceEstimate

DESCRIPTION_LIMIT = 2500

SYSTEM_PROMPT = """\
You are an experienced reseller in Germany. You know used-market prices on Kleinanzeigen.de \
and eBay.de very well and you are good at spotting bad deals and scams. You check one \
classified ad for a buyer who has little money and cannot afford mistakes.

Do the following, using the title, the description AND the attached photos:
1. Identify the exact product: brand, model, variant, capacity/size (e.g. \
"NVIDIA GeForce RTX 3090 24GB, Gigabyte Gaming OC"). If you cannot tell, say what is known.
2. Write a short `search_query` (max 5 words, brand + model terms as a German seller would \
type them, no filler words like "top", "neu", "OVP") to find comparable offers.
3. Check whether the photos match the description: is it the same product? Real photos of \
the item, or stock/internet/press images? Visible damage that the text does not mention? \
Only the box or packaging? Set `photo_matches_description` to true/false, or null if there \
are no usable photos.
4. Assess the condition: new, like_new, good, used, defective or unclear.
5. List red flags (in Russian): scam signals (prepayment only, PayPal Friends, moving to \
WhatsApp, seller abroad), hints of defects, missing parts or accessories, stock photos, \
price too good to be true, box only, locked device (iCloud/account), replica. Empty list if none.
6. Estimate the typical used-market resale price in EUR in Germany for this exact item in \
this condition (`estimated_market_price`, a number, or null if you really cannot tell). \
Prefer the market data given in the message when it matches the product.
7. Give a verdict: "buy", "maybe" or "skip", a `confidence` between 0 and 1, and a short \
`reasoning` of 2-4 sentences.

Purpose "resale": judge whether the item can be resold with a real net profit after \
haggling and effort. Purpose "personal": judge whether the price is good value for the \
buyer's own use (a fair price for a working item is enough; no resale profit needed).

Rules:
- The `reasoning` and every `red_flags` entry MUST be written in Russian.
- Be conservative: if unsure, answer "maybe" with a lower confidence.
- Never invent details that are not visible or written. No photos -> say so.
- Answer with ONLY one JSON object matching the schema, no markdown, no extra text.
"""

VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "product": {"type": "string"},
        "search_query": {"type": "string"},
        "photo_matches_description": {"type": ["boolean", "null"]},
        "condition": {
            "type": "string",
            "enum": ["new", "like_new", "good", "used", "defective", "unclear"],
        },
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "estimated_market_price": {"type": ["number", "null"]},
        "reasoning": {"type": "string"},  # before the verdict: think first, decide after
        "verdict": {"type": "string", "enum": ["buy", "maybe", "skip"]},
        "confidence": {"type": "number"},
    },
    "required": [
        "product",
        "search_query",
        "photo_matches_description",
        "condition",
        "red_flags",
        "estimated_market_price",
        "reasoning",
        "verdict",
        "confidence",
    ],
    "additionalProperties": False,
}

_SOURCE_NAMES = {
    "ebay_sold": "verkaufte eBay-Artikel (echte Verkaufspreise)",
    "kleinanzeigen": "aktuelle Angebote (Angebotspreise, leicht abgezinst)",
    "mixed": "verkaufte eBay-Artikel + aktuelle Angebote",
    "reference": "vom Nutzer hinterlegter Referenzpreis",
    "ai": "frühere KI-Schätzung",
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
    return "Verkäufer: " + ", ".join(bits) if bits else None


def build_user_prompt(
    listing: Listing,
    *,
    purpose: str = "resale",
    estimate: PriceEstimate | None = None,
    target_price: float | None = None,
    n_images: int | None = None,
) -> str:
    """The listing as-is (German) plus market data and the buyer's goal."""
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
    if listing.attributes:
        lines.append("Merkmale: " + "; ".join(f"{k}: {v}" for k, v in listing.attributes.items()))
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
        photos = "Fotos: keine angehängt — Fotos nicht beurteilbar (photo_matches_description = null)"
    lines.append(photos)

    if estimate is not None and estimate.market_price:
        lines += ["", "MARKTDATEN (automatisch gefunden, können falsche Treffer enthalten):"]
        rng = ""
        if estimate.low and estimate.high:
            rng = f" (Spanne {_eur(estimate.low)} – {_eur(estimate.high)})"
        lines.append(f"Typischer Wiederverkaufspreis: ~{_eur(estimate.market_price)}{rng}")
        lines.append(
            f"Basis: {estimate.sample_size} Vergleichsangebote, Quelle: "
            f"{_SOURCE_NAMES.get(estimate.source, estimate.source)}"
        )
        comps = estimate.comparables[:5]
        if comps:
            lines.append("Beispiele:")
            for c in comps:
                kind = "verkauft" if c.sold or c.source == "ebay_sold" else "Angebot"
                lines.append(f"- {c.title} — {_eur(c.price)} ({kind})")

    lines.append("")
    if purpose == "personal":
        goal = "PURPOSE: personal — the buyer wants this item for their OWN use, not for resale."
        if target_price:
            goal += f" Target price: at most {_eur(target_price)} (including shipping)."
        lines.append(goal)
    else:
        lines.append(
            "PURPOSE: resale — the buyer wants to resell it in Germany for a net profit."
        )
    lines.append(
        "Answer with ONLY the JSON object (reasoning and red_flags in Russian)."
    )
    return "\n".join(lines)
