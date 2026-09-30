// Tiny global state: createStore() + useStore() hook. No dependencies.
//
//   export const counter = createStore({ n: 0 });
//   counter.set({ n: counter.get().n + 1 });      // shallow merge
//   counter.update((s) => ({ n: s.n + 1 }));
//   const n = useStore(counter, (s) => s.n);      // re-renders only when n changes
import { useEffect, useState, useRef } from "./html.js";

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  const store = {
    get: () => state,
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      state = { ...state, ...next };
      subs.forEach((fn) => fn(state));
    },
    update(fn) {
      store.set(fn(state));
    },
    replace(next) {
      state = next;
      subs.forEach((fn) => fn(state));
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
  return store;
}

const identity = (s) => s;

function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || !a || !b) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  return ka.every((k) => Object.is(a[k], b[k]));
}

/** Subscribe a component to a store (optionally a slice of it). */
export function useStore(store, selector = identity) {
  const [value, setValue] = useState(() => selector(store.get()));
  const sel = useRef(selector);
  sel.current = selector;
  useEffect(() => {
    const check = (s) => {
      const next = sel.current(s);
      setValue((prev) => (shallowEqual(prev, next) ? prev : next));
    };
    check(store.get());
    return store.subscribe(check);
  }, [store]);
  return value;
}

// ----------------------------------------------------------------- app-wide state
/**
 * app:        GET /api/v1/app payload (onboarded, version, …) or null while loading
 * monitor:    GET /api/v1/monitor payload (running, paused, next_run_at, …)
 * connection: "ok" | "offline" (API unreachable) | "auth" (token needed)
 * live:       SSE status "connecting" | "open" | "closed"
 * newDeals:   deals found since the user last opened the feed (badge counter)
 */
export const appStore = createStore({
  app: null,
  monitor: null,
  connection: "ok",
  live: "connecting",
  newDeals: 0,
});
