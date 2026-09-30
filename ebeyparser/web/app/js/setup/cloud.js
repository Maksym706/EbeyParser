// «Где работает нейросеть» (docs/design/CLOUD_AI.md): a free cloud (OpenRouter / NVIDIA / OmniRoute),
// this computer (LM Studio / Ollama), or the cloud with the computer as a stand-in. Shared by
// Settings → Нейросеть and the onboarding AI step. API: GET /ai/cloud, POST /ai/cloud/test, PUT /ai/cloud.
import { html, cx, useEffect, useState } from "../lib/html.js";
import { api, humanize } from "../lib/api.js";
import { LINKS } from "../lib/links.js";
import { number } from "../lib/format.js";
import { Icon, Button, ChoiceCards, Segmented, SecretInput, Select, TestResult, Toggle, Skeleton, ExternalLink, Badge } from "../ui/index.js";
import { AiConnect } from "./ai.js";

export const WHERE_OPTIONS = [
  { value: "cloud", label: "Бесплатное облако", icon: "cloud", description: "Мощная модель в интернете. Нужен только ключ — ничего не устанавливать" },
  { value: "local", label: "На моём компьютере", icon: "cpu", description: "LM Studio или Ollama. Без интернета, но нужна хорошая видеокарта" },
  { value: "hybrid", label: "Облако + компьютер про запас", icon: "layers", description: "Облако, а когда бесплатный лимит кончился — модель на компьютере" },
];

/** The providers the user picks from (the server also knows "custom" for experts). */
const PROVIDERS = [
  { value: "openrouter", label: "OpenRouter" },
  { value: "nvidia", label: "NVIDIA" },
  { value: "omniroute", label: "OmniRoute" },
];
const KEY_LINK = { openrouter: LINKS.openRouterKeys, nvidia: LINKS.nvidiaKeys, omniroute: LINKS.omniRoute };
const KEY_LINK_LABEL = { openrouter: "Получить ключ", nvidia: "Получить ключ", omniroute: "Как установить" };

/** GET /ai/cloud: presets, the current choice, keys ({set, masked}), today's usage. */
export function useCloud() {
  const [state, setState] = useState({ data: null, error: null });
  const load = () =>
    api.get("/ai/cloud").then(
      (data) => setState({ data, error: null }),
      (error) => setState((s) => ({ data: s.data, error })),
    );
  useEffect(() => {
    load();
  }, []);
  return { ...state, reload: load };
}

export function WherePicker({ value, onChange }) {
  return html`<div class="where-cards">
    <${ChoiceCards} value=${value} onChange=${onChange} options=${WHERE_OPTIONS} columns=${3} />
  </div>`;
}

const clean = (t) => humanize(String(t || "")).message;

/** A readable model name: OpenRouter's «NVIDIA: Nemotron 3 Super (free)» → «Nemotron 3 Super (free)»,
 * a bare id «nvidia/nemotron-nano-12b-v2-vl» → «nemotron-nano-12b-v2-vl». */
export function modelName(m) {
  const id = typeof m === "string" ? m : m.id;
  const name = typeof m === "string" ? "" : m.name;
  if (name && name !== id) return name.replace(/^[^:]{1,24}:\s*/, "");
  return String(id || "").split("/").pop();
}

/** Options with «рекомендую» / «видит фото» marks; the value is the model id. */
function modelOptions(models, { vision, recommended, current }) {
  const list = (models || []).filter((m) => (vision ? m.vision : true));
  const out = list.map((m) => ({
    value: m.id,
    label: `${modelName(m)}${m.id === recommended ? " — рекомендую" : vision ? "" : m.vision ? " — видит фото" : ""}`,
  }));
  if (current && !out.some((o) => o.value === current)) out.unshift({ value: current, label: `${modelName(current)} — сейчас` });
  return out;
}

/** «Сегодня 12 из 50 запросов» + a bar (the Состояние tile has the same). */
export function CloudUsage({ status }) {
  if (!status || !status.enabled) return null;
  const limit = status.daily_limit;
  const used = status.used_today || 0;
  const ratio = limit ? Math.min(1, used / limit) : 0;
  const tone = status.limited ? "red" : ratio >= 0.85 || status.scout_low ? "amber" : "green";
  return html`<div class=${cx("cloud-usage", `cloud-usage--${tone}`)}>
    <div class="cloud-usage__top">
      <span>Запросов сегодня</span>
      <b class="num">${limit ? `${number(Math.min(used, limit))} из ${number(limit)}` : number(used)}</b>
    </div>
    ${limit ? html`<div class="cloud-usage__track" role="meter" aria-label="Запросов сегодня" aria-valuemin="0" aria-valuemax=${limit} aria-valuenow=${used}><span style=${{ width: `${ratio * 100}%` }}></span></div>` : null}
    ${status.text_ru && html`<p class="cloud-usage__note">${clean(status.text_ru)}</p>`}
  </div>`;
}

/**
 * The cloud flow: provider → «Получить ключ» → key → «Проверить» → models from the live list →
 * limits and how far they go → «Сохранить».
 *   mode      "cloud" | "hybrid" (hybrid also shows «Компьютер про запас»)
 *   data      GET /ai/cloud
 *   onSaved(res) — after PUT /ai/cloud
 */
export function CloudConnect({ mode = "cloud", data, onSaved, saveLabel = "Сохранить" }) {
  const cur = (data && data.current) || {};
  const presets = Object.fromEntries(((data && data.presets) || []).map((p) => [p.key, p]));
  const [provider, setProvider] = useState(cur.provider && cur.provider !== "custom" ? cur.provider : "openrouter");
  const [key, setKey] = useState("");
  const [test, setTest] = useState({ state: "idle", r: null, error: null });
  const [text, setText] = useState(cur.provider === provider ? cur.text_model : "");
  const [vision, setVision] = useState(cur.provider === provider ? cur.vision_model : "");
  const [sendEbay, setSendEbay] = useState(Boolean(cur.send_ebay));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(null);
  const [fallbackOk, setFallbackOk] = useState(Boolean(cur.fallback && cur.fallback.base_url && cur.fallback.model));
  const [savedKey, setSavedKey] = useState({}); // provider -> masked key saved by «Проверить» in this session
  const p = presets[provider] || {};
  const stored = (data && data.keys && data.keys[provider]) || {};
  const keyInfo = savedKey[provider] ? { set: true, masked: savedKey[provider] } : stored;
  const r = test.r;

  useEffect(() => {
    setTest({ state: "idle", r: null, error: null });
    setText(cur.provider === provider ? cur.text_model : "");
    setVision(cur.provider === provider ? cur.vision_model : "");
    setSaved(null);
  }, [provider]);

  const runTest = async () => {
    setTest({ state: "loading", r: null, error: null });
    setSaved(null);
    try {
      const body = { provider, photo: true, save_key: true };
      if (key.trim()) body.api_key = key.trim();
      if (text) body.text_model = text;
      if (vision) body.vision_model = vision;
      const res = await api.post("/ai/cloud/test", body, { timeout: 240000 });
      setTest({ state: res.ok ? "ok" : "fail", r: res, error: null });
      if (res.text_model) setText(res.text_model);
      if (res.vision_model) setVision(res.vision_model);
      if (res.key_saved) {
        setKey("");
        setSavedKey((k) => ({ ...k, [provider]: res.key_masked }));
      }
    } catch (e) {
      setTest({ state: "fail", r: null, error: e });
    }
  };

  const save = async () => {
    setSaving(true);
    setSaved(null);
    try {
      const body = { mode, provider, text_model: text || null, vision_model: vision || null, send_ebay: sendEbay };
      if (key.trim()) body.api_key = key.trim();
      const res = await api.put("/ai/cloud", body);
      setKey("");
      setSaved({ state: "ok", title: mode === "hybrid" ? "Сохранено: облако, а компьютер — про запас" : "Сохранено: нейросеть работает в облаке" });
      onSaved && onSaved(res);
    } catch (e) {
      setSaved({ state: "fail", title: e.message, details: e.details });
    } finally {
      setSaving(false);
    }
  };

  const hasKey = Boolean(key.trim() || keyInfo.set || !p.key_required);
  const readyModels = Boolean(text || vision);
  const needsFallback = mode === "hybrid" && !fallbackOk;
  const testTitle = r ? clean(r.summary_ru || r.message_ru || r.error_ru) : "";
  return html`<div class="cloud-flow">
    <div class="cloud-step">
      <span class="cloud-step__label">Сервис</span>
      <${Segmented} value=${provider} onChange=${setProvider} options=${PROVIDERS} label="Облачный сервис" block />
      ${p.terms_ru && html`<p class="cloud-hint">${p.terms_ru}</p>`}
    </div>

    <div class="cloud-step">
      <span class="cloud-step__label">${p.key_required ? "Ключ" : "Ключ (необязательно)"}</span>
      ${p.how_ru && html`<p class="cloud-hint">${p.how_ru}</p>`}
      <div class="cloud-key">
        <div class="cloud-key__input">
          <${SecretInput} value=${key} onChange=${setKey} saved=${keyInfo.set} placeholder=${p.key_hint || "ключ"} class="mono" aria-label=${`Ключ ${p.name || ""}`} />
        </div>
        ${KEY_LINK[provider] && html`<${Button} variant="ghost" icon="key-round" iconRight="arrow-up-right" href=${KEY_LINK[provider]}>${KEY_LINK_LABEL[provider]}<//>`}
      </div>
      ${keyInfo.set && !key && html`<p class="cloud-hint cloud-hint--icon"><${Icon} name="lock" size=${14} /><span>Ключ сохранён: ${keyInfo.masked}. Хранится только на этом компьютере</span></p>`}
      <div class="cloud-actions">
        <${Button} variant="primary" icon="play" loading=${test.state === "loading"} disabled=${!hasKey} onClick=${runTest}>Проверить<//>
      </div>
      ${test.state === "loading" && html`<${TestResult} state="loading" title="Проверяю ключ, лимиты и модели… читаю 5 объявлений-примеров и одно фото" />`}
      ${test.error && html`<${TestResult} state="fail" title=${test.error.message} details=${test.error.details} />`}
      ${r &&
      html`<${TestResult} state=${r.ok ? "ok" : "fail"} title=${testTitle} details=${r.details}>
        ${(r.warnings || []).length > 0 && html`<ul class="cloud-warnings">${r.warnings.map((w) => html`<li>${clean(w)}</li>`)}</ul>`}
        ${r.triage && r.triage.answered > 0 &&
        html`<div class="scout-checks">
          ${[
            [r.triage.hidden_gpu, "нашла видеокарту в старом ПК"],
            [r.triage.typo_fixed, "поняла опечатку"],
            [r.triage.wanted_seen, "узнала «ищу», а не «продаю»"],
          ].map(([ok, t]) => html`<span class=${cx("tag", ok ? "tone-profit" : "tone-haggle")}><${Icon} name=${ok ? "check" : "x"} size=${12} stroke=${2.5} />${t}</span>`)}
          ${r.photo && r.photo.ok && html`<span class=${cx("tag", r.photo.recognised ? "tone-profit" : "tone-haggle")}><${Icon} name=${r.photo.recognised ? "check" : "x"} size=${12} stroke=${2.5} />фото: ${r.photo.product || "неясно"}</span>`}
        </div>`}
      <//>`}
    </div>

    ${(r && (r.models || []).length > 0) || readyModels
      ? html`<div class="cloud-step">
          <span class="cloud-step__label">Модели</span>
          <div class="cloud-models">
            <label class="cloud-model">
              <span class="cloud-model__label"><${Icon} name="file-text" size=${16} />Читает объявления</span>
              ${r && r.models && r.models.length
                ? html`<${Select} value=${text} onChange=${setText} options=${modelOptions(r.models, { vision: false, recommended: r.picks && r.picks.text, current: text })} aria-label="Модель для чтения объявлений" />`
                : html`<code class="mono cloud-model__id">${text ? modelName(text) : "—"}</code>`}
            </label>
            <label class="cloud-model">
              <span class="cloud-model__label"><${Icon} name="scan-eye" size=${16} />Проверяет фото</span>
              ${r && r.models && r.models.length
                ? html`<${Select} value=${vision} onChange=${setVision} options=${modelOptions(r.models, { vision: true, recommended: r.picks && r.picks.vision, current: vision })} aria-label="Модель для проверки фото" />`
                : html`<code class="mono cloud-model__id">${vision ? modelName(vision) : "—"}</code>`}
            </label>
          </div>
          ${!r && html`<p class="cloud-hint">Нажми «Проверить», чтобы выбрать из списка бесплатных моделей</p>`}
        </div>`
      : null}

    ${r && r.budget &&
    html`<div class="cloud-limits">
      <div class="cloud-limits__row"><${Icon} name="gauge" size=${16} /><span>${clean(r.limits_ru)}</span></div>
      <div class="cloud-limits__row"><${Icon} name="file-text" size=${16} /><span>${clean(r.budget.text_ru)}</span></div>
      ${provider === "openrouter" &&
      r.limits &&
      r.limits.is_free_tier !== false &&
      html`<div class="cloud-limits__row cloud-limits__tip"><${Icon} name="lightbulb" size=${16} /><span>Купи $10 кредитов один раз — и лимит станет 1000 запросов в день. Кредиты не расходуются: модели с «:free» остаются бесплатными.</span></div>
        <${ExternalLink} class="cloud-link" href=${LINKS.openRouterCredits}>Купить кредиты на OpenRouter<//>`}
    </div>`}

    ${data && data.status && data.status.enabled && !r && html`<${CloudUsage} status=${data.status} />`}

    ${mode === "hybrid" &&
    html`<div class="cloud-step">
      <span class="cloud-step__label">Компьютер про запас</span>
      <p class="cloud-hint">Когда бесплатный лимит облака кончится или облако не ответит, объявления и фото посмотрит модель на этом компьютере.</p>
      ${fallbackOk && cur.fallback && cur.fallback.model
        ? html`<p class="cloud-hint cloud-hint--icon"><${Icon} name="circle-check" size=${14} /><span>Про запас: <code class="mono">${cur.fallback.model}</code></span></p>`
        : null}
      <${AiConnect} save=${true} saveAs="fallback" current=${{ model: cur.fallback && cur.fallback.model, enabled: false }} onDone=${() => setFallbackOk(true)} />
    </div>`}

    <div class="cloud-privacy">
      <${Icon} name="shield-check" size=${18} />
      <div>
        <p>${data && data.privacy_ru}</p>
        ${provider === "openrouter" &&
        html`<p class="cloud-hint">Некоторые бесплатные модели могут сохранять запросы — это выключается в настройках приватности OpenRouter.</p>
          <${ExternalLink} class="cloud-link" href=${LINKS.openRouterPrivacy}>Настройки приватности OpenRouter<//>`}
      </div>
    </div>
    <${Toggle} checked=${sendEbay} onChange=${setSendEbay} label="Отправлять объявления с eBay в облако" description="Правила eBay запрещают передавать его данные другим сервисам — поэтому выключено: такие объявления смотрит компьютер про запас или обычная проверка." />

    <div class="cloud-actions">
      <${Button} variant="primary" icon="check" loading=${saving} disabled=${!readyModels || !hasKey || needsFallback} onClick=${save}>${saveLabel}<//>
      ${!readyModels && html`<span class="cloud-hint">Сначала нажми «Проверить»</span>`}
      ${readyModels && needsFallback && html`<span class="cloud-hint">Сначала выбери модель на компьютере</span>`}
    </div>
    ${saved && html`<${TestResult} state=${saved.state} title=${saved.title} details=${saved.details} />`}
  </div>`;
}

/** Short status line for places that only need to say where the AI runs. */
export function WhereBadge({ data }) {
  if (!data) return html`<${Skeleton} w="40%" h=${14} />`;
  const where = WHERE_OPTIONS.find((o) => o.value === data.mode) || WHERE_OPTIONS[1];
  return html`<${Badge} tone=${data.mode === "local" ? "neutral" : "blue"} icon=${where.icon}>${where.label}<//>`;
}
