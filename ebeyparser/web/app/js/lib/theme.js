// Theme: "system" (follows the OS), "dark" or "light". Stored per browser.
// index.html applies it before the first paint; this module keeps it in sync afterwards.
import { createStore } from "./store.js";

const KEY = "ebp-theme";
const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

function readPref() {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "dark" || v === "light" || v === "system") return v;
  } catch {
    /* ignore */
  }
  return "system";
}

function resolve(pref) {
  if (pref === "dark" || pref === "light") return pref;
  return media && media.matches ? "light" : "dark";
}

export const themeStore = createStore({ pref: readPref(), theme: resolve(readPref()) });

function apply() {
  const { theme } = themeStore.get();
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#f6f7f9" : "#0b0d11");
}

export function setTheme(pref) {
  try {
    localStorage.setItem(KEY, pref);
  } catch {
    /* ignore */
  }
  themeStore.set({ pref, theme: resolve(pref) });
  apply();
}

/** Flip between light and dark (the top-bar button). */
export function toggleTheme() {
  const root = document.documentElement;
  root.classList.add("theme-switching");
  setTheme(themeStore.get().theme === "dark" ? "light" : "dark");
  setTimeout(() => root.classList.remove("theme-switching"), 350);
}

if (media) {
  const onChange = () => {
    if (themeStore.get().pref === "system") {
      themeStore.set({ theme: resolve("system") });
      apply();
    }
  };
  if (media.addEventListener) media.addEventListener("change", onChange);
  else if (media.addListener) media.addListener(onChange);
}
apply();
