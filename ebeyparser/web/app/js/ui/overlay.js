// Overlays: Modal, Drawer (right drawer on desktop, bottom sheet on phones), confirm(), Portal.
//
//   <Drawer open={open} onClose={() => setOpen(false)} title="Сделка" footer={…}>…</Drawer>
//   <Modal open={open} onClose={close} title="Проверить ссылку" size="sm">…</Modal>
//   if (await confirm({ title: "Удалить поиск?", message: "…", confirmLabel: "Удалить", tone: "danger" })) …
import { html, render, cx, useEffect, useLayoutEffect, useMemo, useRef, useState } from "../lib/html.js";
import { createStore, useStore } from "../lib/store.js";
import { Icon } from "./icons.js";
import { Button, IconButton } from "./core.js";

/** Render children into document.body (escapes overflow/transform of parents). */
export function Portal({ children }) {
  const el = useMemo(() => {
    const d = document.createElement("div");
    d.className = "portal";
    return d;
  }, []);
  useLayoutEffect(() => {
    document.body.appendChild(el);
    return () => {
      render(null, el);
      el.remove();
    };
  }, [el]);
  useLayoutEffect(() => {
    render(children, el);
  });
  return null;
}

/** Keep an element mounted while its exit animation plays. */
export function usePresence(open, ms = 200) {
  const [mounted, setMounted] = useState(open);
  const [leaving, setLeaving] = useState(false);
  useEffect(() => {
    if (open) {
      setMounted(true);
      setLeaving(false);
      return undefined;
    }
    if (!mounted) return undefined;
    setLeaving(true);
    const t = setTimeout(() => {
      setMounted(false);
      setLeaving(false);
    }, ms);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return { mounted, leaving };
}

let openCount = 0;
function lockScroll(on) {
  openCount += on ? 1 : -1;
  document.documentElement.classList.toggle("scroll-locked", openCount > 0);
}

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/**
 * Focus trap + Escape + scroll lock + focus restore for a dialog element.
 * The element mounts a frame after `open` flips (usePresence + Portal), so it is looked up lazily.
 * `restoreFocus(prev)` may return an element to focus on close (e.g. the card that opened a drawer).
 */
function useDialog(ref, open, onClose, restoreFocus, modal = true) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const restoreRef = useRef(restoreFocus);
  restoreRef.current = restoreFocus;
  useEffect(() => {
    if (!open) return undefined;
    const prev = document.activeElement;
    if (modal) lockScroll(true);
    let tries = 0;
    let t = null;
    const focusIn = () => {
      const el = ref.current;
      if (!el) {
        if (tries++ < 20) t = setTimeout(focusIn, 25);
        return;
      }
      if (el.contains(document.activeElement)) return;
      const auto = el.querySelector("[autofocus],[data-autofocus]");
      (auto || el).focus({ preventScroll: true });
    };
    t = setTimeout(focusIn, 30);
    const onKey = (e) => {
      const el = ref.current;
      // only the top-most dialog reacts (a confirm() above a drawer)
      const all = document.querySelectorAll(".overlay:not(.is-leaving) [role=dialog]");
      if (el && all.length && all[all.length - 1] !== el) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current && onCloseRef.current();
      } else if (e.key === "Tab" && el && modal) {
        const items = [...el.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (!el.contains(document.activeElement)) {
          e.preventDefault();
          (e.shiftKey ? last : first).focus();
        } else if (e.shiftKey && (document.activeElement === first || document.activeElement === el)) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      clearTimeout(t);
      if (modal) lockScroll(false);
      document.removeEventListener("keydown", onKey, true);
      const target = (restoreRef.current && restoreRef.current(prev)) || prev;
      if (target && target.focus && target !== document.body) target.focus({ preventScroll: true });
    };
  }, [open, modal]);
}

/** Centered dialog. size: sm | md | lg. On phones it becomes a bottom sheet. */
export function Modal({ open, onClose, title, subtitle, icon, size = "md", footer, dismissable = true, class: cls = "", children }) {
  const { mounted, leaving } = usePresence(open);
  const ref = useRef(null);
  useDialog(ref, open, dismissable ? onClose : null);
  if (!mounted) return null;
  return html`<${Portal}>
    <div class=${cx("overlay", leaving && "is-leaving")} onPointerDown=${(e) => dismissable && e.target === e.currentTarget && onClose && onClose()}>
      <div class=${cx("modal", `modal--${size}`, cls)} role="dialog" aria-modal="true" aria-label=${title} tabindex="-1" ref=${ref}>
        ${(title || dismissable) &&
        html`<header class="modal__header">
          ${icon && html`<span class="modal__icon"><${Icon} name=${icon} size=${22} /></span>`}
          <div class="modal__titles">
            ${title && html`<h2 class="modal__title">${title}</h2>`}
            ${subtitle && html`<p class="modal__subtitle">${subtitle}</p>`}
          </div>
          ${dismissable && html`<${IconButton} icon="x" label="Закрыть" size="sm" class="modal__close" onClick=${onClose} />`}
        </header>`}
        <div class="modal__body">${children}</div>
        ${footer && html`<footer class="modal__footer">${footer}</footer>`}
      </div>
    </div>
  <//>`;
}

/** Side drawer (desktop) / bottom sheet (phone). width in px for desktop. */
export function Drawer({ open, onClose, title, subtitle, header, footer, width = 520, class: cls = "", restoreFocus, modal = true, children }) {
  const { mounted, leaving } = usePresence(open, 240);
  const ref = useRef(null);
  const [drag, setDrag] = useState(0);
  const start = useRef(null);
  // modal=false: a side panel next to the page (wide screens) — no scrim, the page stays usable
  useDialog(ref, open, onClose, restoreFocus, modal);
  if (!mounted) return null;
  const onPointerDown = (e) => {
    start.current = e.clientY;
    e.currentTarget.setPointerCapture && e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e) => {
    if (start.current == null) return;
    setDrag(Math.max(0, e.clientY - start.current));
  };
  const onPointerUp = () => {
    if (drag > 110) onClose && onClose();
    start.current = null;
    setDrag(0);
  };
  return html`<${Portal}>
    <div class=${cx("overlay overlay--drawer", !modal && "overlay--panel", leaving && "is-leaving")} onPointerDown=${(e) => modal && e.target === e.currentTarget && onClose && onClose()}>
      <aside
        class=${cx("drawer", cls, drag > 0 && "is-dragging")}
        role="dialog"
        aria-modal=${modal ? "true" : "false"}
        aria-label=${title}
        tabindex="-1"
        ref=${ref}
        style=${{ "--drawer-w": `${width}px`, "--drag": `${drag}px` }}
      >
        <div class="drawer__grab" onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${onPointerUp} onPointerCancel=${onPointerUp}>
          <span></span>
        </div>
        <header class="drawer__header">
          ${header ||
          html`<div class="drawer__titles">
            ${title && html`<h2 class="drawer__title">${title}</h2>`}
            ${subtitle && html`<p class="drawer__subtitle">${subtitle}</p>`}
          </div>`}
          <${IconButton} icon="x" label="Закрыть" size="sm" class="drawer__close" onClick=${onClose} />
        </header>
        <div class="drawer__body">${children}</div>
        ${footer && html`<footer class="drawer__footer">${footer}</footer>`}
      </aside>
    </div>
  <//>`;
}

// ------------------------------------------------------------ imperative confirm()
const dialogs = createStore({ current: null });

/**
 * await confirm({ title, message, confirmLabel = "Да", cancelLabel = "Отмена", tone = "primary"|"danger", icon })
 * → true / false
 */
export function confirm(opts) {
  return new Promise((resolve) => {
    dialogs.set({ current: { ...opts, resolve } });
  });
}

/** Mounted once by the app shell. */
export function DialogHost() {
  const { current } = useStore(dialogs);
  const [last, setLast] = useState(null);
  useEffect(() => {
    if (current) setLast(current);
  }, [current]);
  const d = current || last;
  const close = (result) => {
    if (current) current.resolve(result);
    dialogs.set({ current: null });
  };
  if (!d) return null;
  const danger = d.tone === "danger";
  return html`<${Modal}
    open=${Boolean(current)}
    onClose=${() => close(false)}
    size="sm"
    class="confirm"
    footer=${html`
      <${Button} variant="ghost" onClick=${() => close(false)}>${d.cancelLabel || "Отмена"}<//>
      <${Button} variant=${danger ? "danger" : "primary"} data-autofocus onClick=${() => close(true)}>${d.confirmLabel || "Да"}<//>
    `}
  >
    <div class="confirm__content">
      <span class=${cx("confirm__icon", danger && "is-danger")}><${Icon} name=${d.icon || (danger ? "triangle-alert" : "circle-question-mark")} size=${24} /></span>
      <h2 class="confirm__title">${d.title}</h2>
      ${d.message && html`<p class="confirm__message">${d.message}</p>`}
    </div>
  <//>`;
}
