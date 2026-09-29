// Toast notifications.
//   toast.success("Сохранено");
//   toast.error(err.message);
//   toast({ kind: "deal", title: "Новая сделка", message: "iPhone 13 · +80 €", action: { label: "Открыть", href: "/deal/1" } });
//   await toast.promise(api.post("/run"), { loading: "Запускаю…", success: "Проверка началась", error: (e) => e.message });
import { html, useEffect, useState, cx } from "../lib/html.js";
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
    icon: opts.icon,
    duration: opts.duration ?? (opts.kind === "error" ? 7000 : opts.kind === "loading" ? 0 : 4500),
  };
  store.update((s) => {
    const rest = s.items.filter((t) => t.id !== id);
    return { items: [...rest, item].slice(-4) };
  });
  return id;
}

export function dismissToast(id) {
  store.update((s) => ({ items: s.items.map((t) => (t.id === id ? { ...t, leaving: true } : t)) }));
  setTimeout(() => store.update((s) => ({ items: s.items.filter((t) => t.id !== id) })), 200);
}

toast.success = (title, o) => toast({ ...o, title, kind: "success" });
toast.error = (title, o) => toast({ ...o, title: typeof title === "object" && title ? title.message : title, kind: "error" });
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
    toast({ id, kind: "error", title: typeof error === "function" ? error(e) : error });
    throw e;
  }
};

function ToastItem({ t }) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (!t.duration || paused || t.leaving) return undefined;
    const timer = setTimeout(() => dismissToast(t.id), t.duration);
    return () => clearTimeout(timer);
  }, [t.duration, paused, t.leaving, t.id, t.title]);
  const act = t.action;
  const onAction = () => {
    if (act.onClick) act.onClick();
    if (act.href) navigate(act.href);
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
    </div>
    ${act && html`<button class="toast__action" type="button" onClick=${onAction}>${act.label}</button>`}
    <button class="toast__close" type="button" aria-label="Закрыть" onClick=${() => dismissToast(t.id)}>
      <${Icon} name="x" size=${16} />
    </button>
  </div>`;
}

export function Toaster() {
  const { items } = useStore(store);
  return html`<div class="toaster" aria-live="polite" aria-relevant="additions">
    ${items.map((t) => html`<${ToastItem} key=${t.id} t=${t} />`)}
  </div>`;
}
