// «Где искать»: city autocomplete (GET /locations) + radius slider with a growing disc (brief §4.1.2).
// Used by onboarding and Settings → Поиск и регион.
import { html, useState, useEffect } from "../lib/html.js";
import { api } from "../lib/api.js";
import { useDebounced } from "../lib/hooks.js";
import { Autocomplete, Slider } from "../ui/index.js";

export const RADIUS_STEPS = [0, 5, 10, 20, 30, 50, 100, 150, 200];

export function radiusText(km) {
  const v = Number(km) || 0;
  if (v <= 0) return "только город";
  return `${v} км`;
}

/** Static heuristic caption under the radius disc. */
export function radiusCaption(km, city = "") {
  const v = Number(km) || 0;
  const berlin = /berlin/i.test(city);
  if (v <= 0) return berlin ? "Только объявления из самого Берлина" : "Только объявления из самого города";
  if (v <= 5) return "Твой район — можно дойти пешком";
  if (v <= 10) return "≈ до 20 минут на велосипеде";
  if (v <= 20) return berlin ? "≈ до 30 минут на S-Bahn" : "Город и ближайшие пригороды";
  if (v <= 30) return berlin ? "Примерно весь Берлин · ≈ до 40 минут на S-Bahn" : "Весь город с окрестностями";
  if (v <= 50) return "≈ до часа на машине или региональном поезде";
  if (v <= 100) return "Соседние города — ехать около 1–1,5 часа";
  return "Готов ехать далеко — находок больше всего";
}

/**
 * City / postal-code field. value = text; onChange({ location, label, confirmed }).
 * `confirmed` becomes true when a suggestion is chosen (or the text equals one).
 */
export function LocationPicker({ value, label, onChange, invalid, autoFocus, placeholder = "Berlin или 10115" }) {
  // `value` is what Kleinanzeigen gets (a city or a postal code); `label` is what the person sees
  const shown = label || value || "";
  const [text, setText] = useState(shown);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [picked, setPicked] = useState(Boolean(value)); // the text is a place from the list
  const q = useDebounced(text, 250);
  useEffect(() => setText(shown), [shown]);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .get("/locations", { params: { q, limit: 7 } })
      .then((res) => {
        if (!alive) return;
        // a bare "plz" row without a town is just the typed number echoed back: not a real place (P2-7)
        const list = ((res && res.items) || []).filter((p) => p.kind !== "plz" || p.state || p.parent);
        setItems(list);
        const typed = String(q).trim().toLowerCase();
        const exact = list.find((p) => p.value.toLowerCase() === typed || p.name.toLowerCase() === typed || displayName(p).toLowerCase() === typed);
        if (exact && q === text) {
          setPicked(true);
          onChange({ location: exact.value, label: displayName(exact), confirmed: true });
        }
      })
      .catch(() => alive && setItems([]))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);
  const options = items.map((p) => ({
    value: p.id,
    label: p.kind === "plz" ? p.label : p.name,
    hint: p.kind === "plz" ? "" : [p.kind === "district" ? `район · ${p.state || ""}` : p.state, p.plz].filter(Boolean).join(" · "),
    icon: p.kind === "district" ? "map" : p.kind === "plz" ? "hash" : "map-pin",
    place: p,
  }));
  return html`<${Autocomplete}
    value=${text}
    invalid=${invalid}
    autoFocus=${autoFocus}
    placeholder=${placeholder}
    icon="map-pin"
    loading=${loading && text.length > 0}
    options=${options}
    emptyText=${picked
      ? null // the chosen place itself (e.g. «Neukölln, Berlin»): nothing to complain about
      : /^\d+$/.test(text.trim()) ? (text.trim().length === 5 ? "Не нашёл такой индекс — проверь цифры" : "Почтовый индекс — это 5 цифр") : "Не нашёл такой город — попробуй почтовый индекс"}
    onInput=${(t) => {
      setText(t);
      setPicked(false);
      // confirmed only when the list knows the place (see the effect above)
      onChange({ location: t, label: t, confirmed: false });
    }}
    onSelect=${(o) => {
      const p = o.place;
      setPicked(true);
      setText(displayName(p));
      onChange({ location: p.value, label: displayName(p), confirmed: true });
    }}
  />`;
}

/** "Berlin", "Neukölln, Berlin" (district), "10115" (bare postal code). */
function displayName(p) {
  if (p.kind === "plz") return p.value;
  if (p.kind === "district") return p.label || p.name;
  return p.name;
}

/** Radius slider with discrete Kleinanzeigen stops and a floating value. */
export function RadiusSlider({ value, onChange, onCommit, steps = RADIUS_STEPS }) {
  return html`<${Slider}
    value=${value}
    steps=${steps}
    tone="green"
    bubble="always"
    label="Радиус поиска"
    format=${(v, short) => (short ? (v === 0 ? "город" : String(v)) : radiusText(v))}
    onChange=${onChange}
    onCommit=${onCommit}
  />`;
}

/** Simple offline "map": concentric discs that grow with the radius. */
export function RadiusDisc({ km = 30, city = "" }) {
  const max = 200;
  const v = Math.max(0, Math.min(max, Number(km) || 0));
  // perceptual scale: sqrt so small radii are still visible
  const r = 18 + Math.sqrt(v / max) * 92;
  return html`<figure class="radius-disc" aria-hidden="true">
    <svg viewBox="0 0 240 240" width="240" height="240">
      <circle cx="120" cy="120" r="112" class="radius-disc__ring" />
      <circle cx="120" cy="120" r="74" class="radius-disc__ring" />
      <circle cx="120" cy="120" r="38" class="radius-disc__ring" />
      <circle cx="120" cy="120" r=${r} class="radius-disc__area" />
      <circle cx="120" cy="120" r=${r} class="radius-disc__edge" />
      <g class="radius-disc__pin" transform="translate(120 120)">
        <circle r="16" class="radius-disc__pin-halo" />
        <circle r="7" class="radius-disc__pin-dot" />
      </g>
    </svg>
    <figcaption>
      <b class="num">${radiusText(v)}</b>
      <span>${radiusCaption(v, city)}</span>
    </figcaption>
  </figure>`;
}
