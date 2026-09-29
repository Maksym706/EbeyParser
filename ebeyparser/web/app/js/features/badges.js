// Nav badges owned by Frontend-B screens: deals = «Написал» older than 24 h, health = status dot.
import { appStore } from "../lib/store.js";

export function setBadge(key, value) {
  const cur = appStore.get().badges || {};
  if (cur[key] === value) return;
  appStore.set({ badges: { ...cur, [key]: value } });
}
