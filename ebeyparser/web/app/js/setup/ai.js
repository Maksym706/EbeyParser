// «Нейросеть»: auto-detect LM Studio / Ollama, pick a vision model, test it on a sample ad
// (POST /ai/detect, POST /ai/test). Brief §4.1.6; also used in Settings → Нейросеть.
import { html, useEffect, useState, useRef } from "../lib/html.js";
import { api, humanize } from "../lib/api.js";
import { LINKS } from "../lib/links.js";
import { useInterval } from "../lib/hooks.js";
import { Icon, Button, Select, Skeleton, TestResult, Chip, CopyButton, Badge, ExternalLink } from "../ui/index.js";

export function useAiDetect() {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const detect = async (silent = false) => {
    if (!silent) setState((s) => ({ ...s, loading: true }));
    const started = Date.now();
    try {
      const data = await api.post("/ai/detect", {}, { timeout: 15000 });
      // keep the "searching" skeleton visible for a moment so it doesn't flash
      if (!silent) await new Promise((r) => setTimeout(r, Math.max(0, 700 - (Date.now() - started))));
      setState({ loading: false, data, error: null });
      return data;
    } catch (e) {
      setState({ loading: false, data: null, error: e });
      return null;
    }
  };
  useEffect(() => {
    detect();
  }, []);
  return { ...state, detect };
}

/** All models of all found servers as select options, vision models first. */
function modelOptions(servers) {
  const out = [];
  for (const s of servers || []) {
    const models = [...(s.models || [])].sort((a, b) => Number(b.vision) - Number(a.vision) || Number(b.recommended) - Number(a.recommended));
    for (const m of models) {
      out.push({
        value: `${s.provider}|${s.base_url}|${m.id}`,
        label: `${m.id}${m.recommended ? " — рекомендуем" : m.vision ? " — видит фото" : " — не видит фото"}${servers.length > 1 ? ` (${s.name})` : ""}`,
        vision: m.vision,
      });
    }
  }
  return out;
}

/** Result card of POST /ai/test in human words. */
export function AiTestResult({ result, error }) {
  if (error) return html`<${TestResult} state="fail" title="Не смог проверить" detail=${error.message} details=${error.details} />`;
  if (!result) return null;
  if (!result.ok) {
    const h = humanize(result.error_ru || result.message_ru || "Нейросеть не ответила");
    return html`<${TestResult} state="fail" title=${h.message} detail=${!result.server_ok && !/Start Server/.test(h.message) ? "Открой LM Studio → Developer → Start Server" : ""} details=${h.details} />`;
  }
  const v = result.verdict;
  const detail = v
    ? html`<dl class="ai-seen">
        <div><dt>Модель увидела</dt><dd>${v.product || "неясно"}</dd></div>
        <div><dt>Состояние</dt><dd>${(v.condition_label || "—").toLowerCase()}</dd></div>
        <div><dt>Фото</dt><dd>${v.photo_matches_description === true ? "совпадают с описанием ✓" : v.photo_matches_description === false ? "не совпадают с описанием ✗" : "по фото неясно"}</dd></div>
        ${result.seconds != null && html`<div><dt>Ответ</dt><dd class="num">за ${Math.round(result.seconds)} с</dd></div>`}
      </dl>`
    : result.message_ru;
  return html`<div class="stack" style=${{ "--gap": "8px" }}>
    <${TestResult} state="ok" title=${result.seconds != null ? `Нейросеть работает · ответ за ${Math.round(result.seconds)} с` : "Нейросеть работает"} detail=${detail} />
    ${result.warning_ru && html`<${TestResult} state="warn" title=${result.warning_ru} />`}
  </div>`;
}

/** The model the guides name: the server's recommended preset (POST /ai/detect `gpu_presets`). */
function recommendedModel(data) {
  const presets = (data && data.gpu_presets) || [];
  const p = presets.find((x) => x.recommended) || presets[0];
  if (p) return { name: p.model_ru, id: p.lmstudio };
  const s = data && data.suggested && data.suggested.model;
  return s ? { name: s, id: s } : null;
}

/** "на процессоре 1–3 минуты" → "На процессоре 1–3 минуты." */
const sentence = (t) => {
  const s = String(t || "").trim();
  if (!s) return s;
  return s[0].toUpperCase() + s.slice(1) + (/[.!?…]$/.test(s) ? "" : ".");
};

function Steps({ items }) {
  return html`<ol class="mini-steps">
    ${items.map(
      (it, i) => html`<li class="mini-steps__item">
        <span class="mini-steps__num">${i + 1}</span>
        <div class="mini-steps__body">
          <div class="mini-steps__title">${it.title}</div>
          ${it.body && html`<div class="mini-steps__text">${it.body}</div>`}
        </div>
        ${it.art && html`<div class="mini-steps__art" aria-hidden="true">${it.art}</div>`}
      </li>`,
    )}
  </ol>`;
}

/** Tiny static mock-ups of LM Studio screens (no external images). */
const ArtWindow = ({ children }) => html`<svg viewBox="0 0 120 72" width="120" height="72">
  <rect x="1" y="1" width="118" height="70" rx="8" class="art-win" />
  <circle cx="10" cy="9" r="2.5" class="art-dot" /><circle cx="18" cy="9" r="2.5" class="art-dot" /><circle cx="26" cy="9" r="2.5" class="art-dot" />
  ${children}
</svg>`;
const ART = {
  download: html`<${ArtWindow}><rect x="30" y="26" width="60" height="22" rx="6" class="art-accent" /><path d="M60 30v10m-4-4 4 4 4-4" class="art-stroke-on" /><//>`,
  discover: html`<${ArtWindow}><rect x="10" y="20" width="100" height="10" rx="5" class="art-line" /><rect x="10" y="36" width="70" height="8" rx="3" class="art-line" /><rect x="10" y="50" width="54" height="8" rx="3" class="art-line" /><rect x="84" y="36" width="26" height="22" rx="4" class="art-accent" /><//>`,
  server: html`<${ArtWindow}><rect x="10" y="22" width="44" height="40" rx="4" class="art-line" /><rect x="62" y="26" width="48" height="12" rx="6" class="art-accent" /><circle cx="68" cy="50" r="4" class="art-ok" /><rect x="76" y="47" width="32" height="6" rx="3" class="art-line" /><//>`,
};

/**
 * The whole AI block. Props:
 *   save — write provider/base_url/model on a successful test (onboarding: true)
 *   onDone(result) — called after a successful test
 */
export function AiConnect({ save = true, onDone, current, onChoice }) {
  const { loading, data, error, detect } = useAiDetect();
  const [choice, setChoice] = useState("");
  const [gpu, setGpu] = useState(null);
  const [test, setTest] = useState({ state: "idle", result: null, error: null });
  const variant = data ? data.variant : null;
  const options = data ? modelOptions(data.servers) : [];
  const first = useRef(true);

  // default model choice: current config if present among options, else the server suggestion
  useEffect(() => {
    if (!data || !options.length) return;
    const cur = current && current.model ? options.find((o) => o.value.endsWith(`|${current.model}`)) : null;
    const sug = data.suggested ? options.find((o) => o.value === `${data.suggested.provider}|${data.suggested.base_url}|${data.suggested.model}`) : null;
    if (!choice || !options.some((o) => o.value === choice)) setChoice((cur || sug || options.find((o) => o.vision) || options[0]).value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  // tell the onboarding which model «Дальше» would accept (a found model that sees photos)
  useEffect(() => {
    if (!onChoice) return;
    if (variant !== "found" || !choice) return onChoice(null);
    const [provider, base_url, ...rest] = choice.split("|");
    const opt = options.find((o) => o.value === choice);
    onChoice({ provider, base_url, model: rest.join("|"), vision: Boolean(opt && opt.vision) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant, choice]);

  // B / C: poll every 5 s while the step is open — "как только запустишь сервер — я сам увижу"
  useInterval(() => detect(true), !loading && variant && variant !== "found" ? 5000 : null);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setTest({ state: "idle", result: null, error: null });
  }, [choice]);

  const runTest = async () => {
    const [provider, base_url, ...rest] = choice.split("|");
    const model = rest.join("|");
    setTest({ state: "loading", result: null, error: null });
    try {
      const result = await api.post("/ai/test", { provider, base_url, model, sample: true, save }, { timeout: 200000 });
      setTest({ state: result.ok ? "ok" : "fail", result, error: null });
      if (result.ok && onDone) onDone(result);
    } catch (e) {
      setTest({ state: "fail", result: null, error: e });
    }
  };

  if (loading && !data) {
    return html`<div class="ai-card ai-card--loading" aria-busy="true">
      <div class="ai-card__head"><span class="spinner" style=${{ width: 18, height: 18 }}></span><b>Ищу LM Studio и Ollama на этом компьютере…</b></div>
      <${Skeleton} h=${40} radius="12px" />
      <${Skeleton} w="45%" h=${14} />
    </div>`;
  }
  if (error && !data) {
    return html`<div class="ai-card ai-card--neutral">
      <${TestResult} state="fail" title="Не смог проверить" detail=${error.message} details=${error.details} />
      <${Button} icon="refresh-cw" onClick=${() => detect()}>Ещё раз<//>
    </div>`;
  }

  if (variant === "found") {
    const srv = data.servers.find((s) => s.vision_models && s.vision_models.length) || data.servers[0];
    return html`<div class="ai-card ai-card--green">
      <div class="ai-card__head">
        <${Icon} name="circle-check" size=${20} class="text-green" />
        <b>Нашёл ${srv.name} · модель видит фото</b>
      </div>
      <div class="ai-card__row">
        <${Select} value=${choice} onChange=${setChoice} options=${options} aria-label="Модель нейросети" />
        <${Button} variant="primary" icon="scan-eye" loading=${test.state === "loading"} onClick=${runTest}>Проверить на примере<//>
      </div>
      ${test.state === "loading" && html`<${TestResult} state="loading" title="Жду ответа от нейросети…" detail="Модель смотрит фото и описание объявления-примера. Первый раз может занять до минуты." />`}
      ${test.state !== "loading" && html`<${AiTestResult} result=${test.result} error=${test.error} />`}
    </div>`;
  }

  const rec = recommendedModel(data);
  if (variant === "no_vision") {
    const srv = data.servers[0];
    return html`<div class="ai-card ai-card--amber">
      <div class="ai-card__head">
        <${Icon} name="triangle-alert" size=${20} class="text-amber" />
        <b>${srv.name} работает, но нет модели, которая понимает фото</b>
      </div>
      <${Steps}
        items=${[
          { title: "Открой вкладку Discover", body: "В LM Studio слева — значок лупы.", art: ART.discover },
          rec
            ? { title: html`Найди <code>${rec.id}</code>`, body: html`<${CopyButton} text=${rec.id} label="Скопировать название" />` }
            : { title: "Найди модель, которая понимает фото", body: "В описании модели должно быть Vision." },
          { title: "Загрузи её с Context Length 8192", body: "Кнопка Load → в настройках модели поставь Context Length 8192.", art: ART.server },
        ]}
      />
      <div class="ai-card__foot">
        <span class="ai-card__watch"><span class="status-dot status-dot--amber is-pulsing"></span>Слежу — как только модель появится, я сам увижу</span>
        <${Button} size="sm" icon="refresh-cw" onClick=${() => detect()}>Проверить снова<//>
      </div>
    </div>`;
  }

  // C: nothing found
  const presets = (data && data.gpu_presets) || [];
  const chosen = presets.find((p) => p.key === gpu);
  // AI is set up (a saved model) but nobody answers: LM Studio is just closed — not «install it» (P1-21)
  if (variant === "not_running" || (current && current.model && current.enabled)) {
    const title = variant === "not_running" && data.message_ru ? humanize(data.message_ru).message : "LM Studio не отвечает — похоже, он закрыт";
    return html`<div class="ai-card ai-card--amber">
      <div class="ai-card__head">
        <${Icon} name="triangle-alert" size=${20} class="text-amber" />
        <b>${title}</b>
      </div>
      <${Steps}
        items=${[
          { title: "Открой LM Studio на этом компьютере" },
          { title: "Developer → Start Server", body: "И включи автозапуск сервера, чтобы проверка работала после перезагрузки.", art: ART.server },
        ]}
      />
      <div class="ai-card__foot">
        <span class="ai-card__watch"><span class="status-dot status-dot--amber is-pulsing"></span>Как только сервер запустится — я сам увижу</span>
        <${Button} size="sm" variant="ghost" icon="refresh-cw" onClick=${() => detect()}>Проверить сейчас<//>
      </div>
      <details class="guide">
        <summary>LM Studio не установлен? Как поставить за 5 минут</summary>
        <div class="guide__body">
          <${Steps}
            items=${[
              { title: html`Скачай LM Studio — это бесплатно`, body: html`<${ExternalLink} href=${LINKS.lmStudio}>lmstudio.ai<//>` },
              { title: html`Во вкладке Discover скачай <b>${rec ? rec.name : "модель, которая понимает фото"}</b>`, body: "Модель, которая понимает фото и немецкий текст." },
              { title: "Developer → Start Server" },
            ]}
          />
        </div>
      </details>
    </div>`;
  }
  return html`<div class="ai-card ai-card--neutral">
    <div class="ai-card__head">
      <${Icon} name="server" size=${20} />
      <b>LM Studio пока не найден — поставим за 5 минут</b>
    </div>
    <${Steps}
      items=${[
        { title: html`Скачай LM Studio — это бесплатно`, body: html`<${ExternalLink} href=${LINKS.lmStudio}>lmstudio.ai<//>`, art: ART.download },
        { title: html`Во вкладке Discover скачай <b>${chosen ? chosen.model_ru : rec ? rec.name : "модель, которая понимает фото"}</b>`, body: chosen ? sentence(chosen.note_ru) : "Модель, которая понимает фото и немецкий текст.", art: ART.discover },
        { title: "Developer → Start Server", body: "И включи автозапуск сервера, чтобы проверка работала после перезагрузки.", art: ART.server },
      ]}
    />
    <div class="ai-card__gpu">
      <div class="ai-card__label">Какая у тебя видеокарта?</div>
      <div class="row" style=${{ "--gap": "6px" }}>
        ${presets.map((p) => html`<${Chip} key=${p.key} selected=${gpu === p.key} onClick=${() => setGpu(gpu === p.key ? null : p.key)}>${p.label_ru}<//>`)}
      </div>
      ${chosen &&
      html`<p class="ai-card__rec">
        Рекомендую <b>${chosen.model_ru}</b>${chosen.recommended ? html` <${Badge} tone="green" size="sm">рекомендуем<//>` : ""} — ${chosen.note_ru}.
        <${CopyButton} text=${chosen.lmstudio} label="Скопировать название" />
      </p>`}
    </div>
    <div class="ai-card__foot">
      <span class="ai-card__watch"><span class="status-dot status-dot--blue is-pulsing"></span>Как только запустишь сервер — я сам увижу</span>
      <${Button} size="sm" variant="ghost" icon="refresh-cw" onClick=${() => detect()}>Проверить сейчас<//>
    </div>
  </div>`;
}

