// Search editor (brief §4.5): chooser «Новый поиск» + side panel with Что / Где / Цена /
// Фильтры / Порог выгоды / eBay. Saves with POST /searches or PATCH /searches/{id}.
import { html, cx, useState, useEffect, useLayoutEffect, useRef } from "../../lib/html.js";
import { useAsync, useDebounced } from "../../lib/hooks.js";
import { api } from "../../lib/api.js";
import { addNavigationGuard, navigate } from "../../lib/router.js";
import {
  Icon,
  Button,
  Drawer,
  Modal,
  Field,
  Input,
  NumberInput,
  Slider,
  Segmented,
  Toggle,
  Autocomplete,
  Banner,
  Skeleton,
  toast,
  confirm,
} from "../../ui/index.js";
import "../../features/icons-extra.js";
import { ChipInput } from "../../features/chip-input.js";

export const KINDS = [
  { key: "category", icon: "layout-grid", label: "Категория в моём районе", line: "Все новые объявления категории" },
  { key: "keyword", icon: "search", label: "По словам", line: "Например: steam deck, rtx 3080" },
  { key: "url", icon: "link", label: "Вставить ссылку с Kleinanzeigen", line: "Настрой фильтры на сайте и вставь адрес — я разберу его сам" },
  { key: "wishlist", icon: "heart", label: "Для себя", line: "Что ищешь и до какой цены" },
  { key: "ebay", icon: "gavel", label: "eBay", line: "Аукционы и Sofort-Kaufen" },
];

export const RADII = [0, 5, 10, 20, 30, 50, 100, 150, 200];
const EXCLUDE_SUGGEST = ["defekt", "bastler", "suche", "tausch", "ersatzteil", "nur Tausch"];

/** «Новый поиск» chooser: 5 big options. */
export function KindChooser({ open, onClose, onPick, ebayConfigured }) {
  return html`<${Modal} open=${open} onClose=${onClose} title="Новый поиск" subtitle="Что будем искать?" size="md">
    <div class="kinds">
      ${KINDS.map((k) => {
        const disabled = k.key === "ebay" && ebayConfigured === false;
        return html`<button type="button" class=${cx("kind", disabled && "is-disabled")} onClick=${() => !disabled && onPick(k.key)} aria-disabled=${disabled}>
          <span class="kind__icon"><${Icon} name=${k.icon} size=${22} /></span>
          <span class="kind__text">
            <b>${k.label}</b>
            <span>${disabled ? "Сначала подключи eBay в настройках" : k.line}</span>
          </span>
          <${Icon} name="chevron-right" size=${18} />
        </button>`;
      })}
    </div>
    ${ebayConfigured === false && html`<p class="kinds__note"><a href="/settings/ebay">Подключить eBay</a> — бесплатно, через официальный API.</p>`}
  <//>`;
}

function kindOf(cfg) {
  if (cfg.source === "ebay") return "ebay";
  if (cfg.url) return "url";
  if (cfg.purpose === "personal") return "wishlist";
  if (cfg.category_id && !cfg.query) return "category";
  return "keyword";
}

function blank(kind, base) {
  const where = { location: base.location || "Berlin", radius_km: base.radius_km ?? 30 };
  const common = { name: "", enabled: true, source: "kleinanzeigen", url: null, query: "", category_id: null, category_name: "", min_price: null, max_price: null, purpose: "resale", include_keywords: [], exclude_keywords: [], min_profit: null, min_roi: null, target_price: null };
  if (kind === "ebay") return { ...common, source: "ebay", location: "", radius_km: null, buying_options: [], ebay_conditions: ["USED"], local_pickup_only: false, ending_within_hours: null };
  if (kind === "wishlist") return { ...common, ...where, purpose: "personal" };
  if (kind === "url") return { ...common, url: "", location: "", radius_km: null };
  return { ...common, ...where };
}

function autoName(f, kind) {
  const where = f.location ? ` · ${f.location}${f.radius_km ? ` ${f.radius_km} км` : ""}` : "";
  if (kind === "ebay") return f.query ? `eBay: ${f.query}` : "";
  if (kind === "wishlist") return f.query ? `Для себя: ${f.query}` : "";
  if (kind === "category") return f.category_name ? `${f.category_name}${where}` : "";
  if (kind === "keyword") return f.query ? `«${f.query}»${where}` : "";
  return f.name;
}

const num = (v) => (v === "" || v === undefined ? null : v);
const MAX_EUR = 100000;

/** Client-side guards (P1-9/P1-10): never clamp silently, say what is wrong next to the field. */
function validate(f, kind, personal) {
  const e = {};
  const money = (k, label) => {
    const v = f[k];
    if (v == null) return;
    if (v < 0) e[k] = "Не может быть меньше 0 €";
    else if (v > MAX_EUR) e[k] = `Слишком большая сумма — не больше ${MAX_EUR.toLocaleString("ru-RU")} €`;
    else if (label && v === 0 && k !== "min_price") e[k] = `${label}: 0 € — так я ничего не найду`;
  };
  money("min_price");
  money("max_price", personal ? "Показывать до" : "Цена до");
  money("target_price", "Готов заплатить");
  money("min_profit");
  if (personal && f.target_price != null && f.max_price != null && f.max_price < f.target_price && !e.max_price)
    e.max_price = `Должно быть не меньше, чем «Готов заплатить» (${f.target_price} €)`;
  if (!personal && f.min_price != null && f.max_price != null && f.max_price < f.min_price && !e.max_price)
    e.max_price = "«Цена до» должна быть больше, чем «Цена от»";
  if (f.min_roi != null && (f.min_roi < 0 || f.min_roi > 10)) e.min_roi = "От 0 до 1000 %";
  if (kind === "ebay" && f.ending_within_hours != null && (f.ending_within_hours < 1 || f.ending_within_hours > 240)) e.ending_within_hours = "От 1 до 240 часов";
  return e;
}

/**
 * <SearchEditor open search={view|null} kind="category" base={defaults} onClose onSaved />
 */
export function SearchEditor({ open, search, kind: newKind, base = {}, categories = [], onClose, onSaved, onDelete, readonly }) {
  const editing = Boolean(search);
  const kind = editing ? search.kind || kindOf(search.config || {}) : newKind || "category";
  const [f, setF] = useState(() => (editing ? { ...search.config } : blank(kind, base)));
  const [named, setNamed] = useState(editing);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [ownLimits, setOwnLimits] = useState(Boolean(editing && (search.config.min_profit != null || search.config.min_roi != null)));
  const initial = useRef(null); // snapshot to tell whether there is something to lose
  const bodyRef = useRef(null);

  // layout effect: reset before the first paint, so typing can never land in the old form
  useLayoutEffect(() => {
    if (!open) return;
    const start = editing ? { ...search.config } : blank(kind, base);
    setF(start);
    initial.current = JSON.stringify(start);
    setNamed(editing);
    setErrors({});
    setOwnLimits(Boolean(editing && (search.config.min_profit != null || search.config.min_roi != null)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, search && search.id, newKind]);

  const dirty = open && initial.current != null && JSON.stringify(f) !== initial.current;
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;

  // «Закрыть без сохранения?» for Esc, the scrim, ×, «Отмена» and links elsewhere (P1-11)
  const askDiscard = () =>
    confirm({
      title: "Закрыть без сохранения?",
      message: "Изменения в этом поиске пропадут.",
      confirmLabel: "Не сохранять",
      cancelLabel: "Вернуться к поиску",
      tone: "danger",
    });
  const requestClose = async () => {
    if (dirtyRef.current && !(await askDiscard())) return;
    initial.current = null;
    onClose && onClose();
  };
  useEffect(() => {
    if (!open) return undefined;
    return addNavigationGuard((url) => {
      if (!dirtyRef.current || url.pathname.startsWith("/searches")) return true;
      askDiscard().then((ok) => {
        if (!ok) return;
        dirtyRef.current = false;
        initial.current = null;
        setTimeout(() => navigate(url.pathname + url.search + url.hash), 0);
      });
      return false;
    });
  }, [open]);

  const set = (patch) => {
    setF((prev) => {
      const next = { ...prev, ...patch };
      if (!named) next.name = autoName(next, kind) || prev.name;
      return next;
    });
    setErrors((e) => {
      const out = { ...e };
      Object.keys(patch).forEach((k) => delete out[k]);
      return out;
    });
  };

  const showErrors = (errs) => {
    setErrors(errs);
    // bring the first problem into view (the footer «Сохранить» stays where it is)
    setTimeout(() => {
      const el = bodyRef.current && bodyRef.current.querySelector(".has-error, .field__error");
      if (el) el.scrollIntoView({ block: "center", behavior: "auto" });
    }, 0);
  };

  const save = async () => {
    // the name is optional: I make one up from what is being searched (P1-25)
    const payload = { ...f, name: (f.name || autoName(f, kind) || "").trim() || (kind === "url" ? "Поиск по ссылке" : "Мой поиск") };
    if (!ownLimits) {
      payload.min_profit = null;
      payload.min_roi = null;
    }
    const personal = kind === "wishlist" || f.purpose === "personal";
    const local = validate(payload, kind, personal);
    if (kind === "url" && !payload.url) local.url = "Вставь ссылку со страницы поиска Kleinanzeigen";
    if ((kind === "keyword" || kind === "wishlist" || kind === "ebay") && !(payload.query || "").trim()) local.query = "Напиши, что искать";
    if (kind === "category" && !payload.category_id) local.category_id = "Выбери категорию";
    if (Object.keys(local).length) {
      showErrors(local);
      return;
    }
    setSaving(true);
    try {
      const res = editing ? await api.patch(`/searches/${encodeURIComponent(search.id)}`, payload) : await api.post("/searches", payload);
      toast.success(editing ? "Поиск сохранён" : "Поиск сохранён · первая проверка — обучение", {
        message: editing ? "" : "Сначала изучу цены, уведомления придут со следующей проверки.",
      });
      initial.current = null;
      dirtyRef.current = false;
      onSaved && onSaved(res);
    } catch (e) {
      const fields = e.fields && Object.keys(e.fields).length ? e.fields : null;
      if (fields) {
        // 422: the reasons go next to the fields, not into a toast over the form (P0-7)
        const byKey = {};
        for (const [k, v] of Object.entries(fields)) byKey[k.split(".").pop()] = v;
        showErrors(byKey);
      } else toast.error(`Не получилось сохранить: ${e.message}`, { details: e.details });
    }
    setSaving(false);
  };

  const k = KINDS.find((x) => x.key === kind) || KINDS[0];
  const footer = html`
    ${editing &&
    html`<${Button} variant="danger-ghost" icon="trash-2" onClick=${() => onDelete && onDelete(search)} disabled=${readonly}>Удалить<//>`}
    <span class="grow"></span>
    <${Button} variant="ghost" onClick=${requestClose}>Отмена<//>
    <${Button} variant="primary" icon="check" loading=${saving} onClick=${save} disabled=${readonly}>Сохранить<//>
  `;
  const personalPrice = kind === "wishlist" || f.purpose === "personal";
  return html`<${Drawer}
    open=${open}
    onClose=${requestClose}
    width=${520}
    class="search-drawer"
    title=${editing ? "Изменить поиск" : k.label}
    subtitle=${editing ? search.name : k.line}
    footer=${footer}
  >
    <div class="sedit" ref=${bodyRef}>
      ${Object.keys(errors).filter((k) => k !== "general" && errors[k]).length > 0 &&
      html`<${Banner} tone="danger" icon="circle-alert">Проверь отмеченные поля — ниже написано, что поправить.<//>`}
      ${readonly && html`<${Banner} tone="haggle">Не могу сохранить: нет доступа к файлу настроек.<//>`}
      <section class="sedit__sec">
        <h3 class="sedit__h"><span>1</span>Что</h3>
        ${kind === "url" && html`<${UrlField} f=${f} set=${set} error=${errors.url} setNamed=${setNamed} />`}
        ${kind === "category" && html`<${CategoryPicker} value=${f.category_id} categories=${categories} error=${errors.category_id || errors.query} onPick=${(c) => set({ category_id: c.id, category_name: c.name_de || c.label })} />`}
        ${(kind === "keyword" || kind === "wishlist" || kind === "ebay") &&
        html`<${Field} label=${kind === "wishlist" ? "Что ищешь" : "Слова для поиска"} error=${errors.query} help=${kind === "ebay" ? "Как в поиске eBay: rtx 3080, steam deck 512" : "Например: rtx 3080 или steam deck"}>
          ${(id) => html`<${Input} id=${id} value=${f.query} onChange=${(v) => set({ query: v })} placeholder=${kind === "wishlist" ? "например, RTX 3090" : "например, steam deck"} icon="search" invalid=${Boolean(errors.query)} data-autofocus />`}
        <//>`}
        ${(kind === "category" || kind === "keyword") &&
        html`<${Field} label="Зачем">
          <${Segmented}
            block
            value=${f.purpose}
            onChange=${(v) => set({ purpose: v })}
            options=${[
              { value: "resale", label: "Перепродажа", icon: "trending-up" },
              { value: "personal", label: "Для себя", icon: "piggy-bank" },
            ]}
          />
        <//>`}
        <${Field} label="Название" optional error=${errors.name} help="Как поиск подписан в ленте и уведомлениях. Оставь пустым — придумаю сам.">
          ${(id) => html`<${Input} id=${id} value=${f.name} onChange=${(v) => (setNamed(Boolean(v.trim())), set({ name: v }))} placeholder=${autoName(f, kind) || "Придумаю сам"} />`}
        <//>
      </section>

      ${kind !== "url" &&
      html`<section class="sedit__sec">
        <h3 class="sedit__h"><span>2</span>Где</h3>
        <${WhereFields} f=${f} set=${set} errors=${errors} ebay=${kind === "ebay"} />
      </section>`}

      <section class="sedit__sec">
        <h3 class="sedit__h"><span>${kind === "url" ? 2 : 3}</span>Цена</h3>
        ${personalPrice
          ? html`<div class="sedit__two">
              <${Field} label="Готов заплатить" error=${errors.target_price} help="Твоя цель">
                ${(id) => html`<${NumberInput} id=${id} value=${f.target_price} onChange=${(v) => set({ target_price: num(v), max_price: v != null && v > 0 && (f.max_price == null || f.max_price < v) ? Math.round(v * 1.2) : f.max_price })} suffix="€" min=${0} max=${MAX_EUR} invalid=${Boolean(errors.target_price)} />`}
              <//>
              <${Field} label="Показывать до" error=${errors.max_price} help="Чуть дороже — для торга">
                ${(id) => html`<${NumberInput} id=${id} value=${f.max_price} onChange=${(v) => set({ max_price: num(v) })} suffix="€" min=${0} max=${MAX_EUR} invalid=${Boolean(errors.max_price)} />`}
              <//>
            </div>`
          : html`<div class="sedit__two">
              <${Field} label="Цена от" error=${errors.min_price}>
                ${(id) => html`<${NumberInput} id=${id} value=${f.min_price} onChange=${(v) => set({ min_price: num(v) })} suffix="€" min=${0} max=${MAX_EUR} invalid=${Boolean(errors.min_price)} />`}
              <//>
              <${Field} label="Цена до" error=${errors.max_price} help="Сколько готов вложить в одну вещь">
                ${(id) => html`<${NumberInput} id=${id} value=${f.max_price} onChange=${(v) => set({ max_price: num(v) })} suffix="€" min=${0} max=${MAX_EUR} invalid=${Boolean(errors.max_price)} />`}
              <//>
            </div>`}
      </section>

      <section class="sedit__sec">
        <h3 class="sedit__h"><span>${kind === "url" ? 3 : 4}</span>Фильтры</h3>
        <${Field} label="Исключить слова" help="Объявления с этими словами пропущу">
          ${(id) => html`<${ChipInput} id=${id} label="Исключить слова" value=${f.exclude_keywords || []} onChange=${(v) => set({ exclude_keywords: v })} suggestions=${EXCLUDE_SUGGEST} />`}
        <//>
        <${Field} label="Обязательные слова" help="Хотя бы одно должно быть в объявлении" optional>
          ${(id) => html`<${ChipInput} id=${id} label="Обязательные слова" value=${f.include_keywords || []} onChange=${(v) => set({ include_keywords: v })} placeholder="например: 24GB" />`}
        <//>
      </section>

      ${kind === "ebay" && html`<${EbayFields} f=${f} set=${set} errors=${errors} />`}

      ${!(kind === "wishlist" || f.purpose === "personal") &&
      html`<section class="sedit__sec">
        <h3 class="sedit__h"><span>${kind === "url" ? 4 : 5}</span>Порог выгоды</h3>
        <${Toggle}
          checked=${!ownLimits}
          onChange=${(v) => setOwnLimits(!v)}
          label="Использовать общие настройки"
          description="Минимальная прибыль и ROI из «Настройки → Деньги»"
        />
        ${ownLimits &&
        html`<div class="sedit__two">
          <${Field} label="Мин. прибыль" error=${errors.min_profit}>
            ${(id) => html`<${NumberInput} id=${id} value=${f.min_profit} onChange=${(v) => set({ min_profit: num(v) })} suffix="€" min=${0} max=${MAX_EUR} invalid=${Boolean(errors.min_profit)} />`}
          <//>
          <${Field} label="Мин. ROI" error=${errors.min_roi} help="25 % = заработать четверть вложенного">
            ${(id) => html`<${NumberInput} id=${id} value=${f.min_roi != null ? Math.round(f.min_roi * 100) : null} onChange=${(v) => set({ min_roi: v == null ? null : v / 100 })} suffix="%" min=${0} max=${1000} invalid=${Boolean(errors.min_roi)} />`}
          <//>
        </div>`}
      </section>`}
      ${errors.general && html`<${Banner} tone="danger">${errors.general}<//>`}
    </div>
  <//>`;
}

function UrlField({ f, set, error, setNamed }) {
  const [parsed, setParsed] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const parse = async (raw) => {
    const url = String(raw || "").trim();
    if (!url) return;
    if (!/^https?:\/\//i.test(url) || !/kleinanzeigen\.de/i.test(url)) {
      // say why nothing happened (P1-12)
      setParsed(null);
      setErr(/ebay\./i.test(url) ? "Это ссылка eBay — для eBay выбери «Новый поиск → eBay»" : "Это не ссылка Kleinanzeigen — открой поиск на kleinanzeigen.de и скопируй адрес из строки браузера");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post("/searches/parse-url", { url });
      setParsed(r);
      const s = r.search || {};
      set({
        url,
        name: s.name || r.suggested_name,
        category_id: s.category_id ?? r.category_id,
        category_name: s.category_name || r.category_name || "",
        min_price: r.min_price,
        max_price: r.max_price,
        location: r.location || "",
        radius_km: r.radius_km,
      });
      setNamed(false);
    } catch (e) {
      setParsed(null);
      setErr(e.field("url") || e.message || "Не получилось разобрать ссылку — проверь, что это страница поиска");
    }
    setBusy(false);
  };
  return html`<${Field}
    label="Ссылка на страницу поиска"
    error=${error || err}
    help="Открой kleinanzeigen.de, настрой город, категорию и цену, скопируй адрес из строки браузера"
  >
    ${(id) => html`<div class="sedit__url">
      <${Input}
        id=${id}
        value=${f.url || ""}
        icon="link"
        placeholder="https://www.kleinanzeigen.de/s-berlin/…"
        inputmode="url"
        onChange=${(v) => (setErr(null), set({ url: v }))}
        onBlur=${() => parse(f.url)}
        onPaste=${(e) => {
          const t = (e.clipboardData || window.clipboardData).getData("text");
          setTimeout(() => parse(t.trim()), 0);
        }}
        data-autofocus
      />
      <${Button} variant="secondary" loading=${busy} onClick=${() => parse(f.url)}>Разобрать<//>
    </div>
    ${parsed && html`<p class="sedit__parsed"><${Icon} name="circle-check" size=${16} />${parsed.summary_ru}</p>`}`}
  <//>`;
}

function CategoryPicker({ value, categories, onPick, error }) {
  const [all, setAll] = useState(false);
  const list = categories.length ? categories : [];
  const shown = all ? list : list.filter((c) => c.recommended || c.id === value).concat(list.filter((c) => !c.recommended && c.id !== value).slice(0, Math.max(0, 9 - list.filter((c) => c.recommended || c.id === value).length)));
  if (!list.length) return html`<div class="catgrid">${[0, 1, 2, 3, 4, 5].map(() => html`<${Skeleton} h=${64} radius="var(--r-md)" />`)}</div>`;
  return html`<div class="field ${error ? "has-error" : ""}">
    <div class="field__top"><span class="field__label">Категория</span></div>
    <div class="catgrid" role="radiogroup" aria-label="Категория">
      ${shown.map(
        (c) => html`<button type="button" role="radio" aria-checked=${c.id === value} class=${cx("cat", c.id === value && "is-on")} onClick=${() => onPick(c)}>
          <${Icon} name=${c.icon || "tag"} size=${20} />
          <span class="cat__de">${c.name_de || c.label}</span>
          ${c.name_ru && html`<span class="cat__ru">${c.name_ru}</span>`}
          ${c.id === value && html`<span class="cat__check"><${Icon} name="check" size=${12} stroke=${3} /></span>`}
        </button>`,
      )}
    </div>
    ${list.length > shown.length || all
      ? html`<button type="button" class="linkish" onClick=${() => setAll(!all)}>${all ? "Только основные" : `Все категории (${list.length})`}</button>`
      : null}
    ${error && html`<p class="field__error" role="alert"><${Icon} name="circle-alert" size=${14} />${error}</p>`}
  </div>`;
}

function WhereFields({ f, set, errors, ebay }) {
  const [text, setText] = useState(f.location || "");
  useEffect(() => setText(f.location || ""), [f.location]);
  const q = useDebounced(text, 250);
  const places = useAsync(() => api.get("/locations", { params: { q, limit: 8 } }).then((r) => r.items || []).catch(() => []), [q]);
  const options = (places.data || []).map((p) => ({ value: p.value || p.name, label: p.name, hint: p.label !== p.name ? p.label : p.plz || "", icon: "map-pin" }));
  return html`
    ${ebay &&
    html`<${Toggle}
      checked=${f.local_pickup_only}
      onChange=${(v) => set({ local_pickup_only: v, radius_km: v && !f.radius_km ? 30 : f.radius_km })}
      label="Только самовывоз в радиусе"
      description="Иначе ищу по всей Германии с доставкой"
    />`}
    ${(!ebay || f.local_pickup_only) &&
    html`<${Field} label=${ebay ? "Почтовый индекс" : "Город или индекс"} error=${errors.location}>
        <${Autocomplete}
          value=${text}
          onInput=${(v) => {
            setText(v);
            set({ location: v, location_id: null });
          }}
          options=${options}
          loading=${places.loading}
          placeholder="Berlin или 10115"
          icon="map-pin"
          onSelect=${(o) => {
            setText(o.value);
            set({ location: o.value, location_id: null });
          }}
        />
      <//>
      <${Field} label="Радиус" aside=${html`<b class="num">${f.radius_km ? `${f.radius_km} км` : "только город"}</b>`} error=${errors.radius_km}>
        <${Slider}
          value=${f.radius_km ?? 0}
          steps=${RADII}
          onChange=${(v) => set({ radius_km: v || null })}
          format=${(v, short) => (short ? (v ? `${v}` : "город") : v ? `${v} км` : "только город")}
          label="Радиус"
          bubble="none"
        />
      <//>`}
  `;
}

function EbayFields({ f, set, errors }) {
  const opts = f.buying_options || [];
  const toggle = (key) => set({ buying_options: opts.includes(key) ? opts.filter((o) => o !== key) : [...opts, key] });
  const conds = f.ebay_conditions || [];
  const toggleCond = (key) => set({ ebay_conditions: conds.includes(key) ? conds.filter((o) => o !== key) : [...conds, key] });
  return html`<section class="sedit__sec">
    <h3 class="sedit__h"><span><${Icon} name="gavel" size=${12} /></span>eBay</h3>
    <${Field} label="Тип" help="Ничего не выбрано — все типы">
      <div class="chips-row">
        ${[
          ["FIXED_PRICE", "Sofort-Kaufen"],
          ["AUCTION", "Аукцион"],
          ["BEST_OFFER", "Preisvorschlag"],
        ].map(([k, l]) => html`<button type="button" class=${cx("fchip", opts.includes(k) && "is-on")} aria-pressed=${opts.includes(k)} onClick=${() => toggle(k)}>${l}</button>`)}
      </div>
    <//>
    <${Field} label="Состояние">
      <div class="chips-row">
        ${[
          ["USED", "Б/у"],
          ["NEW", "Новое"],
          ["UNSPECIFIED", "Не указано"],
        ].map(([k, l]) => html`<button type="button" class=${cx("fchip", conds.includes(k) && "is-on")} aria-pressed=${conds.includes(k)} onClick=${() => toggleCond(k)}>${l}</button>`)}
      </div>
    <//>
    ${opts.includes("AUCTION") &&
    html`<${Field} label="Аукционы, которые заканчиваются в ближайшие" error=${errors.ending_within_hours} help="Классический трюк: дешёвые аукционы в последние часы">
      ${(id) => html`<${NumberInput} id=${id} value=${f.ending_within_hours} onChange=${(v) => set({ ending_within_hours: num(v) })} suffix="ч" min=${0} />`}
    <//>`}
  </section>`;
}

/** Confirm + DELETE /searches/{id}. Resolves true when deleted. */
export async function deleteSearch(search) {
  const ok = await confirm({
    title: `Удалить поиск «${search.name}»?`,
    message: "Найденные объявления останутся в ленте.",
    confirmLabel: "Удалить поиск",
    tone: "danger",
  });
  if (!ok) return false;
  try {
    await api.del(`/searches/${encodeURIComponent(search.id)}`);
    toast.success("Поиск удалён");
    return true;
  } catch (e) {
    toast.error(`Не получилось удалить: ${e.message}`);
    return false;
  }
}

