// Reusable hooks for screens.
import { useState, useEffect, useRef, useCallback } from "./html.js";

/**
 * Load data asynchronously.
 *   const { data, error, loading, reload, setData } = useAsync(() => api.get("/deals"), [filter]);
 * `error` is an ApiError (see api.js); `error.missing` means the endpoint does not exist yet.
 */
export function useAsync(fn, deps = [], { keepPrevious = true } = {}) {
  const [state, setState] = useState({ data: undefined, error: null, loading: true });
  const seq = useRef(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const run = useCallback(
    (silent = false) => {
      const id = ++seq.current;
      if (!silent) setState((s) => ({ data: keepPrevious ? s.data : undefined, error: null, loading: true }));
      return Promise.resolve()
        .then(() => fnRef.current())
        .then(
          (data) => {
            if (id === seq.current) setState({ data, error: null, loading: false });
            return data;
          },
          (error) => {
            if (id === seq.current) setState((s) => ({ data: s.data, error, loading: false }));
          },
        );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  useEffect(() => {
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  const setData = useCallback((data) => setState((s) => ({ ...s, data: typeof data === "function" ? data(s.data) : data })), []);
  return { ...state, reload: run, setData };
}

/** Value that only updates after `delay` ms without changes (search boxes, sliders). */
export function useDebounced(value, delay = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return v;
}

/** Debounced callback: the last call wins after `delay` ms. */
export function useDebouncedCallback(fn, delay = 400) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  return useCallback(
    (...args) => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => fnRef.current(...args), delay);
    },
    [delay],
  );
}

/** setInterval that pauses while the tab is hidden. `ms = null` stops it. */
export function useInterval(fn, ms) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!ms) return undefined;
    const tick = () => {
      if (!document.hidden) fnRef.current();
    };
    const id = setInterval(tick, ms);
    return () => clearInterval(id);
  }, [ms]);
}

export function useMediaQuery(query) {
  const get = () => (window.matchMedia ? window.matchMedia(query).matches : false);
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (!window.matchMedia) return undefined;
    const m = window.matchMedia(query);
    const on = () => setMatches(m.matches);
    on();
    m.addEventListener ? m.addEventListener("change", on) : m.addListener(on);
    return () => (m.removeEventListener ? m.removeEventListener("change", on) : m.removeListener(on));
  }, [query]);
  return matches;
}

/** Breakpoints (brief §6.3): phone < 600 · tablet 600–1023 · desktop ≥ 1024 · wide ≥ 1280. */
export const BREAKPOINTS = { phone: "(max-width: 599px)", tablet: "(min-width: 600px) and (max-width: 1023px)", desktop: "(min-width: 1024px)", wide: "(min-width: 1280px)" };
/** true on phones (bottom tab bar, bottom sheets). */
export const useIsMobile = () => useMediaQuery(BREAKPOINTS.phone);

/** Re-render every `ms` so relative times ("5 мин назад") stay fresh. */
export function useNow(ms = 30000) {
  const [now, setNow] = useState(() => new Date());
  useInterval(() => setNow(new Date()), ms);
  return now;
}

/** useState mirrored to localStorage (per-browser conveniences only). */
export function useLocalState(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : JSON.parse(raw);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* ignore */
    }
  }, [key, value]);
  return [value, setValue];
}

/** Close on Escape / outside click helpers for popovers. */
export function useOutside(ref, onOutside, active = true) {
  useEffect(() => {
    if (!active) return undefined;
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onOutside(e);
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [active]);
}
