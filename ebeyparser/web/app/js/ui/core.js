// Basic building blocks: Button, IconButton, Card, Badge, Chip, Spinner, Avatar, Glyph,
// Money, Tooltip, Kbd, Divider, StatusDot.
import { html, cx } from "../lib/html.js";
import { money } from "../lib/format.js";
import { Icon } from "./icons.js";

export function Spinner({ size = 18, class: cls = "" }) {
  return html`<span class=${cx("spinner", cls)} style=${{ width: size, height: size }} role="progressbar" aria-label="Загрузка"></span>`;
}

/**
 * <Button variant="primary|secondary|ghost|danger|soft" size="sm|md|lg" icon="plus" loading block href="/x">
 * With `href` it renders a link (internal links are routed without reload; external ones open a new tab).
 */
export function Button({
  variant = "secondary",
  size = "md",
  icon,
  iconRight,
  loading = false,
  block = false,
  disabled = false,
  href,
  type = "button",
  class: cls = "",
  children,
  ...rest
}) {
  const classes = cx("btn", `btn--${variant}`, `btn--${size}`, block && "btn--block", loading && "is-loading", cls);
  const iconSize = size === "sm" ? 16 : size === "lg" ? 20 : 18;
  const inner = html`
    ${loading ? html`<${Spinner} size=${iconSize} class="btn__spinner" />` : icon && html`<${Icon} name=${icon} size=${iconSize} />`}
    ${children != null && children !== false && html`<span class="btn__label">${children}</span>`}
    ${iconRight && !loading && html`<${Icon} name=${iconRight} size=${iconSize} />`}
  `;
  if (href) {
    const external = /^https?:/.test(href);
    return html`<a
      class=${classes}
      href=${href}
      target=${external ? "_blank" : undefined}
      rel=${external ? "noopener noreferrer" : undefined}
      aria-disabled=${disabled || undefined}
      ...${rest}
      >${inner}</a
    >`;
  }
  return html`<button class=${classes} type=${type} disabled=${disabled || loading} aria-busy=${loading || undefined} ...${rest}>
    ${inner}
  </button>`;
}

/** Square icon-only button with an accessible label (shown as tooltip). */
export function IconButton({ icon, label, size = "md", variant = "ghost", badge, active, class: cls = "", loading, ...rest }) {
  const iconSize = size === "sm" ? 16 : size === "lg" ? 22 : 20;
  return html`<button
    type="button"
    class=${cx("icon-btn", `icon-btn--${variant}`, `icon-btn--${size}`, active && "is-active", cls)}
    aria-label=${label}
    title=${label}
    disabled=${loading || rest.disabled}
    ...${rest}
  >
    ${loading ? html`<${Spinner} size=${iconSize - 2} />` : html`<${Icon} name=${icon} size=${iconSize} />`}
    ${badge ? html`<span class="icon-btn__badge">${badge > 99 ? "99+" : badge}</span>` : null}
  </button>`;
}

/** Surface container. `interactive` adds hover lift; `as="a"` + href for clickable cards. */
export function Card({ as = "div", interactive = false, padded = true, tone, class: cls = "", children, ...rest }) {
  const Tag = as;
  return html`<${Tag} class=${cx("card", padded && "card--padded", interactive && "card--interactive", tone && `card--${tone}`, cls)} ...${rest}>
    ${children}
  <//>`;
}

export function CardHeader({ title, subtitle, icon, tone = "neutral", actions, children }) {
  return html`<div class="card__header">
    ${icon && html`<${Glyph} icon=${icon} tone=${tone} />`}
    <div class="card__heading">
      <h3 class="card__title">${title}</h3>
      ${subtitle && html`<p class="card__subtitle">${subtitle}</p>`}
      ${children}
    </div>
    ${actions && html`<div class="card__actions">${actions}</div>`}
  </div>`;
}

/**
 * Small status label. tone: neutral | profit | haggle | bid | danger | info | brand
 * variant: soft (default) | solid | outline
 */
export function Badge({ tone = "neutral", variant = "soft", icon, size = "md", class: cls = "", children, ...rest }) {
  return html`<span class=${cx("badge", `badge--${tone}`, `badge--${variant}`, `badge--${size}`, cls)} ...${rest}>
    ${icon && html`<${Icon} name=${icon} size=${size === "sm" ? 12 : 14} stroke=${2.25} />`}${children}
  </span>`;
}

/** Selectable pill (filters, multi-select). */
export function Chip({ selected = false, icon, count, onClick, disabled, class: cls = "", children, ...rest }) {
  return html`<button
    type="button"
    class=${cx("chip", selected && "is-selected", cls)}
    aria-pressed=${selected}
    onClick=${onClick}
    disabled=${disabled}
    ...${rest}
  >
    ${icon && html`<${Icon} name=${icon} size=${16} />`}
    <span>${children}</span>
    ${count != null && html`<span class="chip__count">${count}</span>`}
  </button>`;
}

/** Colored rounded tile with an icon — for list rows, cards, onboarding steps. */
export function Glyph({ icon, tone = "neutral", size = "md", class: cls = "" }) {
  const px = size === "sm" ? 16 : size === "lg" ? 26 : size === "xl" ? 32 : 20;
  return html`<span class=${cx("glyph", `glyph--${tone}`, `glyph--${size}`, cls)} aria-hidden="true"><${Icon} name=${icon} size=${px} /></span>`;
}

export function Avatar({ name = "", src, size = 36, tone }) {
  const initials = String(name)
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
  const hue = [...String(name)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);
  return html`<span
    class=${cx("avatar", tone && `avatar--${tone}`)}
    style=${{ width: size, height: size, fontSize: Math.round(size * 0.4), "--avatar-hue": hue }}
    aria-hidden="true"
  >
    ${src ? html`<img src=${src} alt="" loading="lazy" />` : initials || html`<${Icon} name="user" size=${Math.round(size * 0.5)} />`}
  </span>`;
}

/** Price with tabular digits. tone="profit" colours it green, sign adds "+". */
export function Money({ value, sign = false, cents = false, tone, size, class: cls = "" }) {
  const auto = tone === "auto" ? (value > 0 ? "profit" : value < 0 ? "danger" : "neutral") : tone;
  return html`<span class=${cx("money", auto && `text-${auto}`, size && `money--${size}`, cls)}>${money(value, { sign, cents })}</span>`;
}

/** Hover / focus tooltip around any element. */
export function Tooltip({ text, placement = "top", children }) {
  if (!text) return children;
  return html`<span class=${cx("tooltip", `tooltip--${placement}`)} tabindex="-1">
    ${children}
    <span class="tooltip__bubble" role="tooltip">${text}</span>
  </span>`;
}

export function Kbd({ children }) {
  return html`<kbd class="kbd">${children}</kbd>`;
}

export function Divider({ label }) {
  return label ? html`<div class="divider divider--label"><span>${label}</span></div>` : html`<hr class="divider" />`;
}

/** Coloured dot, optionally pulsing (live status). tone: profit | haggle | danger | info | neutral */
export function StatusDot({ tone = "neutral", pulse = false }) {
  return html`<span class=${cx("status-dot", `status-dot--${tone}`, pulse && "is-pulsing")} aria-hidden="true"></span>`;
}

/** External link that opens in a new tab with an arrow icon. */
export function ExternalLink({ href, children, class: cls = "" }) {
  return html`<a class=${cx("ext-link", cls)} href=${href} target="_blank" rel="noopener noreferrer"
    >${children}<${Icon} name="arrow-up-right" size=${14} /></a
  >`;
}
