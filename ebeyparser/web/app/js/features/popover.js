// Click-triggered popover and overflow menu (works on touch; Esc / outside click close).
//   <Popover label="Как считается?" trigger=${(p) => html`<button ...${p}>?</button>`}>…</Popover>
//   <Menu label="Ещё" items=${[{ label, icon, onClick, href, danger }]} />
import { html, cx, useState, useRef, useEffect, useId } from "../lib/html.js";
import { Icon } from "../ui/index.js";
import "./icons-extra.js";

export function Popover({ trigger, children, align = "end", width = 320, class: cls = "", label }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const id = useId();
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
  const props = {
    "aria-expanded": open,
    "aria-controls": id,
    "aria-haspopup": "dialog",
    onClick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      setOpen(!open);
    },
  };
  return html`<span class=${cx("pop", cls)} ref=${ref}>
    ${trigger(props, open)}
    ${open &&
    html`<div class=${cx("pop__panel", `pop__panel--${align}`)} id=${id} role="dialog" aria-label=${label} style=${{ width: `min(${width}px, calc(100vw - 32px))` }}>
      ${typeof children === "function" ? children(() => setOpen(false)) : children}
    </div>`}
  </span>`;
}

export function Menu({ items = [], label = "Ещё", icon = "ellipsis", align = "end", buttonClass = "", variant = "icon", text }) {
  return html`<${Popover}
    align=${align}
    width=${260}
    label=${label}
    trigger=${(p, open) =>
      variant === "icon"
        ? html`<button type="button" class=${cx("icon-btn icon-btn--ghost icon-btn--md", open && "is-active", buttonClass)} aria-label=${label} title=${label} ...${p}>
            <${Icon} name=${icon} size=${20} />
          </button>`
        : html`<button type="button" class=${cx("btn btn--secondary btn--md", buttonClass)} ...${p}><${Icon} name=${icon} size=${18} /><span class="btn__label">${text || label}</span></button>`}
  >
    ${(close) => html`<div class="menu" role="menu">
      ${items.filter(Boolean).map((it) =>
        it.divider
          ? html`<div class="menu__sep" role="separator"></div>`
          : it.href
            ? html`<a
                role="menuitem"
                class=${cx("menu__item", it.danger && "is-danger")}
                href=${it.href}
                target=${/^https?:/.test(it.href) ? "_blank" : undefined}
                rel=${/^https?:/.test(it.href) ? "noopener noreferrer" : undefined}
                onClick=${() => close()}
                ><${Icon} name=${it.icon || "arrow-right"} size=${16} /><span>${it.label}</span></a
              >`
            : html`<button
                type="button"
                role="menuitem"
                class=${cx("menu__item", it.danger && "is-danger")}
                disabled=${it.disabled}
                onClick=${() => {
                  close();
                  it.onClick && it.onClick();
                }}
              >
                <${Icon} name=${it.icon || "circle"} size=${16} /><span>${it.label}</span>${it.hint && html`<small>${it.hint}</small>`}
              </button>`,
      )}
    </div>`}
  <//>`;
}
