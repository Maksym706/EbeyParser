// «Что искать»: Kleinanzeigen category cards (GET /categories) + load meter (GET /setup/estimate).
import { html, cx, useEffect, useState } from "../lib/html.js";
import { api } from "../lib/api.js";
import { number, count, everyLabel } from "../lib/format.js";
import { useDebounced } from "../lib/hooks.js";
import { Icon, Meter, Skeleton } from "../ui/index.js";

/** Loads categories for a place; `live()` asks kleinanzeigen.de for real counts. */
export function useCategories(location, radius) {
  const [state, setState] = useState({ items: [], loading: true, live: false, error: null, source: null, refreshing: false });
  const load = async (live = false) => {
    setState((s) => ({ ...s, loading: !s.items.length, refreshing: live }));
    try {
      const res = await api.get("/categories", { params: { location, radius_km: radius, live: live ? 1 : undefined }, timeout: live ? 45000 : 15000 });
      setState({ items: res.items || [], loading: false, live: Boolean(res.live), error: res.error_ru || null, source: res.source, refreshing: false });
    } catch (e) {
      setState((s) => ({ ...s, loading: false, refreshing: false, error: e.message }));
    }
  };
  useEffect(() => {
    load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location, radius]);
  return { ...state, refresh: () => load(true) };
}

export function CategoryGrid({ items, selected, onToggle, loading, counting }) {
  if (loading) {
    return html`<div class="cat-grid">${Array.from({ length: 8 }, (_, i) => html`<div class="cat-card cat-card--skeleton" key=${i}><${Skeleton} w=${40} h=${40} radius="12px" /><${Skeleton} w="70%" /><${Skeleton} w="50%" h=${12} /></div>`)}</div>`;
  }
  return html`<div class="cat-grid" role="group" aria-label="Категории">
    ${items.map((c) => {
      const on = selected.includes(c.id);
      return html`<button
        type="button"
        key=${c.id}
        class=${cx("cat-card", on && "is-selected")}
        aria-pressed=${on}
        onClick=${() => onToggle(c.id, c)}
      >
        <span class="cat-card__icon"><${Icon} name=${c.icon || "tag"} size=${22} stroke=${1.75} /></span>
        <span class="cat-card__check"><${Icon} name="check" size=${12} stroke=${3} /></span>
        <span class="cat-card__name">${c.name_de}</span>
        <span class="cat-card__desc">${c.name_ru}</span>
        <span class="cat-card__foot">
          ${c.recommended && html`<span class="cat-card__pill">рекомендуем</span>`}
          ${counting ? html`<${Skeleton} w=${64} h=${12} />` : c.count != null && html`<span class="cat-card__count num">${number(c.count)} объявл.</span>`}
        </span>
      </button>`;
    })}
  </div>`;
}

/** «Нагрузка на Kleinanzeigen» meter for N category scans + M keyword searches (§4.1.3). */
export function LoadMeter({ categories = 0, keywords = 0, interval = null, onEstimate }) {
  const [est, setEst] = useState(null);
  const key = useDebounced(`${categories}|${keywords}|${interval}`, 200);
  useEffect(() => {
    let alive = true;
    if (!categories && !keywords) {
      setEst(null);
      return undefined;
    }
    api
      .get("/setup/estimate", { params: { categories, keywords, interval: interval || undefined } })
      .then((e) => {
        if (!alive) return;
        setEst(e);
        onEstimate && onEstimate(e);
      })
      .catch(() => alive && setEst(null));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (!est) return null;
  const tone = est.level === "danger" ? "red" : est.level === "warn" || categories > 8 ? "amber" : "green";
  const every = everyLabel(est.interval_minutes);
  const hint =
    tone === "green"
      ? `Проверка ${every} — безопасно`
      : `Много категорий: проверять буду ${every}, чтобы не заблокировали`;
  return html`<${Meter}
    label="Нагрузка на Kleinanzeigen"
    value=${est.pages_per_hour}
    max=${est.cap_per_hour || 150}
    marker=${0.4}
    tone=${tone}
    valueText=${`~${number(est.pages_per_hour)} из ${number(est.cap_per_hour)} стр. в час`}
    hint=${hint}
  />`;
}

export function selectedSummary(ids, items) {
  const names = ids.map((id) => (items.find((c) => c.id === id) || {}).name_de).filter(Boolean);
  return names.length ? `${count(names.length, "категория", "категории", "категорий")}: ${names.slice(0, 3).join(", ")}${names.length > 3 ? "…" : ""}` : "";
}

