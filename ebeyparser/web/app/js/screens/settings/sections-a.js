// Settings → Поиск и регион, Деньги, Нейросеть (brief §4.7.1–4.7.3).
import { html, useEffect, useState } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { everyLabel, number } from "../../lib/format.js";
import { useDebounced } from "../../lib/hooks.js";
import { Icon, Button, Toggle, NumberInput, Input, Select, Segmented, Slider, SettingRow, Meter, HelpTip, SecretInput, TestResult, Badge, ChipInput, IconButton } from "../../ui/index.js";
import { LocationPicker, RadiusSlider } from "../../setup/where.js";
import { StrategyCards, ExampleBox, DEFAULT_PRESETS, detectStrategy } from "../../setup/money.js";
import { AiConnect } from "../../setup/ai.js";
import { Group } from "./form.js";
import { ScoutGroup, ModelsGroup } from "./scout.js";

const INTERVALS = [10, 15, 20, 30, 45, 60, 90, 120];

function Advanced({ title = "Для продвинутых", children }) {
  const [open, setOpen] = useState(false);
  return html`<div class="advanced">
    <button type="button" class="disclosure" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <${Icon} name=${open ? "chevron-up" : "chevron-down"} size=${16} />${title}
    </button>
    ${open && html`<div class="advanced__body">${children}</div>`}
  </div>`;
}

/** Reset link «по умолчанию» next to advanced fields. */
function Reset({ form, path, value }) {
  const cur = form.bind(path).value;
  if (JSON.stringify(cur) === JSON.stringify(value)) return null;
  return html`<button type="button" class="reset-link" onClick=${() => form.set(path, value)}>по умолчанию</button>`;
}

// ============================================================ 1. Поиск и регион
export function RegionSection({ form }) {
  const d = form.draft;
  const interval = d.general.interval_minutes;
  const key = useDebounced(interval, 250);
  const [est, setEst] = useState(null);
  useEffect(() => {
    api
      .get("/setup/estimate", { params: { interval: key } })
      .then(setEst)
      .catch(() => setEst(null));
  }, [key]);
  const tone = est ? (est.level === "danger" ? "red" : est.level === "warn" ? "amber" : "green") : "green";
  const steps = INTERVALS.includes(interval) ? INTERVALS : [...INTERVALS, interval].sort((a, b) => a - b);
  return html`
    <${Group} title="Где искать" description="Город и радиус для поисков по категориям. Поменяю их во всех таких поисках разом." icon="map-pin">
      <${SettingRow} label="Город или индекс" error=${form.err("region.location")}>
        <${LocationPicker} value=${d.region.location} onChange=${(v) => form.set("region.location", v.location)} />
      <//>
      <${SettingRow} label="Радиус" wide>
        <${RadiusSlider} value=${d.region.radius_km} onChange=${(v) => form.set("region.radius_km", v)} />
      <//>
    <//>

    <${Group} title="Как часто проверять" description="Чаще — раньше узнаешь о находке, но больше запросов к Kleinanzeigen." icon="timer">
      <${SettingRow} label="Проверять" wide>
        <${Slider}
          value=${interval}
          steps=${steps}
          bubble="always"
          tone="green"
          label="Как часто проверять"
          format=${(v, short) => (short ? String(v) : everyLabel(v))}
          onChange=${(v) => form.set("general.interval_minutes", v)}
        />
      <//>
      ${est &&
      html`<div class="setting-meter">
        <${Meter}
          label="Нагрузка на Kleinanzeigen"
          value=${est.pages_per_hour}
          max=${est.cap_per_hour || 150}
          marker=${0.4}
          tone=${tone}
          valueText=${`~${number(est.pages_per_hour)} из ${number(est.cap_per_hour)} страниц в час`}
          hint=${tone === "green" ? "Безопасно — блокировки маловероятны" : est.suggested_interval ? `Слишком часто: лучше ${everyLabel(est.suggested_interval)}, чтобы оставались запросы на оценку объявлений` : "Слишком часто — есть риск блокировки"}
        />
      </div>`}
      <${SettingRow} kind="switch" label="Первая проверка — только обучение" help="Новый поиск сначала изучает цены и не присылает уведомлений. Рекомендуем.">
        ${(id) => html`<${Toggle} id=${id} checked=${d.general.baseline_first_run} onChange=${(v) => form.set("general.baseline_first_run", v)} />`}
      <//>
      <${SettingRow} label="Не смотреть объявления дешевле" help="Отсекает кабели, чехлы и мусор. Бесплатные объявления всё равно проверяются." error=${form.err("general.min_listing_price")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("general.min_listing_price")} suffix="€" min=${0} max=${10000} />`}
      <//>
    <//>

    <${Group} title="Защита от блокировки" description="Сколько запросов делать к Kleinanzeigen. Трогай, только если понимаешь, зачем." icon="shield">
      <${Advanced}>
        <${SettingRow} label="Страниц выдачи на поиск" tip=${html`<${Reset} form=${form} path="general.max_pages" value=${1} / error=${form.err("general.max_pages")}>`}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("general.max_pages")} min=${1} max=${20} />`}
        <//>
        <${SettingRow} label="Пауза между запросами" help="Случайная пауза от и до, в секундах" tip=${html`<${Reset} form=${form} path="general.request_delay_seconds" value=${[3, 7]} />`} error=${form.err("general.request_delay_seconds")}>
          <div class="pair">
            <${NumberInput} value=${d.general.request_delay_seconds[0]} onChange=${(v) => form.set("general.request_delay_seconds", [v ?? 0, d.general.request_delay_seconds[1]])} suffix="с" min=${0} max=${120} aria-label="Пауза от, секунд" />
            <span>—</span>
            <${NumberInput} value=${d.general.request_delay_seconds[1]} onChange=${(v) => form.set("general.request_delay_seconds", [d.general.request_delay_seconds[0], v ?? 0])} suffix="с" min=${0} max=${120} aria-label="Пауза до, секунд" />
          </div>
        <//>
        <${SettingRow} label="Открывать объявлений за проверку" tip=${html`<${Reset} form=${form} path="general.max_details_per_run" value=${40} / error=${form.err("general.max_details_per_run")}>`}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("general.max_details_per_run")} min=${0} max=${500} />`}
        <//>
        <${SettingRow} label="Поисков цен аналогов за проверку" tip=${html`<${Reset} form=${form} path="general.max_comps_lookups_per_run" value=${40} / error=${form.err("general.max_comps_lookups_per_run")}>`}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("general.max_comps_lookups_per_run")} min=${0} max=${500} />`}
        <//>
        <${SettingRow} label="Проверок нейросетью за раз" tip=${html`<${Reset} form=${form} path="general.max_ai_per_run" value=${30} / error=${form.err("general.max_ai_per_run")}>`}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("general.max_ai_per_run")} min=${0} max=${500} />`}
        <//>
        <${SettingRow} label="Не больше запросов в час" help="Жёсткий потолок на один сайт" tip=${html`<${Reset} form=${form} path="general.max_requests_per_hour" value=${150} />`} error=${form.err("general.max_requests_per_hour")}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("general.max_requests_per_hour")} min=${10} max=${1000} />`}
        <//>
        <${SettingRow} label="Паузы после блокировки" help="Часы для 1-й, 2-й, 3-й… блокировки подряд" tip=${html`<${Reset} form=${form} path="general.block_cooldown_hours" value=${[1, 2, 4, 12]} />`} error=${form.err("general.block_cooldown_hours")}>
          <${ChipInput}
            value=${(d.general.block_cooldown_hours || []).map(String)}
            onChange=${(v) => form.set("general.block_cooldown_hours", v.map((x) => Number(String(x).replace(",", "."))).filter((x) => x > 0))}
            placeholder="часы, например 1"
          />
        <//>
      <//>
    <//>
  `;
}

// ============================================================ 2. Деньги
export function MoneySection({ form }) {
  const d = form.draft;
  const p = d.pricing;
  const strategy = detectStrategy(p, DEFAULT_PRESETS);
  const setStrategy = (key) => {
    const preset = DEFAULT_PRESETS[key];
    form.set("pricing", { ...p, min_profit: preset.min_profit, min_roi: preset.min_roi, safety_margin_percent: preset.safety_margin_percent, min_comparables: preset.min_comparables });
    form.set("notifications.min_score", preset.notify_min_score);
  };
  return html`
    <${Group} title="Стратегия" description="Насколько выгодным должно быть объявление, чтобы я сказал «Покупай»." icon="scale" actions=${strategy === "custom" && html`<${Badge} tone="blue" size="sm">Свой<//>`}>
      <${StrategyCards} value=${strategy} onChange=${setStrategy} />
      <div class="setting-rows">
        <${SettingRow} label="Минимальная прибыль" help="Столько евро должно остаться после продажи" error=${form.err("pricing.min_profit")}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.min_profit")} suffix="€" min=${0} max=${10000} />`}
        <//>
        <${SettingRow} label="Минимальный ROI" tip=${html`<${HelpTip} title="ROI">Сколько заработаешь на каждый вложенный евро: 25 % — купил за 100 €, заработал 25 €.<//>`} error=${form.err("pricing.min_roi")}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.min_roi", { scale: 100 })} suffix="%" min=${0} max=${1000} />`}
        <//>
        <${SettingRow} label="Запас на торг и риск" help=${p.safety_margin_percent > 30 && p.safety_margin_percent <= 50 ? "Больше 30 % — очень осторожно: находок будет мало" : "Насколько ниже рынка считать цену продажи"} error=${form.err("pricing.safety_margin_percent")}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.safety_margin_percent")} suffix="%" min=${0} max=${50} />`}
        <//>
        <${SettingRow} label="Максимум на одну вещь" help="Никогда не советовать покупку дороже. Пусто — без ограничения." error=${form.err("pricing.max_capital")}>
          ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.max_capital")} suffix="€" placeholder="без ограничения" min=${0} max=${100000} />`}
        <//>
      </div>
      <${ExampleBox} pricing=${p} />
    <//>

    <${Group} title="Комиссии и доставка" description="Частные продавцы на eBay.de и Kleinanzeigen комиссий не платят." icon="receipt">
      <${SettingRow} label="Комиссия площадки при продаже" error=${form.err("pricing.selling_fee_percent")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.selling_fee_percent")} suffix="%" min=${0} max=${50} />`}
      <//>
      <${SettingRow} label="Комиссия за оплату" help="Например, 2,49 % за PayPal «Waren & Dienstleistungen»" error=${form.err("pricing.payment_fee_percent")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.payment_fee_percent")} suffix="%" min=${0} max=${20} />`}
      <//>
      <${SettingRow} label="Фиксированная часть комиссии PayPal" error=${form.err("pricing.paypal_fixed_fee")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.paypal_fixed_fee")} suffix="€" min=${0} max=${10} />`}
      <//>
      <${SettingRow} label="Моя доставка покупателю" help="Сколько тратишь на отправку, когда перепродаёшь" error=${form.err("pricing.default_shipping_cost")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.default_shipping_cost")} suffix="€" min=${0} max=${100} />`}
      <//>
    <//>

    <${Group} title="Оценка рынка" icon="chart-line">
      <${SettingRow} label="Скидка при «VB»" help="Объявления с торгом обычно уходят примерно на столько дешевле" error=${form.err("pricing.vb_expected_discount")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.vb_expected_discount", { scale: 100 })} suffix="%" min=${0} max=${50} />`}
      <//>
      <${SettingRow} label="Сравнений для уверенной оценки" help="Меньше похожих объявлений — самое большее «Подумай»" error=${form.err("pricing.min_comparables")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("pricing.min_comparables")} min=${1} max=${100} />`}
      <//>
      <${SettingRow} kind="switch" label="Учиться на истории цен" help="Каждое объявление пополняет историю — оценки без лишних запросов">
        ${(id) => html`<${Toggle} id=${id} checked=${p.use_price_history} onChange=${(v) => form.set("pricing.use_price_history", v)} />`}
      <//>
      <${SettingRow} kind="switch" label="Цены похожих объявлений на Kleinanzeigen">
        ${(id) => html`<${Toggle} id=${id} checked=${p.use_kleinanzeigen_comps} onChange=${(v) => form.set("pricing.use_kleinanzeigen_comps", v)} />`}
      <//>
      <${SettingRow} kind="switch" label="Реальные продажи на eBay">
        ${(id) => html`<${Toggle} id=${id} checked=${p.use_ebay_sold_comps} onChange=${(v) => form.set("pricing.use_ebay_sold_comps", v)} />`}
      <//>
    <//>

    <${ReferencePrices} form=${form} />
  `;
}

function ReferencePrices({ form }) {
  const rows = form.draft.pricing.reference_prices || [];
  const setRows = (next) => form.set("pricing.reference_prices", next);
  const update = (i, patch) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return html`<${Group}
    title="Справочник цен"
    description="Заполняй только если точно знаешь рынок: эта цена важнее найденных объявлений."
    icon="book-open"
    actions=${html`<${Button} size="sm" icon="plus" onClick=${() => setRows([...rows, { keywords: [], price: null, exclude: [] }])}>Добавить<//>`}
  >
    ${!rows.length && html`<p class="muted-line">Пока пусто — цены берутся из объявлений и истории.</p>`}
    <div class="ref-rows">
      ${rows.map(
        (r, i) => html`<div class="ref-row" key=${i}>
          <div class="ref-row__words">
            <span class="mini-field__label">Слова в заголовке</span>
            <${ChipInput} value=${r.keywords || []} onChange=${(v) => update(i, { keywords: v })} placeholder="например rtx, 3090" />
          </div>
          <div class="ref-row__price">
            <span class="mini-field__label">Цена</span>
            <${NumberInput} value=${r.price} onChange=${(v) => update(i, { price: v })} suffix="€" min=${1} max=${100000} aria-label="Цена" />
          </div>
          <${IconButton} icon="trash" label="Удалить строку" onClick=${() => setRows(rows.filter((_, j) => j !== i))} />
        </div>`,
      )}
    </div>
  <//>`;
}

// ============================================================ 3. Нейросеть
export function AiSection({ form }) {
  const d = form.draft;
  const ai = d.ai;
  const so = ai.second_opinion;
  const secrets = (form.settings && form.settings.secrets) || {};
  const [claudeKey, setClaudeKey] = useState("");
  const [keyState, setKeyState] = useState(null);
  const saveKey = async () => {
    try {
      await api.put("/secrets", { anthropic_api_key: claudeKey.trim() });
      setClaudeKey("");
      setKeyState({ state: "ok", title: "Ключ сохранён" });
      form.reload();
    } catch (e) {
      setKeyState({ state: "fail", title: e.message });
    }
  };
  return html`
    <${Group}
      title="Локальная нейросеть"
      description="Проверяет фото и описание — бесплатно, на твоём компьютере."
      icon="scan-eye"
      actions=${html`<${Toggle} checked=${ai.enabled} onChange=${(v) => form.set("ai.enabled", v)} label=${null} />`}
    >
      <p class="muted-line">
        Сейчас: ${ai.enabled ? html`<b>включена</b> · ${ai.provider === "ollama" ? "Ollama" : ai.provider === "anthropic" ? "Claude" : "LM Studio"} · <code>${ai.model}</code>` : html`<b>выключена</b> — уведомления приходят с пометкой ⚠ «фото не проверены»`}
      </p>
      <${AiConnect} save=${true} current=${{ model: ai.model, enabled: form.settings && form.settings.ai && form.settings.ai.enabled }} onDone=${() => form.reload()} />
    <//>

    <${ScoutGroup} form=${form} />
    <${ModelsGroup} form=${form} />

    <${Group} title="Как проверять" icon="sliders-horizontal">
      <${SettingRow} label="Фото на объявление" help="Больше фото — точнее, но медленнее">
        <${Segmented} value=${ai.max_images} onChange=${(v) => form.set("ai.max_images", v)} options=${[1, 2, 3, 4, 5].map((n) => ({ value: n, label: String(n) }))} label="Фото на объявление" block />
      <//>
      <${SettingRow} label="Проверять" help="«Перспективные» — пропускать явный мусор, чтобы экономить время">
        <${Segmented}
          value=${ai.run_for}
          onChange=${(v) => form.set("ai.run_for", v)}
          options=${[
            { value: "promising", label: "Перспективные" },
            { value: "all", label: "Все" },
          ]}
          block
        />
      <//>
      <${SettingRow} label="Ждать ответ до" help="Медленный ПК — ставь больше" error=${form.err("ai.timeout_seconds")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.timeout_seconds")} suffix="с" min=${5} max=${600} />`}
      <//>
      <${SettingRow} label="Уменьшать фото до" help="Меньше — быстрее" error=${form.err("ai.image_max_side")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.image_max_side")} suffix="px" min=${128} max=${4096} />`}
      <//>
      <${Advanced} title="Другой сервер">
        <${SettingRow} label="Сервер">
          ${(id) => html`<${Select}
            id=${id}
            value=${ai.provider}
            onChange=${(v) => form.set("ai.provider", v)}
            options=${[
              { value: "openai", label: "LM Studio / OpenAI-совместимый" },
              { value: "ollama", label: "Ollama" },
            ]}
          />`}
        <//>
        <${SettingRow} label="Адрес" error=${form.err("ai.base_url")}>
          ${(id) => html`<${Input} id=${id} ...${form.bind("ai.base_url")} class="mono" placeholder="http://localhost:1234/v1" />`}
        <//>
        <${SettingRow} label="Модель" error=${form.err("ai.model")}>
          ${(id) => html`<${Input} id=${id} ...${form.bind("ai.model")} class="mono" placeholder="название модели из LM Studio или Ollama" />`}
        <//>
      <//>
    <//>

    <${Group}
      title="Второе мнение (Claude)"
      description="Сильная облачная модель перепроверяет только лучшие находки. Платно, по твоему ключу."
      icon="sparkles"
      tone="violet"
      actions=${html`<${Toggle} checked=${so.enabled} onChange=${(v) => form.set("ai.second_opinion.enabled", v)} />`}
    >
      ${form.err("ai.second_opinion.enabled") && html`<${TestResult} state="fail" title=${form.err("ai.second_opinion.enabled")} />`}
      <${SettingRow} label="Ключ API Claude" help=${secrets.anthropic_api_key && secrets.anthropic_api_key.set ? `Сохранён: ${secrets.anthropic_api_key.masked}` : "Ключ хранится только на этом компьютере"}>
        <div class="row row--nowrap">
          <div class="grow"><${SecretInput} value=${claudeKey} onChange=${setClaudeKey} saved=${secrets.anthropic_api_key && secrets.anthropic_api_key.set} placeholder="sk-ant-…" class="mono" /></div>
          <${Button} disabled=${!claudeKey.trim()} onClick=${saveKey}>Сохранить<//>
        </div>
      <//>
      ${keyState && html`<${TestResult} state=${keyState.state} title=${keyState.title} />`}
      <${SettingRow} label="Проверять, если балл от" wide>
        <${Slider} value=${so.min_score} min=${0} max=${100} step=${5} bubble="always" label="Проверять, если балл от" format=${(v) => `${v} из 100`} onChange=${(v) => form.set("ai.second_opinion.min_score", v)} tone="violet" />
      <//>
      <${SettingRow} label="Не больше за проверку" help="Потолок платных запросов за один проход" error=${form.err("ai.second_opinion.max_per_run")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.second_opinion.max_per_run")} min=${0} max=${100} />`}
      <//>
    <//>
  `;
}

