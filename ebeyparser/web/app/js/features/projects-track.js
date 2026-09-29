// «Сборки» tracking view (spec §6): totals bar, a part card with its best offer / runner-ups /
// variants / price trend, the «Купил» dialog, the «Начать отслеживание» confirmation with the load
// meter, the alerts list, the GPU value ranking and the delete confirmation.
import { html, cx, useState, useEffect, useRef } from "../lib/html.js";
import { useIsMobile, useDebounced } from "../lib/hooks.js";
import { api } from "../lib/api.js";
import { appStore, useStore } from "../lib/store.js";
import { plural, count, everyLabel, number } from "../lib/format.js";
import { verdictTone, DECISION_ICONS } from "../lib/tones.js";
import {
  Icon,
  Button,
  Modal,
  Drawer,
  Field,
  NumberInput,
  NumberStepper,
  Input,
  Toggle,
  Checkbox,
  Meter,
  Banner,
  Ring,
  Skeleton,
  Select,
  Spinner,
  toast,
  confirm,
} from "../ui/index.js";
import { DealImage } from "./deal-card.js";
import { LocationPicker, RADIUS_STEPS, radiusText } from "../setup/where.js";
import { ru, money, approx, lower, Hint, projectsApi, cacheView } from "./projects-common.js";
import { SlotCard } from "./projects-plan.js";

// ================================================================== totals
export function TotalsBar({ p }) {
  const t = p.totals || {};
  const budget = p.budget || t.budget;
  const complete = t.best_complete && t.best_total != null;
  const main = complete ? money(t.best_total) : t.estimated_total ? approx(t.estimated_total) : "—";
  const tone = t.fits_budget === true ? "t-green" : t.over_budget > 0 ? "t-amber" : "";
  const ratio = budget ? (t.estimated_total || 0) / budget : null;
  const over = ratio != null && ratio > 1;
  return html`<section class="pj-totals" aria-label="Итоги сборки">
    <div class="pj-totals__main">
      <div class="pj-totals__money">
        <span class="overline">${complete ? "Лучшая сумма сейчас" : "Вся сборка сейчас"}</span>
        <div class="pj-totals__line">
          <b class=${cx("pj-totals__value num", tone)}>${main}</b>
          ${budget && html`<span class="pj-totals__of num">из ${money(budget)}</span>`}
          ${t.over_budget > 0 && html`<span class="tag tone-haggle">над бюджетом на ${money(t.over_budget)}</span>`}
        </div>
        ${!complete && (t.missing_offers || []).length > 0 && html`<p class="pj-totals__caption">для части деталей — по рынку: ${t.missing_offers.map(lower).join(", ")}</p>`}
      </div>
      <div class="pj-totals__ring">
        <${Ring} value=${p.progress || 0} size=${52} stroke=${5} tone="brand"><span class="num">${t.slots_done ?? p.slots_done}/${t.slots_total ?? p.slots_total}</span><//>
        <span class="pj-totals__progress">${ru(p.progress_label)}</span>
      </div>
    </div>
    ${ratio != null &&
    html`<div class=${cx("pj-totals__bar", over && "is-over")} role="meter" aria-label="Сумма от бюджета" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${Math.round(Math.min(ratio, 2) * 100)}>
      <span class="pj-totals__fill" style=${{ width: `${Math.min(1, ratio) * 100}%` }}></span>
      ${over && html`<span class="pj-totals__overflow" style=${{ width: `${Math.min(0.5, ratio - 1) * 100}%` }}></span>`}
    </div>`}
    <p class="pj-totals__sub">
      <span>Потрачено <b class="num">${money(t.spent || 0)}</b></span>
      ${t.remaining_budget != null && html`<span>осталось <b class="num">${money(t.remaining_budget)}</b></span>`}
      ${t.savings_vs_new > 0 && html`<span class="t-green">б/у выходит дешевле на ~${money(t.savings_vs_new)}</span>`}
    </p>
    ${t.new_label_ru && html`<p class="pj-totals__new">${ru(t.new_label_ru)}</p>`}
    ${t.stretch_label_ru && html`<p class=${cx("pj-totals__stretch", t.ratio != null && t.ratio < 0.65 ? "t-red" : t.ratio != null && t.ratio < 0.9 ? "t-amber" : "")}>${ru(t.stretch_label_ru)}</p>`}
  </section>`;
}

// ================================================================== trend
export function Sparkline({ points = [], direction }) {
  const values = points.map((p) => p.median).filter((v) => v != null);
  if (values.length < 2) return null;
  const w = 60;
  const h = 20;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const x = (i) => (i / (values.length - 1)) * (w - 4) + 2;
  const y = (v) => h - 3 - ((v - min) / (max - min || 1)) * (h - 6);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = values.length - 1;
  return html`<svg class=${cx("pj-spark", `is-${direction || "flat"}`)} width=${w} height=${h} viewBox=${`0 0 ${w} ${h}`} aria-hidden="true">
    <path d=${d} fill="none" />
    <circle cx=${x(last)} cy=${y(values[last])} r="2.5" />
  </svg>`;
}

function Trend({ trend }) {
  if (!trend) return null;
  const tone = { down: "t-green", up: "t-amber", flat: "", unknown: "muted" }[trend.direction] || "muted";
  return html`<span class="pj-trend">
    <${Sparkline} points=${trend.points || []} direction=${trend.direction} />
    <span class=${cx("pj-trend__label", tone)}>${ru(trend.label_ru)}</span>
  </span>`;
}

// ================================================================== offers
function VerdictPill({ offer }) {
  if (!offer.verdict || offer.verdict === "none") return null;
  const tone = verdictTone({ action: offer.action, verdict: offer.verdict });
  const icon = DECISION_ICONS[offer.action] || (offer.verdict === "buy" ? "trending-up" : "eye");
  const toneCls = { green: "tone-profit", amber: "tone-haggle", violet: "tone-bid", red: "tone-danger" }[tone] || "tone-neutral";
  return html`<span class=${cx("pj-verdict", toneCls)}><${Icon} name=${icon} size=${12} stroke=${2.25} />${offer.verdict_label}</span>`;
}

function Flags({ flags = [] }) {
  const warn = flags.filter((f) => f.level === "warn");
  const chips = flags.filter((f) => f.level !== "warn");
  const [all, setAll] = useState(false);
  const shownChips = all ? chips : chips.slice(0, 2);
  if (!flags.length) return null;
  return html`<div class="pj-flags">
    ${warn.map((f) => html`<p class="pj-flag-warn"><${Icon} name="triangle-alert" size=${14} />${ru(f.text_ru)}</p>`)}
    ${chips.length > 0 &&
    html`<div class="chips-row">
      ${shownChips.map((f) => html`<span class=${cx("tag pj-flag-chip", f.level === "good" && "tone-profit")} title=${ru(f.text_ru)}>${f.level === "good" ? html`<${Icon} name="check" size=${11} stroke=${2.5} />` : ""}${ru(f.text_ru)}</span>`)}
      ${chips.length > shownChips.length && html`<button type="button" class="linkish" onClick=${() => setAll(true)}>ещё ${chips.length - shownChips.length}</button>`}
    </div>`}
  </div>`;
}

/**
 * One offer (a compact deal row, brief §4.2.4). `label` = «1-я карта» when several units are needed.
 * `onBought(offer)`, `onDeal(offer)` open the dialog / the deal drawer.
 */
export function OfferRow({ offer, label, onBought, onDeal, compact = false, gpu = false }) {
  const meta = [
    offer.distance_km != null ? `${number(offer.distance_km, offer.distance_km < 10 ? 1 : 0)} км` : null,
    offer.location,
    offer.first_seen_label ? `нашёл ${/^\d/.test(offer.first_seen_label) ? "в " : ""}${offer.first_seen_label}` : null,
    offer.source === "ebay" ? offer.source_label : null,
  ].filter(Boolean);
  const vsTone = offer.under_target ? "t-green" : "t-amber";
  return html`<article class=${cx("pj-offer", compact && "pj-offer--compact", offer.under_target && "is-under")}>
    <${DealImage} src=${offer.image} class="pj-offer__img" />
    <div class="pj-offer__main">
      ${label && html`<span class="pj-offer__label">${label}</span>`}
      <a class="pj-offer__title" href=${offer.url} target="_blank" rel="noopener noreferrer"><span>${offer.title}</span></a>
      <div class="pj-offer__meta">
        <${VerdictPill} offer=${offer} />
        ${meta.length > 0 && html`<span>${meta.join(" · ")}</span>`}
      </div>
      ${!compact &&
      html`
        ${(offer.value || offer.value_score != null) &&
        html`<div class="chips-row">
          ${offer.value && offer.value.text_ru && html`<span class="tag">${ru(offer.value.text_ru)}</span>`}
          ${gpu && offer.value_score != null && html`<span class="tag tone-profit num">${Math.round(offer.value_score)}/100 · ${offer.value_label}</span>`}
        </div>`}
        <${Flags} flags=${offer.flags} />
        ${offer.stale && html`<p class="pj-offer__stale"><${Icon} name="clock" size=${13} />${ru(offer.stale_ru)}</p>`}
      `}
    </div>
    <div class="pj-offer__side">
      <span class="pj-offer__price num">${money(offer.unit_cost ?? offer.price)}${offer.negotiable ? html` <span class="vb">VB</span>` : ""}</span>
      ${offer.shipping_cost > 0 && html`<span class="pj-offer__ship num">${money(offer.price)} + ${money(offer.shipping_cost)} доставка</span>`}
      ${offer.vs_target_label && html`<span class=${cx("pj-offer__vs", vsTone)}>${ru(offer.vs_target_label)}</span>`}
    </div>
    <div class="pj-offer__actions">
      <${Button} size="sm" variant="secondary" icon="external-link" href=${offer.url}>Открыть<//>
      ${onDeal && html`<${Button} size="sm" variant="ghost" icon="message-square" onClick=${() => onDeal(offer)}>Сделка<//>`}
      ${onBought && html`<${Button} size="sm" variant=${offer.under_target ? "primary" : "secondary"} icon="package-check" onClick=${() => onBought(offer)}>Купил<//>`}
    </div>
  </article>`;
}

// ================================================================== part card (tracking)
/**
 * handlers: onBought(slot, offer?), onUndo(slot), onSlot(key, patch, opts), onPick(key, opt),
 * onDeal(offer), onAlt(slot, alt)
 */
export function TrackSlot({ p, slot, handlers = {}, busy, justStarted, interval, full = false }) {
  const phone = useIsMobile();
  const [more, setMore] = useState(full);
  const [details, setDetails] = useState(false);
  const need = slot.need_qty ?? slot.qty;
  const gpu = slot.kind === "gpu";
  const bought = slot.status === "bought";
  const off = slot.status === "have" || slot.status === "skipped";
  const tracking = p.status === "tracking" || p.status === "paused";
  const unit = slot.qty > 1 ? " за штуку" : "";
  const picked = slot.picked || [];
  const runners = (slot.runner_ups || []).filter((o) => !picked.some((x) => x.ad_id === o.ad_id));
  const extra = Math.max(0, (slot.offers_count || 0) - Math.max(1, picked.length));
  // only the variants that are really watched (their own search) or already have offers
  const watched = new Set((p.searches_list || []).filter((x) => x.slot === slot.key && x.enabled !== false).map((x) => String(x.option).replace(/@ebay$/, "")));
  const alts = (slot.alternatives || []).filter((a) => a.option !== slot.chosen && (a.best || watched.has(a.option)));

  const head = html`<header class="pj-tslot__head">
    <div class="pj-tslot__titles">
      <h3 class="pj-tslot__title">${slot.label}</h3>
      <p class="pj-tslot__sub">
        ${!off && !bought && slot.chosen_label && html`<span>${ru(slot.chosen_label)}</span>`}
        ${!off && !bought && need > 1 && html`<span>нужно ${need}</span>`}
        ${!off && !bought && slot.target_unit != null && html`<${Hint} tip="Цель — сколько ты готов заплатить за эту часть. Я считаю её так, чтобы вся сборка уложилась в бюджет, и минимум на 10 % ниже рынка." title="Цель" class="pj-dotted">цель ${money(slot.target_unit)}${unit}<//>`}
      </p>
    </div>
    <span class=${cx("pj-slot__status", `st-${slot.status}`, tracking && slot.status === "open" && p.status === "tracking" && "is-live")}>${slot.status_label}</span>
    ${!off && !bought && slot.trend && !(phone && slot.trend.direction === "unknown" && !(slot.trend.points || []).length) && html`<${Trend} trend=${slot.trend} />`}
  </header>`;

  // bought / have / skipped: one quiet row with «Отменить»
  if (off || bought) {
    const text = bought
      ? `Куплено${slot.spent ? ` за ${money(slot.spent)}` : ""}${(slot.purchases || []).length && slot.purchases[slot.purchases.length - 1].at_label ? ` · ${slot.purchases[slot.purchases.length - 1].at_label}` : ""}`
      : slot.status === "have"
        ? "Уже есть"
        : "Не нужно";
    return html`<section class=${cx("pj-tslot is-done", `is-${slot.status}`)} id=${`slot-${slot.key}`} aria-label=${slot.label}>
      <div class="pj-tslot__done">
        <span class="pj-tslot__check"><${Icon} name=${bought ? "package-check" : slot.status === "have" ? "check" : "circle-slash"} size=${18} /></span>
        <div class="grow">
          <b>${slot.label}</b>
          <span class="pj-tslot__done-text">${bought && slot.chosen_label ? `${ru(slot.chosen_label)} · ` : ""}${text}</span>
        </div>
        ${bought
          ? handlers.onUndo && html`<${Button} size="sm" variant="ghost" icon="undo-2" loading=${busy === `undo:${slot.key}`} onClick=${() => handlers.onUndo(slot)}>Отменить покупку<//>`
          : handlers.onSlot && html`<${Button} size="sm" variant="ghost" icon="rotate-ccw" onClick=${() => handlers.onSlot(slot.key, { status: "open" }, { undoOf: slot.status })}>Снова нужно<//>`}
      </div>
    </section>`;
  }

  const partial = (slot.bought_qty || 0) > 0;
  return html`<section class=${cx("pj-tslot", slot.best && slot.best.under_target && "has-deal")} id=${`slot-${slot.key}`} aria-label=${slot.label}>
    ${head}
    ${partial &&
    html`<p class="pj-tslot__partial"><${Icon} name="package-check" size=${14} />Куплено ${slot.bought_qty} из ${slot.qty}${slot.spent ? ` за ${money(slot.spent)}` : ""}
      ${handlers.onUndo && html`<button type="button" class="linkish" onClick=${() => handlers.onUndo(slot)}>Отменить</button>`}</p>`}
    ${slot.best
      ? html`
          <div class="pj-tslot__offers">
            ${(picked.length > 1 ? picked : [slot.best]).map(
              (o, i) => html`<${OfferRow}
                key=${o.ad_id}
                offer=${o}
                gpu=${gpu}
                compact=${i > 0}
                label=${picked.length > 1 ? `${i + 1}-я ${gpu ? "карта" : "штука"}` : null}
                onBought=${handlers.onBought && ((offer) => handlers.onBought(slot, offer))}
                onDeal=${handlers.onDeal}
              />`,
            )}
            ${more && runners.map((o) => html`<${OfferRow} key=${o.ad_id} offer=${o} gpu=${gpu} compact onBought=${handlers.onBought && ((offer) => handlers.onBought(slot, offer))} onDeal=${handlers.onDeal} />`)}
          </div>
          ${extra > 0 &&
          runners.length > 0 &&
          !full &&
          html`<button type="button" class="pj-more" aria-expanded=${more} onClick=${() => setMore(!more)}>
            <${Icon} name=${more ? "chevron-up" : "chevron-down"} size=${16} />${more ? "Скрыть" : `Ещё ${extra} ${plural(extra, "предложение", "предложения", "предложений")}`}
          </button>`}
        `
      : html`<div class="pj-tslot__wait">
          <${Icon} name="hourglass" size=${16} />
          <span>Пока нет предложений — жду${justStarted ? html`. <span class="muted">Первая проверка изучает цены${interval ? ` (~${interval} мин)` : ""}</span>` : ""}</span>
        </div>`}
    ${html`<div class="pj-tslot__foot">
      ${alts.length > 0 &&
      html`<div class="pj-alts">
        <span class="pj-alts__label">Варианты:</span>
        ${alts.map(
          (a) => html`<button type="button" class="pj-alt" key=${a.option} onClick=${() => handlers.onAlt && handlers.onAlt(slot, a)}>
            ${ru(shortAlt(a.label))} ${a.best ? html`<b class="num">от ${money(a.best.unit_cost)}</b>` : html`<span class="muted">— пока нет</span>`}
          </button>`,
        )}
      </div>`}
      <span class="grow"></span>
      ${phone && !full && html`<a class="linkish" href=${`/projects/${p.id}/slot/${slot.key}`}>Всё о части<${Icon} name="chevron-right" size=${14} /></a>`}
      <button type="button" class="linkish" aria-expanded=${details} onClick=${() => setDetails(!details)}>
        <${Icon} name="sliders-horizontal" size=${14} />${details ? "Скрыть детали" : "Варианты и цель"}
      </button>
      ${handlers.onBought && html`<button type="button" class="linkish" onClick=${() => handlers.onBought(slot, null)}><${Icon} name="package-check" size=${14} />Купил в другом месте</button>`}
    </div>`}
    ${details &&
    html`<div class="pj-tslot__details">
      <${SlotCard} slot=${slot} plan=${p} mode="tracking" onPick=${handlers.onPick} onSlot=${handlers.onSlot} busy=${busy} bare />
      ${handlers.onSlot &&
      html`<div class="pj-tslot__status-actions">
        <${Button} size="sm" variant="ghost" icon="check" onClick=${() => handlers.onSlot(slot.key, { status: "have" }, { undoOf: "open" })}>Уже есть<//>
        <${Button} size="sm" variant="ghost" icon="circle-slash" onClick=${() => handlers.onSlot(slot.key, { status: "skipped" }, { undoOf: "open" })}>Не нужно<//>
      </div>`}
    </div>`}
  </section>`;
}

/** «2× Tesla P40 24 ГБ» stays; long labels are cut at the colon. */
function shortAlt(label) {
  return String(label || "").split(": ")[0];
}

// ================================================================== «Купил»
export function BoughtDialog({ open, p, slot, offer, onClose, onView }) {
  const need = slot ? slot.need_qty || 1 : 1;
  const [qty, setQty] = useState(1);
  const [price, setPrice] = useState(null);
  const [touched, setTouched] = useState(false);
  const [note, setNote] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open || !slot) return;
    const q = offer ? 1 : need;
    setQty(q);
    setPrice(offer ? offer.unit_cost ?? offer.price : null);
    setTouched(false);
    setNote("");
    setErr(null);
  }, [open, slot && slot.key, offer && offer.ad_id]);
  if (!slot) return null;
  const changeQty = (q) => {
    setQty(q);
    if (!touched && offer) setPrice(Math.round((offer.unit_cost ?? offer.price) * q * 100) / 100);
  };
  const submit = async () => {
    if (price == null || !(price > 0)) return setErr("Цена должна быть больше нуля");
    if (price > 100000) return setErr("Не больше 100 000 €");
    setBusy(true);
    setErr(null);
    try {
      const res = await projectsApi.bought(p.id, slot.key, {
        price,
        qty: need > 1 ? qty : undefined,
        ad_id: offer ? offer.ad_id : undefined,
        option: offer && offer.option && offer.option !== slot.chosen ? offer.option : undefined,
        note: note.trim(),
      });
      const view = res && res.project;
      if (view) {
        cacheView(view);
        onView(view);
      }
      onClose();
      toast.success(ru(res.message_ru) || "Записал покупку", {
        action: {
          label: "Отменить",
          onClick: async () => {
            try {
              const back = await projectsApi.unbought(p.id, slot.key);
              let view = back && back.project;
              // the purchase moved the deal to «Купил»; the undo of the build does not move it back
              // (API gap) — put the deal where it was, so the offer shows up in the build again
              if (offer && offer.ad_id) {
                const prev = offer.status && offer.status !== "bought" ? offer.status : "new";
                await api.patch(`/deals/${encodeURIComponent(offer.ad_id)}`, { status: prev }).catch(() => null);
                view = (await projectsApi.get(p.id).catch(() => null)) || view;
              }
              if (view) {
                cacheView(view);
                onView(view);
              }
              toast.info(ru(back && back.message_ru) || "Покупка отменена");
            } catch (e) {
              toast.error(e);
            }
          },
        },
      });
      if (res.warning_ru) toast.warning(ru(res.warning_ru));
    } catch (e) {
      if (e.status === 422) setErr(e.field("price") || e.message);
      else toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    title=${`Купил: ${ru(offer && offer.option && offer.option !== slot.chosen ? optionLabel(slot, offer.option) : slot.chosen_label || slot.label)}`}
    subtitle=${offer ? offer.title : null}
    icon="package-check"
    size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Отмена<//><${Button} variant="primary" icon="check" loading=${busy} onClick=${submit}>Записать покупку<//>`}
  >
    <form
      class="pj-form"
      onSubmit=${(e) => {
        e.preventDefault();
        submit();
      }}
    >
      ${need > 1 &&
      html`<${Field} label="Сколько штук" help=${`Нужно ещё ${need}`}>
        <${NumberStepper} value=${qty} onChange=${changeQty} min=${1} max=${need} label="Сколько штук" format=${(v) => `${v} шт.`} />
      <//>`}
      <${Field} label="Цена" error=${err} help=${qty > 1 ? `Сколько заплатил всего за ${qty} шт., с доставкой` : "Сколько заплатил, с доставкой"}>
        ${(id) => html`<${NumberInput}
          id=${id}
          value=${price}
          onChange=${(v) => {
            setPrice(v);
            setTouched(true);
            setErr(null);
          }}
          min=${0.01}
          max=${100000}
          suffix="€"
          invalid=${Boolean(err)}
          class="num"
          data-autofocus
        />`}
      <//>
      <${Field} label="Заметка" optional>
        ${(id) => html`<${Input} id=${id} value=${note} onChange=${setNote} maxLength=${300} placeholder="Например: с гарантией до 2027" />`}
      <//>
      ${offer && html`<p class="pj-form__hint"><${Icon} name="wallet" size=${14} />Сделка тоже переедет в «Мои сделки → Купил».</p>`}
      <button type="submit" hidden></button>
    </form>
  <//>`;
}

function optionLabel(slot, key) {
  const o = (slot.options || []).find((x) => x.key === key);
  return o ? o.label : slot.chosen_label;
}

// ================================================================== «Начать отслеживание»
function LoadCard({ est }) {
  if (!est) return null;
  const tone = { ok: "green", warn: "amber", danger: "red" }[est.level] || "green";
  return html`<div class="pj-load">
    <${Meter}
      label="Нагрузка на Kleinanzeigen"
      value=${est.pages_per_hour || 0}
      max=${est.cap_per_hour || 150}
      marker=${0.4}
      tone=${tone}
      valueText=${ru(est.pages_label_ru) || `~${number(est.pages_per_hour || 0)} из ${number(est.cap_per_hour || 150)} страниц в час`}
      hint=${ru(est.short_ru) || `Проверяю ${everyLabel(est.interval_minutes)}`}
    />
  </div>`;
}

/**
 * Confirmation sheet: dry run first («Создам 10 поисков для себя» + the load meter), options,
 * then the real call. onStarted(view).
 */
export function TrackSheet({ open, p, onClose, onStarted }) {
  const app = useStore(appStore, (s) => s.app);
  const ebayOk = Boolean(app && app.features && app.features.ebay);
  const [opts, setOpts] = useState({ alternatives: true, alternatives_scope: "key", ebay: false, price_filter: false });
  const [place, setPlace] = useState(null); // {location, label, radius_km} when changed
  const [region, setRegion] = useState(null);
  const [advanced, setAdvanced] = useState(false);
  const [dry, setDry] = useState({ data: null, error: null, loading: false });
  const [busy, setBusy] = useState(false);
  const [readOnly, setReadOnly] = useState(null);
  const body = { ...opts, ...(place ? { location: place.location, radius_km: place.radius_km } : {}) };
  const key = useDebounced(JSON.stringify(body), 250);
  const seq = useRef(0);

  useEffect(() => {
    if (!open) return;
    setReadOnly(null);
    setAdvanced(false);
    setPlace(null);
    setOpts({ alternatives: true, alternatives_scope: "key", ebay: false, price_filter: false });
    if (!p.location)
      api
        .get("/settings")
        .then((s) => s && s.region && setRegion({ label: s.region.location_label || s.region.location, radius_km: s.region.radius_km }))
        .catch(() => {});
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const id = ++seq.current;
    setDry((d) => ({ ...d, loading: true, error: null }));
    projectsApi
      .track(p.id, { ...JSON.parse(key), dry_run: true })
      .then((data) => id === seq.current && setDry({ data, error: null, loading: false }))
      .catch((error) => id === seq.current && setDry({ data: null, error, loading: false }));
  }, [open, key]);

  const d = dry.data;
  const created = (d && d.created) || [];
  const updated = (d && d.updated) || [];
  const heavy = d && d.estimate && (d.estimate.level === "warn" || d.estimate.level === "danger");
  const where = place ? `${place.label || place.location} · ${radiusText(place.radius_km)}` : p.location ? `${p.location}${p.radius_km != null ? ` · ${radiusText(p.radius_km)}` : ""}` : region ? `${region.label} · ${radiusText(region.radius_km)}` : "как в твоих поисках";

  const start = async () => {
    setBusy(true);
    setReadOnly(null);
    try {
      const res = await projectsApi.track(p.id, { ...body, dry_run: false });
      if (res && res.project) cacheView(res.project);
      toast.success(ru(res.message_ru) || "Слежу за сборкой");
      onStarted(res.project);
    } catch (e) {
      if (e.status === 403) setReadOnly(e);
      else toast.error(e);
    } finally {
      setBusy(false);
    }
  };

  const n = created.length;
  const title = d ? (n ? `Создам ${count(n, "поиск", "поиска", "поисков")} для себя` : updated.length ? `Обновлю ${count(updated.length, "поиск", "поиска", "поисков")}` : "Поиски уже есть") : "Начать отслеживание";
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    title=${title}
    subtitle="Каждая часть — отдельный поиск «для себя» с твоей ценой"
    icon="radar"
    size="md"
    class="pj-track-modal"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Отмена<//><${Button} variant="primary" icon="radar" loading=${busy} disabled=${!d || Boolean(readOnly)} onClick=${start}>Начать отслеживание<//>`}
  >
    <div class="pj-track">
      ${readOnly && html`<${Banner} tone="haggle" title="Не могу сохранить настройки" details=${readOnly.details}>${readOnly.message}<//>`}
      ${dry.error && !readOnly && html`<${Banner} tone="danger" title="Не получилось посчитать" details=${dry.error.details}>${dry.error.message}<//>`}
      ${!d && !dry.error
        ? html`<div class="pj-track__sk"><${Skeleton} h=${14} w="70%" /><${Skeleton} h=${56} radius="var(--r-md)" /></div>`
        : d &&
          html`
            <details class="pj-track__names">
              <summary><${Icon} name="chevron-right" size=${14} />${created.length ? "Какие поиски" : "Список поисков"}${dry.loading ? html` <${Spinner} size=${12} />` : ""}</summary>
              <ul>
                ${created.map((name) => html`<li><${Icon} name="plus" size=${12} />${name}</li>`)}
                ${updated.map((name) => html`<li class="muted"><${Icon} name="refresh-cw" size=${12} />${name}</li>`)}
              </ul>
            </details>
            <${LoadCard} est=${d.estimate} />
            ${heavy && opts.alternatives && html`<p class="pj-track__hint t-amber"><${Icon} name="lightbulb" size=${14} />Можно следить только за главным вариантом — выключи „варианты“.</p>`}
          `}
      <${Toggle}
        checked=${opts.alternatives}
        onChange=${(v) => setOpts({ ...opts, alternatives: v })}
        label="Следить и за вариантами видеокарт"
        description="Сообщу, если другой вариант выйдет выгоднее — например, RTX 3090 дешевле цели"
      />
      <button type="button" class="pj-track__adv" aria-expanded=${advanced} onClick=${() => setAdvanced(!advanced)}>
        <${Icon} name=${advanced ? "chevron-up" : "chevron-down"} size=${16} />Ещё настройки
      </button>
      ${advanced &&
      html`<div class="pj-track__more">
        <${Toggle}
          checked=${opts.alternatives_scope === "all"}
          disabled=${!opts.alternatives}
          onChange=${(v) => setOpts({ ...opts, alternatives_scope: v ? "all" : "key" })}
          label="За вариантами всех частей"
          description="Больше поисков — больше нагрузка"
        />
        <${Toggle}
          checked=${opts.ebay && ebayOk}
          disabled=${!ebayOk}
          onChange=${(v) => setOpts({ ...opts, ebay: v })}
          label="Также на eBay"
          description=${ebayOk ? "Серверные карты чаще продают на eBay" : html`Сначала подключи eBay — <a href="/settings/ebay">в настройках</a>`}
        />
        <${Toggle}
          checked=${opts.price_filter}
          onChange=${(v) => setOpts({ ...opts, price_filter: v })}
          label="Искать только в пределах цены"
          description="Меньше объявлений, но цены рынка тогда не изучаются — тренды и „рынок ~…“ станут хуже"
        />
        <${PlacePicker} where=${where} place=${place} base=${p} onChange=${setPlace} />
      </div>`}
    </div>
  <//>`;
}

function PlacePicker({ where, place, base, onChange }) {
  const [edit, setEdit] = useState(false);
  const [loc, setLoc] = useState({ location: base.location || "", label: base.location || "" });
  const [radius, setRadius] = useState(base.radius_km ?? 30);
  if (!edit)
    return html`<div class="pj-place">
      <span><${Icon} name="map-pin" size=${14} />Где: <b>${where}</b></span>
      <button type="button" class="linkish" onClick=${() => setEdit(true)}>Изменить</button>
    </div>`;
  const apply = (next, r) => next.location && onChange({ location: next.location, label: next.label, radius_km: r });
  return html`<div class="pj-place pj-place--edit">
    <${Field} label="Где искать">
      <${LocationPicker}
        value=${loc.location}
        label=${loc.label}
        onChange=${(v) => {
          setLoc(v);
          if (v.confirmed) apply(v, radius);
        }}
      />
    <//>
    <${Field} label="Радиус">
      ${(id) => html`<${Select}
        id=${id}
        value=${String(radius)}
        onChange=${(v) => {
          setRadius(Number(v));
          apply(loc, Number(v));
        }}
        options=${RADIUS_STEPS.map((r) => ({ value: String(r), label: radiusText(r) }))}
      />`}
    <//>
    ${place && html`<p class="pj-form__hint">Поиски сборки: ${where}</p>`}
  </div>`;
}

// ================================================================== alerts
export function AlertsList({ p, onDeal }) {
  const items = p.alerts_list || [];
  const app = useStore(appStore, (s) => s.app);
  const channel = app && app.features && (app.features.telegram || app.features.email);
  if (!items.length) return null;
  return html`<section class="pj-alerts" aria-label="Уведомления">
    <h2 class="pj-section-title">Уведомления<span class="num">${items.length}</span></h2>
    <ul class="pj-alerts__list">
      ${items.map(
        (a, i) => html`<li key=${i} class="pj-alert">
          <span class="pj-alert__icon"><${Icon} name=${a.kind === "budget_fit" ? "wallet" : "bell-ring"} size=${16} /></span>
          <div class="pj-alert__body">
            <div class="pj-alert__top">
              <b>${a.kind_label}</b>
              ${a.price != null && html`<span class="num">${money(a.price)}</span>`}
              <span class="muted">${a.sent_at_label}</span>
            </div>
            <p class="pj-alert__text">${alertLine(a.text)}</p>
            <div class="pj-alert__foot">
              <span class=${cx("pj-alert__delivered", !a.delivered && "muted")}><${Icon} name=${a.delivered ? "send" : "smartphone"} size=${12} />${a.delivered_label}</span>
              ${!a.delivered && !channel && html`<a href="/settings/notifications" class="linkish">Настроить Telegram</a>`}
              ${a.ad_id && html`<button type="button" class="linkish" onClick=${() => onDeal({ ad_id: a.ad_id, title: a.text })}>Открыть сделку</button>`}
            </div>
          </div>
        </li>`,
      )}
    </ul>
  </section>`;
}

/** A stored alert is the whole Telegram message (several lines, links): the first line is the news. */
function alertLine(text) {
  const first = String(text || "")
    .split(/\n/)
    .map((l) => l.trim())
    .find(Boolean);
  return ru((first || "").replace(/https?:\/\/\S+/g, "").replace(/\s{2,}/g, " "));
}

// ================================================================== side sheets
/** «Лучшие видеокарты по ценности»: offers across the tracked GPU variants ranked by value_score. */
export function GpuRankingSheet({ open, p, onClose, onDeal, onBought }) {
  const gpu = (p.slots || []).find((s) => s.kind === "gpu");
  const items = p.gpu_ranking || [];
  return html`<${Drawer} open=${open} onClose=${onClose} title="Лучшие видеокарты по ценности" subtitle="Предложения всех вариантов, от самой выгодной для нейросети" width=${560}>
    <div class="pj-sheet">
      <p class="pj-sheet__help">Ценность для нейросети: сколько видеопамяти и скорости получаешь за каждый евро (RTX 3090 по рынку = 50).</p>
      ${items.length
        ? items.map((o, i) => html`<${OfferRow} key=${o.ad_id} offer=${o} gpu label=${`№${i + 1}`} onDeal=${onDeal} onBought=${gpu && onBought ? (offer) => onBought(gpu, offer) : null} />`)
        : html`<p class="pj-sheet__empty"><${Icon} name="hourglass" size=${16} />Пока нет предложений видеокарт — жду первых объявлений.</p>`}
    </div>
  <//>`;
}

/** Offers of one variant (GET /projects/:id/offers?slot=…) + «Выбрать этот вариант». */
export function AltSheet({ open, p, slot, alt, onClose, onDeal, onPick }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open || !slot) return;
    setData(null);
    setError(null);
    projectsApi.offers(p.id, slot.key).then(setData, setError);
  }, [open, slot && slot.key, alt && alt.option]);
  if (!slot || !alt) return null;
  const row = data && (data.slots || []).find((s) => s.slot === slot.key);
  const offers = row ? ((row.alternatives || []).find((a) => a.option === alt.option) || {}).offers || [] : [];
  const option = (slot.options || []).find((o) => o.key === alt.option);
  return html`<${Drawer}
    open=${open}
    onClose=${onClose}
    title=${ru(alt.label)}
    subtitle=${`Вариант для части «${slot.label}»`}
    width=${560}
    footer=${onPick &&
    html`<${Button} variant="ghost" onClick=${onClose}>Закрыть<//><${Button}
        variant="primary"
        icon="check"
        loading=${busy}
        onClick=${async () => {
          setBusy(true);
          const ok = await onPick(slot.key, alt.option);
          setBusy(false);
          if (ok) onClose();
        }}
        >Выбрать этот вариант<//
      >`}
  >
    <div class="pj-sheet">
      ${option &&
      html`<div class="pj-sheet__facts">
        ${option.price && option.price.typical && html`<span class="tag">рынок ~${money(option.price.typical)} за штуку</span>`}
        ${option.target_unit != null && html`<span class="tag">цель ${money(option.target_unit)}</span>`}
        ${option.build_total && html`<span class=${cx("tag", option.fits_budget === false && "tone-danger")}>вся сборка ~${money(option.build_total)}</span>`}
        ${option.value_score != null && html`<span class="tag tone-profit">${Math.round(option.value_score)}/100 · ${option.value_label}</span>`}
      </div>`}
      ${error
        ? html`<${Banner} tone="danger" details=${error.details}>${error.message}<//>`
        : !data
          ? html`<${Skeleton} h=${96} radius="var(--r-lg)" /><${Skeleton} h=${96} radius="var(--r-lg)" />`
          : offers.length
            ? offers.map((o) => html`<${OfferRow} key=${o.ad_id} offer=${o} gpu=${slot.kind === "gpu"} onDeal=${onDeal} />`)
            : html`<p class="pj-sheet__empty"><${Icon} name="hourglass" size=${16} />Предложений этого варианта пока нет.</p>`}
      <p class="pj-sheet__help">«Выбрать этот вариант» поменяет часть в сборке, пересчитает цели и поиски. Блок питания, плату и память я подгоню сам, если понадобится.</p>
    </div>
  <//>`;
}

// ================================================================== delete
export function DeleteDialog({ open, p, onClose, onDeleted, onStart }) {
  const [keep, setKeep] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => open && setKeep(false), [open]);
  const n = p.searches || (p.searches_list || []).length || 0;
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    size="sm"
    class="confirm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Отмена<//><${Button}
        variant="danger"
        icon="trash-2"
        loading=${busy}
        onClick=${async () => {
          setBusy(true);
          onStart && onStart(true); // the `deleted` event may arrive before the answer
          try {
            const res = await projectsApi.remove(p.id, keep);
            onDeleted(res);
          } catch (e) {
            onStart && onStart(false);
            toast.error(e);
          } finally {
            setBusy(false);
          }
        }}
        >Удалить сборку<//
      >`}
  >
    <div class="confirm__content">
      <span class="confirm__icon is-danger"><${Icon} name="triangle-alert" size=${24} /></span>
      <h2 class="confirm__title">Удалить сборку «${p.name}»?</h2>
      <p class="confirm__message">
        ${n
          ? `${n === 1 ? "Её поиск тоже удалится" : `Её ${count(n, "поиск", "поиска", "поисков")} тоже удалятся`}, найденные объявления останутся в ленте.`
          : "Найденные объявления останутся в ленте."}
      </p>
      ${n > 0 && html`<div class="pj-keep"><${Checkbox} checked=${keep} onChange=${setKeep} label="Оставить поиски" description="Они продолжат искать как обычные поиски «для себя»" /></div>`}
    </div>
  <//>`;
}

/** «Удалить поиски сборки» after everything is bought (they are already paused). */
export async function deleteProjectSearches(p) {
  const list = (p.searches_list || []).filter((s) => s.exists && s.id);
  if (!list.length) return false;
  const ok = await confirm({
    title: `Удалить ${count(list.length, "поиск", "поиска", "поисков")} сборки?`,
    message: "Они уже на паузе. Найденные объявления останутся в ленте.",
    confirmLabel: "Удалить поиски",
    tone: "danger",
  });
  if (!ok) return false;
  let done = 0;
  for (const s of list) {
    try {
      await api.del(`/searches/${encodeURIComponent(s.id)}`);
      done++;
    } catch (e) {
      toast.error(e);
      break;
    }
  }
  if (done) toast.success(`Удалил ${count(done, "поиск", "поиска", "поисков")} сборки`);
  return done > 0;
}

