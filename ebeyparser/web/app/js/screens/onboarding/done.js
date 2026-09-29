// Onboarding step 8: Готово (brief §4.1.9) — summary, what happens next, «Запустить и открыть ленту».
import { html, cx, useEffect, useState } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate } from "../../lib/router.js";
import { loadApp } from "../../lib/app.js";
import { money, count, everyLabel, number } from "../../lib/format.js";
import { useStore } from "../../lib/store.js";
import { Icon, Button, Banner, Meter, Skeleton, toast } from "../../ui/index.js";
import { DEFAULT_PRESETS } from "../../setup/money.js";
import { radiusText } from "../../setup/where.js";
import { draftStore, setupBody, clearDraft } from "./draft.js";

function Row({ icon, state = "ok", text, sub, to, go }) {
  return html`<li class=${cx("summary-row", `is-${state}`)}>
    <span class="summary-row__icon"><${Icon} name=${state === "ok" ? "circle-check" : state === "skip" ? "circle-dot" : "triangle-alert"} size=${18} /></span>
    <span class="summary-row__main">
      <span class="summary-row__text"><${Icon} name=${icon} size=${16} class="summary-row__kind" />${text}</span>
      ${sub && html`<span class="summary-row__sub">${sub}</span>`}
    </span>
    <button type="button" class="summary-row__edit" onClick=${() => go(to)}>Изменить</button>
  </li>`;
}

export function DoneStep({ draft, go }) {
  const { options } = useStore(draftStore);
  const [preview, setPreview] = useState(null);
  const [state, setState] = useState({ state: "idle", error: null });

  useEffect(() => {
    let alive = true;
    api
      .post("/searches/preview", setupBody(draft))
      .then((p) => alive && setPreview(p))
      .catch((e) => alive && setPreview({ error: e }));
    return () => {
      alive = false;
    };
  }, []);

  const presets = (options && options.presets) || DEFAULT_PRESETS;
  const cats = (draft.category_ids || []).length;
  const wishes = (draft.wishlist || []).filter((w) => w.item).length;
  const strategy = draft.strategy === "custom" ? "Свои пороги" : (presets[draft.strategy] || {}).title_ru || "Сбалансированно";
  const est = preview && preview.estimate;
  const interval = preview && preview.interval_minutes;
  const problems = preview && preview.problems && Object.values(preview.problems);

  const launch = async () => {
    setState({ state: "loading", error: null });
    try {
      const res = await api.post("/setup", setupBody(draft, { complete_onboarding: true, start_run: true }), { timeout: 60000 });
      await clearDraft();
      await loadApp();
      toast.success("Готово! Изучаю рынок", { message: res.run_started ? "Первая проверка уже идёт — это 10–30 минут" : res.message_ru });
      navigate("/", { replace: true });
    } catch (e) {
      setState({ state: "error", error: e });
    }
  };

  const tg = draft.telegram;
  const notifyText = tg.done || tg.linked ? `Telegram${tg.bot && tg.bot.username ? ` · @${tg.bot.username}` : ""}` : draft.email && draft.email.done ? "Почта" : "Уведомления — пропущено";
  return html`<div class="done">
    <ul class="summary">
      <${Row} go=${go} to="where" icon="map-pin" text=${`${draft.location_label || draft.location} + ${radiusText(draft.radius_km)}`} />
      <${Row}
        go=${go}
        to="what"
        icon="layout-grid"
        state=${cats || wishes ? "ok" : "warn"}
        text=${[cats ? count(cats, "категория", "категории", "категорий") : null, wishes ? count(wishes, "желание", "желания", "желаний") : null].filter(Boolean).join(" · ") || "Ничего не выбрано"}
      />
      <${Row} go=${go} to="money" icon="wallet" text=${`${strategy} · до ${money(draft.max_price)} за вещь`} />
      <${Row} go=${go} to="ai" icon="scan-eye" state=${draft.ai.done ? "ok" : "skip"} text=${draft.ai.done ? `Нейросеть: ${draft.ai.model || "подключена"}` : "Нейросеть — пропущено"} sub=${draft.ai.done ? "" : "Фото проверять не буду, уведомления придут с пометкой ⚠"} />
      <${Row} go=${go} to="telegram" icon="send" state=${tg.done || tg.linked || (draft.email && draft.email.done) ? "ok" : "skip"} text=${notifyText} />
      <${Row} go=${go} to="ebay" icon="gavel" state=${draft.ebay.done ? "ok" : "skip"} text=${draft.ebay.done ? "eBay подключён" : "eBay — пропущено"} />
    </ul>

    <div class="done__load">
      ${!preview && html`<${Skeleton} h=${46} radius="12px" />`}
      ${est &&
      html`<${Meter}
        label=${`Проверка ${everyLabel(interval)} · ${count(preview.count, "поиск", "поиска", "поисков")}`}
        value=${est.pages_per_hour}
        max=${est.cap_per_hour || 150}
        marker=${0.4}
        tone=${est.level === "danger" ? "red" : est.level === "warn" ? "amber" : "green"}
        valueText=${`~${number(est.requests_per_hour)} запросов в час`}
        hint=${est.level === "ok" ? "Безопасная нагрузка на Kleinanzeigen" : "Нагрузка высокая — проверять буду реже, чтобы не заблокировали"}
      />`}
      ${problems && problems.length > 0 && html`<${Banner} tone="amber">${problems[0]}<//>`}
    </div>

    <h2 class="done__next-title">Что будет дальше</h2>
    <ol class="timeline">
      <li>
        <span class="timeline__icon glyph glyph--blue"><${Icon} name="hourglass" size=${18} /></span>
        <div><b>Сейчас: изучаю рынок.</b> Первая проверка только собирает цены — уведомлений не будет. Это 10–30 минут.</div>
      </li>
      <li>
        <span class="timeline__icon glyph glyph--green"><${Icon} name="radar" size=${18} /></span>
        <div><b>Потом: ${interval ? everyLabel(interval) : "каждые 30 мин"}</b> смотрю только новые объявления.</div>
      </li>
      <li>
        <span class="timeline__icon glyph glyph--amber"><${Icon} name="send" size=${18} /></span>
        <div><b>Нашёл выгодное — пишу ${tg.done || tg.linked ? "в Telegram" : draft.email && draft.email.done ? "на почту" : "в ленту"}</b> с фото и суммой, которую предложить.</div>
      </li>
    </ol>

    ${state.state === "error" &&
    html`<${Banner} tone="red" title="Не получилось сохранить" action=${html`<${Button} size="sm" onClick=${launch}>Попробовать ещё раз<//>`}>
      ${state.error.message}${state.error.fields && Object.keys(state.error.fields).length ? ` (${Object.values(state.error.fields)[0]})` : ""}
    <//>`}

    <div class="done__cta">
      <${Button} variant="primary" size="lg" icon="rocket" loading=${state.state === "loading"} disabled=${!(cats || wishes)} onClick=${launch}>Запустить и открыть ленту<//>
      <p class="done__cta-hint">Все настройки можно поменять потом в разделе «Настройки».</p>
    </div>
  </div>`;
}
