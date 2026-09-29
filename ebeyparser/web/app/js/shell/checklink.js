// «Проверить объявление» (brief §4.8): paste a Kleinanzeigen / eBay URL → POST /check (job) → decision.
import { html, cx, useState, useRef, useEffect } from "../lib/html.js";
import { api } from "../lib/api.js";
import { money, percent } from "../lib/format.js";
import { navigate } from "../lib/router.js";
import { Icon, Button, Modal, Badge, Progress, Banner, Checklist, Segmented, NumberInput, Field } from "../ui/index.js";

const URL_RE = /^https?:\/\/([a-z0-9-]+\.)*(kleinanzeigen\.de|ebay\.[a-z.]+|ebay-kleinanzeigen\.de)\//i;

export function looksLikeAdUrl(text) {
  return URL_RE.test(String(text || "").trim());
}

const STAGES = [
  { key: "fetch", label: "Открываю объявление" },
  { key: "market", label: "Ищу цены похожих" },
  { key: "ai", label: "Нейросеть смотрит фото (~15 с)" },
  { key: "done", label: "Считаю выгоду" },
];

/** Decision verb + hue for a deal card (brief §4.2.3 decision pill). */
export function decisionOf(card) {
  const a = card.action;
  const personal = card.purpose === "personal";
  if (a === "buy") return { tone: "green", icon: personal ? "piggy-bank" : "trending-up", label: "Покупай", line: card.profit != null ? `${personal ? "Экономия" : "Прибыль"} ≈ ${money(card.profit, { sign: !personal })}` : "" };
  if (a === "haggle")
    return { tone: "amber", icon: "hand-coins", label: "Торгуйся", line: card.offer_price != null ? `Предложи ${money(card.offer_price)}${card.profit_at_offer != null ? ` → ${money(card.profit_at_offer, { sign: true })}` : ""}` : "" };
  if (a === "bid") return { tone: "violet", icon: "gavel", label: "Аукцион", line: card.max_buy_price != null ? `Ставь максимум ${money(card.max_buy_price)}` : "" };
  if (a === "watch" || card.verdict === "maybe") return { tone: "neutral", icon: "eye", label: "Подумай", line: (card.reasons && card.reasons[0]) || "" };
  return { tone: "neutral", icon: "eye-off", label: "Не выгодно", line: (card.reasons && card.reasons[0]) || "" };
}

export function CheckLinkModal({ open, onClose, initialUrl = "" }) {
  const [url, setUrl] = useState(initialUrl);
  const [purpose, setPurpose] = useState("resale");
  const [target, setTarget] = useState(null);
  const [state, setState] = useState("idle"); // idle | loading | done | error
  const [stage, setStage] = useState(0);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    setUrl(initialUrl);
    setState("idle");
    setResult(null);
    setError(null);
    if (initialUrl && looksLikeAdUrl(initialUrl)) run(initialUrl);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialUrl]);

  async function run(target_url = url) {
    const clean = String(target_url || "").trim();
    if (!looksLikeAdUrl(clean)) {
      setError("Это не похоже на ссылку на объявление Kleinanzeigen или eBay. Скопируй адрес из браузера целиком.");
      setState("error");
      return;
    }
    setState("loading");
    setStage(0);
    setError(null);
    try {
      let res = await api.post("/check", { url: clean, purpose, target_price: purpose === "personal" ? target : null }, { timeout: 60000 });
      if (res && res.job) {
        res = await api.waitJob(res.job.id, {
          interval: 1200,
          timeout: 16 * 60 * 1000,
          onProgress: (job) => {
            const i = STAGES.findIndex((s) => s.key === job.stage);
            if (i >= 0) setStage(i);
            else if (job.stages && job.stages.length) setStage(Math.min(STAGES.length - 1, job.stages.length - 1));
          },
        });
      }
      setResult(res);
      setState("done");
    } catch (e) {
      setError(e.missing ? "Проверка ссылок появится в следующем обновлении программы." : e.message);
      setState("error");
    }
  }

  const paste = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        setUrl(text.trim());
        if (looksLikeAdUrl(text)) run(text);
      }
    } catch {
      inputRef.current && inputRef.current.focus();
    }
  };

  const d = result && decisionOf(result);
  return html`<${Modal} open=${open} onClose=${onClose} title="Проверить объявление" subtitle="Вставь ссылку — посчитаю, выгодно ли покупать" icon="scan-search">
    <form
      class="checklink__form"
      onSubmit=${(e) => {
        e.preventDefault();
        run();
      }}
    >
      <div class=${cx("input input--lg", state === "error" && !result && "is-invalid")}>
        <span class="input__icon"><${Icon} name="link" size=${18} /></span>
        <input
          ref=${inputRef}
          value=${url}
          onInput=${(e) => setUrl(e.currentTarget.value)}
          placeholder="Ссылка на объявление"
          inputmode="url"
          autocomplete="off"
          data-autofocus
          aria-label="Ссылка на объявление"
        />
        <button type="button" class="input__btn" onClick=${paste} aria-label="Вставить из буфера" title="Вставить из буфера">
          <${Icon} name="clipboard-paste" size=${18} />
        </button>
      </div>
      <${Button} type="submit" variant="primary" size="lg" loading=${state === "loading"} disabled=${!url.trim()}>Оценить<//>
    </form>
    <div class="checklink__options">
      <${Segmented}
        size="sm"
        value=${purpose}
        onChange=${setPurpose}
        label="Для чего"
        options=${[
          { value: "resale", label: "Перепродажа", icon: "trending-up" },
          { value: "personal", label: "Для себя", icon: "piggy-bank" },
        ]}
      />
      ${purpose === "personal" &&
      html`<div class="checklink__target">
        <${NumberInput} size="sm" value=${target} onChange=${setTarget} suffix="€" placeholder="Хочу заплатить до" aria-label="Хочу заплатить до" />
      </div>`}
    </div>

    ${state === "loading" &&
    html`<div class="checklink__progress">
      <${Progress} value=${null} />
      <${Checklist} items=${STAGES.map((s, i) => ({ label: s.label, state: i < stage ? "done" : i === stage ? "active" : "todo" }))} />
    </div>`}

    ${state === "error" && error && html`<${Banner} tone="red" class="mt-4">${error}<//>`}

    ${state === "done" &&
    result &&
    html`<div class="checklink__result">
      <div class="checklink__head">
        ${result.image
          ? html`<img class="checklink__img" src=${result.image} alt="" referrerpolicy="no-referrer" />`
          : html`<span class="checklink__img checklink__img--empty"><${Icon} name="image" size=${22} /></span>`}
        <div class="grow">
          <div class="checklink__title">${result.title}</div>
          <div class="checklink__price money">${money(result.price)}${result.negotiable ? " · VB" : ""}</div>
        </div>
      </div>
      <div class=${cx("decision", `decision--${d.tone}`)}>
        <${Icon} name=${d.icon} size=${20} />
        <div class="grow">
          <div class="decision__verb">${d.label}</div>
          ${d.line && html`<div class="decision__line">${d.line}</div>`}
        </div>
        ${result.score != null && html`<span class="decision__score" title=${`${result.score} из 100`}>${result.score}</span>`}
      </div>
      ${result.red_flags && result.red_flags.length > 0 &&
      html`<${Banner} tone="red" icon="shield-alert" title="Осторожно">${result.red_flags.slice(0, 3).join(" · ")}<//>`}
      <div class="checklink__numbers">
        <div><span>Рынок</span><b class="money">${result.market_price != null ? "~" + money(result.market_price) : "—"}</b></div>
        <div><span>${result.profit_kind === "savings" ? "Экономия" : "Прибыль"}</span><b class=${cx("money", result.profit > 0 ? "text-green" : result.profit < 0 && "text-red")}>${money(result.profit, { sign: true })}</b></div>
        <div><span>ROI</span><b class="money">${percent(result.roi)}</b></div>
      </div>
      ${result.market_source_label && html`<p class="checklink__summary">Рынок по данным: ${result.market_source_label}</p>`}
      <div class="row">
        ${result.id &&
        html`<${Button}
          variant="secondary"
          iconRight="arrow-right"
          onClick=${() => {
            onClose();
            navigate(`/deal/${encodeURIComponent(result.id)}`);
          }}
          >Открыть полностью<//
        >`}
        ${result.url && html`<${Button} variant="ghost" icon="external-link" href=${result.url}>Объявление<//>`}
      </div>
    </div>`}

    ${state === "idle" &&
    html`<p class="checklink__hint">
      <${Icon} name="lightbulb" size=${16} />
      Открой объявление на Kleinanzeigen или eBay, скопируй адрес из строки браузера и вставь сюда. Можно просто нажать Ctrl+V на любой странице.
    </p>`}
  <//>`;
}
