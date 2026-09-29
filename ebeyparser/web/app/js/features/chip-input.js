// Token input for word lists (exclude / required words): Enter or comma adds, paste splits,
// Backspace on empty removes the last chip, optional suggestion chips below.
import { html, cx, useState, useRef } from "../lib/html.js";
import { Icon } from "../ui/index.js";

export function ChipInput({ value = [], onChange, placeholder = "Добавь слово и нажми Enter", suggestions = [], id, invalid, label }) {
  const [text, setText] = useState("");
  const input = useRef(null);
  const add = (raw) => {
    const words = String(raw)
      .split(/[,\n;]+/)
      .map((w) => w.trim())
      .filter(Boolean);
    if (!words.length) return;
    const have = new Set(value.map((v) => v.toLowerCase()));
    const next = [...value];
    for (const w of words) if (!have.has(w.toLowerCase())) (next.push(w), have.add(w.toLowerCase()));
    onChange(next);
    setText("");
  };
  const remove = (w) => onChange(value.filter((v) => v !== w));
  const free = suggestions.filter((s) => !value.some((v) => v.toLowerCase() === s.toLowerCase()));
  return html`<div class="chipin-wrap">
    <div class=${cx("chipin", invalid && "is-invalid")} onClick=${() => input.current && input.current.focus()}>
      ${value.map(
        (w) => html`<span class="chipin__chip">
          ${w}
          <button type="button" aria-label=${`Убрать «${w}»`} onClick=${(e) => (e.stopPropagation(), remove(w))}><${Icon} name="x" size=${12} /></button>
        </span>`,
      )}
      <input
        ref=${input}
        id=${id}
        value=${text}
        aria-label=${label}
        placeholder=${value.length ? "" : placeholder}
        onInput=${(e) => {
          const v = e.currentTarget.value;
          if (/[,;]/.test(v)) add(v);
          else setText(v);
        }}
        onKeyDown=${(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add(text);
          } else if (e.key === "Backspace" && !text && value.length) remove(value[value.length - 1]);
        }}
        onPaste=${(e) => {
          const t = (e.clipboardData || window.clipboardData).getData("text");
          if (/[,\n;]/.test(t)) {
            e.preventDefault();
            add(t);
          }
        }}
        onBlur=${() => text.trim() && add(text)}
      />
    </div>
    ${free.length > 0 &&
    html`<div class="chipin__sugg">
      ${free.map((s) => html`<button type="button" class="chipin__add" onClick=${() => add(s)}><${Icon} name="plus" size=${12} />${s}</button>`)}
    </div>`}
  </div>`;
}
