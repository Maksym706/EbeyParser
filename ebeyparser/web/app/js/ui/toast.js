// Toast notifications.
//   toast.success("Сохранено");
//   toast.error(err.message);
//   toast({ kind: "deal", title: "Новая сделка", message: "iPhone 13 · +80 €", action: { label: "Открыть", href: "/deal/1" } });
//   await toast.promise(api.post("/run"), { loading: "Запускаю…", success: "Проверка началась", error: (e) => e.message });
import { html, useEffect, useLayoutEffect, useRef, useState, cx } from "../lib/html.js";
import { createStore, useStore } from "../lib/store.js";
import { navigate } from "../lib/router.js";
import { Icon } from "./icons.js";

const store = createStore({ items: [] });
let seq = 0;

const KIND_ICON = {
  success: "circle-check",
  error: "circle-alert",
  warning: "triangle-alert",
  info: "info",
  deal: "sparkles",
  loading: "loader-circle",
  neutral: "bell",
};

export function toast(input, extra = {}) {
  const opts = typeof input === "string" ? { title: input, ...extra } : { ...input };
  const id = opts.id || `t${++seq}`;
  const item = {
    id,
    kind: opts.kind || "neutral",
    title: opts.title || "",
    message: opts.message || "",
    action: opts.action || null,
    action2: opts.action2 || null,
    details: opts.details || "",
    icon: opts.icon,
    // §6.8.10: 4 s plain, 6 s with an action, 8 s for errors; loading toasts stay until replaced
    duration: opts.duration ?? (opts.kind === "loading" ? 0 : opts.kind === "error" ? 8000 : opts.action ? 6000 : 4000),
  };
  store.update((s) => {
    const rest = s.items.filter((t) => t.id !== id);
    return { items: [...rest, item].slice(-3) };
  });
  return id;
}

export function dismissToast(id) {
  store.update((s) => ({ items: s.items.map((t) => (t.id === id ? { ...t, leaving: true } : t)) }));
  setTimeout(() => store.update((s) => ({ items: s.items.filter((t) => t.id !== id) })), 200);
}

toast.success = (title, o) => toast({ ...o, title, kind: "success" });
toast.error = (title, o) =>
  toast({
    ...o,
    title: typeof title === "object" && title ? title.message : title,
    details: (o && o.details) || (typeof title === "object" && title ? title.details : ""),
    kind: "error",
  });
toast.warning = (title, o) => toast({ ...o, title, kind: "warning" });
toast.info = (title, o) => toast({ ...o, title, kind: "info" });
toast.dismiss = dismissToast;
toast.promise = async (promise, { loading = "Подожди…", success = "Готово", error = (e) => e.message } = {}) => {
  const id = toast({ title: loading, kind: "loading" });
  try {
    const result = await promise;
    toast({ id, kind: "success", title: typeof success === "function" ? success(result) : success });
    return result;
  } catch (e) {
    toast({ id, kind: "error", title: typeof error === "function" ? error(e) : error, details: e && e.details });
    throw e;
  }
};

function ToastItem({ t }) {
  const [paused, setPaused] = useState(false);
  const [more, setMore] = useState(false);
  useEffect(() => {
    if (!t.duration || paused || more || t.leaving) return undefined;
    const timer = setTimeout(() => dismissToast(t.id), t.duration);
    return () => clearTimeout(timer);
  }, [t.duration, paused, more, t.leaving, t.id, t.title]);
  const act = t.action;
  const run = (a) => {
    if (a.onClick) a.onClick();
    if (a.href) navigate(a.href);
    dismissToast(t.id);
  };
  return html`<div
    class=${cx("toast", `toast--${t.kind}`, t.leaving && "is-leaving")}
    role=${t.kind === "error" ? "alert" : "status"}
    onMouseEnter=${() => setPaused(true)}
    onMouseLeave=${() => setPaused(false)}
  >
    <span class="toast__icon"><${Icon} name=${t.icon || KIND_ICON[t.kind] || "bell"} size=${18} class=${t.kind === "loading" ? "spin" : ""} /></span>
    <div class="toast__body">
      <div class="toast__title">${t.title}</div>
      ${t.message && html`<div class="toast__message">${t.message}</div>`}
      ${t.details &&
      html`<button type="button" class="toast__more" aria-expanded=${more} onClick=${() => setMore(!more)}>${more ? "Скрыть подробности" : "Подробнее"}</button>`}
      ${more && html`<pre class="toast__details">${t.details}</pre>`}
    </div>
    ${t.action2 && html`<button class="toast__action toast__action--quiet" type="button" onClick=${() => run(t.action2)}>${t.action2.label}</button>`}
    ${act && html`<button class="toast__action" type="button" onClick=${() => run(act)}>${act.label}</button>`}
    <button class="toast__close" type="button" aria-label="Закрыть" onClick=${() => dismissToast(t.id)}>
      <${Icon} name="x" size=${16} />
    </button>
  </div>`;
}

// Controls docked at the bottom of the screen that a toast must never cover (brief §6.8.10):
// drawer / sheet / dialog footers, the phone deal action bar, the settings save bar, the tab bar,
// the onboarding footer.
const OBSTACLES = [
  ".overlay:not(.is-leaving) .drawer__footer",
  ".overlay:not(.is-leaving) .modal__footer",
  ".dv-bottom",
  ".savebar__inner",
  ".tabbar",
  ".onb-footer",
].join(",");

/** Keep the toast stack clear of docked controls; beside an open side drawer on desktop. */
function useToasterPlacement(ref, active) {
  useLayoutEffect(() => {
    if (!active) return undefined;
    const place = () => {
      const el = ref.current;
      if (!el) return;
      const W = window.innerWidth;
      const H = window.innerHeight;
      const phone = W < 600;
      const side = phone ? 16 : 24;
      let width = phone ? W - 2 * side : Math.min(360, W - 2 * side);
      let right = side;
      let bottom = phone ? 12 : 24;
      if (!phone) {
        // a side drawer is open: sit left of it, over the dimmed page, not over its content
        const drawer = document.querySelector(".overlay:not(.is-leaving) > .drawer");
        const r = drawer && drawer.getBoundingClientRect();
        const space = r ? r.left - 2 * side : 0;
        if (r && r.width && r.height > H * 0.6 && space >= 280) {
          width = Math.min(360, space);
          right = W - r.left + side;
        }
      }
      const left = W - right - width;
      for (const o of document.querySelectorAll(OBSTACLES)) {
        const r = o.getBoundingClientRect();
        if (!r.width || !r.height || r.bottom < H - 200) continue; // only things docked at the bottom
        if (r.right <= left || r.left >= W - right) continue; // no horizontal overlap
        bottom = Math.max(bottom, Math.round(H - r.top + 12));
      }
      el.style.setProperty("--toaster-right", `${right}px`);
      if (!phone) el.style.setProperty("--toaster-width", `${width}px`);
      el.style.setProperty("--toaster-bottom", `${bottom}px`);
    };
    place();
    const timer = setInterval(place, 250);
    window.addEventListener("resize", place);
    return () => {
      clearInterval(timer);
      window.removeEventListener("resize", place);
    };
  }, [active]);
}

export function Toaster() {
  const { items } = useStore(store);
  const ref = useRef(null);
  useToasterPlacement(ref, items.length > 0);
  return html`<div class="toaster" ref=${ref} aria-live="polite" aria-relevant="additions">
    ${items.map((t) => html`<${ToastItem} key=${t.id} t=${t} />`)}
  </div>`;
}
