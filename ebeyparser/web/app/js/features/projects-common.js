// «Сборки» (build projects, docs/design/PROJECTS.md): shared helpers for the projects screens —
// server-text clean-up, a tiny cache of project views, the PATCH/undo helper, status tones,
// the phone «Сделки · Сборки» switch and a click popover hint that works on touch.
import { html, cx } from "../lib/html.js";
import { api } from "../lib/api.js";
import { money, localizeText } from "../lib/format.js";
import { navigate } from "../lib/router.js";
import { createStore } from "../lib/store.js";
import { ICONS } from "../ui/icons.js";
import { Icon, Segmented, toast } from "../ui/index.js";
import { Popover } from "./popover.js";
import "./icons-extra.js";

// Lucide (ISC, lucide-static 1.48.0): icons of the check rows not in ui/icons.js yet (additive)
const EXTRA = {
  "plug-zap":
    '<path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z" /><path d="m2 22 3-3" /><path d="M7.5 13.5 10 11" /><path d="M10.5 16.5 13 14" /><path d="m18 3-4 4h6l-4 4" />',
  fan: '<path d="M10.827 16.379a6.082 6.082 0 0 1-8.618-7.002l5.412 1.45a6.082 6.082 0 0 1 7.002-8.618l-1.45 5.412a6.082 6.082 0 0 1 8.618 7.002l-5.412-1.45a6.082 6.082 0 0 1-7.002 8.618l1.45-5.412Z" /><path d="M12 12v.01" />',
  "sticky-note": '<path d="M21 9a2.4 2.4 0 0 0-.706-1.706l-3.588-3.588A2.4 2.4 0 0 0 15 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2z" /><path d="M15 3v5a1 1 0 0 0 1 1h5" />',
  "circle-slash": '<circle cx="12" cy="12" r="10" /><line x1="9" x2="15" y1="15" y2="9" />',
  trophy:
    '<path d="M10 14.66V17a1 1 0 0 1-1 1 2 2 0 0 0-2 2v2" /><path d="M14 14.66V17a1 1 0 0 0 1 1 2 2 0 0 1 2 2v2" /><path d="M17.916 10H19.5A2.5 2.5 0 0 0 22 7.5V5a1 1 0 0 0-1-1h-3" /><path d="M4 22h16" /><path d="M6 9a6 6 0 0 0 12 0V3a1 1 0 0 0-1-1H7a1 1 0 0 0-1 1z" /><path d="M6.084 10H4.5A2.5 2.5 0 0 1 2 7.5V5a1 1 0 0 1 1-1h3" />',
};
for (const [name, body] of Object.entries(EXTRA)) if (!(name in ICONS)) ICONS[name] = body;

// ------------------------------------------------------------------ text & money
/**
 * Server texts are ready to show (`*_ru`, `lines[]`), but money inside them is German-style
 * («1.065 €») while the rest of the UI prints «1 065 €»; ISO times → Berlin «21:34»; a leading
 * emoji (Telegram style «📦 …») is dropped — the UI has its own icons.
 */
export function ru(text) {
  if (text == null) return "";
  return localizeText(String(text))
    .replace(/(\d)\.(?=\d{3}(?!\d))/g, "$1 ")
    .replace(/^[\s\u{1F4E6}\u{1F9E9}⚠️❗\u{1F389}]+/u, "")
    .trim();
}

/** «~1 065 €» (a market estimate), «—» for null. */
export const approx = (v) => (v == null ? "—" : `~${money(v)}`);
export { money };

// ------------------------------------------------------------------ vocabulary
export const STATUS_TONE = { draft: "neutral", tracking: "profit", paused: "neutral", done: "profit" };
export const STATUS_ORDER = { tracking: 0, paused: 1, draft: 2, done: 3 };

/** Icon per compatibility check key (spec §5.2). */
export const CHECK_ICON = {
  vram: "memory-stick",
  speed: "gauge",
  psu: "plug-zap",
  power: "plug-zap",
  socket: "cpu",
  pcie: "layout-grid",
  spacing: "layout-grid",
  ram: "memory-stick",
  display: "monitor",
  cooling: "fan",
  capacity: "hard-drive",
  sata: "hard-drive",
  electricity: "zap",
  budget: "wallet",
};
/** ok / warn / fail / info → tone class + mark icon. */
export const CHECK_TONE = { ok: "profit", warn: "haggle", fail: "danger", info: "neutral" };
export const CHECK_MARK = { ok: "check", warn: "triangle-alert", fail: "x", info: "info" };

export const TEMPLATE_ICON = { llm_24: "cpu", llm_48: "server", nas: "hard-drive", gaming_1080p: "gamepad-2", custom: "list-checks" };

/** «Видеокарты» → «видеокарты», but keep «SSD» / «NAS» upper-case. */
export function lower(label) {
  const s = String(label || "");
  return /^[A-ZА-ЯЁ0-9]{2,}\b/.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1);
}

// ------------------------------------------------------------------ hand-off between screens
/** Set right before navigating from the creation screen to the saved project. */
export const handoff = { flash: null, track: false };

// ------------------------------------------------------------------ cache (instant page switches)
/** id → last ProjectView; and the list cards. The project page renders from here while it refetches. */
export const projectsStore = createStore({ views: {}, cards: null });

export function cacheView(view) {
  if (!view || view.id == null) return view;
  const s = projectsStore.get();
  projectsStore.set({ views: { ...s.views, [view.id]: view } });
  if (s.cards) projectsStore.set({ cards: s.cards.map((c) => (c.id === view.id ? pickCard(view, c) : c)) });
  return view;
}

export function forgetView(id) {
  const s = projectsStore.get();
  const views = { ...s.views };
  delete views[id];
  projectsStore.set({ views, cards: s.cards ? s.cards.filter((c) => c.id !== Number(id)) : s.cards });
}

/** A ProjectView is a ProjectCard plus more: take the card fields for the list. */
function pickCard(view, prev) {
  const out = {};
  for (const k of Object.keys(prev)) out[k] = view[k] !== undefined ? view[k] : prev[k];
  return out;
}

// ------------------------------------------------------------------ API
export const projectsApi = {
  templates: () => api.get("/projects/templates"),
  list: () => api.get("/projects"),
  get: (id) => api.get(`/projects/${encodeURIComponent(id)}`),
  plan: (body, opts) => api.post("/projects/plan", body, opts),
  create: (body) => api.post("/projects", body),
  patch: (id, body) => api.patch(`/projects/${encodeURIComponent(id)}`, body),
  remove: (id, keepSearches) => api.del(`/projects/${encodeURIComponent(id)}`, { params: keepSearches ? { keep_searches: true } : null }),
  track: (id, body) => api.post(`/projects/${encodeURIComponent(id)}/track`, body),
  bought: (id, slot, body) => api.post(`/projects/${encodeURIComponent(id)}/slots/${encodeURIComponent(slot)}/bought`, body),
  unbought: (id, slot) => api.del(`/projects/${encodeURIComponent(id)}/slots/${encodeURIComponent(slot)}/bought`),
  offers: (id, slot) => api.get(`/projects/${encodeURIComponent(id)}/offers`, { params: { slot, limit: 10 } }),
};

/**
 * Which OTHER slots changed their choice (the one local computation the spec allows, §11).
 * → [{ key, label, from, to, toLabel }]
 */
export function diffChosen(before, after, except = []) {
  if (!before || !after) return [];
  const prev = Object.fromEntries((before.slots || []).map((s) => [s.key, s]));
  const out = [];
  for (const s of after.slots || []) {
    const p = prev[s.key];
    if (!p || except.includes(s.key) || p.chosen === s.chosen) continue;
    out.push({ key: s.key, label: s.label, from: p.chosen, to: s.chosen, toLabel: s.chosen_label });
  }
  return out;
}

/** «Заодно поменял: блок питания → 1300 Вт; корпус → Большой корпус» */
export function sideChangesText(changes) {
  if (!changes.length) return "";
  return "Заодно поменял: " + changes.map((c) => `${lower(c.label)} → ${ru(withoutPrefix(shortOption(c.toLabel), c.label))}`).join("; ");
}

/** «Блок питания 1300 Вт (80+ Gold)» under «Блок питания» → «1300 Вт (80+ Gold)». */
function withoutPrefix(label, slotLabel) {
  const l = String(label || "");
  const pre = String(slotLabel || "");
  return pre && l.toLowerCase().startsWith(pre.toLowerCase() + " ") ? l.slice(pre.length + 1) : l;
}

/** «Большой корпус: 8+ слотов (Define 7 XL …)» → «Большой корпус»; «Блок питания 1300 Вт (80+ Gold)» → as is. */
export function shortOption(label) {
  const s = String(label || "");
  const cut = s.split(": ")[0];
  return cut.length > 48 ? cut.slice(0, 46) + "…" : cut;
}

/**
 * Change a choice (or a fix) of a saved project: PATCH, tell what else changed, offer «Отменить».
 * Returns the new view (or null on error — the toast already said why).
 */
export async function changeChoice(project, slotKey, optionKey, { onView, title } = {}) {
  const slot = (project.slots || []).find((s) => s.key === slotKey);
  const prevChosen = slot ? slot.chosen : null;
  try {
    const view = await projectsApi.patch(project.id, { choices: { [slotKey]: optionKey } });
    cacheView(view);
    onView && onView(view);
    const others = diffChosen(project, view, [slotKey]);
    const next = (view.slots || []).find((s) => s.key === slotKey);
    const undoChoices = { [slotKey]: prevChosen, ...Object.fromEntries(others.map((c) => [c.key, c.from])) };
    toast.success(title || `${slot ? slot.label : "Часть"}: ${ru(shortOption(next ? next.chosen_label : optionKey))}`, {
      message: sideChangesText(others),
      action: prevChosen
        ? {
            label: "Отменить",
            onClick: async () => {
              try {
                const back = await projectsApi.patch(project.id, { choices: undoChoices, adjust: false });
                cacheView(back);
                onView && onView(back);
              } catch (e) {
                toast.error(e);
              }
            },
          }
        : null,
    });
    return view;
  } catch (e) {
    toast.error(e);
    return null;
  }
}

// ------------------------------------------------------------------ small UI pieces
/** Phone only: «Мои сделки» holds both the pipeline and the builds (no 6th tab, spec §1). */
export function DealsSwitch({ value, count }) {
  return html`<div class="pj-switch">
    <${Segmented}
      block
      label="Мои сделки"
      value=${value}
      onChange=${(v) => v !== value && navigate(v === "projects" ? "/projects" : "/deals", { replace: true })}
      options=${[
        { value: "deals", label: "Сделки", icon: "wallet" },
        { value: "projects", label: count ? `Сборки · ${count}` : "Сборки", icon: "boxes" },
      ]}
    />
  </div>`;
}

/**
 * Text with an explanation on tap / click (tooltips don't exist on touch — brief §2.10).
 *   <Hint tip="если брать каждую часть на ~10 % дешевле рынка">Цель 925 €</Hint>
 */
export function Hint({ tip, title, children, class: cls = "", align = "start", width = 300 }) {
  if (!tip) return html`<span class=${cls}>${children}</span>`;
  return html`<${Popover}
    align=${align}
    width=${width}
    label=${title || "Пояснение"}
    class="pj-hint"
    trigger=${(p) => html`<button type="button" class=${cx("pj-hint__btn", cls)} ...${p}>${children}</button>`}
  >
    <div class="pj-hint__body">
      ${title && html`<b>${title}</b>`}
      ${Array.isArray(tip) ? tip.map((t) => html`<p>${t}</p>`) : html`<p>${tip}</p>`}
    </div>
  <//>`;
}

/** Dotted «◌ ориентир» badge: prices from the built-in knowledge base, not from the market yet. */
export function RoughBadge({ note, label = "ориентир" }) {
  return html`<${Hint}
    title="Цены-ориентиры"
    tip=${note || "Цены пока из встроенной базы знаний — точнее скажет история цен из объявлений после первых проверок."}
    class="pj-rough"
    align="end"
    ><${Icon} name="circle-dashed" size=${12} />${label}<//
  >`;
}

/** Status chip of a project: «● Отслеживаю», «Черновик», «На паузе», «✓ Собрано». */
export function StatusChip({ status, label, extra }) {
  const tone = STATUS_TONE[status] || "neutral";
  return html`<span class=${cx("pj-status", `tone-${tone}`, `pj-status--${status}`)}>
    ${status === "done" ? html`<${Icon} name="check" size=${12} stroke=${3} />` : html`<span class=${cx("sdot", status === "tracking" && "is-pulse")}></span>`}
    ${label}${extra ? html`<span class="pj-status__extra"> · ${extra}</span>` : ""}
  </span>`;
}
