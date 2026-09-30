// Deal domain helpers shared by the feed, deal view, pipeline and check-ad modal.
// Pure functions: card normalisation, the decision (verb + money + colour), red-flag
// explanations, product type → checklist, human offer rounding, "seen" state.
import { money, percent, span, plural } from "../lib/format.js";

// ------------------------------------------------------------------ normalise
/**
 * Accept a v1 DealCard / DealDetail (presenters.deal_card) or a legacy DealView
 * ({listing, evaluation, status}) and return the card shape the UI works with.
 */
export function normalizeCard(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw.id !== undefined && raw.title !== undefined && !raw.evaluation?.ad_id) return withDefaults(raw);
  const l = raw.listing || {};
  const e = raw.evaluation || null;
  const est = (e && e.estimate) || {};
  const market = est.market_price ?? (e && e.ai && e.ai.estimated_market_price) ?? null;
  const images = (l.image_urls || l.images || []).filter(Boolean);
  const auction = (l.buying_options || []).includes("AUCTION");
  return withDefaults({
    id: l.ad_id || raw.ad_id || raw.id,
    url: l.url,
    source: l.source || "kleinanzeigen",
    title: l.title || "Объявление",
    image: images[0] || null,
    images_count: images.length,
    price: l.price ?? null,
    price_text: l.price_text || "",
    is_free: Boolean(l.is_free),
    negotiable: Boolean(l.negotiable),
    shipping_cost: l.shipping_cost ?? null,
    shipping_possible: l.shipping_possible ?? null,
    buy_price: e ? e.buy_price : l.price,
    market_price: market,
    market_low: est.low ?? null,
    market_high: est.high ?? null,
    market_source: est.source || "none",
    profit: e ? e.expected_profit : null,
    profit_kind: e && e.purpose === "personal" ? "savings" : "profit",
    roi: e ? e.roi : null,
    offer_price: e ? e.offer_price : null,
    max_buy_price: e ? e.max_buy_price : null,
    action: (e && e.action) || "",
    verdict: e ? e.verdict : "none",
    score: e ? Math.round(e.score || 0) : null,
    reasons: e ? (e.reasons || []).slice(0, 3) : [],
    red_flags: e ? e.red_flags || [] : [],
    ai_flags: aiFlagsLegacy(e),
    ai_checked: e ? e.ai_checked : null,
    unchecked: Boolean(e && e.ai_checked === false),
    status: raw.status || "new",
    note: raw.note || "",
    notified: Boolean(raw.notified),
    location: l.location || "",
    postal_code: l.postal_code || null,
    distance_km: l.distance_km ?? null,
    posted_at_text: l.posted_at_text || "",
    first_seen: l.first_seen || null,
    search_name: l.search_name || "",
    purpose: (e && e.purpose) || "resale",
    condition: l.condition || (l.attributes && l.attributes.Zustand) || "",
    seller_type: l.seller_type || "unknown",
    buying_options: l.buying_options || [],
    auction: auction ? { bid_count: l.bid_count ?? null, ends_at: l.ends_at || null } : null,
  });
}

function aiFlagsLegacy(e) {
  if (!e) return [];
  const out = [];
  for (const v of [e.ai, e.ai_second]) {
    if (!v) continue;
    out.push(...(v.red_flags || []));
    if (v.locked) out.push("Похоже на блокировку (iCloud / аккаунт)");
    if (v.stock_photos) out.push("Фото похожи на картинки из интернета");
    if (v.photo_matches_description === false) out.push("Фото не совпадают с описанием");
  }
  return dedupe(out);
}

function withDefaults(c) {
  return {
    reasons: [],
    red_flags: [],
    ai_flags: [],
    buying_options: [],
    status: "new",
    purpose: "resale",
    ...c,
    id: c.id != null ? String(c.id) : c.id,
  };
}

export function dedupe(list) {
  const seen = new Set();
  const out = [];
  for (const item of list || []) {
    const t = String(item || "").trim();
    const k = t.toLowerCase();
    if (t && !seen.has(k)) {
      seen.add(k);
      out.push(t);
    }
  }
  return out;
}

// ------------------------------------------------------------------ decision
const TONE_ICON = {
  buy: "trending-up",
  haggle: "hand-coins",
  bid: "gavel",
  watch: "eye",
  skip: "eye-off",
  personal: "piggy-bank",
  free: "gift",
};

/** Seconds until an ISO time (negative when passed). */
export function secondsLeft(iso, now = new Date()) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.round((t - now.getTime()) / 1000);
}

/** "2 ч 14 мин" / "14:05" style countdown for auctions (h:mm:ss under a day when `clock`). */
export function countdown(seconds, { clock = false } = {}) {
  if (seconds == null) return "";
  if (seconds <= 0) return "закончился";
  if (clock && seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  }
  return span(seconds);
}

/** Round an offer to a number people actually say: to 5 € under 100 €, to 10 € above (down). */
export function humanOffer(v) {
  if (v == null || !Number.isFinite(Number(v))) return null;
  const n = Number(v);
  if (n < 10) return Math.max(1, Math.round(n));
  const step = n < 100 ? 5 : 10;
  return Math.max(step, Math.floor(n / step) * step);
}

export function isAuction(d) {
  if (!d) return false;
  const opts = (d.buying_options || []).map((o) => (o && typeof o === "object" ? o.key || o.value || o.code : o));
  return Boolean(d.auction || opts.includes("AUCTION") || d.action === "bid");
}

/** The verb key for a deal: the server's `action`, or derived from the verdict (older data). */
export function actionOf(d) {
  if (!d) return "watch";
  let action = d.action || (d.verdict === "buy" ? "buy" : d.verdict === "skip" ? "skip" : "watch");
  if (action === "maybe") action = "watch";
  // an auction is never «Покупай»: the move is a maximum bid near the end (brief §4.3.3)
  if (isAuction(d) && (action === "buy" || action === "haggle")) action = "bid";
  return action;
}

/**
 * The decision for a deal: what to do, in which colour, with which money number.
 *   kind: buy | haggle | bid | watch | skip | personal | free
 *   tone: profit | haggle | bid | neutral          (A's semantic tones)
 *   verb: "Покупай", badge: "Покупай · 87", pill: "+110 € прибыль · ROI 38 %"
 *   title / sub: the decision block in the deal view
 */
export function decide(d, { now = new Date(), offer } = {}) {
  if (!d) return { kind: "watch", tone: "neutral", icon: "eye", verb: "Подумай", pill: "", badge: "" };
  const personal = d.purpose === "personal";
  const action = actionOf(d);
  const score = d.score != null ? Math.round(d.score) : null;
  const firstReason = trimReason((d.reasons || [])[0]);
  const market = d.market_price;
  const profit = d.profit;
  // offers are shown rounded the way people say them (293 → 290); profit follows the rounding
  const rawOffer = offer ?? d.offer_price;
  const offerPrice = offer != null ? offer : humanOffer(rawOffer);
  const offerProfit = d.profit_at_offer != null && d.offer_price != null && offerPrice != null ? d.profit_at_offer + (d.offer_price - offerPrice) : null;
  const maxBuy = d.max_buy_price;
  const price = d.price;

  let kind = action;
  if (d.is_free && (kind === "buy" || kind === "haggle" || kind === "watch")) kind = "free";
  else if (personal && (kind === "buy" || kind === "haggle")) kind = "personal";

  const out = { kind, score, icon: TONE_ICON[kind] || "eye" };

  if (kind === "free") {
    Object.assign(out, {
      tone: "profit",
      verb: "Забирай",
      pill: market ? `Бесплатно · рынок ~${money(market)}` : "Бесплатно",
      title: "Бесплатно — забирай первым",
      sub: market ? `На рынке такое стоит ~${money(market)}. Главное — написать раньше других.` : "Главное — написать раньше других.",
    });
  } else if (kind === "personal") {
    const haggle = action === "haggle" && offerPrice != null;
    const savings = haggle ? offerProfit ?? (market != null ? market - offerPrice : null) : profit;
    const pct = market && savings != null ? Math.round((savings / market) * 100) : null;
    Object.assign(out, {
      tone: haggle ? "haggle" : "profit",
      verb: haggle ? "Торгуйся" : "Бери",
      pill: haggle
        ? `Предложи ${money(offerPrice)}${savings != null ? ` → экономия ${money(savings)}` : ""}`
        : savings != null
          ? `Экономия ${money(savings)}${pct ? ` (−${pct} %)` : ""}`
          : "Цена ниже твоего лимита",
      title: haggle ? `Торгуйся: предложи ${money(offerPrice)}` : savings != null ? `Экономия ${money(savings)} к рынку` : "Подходит под твой лимит",
      sub: [
        market != null ? `Рынок ~${money(market)}` : null,
        price != null ? `цена в объявлении ${money(price)}` : null,
        maxBuy != null ? `твой лимит до ${money(maxBuy)}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    });
  } else if (kind === "buy") {
    Object.assign(out, {
      tone: "profit",
      verb: "Покупай",
      pill: profit != null ? `${money(profit, { sign: true })} прибыль${d.roi != null ? ` · ROI ${percent(d.roi)}` : ""}` : "Выгодно",
      title: profit != null ? `Покупай — прибыль ≈ ${money(profit, { sign: true })}` : "Покупай",
      sub: [
        maxBuy != null ? `Выгодно до ${money(maxBuy)}` : null,
        d.roi != null ? `ROI ${percent(d.roi)}` : null,
        price != null ? `цена в объявлении ${money(price)}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    });
  } else if (kind === "haggle") {
    const atOffer = offerProfit;
    Object.assign(out, {
      tone: "haggle",
      verb: "Торгуйся",
      pill: offerPrice != null ? `Предложи ${money(offerPrice)}${atOffer != null ? ` → ${money(atOffer, { sign: true })}` : ""}` : "Торгуйся",
      title: offerPrice != null ? `Торгуйся: предложи ${money(offerPrice)}` : "Торгуйся",
      sub: haggleSub(d, offerPrice, atOffer),
    });
  } else if (kind === "bid") {
    const left = d.auction ? secondsLeft(d.auction.ends_at, now) : null;
    const limit = maxBuy ?? offerPrice;
    const ended = left != null && left <= 0;
    Object.assign(out, {
      tone: "bid",
      verb: "Аукцион",
      pill: ended
        ? "Аукцион закончился"
        : limit != null
          ? `Ставь максимум ${money(limit)}${left != null ? ` · ${countdown(left)}` : ""}`
          : `Аукцион${left != null ? ` · ещё ${countdown(left)}` : ""}`,
      urgent: left != null && left > 0 && left < 3600,
      endsIn: left,
      title: ended ? "Аукцион закончился" : limit != null ? `Аукцион: ставь максимум ${money(limit)}` : "Аукцион: следи за ценой ближе к концу",
      sub: [
        price != null ? `Сейчас ${money(price)}` : null,
        d.auction && d.auction.bid_count != null ? `${d.auction.bid_count} ${plural(d.auction.bid_count, "ставка", "ставки", "ставок")}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    });
  } else if (kind === "skip") {
    Object.assign(out, {
      tone: "neutral",
      verb: "Не выгодно",
      pill: firstReason || "Не выгодно",
      title: `Не выгодно${firstReason ? `: ${lowerFirst(firstReason)}` : ""}`,
      sub: profit != null ? `Прибыль была бы ${money(profit, { sign: true })}` : "",
    });
  } else {
    Object.assign(out, {
      kind: "watch",
      tone: "neutral",
      verb: "Подумай",
      pill: `Подумай${firstReason ? `: ${lowerFirst(firstReason)}` : ""}`,
      title: `Подумай${firstReason ? `: ${lowerFirst(firstReason)}` : ""}`,
      sub: profit != null ? `Возможная ${personal ? "экономия" : "прибыль"} ${money(profit, { sign: true })}` : "",
    });
  }
  // badge verbs follow the glossary (§7.2): the same word as the decision, «Торг» only as the short form
  const short = { haggle: "Торг", skip: "Не выгодно", free: "Забирай" }[out.kind] || (out.tone === "haggle" ? "Торг" : out.verb);
  out.badge = score != null ? `${short} · ${score}` : short;
  return out;
}

function haggleSub(d, offerPrice, atOffer) {
  const parts = [];
  if (d.price != null && d.profit != null) parts.push(`По цене ${money(d.price)} прибыль ${money(d.profit, { sign: true })}.`);
  if (offerPrice != null && atOffer != null) parts.push(`При ${money(offerPrice)} → ${money(atOffer, { sign: true })}.`);
  if (d.max_buy_price != null) parts.push(`Дороже ${money(d.max_buy_price)} — не бери.`);
  return parts.join(" ");
}

function trimReason(r) {
  if (!r) return "";
  const t = String(r).replace(/\s+/g, " ").trim();
  return t.length > 70 ? t.slice(0, 68).replace(/[\s,.;:—-]+\S*$/, "") + "…" : t;
}

function lowerFirst(s) {
  return s ? s[0].toLowerCase() + s.slice(1) : s;
}

// ------------------------------------------------------------------ flags
const FLAG_RULES = [
  { re: /vorkasse|voraus|заранее|предоплат|überweis/i, scam: true, why: "Продавец хочет оплату заранее — без защиты покупателя. Только наличные при встрече или «Sicher bezahlen»." },
  { re: /freunde|friends|f&f|familie/i, scam: true, why: "PayPal «Freunde & Familie» — без защиты покупателя. Проси «Sicher bezahlen» или плати при встрече." },
  { re: /whats\s?app|telegram|e-?mail|почт|вне kleinanzeigen/i, scam: true, why: "Не уходи из чата Kleinanzeigen: в мессенджерах нет защиты, так работают мошенники." },
  { re: /icloud|locked|блокир|gesperrt|aktivierung|activation|аккаунт/i, scam: true, why: "Устройство может быть заблокировано — проверь при встрече (чек-лист ниже)." },
  { re: /интернет|stock|каталог|чужие фото/i, scam: true, why: "Фото, похоже, из интернета — попроси сфотографировать вещь с листком с сегодняшней датой." },
  { re: /слишком дешев|too good|betrug|fake|мошен|развод/i, scam: true, why: "Слишком хорошо, чтобы быть правдой. Плати только при встрече, после проверки." },
  { re: /nur tausch|tausch|обмен/i, scam: false, why: "Только обмен, не продажа." },
  { re: /не совпада/i, scam: false, why: "Фото не совпадают с описанием — уточни у продавца, что именно продаётся." },
  { re: /defekt|дефект|неиспр|bastler|ersatzteil|kaputt|сломан/i, scam: false, why: "Есть неисправность — прибыль посчитана как для исправного? Проверь при встрече." },
  { re: /коммерч|gewerb|händler/i, scam: false, why: "У коммерческих продавцов торг маловероятен, зато есть гарантия." },
  { re: /самовывоз|abholung/i, scam: false, why: "Только самовывоз — заложи время на дорогу." },
];

/** [{ text, why, scam }] from engine + AI flags, deduplicated, scam-type first. */
export function explainFlags(d) {
  const all = dedupe([...(d.red_flags || []), ...(d.ai_flags || [])]);
  return all
    .map((text) => {
      const rule = FLAG_RULES.find((r) => r.re.test(text));
      return { text, why: rule ? rule.why : "", scam: rule ? rule.scam : false };
    })
    .sort((a, b) => Number(b.scam) - Number(a.scam));
}

// ------------------------------------------------------------------ product type
const TYPES = [
  { key: "phone", re: /iphone|galaxy|pixel|smartphone|handy|xiaomi|oneplus|redmi|телефон|смартфон/i },
  { key: "gpu", re: /\brtx\b|\bgtx\b|radeon|geforce|grafikkarte|\brx\s?\d{3,4}|видеокарт/i },
  { key: "laptop", re: /notebook|laptop|macbook|thinkpad|ultrabook|ноутбук|zenbook|vivobook/i },
  { key: "console", re: /playstation|\bps[45]\b|xbox|nintendo|switch|steam\s?deck|консол/i },
];

export function productType(d) {
  if (d && d.product_type && d.product_type !== "other" && CHECKLISTS_KEYS.includes(d.product_type)) return d.product_type;
  const text = [d && d.title, d && d.ai && d.ai.product, d && d.search_name].filter(Boolean).join(" ");
  const t = TYPES.find((x) => x.re.test(text));
  return t ? t.key : "generic";
}

const CHECKLISTS_KEYS = ["phone", "gpu", "laptop", "console"];

export const TYPE_LABELS = { phone: "Телефон", gpu: "Видеокарта", laptop: "Ноутбук", console: "Консоль", generic: "Общее" };

export const CHECKLISTS = {
  phone: [
    "iCloud/Google-аккаунт отвязан (Настройки → имя)",
    "IMEI в настройках = IMEI на коробке",
    "Ёмкость аккумулятора ≥ 85 %",
    "Face ID / отпечаток работает",
    "Камеры, динамик, микрофон",
    "Нет «неоригинальный дисплей»",
  ],
  gpu: [
    "Запусти тест под нагрузкой 5–10 мин (FurMark/игра)",
    "Вентиляторы крутятся тихо, без треска",
    "Нет следов вскрытия и перегрева",
    "Модель и память совпадают (GPU-Z)",
  ],
  laptop: [
    "Аккумулятор: износ и циклы",
    "Нет пароля BIOS/аккаунта, Windows сброшена",
    "Клавиатура, тачпад, порты, петли",
    "Экран без битых пикселей",
    "Зарядка оригинальная",
  ],
  console: ["Включается, читает диск/скачивает", "Аккаунт отвязан", "Нет бана (онлайн работает)", "Геймпады держат заряд"],
  generic: [],
};

export const SAFETY_CHECKLIST = [
  "Плачу наличными при встрече или через «Sicher bezahlen»",
  "Не перевожу деньги заранее",
  "Встреча в людном месте",
];

// ------------------------------------------------------------------ seen state
const SEEN_KEY = "ebp.seen";
const SEEN_CAP = 5000;
let seen = null;

function loadSeen() {
  if (seen) return seen;
  try {
    seen = new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || "[]"));
  } catch {
    seen = new Set();
  }
  return seen;
}

export function isSeen(id) {
  return loadSeen().has(String(id));
}

export function markSeen(id) {
  const s = loadSeen();
  const key = String(id);
  if (s.has(key)) return;
  s.add(key);
  try {
    let list = [...s];
    if (list.length > SEEN_CAP) {
      list = list.slice(-SEEN_CAP);
      seen = new Set(list);
    }
    localStorage.setItem(SEEN_KEY, JSON.stringify(list));
  } catch {
    /* storage full / blocked */
  }
}

// ------------------------------------------------------------------ misc labels
export const STATUS = {
  new: { label: "Новое", icon: "circle-dashed", tone: "neutral" },
  starred: { label: "Избранное", icon: "star", tone: "haggle" },
  contacted: { label: "Написал", icon: "message-square", tone: "info" },
  bought: { label: "Купил", icon: "package-check", tone: "bid" },
  sold: { label: "Продал", icon: "banknote", tone: "profit" },
  ignored: { label: "Скрыто", icon: "eye-off", tone: "neutral" },
};

export const PIPELINE = ["starred", "contacted", "bought", "sold"];

export const HIDE_REASONS = [
  { key: "expensive", label: "Дорого" },
  { key: "wrong", label: "Не то" },
  { key: "scam", label: "Развод" },
  { key: "sold", label: "Уже продано" },
  { key: "far", label: "Далеко" },
];

export function sourceLabel(d) {
  return d.source_label || (d.source === "ebay" ? "eBay" : "Kleinanzeigen");
}

/** "7 мин" style age of an ad (short). */
export function shortAge(iso, now = new Date()) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const s = Math.max(0, (now - d) / 1000);
  if (s < 60) return "сейчас";
  if (s < 3600) return `${Math.round(s / 60)} мин`;
  if (s < 86400) return `${Math.round(s / 3600)} ч`;
  const days = Math.round(s / 86400);
  return `${days} дн`;
}

export function ageMinutes(iso, now = new Date()) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (now.getTime() - t) / 60000;
}

export function distanceText(d) {
  const parts = [];
  if (d.distance_km != null) parts.push(`${String(Math.round(d.distance_km * 10) / 10).replace(".", ",")} км`);
  const place = String(d.location || "").replace(/^\d{5}\s*/, "");
  if (place) parts.push(place);
  return parts.join(" · ");
}

/** Pick the right tone class name for a hue in A's tokens. */
export function toneVar(tone) {
  return { profit: "profit", haggle: "haggle", bid: "bid", danger: "danger", info: "info" }[tone] || "neutral";
}

// ------------------------------------------------------------------ German → Russian labels
// Listing texts stay German (the seller wrote them), but everything the UI itself labels —
// condition, attribute names, tags, dates — is shown in Russian (brief §2.2). The composer
// (German message to the seller) is not touched.
const CONDITION_RU = [
  [/^neu\s*mit\s*etikett|^new with tags/i, "Новое с биркой"],
  [/^neu\b.*sonstige|^new.*other/i, "Новое (без упаковки)"],
  [/^(neu|new|brand new)$/i, "Новое"],
  [/^wie\s*neu|^like\s*new|^neuwertig/i, "Как новое"],
  [/^sehr\s*gut|^very\s*good|^excellent/i, "Отличное"],
  [/^gut$|^good$/i, "Хорошее"],
  [/^in\s*ordnung|^akzeptabel|^acceptable|^ok$/i, "Нормальное"],
  [/^gebraucht|^used$/i, "Б/у"],
  [/generalüberholt|refurbished|überholt/i, "Восстановленное"],
  [/defekt|ersatzteil|for parts|broken/i, "Неисправно / на запчасти"],
];

/** "Gut" → "Хорошее", "In Ordnung" → "Нормальное"; unknown values are returned as is. */
export function ruCondition(text) {
  const t = String(text || "").trim();
  if (!t) return "";
  const hit = CONDITION_RU.find(([re]) => re.test(t));
  return hit ? hit[1] : t;
}

const ATTR_RU = {
  art: "Тип",
  zustand: "Состояние",
  versand: "Доставка",
  marke: "Марка",
  hersteller: "Производитель",
  modell: "Модель",
  farbe: "Цвет",
  "größe": "Размер",
  groesse: "Размер",
  speicherkapazität: "Память",
  speichergröße: "Объём памяти",
  speichertyp: "Тип памяти",
  arbeitsspeicher: "Оперативная память",
  prozessor: "Процессор",
  bildschirmgröße: "Диагональ",
  displaygröße: "Диагональ",
  betriebssystem: "Система",
  plattform: "Платформа",
  material: "Материал",
  rahmengröße: "Размер рамы",
  typ: "Тип",
  kategorie: "Категория",
  garantie: "Гарантия",
  zubehör: "Комплект",
  lieferumfang: "Комплект",
  "anzahl der artikel": "Количество",
  ort: "Место",
};

export function ruAttrKey(key) {
  const k = String(key || "").trim();
  return ATTR_RU[k.toLowerCase()] || k;
}

/** Attribute value in Russian where it is a UI-ish word (condition, shipping), else unchanged. */
export function ruAttrValue(key, value) {
  const k = String(key || "").toLowerCase();
  if (k === "zustand") return ruCondition(value);
  if (k === "versand") return ruTag(value);
  return value;
}

const TAG_RU = [
  [/^auktion$|^auction$/i, () => "Аукцион"],
  [/^versand\s*möglich$/i, () => "Доставка возможна"],
  [/^versand\s+(.+)$/i, (m) => `Доставка ${m[1]}`],
  [/^nur\s*abholung$/i, () => "Только самовывоз"],
  [/^abholung$/i, () => "Самовывоз"],
  [/^(direkt kaufen|sofort-?kaufen|fixed_price|buy it now)$/i, () => "Купить сразу"],
  [/^(preisvorschlag|best_offer)$/i, () => "Можно предложить цену"],
  [/^gewerblich/i, () => "Коммерческий продавец"],
  [/^privat$/i, () => "Частное лицо"],
  [/^(tausch|nur tausch)$/i, () => "Обмен"],
  [/^vb$/i, () => "Торг"],
  [/^zu verschenken$/i, () => "Даром"],
  [/^top$/i, () => "Топ-объявление"],
];

export function ruTag(text) {
  const t = String(text || "").trim();
  for (const [re, fn] of TAG_RU) {
    const m = t.match(re);
    if (m) return fn(m);
  }
  return t;
}

/** "Heute, 19:35" → "сегодня в 19:35", "Gestern" → "вчера", "Verkauft 15.09.2026" → "15.09". */
export function ruDateText(text, now = new Date()) {
  let t = String(text || "").trim();
  if (!t) return "";
  t = t.replace(/^(verkauft|sold|beendet|endet)\s*(am\s*)?/i, "");
  t = t.replace(/\bheute\b,?\s*/i, "сегодня ").replace(/\bgestern\b,?\s*/i, "вчера ").replace(/\bvorgestern\b,?\s*/i, "позавчера ");
  t = t.replace(/^(сегодня|вчера|позавчера),\s*/, "$1 ");
  t = t.replace(/^(сегодня|вчера|позавчера)\s+(\d{1,2}:\d{2})/, "$1 в $2");
  t = t.replace(/^sofort-?kaufen$/i, "купить сразу");
  const year = String(now.getFullYear());
  t = t.replace(/\b(\d{2})\.(\d{2})\.(\d{4})\b/, (_, d, m, y) => (y === year ? `${d}.${m}` : `${d}.${m}.${y}`));
  return t.trim();
}

/** "6.99 €" → "6,99 €" inside server notes. */
export function ruNumbers(text) {
  return String(text || "").replace(/(\d)\.(\d{1,2})(?=\s*€)/g, "$1,$2");
}
