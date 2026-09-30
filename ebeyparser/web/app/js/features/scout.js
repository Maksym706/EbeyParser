// AI scout («Разведчик», docs/design/AI_SCOUT.md §12): the «Нашла нейросеть» badge and the
// «🔥 Супер-находка» mark of a deal, the scout status vocabulary, «Проверить разведчика»
// (POST /ai/scout/test) with the four sample readings, and «Подобрать модель под железо»
// (GET /ai/recommend). Used by the feed / deal view, Settings → Нейросеть and Состояние.
import { html, cx, useState, useEffect } from "../lib/html.js";
import { useDebounced } from "../lib/hooks.js";
import { api, humanize } from "../lib/api.js";
import { localizeText } from "../lib/format.js";
import { ICONS } from "../ui/icons.js";
import { Icon, Button, Tooltip, TestResult, CopyButton, Segmented, Skeleton, Slider, Tabs, toast } from "../ui/index.js";
import "./icons-extra.js";

// Lucide (ISC, lucide-static 1.48.0), additive
const EXTRA = {
  "telescope":
    '<path d="m10.065 12.493-6.18 1.318a.934.934 0 0 1-1.108-.702l-.537-2.15a1.07 1.07 0 0 1 .691-1.265l13.504-4.44" /><path d="m13.56 11.747 4.332-.924" /><path d="m16 21-3.105-6.21" /><path d="M16.485 5.94a2 2 0 0 1 1.455-2.425l1.09-.272a1 1 0 0 1 1.212.727l1.515 6.06a1 1 0 0 1-.727 1.213l-1.09.272a2 2 0 0 1-2.425-1.455z" /><path d="m6.158 8.633 1.114 4.456" /><path d="m8 21 3.105-6.21" /><circle cx="12" cy="13" r="2" />',
  "calendar-clock":
    '<path d="M16 14v2.2l1.6 1" /><path d="M16 2v3" /><path d="M21 7.338V5a2 2 0 00-2-2H5a2 2 0 00-2 2v14a2 2 0 002 2h2.338" /><path d="M3 9h5.859" /><path d="M8 2v3" /><circle cx="16" cy="16" r="6" />',
};
for (const [name, body] of Object.entries(EXTRA)) if (!(name in ICONS)) ICONS[name] = body;

/** Server texts may carry a Telegram-style emoji and German money; keep them calm. */
const clean = (t) =>
  localizeText(String(t || ""))
    .replace(/^[\s\u{1F50E}\u{1F525}]+/u, "")
    .replace(/(\d)\.(?=\d{3}(?!\d))/g, "$1 ")
    .trim();

// ------------------------------------------------------------------ deal marks
export const foundByScout = (deal) => Boolean(deal && deal.found_by === "ai_scout");
export const isSuper = (deal) => Boolean(deal && deal.tier === "super");

/**
 * «Нашла нейросеть» (blue = AI, brief §2.6) with the scout's one line as the explanation.
 * variant: "sig" (card / row signal line) | "tag" (deal view chips)
 */
export function FoundByBadge({ deal, variant = "sig" }) {
  if (!foundByScout(deal)) return null;
  const label = deal.found_by_label || "Нашла нейросеть";
  const reason = clean(deal.scout_reason);
  const badge =
    variant === "tag"
      ? html`<span class="tag tone-info scout-badge"><${Icon} name="telescope" size=${12} />${label}</span>`
      : html`<span class="sig sig--scout" aria-label=${reason ? `${label}: ${reason}` : label}><${Icon} name="telescope" size=${14} />${label}</span>`;
  return reason ? html`<${Tooltip} text=${reason}>${badge}<//>` : badge;
}

/** «🔥 Супер-находка» (the server's `tier_label`): a solid green mark on top of the card. */
export function SuperMark({ deal, class: cls = "" }) {
  if (!isSuper(deal)) return null;
  const label = String(deal.tier_label || "Супер-находка").replace(/^\s*\u{1F525}\s*/u, "");
  return html`<span class=${cx("super-mark", cls)}><${Icon} name="flame" size=${13} stroke=${2.4} />${label}</span>`;
}

/** Deal view: why the scout brought this ad back («старый ПК, внутри RTX 3070»). */
export function ScoutNote({ deal }) {
  if (!foundByScout(deal)) return null;
  const reason = clean(deal.scout_reason);
  return html`<div class="scout-note">
    <span class="scout-note__icon"><${Icon} name="telescope" size=${18} /></span>
    <div>
      <b>${deal.found_by_label || "Нашла нейросеть"}</b>${reason ? html`: ${reason}` : ""}
      <p class="scout-note__sub">Скрипт это объявление пропустил — нейросеть прочитала текст и узнала товар. Цена — по истории объявлений, не от нейросети.</p>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ status
/** GET /monitor|/health `scout.state` → tone + short label. */
export const SCOUT_STATE = {
  off: { tone: "neutral", label: "Выключен" },
  idle: { tone: "neutral", label: "Ждёт новых объявлений" },
  ok: { tone: "profit", label: "Успевает" },
  behind: { tone: "haggle", label: "Не успевает всё" },
  down: { tone: "danger", label: "Не отвечает" },
  too_small: { tone: "haggle", label: "Модель слишком маленькая" },
};

export function scoutState(s) {
  if (!s) return { tone: "neutral", label: "—" };
  return SCOUT_STATE[s.state] || { tone: "neutral", label: s.enabled ? "Работает" : "Выключен" };
}

/** «Успевает смотреть 90 из 120 новых объявлений в час · ≈ 5 с на объявление» */
export function ScoutLine({ scout, class: cls = "" }) {
  if (!scout) return null;
  const st = scoutState(scout);
  const text = humanize(clean(scout.text_ru)).message;
  return html`<div class=${cx("scout-line", `tone-${st.tone}`, cls)} role="status">
    <span class="sdot"></span>
    <div>
      <div class="scout-line__text">${text}</div>
      ${scout.speed_ru && html`<div class="scout-line__speed">${clean(scout.speed_ru)}</div>`}
    </div>
  </div>`;
}

// ------------------------------------------------------------------ «Проверить разведчика»
const KIND_RU = {
  pc: "компьютер",
  bundle: "комплект",
  lot: "лот",
  single: "товар",
  wanted: "ищет (не продаёт)",
  swap: "обмен",
  service: "услуга",
  part: "запчасть",
  acc: "аксессуар",
  defect: "неисправное",
  other: "другое",
};

/**
 * Runs POST /ai/scout/test with `body` ({provider, base_url, model}) and shows the result:
 * speed, the three quality checks, the four readings, and a fix when the model is too small.
 * onApply({model}) — take the suggested / recommended model into the form.
 */
export function ScoutTest({ body, onApply, onDone, disabled }) {
  const [state, setState] = useState({ phase: "idle", r: null, error: null });
  const [slow, setSlow] = useState(false);
  const run = async () => {
    setState({ phase: "loading", r: null, error: null });
    setSlow(false);
    const t = setTimeout(() => setSlow(true), 5000);
    try {
      const r = await api.post("/ai/scout/test", body || {}, { timeout: 240000 });
      setState({ phase: "done", r, error: null });
      onDone && onDone(r);
    } catch (e) {
      setState({ phase: "done", r: null, error: e });
    } finally {
      clearTimeout(t);
    }
  };
  const { phase, r, error } = state;
  let result = null;
  if (phase === "loading")
    result = html`<${TestResult} state="loading" title=${slow ? "Разведчик читает 4 объявления-примера… на слабом сервере это до пары минут" : "Проверяю разведчика…"} />`;
  else if (error) result = html`<${TestResult} state="fail" title=${error.message} details=${error.details} />`;
  else if (r) {
    const msg = humanize(clean(r.message_ru || r.error_ru));
    const st = r.ok ? "ok" : r.answered ? "warn" : "fail";
    result = html`<${TestResult} state=${st} title=${msg.message} details=${msg.details}>
      ${r.answered > 0 &&
      html`<div class="scout-checks">
        ${[
          [r.hidden_gpu, "нашла видеокарту в старом ПК"],
          [r.typo_fixed, "поняла опечатку"],
          [r.wanted_seen, "узнала «ищу», а не «продаю»"],
        ].map(([ok, text]) => html`<span class=${cx("tag", ok ? "tone-profit" : "tone-haggle")}><${Icon} name=${ok ? "check" : "x"} size=${12} stroke=${2.5} />${text}</span>`)}
        ${r.sec_per_ad != null && html`<span class="tag"><${Icon} name="gauge" size=${12} />≈ ${String(r.sec_per_ad).replace(".", ",")} с на объявление</span>`}
      </div>`}
      ${(r.items || []).length > 0 && html`<${Readings} items=${r.items} />`}
      ${(r.too_small || !r.ok) && (r.suggested_model || r.recommended) && html`<${ModelFix} r=${r} onApply=${onApply} />`}
      ${r.ok && onApply && html`<div class="scout-apply"><${Button} size="sm" variant="primary" icon="check" onClick=${() => onApply({ model: r.model, provider: r.provider, base_url: r.base_url, enable: true })}>Включить с этой моделью<//></div>`}
    <//>`;
  }
  return html`<div class="scout-test">
    <${Button} icon="play" variant="secondary" loading=${phase === "loading"} disabled=${disabled} onClick=${run}>Проверить разведчика<//>
    ${result}
  </div>`;
}

function Readings({ items }) {
  return html`<details class="scout-readings">
    <summary><${Icon} name="chevron-right" size=${14} />Что нейросеть прочитала в примерах</summary>
    <ul>
      ${items.map(
        (it) => html`<li>
          <span class="scout-readings__title">${it.title}</span>
          <span class="scout-readings__read">
            ${KIND_RU[it.kind] || it.kind}${it.product ? html` · <b>${it.product}</b>` : ""}${(it.contents || []).length ? ` · внутри: ${it.contents.join(", ")}` : ""}
          </span>
          ${it.reason && html`<span class="scout-readings__why">${clean(it.reason)}</span>`}
        </li>`,
      )}
    </ul>
  </details>`;
}

/** Too small / reads badly: the model already on the server, or the one to download. */
function ModelFix({ r, onApply }) {
  const rec = r.recommended;
  const provider = r.provider === "ollama" ? "ollama" : "lmstudio";
  const id = rec ? rec[provider] || rec.lmstudio : null;
  return html`<div class="scout-fix">
    ${r.suggested_model &&
    onApply &&
    html`<${Button} size="sm" variant="tinted" tone="blue" icon="check" onClick=${() => onApply({ model: r.suggested_model })}>Взять ${r.suggested_model}<//>`}
    ${rec &&
    id &&
    html`<div class="scout-fix__rec">
      <span>Рекомендую: <b>${rec.name}</b> · <code class="mono">${id}</code></span>
      <${CopyButton} text=${id} label="Скопировать название" done="Скопировано" />
      ${onApply && html`<${Button} size="sm" variant="ghost" onClick=${() => onApply({ model: id })}>Вписать в поле<//>`}
    </div>`}
  </div>`;
}

// ------------------------------------------------------------------ «Какую модель поставить»
const ROLE_OPTIONS = [
  { value: "server", label: "Сервер 24/7", icon: "server" },
  { value: "pc", label: "Игровой ПК", icon: "gamepad-2" },
];
const RUNTIMES = [
  { value: "lmstudio", label: "LM Studio" },
  { value: "ollama", label: "Ollama" },
  { value: "llamacpp", label: "llama.cpp" },
];
const TASK_ORDER = ["triage", "vision", "embeddings", "second_opinion"];
const RAM_STEPS = [2, 4, 8, 16, 32, 64, 128];
const VRAM_STEPS = [0, 4, 6, 8, 12, 16, 24, 48];
/** Where «Использовать» puts an installed model (PATCH /settings). Embeddings have no setting yet. */
const USE_PATH = { triage: "ai.scout", vision: "ai", second_opinion: "ai.second_opinion" };
const USE_DONE = {
  triage: (m) => `Разведчик будет читать объявления моделью ${m}`,
  vision: (m) => `Фото будет проверять модель ${m}`,
  second_opinion: (m) => `Второе мнение даст модель ${m}`,
};

function nestPatch(path, value) {
  return path.split(".").reduceRight((acc, key) => ({ [key]: acc }), value);
}

/** PATCH /settings with an installed model for a task; returns the new settings or null. */
export async function applyInstalledModel(task, match) {
  const path = USE_PATH[task];
  if (!path || !match) return null;
  try {
    const res = await api.patch("/settings", nestPatch(path, { provider: match.provider || "openai", base_url: match.base_url, model: match.model, enabled: true }));
    toast.success(USE_DONE[task](match.model), { message: match.server ? `Сервер: ${match.server}` : "" });
    return res;
  } catch (e) {
    toast.error(e);
    return null;
  }
}

function hostLine(host) {
  if (!host || !host.cores) return "";
  const cores = `${host.cores} ${host.cores === 1 ? "ядро" : host.cores < 5 ? "ядра" : "ядер"}`;
  const gpu = host.gpu ? `, ${host.gpu.name || "видеокарта"}${host.gpu.vram_gb ? ` ${Math.round(host.gpu.vram_gb)} ГБ` : ""}` : ", без видеокарты";
  return `${cores}, ${Math.round(host.ram_gb || 0)} ГБ памяти${gpu}`;
}

/**
 * «Выбери своё железо → какую модель поставить» (GET /ai/recommend, docs/design/AI_MODELS.md):
 * this machine by default, a server / gaming-PC switch, manual RAM / VRAM, one card per task with
 * the id per runtime (copy), where to get it, «уже установлено» + «Использовать».
 *   onUsed(task, settings) — after «Использовать» saved the settings (Settings reloads its form)
 */
export function ModelAdvice({ onUsed, initialRole = "server" }) {
  const [role, setRole] = useState(initialRole);
  const [manual, setManual] = useState(null); // {ram, vram} | null = this machine
  const [runtime, setRuntime] = useState("lmstudio");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const q = useDebounced(JSON.stringify({ role, manual }), 250);
  useEffect(() => {
    let alive = true;
    setError(null);
    const params = { role };
    if (manual) Object.assign(params, { ram_gb: manual.ram, vram_gb: manual.vram });
    api.get("/ai/recommend", { params, timeout: 30000 }).then(
      (d) => {
        if (!alive) return;
        setData(d);
        if (!manual && d && d.installed_match && (d.installed_match.servers || []).some((s) => s.provider === "ollama") && !(d.installed_match.servers || []).some((s) => s.provider === "openai"))
          setRuntime("ollama");
      },
      (e) => alive && setError(e),
    );
    return () => {
      alive = false;
    };
  }, [q]);
  const host = data && data.host;
  const input = (data && data.input) || {};
  const picks = data ? TASK_ORDER.map((k) => data.picks && data.picks[k]).filter(Boolean) : [];
  const installed = (data && data.installed_match) || null;
  const use = async (task) => {
    setBusy(task);
    const res = await applyInstalledModel(task, installed && installed[task]);
    setBusy(null);
    if (res && onUsed) onUsed(task, res);
  };
  const cur = manual || { ram: nearest(RAM_STEPS, input.ram_gb || 8), vram: nearest(VRAM_STEPS, input.vram_gb || 0) };
  return html`<div class="advice">
    <div class="advice__top">
      <${Segmented} size="sm" label="Для какого компьютера" value=${role} onChange=${setRole} options=${ROLE_OPTIONS} />
      ${host && host.cores && html`<span class="advice__host"><${Icon} name="cpu" size=${14} />${manual ? "Указано вручную" : `Этот компьютер: ${hostLine(host)}`}</span>`}
    </div>
    <details class="advice__manual" open=${Boolean(manual)}>
      <summary><${Icon} name="chevron-right" size=${14} />Указать память вручную</summary>
      <div class="advice__sliders">
        <div class="advice__slider">
          <span class="advice__slabel">Оперативная память</span>
          <${Slider} value=${cur.ram} steps=${RAM_STEPS} format=${(v) => `${v} ГБ`} label="Оперативная память" onChange=${(v) => setManual({ ...cur, ram: v })} />
        </div>
        <div class="advice__slider">
          <span class="advice__slabel">Память видеокарты</span>
          <${Slider} value=${cur.vram} steps=${VRAM_STEPS} format=${(v) => (v ? `${v} ГБ` : "нет")} label="Память видеокарты" onChange=${(v) => setManual({ ...cur, vram: v })} />
        </div>
        ${manual && html`<button type="button" class="linkish" onClick=${() => setManual(null)}><${Icon} name="rotate-ccw" size=${14} />Как у этого компьютера</button>`}
      </div>
    </details>
    ${error && html`<${TestResult} state="fail" title=${error.message} details=${error.details} />`}
    ${!data && !error && html`<div class="advice__sk"><${Skeleton} h=${14} w="60%" /><${Skeleton} h=${120} radius="var(--r-md)" /></div>`}
    ${data &&
    html`
      <div class="advice__lead">
        ${data.summary_ru && html`<b>${clean(data.summary_ru)}</b>`}
        <span class="advice__tier">${clean(data.tier_label_ru)}</span>
      </div>
      <${Tabs} variant="pills" label="Программа для нейросети" value=${runtime} onChange=${setRuntime} items=${RUNTIMES} />
      <div class="advice__picks">
        ${picks.map((pk) => {
          const id = (pk.ids && pk.ids[runtime]) || "";
          const inst = installed && installed[pk.task];
          return html`<article class=${cx("advice__pick", inst && "is-installed")} key=${pk.task}>
            <span class="overline">${pk.task_ru}</span>
            <div class="advice__name"><b>${pk.name}</b>${pk.label_ru && html`<span class="tag">${pk.label_ru}</span>`}</div>
            ${pk.desc_ru && html`<p class="advice__desc">${clean(pk.desc_ru)}</p>`}
            ${pk.speed_ru && html`<p class="advice__speed"><${Icon} name="gauge" size=${13} />${clean(pk.speed_ru)}</p>`}
            ${id
              ? html`<div class="advice__id">
                  <code class="mono">${id}</code>
                  <${CopyButton} text=${id} label="Скопировать" done="Скопировано" variant="ghost" />
                </div>`
              : html`<p class="advice__get muted">Для ${RUNTIMES.find((r) => r.value === runtime).label} этой модели нет — выбери другую программу</p>`}
            ${id && pk.get_ru && pk.get_ru[runtime] && html`<p class="advice__get">${clean(pk.get_ru[runtime])}</p>`}
            ${inst &&
            html`<div class="advice__installed">
              <span class="tag tone-profit"><${Icon} name="circle-check" size=${12} />уже установлено${inst.server ? ` в ${inst.server}` : ""}: ${inst.model}</span>
              ${USE_PATH[pk.task] && html`<${Button} size="sm" variant="primary" loading=${busy === pk.task} disabled=${Boolean(busy)} onClick=${() => use(pk.task)}>Использовать<//>`}
            </div>`}
          </article>`;
        })}
      </div>
      ${(data.notes_ru || []).length > 0 &&
      html`<ul class="advice__notes">${data.notes_ru.map((n) => html`<li><${Icon} name="lightbulb" size=${14} />${clean(n)}</li>`)}</ul>`}
      ${data.setup_ru && data.setup_ru[runtime] && html`<p class="advice__setup"><${Icon} name="info" size=${14} />${clean(data.setup_ru[runtime])}</p>`}
    `}
  </div>`;
}

function nearest(list, v) {
  return list.reduce((best, x) => (Math.abs(x - v) < Math.abs(best - v) ? x : best), list[0]);
}
