// German seller messages (brief §7.4): templates, category add-ons, Russian translations,
// the default choice per deal, plus clipboard + "open the ad" helpers.
import { humanOffer, productType, actionOf } from "./deal-model.js";

export const DAYS = [
  { value: "heute", label: "сегодня", ru: "сегодня" },
  { value: "morgen", label: "завтра", ru: "завтра" },
  { value: "am Wochenende", label: "в выходные", ru: "в выходные" },
];

const dayRu = (day) => (DAYS.find((d) => d.value === day) || DAYS[0]).ru;

/** Short product name for messages: AI product or the title cut to ~40 chars on a word. */
export function shortTitle(d) {
  const ai = d && d.ai && d.ai.product;
  let t = String(ai || (d && d.title) || "den Artikel")
    .replace(/\s*[([][^)\]]*[)\]]/g, "")
    .replace(/\s+[–—-]\s+.*$/, "")
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, "")
    .replace(/[!*#|]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (t.length > 40) {
    t = t.slice(0, 41);
    const cut = t.lastIndexOf(" ");
    t = (cut > 20 ? t.slice(0, cut) : t.slice(0, 40)).replace(/[\s,.;:–-]+$/, "");
  }
  const open = t.lastIndexOf("(");
  if (open > 10 && t.indexOf(")", open) < 0) t = t.slice(0, open).trim();
  return t;
}

const q = (t) => `„${t}“`;
const eur = (v) => `${Math.round(Number(v) || 0)} €`;

/** Templates: de(vars) / ru(vars). vars: { title, offer, day, cash } */
export const TEMPLATES = [
  {
    key: "offer",
    label: "Предложить цену",
    usesOffer: true,
    de: (v) =>
      `Hallo, ist ${q(v.title)} noch zu haben? Ich würde ${eur(v.offer)} bieten und könnte ${v.day} abholen${v.cash ? " und bar bezahlen" : ""}.`,
    ru: (v) => `Привет, «${v.title}» ещё продаётся? Предлагаю ${eur(v.offer)}, могу забрать ${dayRu(v.day)}${v.cash ? " и заплатить наличными" : ""}.`,
  },
  {
    key: "buy_now",
    label: "Купить по цене",
    de: (v) => `Hallo, ich nehme ${q(v.title)} gern zum angegebenen Preis. Ich könnte ${v.day} vorbeikommen${v.cash ? " und bar bezahlen" : ""}. Passt das?`,
    ru: (v) => `Привет, беру «${v.title}» по указанной цене. Могу заехать ${dayRu(v.day)}${v.cash ? " и заплатить наличными" : ""}. Удобно?`,
  },
  {
    key: "available",
    label: "Спросить наличие",
    de: (v) => `Hallo, ist ${q(v.title)} noch verfügbar? Ich könnte ${v.day} abholen${v.cash ? " und bar bezahlen" : ""}.`,
    ru: (v) => `Привет, «${v.title}» ещё доступен? Могу забрать ${dayRu(v.day)}${v.cash ? " и заплатить наличными" : ""}.`,
  },
  {
    key: "condition",
    label: "Уточнить состояние",
    de: (v) =>
      `Hallo, ist ${q(v.title)} noch da? Funktioniert alles einwandfrei, gibt es Kratzer oder Defekte? Ich würde es bei der Abholung gern kurz testen.`,
    ru: (v) => `Привет, «${v.title}» ещё продаётся? Всё работает? Есть царапины или дефекты? Хочу быстро проверить при встрече.`,
  },
  {
    key: "shipping",
    label: "Доставка",
    usesOffer: true,
    de: (v) => `Hallo, wäre für ${q(v.title)} auch Versand über „Sicher bezahlen“ möglich? Dann würde ich ${eur(v.offer)} inklusive Versand vorschlagen.`,
    ru: (v) => `Привет, можно отправить «${v.title}» через «Sicher bezahlen»? Тогда предлагаю ${eur(v.offer)} с доставкой.`,
  },
  {
    key: "counter",
    label: "После ответа",
    usesOffer: true,
    noGreeting: true,
    de: (v) => `Danke für die schnelle Antwort! Wären ${eur(v.offer)} in Ordnung? Dann komme ich ${v.day} vorbei.`,
    ru: (v) => `Спасибо за быстрый ответ! ${eur(v.offer)} подойдёт? Тогда приеду ${dayRu(v.day)}.`,
  },
  {
    key: "decline",
    label: "Отказаться",
    noGreeting: true,
    noSignoff: true,
    de: () => "Danke, dann passt es leider nicht für mich. Viel Erfolg beim Verkauf!",
    ru: () => "Спасибо, тогда мне не подходит. Удачной продажи!",
  },
];

export const ADDONS = {
  phone: {
    label: "iCloud и аккумулятор",
    de: "Ist das Gerät aus iCloud abgemeldet und ohne Netlock? Wie hoch ist die Akkukapazität?",
    ru: "Устройство отвязано от iCloud и без блокировки оператора? Какая ёмкость аккумулятора?",
  },
  gpu: {
    label: "Майнинг и тест",
    de: "Wurde die Karte zum Mining genutzt? Kann ich sie bei der Abholung kurz unter Last testen?",
    ru: "Карта была в майнинге? Можно проверить её под нагрузкой при встрече?",
  },
  laptop: {
    label: "Батарея и BIOS",
    de: "Wie ist der Akkuzustand? Ist das Gerät zurückgesetzt und ohne BIOS-Passwort?",
    ru: "Какое состояние батареи? Ноутбук сброшен, без пароля BIOS?",
  },
};

/** Which template to start with (brief §7.4 "Composer logic"). */
export function defaultTemplate(d) {
  if (!d) return "available";
  const action = actionOf(d);
  if (action === "haggle") return "offer";
  if (action === "bid") return "condition";
  if (action === "buy" && d.price != null && d.max_buy_price != null && d.price <= d.max_buy_price * 0.8) return "buy_now";
  if (d.negotiable && d.offer_price != null) return "offer";
  if (action === "buy") return "buy_now";
  if (d.price != null && d.max_buy_price != null && d.max_buy_price < d.price) return "offer";
  return "available";
}

/** Offer the composer starts with. */
export function defaultOffer(d) {
  if (!d) return null;
  if (d.offer_price != null) return humanOffer(d.offer_price);
  if (d.price != null && d.max_buy_price != null && d.max_buy_price < d.price) return humanOffer(d.max_buy_price);
  return d.price != null ? Math.round(d.price) : null;
}

export function defaultAddons(d) {
  const t = productType(d);
  return ADDONS[t] ? [t] : [];
}

/** Build { de, ru } for a template + variables + add-ons. */
export function buildMessage({ template = "available", title, offer, day = "heute", cash = true, addons = [] }) {
  const tpl = TEMPLATES.find((t) => t.key === template) || TEMPLATES[2];
  const vars = { title, offer: offer ?? 0, day, cash };
  let de = tpl.de(vars);
  let ru = tpl.ru(vars);
  const extras = template === "decline" || template === "counter" ? [] : addons.filter((k) => ADDONS[k]);
  if (extras.length) {
    de += " " + extras.map((k) => ADDONS[k].de).join(" ");
    ru += " " + extras.map((k) => ADDONS[k].ru).join(" ");
  }
  if (!tpl.noSignoff) de += "\n\nViele Grüße";
  return { de, ru, usesOffer: Boolean(tpl.usesOffer) };
}

/** The message the card's quick "Написать" copies. */
export function quickMessage(d) {
  return buildMessage({
    template: defaultTemplate(d),
    title: shortTitle(d),
    offer: defaultOffer(d),
    day: "heute",
    cash: true,
    addons: defaultAddons(d),
  }).de;
}

// ------------------------------------------------------------------ clipboard
/** Copy text; works on plain-http LAN too (execCommand fallback). Resolves true/false. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall back */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    ta.style.top = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/** Open the original ad in a new tab (call inside the click handler — popup blockers). */
export function openAd(url) {
  if (!url) return false;
  // with "noopener" window.open always returns null, so there is nothing to check
  window.open(url, "_blank", "noopener,noreferrer");
  return true;
}
