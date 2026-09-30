// Settings pieces of the AI scout (docs/design/AI_SCOUT.md §10, §12): Нейросеть → «Разведчик» and
// «Какую модель поставить»; Уведомления → «Супер-находки сразу» and «Топ за день».
import { html, useEffect, useState } from "../../lib/html.js";
import { appStore, useStore } from "../../lib/store.js";
import { percent } from "../../lib/format.js";
import { Icon, Toggle, NumberInput, Input, Select, Segmented, SettingRow, Slider } from "../../ui/index.js";
import { refreshMonitor } from "../../shell/monitor.js";
import { ScoutLine, ScoutTest, ModelAdvice } from "../../features/scout.js";
import { Group } from "./form.js";

const MODES = [
  { value: "auto", label: "Авто" },
  { value: "all", label: "Все" },
  { value: "candidates", label: "Только непонятные" },
];
const MODE_HELP = {
  auto: "Читает все новые объявления, а когда не успевает — только те, что скрипт не понимает",
  all: "Каждое новое объявление — нужен быстрый сервер",
  candidates: "Только опечатки, комплекты, старые ПК и расплывчатые названия — для слабого сервера",
};

function Disclosure({ title, children, id }) {
  const [open, setOpen] = useState(false);
  return html`<div class="advanced" id=${id}>
    <button type="button" class="disclosure" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <${Icon} name=${open ? "chevron-up" : "chevron-down"} size=${16} />${title}
    </button>
    ${open && html`<div class="advanced__body">${children}</div>`}
  </div>`;
}

/** Settings → Нейросеть → «Разведчик». */
export function ScoutGroup({ form }) {
  const d = form.draft;
  const ai = d.ai;
  const sc = ai.scout || {};
  const saved = (form.settings && form.settings.ai && form.settings.ai.scout) || {};
  const status = useStore(appStore, (s) => s.monitor && s.monitor.raw && s.monitor.raw.scout);
  useEffect(() => {
    refreshMonitor();
  }, [saved.enabled, saved.model, saved.base_url, saved.mode]);
  const own = Boolean((sc.base_url || "").trim());
  const apply = ({ model, provider, base_url, enable }) => {
    if (model) form.set("ai.scout.model", model);
    if (provider && own) form.set("ai.scout.provider", provider);
    if (base_url && own) form.set("ai.scout.base_url", base_url);
    if (enable) form.set("ai.scout.enabled", true);
  };
  const testBody = {};
  if ((sc.model || "").trim()) testBody.model = sc.model.trim();
  if (own) Object.assign(testBody, { base_url: sc.base_url.trim(), provider: sc.provider });
  return html`<${Group}
    id="scout"
    title="Разведчик"
    description="Маленькая модель на сервере читает все объявления, большая на ПК смотрит фото. Разведчик находит то, что скрипт пропускает: опечатки, комплекты, старый ПК с дорогой видеокартой."
    icon="telescope"
    tone="blue"
    actions=${html`<${Toggle} checked=${sc.enabled} onChange=${(v) => form.set("ai.scout.enabled", v)} label=${null} />`}
  >
    ${status && html`<${ScoutLine} scout=${status} />`}
    <p class="muted-line">Цены разведчик не придумывает: деньги считаю только по истории объявлений. Если он выключен или не отвечает — всё работает как раньше.</p>
    <${SettingRow} label="Что читать" help=${MODE_HELP[sc.mode] || MODE_HELP.auto} wide>
      <${Segmented} value=${sc.mode || "auto"} onChange=${(v) => form.set("ai.scout.mode", v)} options=${MODES} label="Что читать" block />
    <//>
    <${SettingRow} label="Модель разведчика" help=${`Пусто — ${ai.model ? `как у нейросети для фото (${ai.model})` : "как у нейросети для фото"}. Хватит небольшой: Qwen3.5 2B или 4B`} error=${form.err("ai.scout.model")}>
      ${(id) => html`<${Input} id=${id} ...${form.bind("ai.scout.model")} class="mono" placeholder=${ai.model || "название модели из LM Studio или Ollama"} />`}
    <//>
    <${SettingRow} label="Адрес сервера" help=${`Пусто — тот же, что у нейросети для фото${ai.base_url ? ` (${ai.base_url})` : ""}. Свой — если разведчик работает на другом компьютере`} error=${form.err("ai.scout.base_url")}>
      ${(id) => html`<${Input} id=${id} ...${form.bind("ai.scout.base_url")} class="mono" placeholder=${ai.base_url || "http://127.0.0.1:1234/v1"} />`}
    <//>
    ${own &&
    html`<${SettingRow} label="Программа">
      ${(id) => html`<${Select}
        id=${id}
        value=${sc.provider}
        onChange=${(v) => form.set("ai.scout.provider", v)}
        options=${[
          { value: "openai", label: "LM Studio / llama.cpp (OpenAI-совместимый)" },
          { value: "ollama", label: "Ollama" },
        ]}
      />`}
    <//>`}
    <${ScoutTest} body=${testBody} onApply=${apply} onDone=${() => refreshMonitor()} />
    <${SettingRow} label="Ждать проверку фото" help="Если ПК с нейросетью для фото выключен, находка ждёт его столько минут, потом приходит с пометкой ⚠ «фото не проверены»" error=${form.err("ai.vision_wait_minutes")}>
      ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.vision_wait_minutes")} suffix="мин" min=${0} max=${1440} />`}
    <//>
    <${Disclosure} title="Для продвинутых">
      <${SettingRow} label="Объявлений за один запрос" help="0 — авто по модели (5 для маленькой, 10 для 4B и больше)" error=${form.err("ai.scout.batch_size")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.scout.batch_size")} min=${0} max=${32} placeholder="авто" />`}
      <//>
      <${SettingRow} label="Не больше в час" help="Потолок прочитанных объявлений — чтобы слабый сервер не был занят только разведчиком" error=${form.err("ai.scout.max_per_hour")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.scout.max_per_hour")} min=${0} max=${20000} suffix="в час" />`}
      <//>
      <${SettingRow} label="Доля времени проверки" wide help=${`Сколько от интервала проверки разведчик может читать: ${percent(sc.pass_share ?? 0.5)}`}>
        <${Slider} value=${Math.round((sc.pass_share ?? 0.5) * 100)} min=${5} max=${100} step=${5} bubble="always" format=${(v) => `${v} %`} label="Доля времени проверки" onChange=${(v) => form.set("ai.scout.pass_share", v / 100)} />
      <//>
      <${SettingRow} label="Минимальный интерес" help="0 — не отсеивать (рекомендую: маленькие модели плохо оценивают интерес). Выше — разведчик возвращает меньше объявлений" error=${form.err("ai.scout.min_interest")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("ai.scout.min_interest")} min=${0} max=${10} suffix="из 10" />`}
      <//>
    <//>
  <//>`;
}

/** Settings → Нейросеть → «Какую модель поставить» (GET /ai/recommend). */
export function ModelsGroup({ form }) {
  return html`<${Group} id="models" title="Какую модель поставить" description="Подберу модели под память этого компьютера или игрового ПК — с названиями для LM Studio и Ollama." icon="cpu">
    <${ModelAdvice} onUsed=${() => (form.dirty ? null : form.reload())} />
  <//>`;
}

/** Settings → Уведомления: «🔥 Супер-находки» and «Топ за день». */
export function AlertTiersGroups({ form }) {
  const n = form.draft.notifications;
  const sup = n.super_deals || {};
  const top = n.daily_top || {};
  return html`
    <${Group}
      title="Супер-находки — сразу"
      description="Самые выгодные находки приходят сразу, даже сверх лимита в час, с пометкой «Супер-находка»."
      icon="flame"
      actions=${html`<${Toggle} checked=${sup.enabled} onChange=${(v) => form.set("notifications.super_deals.enabled", v)} label=${null} />`}
    >
      <p class="muted-line">Супер — только «Покупай» с надёжной ценой рынка, проверенными фото и без тревожных признаков, если выполнены все условия ниже.</p>
      <${SettingRow} label="Прибыль от" error=${form.err("notifications.super_deals.min_profit")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("notifications.super_deals.min_profit")} min=${0} max=${100000} suffix="€" disabled=${!sup.enabled} />`}
      <//>
      <${SettingRow} label="ROI от" help="Сколько заработаешь на каждый вложенный евро: 80 % — вложил 100 €, получил 180 €" error=${form.err("notifications.super_deals.min_roi")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("notifications.super_deals.min_roi", { scale: 100 })} min=${0} max=${1000} suffix="%" disabled=${!sup.enabled} />`}
      <//>
      <${SettingRow} label="Балл от" wide>
        <${Slider} value=${sup.min_score ?? 85} min=${0} max=${100} step=${5} bubble="always" tone="green" label="Балл от" format=${(v) => `${v} из 100`} disabled=${!sup.enabled} onChange=${(v) => form.set("notifications.super_deals.min_score", v)} />
      <//>
    <//>

    <${Group}
      title="Топ за день"
      description="Раз в день — лучшие находки каждого поиска одним сообщением. Удобно, если отключаешь уведомления днём."
      icon="calendar-clock"
      actions=${html`<${Toggle} checked=${top.enabled} onChange=${(v) => form.set("notifications.daily_top.enabled", v)} label=${null} />`}
    >
      <${SettingRow} label="Когда присылать">
        ${(id) => html`<${Select}
          id=${id}
          value=${String(top.hour ?? 20)}
          disabled=${!top.enabled}
          onChange=${(v) => form.set("notifications.daily_top.hour", Number(v))}
          options=${Array.from({ length: 24 }, (_, h) => ({ value: String(h), label: `в ${String(h).padStart(2, "0")}:00` }))}
        />`}
      <//>
      <${SettingRow} label="Сколько с каждого поиска" error=${form.err("notifications.daily_top.per_search")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("notifications.daily_top.per_search")} min=${1} max=${20} disabled=${!top.enabled} />`}
      <//>
    <//>
  `;
}
