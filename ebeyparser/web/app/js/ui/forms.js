// Form controls: Field, Input, SecretInput, Textarea, Select, Toggle, Checkbox, Slider,
// Segmented, NumberStepper, Autocomplete, TestResult, SaveState.
// All controls are controlled components: pass `value` + `onChange(newValue)`.
import { html, cx, useState, useRef, useEffect, useId } from "../lib/html.js";
import { Icon } from "./icons.js";
import { Spinner, Details } from "./core.js";

/** Label + control + help/error text. Children get the generated id via render prop or `id`. */
export function Field({ label, help, error, optional, id, inline = false, class: cls = "", children, aside }) {
  const auto = useId();
  const fid = id || auto;
  const content = typeof children === "function" ? children(fid) : children;
  return html`<div class=${cx("field", inline && "field--inline", error && "has-error", cls)}>
    ${(label || aside) &&
    html`<div class="field__top">
      ${label && html`<label class="field__label" for=${fid}>${label}${optional && html`<span class="field__optional">необязательно</span>`}</label>`}
      ${aside && html`<span class="field__aside">${aside}</span>`}
    </div>`}
    <div class="field__control">${content}</div>
    ${error
      ? html`<p class="field__error" role="alert"><${Icon} name="circle-alert" size=${14} />${error}</p>`
      : help && html`<p class="field__help">${help}</p>`}
  </div>`;
}

/** Text input. `prefix`/`suffix` are text or icons inside the box ("€", "км"). */
export function Input({
  value,
  onChange,
  onInput,
  icon,
  prefix,
  suffix,
  type = "text",
  size = "md",
  invalid,
  class: cls = "",
  inputRef,
  trailing,
  ...rest
}) {
  return html`<div class=${cx("input", `input--${size}`, invalid && "is-invalid", rest.disabled && "is-disabled", cls)}>
    ${icon && html`<span class="input__icon"><${Icon} name=${icon} size=${18} /></span>`}
    ${prefix && html`<span class="input__affix">${prefix}</span>`}
    <input
      ref=${inputRef}
      type=${type}
      value=${value ?? ""}
      aria-invalid=${invalid || undefined}
      onInput=${(e) => {
        onInput && onInput(e);
        onChange && onChange(e.currentTarget.value);
      }}
      ...${rest}
    />
    ${suffix && html`<span class="input__affix">${suffix}</span>`}
    ${trailing}
  </div>`;
}

const fmtNum = (v) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(v);

/**
 * Before saving: if a number field inside `root` is out of range, reveal the messages, focus the
 * first bad field and return true (= don't save).
 */
export function showFieldErrors(root = document) {
  const bad = root.querySelector("input[data-range-error]");
  if (!bad) return false;
  window.dispatchEvent(new CustomEvent("ebp:show-field-errors"));
  bad.focus({ preventScroll: true });
  bad.scrollIntoView({ block: "center" });
  return true;
}

/**
 * "Не меньше 0 €" / "Можно от 0 до 90 %" when `v` is outside [min, max], else null.
 * Forms use it for their own save-time checks; NumberInput shows it under the field.
 */
export function rangeError(v, min, max, suffix) {
  if (v == null || typeof v !== "number" || Number.isNaN(v)) return null;
  const u = typeof suffix === "string" && suffix ? ` ${suffix}` : "";
  const low = min != null && v < min;
  const high = max != null && v > max;
  if (!low && !high) return null;
  if (min != null && max != null && max - min <= 2000) return `Можно от ${fmtNum(min)} до ${fmtNum(max)}${u}`;
  return low ? `Не меньше ${fmtNum(min)}${u}` : `Не больше ${fmtNum(max)}${u}`;
}

/**
 * Number input returning numbers (or null when empty). Out-of-range values are never clamped
 * silently (brief §2.2): the field turns red with «Можно от 0 до 90 %» under it after leaving it,
 * and the value is passed on as typed so the form can refuse to save. Pass `invalid` when the
 * parent shows its own message for this field.
 */
export function NumberInput({ value, onChange, min, max, step = 1, invalid, onBlur, ...rest }) {
  const [text, setText] = useState(value ?? "");
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (Number(String(text).replace(/\s/g, "").replace(",", ".")) !== value) setText(value ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  // a form that refuses to save asks every field to show its message: showFieldErrors()
  useEffect(() => {
    const on = () => setTouched(true);
    window.addEventListener("ebp:show-field-errors", on);
    return () => window.removeEventListener("ebp:show-field-errors", on);
  }, []);
  const out = rangeError(value, min, max, rest.suffix);
  const bad = Boolean(invalid) || (touched && Boolean(out));
  return html`<span class="numfield">
    <${Input}
      ...${rest}
      inputmode="decimal"
      value=${text}
      invalid=${bad}
      data-range-error=${out ? "1" : undefined}
      onChange=${(raw) => {
        setText(raw);
        const cleaned = raw.replace(/\s/g, "").replace(",", ".");
        if (cleaned === "") return onChange(null);
        const n = Number(cleaned);
        if (!Number.isNaN(n)) onChange(n);
      }}
      onBlur=${(e) => {
        setTouched(true);
        onBlur && onBlur(e);
      }}
    />
    ${touched && out && !invalid && html`<span class="numfield__error" role="alert"><${Icon} name="circle-alert" size=${14} />${out}</span>`}
  </span>`;
}

/** Password-style input with a show/hide eye. Shows "•••• сохранён" when a secret already exists. */
export function SecretInput({ value, onChange, saved = false, placeholder, ...rest }) {
  const [show, setShow] = useState(false);
  return html`<${Input}
    ...${rest}
    type=${show ? "text" : "password"}
    value=${value}
    onChange=${onChange}
    autocomplete="off"
    spellcheck=${false}
    placeholder=${saved && !value ? "•••••••• сохранён — вставь новый, чтобы заменить" : placeholder}
    trailing=${html`<button type="button" class="input__btn" aria-label=${show ? "Скрыть" : "Показать"} onClick=${() => setShow(!show)}>
      <${Icon} name=${show ? "eye-off" : "eye"} size=${18} />
    </button>`}
  />`;
}

export function Textarea({ value, onChange, rows = 3, invalid, class: cls = "", ...rest }) {
  return html`<textarea
    class=${cx("textarea", invalid && "is-invalid", cls)}
    rows=${rows}
    value=${value ?? ""}
    onInput=${(e) => onChange && onChange(e.currentTarget.value)}
    ...${rest}
  ></textarea>`;
}

/** Native select, styled. options: [{ value, label, disabled }] or strings. */
export function Select({ value, onChange, options = [], placeholder, invalid, size = "md", class: cls = "", ...rest }) {
  const opts = options.map((o) => (typeof o === "object" ? o : { value: o, label: o }));
  return html`<div class=${cx("select", `select--${size}`, invalid && "is-invalid", cls)}>
    <select value=${value ?? ""} onChange=${(e) => onChange && onChange(e.currentTarget.value)} ...${rest}>
      ${placeholder && html`<option value="" disabled>${placeholder}</option>`}
      ${opts.map((o) => html`<option value=${o.value} disabled=${o.disabled}>${o.label}</option>`)}
    </select>
    <${Icon} name="chevrons-up-down" size=${16} class="select__chevron" />
  </div>`;
}

/** iOS-style switch. With `label` it renders a full clickable row. */
export function Toggle({ checked, onChange, label, description, disabled, size = "md", id }) {
  const auto = useId();
  const tid = id || auto;
  const sw = html`<button
    id=${tid}
    type="button"
    role="switch"
    aria-checked=${Boolean(checked)}
    class=${cx("switch", `switch--${size}`, checked && "is-on")}
    disabled=${disabled}
    onClick=${() => onChange && onChange(!checked)}
  >
    <span class="switch__thumb"></span>
  </button>`;
  if (!label) return sw;
  return html`<div class=${cx("toggle-row", disabled && "is-disabled")}>
    <label class="toggle-row__text" for=${tid}>
      <span class="toggle-row__label">${label}</span>
      ${description && html`<span class="toggle-row__desc">${description}</span>`}
    </label>
    ${sw}
  </div>`;
}

export function Checkbox({ checked, onChange, label, description, disabled }) {
  return html`<label class=${cx("checkbox", checked && "is-checked", disabled && "is-disabled")}>
    <input type="checkbox" checked=${checked} disabled=${disabled} onChange=${(e) => onChange && onChange(e.currentTarget.checked)} />
    <span class="checkbox__box"><${Icon} name="check" size=${14} stroke=${3} /></span>
    <span class="checkbox__text">
      <span>${label}</span>
      ${description && html`<small>${description}</small>`}
    </span>
  </label>`;
}

/**
 * Range slider (§6.8.4) with a value bubble above the thumb.
 *   <Slider value={30} steps={[0,5,10,20,30,50,100]} format={(v) => `${v} км`} onChange={set} bubble="always" />
 *   <Slider value={400} min={50} max={1500} step={10} format={money} onChange={set} onCommit={save} tone="green" />
 * `steps` snaps to those values (evenly spaced, with 4 px ticks). bubble: "drag" (default) | "always" | "none".
 * tone: ink (default) | green | amber | violet | blue. Keyboard: arrows step, PgUp/PgDn ×5 (native).
 */
export function Slider({ value, onChange, onCommit, min = 0, max = 100, step = 1, steps, format = (v) => v, marks, tone = "ink", label, disabled, bubble = "drag" }) {
  const discrete = Array.isArray(steps) && steps.length > 1;
  const idx = discrete ? Math.max(0, nearestIndex(steps, value)) : null;
  const rmin = discrete ? 0 : min;
  const rmax = discrete ? steps.length - 1 : max;
  const rval = discrete ? idx : Math.min(max, Math.max(min, Number(value) || min));
  const pct = rmax === rmin ? 0 : ((rval - rmin) / (rmax - rmin)) * 100;
  const toValue = (raw) => (discrete ? steps[Number(raw)] : Number(raw));
  const [dragging, setDragging] = useState(false);
  const showMarks = marks === false ? null : marks || (discrete ? steps.map((s) => ({ value: s, label: format(s, true) })) : null);
  return html`<div
    class=${cx("slider", `slider--${tone}`, dragging && "is-dragging", disabled && "is-disabled", bubble === "always" && "has-bubble")}
    style=${{ "--pos": `${pct}%`, "--pct": pct / 100 }}
  >
    <div class="slider__track-wrap">
      ${bubble !== "none" && html`<output class="slider__bubble" aria-hidden="true">${format(value)}</output>`}
      ${discrete &&
      html`<span class="slider__ticks" aria-hidden="true">
        ${steps.map((_, i) => html`<span class=${cx("slider__tick", i <= idx && "is-filled")} style=${{ left: `${(i / (steps.length - 1)) * 100}%` }}></span>`)}
      </span>`}
      <input
        type="range"
        class="slider__input"
        min=${rmin}
        max=${rmax}
        step=${discrete ? 1 : step}
        value=${rval}
        disabled=${disabled}
        aria-label=${label}
        aria-valuetext=${String(format(value))}
        onInput=${(e) => onChange && onChange(toValue(e.currentTarget.value))}
        onChange=${(e) => onCommit && onCommit(toValue(e.currentTarget.value))}
        onPointerDown=${() => setDragging(true)}
        onPointerUp=${() => setDragging(false)}
        onPointerCancel=${() => setDragging(false)}
        onBlur=${() => setDragging(false)}
      />
    </div>
    ${showMarks &&
    html`<div class="slider__marks" aria-hidden="true">
      ${showMarks.map((m, i) => {
        const pos = discrete ? (i / (steps.length - 1)) * 100 : ((m.value - min) / (max - min)) * 100;
        return html`<span class=${cx("slider__mark", m.value === value && "is-current")} style=${{ left: `${pos}%` }}>${m.label}</span>`;
      })}
    </div>`}
  </div>`;
}

function nearestIndex(list, v) {
  let best = 0;
  list.forEach((x, i) => {
    if (Math.abs(x - v) < Math.abs(list[best] - v)) best = i;
  });
  return best;
}

/**
 * Segmented control (one of a few). options: [{ value, label, icon, hint }].
 * Keyboard: arrows move the selection.
 */
export function Segmented({ value, onChange, options = [], size = "md", block = false, label, class: cls = "" }) {
  const index = Math.max(0, options.findIndex((o) => o.value === value));
  const onKey = (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = (index + (e.key === "ArrowRight" ? 1 : -1) + options.length) % options.length;
    onChange(options[next].value);
    const btns = e.currentTarget.querySelectorAll("button");
    btns[next] && btns[next].focus();
  };
  return html`<div
    class=${cx("segmented", `segmented--${size}`, block && "segmented--block", cls)}
    role="radiogroup"
    aria-label=${label}
    style=${{ "--count": options.length, "--index": index }}
    onKeyDown=${onKey}
  >
    <span class="segmented__indicator" aria-hidden="true"></span>
    ${options.map(
      (o) => html`<button
        type="button"
        role="radio"
        aria-checked=${o.value === value}
        tabindex=${o.value === value ? 0 : -1}
        class=${cx("segmented__item", o.value === value && "is-active")}
        onClick=${() => onChange(o.value)}
        title=${o.hint}
      >
        ${o.icon && html`<${Icon} name=${o.icon} size=${16} />`}<span>${o.label}</span>
      </button>`,
    )}
  </div>`;
}

/** − value + */
export function NumberStepper({ value, onChange, min = 0, max = 999, step = 1, format = (v) => v, label }) {
  const set = (v) => onChange(Math.min(max, Math.max(min, v)));
  return html`<div class="stepper" role="group" aria-label=${label}>
    <button type="button" class="stepper__btn" aria-label="Меньше" disabled=${value <= min} onClick=${() => set(value - step)}>
      <${Icon} name="minus" size=${18} />
    </button>
    <output class="stepper__value">${format(value)}</output>
    <button type="button" class="stepper__btn" aria-label="Больше" disabled=${value >= max} onClick=${() => set(value + step)}>
      <${Icon} name="plus" size=${18} />
    </button>
  </div>`;
}

/**
 * Text input with suggestions (combobox).
 *   <Autocomplete value={text} onInput={setText} options={[{ value, label, hint }]} onSelect={(o) => …} loading />
 */
export function Autocomplete({ value, onInput, options = [], onSelect, loading, placeholder, icon = "search", emptyText, invalid, autoFocus, ...rest }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrap = useRef(null);
  const listId = useId();
  useEffect(() => setActive(0), [options]);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => wrap.current && !wrap.current.contains(e.target) && setOpen(false);
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [open]);
  const choose = (o) => {
    onSelect && onSelect(o);
    setOpen(false);
  };
  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(options.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter" && open && options[active]) {
      e.preventDefault();
      choose(options[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };
  const showList = open && (options.length > 0 || (emptyText && value && !loading));
  return html`<div class="autocomplete" ref=${wrap}>
    <${Input}
      ...${rest}
      icon=${icon}
      value=${value}
      invalid=${invalid}
      placeholder=${placeholder}
      autocomplete="off"
      autoFocus=${autoFocus}
      role="combobox"
      aria-expanded=${showList}
      aria-controls=${listId}
      aria-autocomplete="list"
      onChange=${(v) => {
        onInput && onInput(v);
        setOpen(true);
      }}
      onFocus=${() => setOpen(true)}
      onKeyDown=${onKeyDown}
      trailing=${loading ? html`<span class="input__trail"><${Spinner} size=${16} /></span>` : null}
    />
    ${showList &&
    html`<ul class="autocomplete__list" id=${listId} role="listbox">
      ${options.length
        ? options.map(
            (o, i) => html`<li
              key=${o.value + ":" + i}
              role="option"
              aria-selected=${i === active}
              class=${cx("autocomplete__item", i === active && "is-active")}
              onPointerDown=${(e) => {
                e.preventDefault();
                choose(o);
              }}
              onPointerEnter=${() => setActive(i)}
            >
              <${Icon} name=${o.icon || "map-pin"} size=${16} />
              <span class="autocomplete__label">${o.label}</span>
              ${o.hint && html`<span class="autocomplete__hint">${o.hint}</span>`}
            </li>`,
          )
        : html`<li class="autocomplete__empty">${emptyText}</li>`}
    </ul>`}
  </div>`;
}

/**
 * Result line of an inline "Проверить" button.
 *   state: idle | loading | ok | fail | warn
 */
export function TestResult({ state = "idle", title, detail, details, children }) {
  if (state === "idle") return null;
  const icon = { loading: null, ok: "circle-check", fail: "circle-x", warn: "triangle-alert" }[state];
  return html`<div class=${cx("test-result", `test-result--${state}`)} role="status" aria-live="polite">
    <span class="test-result__icon">${state === "loading" ? html`<${Spinner} size=${16} />` : html`<${Icon} name=${icon} size=${18} />`}</span>
    <div class="test-result__body">
      <div class="test-result__title">${title}</div>
      ${detail && html`<div class="test-result__detail">${detail}</div>`}
      ${children}
      <${Details} text=${details} />
    </div>
  </div>`;
}

/** Tiny "Сохраняю… / Сохранено ✓ / Не сохранено" indicator for auto-saving forms. */
export function SaveState({ state }) {
  const map = {
    saving: { icon: null, text: "Сохраняю…" },
    saved: { icon: "check", text: "Сохранено" },
    error: { icon: "circle-alert", text: "Не сохранено" },
  };
  const m = map[state];
  if (!m) return html`<span class="save-state"></span>`;
  return html`<span class=${cx("save-state", `save-state--${state}`)} role="status">
    ${m.icon ? html`<${Icon} name=${m.icon} size=${14} stroke=${2.5} />` : html`<${Spinner} size=${12} />`}${m.text}
  </span>`;
}

/** Group of options rendered as selectable cards (radio). */
export function ChoiceCards({ value, onChange, options = [], columns = 3 }) {
  return html`<div class="choice-cards" role="radiogroup" style=${{ "--cols": columns }}>
    ${options.map(
      (o) => html`<button
        type="button"
        role="radio"
        aria-checked=${o.value === value}
        class=${cx("choice-card", o.value === value && "is-selected", o.tone && `choice-card--${o.tone}`)}
        onClick=${() => onChange(o.value)}
      >
        ${o.icon && html`<span class="choice-card__icon"><${Icon} name=${o.icon} size=${22} /></span>`}
        <span class="choice-card__title">${o.label}</span>
        ${o.description && html`<span class="choice-card__desc">${o.description}</span>`}
        <span class="choice-card__check"><${Icon} name="check" size=${14} stroke=${3} /></span>
      </button>`,
    )}
  </div>`;
}


/**
 * Token input for word lists (§6.8.2 chip input): Enter or comma adds, paste splits by comma/newline,
 * Backspace on an empty field removes the last token.
 *   <ChipInput value={["defekt","bastler"]} onChange={setWords} suggestions={["suche","tausch"]} />
 */
export function ChipInput({ value = [], onChange, placeholder = "Добавь слово и нажми Enter", suggestions = [], max = 50 }) {
  const [text, setText] = useState("");
  const inputRef = useRef(null);
  const add = (raw) => {
    const words = String(raw)
      .split(/[,\n;]+/)
      .map((w) => w.trim())
      .filter(Boolean);
    if (!words.length) return;
    const next = [...value];
    for (const w of words) if (!next.some((x) => x.toLowerCase() === w.toLowerCase()) && next.length < max) next.push(w);
    onChange(next);
    setText("");
  };
  const remove = (i) => onChange(value.filter((_, j) => j !== i));
  const left = suggestions.filter((s) => !value.some((v) => v.toLowerCase() === s.toLowerCase()));
  return html`<div>
    <div class="chip-input" onClick=${() => inputRef.current && inputRef.current.focus()}>
      ${value.map(
        (w, i) => html`<span class="chip-input__token" key=${w}>
          ${w}
          <button type="button" aria-label=${`Убрать «${w}»`} onClick=${(e) => {
            e.stopPropagation();
            remove(i);
          }}><${Icon} name="x" size=${12} stroke=${2.5} /></button>
        </span>`,
      )}
      <input
        ref=${inputRef}
        value=${text}
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
          } else if (e.key === "Backspace" && !text && value.length) {
            remove(value.length - 1);
          }
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
    ${left.length > 0 &&
    html`<div class="chip-input__suggest">
      ${left.map((sug) => html`<button type="button" key=${sug} onClick=${() => add(sug)}>+ ${sug}</button>`)}
    </div>`}
  </div>`;
}

/**
 * Settings row (§4.7): label + helper on the left, control on the right (stacked on phones).
 *   <SettingRow label="Не больше в час" help="Остальное придёт одним сообщением" error={errors.max}>
 *     <NumberInput … />
 *   </SettingRow>
 * `kind="switch"` keeps a toggle on the right even on phones; `wide` puts the control under the text.
 */
export function SettingRow({ label, help, error, tip, kind, wide = false, id, children }) {
  const auto = useId();
  const rid = id || auto;
  return html`<div class=${cx("setting-row", kind === "switch" && "setting-row--switch", wide && "setting-row--wide")}>
    <div class="setting-row__text">
      <label class="setting-row__label" for=${rid}>${label}${tip}</label>
      ${help && html`<div class="setting-row__help">${help}</div>`}
      ${error && html`<div class="setting-row__error" role="alert"><${Icon} name="circle-alert" size=${14} />${error}</div>`}
    </div>
    <div class="setting-row__control">${typeof children === "function" ? children(rid) : children}</div>
  </div>`;
}
