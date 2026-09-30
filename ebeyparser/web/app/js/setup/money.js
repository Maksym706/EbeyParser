// «Деньги»: purpose, budget, strategy presets and the live example (brief §4.1.4, §4.7.2).
import { html } from "../lib/html.js";
import { money, percent } from "../lib/format.js";
import { Icon, Slider, NumberInput, ChoiceCards, HelpTip } from "../ui/index.js";

/** Same values as the server's PRESETS (routes_setup.py); the server list wins when loaded. */
export const DEFAULT_PRESETS = {
  careful: { title_ru: "Осторожно", line_ru: "Только очень выгодное, меньше уведомлений", icon: "shield", min_profit: 60, min_roi: 0.35, safety_margin_percent: 15, min_comparables: 8, notify_min_score: 80 },
  balanced: { title_ru: "Сбалансированно", line_ru: "Золотая середина", icon: "scale", min_profit: 40, min_roi: 0.25, safety_margin_percent: 10, min_comparables: 6, notify_min_score: 70 },
  aggressive: { title_ru: "Агрессивно", line_ru: "Больше находок, больше проверять самому", icon: "rocket", min_profit: 25, min_roi: 0.15, safety_margin_percent: 7, min_comparables: 4, notify_min_score: 60 },
};
const NOTIFY_HINT = { careful: "≈ 1–2 уведомления в день", balanced: "≈ 3–5 уведомлений в день", aggressive: "≈ 6–10 уведомлений в день" };

export const PURPOSES = [
  { value: "resale", label: "Перепродажа", description: "Купить дешевле рынка и продать", icon: "trending-up" },
  { value: "personal", label: "Для себя", description: "Купить дешевле рынка себе", icon: "piggy-bank" },
  { value: "both", label: "И то и другое", description: "Перепродажа + свой список желаний", icon: "layers" },
];

/** Which preset matches these pricing values (else "custom"). */
export function detectStrategy(p, presets = DEFAULT_PRESETS) {
  for (const [key, v] of Object.entries(presets)) {
    if (Math.abs((p.min_profit ?? -1) - v.min_profit) < 0.01 && Math.abs((p.min_roi ?? -1) - v.min_roi) < 1e-6 && Math.abs((p.safety_margin_percent ?? -1) - v.safety_margin_percent) < 0.01) return key;
  }
  return "custom";
}

export function StrategyCards({ value, onChange, presets = DEFAULT_PRESETS, columns = 3 }) {
  const options = Object.entries(presets).map(([key, p]) => ({
    value: key,
    label: p.title_ru,
    icon: p.icon,
    description: html`${p.line_ru}<span class="choice-card__meta">от ${money(p.min_profit)} · ROI от ${percent(p.min_roi)} · ${NOTIFY_HINT[key] || ""}</span>`,
  }));
  return html`<${ChoiceCards} value=${value} onChange=${onChange} options=${options} columns=${columns} />`;
}

/**
 * Max price at which a listing with this market price still passes the thresholds:
 * resale after the risk margin must leave min_profit and min_roi.
 */
export function maxBuyFor(market, { min_profit = 40, min_roi = 0.25, safety_margin_percent = 10 } = {}) {
  const net = market * (1 - safety_margin_percent / 100);
  const byProfit = net - min_profit;
  const byRoi = net / (1 + min_roi);
  const raw = Math.max(0, Math.min(byProfit, byRoi));
  const step = raw >= 100 ? 10 : 5;
  return Math.floor(raw / step) * step;
}

/** «Купишь до 300 € → продашь за ~420 € → прибыль ≈ +78 €» (blue-tinted, §4.1.4). */
export function ExampleBox({ pricing, market = 420, product = "iPhone 13 128 GB" }) {
  const buy = maxBuyFor(market, pricing);
  const profit = Math.round(market * (1 - (pricing.safety_margin_percent ?? 10) / 100) - buy);
  const bad =
    pricing.min_profit == null ||
    pricing.min_profit < 0 ||
    pricing.min_roi == null ||
    pricing.min_roi < 0 ||
    pricing.min_roi > 10 ||
    pricing.safety_margin_percent == null ||
    pricing.safety_margin_percent < 0 ||
    pricing.safety_margin_percent > 50;
  if (bad || buy <= 0) {
    // impossible thresholds: say it plainly instead of «цена до 0 € · прибыль +378 €» (P1-9)
    return html`<div class="example-box example-box--warn" aria-live="polite">
      <div class="example-box__title"><${Icon} name="triangle-alert" size=${16} />С такими порогами я не найду ничего</div>
      <p class="example-box__text">
        ${bad
          ? "Исправь значения, отмеченные красным: прибыль от 0 €, ROI от 0 до 1000 %, запас на риск от 0 до 50 %."
          : html`Чтобы ${product} за ~${money(market)} прошёл по порогам, его пришлось бы отдать даром. Уменьши минимальную прибыль или ROI.`}
      </p>
    </div>`;
  }
  return html`<div class="example-box" aria-live="polite">
    <div class="example-box__title"><${Icon} name="lightbulb" size=${16} />Пример</div>
    <p class="example-box__text">
      ${product} стоит на рынке ~${money(market)}. Уведомлю, если цена до <b class="num">${money(buy)}</b> (прибыль от ${money(pricing.min_profit)}).
    </p>
    <div class="example-flow">
      <div class="example-flow__step"><span>Купишь за</span><b class="num">${money(buy)}</b></div>
      <${Icon} name="arrow-right" size=${18} class="example-flow__arrow" />
      <div class="example-flow__step"><span>Продашь за</span><b class="num">~${money(market)}</b></div>
      <${Icon} name="arrow-right" size=${18} class="example-flow__arrow" />
      <div class="example-flow__step example-flow__step--profit"><span>Прибыль</span><b class="num">≈ ${money(profit, { sign: true })}</b></div>
    </div>
  </div>`;
}

/** Budget slider + linked number input (50–1500 €, step 10). */
export function BudgetField({ value, onChange, min = 50, max = 1500, step = 10 }) {
  return html`<div class="budget">
    <${Slider}
      value=${value}
      min=${min}
      max=${max}
      step=${step}
      tone="green"
      label="Сколько готов потратить на одну вещь"
      format=${(v) => money(v)}
      marks=${[
        { value: min, label: money(min) },
        { value: 500, label: money(500) },
        { value: 1000, label: money(1000) },
        { value: max, label: money(max) },
      ]}
      onChange=${onChange}
    />
    <div class="budget__input">
      <${NumberInput} value=${value} onChange=${(v) => onChange(v)} min=${min} max=${max} suffix="€" aria-label="Бюджет, евро" />
    </div>
  </div>`;
}

/** Manual thresholds (min profit €, min ROI %, risk margin %). */
export function ManualPricing({ pricing, onChange, errors = {} }) {
  const set = (k, v) => onChange({ ...pricing, [k]: v });
  return html`<div class="manual-pricing">
    <label class="mini-field">
      <span class="mini-field__label">Мин. прибыль<${HelpTip} title="Минимальная прибыль">Сколько евро должно остаться после продажи, чтобы я сказал «Покупай».<//></span>
      <${NumberInput} value=${pricing.min_profit} min=${0} max=${10000} onChange=${(v) => set("min_profit", v)} suffix="€" invalid=${errors.min_profit} />
    </label>
    <label class="mini-field">
      <span class="mini-field__label">Мин. ROI<${HelpTip} title="ROI">Сколько заработаешь на каждый вложенный евро. 25 % — купил за 100 €, заработал 25 €.<//></span>
      <${NumberInput} value=${pricing.min_roi != null ? Math.round(pricing.min_roi * 1000) / 10 : null} min=${0} max=${1000} onChange=${(v) => set("min_roi", v == null ? null : v / 100)} suffix="%" invalid=${errors.min_roi} />
    </label>
    <label class="mini-field">
      <span class="mini-field__label">Запас на риск<${HelpTip} title="Запас на торг и риск">Насколько ниже рынка считать цену продажи: торг покупателя, время, мелкие дефекты.<//></span>
      <${NumberInput} value=${pricing.safety_margin_percent} min=${0} max=${50} onChange=${(v) => set("safety_margin_percent", v)} suffix="%" invalid=${errors.safety_margin_percent} />
      ${pricing.safety_margin_percent > 30 && pricing.safety_margin_percent <= 50 && html`<span class="mini-field__warn">Больше 30 % — находок будет мало</span>`}
    </label>
  </div>`;
}

