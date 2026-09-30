// Onboarding step 8: Готово (brief §4.1.9) — summary, what happens next, «Запустить и открыть ленту».
// The summary is built from POST /searches/preview (exactly what will be created), and a re-run
// with existing searches asks «Заменить текущие поиски или добавить к ним?» (P0-1, P1-7).
import { html, cx, useEffect, useState } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate } from "../../lib/router.js";
import { loadApp } from "../../lib/app.js";
import { appStore, useStore } from "../../lib/store.js";
import { money, count, everyLabel, number } from "../../lib/format.js";
import { Icon, Button, Banner, Meter, Skeleton, ChoiceCards, toast } from "../../ui/index.js";
import { DEFAULT_PRESETS } from "../../setup/money.js";
import { radiusText } from "../../setup/where.js";
import "../../features/icons-extra.js";
import { draftStore, setupBody, clearDraft } from "./draft.js";
import { refreshMonitor, cooldownOf } from "../../shell/monitor.js";

function Row({ icon, state = "ok", text, sub, to, go }) {
  return html`<li class=${cx("summary-row", `is-${state}`)}>
    <span class="summary-row__icon"><${Icon} name=${state === "ok" ? "circle-check" : state === "skip" ? "circle-minus" : "triangle-alert"} size=${18} /></span>
    <span class="summary-row__main">
      <span class="summary-row__text"><${Icon} name=${icon} size=${16} class="summary-row__kind" />${text}</span>
      ${sub && html`<span class="summary-row__sub">${sub}</span>`}
    </span>
    <button type="button" class="summary-row__edit" onClick=${() => go(to)}>Изменить</button>
  </li>`;
}

const kindOf = (s) => s.kind || (s.purpose === "personal" ? "wishlist" : s.config && s.config.category_id && !s.config.query ? "category" : "keyword");

/** "Notebooks, Handy & Telefon и ещё 2" */
function names(list, max = 3) {
  const n = list.filter(Boolean);
  if (n.length <= max) return n.join(", ");
  return `${n.slice(0, max).join(", ")} и ещё ${n.length - max}`;
}

export function DoneStep({ draft, go }) {
  const { options } = useStore(draftStore);
  const app = useStore(appStore, (s) => s.app) || {};
  const [mode, setMode] = useState("add"); // re-run with existing searches: add | replace
  const [preview, setPreview] = useState(null);
  const [existing, setExisting] = useState(null); // current searches (names for the replace choice)
  const [state, setState] = useState({ state: "idle", error: null });

  const fromApp = Array.isArray(app.existing_searches) ? app.existing_searches.length : typeof app.existing_searches === "number" ? app.existing_searches : null;
  const existingCount =
    (preview && typeof preview.existing_count === "number" ? preview.existing_count : null) ??
    (options && options.current && typeof options.current.existing_searches === "number" ? options.current.existing_searches : null) ??
    fromApp ??
    (app.counts && app.counts.searches) ??
    0;
  const rerun = existingCount > 0;
  const replace = rerun && mode === "replace";

  useEffect(() => {
    let alive = true;
    setPreview((p) => (p ? { ...p, stale: true } : p));
    api
      .post("/searches/preview", setupBody(draft, { replace }))
      .then((p) => alive && setPreview(p))
      .catch((e) => alive && setPreview({ error: e }));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replace]);

  useEffect(() => {
    if (!rerun || existing) return;
    const known = (options && options.current && options.current.existing_search_names) || app.existing_search_names;
    if (Array.isArray(known) && known.length) setExisting(known.map((name) => ({ name })));
    else if (Array.isArray(app.existing_searches) && app.existing_searches.length && typeof app.existing_searches[0] === "object") setExisting(app.existing_searches);
    else
      api
        .get("/searches")
        .then((r) => setExisting((r && r.items) || []))
        .catch(() => setExisting([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rerun]);

  const presets = (options && options.presets) || DEFAULT_PRESETS;
  const created = preview && Array.isArray(preview.searches) ? preview.searches : null;
  const draftWishes = (draft.wishlist || []).filter((w) => w.item && w.item.trim()).length;
  const cats = created ? created.filter((s) => kindOf(s) === "category").length : (draft.category_ids || []).length;
  const wishes = created ? created.filter((s) => kindOf(s) === "wishlist" || s.purpose === "personal").length : draft.purpose === "resale" ? 0 : draftWishes;
  const others = created ? created.length - cats - wishes : 0;
  const lostWishes = draft.purpose === "resale" && draftWishes > 0;
  const strategy = draft.strategy === "custom" ? "Свои пороги" : (presets[draft.strategy] || {}).title_ru || "Сбалансированно";
  const est = preview && preview.estimate;
  const interval = preview && preview.interval_minutes;
  const problems = preview && preview.problems && Object.values(preview.problems);
  const nothing = !(cats || wishes || others);

  const launch = async () => {
    setState({ state: "loading", error: null });
    try {
      const res = await api.post("/setup", setupBody(draft, { replace, complete_onboarding: true, start_run: true }), { timeout: 60000 });
      await clearDraft();
      await loadApp();
      const replaced = (res && Array.isArray(res.replaced) && res.replaced.length ? res.replaced.map((s) => (typeof s === "string" ? s : s.name)) : replace ? (existing || []).map((s) => s.name) : []).filter(Boolean);
      // name only what is really gone: searches recreated with the same name are not "removed"
      const kept = new Set((created || []).map((s) => s.name));
      const removed = replaced.filter((n) => !kept.has(n));
      if (replace)
        toast.success(`Поиски заменены — теперь ${count((res && res.result_count) || (preview && preview.result_count) || (created && created.length) || 0, "поиск", "поиска", "поисков")}`, {
          message: removed.length ? `Убрал: ${names(removed, 4)}` : "Все прежние поиски созданы заново с новыми настройками",
          duration: 9000,
        });
      else {
        // a site pause means the first check waits: never promise «уже идёт» then (P1-23)
        await refreshMonitor().catch(() => {});
        const cd = cooldownOf((res && res.cooldown && { cooldown: res.cooldown }) || appStore.get().monitor);
        toast.success("Готово! Изучаю рынок", {
          message: cd
            ? `${cd.site} попросил паузу — первая проверка начнётся${cd.label ? ` ${/^\d/.test(cd.label) ? "в " : ""}${cd.label}` : " сразу после неё"}`
            : res && res.run_started
              ? "Первая проверка уже идёт — это 10–30 минут"
              : (res && res.message_ru) || "Первая проверка начнётся в течение пары минут",
        });
      }
      navigate("/", { replace: true });
    } catch (e) {
      setState({ state: "error", error: e });
    }
  };

  const tg = draft.telegram;
  const email = draft.email && draft.email.done;
  const notifyText = tg.done || tg.linked ? `Telegram${tg.bot && tg.bot.username ? ` · @${tg.bot.username}` : ""}` : email ? "Уведомления на почту" : "Уведомления — пропущено";
  const whatText =
    [cats ? count(cats, "категория", "категории", "категорий") : null, wishes ? count(wishes, "желание", "желания", "желаний") : null, others ? count(others, "поиск", "поиска", "поисков") : null]
      .filter(Boolean)
      .join(" · ") || "Ничего не выбрано";
  return html`<div class="done">
    <ul class="summary">
      <${Row} go=${go} to="where" icon="map-pin" text=${`${draft.location_label || draft.location} + ${radiusText(draft.radius_km)}`} />
      <${Row}
        go=${go}
        to="what"
        icon="layout-grid"
        state=${nothing ? "warn" : lostWishes ? "warn" : "ok"}
        text=${whatText}
        sub=${lostWishes
          ? `Список «для себя» (${count(draftWishes, "вещь", "вещи", "вещей")}) не сохранится — на шаге «Деньги» выбрана только перепродажа`
          : !rerun && preview && preview.summary_ru && !preview.stale
            ? `${preview.summary_ru}${preview.summary_ru.endsWith(".") ? "" : "."}`
            : ""}
      />
      <${Row} go=${go} to="money" icon="wallet" text=${`${strategy} · до ${money(draft.max_price)} за вещь`} />
      <${Row} go=${go} to="ai" icon="scan-eye" state=${draft.ai.done ? "ok" : "skip"} text=${draft.ai.done ? `Нейросеть: ${draft.ai.model || "подключена"}` : "Нейросеть — пропущено"} sub=${draft.ai.done ? "" : "Фото проверять не буду, уведомления придут с пометкой «фото не проверены»"} />
      <${Row} go=${go} to="telegram" icon=${email && !(tg.done || tg.linked) ? "mail" : "send"} state=${tg.done || tg.linked || email ? "ok" : "skip"} text=${notifyText} sub=${tg.done || tg.linked || email ? "" : "Находки будут только здесь, в ленте"} />
      <${Row} go=${go} to="ebay" icon="gavel" state=${draft.ebay.done ? "ok" : "skip"} text=${draft.ebay.done ? "eBay подключён" : "eBay — пропущено"} />
    </ul>

    ${rerun &&
    html`<section class="done__replace">
      <h2 class="done__next-title">Заменить текущие поиски или добавить к ним?</h2>
      <p class="done__replace-sub">
        Сейчас ${count(existingCount, "поиск", "поиска", "поисков")}${existing && existing.length ? html`: <b>${names(existing.map((s) => s.name))}</b>` : ""}.
        ${preview && preview.summary_ru && !preview.stale && html`<br /><span class="done__plan"><${Icon} name="info" size=${14} />${preview.summary_ru}.</span>`}
      </p>
      <${ChoiceCards}
        value=${mode}
        onChange=${setMode}
        columns=${2}
        options=${[
          { value: "add", label: "Добавить к текущим", icon: "copy-plus", description: `Старые поиски останутся, новые добавятся рядом` },
          { value: "replace", label: "Заменить", icon: "refresh-cw", tone: "amber", description: `Удалю ${count(existingCount, "текущий поиск", "текущих поиска", "текущих поисков")} и создам новые. Находки в ленте останутся` },
        ]}
      />
    </section>`}

    <div class="done__load">
      ${!preview && html`<${Skeleton} h=${46} radius="12px" />`}
      ${est &&
      html`<${Meter}
        label=${`Проверка ${everyLabel(interval)} · ${count(preview.result_count ?? preview.count ?? created?.length ?? 0, "поиск", "поиска", "поисков")}`}
        value=${est.pages_per_hour}
        max=${est.cap_per_hour || 150}
        marker=${0.4}
        tone=${est.level === "danger" ? "red" : est.level === "warn" ? "amber" : "green"}
        valueText=${est.pages_label_ru || `~${number(est.pages_per_hour)} из ${number(est.cap_per_hour || 150)} страниц в час`}
        hint=${est.short_ru || (est.level === "ok" ? "Безопасная нагрузка на Kleinanzeigen" : "Нагрузка высокая — проверять буду реже, чтобы не заблокировали")}
      />`}
      ${problems && problems.length > 0 && html`<${Banner} tone="amber">${problems[0]}<//>`}
      ${preview && preview.error && html`<${Banner} tone="amber" details=${preview.error.details}>Не получилось заранее посчитать поиски: ${preview.error.message}<//>`}
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
        <span class="timeline__icon glyph glyph--neutral"><${Icon} name=${tg.done || tg.linked ? "send" : email ? "mail" : "zap"} size=${18} /></span>
        <div><b>Нашёл выгодное — ${tg.done || tg.linked ? "пишу в Telegram" : email ? "пишу на почту" : "показываю в ленте"}</b> с фото и суммой, которую предложить.</div>
      </li>
    </ol>

    ${state.state === "error" &&
    html`<${Banner} tone="red" title="Не получилось сохранить" details=${state.error.details} action=${html`<${Button} size="sm" onClick=${launch}>Попробовать ещё раз<//>`}>
      ${state.error.message}${state.error.fields && Object.keys(state.error.fields).length ? ` (${Object.values(state.error.fields)[0]})` : ""}
    <//>`}

    <div class="done__cta">
      <${Button} variant="primary" size="lg" icon="rocket" loading=${state.state === "loading"} disabled=${nothing || (preview && preview.stale)} onClick=${launch}>
        ${replace ? "Заменить поиски и открыть ленту" : "Запустить и открыть ленту"}
      <//>
      <p class="done__cta-hint">${nothing ? "Выбери хотя бы одну категорию или вещь «для себя»." : "Все настройки можно поменять потом в разделе «Настройки»."}</p>
    </div>
  </div>`;
}
