// Live updates from GET /api/v1/events (Server-Sent Events).
//
//   import { onEvent } from "../lib/events.js";
//   useEffect(() => onEvent("deal_found", (data) => reload()), []);
//   onEvent("*", ({ type, data }) => …)          // every event
//
// Event types (see ebeyparser/web/api/events.py): ready, run_started, run_progress, run_finished,
// deal_found {ad_id, verdict, action, score, card}, deal_updated {ad_id, card},
// health_alert {kind, text, at}, settings_changed, searches_changed, data_changed,
// monitor_paused, monitor_resumed, job_progress, job_finished (a Job),
// project_updated {id, reason, card, ad_id?, slot?, alert?} («Сборки», features/projects-live.js).
// Plus the local pseudo-event "connected". EventSource only delivers named events that have a
// listener — every name in KNOWN gets one; add new server event names here.
// Reconnects with backoff; handlers get (data, type).
import { API_BASE, authToken } from "./api.js";
import { appStore } from "./store.js";

const KNOWN = [
  "ready",
  "run_started",
  "run_progress",
  "run_finished",
  "deal_found",
  "deal_updated",
  "health_alert",
  "settings_changed",
  "searches_changed",
  "data_changed",
  "monitor_paused",
  "monitor_resumed",
  "job_progress",
  "job_finished",
  "project_updated",
];

const handlers = new Map(); // type -> Set(fn)
let source = null;
let retry = 0;
let timer = null;

export function onEvent(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  return () => handlers.get(type).delete(fn);
}

export function emit(type, data) {
  (handlers.get(type) || []).forEach((fn) => safe(fn, data, type));
  (handlers.get("*") || []).forEach((fn) => safe(fn, { type, data }, type));
}

function safe(fn, ...args) {
  try {
    fn(...args);
  } catch (e) {
    console.error("event handler failed", e);
  }
}

function dispatch(ev, fallbackType) {
  let data = ev.data;
  try {
    data = JSON.parse(ev.data);
  } catch {
    /* plain text */
  }
  const type = ev.type && ev.type !== "message" ? ev.type : (data && data.type) || fallbackType;
  const payload = data && typeof data === "object" && "data" in data && data.type === type ? data.data : data;
  emit(type, payload);
}

export function connectEvents() {
  if (!("EventSource" in window)) {
    appStore.set({ live: "closed" });
    return;
  }
  clearTimeout(timer);
  if (source) source.close();
  const token = authToken();
  const url = API_BASE + "/events" + (token ? `?token=${encodeURIComponent(token)}` : "");
  appStore.set({ live: "connecting" });
  source = new EventSource(url, { withCredentials: true });
  source.onopen = () => {
    retry = 0;
    appStore.set({ live: "open" });
    emit("connected", null);
  };
  source.onmessage = (ev) => dispatch(ev, "message");
  KNOWN.forEach((name) => source.addEventListener(name, (ev) => dispatch(ev, name)));
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) {
      appStore.set({ live: "closed" });
      source.close();
      source = null;
      retry = Math.min(retry + 1, 6);
      timer = setTimeout(connectEvents, Math.min(60000, 1500 * 2 ** retry));
    } else {
      appStore.set({ live: "connecting" });
    }
  };
}

export function disconnectEvents() {
  clearTimeout(timer);
  if (source) source.close();
  source = null;
}
