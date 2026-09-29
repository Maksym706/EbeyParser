// Feedback & layout helpers: Skeleton, EmptyState, ErrorState, Progress, Checklist, Banner,
// Steps, Tabs, PageHeader, Section, KeyValue, Stat.
import { html, cx, useRef } from "../lib/html.js";
import { Icon } from "./icons.js";
import { Button, Glyph } from "./core.js";
import { hue } from "../lib/tones.js";

/** Shimmering placeholder. <Skeleton w="60%" h={14} /> or <Skeleton variant="card" /> */
export function Skeleton({ w = "100%", h = 14, radius, variant, count = 1, class: cls = "" }) {
  if (variant === "card") {
    return html`<div class=${cx("skeleton-card", cls)} aria-hidden="true">
      <div class="skeleton" style=${{ height: 160, borderRadius: "var(--r-md)" }}></div>
      <div class="skeleton" style=${{ width: "70%", height: 16 }}></div>
      <div class="skeleton" style=${{ width: "40%", height: 14 }}></div>
    </div>`;
  }
  if (variant === "row") {
    return html`<div class=${cx("skeleton-row", cls)} aria-hidden="true">
      <div class="skeleton" style=${{ width: 40, height: 40, borderRadius: "var(--r-md)" }}></div>
      <div class="skeleton-row__lines">
        <div class="skeleton" style=${{ width: "55%", height: 14 }}></div>
        <div class="skeleton" style=${{ width: "35%", height: 12 }}></div>
      </div>
    </div>`;
  }
  return html`${Array.from({ length: count }, (_, i) => html`<div
    key=${i}
    class=${cx("skeleton", cls)}
    style=${{ width: typeof w === "number" ? `${w}px` : w, height: typeof h === "number" ? `${h}px` : h, borderRadius: radius }}
    aria-hidden="true"
  ></div>`)}`;
}

/** Friendly empty screen with an icon, text and optional action(s). */
export function EmptyState({ icon = "inbox", tone = "neutral", title, message, action, secondary, compact = false, children }) {
  return html`<div class=${cx("empty", compact && "empty--compact")}>
    <div class=${cx("empty__art", `empty__art--${hue(tone)}`)}>
      <span class="empty__ring"></span>
      <${Icon} name=${icon} size=${compact ? 24 : 30} />
    </div>
    ${title && html`<h3 class="empty__title">${title}</h3>`}
    ${message && html`<p class="empty__message">${message}</p>`}
    ${children}
    ${(action || secondary) && html`<div class="empty__actions">${action}${secondary}</div>`}
  </div>`;
}

/**
 * Error block for failed loads. Understands ApiError: a missing endpoint shows a calm
 * "скоро будет" placeholder instead of a red error.
 */
export function ErrorState({ error, onRetry, compact = false, title }) {
  if (error && error.missing) {
    return html`<${EmptyState}
      compact=${compact}
      icon="hammer"
      tone="info"
      title=${title || "Этот раздел ещё в работе"}
      message="Функция появится в следующем обновлении программы."
    />`;
  }
  const offline = error && (error.status === 0 || error.code === "network");
  return html`<${EmptyState}
    compact=${compact}
    icon=${offline ? "wifi-off" : "circle-alert"}
    tone="danger"
    title=${title || (offline ? "Нет связи с программой" : "Не получилось загрузить")}
    message=${(error && error.message) || "Попробуй ещё раз через минуту."}
    action=${onRetry && html`<${Button} icon="refresh-cw" onClick=${() => onRetry()}>Повторить<//>`}
  />`;
}

/** Linear progress. value 0..1, or indeterminate when value is null. */
export function Progress({ value = null, tone = "brand", size = "md", label }) {
  const indeterminate = value === null || value === undefined;
  return html`<div
    class=${cx("progress", `progress--${hue(tone)}`, `progress--${size}`, indeterminate && "is-indeterminate")}
    role="progressbar"
    aria-label=${label}
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow=${indeterminate ? undefined : Math.round(value * 100)}
  >
    <span class="progress__bar" style=${indeterminate ? null : { width: `${Math.max(0, Math.min(1, value)) * 100}%` }}></span>
  </div>`;
}

/** Circular progress ring (0..1). */
export function Ring({ value = 0, size = 44, stroke = 4, tone = "brand", children }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return html`<span class=${cx("ring", `ring--${hue(tone)}`)} style=${{ width: size, height: size }}>
    <svg width=${size} height=${size} viewBox=${`0 0 ${size} ${size}`} aria-hidden="true">
      <circle class="ring__track" cx=${size / 2} cy=${size / 2} r=${r} stroke-width=${stroke} fill="none" />
      <circle
        class="ring__value"
        cx=${size / 2}
        cy=${size / 2}
        r=${r}
        stroke-width=${stroke}
        fill="none"
        stroke-dasharray=${c}
        stroke-dashoffset=${c * (1 - Math.max(0, Math.min(1, value)))}
        stroke-linecap="round"
      />
    </svg>
    <span class="ring__label">${children}</span>
  </span>`;
}

/**
 * Checklist of steps. items: [{ label, description, state: "done"|"active"|"todo"|"error"|"skipped", action }]
 */
export function Checklist({ items = [] }) {
  return html`<ol class="checklist">
    ${items.map(
      (it, i) => html`<li class=${cx("checklist__item", `is-${it.state || "todo"}`)}>
        <span class="checklist__mark">
          ${it.state === "done"
            ? html`<${Icon} name="check" size=${14} stroke=${3} />`
            : it.state === "error"
              ? html`<${Icon} name="x" size=${14} stroke=${3} />`
              : it.state === "active"
                ? html`<span class="checklist__pulse"></span>`
                : i + 1}
        </span>
        <div class="checklist__text">
          <div class="checklist__label">${it.label}</div>
          ${it.description && html`<div class="checklist__desc">${it.description}</div>`}
          ${it.action && html`<div class="checklist__action">${it.action}</div>`}
        </div>
      </li>`,
    )}
  </ol>`;
}

/** Inline callout. tone: info | profit | haggle | danger | neutral | bid */
export function Banner({ tone = "info", icon, title, children, action, onClose, class: cls = "" }) {
  const h = hue(tone);
  const defaultIcon = { blue: "info", green: "circle-check", amber: "triangle-alert", red: "circle-alert", neutral: "lightbulb", violet: "gavel" }[h];
  return html`<div class=${cx("banner", `banner--${h}`, cls)} role=${h === "red" ? "alert" : "note"}>
    <span class="banner__icon"><${Icon} name=${icon || defaultIcon} size=${18} /></span>
    <div class="banner__body">
      ${title && html`<div class="banner__title">${title}</div>`}
      ${children && html`<div class="banner__text">${children}</div>`}
    </div>
    ${action && html`<div class="banner__action">${action}</div>`}
    ${onClose && html`<button type="button" class="banner__close" aria-label="Скрыть" onClick=${onClose}><${Icon} name="x" size=${16} /></button>`}
  </div>`;
}

/** Wizard progress: numbered steps with labels. current = index. */
export function Steps({ steps = [], current = 0, onSelect, compact = false }) {
  return html`<nav class=${cx("steps", compact && "steps--compact")} aria-label="Шаги">
    <div class="steps__bar"><span style=${{ width: `${(current / Math.max(1, steps.length - 1)) * 100}%` }}></span></div>
    <ol class="steps__list">
      ${steps.map((s, i) => {
        const state = i < current ? "done" : i === current ? "active" : "todo";
        const clickable = onSelect && (i <= current || s.reachable);
        return html`<li class=${cx("steps__item", `is-${state}`)}>
          <button type="button" class="steps__btn" disabled=${!clickable} aria-current=${state === "active" ? "step" : undefined} onClick=${() => clickable && onSelect(i)}>
            <span class="steps__dot">${state === "done" ? html`<${Icon} name="check" size=${12} stroke=${3} />` : i + 1}</span>
            <span class="steps__label">${s.label}</span>
          </button>
        </li>`;
      })}
    </ol>
  </nav>`;
}

/**
 * Tabs. items: [{ value, label, icon, count }]. Renders only the tab list; render the panel yourself.
 * variant: line (default) | pills
 */
export function Tabs({ value, onChange, items = [], variant = "line", label }) {
  const listRef = useRef(null);
  const onKey = (e) => {
    const i = items.findIndex((t) => t.value === value);
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const next = items[(i + (e.key === "ArrowRight" ? 1 : -1) + items.length) % items.length];
    onChange(next.value);
    const btn = listRef.current && listRef.current.querySelector(`[data-value="${CSS.escape(String(next.value))}"]`);
    btn && btn.focus();
  };
  return html`<div class=${cx("tabs", `tabs--${variant}`)} role="tablist" aria-label=${label} ref=${listRef} onKeyDown=${onKey}>
    ${items.map(
      (t) => html`<button
        type="button"
        role="tab"
        data-value=${t.value}
        aria-selected=${t.value === value}
        tabindex=${t.value === value ? 0 : -1}
        class=${cx("tabs__tab", t.value === value && "is-active")}
        onClick=${() => onChange(t.value)}
      >
        ${t.icon && html`<${Icon} name=${t.icon} size=${16} />`}
        <span>${t.label}</span>
        ${t.count != null && html`<span class="tabs__count">${t.count}</span>`}
      </button>`,
    )}
  </div>`;
}

/** Page title block used at the top of every screen. */
export function PageHeader({ title, subtitle, icon, actions, back, children }) {
  return html`<header class="page-header">
    <div class="page-header__main">
      ${back && html`<a class="page-header__back" href=${back.href}><${Icon} name="chevron-left" size=${18} />${back.label || "Назад"}</a>`}
      <div class="page-header__row">
        ${icon && html`<${Glyph} icon=${icon} tone="brand" size="lg" />`}
        <div>
          <h1 class="page-header__title">${title}</h1>
          ${subtitle && html`<p class="page-header__subtitle">${subtitle}</p>`}
        </div>
      </div>
      ${children}
    </div>
    ${actions && html`<div class="page-header__actions">${actions}</div>`}
  </header>`;
}

/** Titled group inside a page (settings groups, detail blocks). */
export function Section({ title, description, actions, id, children, class: cls = "" }) {
  return html`<section class=${cx("section", cls)} id=${id}>
    ${(title || actions) &&
    html`<div class="section__head">
      <div>
        ${title && html`<h2 class="section__title">${title}</h2>`}
        ${description && html`<p class="section__desc">${description}</p>`}
      </div>
      ${actions && html`<div class="section__actions">${actions}</div>`}
    </div>`}
    ${children}
  </section>`;
}

/** Label/value rows. rows: [[label, value], …] */
export function KeyValue({ rows = [] }) {
  return html`<dl class="kv">
    ${rows.filter(Boolean).map(([k, v]) => html`<div class="kv__row"><dt>${k}</dt><dd>${v}</dd></div>`)}
  </dl>`;
}

/** Big number with a label (KPI tiles). */
export function Stat({ label, value, hint, tone, icon }) {
  return html`<div class=${cx("stat", tone && `stat--${hue(tone)}`)}>
    <div class="stat__label">${icon && html`<${Icon} name=${icon} size=${14} />`}${label}</div>
    <div class="stat__value">${value}</div>
    ${hint && html`<div class="stat__hint">${hint}</div>`}
  </div>`;
}

/**
 * Load / limit meter (§6.8.14): 8 px bar, green < 60 %, amber 60–85 %, red > 85 %, optional marker.
 *   <Meter label="Нагрузка на Kleinanzeigen" value={64} max={150} marker={0.4} hint="Проверка каждые 30 мин — безопасно" />
 */
export function Meter({ label, value = 0, max = 100, marker, hint, valueText, tone }) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const auto = ratio > 0.85 ? "red" : ratio >= 0.6 ? "amber" : "green";
  const t = tone || auto;
  return html`<div class=${cx("meter", `meter--${t}`)}>
    ${(label || valueText !== false) &&
    html`<div class="meter__top">
      <span class="meter__label">${label}</span>
      <span class="meter__value">${valueText ?? `${Math.round(value)} / ${Math.round(max)}`}</span>
    </div>`}
    <div class="meter__track" role="meter" aria-label=${label} aria-valuemin="0" aria-valuemax=${max} aria-valuenow=${value}>
      <span class="meter__fill" style=${{ width: `${ratio * 100}%` }}></span>
      ${marker != null && html`<span class="meter__marker" style=${{ left: `${marker * 100}%` }} title="рекомендуемая граница"></span>`}
    </div>
    ${hint && html`<div class="meter__hint">${hint}</div>`}
  </div>`;
}
