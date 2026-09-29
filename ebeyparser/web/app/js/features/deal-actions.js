// Deal API + user actions with optimistic updates and undo:
// star / hide / status moves / "Купил за…" / "Продал за…" / "Написать" (copy + open ad).
// Every local change is re-broadcast as a "deal_updated" event so the feed, the open
// deal view and the pipeline stay in sync without reloading.
import { html, render, useState } from "../lib/html.js";
import { api, ApiError } from "../lib/api.js";
import { emit } from "../lib/events.js";
import { money, berlinDay } from "../lib/format.js";
import { Modal, Button, Field, NumberInput, Input, toast } from "../ui/index.js";
import { normalizeCard, STATUS, humanOffer, HIDE_REASONS } from "./deal-model.js";
import { copyText, openAd, quickMessage } from "./messages.js";

const enc = encodeURIComponent;

// ------------------------------------------------------------------ API
export const dealsApi = {
  async list(params, opts) {
    try {
      const res = await api.get("/deals", { params: csvParams(params), ...opts });
      return shapeList(res);
    } catch (e) {
      if (!(e instanceof ApiError) || !e.missing) throw e;
      // older program version: legacy /api/deals (DealView objects)
      const legacy = await api.get("/api/deals", { params: legacyParams(params), ...opts });
      return shapeList(legacy);
    }
  },
  async get(id, opts) {
    try {
      return await api.get(`/deals/${enc(id)}`, { params: { seen: 1 }, ...opts });
    } catch (e) {
      if (!(e instanceof ApiError) || !e.missing || e.status !== 404 || e.code === "not_found") throw e;
      return normalizeCard(await api.get(`/api/deals/${enc(id)}`, opts));
    }
  },
  /** PATCH /deals/{id}; falls back to the legacy status endpoint. Returns the new card or null. */
  async patch(id, patch, current) {
    try {
      const res = await api.patch(`/deals/${enc(id)}`, patch);
      return res && typeof res === "object" ? res.deal || res.card || res : null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && "extra_costs" in patch) {
        const { extra_costs, ...rest } = patch;
        return dealsApi.patch(id, rest, current);
      }
      if (!(e instanceof ApiError) || !e.missing) throw e;
      const status = patch.status || (current && current.status) || "new";
      const legacy = await api.post(`/api/deals/${enc(id)}/status`, { status, note: patch.note ?? undefined });
      return normalizeCard(legacy);
    }
  },
  /** Price history of the same product: GET /deals/{id}/market?days=60 (0 = all). */
  market(id, days = 60, opts) {
    return api.get(`/deals/${enc(id)}/market`, { params: { days }, ...opts });
  },
  reevaluate(id) {
    return api.post(`/deals/${enc(id)}/reevaluate`, {});
  },
};

/** The API takes lists as comma-separated values (verdict=buy,maybe). */
function csvParams(p = {}) {
  const out = {};
  for (const [k, v] of Object.entries(p)) out[k] = Array.isArray(v) ? v.join(",") : v;
  return out;
}

function shapeList(res) {
  if (Array.isArray(res)) return { items: res.map(normalizeCard).filter(Boolean), total: res.length };
  const items = (res && (res.items || res.deals || res.results)) || [];
  return {
    ...res,
    items: items.map(normalizeCard).filter(Boolean),
    total: res && (res.total ?? res.count ?? items.length),
  };
}

function legacyParams(p = {}) {
  const out = { limit: p.limit, offset: p.offset, q: p.q };
  const sortMap = { score: "score", newest: "newest", profit: "profit" };
  if (p.sort) out.sort = sortMap[p.sort] || "score";
  if (p.status) out.status = Array.isArray(p.status) ? p.status[0] : p.status;
  if (p.purpose) out.purpose = p.purpose;
  if (p.verdict && !Array.isArray(p.verdict)) out.verdict = p.verdict;
  return out;
}

// ------------------------------------------------------------------ local sync
/** Tell every screen that a deal changed (patch is merged into their copy). */
export function publishDeal(id, patch) {
  emit("deal_updated", { ad_id: String(id), patch, local: true });
}

/** Apply a deal_updated event payload to a card (returns a new object or the same one). */
export function applyUpdate(card, data) {
  if (!card || !data) return card;
  const id = String(data.ad_id ?? data.id ?? (data.card && data.card.id) ?? "");
  if (String(card.id) !== id) return card;
  if (data.card) return { ...card, ...normalizeCard(data.card) };
  if (data.patch) return { ...card, ...data.patch };
  return card;
}

// ------------------------------------------------------------------ status moves
const snapshot = (c) => ({
  status: c.status,
  bought_price: c.bought_price ?? null,
  sold_price: c.sold_price ?? null,
  bought_at: c.bought_at ?? null,
  sold_at: c.sold_at ?? null,
});

/**
 * Move a deal to `status` right away (optimistic), save it, show a toast with «Отменить».
 *   setStatus(card, "starred", { message: "Добавлено в избранное" })
 */
export async function setStatus(card, status, { extra = {}, message, undo = true, silent = false, action2 = null } = {}) {
  const before = snapshot(card);
  const patch = { status, ...extra };
  publishDeal(card.id, localPatch(patch));
  try {
    const saved = await dealsApi.patch(card.id, patch, card);
    if (saved && saved.id !== undefined) publishDeal(card.id, pickState(saved));
  } catch (e) {
    publishDeal(card.id, before);
    toast.error(`Не получилось сохранить: ${e.message}`, {
      action: { label: "Повторить", onClick: () => setStatus(card, status, { extra, message, undo }) },
    });
    return false;
  }
  if (!silent) {
    toast({
      kind: status === "ignored" ? "neutral" : "success",
      icon: (STATUS[status] || {}).icon,
      title: message || `Статус: ${(STATUS[status] || {}).label || status}`,
      duration: undo ? 6000 : 4000,
      action2,
      action: undo
        ? {
            label: "Отменить",
            onClick: () => {
              publishDeal(card.id, before);
              dealsApi.patch(card.id, before, card).catch((e) => toast.error(`Не получилось отменить: ${e.message}`));
            },
          }
        : null,
    });
  }
  return true;
}

function localPatch(p) {
  const out = { ...p };
  if (p.status === "sold" && p.sold_price != null && p.bought_price == null) delete out.bought_price;
  return out;
}

function pickState(saved) {
  const c = normalizeCard(saved);
  const out = { status: c.status };
  for (const k of ["bought_price", "sold_price", "realized_profit", "note", "bought_at", "sold_at"]) if (k in c) out[k] = c[k];
  if (saved.pipeline) {
    for (const k of ["bought_price", "sold_price", "bought_at", "sold_at", "realized_profit"]) if (saved.pipeline[k] !== undefined) out[k] = saved.pipeline[k];
  }
  return out;
}

export function toggleStar(card) {
  if (card.status === "starred") return setStatus(card, "new", { message: "Убрано из избранного" });
  return setStatus(card, "starred", { message: "Добавлено в избранное" });
}

export function hideDeal(card, reason) {
  return setStatus(card, "ignored", {
    extra: reason ? { hidden_reason: reason } : {},
    message: "Скрыто",
    // brief §4.2.5: «Скрыто · Отменить · Почему?» — the reason teaches what doesn't fit
    action2: reason ? null : { label: "Почему?", onClick: () => askHideReason(card) },
  });
}

/** «Почему скрыл?» — one tap on a reason, saved as hidden_reason. */
function HideReasonDialog({ deal, onDone }) {
  const [open, setOpen] = useState(true);
  const close = (reason) => {
    setOpen(false);
    setTimeout(() => onDone(reason), 220);
  };
  return html`<${Modal} open=${open} onClose=${() => close(null)} size="sm" icon="eye-off" title="Почему скрываешь?" subtitle="Так я лучше пойму, что тебе не подходит">
    <div class="chips-row">
      ${HIDE_REASONS.map((r) => html`<button type="button" class="chip" onClick=${() => close(r.key)}>${r.label}</button>`)}
    </div>
  <//>`;
}

function askHideReason(deal) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const done = (reason) => {
    render(null, host);
    host.remove();
    if (!reason) return;
    publishDeal(deal.id, { hidden_reason: reason });
    dealsApi
      .patch(deal.id, { status: "ignored", hidden_reason: reason }, deal)
      .then(() => toast.success("Спасибо — учту"))
      .catch((e) => toast.error(`Не получилось сохранить: ${e.message}`, { details: e.details }));
  };
  render(html`<${HideReasonDialog} deal=${deal} onDone=${done} />`, host);
}

export function unhideDeal(card) {
  return setStatus(card, "new", { message: "Вернул в ленту", undo: false });
}

/** «Написать»: copy the German message, open the ad, mark «Написал» (with undo). */
export function writeToSeller(card, text) {
  const message = text || quickMessage(card);
  const copying = copyText(message);
  openAd(card.url);
  copying.then((ok) => {
    const later = ["contacted", "bought", "sold"].includes(card.status);
    if (!ok) {
      toast.warning("Не получилось скопировать", { message: "Открой сделку и скопируй сообщение вручную." });
      return;
    }
    if (later) {
      toast.success("Сообщение скопировано — вставь его в чат продавцу");
      return;
    }
    setStatus(card, "contacted", { message: "Сообщение скопировано — вставь его в чат продавцу" });
  });
}

// ------------------------------------------------------------------ price dialogs
function PriceDialog({ kind, deal, onDone }) {
  const bought = kind === "bought";
  const suggested = bought
    ? humanOffer(deal.offer_price) ?? deal.buy_price ?? deal.price
    : deal.market_price != null
      ? Math.round(deal.market_price)
      : deal.bought_price ?? deal.price;
  const [open, setOpen] = useState(true);
  const [price, setPrice] = useState(suggested ?? null);
  const today = berlinDay(); // the Berlin calendar day, not the UTC one (00:00–02:00 edge)
  const [date, setDate] = useState(today);
  const [extra, setExtra] = useState(0);
  const [error, setError] = useState(null);
  const close = (result) => {
    setOpen(false);
    setTimeout(() => onDone(result), 220);
  };
  const paid = deal.bought_price ?? deal.buy_price ?? deal.price;
  const real = !bought && price != null && paid != null ? price - paid - (extra || 0) : null;
  const expected = deal.profit;
  const free = Boolean(deal.is_free);
  const submit = (e) => {
    e && e.preventDefault();
    if (price == null || Number.isNaN(price)) return setError("Введи сумму в евро, например 290");
    if (price < 0) return setError("Сумма не может быть меньше 0 €");
    if (price === 0 && !free) return setError(bought ? "0 € — это даром? Введи, сколько заплатил" : "0 € — введи, за сколько продал");
    if (price > 100000) return setError("Слишком большая сумма — проверь, нет ли лишнего нуля");
    if (extra != null && extra < 0) return setError("Расходы не могут быть меньше 0 €");
    if (date && date > today) return setError("Дата не может быть в будущем");
    close({ price, date, extra: extra || 0 });
  };
  return html`<${Modal}
    open=${open}
    onClose=${() => close(null)}
    size="sm"
    icon=${bought ? "package-check" : "banknote"}
    title=${bought ? "За сколько купил?" : "За сколько продал?"}
    subtitle=${deal.title}
    footer=${html`<${Button} variant="ghost" onClick=${() => close(null)}>Отмена<//>
      <${Button} variant="primary" icon="check" onClick=${submit}>${bought ? "Сохранить покупку" : "Сохранить продажу"}<//>`}
  >
    <form class="price-dialog" onSubmit=${submit}>
      <${Field}
        label=${bought ? "Цена покупки" : "Цена продажи"}
        error=${error}
        help=${bought
          ? deal.offer_price != null
            ? `Предлагали ${money(deal.offer_price)}, в объявлении ${money(deal.price)}`
            : `В объявлении ${money(deal.price)}`
          : deal.market_price != null
            ? `Рынок ~${money(deal.market_price)}`
            : ""}
      >
        ${(id) => html`<${NumberInput} id=${id} value=${price} onChange=${(v) => (setPrice(v), setError(null))} suffix="€" size="lg" min=${0} max=${100000} invalid=${Boolean(error)} data-autofocus />`}
      <//>
      ${!bought &&
      html`<${Field} label="Расходы на продажу" help="Комиссии, доставка, упаковка — вычту из прибыли" optional>
        ${(id) => html`<${NumberInput} id=${id} value=${extra} onChange=${(v) => (setExtra(v ?? 0), setError(null))} suffix="€" min=${0} max=${100000} />`}
      <//>`}
      <${Field} label=${bought ? "Когда купил" : "Когда продал"}>
        ${(id) => html`<${Input} id=${id} type="date" value=${date} onChange=${(v) => (setDate(v), setError(null))} max=${today} />`}
      <//>
      ${real != null &&
      html`<div class=${"price-dialog__result " + (real >= 0 ? "is-profit" : "is-loss")}>
        <span>Реальная прибыль</span>
        <b class="money">${money(real, { sign: true })}</b>
        ${expected != null && html`<small>прогноз был ${money(expected, { sign: true })}</small>`}
      </div>`}
    </form>
  <//>`;
}

/** Open the «Купил за…» / «Продал за…» dialog → { price, date, extra } or null. */
export function askPrice(kind, deal) {
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const done = (result) => {
      render(null, host);
      host.remove();
      resolve(result);
    };
    render(html`<${PriceDialog} kind=${kind} deal=${deal} onDone=${done} />`, host);
  });
}

export async function markBought(card) {
  const r = await askPrice("bought", card);
  if (!r) return false;
  return setStatus(card, "bought", {
    extra: { bought_price: r.price, bought_at: toIso(r.date) },
    message: `Купил за ${money(r.price)} — отмечу в «Моих сделках»`,
  });
}

export async function markSold(card) {
  const r = await askPrice("sold", card);
  if (!r) return false;
  const paid = card.bought_price ?? card.buy_price ?? card.price;
  const profit = paid != null ? r.price - paid - (r.extra || 0) : null;
  const extra = { sold_price: r.price, sold_at: toIso(r.date) };
  if (r.extra) extra.extra_costs = r.extra;
  if (card.bought_price == null && paid != null) extra.bought_price = paid;
  return setStatus(card, "sold", {
    extra,
    message: profit != null ? `Продано! Прибыль ${money(profit, { sign: true })}` : "Продано!",
  });
}

function toIso(day) {
  if (!day) return new Date().toISOString();
  return day === berlinDay() ? new Date().toISOString() : new Date(`${day}T12:00:00Z`).toISOString();
}

/** Move by pipeline key, asking for prices where needed. */
export function moveTo(card, status) {
  if (status === "bought") return markBought(card);
  if (status === "sold") return markSold(card);
  const msg = { starred: "В избранном", contacted: "Отмечено: написал продавцу", new: "Вернул в ленту", ignored: "Скрыто" }[status];
  return setStatus(card, status, { message: msg });
}
