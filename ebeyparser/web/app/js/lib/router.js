// Minimal history-API router. The server answers every non-API path with index.html,
// so deep links like /deal/123 or /settings/ai work after a reload.
//
//   navigate("/deal/42")                 // push
//   navigate("/settings", { replace: true })
//   const { path, params, query } = useRoute();
//   <a href="/searches">…</a>            // plain links are intercepted automatically
import { useStore, createStore } from "./store.js";

// Links written as "/#/deal/123" (hash style) are converted to "/deal/123".
if (window.location.hash.startsWith("#/")) {
  const h = window.location.hash.slice(1);
  history.replaceState(history.state, "", h);
}

export const routeStore = createStore(readLocation());

function readLocation() {
  const { pathname, search, hash } = window.location;
  return { path: normalize(pathname), query: Object.fromEntries(new URLSearchParams(search)), hash, state: history.state };
}

function normalize(path) {
  if (path.length > 1 && path.endsWith("/")) return path.slice(0, -1);
  return path || "/";
}

const guards = new Set();

/** Register a function that may veto navigation (return false), e.g. unsaved changes. */
export function addNavigationGuard(fn) {
  guards.add(fn);
  return () => guards.delete(fn);
}

export function navigate(to, { replace = false, state = null, scroll = true } = {}) {
  const url = new URL(to, window.location.origin);
  for (const g of guards) if (g(url) === false) return;
  const target = url.pathname + url.search + url.hash;
  const current = window.location.pathname + window.location.search + window.location.hash;
  if (target === current && !replace) return;
  const inApp = replace ? Boolean(history.state && history.state.__inApp) : true;
  history[replace ? "replaceState" : "pushState"]({ ...(state || {}), __inApp: inApp }, "", target);
  routeStore.replace(readLocation());
  if (scroll && !url.hash) window.scrollTo({ top: 0, behavior: "instant" in document.documentElement.style ? "instant" : "auto" });
}

/** Update query parameters of the current URL (null/"" removes a key). */
export function setQuery(patch, { replace = true } = {}) {
  const url = new URL(window.location.href);
  for (const [k, v] of Object.entries(patch)) {
    if (v === null || v === undefined || v === "") url.searchParams.delete(k);
    else url.searchParams.set(k, v);
  }
  navigate(url.pathname + url.search + url.hash, { replace, scroll: false });
}

export function back(fallback = "/") {
  if (history.state && history.state.__inApp) history.back();
  else navigate(fallback);
}

window.addEventListener("popstate", () => routeStore.replace(readLocation()));

// Intercept clicks on same-origin <a href> so the page does not reload.
document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download") || a.dataset.native !== undefined) return;
  const url = new URL(a.href, window.location.href);
  if (url.origin !== window.location.origin) return;
  if (/^\/(api|app|static|classic)(\/|$)/.test(url.pathname)) return;
  e.preventDefault();
  navigate(url.pathname + url.search + url.hash);
});

/**
 * Match `path` against a pattern like "/deal/:id" or "/settings/:section?".
 * Returns params or null.
 */
export function matchPath(pattern, path) {
  const p = pattern.split("/").filter(Boolean);
  const s = path.split("/").filter(Boolean);
  const params = {};
  for (let i = 0; i < Math.max(p.length, s.length); i++) {
    const seg = p[i];
    if (seg === undefined) return null;
    if (seg === "*") {
      params.rest = s.slice(i).map(decodeURIComponent).join("/");
      return params;
    }
    if (seg.startsWith(":")) {
      const optional = seg.endsWith("?");
      const name = seg.slice(1, optional ? -1 : undefined);
      if (s[i] === undefined) {
        if (optional) continue;
        return null;
      }
      params[name] = decodeURIComponent(s[i]);
    } else if (seg !== s[i]) {
      return null;
    }
  }
  return params;
}

export function useRoute() {
  return useStore(routeStore);
}
