// Russian number / money / time formatting. Pure functions, safe with null/undefined.

const nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });

/** 1234.5 → "1 235 €"; money(12.5, { cents: true }) → "12,50 €"; sign: "+80 €" / "−20 €". */
export function money(value, { sign = false, cents = false, dash = "—", currency = "€" } = {}) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return dash;
  const v = Number(value);
  const abs = Math.abs(v);
  const body = cents
    ? new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(abs)
    : nf0.format(Math.round(abs));
  const prefix = v < 0 ? "−" : sign && v > 0 ? "+" : "";
  return `${prefix}${body} ${currency}`;
}

/** 0.253 → "25 %" (ratio); percent(25, { ratio: false }) → "25 %". */
export function percent(value, { ratio = true, sign = false, digits = 0, dash = "—" } = {}) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return dash;
  const v = ratio ? Number(value) * 100 : Number(value);
  const body = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(Math.abs(v));
  const prefix = v < 0 ? "−" : sign && v > 0 ? "+" : "";
  return `${prefix}${body} %`;
}

export function number(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return digits ? nf2.format(Number(value)) : nf0.format(Number(value));
}

/** plural(5, "объявление", "объявления", "объявлений") → "объявлений". */
export function plural(n, one, few, many) {
  const a = Math.abs(Math.trunc(Number(n) || 0));
  if (a % 10 === 1 && a % 100 !== 11) return one;
  if (a % 10 >= 2 && a % 10 <= 4 && (a % 100 < 12 || a % 100 > 14)) return few;
  return many;
}

/** count(3, "поиск", "поиска", "поисков") → "3 поиска". */
export function count(n, one, few, many) {
  return `${number(n)} ${plural(n, one, few, many)}`;
}

function toDate(v) {
  if (!v) return null;
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Human span: 45 → "45 с", 600 → "10 мин", 5400 → "1 ч 30 мин", 2 days → "2 дн". */
export function span(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s} с`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} мин`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 24) return rm ? `${h} ч ${rm} мин` : `${h} ч`;
  const d = Math.floor(h / 24);
  return `${d} дн`;
}

/** "только что", "5 мин назад", "вчера в 14:05", "12 марта". */
export function ago(value, now = new Date()) {
  const d = toDate(value);
  if (!d) return "—";
  const diff = (now - d) / 1000;
  if (diff < 0) return until(value, now);
  if (diff < 45) return "только что";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} мин назад`;
  if (diff < 6 * 3600) return `${Math.round(diff / 3600)} ч назад`;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((today - day) / 86400000);
  const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (days === 0) return `сегодня в ${hm}`;
  if (days === 1) return `вчера в ${hm}`;
  if (days < 7) return `${days} ${plural(days, "день", "дня", "дней")} назад`;
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

/** "через 12 мин", "сейчас". */
export function until(value, now = new Date()) {
  const d = toDate(value);
  if (!d) return "—";
  const diff = (d - now) / 1000;
  if (diff <= 30) return "сейчас";
  return `через ${span(diff)}`;
}

export function dateTime(value) {
  const d = toDate(value);
  if (!d) return "—";
  return d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function bytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  if (v < 1024) return `${v} Б`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(0)} КБ`;
  if (v < 1024 * 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1).replace(".", ",")} МБ`;
  return `${(v / 1024 / 1024 / 1024).toFixed(2).replace(".", ",")} ГБ`;
}

/** Radius in km → human label for the location slider. */
export function radiusLabel(km) {
  const v = Number(km) || 0;
  if (v <= 0) return "Только мой город";
  if (v <= 5) return "Мой район";
  if (v <= 10) return "Можно доехать на велосипеде";
  if (v <= 20) return "Город и ближайшие пригороды";
  if (v <= 30) return "Весь город с окрестностями";
  if (v <= 50) return "До получаса на машине";
  if (v <= 100) return "До часа на машине";
  return "Готов ехать далеко";
}

/** Interval in minutes → "каждые 15 мин" / "раз в час". */
export function everyLabel(minutes) {
  const m = Number(minutes) || 0;
  if (m === 60) return "раз в час";
  if (m > 60 && m % 60 === 0) return `раз в ${m / 60} ${plural(m / 60, "час", "часа", "часов")}`;
  if (m > 60) return `раз в ${span(m * 60)}`;
  return `каждые ${m} мин`;
}
