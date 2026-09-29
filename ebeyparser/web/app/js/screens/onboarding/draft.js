// Onboarding answers: kept in localStorage (ebp.onboarding.v1) and on the server
// (PUT /onboarding/draft), so a reload or switching to the phone keeps the progress.
// Nothing is written to config.yaml until «Запустить» (POST /setup); integrations
// (AI, Telegram, eBay, e-mail) store their secrets as soon as they are verified.
import { api } from "../../lib/api.js";
import { createStore } from "../../lib/store.js";
import { DEFAULT_PRESETS } from "../../setup/money.js";

const KEY = "ebp.onboarding.v1";

export const DEFAULT_DRAFT = {
  location: "Berlin",
  location_label: "Berlin",
  location_confirmed: true,
  radius_km: 30,
  category_ids: null, // null = use the recommended ones once categories load
  category_names: {},
  purpose: "both",
  max_price: 400,
  strategy: "balanced",
  pricing: { min_profit: 40, min_roi: 0.25, safety_margin_percent: 10 },
  wishlist: [],
  ai: { done: false, skipped: false, model: null, seconds: null },
  telegram: { done: false, skipped: false, linked: false, bot: null, chat: null },
  email: { done: false },
  ebay: { done: false, skipped: false },
  wishlist_skipped: false,
  saved_at: null,
};

export const draftStore = createStore({ draft: null, loaded: false, options: null });

let timer = null;

function readLocal() {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function newer(a, b) {
  if (!a) return b;
  if (!b) return a;
  return (a.saved_at || "") >= (b.saved_at || "") ? a : b;
}

/** Load options (defaults, presets, current config answers) and the latest draft. */
export async function loadDraft() {
  const [options, server] = await Promise.all([
    api.get("/setup/options").catch(() => null),
    api.get("/onboarding/draft").catch(() => null),
  ]);
  const saved = newer(readLocal(), server && server.draft && typeof server.draft === "object" ? server.draft : null);
  let draft = { ...DEFAULT_DRAFT };
  if (options && options.current) {
    const c = options.current;
    const strategy = c.strategy && c.strategy !== "custom" ? c.strategy : "balanced";
    const presets = options.presets || DEFAULT_PRESETS;
    draft = {
      ...draft,
      location: c.location || draft.location,
      location_label: c.location || draft.location_label,
      radius_km: c.radius_km ?? draft.radius_km,
      category_ids: c.existing_searches ? c.category_ids : null,
      purpose: c.existing_searches ? c.purpose : draft.purpose,
      max_price: c.max_price ?? draft.max_price,
      strategy,
      pricing: pick(presets[strategy] || DEFAULT_PRESETS.balanced),
      wishlist: (c.wishlist || []).map((w) => ({ item: w.item, max_price: w.max_price, haggle: true })),
    };
  }
  if (saved) draft = { ...draft, ...saved, ai: { ...draft.ai, ...(saved.ai || {}) }, telegram: { ...draft.telegram, ...(saved.telegram || {}) }, ebay: { ...draft.ebay, ...(saved.ebay || {}) }, email: { ...draft.email, ...(saved.email || {}) } };
  draftStore.set({ draft, loaded: true, options });
  return draft;
}

export function pick(p) {
  return { min_profit: p.min_profit, min_roi: p.min_roi, safety_margin_percent: p.safety_margin_percent };
}

/** Merge a change into the draft and persist it (local now, server debounced). */
export function updateDraft(patch) {
  const cur = draftStore.get().draft || { ...DEFAULT_DRAFT };
  const next = { ...cur, ...(typeof patch === "function" ? patch(cur) : patch), saved_at: new Date().toISOString() };
  draftStore.set({ draft: next });
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* ignore */
  }
  clearTimeout(timer);
  timer = setTimeout(() => api.put("/onboarding/draft", { draft: next }).catch(() => {}), 600);
  return next;
}

export async function clearDraft() {
  clearTimeout(timer);
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  await api.del("/onboarding/draft").catch(() => {});
}

/** The body for POST /setup and POST /searches/preview. */
export function setupBody(d, extra = {}) {
  const wishlist = d.purpose === "resale" ? [] : (d.wishlist || []).filter((w) => w.item && w.item.trim()).map((w) => ({ item: w.item.trim(), max_price: w.max_price || null, haggle: w.haggle !== false }));
  return {
    location: d.location,
    radius_km: d.radius_km,
    category_ids: d.category_ids || [],
    category_names: d.category_names || {},
    purpose: d.purpose === "personal" ? "personal" : d.purpose === "both" ? "both" : "resale",
    max_price: d.max_price,
    strategy: d.strategy === "custom" ? "custom" : d.strategy,
    pricing: d.strategy === "custom" ? d.pricing : undefined,
    min_profit: d.pricing ? d.pricing.min_profit : undefined,
    wishlist,
    replace: true,
    ...extra,
  };
}
