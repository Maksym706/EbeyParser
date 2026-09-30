// Settings form state: one draft of GET /settings, per-field binding, diff → PATCH /settings,
// sticky save bar (brief §4.7) and a «Уйти без сохранения?» guard.
import { html, cx, useEffect, useState, useRef, createContext, useContext } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { addNavigationGuard, navigate } from "../../lib/router.js";
import { Button, Spinner, Icon, confirm, toast, showFieldErrors } from "../../ui/index.js";

export const SECTIONS_EDITABLE = ["region", "general", "pricing", "ai", "notifications", "ebay", "web"];

export function getIn(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

export function setIn(obj, path, value) {
  const [head, ...rest] = path.split(".");
  const copy = Array.isArray(obj) ? [...obj] : { ...(obj || {}) };
  copy[head] = rest.length ? setIn(copy[head], rest.join("."), value) : value;
  return copy;
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** Nested object of the leaves that differ (arrays are compared and sent whole). */
export function diff(base, draft) {
  if (same(base, draft)) return undefined;
  if (typeof draft !== "object" || draft === null || Array.isArray(draft) || typeof base !== "object" || base === null) return draft;
  const out = {};
  for (const k of Object.keys(draft)) {
    const d = diff(base[k], draft[k]);
    if (d !== undefined) out[k] = d;
  }
  return Object.keys(out).length ? out : undefined;
}

const FormContext = createContext(null);
export const useForm = () => useContext(FormContext);

/**
 * Holds settings + draft for the whole Settings screen.
 *   const form = useSettingsForm();  →  { settings, draft, dirty, saving, errors, bind(path), set(path, v), save(), reset(), reload() }
 */
export function useSettingsForm() {
  const [settings, setSettings] = useState(null);
  const [draft, setDraft] = useState(null);
  const [status, setStatus] = useState({ state: "idle", error: null }); // idle | saving | saved | error
  const [loadError, setLoadError] = useState(null);
  const [errors, setErrors] = useState({});

  const accept = (s) => {
    setSettings(s);
    setDraft(pickEditable(s));
  };
  const reload = () =>
    api.get("/settings").then(accept, (e) => {
      setLoadError(e);
    });
  useEffect(() => {
    reload();
  }, []);

  const base = settings ? pickEditable(settings) : null;
  const patch = base && draft ? diff(base, draft) : undefined;
  const dirty = Boolean(patch);

  const set = (path, value) => {
    setDraft((d) => setIn(d, path, value));
    if (errors[path]) setErrors((e) => ({ ...e, [path]: undefined }));
    if (status.state === "saved" || status.state === "error") setStatus({ state: "idle", error: null });
  };

  const save = async () => {
    if (!patch) return true;
    // never save (or silently clamp) an impossible number: point at the field instead (P1-9)
    const body = document.querySelector(".settings-body");
    if (body && showFieldErrors(body)) {
      setStatus({ state: "error", error: { message: "исправь поле, отмеченное красным" } });
      return false;
    }
    setStatus({ state: "saving", error: null });
    try {
      const res = await api.patch("/settings", patch);
      accept(res);
      setErrors({});
      setStatus({ state: "saved", error: null });
      setTimeout(() => setStatus((s) => (s.state === "saved" ? { state: "idle", error: null } : s)), 2000);
      if (res.restart_required && res.restart_required.length) toast.info("Изменения вступят в силу после перезапуска программы");
      return true;
    } catch (e) {
      setErrors(e.fields || {});
      setStatus({ state: "error", error: e });
      return false;
    }
  };
  const reset = () => {
    if (settings) setDraft(pickEditable(settings));
    setErrors({});
    setStatus({ state: "idle", error: null });
  };

  /** Props for a control bound to a dotted path. scale: 100 shows a 0..1 fraction as percent. */
  const bind = (path, { scale } = {}) => {
    const raw = draft ? getIn(draft, path) : undefined;
    const value = scale && raw != null ? Math.round(raw * scale * 100) / 100 : raw;
    const error = errors[path] || errorFor(errors, path);
    return {
      value,
      onChange: (v) => set(path, scale && v != null && typeof v === "number" ? v / scale : v),
      invalid: Boolean(error) || undefined,
    };
  };
  /** Validation message for a dotted path (after a failed save). */
  const err = (path) => errors[path] || errorFor(errors, path);

  return { settings, setSettings: accept, draft, dirty, patch, status, errors, loadError, set, save, reset, reload, bind, err };
}

function errorFor(errors, path) {
  const tail = path.split(".").slice(-1)[0];
  const hit = Object.keys(errors).find((k) => k === tail || k.endsWith("." + tail));
  return hit ? errors[hit] : undefined;
}

function pickEditable(s) {
  const out = {};
  for (const k of SECTIONS_EDITABLE) if (s[k] !== undefined) out[k] = strip(s[k]);
  return out;
}

function strip(v) {
  // read-only helper fields are not sent back
  if (!v || typeof v !== "object" || Array.isArray(v)) return v;
  const { configured, radius_choices, ...rest } = v;
  return rest;
}

export function FormProvider({ form, children }) {
  return html`<${FormContext.Provider} value=${form}>${children}<//>`;
}

/** Guard navigation away from unsaved changes (router links + closing the tab). */
export function useUnsavedGuard(form) {
  const ref = useRef(form);
  ref.current = form;
  useEffect(() => {
    const off = addNavigationGuard((url) => {
      const f = ref.current;
      if (!f.dirty || url.pathname === window.location.pathname) return true;
      confirm({
        title: "Уйти без сохранения?",
        message: "Изменения на этой странице не сохранены.",
        confirmLabel: "Уйти без сохранения",
        cancelLabel: "Остаться",
        tone: "danger",
      }).then((ok) => {
        if (!ok) return;
        f.reset();
        setTimeout(() => navigate(url.pathname + url.search + url.hash), 0);
      });
      return false;
    });
    const onUnload = (e) => {
      if (ref.current.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onUnload);
    return () => {
      off();
      window.removeEventListener("beforeunload", onUnload);
    };
  }, []);
}

/** Sticky bar: «Есть несохранённые изменения · Отменить · Сохранить» → «Сохранено ✓». */
export function SaveBar({ form }) {
  const { dirty, status } = form;
  const visible = dirty || status.state === "saved" || status.state === "error";
  if (!visible) return null;
  const state = status.state === "error" ? "error" : status.state === "saved" && !dirty ? "saved" : "dirty";
  return html`<div class=${cx("savebar", `savebar--${state}`)} role="status">
    <div class="savebar__inner">
      ${state === "saved" && html`<span class="savebar__text"><${Icon} name="circle-check" size=${18} />Сохранено</span>`}
      ${state === "error" && html`<span class="savebar__text"><${Icon} name="circle-alert" size=${18} />Не сохранилось: ${status.error && status.error.message}</span>`}
      ${state === "dirty" && html`<span class="savebar__text">${status.state === "saving" ? html`<${Spinner} size=${14} /> Применяю…` : "Есть несохранённые изменения"}</span>`}
      ${state !== "saved" &&
      html`<div class="savebar__actions">
        <${Button} variant="ghost" size="sm" onClick=${form.reset} disabled=${status.state === "saving"}>Отменить<//>
        <${Button} variant="primary" size="sm" loading=${status.state === "saving"} onClick=${form.save}>${state === "error" ? "Ещё раз" : "Сохранить"}<//>
      </div>`}
    </div>
  </div>`;
}

/** Card with a title, one-line description and rows (brief §4.7 "setting groups"). */
export function Group({ title, description, icon, actions, children, tone, id }) {
  return html`<section class=${cx("sgroup", tone && `sgroup--${tone}`)} id=${id}>
    ${(title || actions) &&
    html`<header class="sgroup__head">
      ${icon && html`<span class="sgroup__icon"><${Icon} name=${icon} size=${18} /></span>`}
      <div class="sgroup__titles">
        <h2 class="sgroup__title">${title}</h2>
        ${description && html`<p class="sgroup__desc">${description}</p>`}
      </div>
      ${actions && html`<div class="sgroup__actions">${actions}</div>`}
    </header>`}
    <div class="sgroup__body">${children}</div>
  </section>`;
}
