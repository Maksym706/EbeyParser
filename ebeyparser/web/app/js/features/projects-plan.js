// «Сборки» plan view (spec §5): summary hero, compatibility checks with the maths and «Исправить»,
// the GPU comparison table, one card per part with its variants, and the quiet «не нужно» line.
// Used by the unsaved preview (/projects/new) and by a saved project (draft and tracking).
//
// Handlers (all optional; without them the piece is read-only):
//   onPick(slotKey, optionKey)   choose a variant (preview: saves first; saved: PATCH choices)
//   onFix(fix)                   {slot, option, label_ru} from a check
//   onSlot(slotKey, patch)       PATCH {slots: {key: patch}} — status / target_price / auto_target / note
//   onRemove(slotKey)            custom item: PATCH {remove_slots}
//   onBudget(budget)             edit the budget from the hero
import { html, cx, useState, useEffect } from "../lib/html.js";
import { Icon, Button, NumberInput, Textarea, Modal, Spinner, Field, Input } from "../ui/index.js";
import { Menu } from "./popover.js";
import { ru, money, approx, lower, Hint, RoughBadge, CHECK_ICON, CHECK_TONE, CHECK_MARK } from "./projects-common.js";

const TARGET_HELP =
  "Цель — сколько ты готов заплатить за эту часть. Я считаю её так, чтобы вся сборка уложилась в бюджет, и минимум на 10 % ниже рынка.";
const MAX_HELP =
  "Объявления до 30 % дороже цели тоже проверяю — с продавцом можно поторговаться. Дороже — только запоминаю цену, чтобы знать рынок.";
const VALUE_HELP = "Ценность для нейросети: сколько видеопамяти и скорости получаешь за каждый евро (RTX 3090 по рынку = 50).";
const SPEED_HELP = "Грубая оценка: каждый токен читает все веса модели из памяти видеокарты; реальная скорость — примерно половина от потолка.";
const TOKENS_HELP = "Ток/с — сколько слов в секунду пишет модель (1 токен ≈ ¾ слова).";

/** Scroll to a part card and glow it once (after a fix / a variant change). */
export function flashSlot(key) {
  setTimeout(() => {
    const el = document.getElementById(`slot-${key}`);
    if (!el) return;
    const motion = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const r = el.getBoundingClientRect();
    if (r.top < 64 || r.bottom > window.innerHeight) el.scrollIntoView({ block: "center", behavior: motion ? "smooth" : "auto" });
    el.classList.remove("is-flash");
    void el.offsetWidth;
    el.classList.add("is-flash");
    setTimeout(() => el.classList.remove("is-flash"), 1600);
  }, 60);
}

// ================================================================== hero
export function PlanHero({ plan, onBudget }) {
  const t = plan.totals || {};
  const ai = plan.ai || {};
  const notes = (plan.notes || []).map(ru).filter(Boolean);
  const understood = notes.length && /^Понял так:/.test(notes[0]) ? notes[0].replace(/^Понял так:\s*/, "").split(/,\s+/) : null;
  const rest = understood ? notes.slice(1) : notes;
  const ratio = t.ratio;
  // unknown_price_slots may hold slot keys («item2») — show the part labels
  const labels = Object.fromEntries((plan.slots || []).map((s) => [s.key, s.label]));
  const unknown = (t.unknown_price_slots || []).map((k) => (labels[k] ? lower(labels[k]) : lower(k)));
  const stretchTone = ratio != null && ratio < 0.65 ? "t-red" : ratio != null && ratio < 0.9 ? "t-amber" : "";
  const newSub =
    t.savings_vs_new > 0
      ? html`<span class="t-green">б/у дешевле на ~${money(t.savings_vs_new)}</span>`
      : (t.new_missing || []).length
        ? `не считая: ${t.new_missing.map(lower).join(", ")}`
        : "в магазине";
  return html`<section class="pj-hero" aria-label="Итог плана">
    <p class="pj-hero__summary">${ru(plan.summary_ru)}</p>
    <div class="pj-stats">
      <div class="pj-stat">
        <span class="overline">По рынку</span>
        <b class="num">${t.estimated_total || t.typical_total ? approx(t.estimated_total || t.typical_total) : "—"}</b>
        ${unknown.length > 0 && html`<span class="pj-stat__sub">пока без цены: ${unknown.join(", ")}</span>`}
      </div>
      <div class="pj-stat">
        <span class="overline">Цель <${Hint} tip="Если брать каждую часть примерно на 10 % дешевле рынка." title="Цель" class="pj-q"><${Icon} name="circle-help" size=${13} /><//></span>
        <b class="num">${money(t.target_total)}</b>
      </div>
      <div class="pj-stat">
        <span class="overline">Бюджет</span>
        ${onBudget ? html`<${BudgetEdit} value=${plan.budget} onSave=${onBudget} />` : html`<b class="num">${plan.budget ? money(plan.budget) : "не задан"}</b>`}
      </div>
      <div class="pj-stat">
        <span class="overline">Новым</span>
        <b class="num">${t.new_total ? approx(t.new_total) : "—"}</b>
        <span class="pj-stat__sub">${t.new_total ? newSub : "цены новых не знаю"}</span>
      </div>
    </div>
    ${t.stretch_label_ru && html`<p class=${cx("pj-hero__stretch", stretchTone)}>${ru(t.stretch_label_ru)}</p>`}
    ${(understood || rest.length > 0 || plan.prices_rough) &&
    html`<div class="pj-hero__notes">
      ${understood &&
      html`<div class="pj-understood">
        <span class="pj-understood__label">Понял так:</span>
        ${understood.map((c) => html`<span class="tag">${c}</span>`)}
      </div>`}
      ${rest.map((n) => html`<p class="pj-note">${n}</p>`)}
      ${plan.prices_rough && html`<span class="pj-hero__rough"><${RoughBadge} note=${ru(plan.prices_note_ru)} /></span>`}
    </div>`}
    <${AiNote} ai=${ai} />
  </section>`;
}

function AiNote({ ai }) {
  if (!ai) return null;
  if (ai.used && ai.ok)
    return html`<div class="pj-ai">
      <span class="pj-ai__icon"><${Icon} name="bot" size=${18} /></span>
      <div>
        ${ai.summary_ru && html`<p>${ru(ai.summary_ru)}</p>`}
        ${ai.message_ru && html`<p class="pj-ai__caption">${ru(ai.message_ru)}</p>`}
      </div>
    </div>`;
  if (ai.ok === false && ai.message_ru) return html`<p class="pj-ai-caption t-amber"><${Icon} name="bot" size=${14} />${ru(ai.message_ru)}</p>`;
  if (ai.message_ru && /выключ|недоступ/i.test(ai.message_ru))
    return html`<p class="pj-ai-caption"><${Icon} name="bot" size=${14} />${ru(String(ai.message_ru).split(/\.\s/)[0])}. <a href="/settings/ai">Включить нейросеть</a></p>`;
  return null;
}

/** «1 500 €» with a pencil → inline number field (PATCH budget). */
function BudgetEdit({ value, onSave }) {
  const [edit, setEdit] = useState(false);
  const [v, setV] = useState(value);
  const [busy, setBusy] = useState(false);
  useEffect(() => setV(value), [value]);
  if (!edit)
    return html`<button type="button" class="pj-inline-edit num" onClick=${() => setEdit(true)} aria-label="Изменить бюджет">
      ${value ? money(value) : "задать"}<${Icon} name="pencil" size=${13} />
    </button>`;
  const save = async () => {
    if (v != null && (v < 1 || v > 100000)) return;
    setBusy(true);
    const ok = await onSave(v);
    setBusy(false);
    if (ok !== false) setEdit(false);
  };
  return html`<form
    class="pj-budget-form"
    onSubmit=${(e) => {
      e.preventDefault();
      save();
    }}
  >
    <${NumberInput} value=${v} onChange=${setV} min=${1} max=${100000} suffix="€" size="sm" aria-label="Бюджет" autoFocus />
    <${Button} type="submit" size="sm" variant="primary" loading=${busy} icon="check" aria-label="Сохранить бюджет" />
    <${Button} size="sm" variant="ghost" icon="x" aria-label="Отмена" onClick=${() => setEdit(false)} />
  </form>`;
}

// ================================================================== checks
export function ChecksPanel({ checks = [], summary, onFix, busy }) {
  const [open, setOpen] = useState(false);
  const status = (summary && summary.status) || "ok";
  const tone = CHECK_TONE[status] || "neutral";
  const problems = checks.filter((c) => c.status === "fail" || (c.status === "warn" && c.fix));
  const jump = (key) => {
    setOpen(true);
    setTimeout(() => {
      const el = document.getElementById(`check-${key}`);
      if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 50);
  };
  return html`<section class=${cx("pj-checks", `tone-${tone}`)} aria-label="Проверки">
    <header class="pj-checks__head">
      <span class="pj-checks__badge"><${Icon} name=${status === "ok" ? "circle-check" : status === "fail" ? "circle-alert" : "triangle-alert"} size=${18} /></span>
      <h2 class="pj-checks__title">Проверки: ${ru(summary && summary.text_ru ? lowerFirst(summary.text_ru) : "всё совместимо")}</h2>
      <button type="button" class="pj-checks__toggle" aria-expanded=${open} onClick=${() => setOpen(!open)}>
        ${open ? "Скрыть расчёты" : "Показать расчёты"}<${Icon} name=${open ? "chevron-up" : "chevron-down"} size=${16} />
      </button>
    </header>
    ${!open &&
    html`<div class="pj-checks__chips">
      ${checks.map(
        (c) => html`<button type="button" key=${c.key} class=${cx("pj-cchip", `tone-${CHECK_TONE[c.status] || "neutral"}`)} onClick=${() => jump(c.key)} title=${ru(c.summary)}>
          <${Icon} name=${CHECK_MARK[c.status] || "info"} size=${13} stroke=${2.5} />
          <span class="pj-cchip__title">${c.title}</span>
          ${c.summary && html`<span class="pj-cchip__sum">${shortSummary(ru(c.summary))}</span>`}
        </button>`,
      )}
    </div>`}
    ${!open && problems.length > 0 && html`<div class="pj-checks__problems">${problems.map((c) => html`<${CheckRow} key=${c.key} c=${c} onFix=${onFix} busy=${busy} compact />`)}</div>`}
    ${open && html`<div class="pj-checks__list">${checks.map((c) => html`<${CheckRow} key=${c.key} c=${c} onFix=${onFix} busy=${busy} />`)}</div>`}
  </section>`;
}

function lowerFirst(s) {
  return s ? s.charAt(0).toLowerCase() + s.slice(1) : s;
}

/** «64 ГБ ≥ нужно ≈44 ГБ — запас 20 ГБ» → «64 ГБ ≥ нужно ≈44 ГБ» (the chip is short). */
function shortSummary(s) {
  const cut = s.split(" — ")[0];
  return cut.length > 42 ? cut.slice(0, 40) + "…" : cut;
}

function CheckRow({ c, onFix, busy, compact = false }) {
  const tone = CHECK_TONE[c.status] || "neutral";
  return html`<div class=${cx("pj-check", `tone-${tone}`, compact && "pj-check--compact")} id=${compact ? undefined : `check-${c.key}`}>
    <span class="pj-check__icon" aria-hidden="true"><${Icon} name=${CHECK_ICON[c.key] || "circle-check"} size=${18} /></span>
    <div class="pj-check__body">
      <div class="pj-check__top">
        <b>${c.title}</b>
        <span class="pj-check__status"><${Icon} name=${CHECK_MARK[c.status] || "info"} size=${12} stroke=${2.5} />${c.status_label}</span>
      </div>
      ${c.summary && html`<p class="pj-check__summary">${ru(c.summary)}</p>`}
      ${!compact && (c.lines || []).length > 0 && html`<div class="pj-math">${c.lines.map((l) => html`<div>${ru(l)}</div>`)}</div>`}
      ${c.fix &&
      onFix &&
      html`<div class="pj-check__fix">
        <${Button} size="sm" variant="tinted" tone=${c.status === "fail" ? "red" : "amber"} icon="wrench" loading=${busy === `${c.fix.slot}:${c.fix.option}`} disabled=${Boolean(busy)} onClick=${() => onFix(c.fix)}>
          Исправить: ${ru(lowerFirst(c.fix.label_ru))}
        <//>
      </div>`}
    </div>
  </div>`;
}

// ================================================================== GPU comparison
/** The GPU slot of an LLM / gaming build as a comparison table (spec §5.3) — «what to buy for my money». */
export function GpuTable({ slot, onPick, busy, rough }) {
  const opts = slot.options || [];
  const llm = opts.some((o) => o.vram_status);
  const pick = (o) => onPick && !o.chosen && !busy && onPick(slot.key, o.key);
  return html`<div class=${cx("pj-gpu", !llm && "pj-gpu--gaming")} role="radiogroup" aria-label=${slot.label}>
    <div class="pj-gpu__row pj-gpu__row--head" aria-hidden="true">
      <span class="pj-gpu__c-name">Вариант</span>
      ${llm && html`<span class="pj-gpu__c-mem">Память</span><span class="pj-gpu__c-speed">Скорость</span>`}
      <span class="pj-gpu__c-cards">Видеокарты</span>
      <span class="pj-gpu__c-build">Вся сборка</span>
      <span class="pj-gpu__c-value">Ценность <${Hint} tip=${VALUE_HELP} title="Ценность" class="pj-q" align="end"><${Icon} name="circle-help" size=${13} /><//></span>
      <span class="pj-gpu__c-note">Главное</span>
    </div>
    ${opts.map((o) => {
      const loading = busy === `${slot.key}:${o.key}`;
      const statusTone = { ok: "t-green", warn: "t-amber", fail: "t-red" }[o.vram_status] || "";
      return html`<div
        key=${o.key}
        class=${cx("pj-gpu__row", o.chosen && "is-chosen", onPick && !o.chosen && "is-pickable")}
        role="radio"
        aria-checked=${o.chosen}
        aria-disabled=${!onPick || undefined}
        tabindex=${onPick ? 0 : -1}
        onClick=${(e) => !e.target.closest("button, a") && pick(o)}
        onKeyDown=${(e) => (e.key === "Enter" || e.key === " ") && e.target === e.currentTarget && (e.preventDefault(), pick(o))}
      >
        <span class="pj-gpu__c-name">
          <span class=${cx("pj-radio", o.chosen && "is-on")}>${loading ? html`<${Spinner} size=${14} />` : null}</span>
          <span class="pj-gpu__name">
            <b>${o.label}</b>
            ${o.source !== "kb" && o.source_label && html`<${SourceChip} o=${o} />`}
          </span>
        </span>
        ${llm &&
        html`<span class="pj-gpu__c-mem" data-label="Память">
            <b class="num">${o.vram_gb} ГБ</b>
            <span class=${cx("pj-gpu__status", statusTone)}><${Icon} name=${CHECK_MARK[o.vram_status] || "info"} size=${12} stroke=${2.5} />${o.vram_status_label}</span>
          </span>
          <span class="pj-gpu__c-speed" data-label="Скорость">
            ${o.speed ? html`<${Hint} tip=${[ru(o.speed.text_ru), SPEED_HELP, TOKENS_HELP]} title="Скорость ответа" class="num pj-dotted">${ru(o.speed.label_ru)}<//>` : "—"}
          </span>`}
        <span class="pj-gpu__c-cards" data-label="Видеокарты">
          <b class="num">${o.price && o.price.total ? approx(o.price.total) : "цены нет"}${o.price && o.price.rough && rough !== false ? html`<${Icon} name="circle-dashed" size=${12} class="pj-rough-mark" aria-label="ориентир" />` : ""}</b>
        </span>
        <span class="pj-gpu__c-build" data-label="Вся сборка">
          <b class=${cx("num", o.fits_budget === false && !o.reachable && "t-red")}>${o.build_total ? approx(o.build_total) : "—"}</b>
          ${o.fits_budget === false && (o.reachable ? html`<span class="tag tone-haggle">реально с торгом</span>` : html`<span class="tag tone-danger">над бюджетом</span>`)}
        </span>
        <span class="pj-gpu__c-value" data-label="Ценность">
          ${o.value_score != null
            ? html`<span class="pj-value">
                <span class="pj-value__bar"><span style=${{ width: `${Math.max(4, Math.min(100, o.value_score))}%` }} class=${valueTone(o.value_score)}></span></span>
                <b class="num">${Math.round(o.value_score)}</b>
                <span class="pj-value__label">${o.value_label}</span>
              </span>`
            : "—"}
        </span>
        <span class="pj-gpu__c-note">${(o.caveats || [])[0] ? html`<span class="pj-gpu__caveat" title=${ru(o.caveats[0])}><${Icon} name="triangle-alert" size=${13} />${ru(o.caveats[0])}</span>` : (o.pros || [])[0] ? html`<span class="pj-gpu__pro" title=${ru(o.pros[0])}><${Icon} name="check" size=${13} />${ru(o.pros[0])}</span>` : ""}</span>
      </div>`;
    })}
  </div>`;
}

function valueTone(score) {
  return score >= 70 ? "is-green" : score >= 45 ? "is-amber" : "is-neutral";
}

function SourceChip({ o }) {
  const ai = o.source === "ai";
  return html`<span class=${cx("tag", ai && "tone-info")}><${Icon} name=${ai ? "bot" : "user"} size=${12} />${ai ? "Предложила нейросеть" : o.source_label}</span>`;
}

// ================================================================== parts
/**
 * One part (slot) of the plan. mode: "preview" (unsaved: only variant picks) | "draft" | "tracking"
 * (in tracking the card is the body of «Варианты и цель»).
 */
export function SlotCard({ slot, plan, mode = "draft", onPick, onSlot, onRemove, busy, bare = false }) {
  const opts = slot.options || [];
  const chosen = opts.find((o) => o.chosen) || opts[0];
  const gpuTable = slot.kind === "gpu" && opts.length > 1;
  const generic = slot.kind === "generic" || (chosen && chosen.kind === "generic");
  const off = slot.status === "have" || slot.status === "skipped";
  const saved = mode !== "preview";
  const body = off
    ? null
    : html`
        ${gpuTable
          ? html`<${GpuTable} slot=${slot} onPick=${onPick} busy=${busy} rough=${plan && plan.prices_rough} />`
          : opts.length > 1 && html`<${OptionPicker} slot=${slot} onPick=${onPick} busy=${busy} />`}
        ${chosen && html`<${OptionDetail} o=${chosen} slot=${slot} generic=${generic} onSlot=${saved ? onSlot : null} />`}
        ${slot.note && html`<p class="pj-slot__note"><${Icon} name="sticky-note" size=${14} />${slot.note}</p>`}
      `;
  if (bare) return html`<div class="pj-slot__bare">${body}</div>`;
  return html`<section class=${cx("pj-slot", off && "is-off", gpuTable && "pj-slot--gpu")} id=${`slot-${slot.key}`} aria-label=${slot.label}>
    <header class="pj-slot__head">
      <div class="pj-slot__titles">
        <h3 class="pj-slot__title">
          ${slot.label}${slot.qty > 1 && html`<span class="pj-slot__qty num">× ${slot.qty}</span>`}
        </h3>
        ${slot.hint && !off && html`<p class="pj-slot__hint">${ru(slot.hint)}</p>`}
      </div>
      <span class=${cx("pj-slot__status", `st-${slot.status}`)}>${slot.status_label}</span>
      ${saved && (onSlot || onRemove) && html`<${SlotMenu} slot=${slot} onSlot=${onSlot} onRemove=${generic ? onRemove : null} />`}
    </header>
    ${off
      ? html`<p class="pj-slot__offline">
          Не ищу эту часть и не считаю её в сумме.
          ${onSlot && html`<button type="button" class="linkish" onClick=${() => onSlot(slot.key, { status: "open" }, { undoOf: slot.status })}>Снова нужно</button>`}
        </p>`
      : body}
  </section>`;
}

/** Radio cards (desktop) / chip scroller (phone) of the variants of an ordinary part. */
function OptionPicker({ slot, onPick, busy }) {
  const opts = slot.options || [];
  return html`<div class="pj-opts" role="radiogroup" aria-label=${`${slot.label}: варианты`}>
    ${opts.map((o) => {
      const loading = busy === `${slot.key}:${o.key}`;
      return html`<button
        type="button"
        key=${o.key}
        role="radio"
        aria-checked=${o.chosen}
        class=${cx("pj-opt", o.chosen && "is-chosen")}
        disabled=${!onPick || (Boolean(busy) && !loading)}
        onClick=${() => onPick && !o.chosen && onPick(slot.key, o.key)}
      >
        <span class=${cx("pj-radio", o.chosen && "is-on")}>${loading ? html`<${Spinner} size=${14} />` : null}</span>
        <span class="pj-opt__text">
          <span class="pj-opt__label">${ru(o.label)}</span>
          <span class="pj-opt__price num">
            ${o.price && o.price.typical ? html`${approx(o.price.total || o.price.typical)}${o.price.rough ? html`<${Icon} name="circle-dashed" size=${11} class="pj-rough-mark" />` : ""}` : "цены пока нет"}
          </span>
        </span>
      </button>`;
    })}
  </div>`;
}

/** Everything about the chosen variant: specs, market price, target, why, pros / caveats. */
export function OptionDetail({ o, slot, generic, onSlot }) {
  const [more, setMore] = useState(false);
  const pros = o.pros || [];
  const cons = o.caveats || [];
  const p = o.price || {};
  const shownCons = more ? cons : cons.slice(0, 2);
  const shownPros = more ? pros : pros.slice(0, 2);
  const hidden = cons.length + pros.length - shownCons.length - shownPros.length;
  const target = o.target_unit ?? slot.target_unit;
  const top = o.max_unit ?? slot.max_unit;
  return html`<div class="pj-detail">
    ${!(generic && (slot.options || []).length === 1 && o.label === slot.label && o.source === "kb") &&
    html`<div class="pj-detail__head">
      ${!(generic && o.label === slot.label) && html`<b class="pj-detail__label">${ru(o.label)}</b>`}
      ${o.source !== "kb" && o.source_label && html`<${SourceChip} o=${o} />`}
    </div>`}
    ${generic && o.query && html`<p class="pj-detail__query">ищу по словам: «${o.query}»</p>`}
    ${(o.specs_ru || []).length > 0 && html`<div class="chips-row pj-detail__specs">${o.specs_ru.map((s) => html`<span class="tag">${ru(s)}</span>`)}</div>`}
    <div class="pj-prices">
      <div class="pj-prices__market">
        ${p.typical
          ? html`<span>рынок <b class="num">~${money(p.typical)}</b>${o.qty > 1 ? html` <span class="muted">за штуку · ×${o.qty} = ~${money(p.total)}</span>` : ""}</span>
              ${p.range_label
                ? html`<${Hint} tip=${`Обычно ${ru(p.range_label)}`} title="Разброс цен" class="pj-dotted pj-prices__src">${p.rough ? html`<${Icon} name="circle-dashed" size=${12} />` : ""}${ru(p.label_ru)}<//>`
                : html`<span class="pj-prices__src">${ru(p.label_ru)}</span>`}`
          : html`<span class="muted">цены пока нет — узнаю из объявлений</span>`}
      </div>
      ${target != null &&
      html`<${TargetLine} slot=${slot} o=${o} target=${target} top=${top} onSlot=${onSlot} />`}
    </div>
    ${o.why && html`<p class="pj-detail__why">${ru(o.why)}</p>`}
    ${(shownPros.length > 0 || shownCons.length > 0) &&
    html`<ul class="pj-procon">
      ${shownPros.map((t) => html`<li class="is-pro"><${Icon} name="check" size=${14} stroke=${2.5} />${ru(t)}</li>`)}
      ${shownCons.map((t) => html`<li class="is-con"><${Icon} name="triangle-alert" size=${14} />${ru(t)}</li>`)}
    </ul>`}
    ${hidden > 0 && html`<button type="button" class="linkish" onClick=${() => setMore(true)}>ещё ${hidden}</button>`}
    <div class="pj-detail__meta">
      ${o.value && o.value.text_ru && html`<span class="tag">${ru(o.value.text_ru)}</span>`}
      ${o.new_label &&
      html`<${Hint} tip=${o.new_price ? `Новым: ~${money(o.new_price)} — ${ru(o.new_label)}` : ru(o.new_label)} title="Новым в магазине" class="pj-dotted pj-detail__new"
        >${o.new_price ? `новым ~${money(o.new_price)}` : ru(o.new_label)}<//
      >`}
      ${o.where_ru && html`<span class="pj-detail__where"><${Icon} name="map-pin" size=${13} />${ru(o.where_ru)}</span>`}
    </div>
  </div>`;
}

/** «цель 190 € · проверяю до 250 €» + pencil (own target) / «Вернуть авто». */
function TargetLine({ slot, o, target, top, onSlot }) {
  const [edit, setEdit] = useState(false);
  const [v, setV] = useState(target);
  const [busy, setBusy] = useState(false);
  useEffect(() => setV(target), [target]);
  const own = o.target_by === "user";
  const unit = slot.qty > 1 ? " за штуку" : "";
  if (edit && onSlot) {
    const save = async (patch) => {
      setBusy(true);
      const ok = await onSlot(slot.key, patch);
      setBusy(false);
      if (ok !== false) setEdit(false);
    };
    return html`<form
      class="pj-target-form"
      onSubmit=${(e) => {
        e.preventDefault();
        if (v != null && v > 0 && v <= 100000) save({ target_price: v });
      }}
    >
      <label class="pj-target-form__label" for=${`tgt-${slot.key}`}>Цель${unit}</label>
      <${NumberInput} id=${`tgt-${slot.key}`} value=${v} onChange=${setV} min=${1} max=${100000} suffix="€" size="sm" autoFocus />
      <${Button} type="submit" size="sm" variant="primary" loading=${busy}>Сохранить<//>
      ${own && html`<${Button} size="sm" variant="ghost" icon="rotate-ccw" disabled=${busy} onClick=${() => save({ auto_target: true })}>Вернуть авто<//>`}
      <${Button} size="sm" variant="ghost" onClick=${() => setEdit(false)}>Отмена<//>
    </form>`;
  }
  return html`<div class="pj-target">
    <${Hint} tip=${TARGET_HELP} title="Цель" class="pj-dotted"><span>цель <b class="num">${money(target)}</b>${unit}</span><//>
    ${top != null && html`<${Hint} tip=${MAX_HELP} title="Проверяю до" class="pj-dotted pj-target__max">проверяю до ${money(top)}<//>`}
    ${own && html`<span class="tag tone-info"><${Icon} name="pencil" size=${11} />твоя цель</span>`}
    ${onSlot && html`<button type="button" class="pj-inline-edit pj-target__edit" onClick=${() => setEdit(true)} aria-label="Изменить цель"><${Icon} name="pencil" size=${13} />${own ? "изменить" : "своя цель"}</button>`}
  </div>`;
}

/** Ellipsis menu of a part: «Уже есть», «Не нужно», «Снова нужно», «Заметка», custom: «Убрать». */
function SlotMenu({ slot, onSlot, onRemove }) {
  const [note, setNote] = useState(false);
  const items = [];
  if (onSlot) {
    if (slot.status === "open") {
      items.push({ label: "Уже есть", icon: "package-check", onClick: () => onSlot(slot.key, { status: "have" }, { undoOf: "open" }) });
      items.push({ label: "Не нужно", icon: "circle-slash", onClick: () => onSlot(slot.key, { status: "skipped" }, { undoOf: "open" }) });
    } else if (slot.status === "have" || slot.status === "skipped") {
      items.push({ label: "Снова нужно", icon: "rotate-ccw", onClick: () => onSlot(slot.key, { status: "open" }, { undoOf: slot.status }) });
    }
    items.push({ label: slot.note ? "Изменить заметку" : "Заметка", icon: "notebook-pen", onClick: () => setNote(true) });
  }
  if (onRemove) items.push({ divider: true }, { label: "Убрать из сборки", icon: "trash-2", danger: true, onClick: () => onRemove(slot.key) });
  return html`<span class="pj-slot__menu">
    <${Menu} label=${`${slot.label}: действия`} items=${items} />
    <${NoteDialog} open=${note} slot=${slot} onClose=${() => setNote(false)} onSave=${(text) => onSlot(slot.key, { note: text })} />
  </span>`;
}

function NoteDialog({ open, slot, onClose, onSave }) {
  const [text, setText] = useState(slot.note || "");
  const [busy, setBusy] = useState(false);
  useEffect(() => open && setText(slot.note || ""), [open]);
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    title=${`Заметка: ${lower(slot.label)}`}
    size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Отмена<//><${Button}
        variant="primary"
        loading=${busy}
        onClick=${async () => {
          setBusy(true);
          const ok = await onSave(text.trim());
          setBusy(false);
          if (ok !== false) onClose();
        }}
        >Сохранить<//
      >`}
  >
    <${Field} label="Заметка" help="Например: «взять с гарантией» или «спросить про коробку»">
      ${(id) => html`<${Textarea} id=${id} value=${text} onChange=${setText} rows=${3} maxLength=${300} />`}
    <//>
  <//>`;
}

/** «Не нужно для этого варианта: Райзеры PCIe (карты стоят прямо в корпусе) · …» */
export function InactiveSlots({ slots = [] }) {
  const off = slots.filter((s) => !s.active);
  if (!off.length) return null;
  return html`<p class="pj-inactive">
    <${Icon} name="circle-slash" size=${14} />
    <span>Не нужно для этого варианта: ${off.map((s, i) => html`${i ? " · " : ""}<b>${s.label}</b>${s.inactive_reason ? ` (${ru(s.inactive_reason)})` : ""}`)}</span>
  </p>`;
}

/** «+ Добавить вещь» (custom builds): label, search words, target, quantity. */
export function AddItemDialog({ open, onClose, onAdd }) {
  const [row, setRow] = useState({ label: "", query: "", target_price: null, qty: 1 });
  const [err, setErr] = useState({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) {
      setRow({ label: "", query: "", target_price: null, qty: 1 });
      setErr({});
    }
  }, [open]);
  const submit = async () => {
    if (!row.label.trim() && !row.query.trim()) return setErr({ label: "Напиши, что ищешь" });
    setBusy(true);
    const res = await onAdd({ label: row.label.trim() || row.query.trim(), query: row.query.trim() || row.label.trim(), target_price: row.target_price || null, qty: row.qty || 1 });
    setBusy(false);
    if (res && res.fields) setErr(res.fields);
    else if (res !== false) onClose();
  };
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    title="Добавить вещь"
    size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Отмена<//><${Button} variant="primary" icon="plus" loading=${busy} onClick=${submit}>Добавить<//>`}
  >
    <div class="pj-form">
      <${Field} label="Что ищешь" error=${err.label} help="Пиши так, как продавцы называют вещь: „ThinkPad T480“, „Monitor 27 Zoll“">
        ${(id) => html`<${Input} id=${id} value=${row.label} onChange=${(v) => setRow({ ...row, label: v })} maxLength=${80} autoFocus />`}
      <//>
      <${Field} label="Слова для поиска" optional help="Если пусто — ищу по названию">
        ${(id) => html`<${Input} id=${id} value=${row.query} onChange=${(v) => setRow({ ...row, query: v })} maxLength=${80} />`}
      <//>
      <div class="pj-form__row">
        <${Field} label="До" optional error=${err.target_price}>
          ${(id) => html`<${NumberInput} id=${id} value=${row.target_price} onChange=${(v) => setRow({ ...row, target_price: v })} min=${1} max=${100000} suffix="€" />`}
        <//>
        <${Field} label="Штук">
          ${(id) => html`<${NumberInput} id=${id} value=${row.qty} onChange=${(v) => setRow({ ...row, qty: v })} min=${1} max=${20} suffix="шт." />`}
        <//>
      </div>
    </div>
  <//>`;
}

/**
 * The whole plan body: hero, checks, parts (GPU first), the quiet inactive line.
 * `slotView(slot)` lets the tracking page render its own part cards instead.
 */
export function PlanBody({ plan, mode, handlers = {}, busy, slotView, afterChecks }) {
  const slots = (plan.slots || []).filter((s) => s.active);
  const gpuFirst = [...slots].sort((a, b) => (a.kind === "gpu" ? -1 : 0) - (b.kind === "gpu" ? -1 : 0));
  const custom = plan.template === "custom" || plan.kind === "custom";
  return html`<div class="pj-plan">
    <${PlanHero} plan=${plan} onBudget=${handlers.onBudget} />
    <${ChecksPanel} checks=${plan.checks || []} summary=${plan.checks_summary} onFix=${handlers.onFix} busy=${busy} />
    ${afterChecks}
    <div class="pj-slots">
      ${gpuFirst.map((s) =>
        slotView
          ? slotView(s)
          : html`<${SlotCard} key=${s.key} slot=${s} plan=${plan} mode=${mode} onPick=${handlers.onPick} onSlot=${handlers.onSlot} onRemove=${handlers.onRemove} busy=${busy} />`,
      )}
      ${!slots.length &&
      html`<div class="pj-slot pj-slot--empty">
        <${Icon} name="list-checks" size=${22} />
        <p><b>Добавь, что нужно купить</b><br />Каждая вещь — отдельный поиск со своей ценой.</p>
      </div>`}
    </div>
    ${custom && handlers.onAddItem && html`<div class="pj-add"><${Button} variant="secondary" icon="plus" onClick=${handlers.onAddItem}>Добавить вещь<//></div>`}
    <${InactiveSlots} slots=${plan.slots} />
  </div>`;
}

export { TARGET_HELP, MAX_HELP, VALUE_HELP };
