"""DealView / SearchConfig -> JSON for the API. Plain numbers (the UI formats them), ISO
timestamps (UTC), plus a few ready Russian labels."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, overload

from ...config import AppConfig, SearchConfig
from ...models import AIVerdict, DealView, Evaluation, Listing, utcnow
from ...timefmt import local_tz, when_label

# the glossary of the UI (§7.2): Покупай / Подумай / Не выгодно
VERDICT_LABELS = {"buy": "Покупай", "maybe": "Подумай", "skip": "Не выгодно", "none": "Не оценено"}
ACTION_LABELS = {
    "buy": "Брать по цене",
    "haggle": "Торговаться",
    "bid": "Ставить на аукционе",
    "watch": "Наблюдать",
    "skip": "Пропустить",
    "": "—",
}
# API statuses: the pipeline Избранное → Написал → Купил → Продал (+ new / ignored)
STATUS_LABELS = {
    "new": "Новое",
    "starred": "Избранное",
    "contacted": "Написал продавцу",
    "bought": "Купил",
    "sold": "Продал",
    "ignored": "Скрыто",
}
API_STATUSES = ("new", "starred", "contacted", "bought", "sold", "ignored")
PIPELINE = ("starred", "contacted", "bought", "sold")
PURPOSE_LABELS = {"resale": "Перепродажа", "personal": "Для себя"}
SOURCE_LABELS = {"kleinanzeigen": "Kleinanzeigen", "ebay": "eBay"}
CONDITION_LABELS = {
    "new": "Новое",
    "like_new": "Как новое",
    "good": "Хорошее",
    "used": "Б/у, есть следы использования",
    "defective": "Неисправно / на запчасти",
    "unclear": "Неясно по фото",
}
ITEM_TYPE_LABELS = {
    "single": "Один товар", "bundle": "Комплект", "complete_pc": "Компьютер целиком", "laptop": "Ноутбук",
    "part": "Запчасть", "accessory": "Аксессуар", "box_only": "Только коробка", "wanted": "Поиск/покупка",
    "unclear": "Неясно",
}
COMP_SOURCE_LABELS = {
    "ebay_sold": "eBay · продано",
    "ebay": "eBay · цена",
    "kleinanzeigen": "Kleinanzeigen",
    "reference": "Справочная",
    "history": "История цен",
    "other": "Другое",
}
PRICE_SOURCE_LABELS = {
    "reference": "справочная цена из конфига",
    "history": "история цен",
    "kleinanzeigen": "объявления Kleinanzeigen",
    "ebay_sold": "реальные продажи eBay",
    "mixed": "продажи eBay + объявления",
    "ai": "оценка ИИ",
    "none": "нет данных",
}
SELLER_TYPE_LABELS = {"private": "Частное лицо", "commercial": "Коммерческий продавец", "unknown": "Не указано"}
BUYING_OPTION_LABELS = {"FIXED_PRICE": "Купить сейчас", "AUCTION": "Аукцион", "BEST_OFFER": "Предложить цену"}
STAGE_LABELS = {"": "—", "prefilter": "Отсеяно фильтром", "market": "Оценено по рынку", "full": "Полная проверка",
                "expired": "Не успели проверить"}


# the user's time zone lives in ebeyparser.timefmt (general.timezone, default Europe/Berlin)
LOCAL_TZ: Any = local_tz()  # the default zone; code uses local_tz() so a changed setting applies


# German words of the sites -> Russian (the UI must not show "Heute", "Gut", "Zustand")
CONDITION_DE_RU = {
    "neu": "Новое", "neu mit etikett": "Новое с биркой", "neuwertig": "Как новое", "wie neu": "Как новое",
    "sehr gut": "Очень хорошее", "gut": "Хорошее", "in ordnung": "Нормальное", "akzeptabel": "Нормальное",
    "gebraucht": "Б/у", "defekt": "Неисправно", "als ersatzteil / defekt": "На запчасти / неисправно",
    "generalüberholt": "Восстановленное", "gebraucht – sehr gut": "Б/у, очень хорошее",
    "gebraucht – gut": "Б/у, хорошее", "gebraucht – akzeptabel": "Б/у, нормальное",
}
ATTRIBUTE_KEYS_RU = {
    "art": "Тип", "zustand": "Состояние", "versand": "Доставка", "marke": "Бренд", "modell": "Модель",
    "farbe": "Цвет", "größe": "Размер", "speichergröße": "Память", "speicherkapazität": "Объём памяти",
    "speichertyp": "Тип памяти", "kapazität": "Ёмкость", "hersteller": "Производитель", "typ": "Тип",
    "material": "Материал", "rahmengröße": "Размер рамы", "anzahl": "Количество", "produktart": "Тип товара",
}
ATTRIBUTE_VALUES_RU = {
    "versand möglich": "можно с доставкой", "nur abholung": "только самовывоз", "nur versand": "только доставка",
}


def condition_ru(text: str) -> str:
    """'Gut' -> 'Хорошее', 'Gebraucht – Sehr gut' -> 'Б/у, очень хорошее' (unknown: as is)."""
    raw = " ".join((text or "").split())
    return CONDITION_DE_RU.get(raw.lower().replace(" - ", " – "), raw)


def day_words_ru(text: str) -> str:
    """'Heute, 19:35' -> 'сегодня, 19:35'; 'Gestern' -> 'вчера'; 'Verkauft 15.09.2026' -> '15.09.2026'."""
    out = " ".join((text or "").split())
    out = re.sub(r"^(?:Verkauft|Verkauft am)\s+", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bHeute\b", "сегодня", out)
    out = re.sub(r"\bGestern\b", "вчера", out)
    out = re.sub(r"\bSofort-Kaufen\b", "купить сразу", out)
    return out


def attributes_ru(attributes: dict[str, str]) -> list[dict[str, str]]:
    """[{key, key_ru, value, value_ru}] — the seller's description table in Russian."""
    rows = []
    for key, value in attributes.items():
        value_ru = ATTRIBUTE_VALUES_RU.get(str(value).strip().lower())
        if value_ru is None and key.strip().lower() == "zustand":
            value_ru = condition_ru(str(value))
        rows.append({"key": key, "key_ru": ATTRIBUTE_KEYS_RU.get(key.strip().lower(), key), "value": value,
                     "value_ru": value_ru or str(value)})
    return rows


# --------------------------------------------------------------------- helpers
@overload
def aware(dt: datetime) -> datetime: ...


@overload
def aware(dt: None) -> None: ...


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    value = aware(dt)
    return value.isoformat() if value is not None else None


def rnd(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})
_CYRILLIC = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
     "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))


def slugify(text: str) -> str:
    """'Handy & Telefon · Berlin 30 км' -> 'handy-telefon-berlin-30-km'."""
    text = (text or "").translate(_TRANSLIT).lower()
    text = "".join(_CYRILLIC.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug[:80].strip("-") or "search"


def search_ids(searches: Iterable[SearchConfig]) -> list[str]:
    """Stable ids for the searches of a config, in order: the name's slug, '-2', '-3', ... for
    names that only differ in punctuation."""
    ids: list[str] = []
    used: set[str] = set()
    for search in searches:
        base = slugify(search.name)
        sid, n = base, 2
        while sid in used:
            sid = f"{base}-{n}"
            n += 1
        used.add(sid)
        ids.append(sid)
    return ids


def search_id_for(config: AppConfig, name: str) -> str | None:
    for search, sid in zip(config.searches, search_ids(config.searches)):
        if search.name == name:
            return sid
    return None


def api_status(deal: DealView, extras: dict[str, Any] | None = None) -> str:
    return deal.status


def is_auction(listing: Listing) -> bool:
    return "AUCTION" in {str(o).upper() for o in (listing.buying_options or [])}


def derive_action(listing: Listing, ev: Evaluation | None) -> str:
    """What to do with a deal — never empty in API output (older evaluations and demo data
    have action ""): an auction is always "bid" (never "buy"), a buy with an offer on a VB /
    best-offer ad is "haggle", buy -> "buy", maybe -> "watch", skip -> "skip". The same rule
    is the SQL of Database.ACTION_SQL (the ?action= filter and the facets)."""
    if ev is None:
        return "watch"
    action = ev.action or ""
    auction = is_auction(listing)
    if action:
        return "bid" if auction and action in ("buy", "haggle") else action
    if ev.verdict == "skip":
        return "skip"
    if auction:
        return "bid" if (ev.max_buy_price or 0) > 0 else "watch"
    negotiable = listing.negotiable or "BEST_OFFER" in {str(o).upper() for o in (listing.buying_options or [])}
    if ev.verdict == "buy" and ev.offer_price is not None and negotiable:
        return "haggle"
    if ev.verdict == "buy":
        return "buy"
    return "watch"


def market_price(ev: Evaluation | None) -> float | None:
    if ev is None:
        return None
    if ev.estimate.market_price is not None:
        return ev.estimate.market_price
    return ev.ai.estimated_market_price if ev.ai else None


def offer_terms(ev: Evaluation | None, listing: Listing) -> tuple[float, float | None, float | None] | None:
    """Haggle deals: (offer, profit/savings at the offer, ROI at the offer) — as the notifier."""
    from ...notify.render import offer_terms as render_offer_terms

    if ev is not None and ev.action != "haggle" and derive_action(listing, ev) == "haggle":
        ev = ev.model_copy(update={"action": "haggle"})  # an old / demo evaluation without an action
    return render_offer_terms(ev, listing)


def ai_flags(ev: Evaluation | None) -> list[str]:
    """Warnings from the AI verdicts (red flags + structured findings)."""
    if ev is None:
        return []
    flags: list[str] = []
    for verdict in (ev.ai, ev.ai_second):
        if verdict is None:
            continue
        flags += verdict.red_flags
        if verdict.locked:
            flags.append("Похоже на блокировку (iCloud / аккаунт)")
        if verdict.stock_photos:
            flags.append("Фото похожи на картинки из интернета")
        if verdict.photo_matches_description is False:
            flags.append("Фото не совпадают с описанием")
        flags += [f"Дефект: {d}" for d in verdict.defects]
    return dedupe(flags)


def realized_profit(status: str, extras: dict[str, Any] | None) -> float | None:
    """Real profit of a sold deal: sold − bought − extra costs (fees, shipping)."""
    if status != "sold" or not extras:
        return None
    bought, sold = extras.get("bought_price"), extras.get("sold_price")
    if bought is None or sold is None:
        return None
    return rnd(float(sold) - float(bought) - float(extras.get("extra_costs") or 0.0))


PRODUCT_TYPES = {
    "phone": "phone", "tablet": "phone", "watch": "phone", "gpu": "gpu", "laptop": "laptop",
    "console": "console", "handheld": "console",
}
_TYPE_WORDS = (
    ("phone", ("iphone", "galaxy", "pixel", "smartphone", "handy", "xiaomi", "oneplus", "ipad")),
    ("gpu", ("rtx", "gtx", "radeon", "grafikkarte", " rx ")),
    ("laptop", ("notebook", "laptop", "macbook", "thinkpad", "ultrabook")),
    ("console", ("playstation", "ps5", "ps4", "xbox", "nintendo", "switch", "steam deck")),
)


def product_type(listing: Listing, ev: Evaluation | None = None) -> str:
    """phone | gpu | laptop | console | other — for the "check at pickup" list and message add-ons."""
    try:
        from ...pricing.estimator import listing_identity

        category = listing_identity(listing).category
    except Exception:  # noqa: BLE001
        category = None
    if category in PRODUCT_TYPES:
        return PRODUCT_TYPES[category]
    text = f" {listing.title} {(ev.ai.product if ev and ev.ai else '')} ".lower()
    for kind, words in _TYPE_WORDS:
        if any(w in text for w in words):
            return kind
    return "other"


# ----------------------------------------------------------------------- cards
def deal_card(deal: DealView, extras: dict[str, Any] | None = None, *, config: AppConfig | None = None) -> dict[str, Any]:
    """Everything a deal card in a list needs."""
    listing, ev = deal.listing, deal.evaluation
    source = listing.source or "kleinanzeigen"
    verdict = ev.verdict if ev else "none"
    action = derive_action(listing, ev)
    purpose = ev.purpose if ev else "resale"
    status = api_status(deal, extras)
    images = [u for u in listing.image_urls if u]
    market = market_price(ev)
    terms = offer_terms(ev, listing)
    auction = is_auction(listing)
    extras = extras or {}
    card: dict[str, Any] = {
        "id": listing.ad_id,
        "url": listing.url,
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "title": listing.title,
        "image": images[0] if images else None,
        "images_count": len(images),
        "price": rnd(listing.price),
        "price_text": listing.price_text,
        "is_free": listing.is_free,
        "negotiable": listing.negotiable,
        "shipping_cost": rnd(listing.shipping_cost),
        "shipping_possible": listing.shipping_possible,
        "buy_price": rnd(ev.buy_price) if ev else rnd(listing.price),
        "market_price": rnd(market),
        "market_low": rnd(ev.estimate.low) if ev else None,
        "market_high": rnd(ev.estimate.high) if ev else None,
        "market_source": ev.estimate.source if ev else "none",
        "market_source_label": PRICE_SOURCE_LABELS.get(ev.estimate.source if ev else "none", ""),
        "profit": rnd(ev.expected_profit) if ev else None,
        "profit_kind": "savings" if purpose == "personal" else "profit",
        "roi": rnd(ev.roi, 4) if ev else None,
        "offer_price": rnd(ev.offer_price) if ev else None,
        "profit_at_offer": rnd(terms[1]) if terms else None,
        "roi_at_offer": rnd(terms[2], 4) if terms else None,
        "max_buy_price": rnd(ev.max_buy_price) if ev else None,
        "action": action,
        "action_label": ACTION_LABELS.get(action, action),
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        "score": round(ev.score) if ev else None,
        "reasons": list(ev.reasons[:3]) if ev else [],
        "red_flags": dedupe(ev.red_flags) if ev else [],
        "ai_flags": ai_flags(ev),
        "ai_checked": ev.ai_checked if ev else None,
        "ai_verdict": ev.ai.verdict if ev and ev.ai else None,
        "ai_confidence": rnd(ev.ai.confidence, 2) if ev and ev.ai else None,
        "ai_summary": (ev.ai.reasoning if ev and ev.ai else "") or "",
        "unchecked": bool(ev and ev.ai_checked is False),
        "would_buy": bool(ev and ev.would_buy),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "note": deal.note,
        "bought_price": rnd(extras.get("bought_price")),
        "sold_price": rnd(extras.get("sold_price")),
        "extra_costs": rnd(extras.get("extra_costs")),
        "realized_profit": realized_profit(status, extras),
        "hidden_reason": extras.get("hidden_reason"),
        "bought_at": iso(extras.get("bought_at")),
        "sold_at": iso(extras.get("sold_at")),
        "contacted_at": iso(extras.get("contacted_at")),
        "seen": extras.get("seen_at") is not None,
        "status_changed_at": iso(extras.get("updated_at")),
        "notified": deal.notified,
        "location": listing.location,
        "postal_code": listing.postal_code,
        "distance_km": rnd(listing.distance_km, 1),
        "posted_at_text": listing.posted_at_text,
        "posted_at_ru": day_words_ru(listing.posted_at_text),
        "first_seen": iso(listing.first_seen),
        "evaluated_at": iso(ev.evaluated_at) if ev else None,
        "stage": ev.stage if ev else "",
        "search_name": listing.search_name,
        "search_id": search_id_for(config, listing.search_name) if config is not None else None,
        "purpose": purpose,
        "purpose_label": PURPOSE_LABELS.get(purpose, purpose),
        "condition": (listing.condition or listing.attributes.get("Zustand", "")).strip(),
        "condition_ru": condition_ru(listing.condition or listing.attributes.get("Zustand", "")),
        "seller_type": listing.seller_type,
        "buying_options": list(listing.buying_options or []),
        "auction": {"bid_count": listing.bid_count, "ends_at": iso(listing.ends_at),
                    "ends_at_label": when_label(listing.ends_at) if listing.ends_at else ""} if auction else None,
        "first_seen_label": when_label(listing.first_seen),
    }
    return card


# ------------------------------------------------------------ profit breakdown
def profit_breakdown(deal: DealView, config: AppConfig) -> dict[str, Any]:
    """Rows market -> safety margin -> fees -> shipping -> buy price -> net, as numbers
    (negative = cost). `total` is the stored expected profit; a gap is shown as its own row."""
    ev = deal.evaluation
    if ev is None:
        return {"available": False, "rows": [], "total": None, "reason_ru": "Объявление ещё не оценено"}
    market = market_price(ev)
    buy = ev.buy_price if ev.buy_price is not None else deal.listing.price
    if market is None or buy is None:
        why = "Рыночная цена неизвестна" if market is None else "Цена не указана"
        return {"available": False, "rows": [], "total": None, "reason_ru": why}
    pricing = config.pricing
    shipping_in = deal.listing.shipping_cost or 0.0
    rows: list[dict[str, Any]] = [{
        "key": "market", "label": "Рыночная цена", "value": rnd(market),
        "note": _market_note(ev),
    }]
    if ev.purpose == "personal":
        rows.append({"key": "buy", "label": "Цена покупки", "value": rnd(-buy),
                     "note": f"вкл. доставку {_euro(shipping_in)}" if shipping_in else ""})
        computed = market - buy
        total_label = "Экономия"
    else:
        margin_pct = pricing.safety_margin_percent
        margin = market * margin_pct / 100
        fee_pct = pricing.selling_fee_percent + pricing.payment_fee_percent
        rows += [
            {"key": "margin", "label": f"Запас на торг и риск ({margin_pct:g} %)", "value": rnd(-margin), "note": ""},
            {"key": "fees", "label": "Комиссии при продаже", "value": rnd(-ev.fees),
             "note": f"{fee_pct:g} %" if fee_pct else "частным продавцам 0 %"},
            {"key": "shipping", "label": "Твоя доставка покупателю", "value": rnd(-ev.shipping_cost), "note": ""},
            {"key": "buy", "label": "Цена покупки", "value": rnd(-buy),
             "note": f"вкл. доставку {_euro(shipping_in)}" if shipping_in else ""},
        ]
        computed = market - margin - ev.fees - ev.shipping_cost - buy
        total_label = "Чистая прибыль"
    total = ev.expected_profit if ev.expected_profit is not None else computed
    if abs(total - computed) >= 0.5:
        rows.append({"key": "other", "label": "Прочие поправки оценки", "value": rnd(total - computed), "note": ""})
    if total < 0:
        total_label = "Переплата" if ev.purpose == "personal" else "Убыток"
    roi = ev.roi if ev.roi is not None else (total / buy if buy else None)
    return {
        "available": True,
        "rows": rows,
        "total": rnd(total),
        "total_label": total_label,
        "roi": rnd(roi, 4),
        "purpose": ev.purpose,
        "market_source": ev.estimate.source,
        "market_source_label": PRICE_SOURCE_LABELS.get(ev.estimate.source, ev.estimate.source),
        "notes": ev.estimate.notes,
        "query": ev.estimate.query,
    }


def _market_note(ev: Evaluation) -> str:
    est = ev.estimate
    if est.sample_size:
        note = f"{est.sample_size} сравн."
        if est.low is not None and est.high is not None:
            note += f", {round(est.low)}–{round(est.high)} €"
        return note
    if est.market_price is None and ev.ai:
        return "оценка ИИ"
    return PRICE_SOURCE_LABELS.get(est.source, "")


# -------------------------------------------------------------- seller message
def _euro(value: float) -> str:
    from ...notify.render import format_money

    return format_money(value)


def _round_down(value: float) -> float:
    return float(5 * math.floor(value / 5)) if value >= 10 else round(max(value, 0.0), 2)


def seller_message(deal: DealView) -> dict[str, Any]:
    """A ready German message to the seller, using the suggested offer / the max price that
    still pays off. kind: haggle | offer | buy | question | bid."""
    listing, ev = deal.listing, deal.evaluation
    title = " ".join(listing.title.split())[:90] or "Ihren Artikel"
    quoted = f"„{title}“"
    price = listing.price
    auction = "AUCTION" in (listing.buying_options or [])
    pickup = listing.source == "kleinanzeigen" and (listing.distance_km is None or listing.distance_km <= 60)
    if pickup:
        logistics = "Ich könnte es zeitnah abholen und bar bezahlen."
    elif listing.shipping_possible or listing.source == "ebay":
        logistics = "Versand wäre für mich in Ordnung (gern auch versichert)."
    else:
        logistics = "Ich könnte es zeitnah abholen."
    questions = "Funktioniert alles einwandfrei, und gibt es Mängel, die auf den Fotos nicht zu sehen sind?"
    offer = ev.offer_price if ev is not None else None
    max_buy = ev.max_buy_price if ev is not None else None
    kind = "question"
    note_ru = ""
    if auction:
        kind = "bid"
        body = f"ich interessiere mich für {quoted}. {questions}"
        if max_buy:
            note_ru = f"Аукцион: ставь не выше {_euro(max_buy)} — дороже уже невыгодно"
    elif listing.is_free:
        kind = "question"
        body = f"ist {quoted} noch zu haben? {logistics}"
    elif offer is not None and (price is None or offer < price):
        kind = "haggle"
        body = (f"ich interessiere mich für {quoted}. Wäre {_euro(offer)} für Sie in Ordnung? "
                f"{logistics}")
        note_ru = f"Предложи {_euro(offer)}" + (f", максимум {_euro(max_buy)}" if max_buy and max_buy > offer else "")
    elif price is not None and max_buy is not None and 0 < max_buy < price:
        kind = "offer"
        offer = _round_down(max_buy)
        body = (f"ich interessiere mich für {quoted}. Wäre {_euro(offer)} für Sie machbar? "
                f"{logistics}")
        note_ru = f"Выгодно только до {_euro(max_buy)} — предложи {_euro(offer)}"
    elif price is not None:
        kind = "buy"
        offer = price
        body = (f"ist {quoted} noch verfügbar? Ich würde es gern zum angegebenen Preis von {_euro(price)} "
                f"nehmen. {logistics}")
        note_ru = "Цена и так выгодная — главное успеть первым"
    else:
        body = f"ist {quoted} noch verfügbar? Was wäre Ihr Preis? {logistics}"
    text = f"Hallo,\n\n{body}\n\nViele Grüße"
    return {"language": "de", "kind": kind, "text": text, "offer_price": rnd(offer), "max_price": rnd(max_buy),
            "note_ru": note_ru}


# ---------------------------------------------------------------------- detail
def ai_panel(verdict: AIVerdict | None) -> dict[str, Any] | None:
    if verdict is None:
        return None
    data = verdict.model_dump(mode="json")
    data.update({
        "verdict_label": VERDICT_LABELS.get(verdict.verdict, verdict.verdict),
        "condition_label": CONDITION_LABELS.get(verdict.condition, verdict.condition),
        "item_type_label": ITEM_TYPE_LABELS.get(verdict.item_type, verdict.item_type),
        "confidence_percent": max(0, min(100, round(verdict.confidence * 100))),
        "failed": verdict.confidence <= 0,
    })
    return data


def deal_detail(deal: DealView, extras: dict[str, Any] | None, config: AppConfig) -> dict[str, Any]:
    listing, ev = deal.listing, deal.evaluation
    card = deal_card(deal, extras, config=config)
    extras = extras or {}
    comps = sorted(ev.estimate.comparables, key=lambda c: (not c.sold, c.price)) if ev else []
    terms = offer_terms(ev, listing)
    search = config.search_by_name(listing.search_name)
    status = card["status"]
    return {
        **card,
        "listing": {
            "description": listing.description,
            "images": [u for u in listing.image_urls if u],
            "attributes": dict(listing.attributes),
            "attributes_ru": attributes_ru(listing.attributes),
            "tags": list(listing.tags),
            "condition": card["condition"],
            "detail_loaded": listing.detail_loaded,
            "is_top_ad": listing.is_top_ad,
            "shipping_possible": listing.shipping_possible,
            "shipping_cost": rnd(listing.shipping_cost),
            "buying_options": [{"key": b, "label": BUYING_OPTION_LABELS.get(b, b)} for b in listing.buying_options],
        },
        "seller": {
            "name": listing.seller_name,
            "type": listing.seller_type,
            "type_label": SELLER_TYPE_LABELS.get(listing.seller_type, listing.seller_type),
            "commercial": listing.seller_type == "commercial",
            "feedback_percent": rnd(listing.seller_feedback_percent, 1),
            "feedback_score": listing.seller_feedback_score,
            "is_top_ad": listing.is_top_ad,
        },
        "evaluation": None if ev is None else {
            "stage": ev.stage,
            "stage_label": STAGE_LABELS.get(ev.stage, ev.stage),
            "evaluated_at": iso(ev.evaluated_at),
            "reasons": list(ev.reasons),
            "red_flags": dedupe(ev.red_flags),
            "fees": rnd(ev.fees),
            "shipping_cost": rnd(ev.shipping_cost),
            "would_buy": ev.would_buy,
            "no_alert": ev.no_alert,
            "estimate": {
                "market_price": rnd(ev.estimate.market_price),
                "low": rnd(ev.estimate.low),
                "high": rnd(ev.estimate.high),
                "sample_size": ev.estimate.sample_size,
                "source": ev.estimate.source,
                "source_label": PRICE_SOURCE_LABELS.get(ev.estimate.source, ev.estimate.source),
                "query": ev.estimate.query,
                "notes": ev.estimate.notes,
                "warning": ev.estimate.warning,
                "history_days": ev.estimate.history_days,
                "age_days": rnd(ev.estimate.age_days, 1),
                "ai_variant_matches": ev.estimate.ai_variant_matches,
                "comparables": [{
                    "title": c.title, "price": rnd(c.price), "url": c.url, "source": c.source,
                    "source_label": COMP_SOURCE_LABELS.get(c.source, c.source), "sold": c.sold,
                    "date_text": c.date_text, "date_ru": day_words_ru(c.date_text),
                } for c in comps],
            },
        },
        "ai": ai_panel(ev.ai if ev else None),
        "ai_second": ai_panel(ev.ai_second if ev else None),
        "breakdown": profit_breakdown(deal, config),
        "offer": None if terms is None else {
            "offer_price": rnd(terms[0]), "profit": rnd(terms[1]), "roi": rnd(terms[2], 4)},
        "seller_message": seller_message(deal),
        "product_type": product_type(listing, ev),
        "search": {"id": search_id_for(config, listing.search_name), "name": listing.search_name,
                   "exists": search is not None},
        "pipeline": {
            "status": status,
            "steps": [{"key": s, "label": STATUS_LABELS[s], "done": _pipeline_done(status, s)} for s in PIPELINE],
            "bought_price": rnd(extras.get("bought_price")),
            "sold_price": rnd(extras.get("sold_price")),
            "extra_costs": rnd(extras.get("extra_costs")),
            "bought_at": iso(extras.get("bought_at")),
            "sold_at": iso(extras.get("sold_at")),
            "contacted_at": iso(extras.get("contacted_at")),
            "hidden_reason": extras.get("hidden_reason"),
            "updated_at": iso(extras.get("updated_at")),
            "expected_profit": rnd(ev.expected_profit) if ev else None,
            "realized_profit": realized_profit(status, extras),
        },
        "links": {"market": f"/api/v1/deals/{listing.ad_id}/market"},
    }


def _pipeline_done(status: str, step: str) -> bool:
    if status not in PIPELINE:
        return False
    return PIPELINE.index(step) <= PIPELINE.index(status)


# ---------------------------------------------------------------------- search
def location_label(search: SearchConfig) -> str:
    """The place as the user picked it ('Neukölln', never the postal code '12043')."""
    from .locations import place_label

    return place_label(search.location)


def where_text(search: SearchConfig) -> str:
    """'Neukölln · 50 км' / 'Berlin' / 'вся Германия' for a search card."""
    where = location_label(search) or ("вся Германия" if search.source == "kleinanzeigen" and not search.url else "")
    if where and search.radius_km:
        where = f"{where} · {search.radius_km} км"
    return where


def search_summary(search: SearchConfig) -> str:
    """'Berlin + 30 км · до 400 €' for a search card."""
    parts: list[str] = []
    where = location_label(search) or ("вся Германия" if search.source == "kleinanzeigen" else "")
    if search.radius_km and where:
        where = f"{where} + {search.radius_km} км"
    if where:
        parts.append(where)
    if search.category_name or search.category_id:
        parts.append(search.category_name or f"категория {search.category_id}")
    if search.query:
        parts.append(f"«{search.query}»")
    if search.min_price is not None and search.max_price is not None:
        parts.append(f"{search.min_price:g}–{search.max_price:g} €")
    elif search.max_price is not None:
        parts.append(f"до {search.max_price:g} €")
    elif search.min_price is not None:
        parts.append(f"от {search.min_price:g} €")
    if search.purpose == "personal" and search.target_price:
        parts.append(f"мой лимит {search.target_price:g} €")
    return " · ".join(parts)


def search_kind(search: SearchConfig) -> str:
    if search.source == "ebay":
        return "ebay"
    if search.url:
        return "url"
    if search.category_id and not search.query:
        return "category"
    if search.purpose == "personal":
        return "wishlist"
    return "keyword"


def search_view(search: SearchConfig, sid: str, stats: dict[str, Any] | None, *, errors: list[str] | None = None,
                baseline_first_run: bool = True) -> dict[str, Any]:
    stats = stats or {}
    last_run = stats.get("last_run_at")
    baseline = stats.get("baseline_at")
    if last_run is None and not stats.get("ads"):
        learning = "pending" if baseline_first_run else "active"
    elif baseline is not None and last_run is not None and abs((aware(last_run) - aware(baseline)).total_seconds()) < 1:
        learning = "learned"
    else:
        learning = "active"
    learning_ru = {
        "pending": "Первая проверка только изучит цены — сделки начнут приходить со второй",
        "learned": "Цены изучены — сделки начнут приходить со следующей проверки",
        "active": "",
    }[learning]
    return {
        "id": sid,
        "name": search.name,
        "enabled": search.enabled,
        "source": search.source,
        "source_label": SOURCE_LABELS.get(search.source, search.source),
        "purpose": search.purpose,
        "purpose_label": PURPOSE_LABELS.get(search.purpose, search.purpose),
        "kind": search_kind(search),
        "summary": search_summary(search),
        "location_label": location_label(search),  # 'Neukölln' (config.location may be '12043')
        "where_ru": where_text(search),
        "config": search.model_dump(mode="json"),
        "stats": {
            "ads": int(stats.get("ads") or 0),
            "ads_24h": int(stats.get("ads_24h") or 0),
            "deals": int(stats.get("buy") or 0),
            "maybe": int(stats.get("maybe") or 0),
            "pending": int(stats.get("pending") or 0),
            "last_run_at": iso(last_run),
            "first_run_at": iso(stats.get("first_run_at")),
            "last_new_at": iso(stats.get("last_new_at")),
            "learning": learning,
            "learning_ru": learning_ru,
            "errors": list(errors or []),
        },
    }


def search_errors(runs: Iterable[Any], name: str, limit: int = 5) -> list[str]:
    """Errors of the latest passes that belong to search `name` (they start with its name)."""
    from ...errors_ru import friendly_run_error

    out: list[str] = []
    prefixes = (f"{name}:", f"{name} /")
    for run in runs:
        for err in getattr(run, "errors", []) or []:
            text = friendly_run_error(str(err))  # plain Russian even for runs of older versions
            if str(err).startswith(prefixes) and text not in out:
                out.append(text)
        if len(out) >= limit:
            break
    return out[:limit]


def now_iso() -> str:
    return utcnow().isoformat()
