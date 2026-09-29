// Global "Проверить ссылку": paste a Kleinanzeigen / eBay URL → POST /check → verdict.
import { html, cx, useState, useRef, useEffect } from "../lib/html.js";
import { api } from "../lib/api.js";
import { money, percent } from "../lib/format.js";
import { navigate } from "../lib/router.js";
import { Icon, Button, Modal, Badge, Progress, Banner, Checklist } from "../ui/index.js";

const URL_RE = /^https?:\/\/([a-z0-9-]+\.)*(kleinanzeigen\.de|ebay\.[a-z.]+|ebay-kleinanzeigen\.de)\//i;

export function looksLikeAdUrl(text) {
  return URL_RE.test(String(text || "").trim());
}

const VERDICT = {
  buy: { tone: "profit", label: "Покупать", icon: "circle-check" },
  maybe: { tone: "haggle", label: "Подумать", icon: "hand-coins" },
  skip: { tone: "danger", label: "Пропустить", icon: "circle-x" },
};

const STAGES = ["Открываю объявление", "Ищу, за сколько такое продают", "Смотрю фото нейросетью", "Считаю выгоду"];

/** Pull the useful bits out of a check result (deal view or job result). */
function summarize(r) {
  const deal = r && (r.deal || r.item || r);
  const listing = (deal && (deal.listing || deal)) || {};
  const ev = (deal && (deal.evaluation || deal.eval)) || {};
  const est = ev.estimate || {};
  return {
    id: listing.ad_id || deal.ad_id || deal.id || r.ad_id || null,
    title: listing.title || deal.title || "Объявление",
    price: listing.price ?? deal.price ?? null,
    image: (listing.image_urls && listing.image_urls[0]) || deal.image || null,
    verdict: ev.verdict || deal.verdict || null,
    profit: est.profit ?? ev.profit ?? deal.profit ?? null,
    resale: est.resale_price ?? est.market_price ?? deal.resale_price ?? null,
    roi: est.roi ?? deal.roi ?? null,
    reasons: ev.reasons || deal.reasons || [],
    summary: ev.summary_ru || deal.summary_ru || ev.summary || null,
  };
}

export function CheckLinkModal({ open, onClose, initialUrl = "" }) {
  const [url, setUrl] = useState(initialUrl);
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

  useEffect(() => {
    if (state !== "loading") return undefined;
    const t = setInterval(() => setStage((s) => Math.min(STAGES.length - 1, s + 1)), 2600);
    return () => clearInterval(t);
  }, [state]);

  async function run(target = url) {
    const clean = String(target || "").trim();
    if (!looksLikeAdUrl(clean)) {
      setError("Это не похоже на ссылку на объявление Kleinanzeigen или eBay. Скопируй адрес из браузера целиком.");
      setState("error");
      return;
    }
    setState("loading");
    setStage(0);
    setError(null);
    try {
      let res = await api.post("/check", { url: clean }, { timeout: 120000 });
      const jobId = res && (res.job_id || (res.job && res.job.id));
      if (jobId) {
        res = await api.waitJob(jobId, {
          onProgress: (job) => {
            if (typeof job.step === "number") setStage(Math.min(STAGES.length - 1, job.step));
          },
        });
      }
      setResult(summarize(res));
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

  const v = result && VERDICT[result.verdict];
  return html`<${Modal} open=${open} onClose=${onClose} title="Проверить объявление" subtitle="Вставь ссылку — посчитаю, выгодно ли покупать" icon="scan-line">
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
          placeholder="https://www.kleinanzeigen.de/s-anzeige/…"
          inputmode="url"
          autocomplete="off"
          data-autofocus
          aria-label="Ссылка на объявление"
        />
        <button type="button" class="input__btn" onClick=${paste} aria-label="Вставить из буфера" title="Вставить из буфера">
          <${Icon} name="clipboard-paste" size=${18} />
        </button>
      </div>
      <${Button} type="submit" variant="primary" size="lg" loading=${state === "loading"} disabled=${!url.trim()}>Проверить<//>
    </form>

    ${state === "loading" &&
    html`<div class="checklink__progress">
      <${Progress} value=${null} />
      <${Checklist} items=${STAGES.map((label, i) => ({ label, state: i < stage ? "done" : i === stage ? "active" : "todo" }))} />
    </div>`}

    ${state === "error" && error && html`<${Banner} tone="danger" class="mt-4">${error}<//>`}

    ${state === "done" &&
    result &&
    html`<div class="checklink__result">
      <div class="checklink__head">
        ${result.image ? html`<img class="checklink__img" src=${result.image} alt="" />` : html`<span class="checklink__img checklink__img--empty"><${Icon} name="image" size=${22} /></span>`}
        <div class="grow">
          <div class="checklink__title">${result.title}</div>
          <div class="checklink__price money">${money(result.price)}</div>
        </div>
        ${v && html`<${Badge} tone=${v.tone} size="lg" icon=${v.icon}>${v.label}<//>`}
      </div>
      <div class="checklink__numbers">
        <div><span>Продашь за</span><b class="money">${result.resale != null ? "~" + money(result.resale) : "—"}</b></div>
        <div><span>Прибыль</span><b class=${cx("money", result.profit > 0 ? "text-profit" : result.profit < 0 && "text-danger")}>${money(result.profit, { sign: true })}</b></div>
        <div><span>Доходность</span><b class="money">${percent(result.roi)}</b></div>
      </div>
      ${result.summary && html`<p class="checklink__summary">${result.summary}</p>`}
      ${result.id &&
      html`<${Button}
        variant="secondary"
        block
        iconRight="arrow-right"
        onClick=${() => {
          onClose();
          navigate(`/deal/${encodeURIComponent(result.id)}`);
        }}
        >Подробный разбор<//
      >`}
    </div>`}

    ${state === "idle" &&
    html`<p class="checklink__hint">
      <${Icon} name="lightbulb" size=${16} />
      Открой объявление на Kleinanzeigen или eBay, скопируй адрес из строки браузера и вставь сюда.
    </p>`}
  <//>`;
}
