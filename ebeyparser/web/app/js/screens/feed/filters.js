// Feed filters: state <-> URL query <-> API params, quick chips, sort, «Ещё фильтры» sheet.
import { html, cx, useState, useEffect } from "../../lib/html.js";
import { useAsync, useIsMobile } from "../../lib/hooks.js";
import { api } from "../../lib/api.js";
import { Icon, Button, Modal, Drawer, Field, NumberInput, Slider, Segmented, Select } from "../../ui/index.js";
import "../../features/icons-extra.js";

export const LAST_KEY = "ebp.feed.filters.v1";

export const DEFAULTS = {
  act: "",
  personal: false,
  near: false,
  ship: false,
  noflags: false,
  unseen: false,
  sort: "best",
  source: "",
  search: "",
  pmin: null,
  pmax: null,
  profit: null,
  score: null,
  st: "",
  since: "",
};

const BOOL = ["personal", "near", "ship", "noflags", "unseen"];
const NUM = ["pmin", "pmax", "profit", "score"];
const URL_KEYS = { act: "f", noflags: "nf", source: "src" };

export function fromQuery(query = {}) {
  const f = { ...DEFAULTS };
  for (const k of Object.keys(DEFAULTS)) {
    const v = query[URL_KEYS[k] || k];
    if (v === undefined || v === "") continue;
    if (BOOL.includes(k)) f[k] = v === "1" || v === "true";
    else if (NUM.includes(k)) f[k] = Number.isFinite(Number(v)) ? Number(v) : null;
    else f[k] = String(v);
  }
  if (!["", "buy", "haggle", "bid"].includes(f.act)) f.act = "";
  if (!SORTS.some((x) => x.value === f.sort)) f.sort = "best";
  if (f.sort === "ending" && f.act !== "bid") f.sort = "best";
  return f;
}

export function toQuery(f) {
  const q = {};
  for (const k of Object.keys(DEFAULTS)) {
    const v = f[k];
    const key = URL_KEYS[k] || k;
    if (v === DEFAULTS[k] || v === null || v === "" || v === false) q[key] = null;
    else q[key] = BOOL.includes(k) ? "1" : String(v);
  }
  return q;
}

export function hasQuery(query) {
  return Object.keys(DEFAULTS).some((k) => query[URL_KEYS[k] || k] !== undefined);
}

/** Filters that are not the defaults (for the «Сбросить» state and the empty-state variant). */
export function activeCount(f) {
  return Object.keys(DEFAULTS).filter((k) => k !== "sort" && f[k] !== DEFAULTS[k] && f[k] !== null && f[k] !== "").length;
}

/** Filters inside the «Ещё» sheet (for its badge). */
export function moreCount(f) {
  return ["source", "search", "pmin", "pmax", "profit", "score", "st", "since"].filter((k) => f[k] !== DEFAULTS[k] && f[k] !== null && f[k] !== "").length;
}

/** API params for GET /api/v1/deals (lists are comma-separated; default verdict = buy + maybe). */
export function toParams(f, q) {
  const p = { sort: f.sort || "best" };
  if (f.act) p.action = f.act;
  if (f.personal) p.purpose = "personal";
  if (f.near) p.max_km = 10;
  if (f.ship) p.shipping = 1;
  if (f.noflags) p.no_flags = 1;
  if (f.unseen) p.unseen = 1;
  if (f.source) p.source = f.source;
  if (f.search) p.search = f.search;
  if (f.pmin != null) p.min_price = f.pmin;
  if (f.pmax != null) p.max_price = f.pmax;
  if (f.profit != null) p.min_profit = f.profit;
  if (f.score != null) p.min_score = f.score;
  if (f.st) {
    p.status = f.st;
    p.verdict = "all";
  }
  if (f.since) p.since = f.since;
  if (q) p.q = q;
  return p;
}

const ACTION_CHIPS = [
  { key: "", label: "Все выгодные", icon: "zap" },
  { key: "buy", label: "Купить сразу", icon: "trending-up" },
  { key: "haggle", label: "Торг", icon: "hand-coins" },
  { key: "bid", label: "Аукционы", icon: "gavel" },
];
const TOGGLE_CHIPS = [
  { key: "personal", label: "Для себя", icon: "piggy-bank" },
  { key: "near", label: "Рядом ≤ 10 км", icon: "map-pin" },
  { key: "ship", label: "С доставкой", icon: "truck" },
  { key: "noflags", label: "Без ⚠", icon: "shield-check" },
  { key: "unseen", label: "Не смотрел", icon: "eye" },
];

export const SORTS = [
  { value: "best", label: "Лучшие" },
  { value: "fresh", label: "Новые" },
  { value: "profit", label: "Прибыль" },
  { value: "distance", label: "Ближе" },
  { value: "ending", label: "Скоро конец", auction: true },
];

function FChip({ on, icon, children, onClick, count }) {
  return html`<button type="button" class=${cx("fchip", on && "is-on")} aria-pressed=${on} onClick=${onClick}>
    <${Icon} name=${on ? "check" : icon} size=${14} stroke=${on ? 2.75 : 2} />
    <span>${children}</span>
    ${count != null && count > 0 && html`<span class="fchip__count num">${count}</span>`}
  </button>`;
}

/** Chips row + sort + «Ещё фильтры» + view toggle. */
export function FilterBar({ f, set, view, setView, total, reset, facets, search }) {
  const isMobile = useIsMobile();
  const [more, setMore] = useState(false);
  const extra = moreCount(f);
  const sorts = SORTS.filter((s) => !s.auction || f.act === "bid");
  const fc = facets || null;
  const countOf = (key) => {
    if (!fc) return null;
    if (key === "") return fc.verdict && fc.verdict.good;
    if (["buy", "haggle", "bid"].includes(key)) return fc.action && fc.action[key];
    return { personal: fc.purpose && fc.purpose.personal, near: fc.near_10km, ship: fc.shipping, noflags: fc.no_flags, unseen: fc.unseen }[key];
  };
  return html`<div class="fbar">
    <div class="fbar__chips" role="toolbar" aria-label="Фильтры">
      ${ACTION_CHIPS.map(
        (c) => html`<${FChip}
          on=${f.act === c.key}
          icon=${c.icon}
          count=${c.key && f.act !== c.key ? countOf(c.key) : null}
          onClick=${() => set({ act: c.key, sort: c.key !== "bid" && f.sort === "ending" ? "best" : f.sort })}
          >${c.label}<//
        >`,
      )}
      <span class="fbar__sep" aria-hidden="true"></span>
      ${TOGGLE_CHIPS.map(
        (c) => html`<${FChip} on=${f[c.key]} icon=${c.icon} count=${!f[c.key] && c.key === "unseen" ? countOf(c.key) : null} onClick=${() => set({ [c.key]: !f[c.key] })}>${c.label}<//>`,
      )}
      <button type="button" class=${cx("fchip fchip--more", extra > 0 && "is-on")} onClick=${() => setMore(true)}>
        <${Icon} name="sliders-horizontal" size=${14} /><span>Ещё</span>${extra > 0 && html`<span class="fchip__count num">${extra}</span>`}
      </button>
    </div>
    <div class="fbar__right">
      ${isMobile &&
      search &&
      html`<label class="feed-search">
        <${Icon} name="search" size=${16} />
        <input type="search" value=${search.value} onInput=${(e) => search.onChange(e.currentTarget.value)} placeholder="Найти: 3090, iPhone…" aria-label="Поиск в ленте" />
      </label>`}
      ${isMobile
        ? html`<label class="sort-select">
            <${Icon} name="arrow-down-up" size=${14} />
            <select value=${f.sort} onChange=${(e) => set({ sort: e.currentTarget.value })} aria-label="Сортировка">
              ${sorts.map((s) => html`<option value=${s.value}>${s.label}</option>`)}
            </select>
          </label>`
        : html`<${Segmented} size="sm" label="Сортировка" value=${f.sort} onChange=${(v) => set({ sort: v })} options=${sorts} />`}
      ${!isMobile &&
      html`<div class="viewtoggle" role="radiogroup" aria-label="Вид">
        <button type="button" role="radio" aria-checked=${view === "grid"} class=${cx(view === "grid" && "is-on")} title="Карточки" onClick=${() => setView("grid")}>
          <${Icon} name="layout-grid" size=${16} />
        </button>
        <button type="button" role="radio" aria-checked=${view === "rows"} class=${cx(view === "rows" && "is-on")} title="Компактно" onClick=${() => setView("rows")}>
          <${Icon} name="rows-3" size=${16} />
        </button>
      </div>`}
    </div>
    <${MoreFilters} open=${more} onClose=${() => setMore(false)} f=${f} set=${set} reset=${reset} total=${total} />
  </div>`;
}

const SINCE = [
  { value: "1h", label: "1 ч" },
  { value: "24h", label: "24 ч" },
  { value: "7d", label: "7 дн" },
  { value: "", label: "Всё" },
];
const STATUSES = [
  { value: "", label: "Новые и в работе" },
  { value: "starred", label: "Избранное" },
  { value: "contacted", label: "Написал" },
  { value: "bought", label: "Купил" },
  { value: "ignored", label: "Скрытые" },
];

function MoreFilters({ open, onClose, f, set, reset, total }) {
  const isMobile = useIsMobile();
  const [draft, setDraft] = useState(f);
  useEffect(() => {
    if (open) setDraft(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const searches = useAsync(() => (open ? api.get("/searches").catch(() => null) : Promise.resolve(null)), [open]);
  const list = (searches.data && (searches.data.items || searches.data.searches || searches.data)) || [];
  const d = (patch) => setDraft({ ...draft, ...patch });
  // live count for the «Показать N» button
  const [count, setCount] = useState(null);
  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    const t = setTimeout(() => {
      api
        .get("/deals", { params: { ...toParams(draft), limit: 1 } })
        .then((r) => alive && setCount(r && (r.total ?? null)))
        .catch(() => alive && setCount(null));
    }, 300);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [open, JSON.stringify(draft)]);
  const apply = () => {
    set(draft);
    onClose();
  };
  const body = html`<div class="more">
    <${Field} label="Где">
      <${Segmented}
        block
        value=${draft.source}
        onChange=${(v) => d({ source: v })}
        options=${[
          { value: "", label: "Везде" },
          { value: "kleinanzeigen", label: "Kleinanzeigen" },
          { value: "ebay", label: "eBay" },
        ]}
      />
    <//>
    ${Array.isArray(list) &&
    list.length > 0 &&
    html`<${Field} label="Поиск">
      <${Select}
        value=${draft.search}
        onChange=${(v) => d({ search: v })}
        options=${[{ value: "", label: "Все поиски" }, ...list.map((s) => ({ value: s.name, label: s.name }))]}
      />
    <//>`}
    <div class="more__two">
      <${Field} label="Цена от">${(id) => html`<${NumberInput} id=${id} value=${draft.pmin} onChange=${(v) => d({ pmin: v })} suffix="€" min=${0} />`}<//>
      <${Field} label="Цена до">${(id) => html`<${NumberInput} id=${id} value=${draft.pmax} onChange=${(v) => d({ pmax: v })} suffix="€" min=${0} />`}<//>
    </div>
    <${Field} label="Прибыль от" help="Только находки, где заработаешь не меньше">
      ${(id) => html`<${NumberInput} id=${id} value=${draft.profit} onChange=${(v) => d({ profit: v })} suffix="€" min=${0} />`}
    <//>
    <${Field} label="Оценка от" aside=${html`<span class="num">${draft.score ?? 0} из 100</span>`}>
      <${Slider} value=${draft.score ?? 0} min=${0} max=${100} step=${5} onChange=${(v) => d({ score: v || null })} format=${(v) => `${v}`} label="Минимальная оценка" />
    <//>
    <${Field} label="Статус">
      <${Select} value=${draft.st} onChange=${(v) => d({ st: v })} options=${STATUSES} />
    <//>
    <${Field} label="Найдено за">
      <${Segmented} block value=${draft.since} onChange=${(v) => d({ since: v })} options=${SINCE} />
    <//>
  </div>`;
  const footer = html`<${Button}
      variant="ghost"
      onClick=${() => {
        reset();
        onClose();
      }}
      >Сбросить<//
    >
    <${Button} variant="primary" onClick=${apply}>${count != null ? `Показать ${count}` : "Показать"}<//>`;
  return isMobile
    ? html`<${Modal} open=${open} onClose=${onClose} title="Фильтры" footer=${footer}>${body}<//>`
    : html`<${Drawer} open=${open} onClose=${onClose} title="Ещё фильтры" subtitle=${total != null ? `Сейчас в ленте: ${total}` : null} width=${400} footer=${footer}>${body}<//>`;
}
