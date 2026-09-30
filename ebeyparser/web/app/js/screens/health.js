// Состояние — health (brief §4.6): one-sentence banner with a fix, 6 status tiles with actions
// (run now, pause/resume, test AI, test notifications), runs chart + table; /health/logs = logs.
import { html, cx, useState, useEffect } from "../lib/html.js";
import { useAsync, useNow, useMediaQuery, BREAKPOINTS } from "../lib/hooks.js";
import { api, humanize } from "../lib/api.js";
import { onEvent } from "../lib/events.js";
import { navigate } from "../lib/router.js";
import { ago, number, plural, span, bytes, dateTime, untilTime, whenTime, localizeText } from "../lib/format.js";
import { Icon, Button, PageHeader, ErrorState, Skeleton, Tooltip, Meter, toast, Tabs, Details } from "../ui/index.js";
import { useTopbar } from "../shell/topbar.js";
import { refreshMonitor, monitorAction, cooldownOf, normalize } from "../shell/monitor.js";
import "../features/icons-extra.js";
import { setBadge } from "../features/badges.js";
import { LogsView } from "./health/logs.js";
import { scoutState, scoutSpeed } from "../features/scout.js";

const LEVEL_TONE = { ok: "profit", warn: "haggle", error: "danger" };
/** Server text → { message, details }: Berlin times instead of ISO, no CLI / exception text. */
const siteNames = (text) =>
  String(text || "")
    .replace(/\b(?:https?:\/\/)?(?:www\.)?kleinanzeigen\.de\b/gi, "Kleinanzeigen")
    .replace(/\b(?:https?:\/\/)?(?:api\.|www\.)?ebay\.(?:com|de)\b/gi, "eBay");
const human = (text) => humanize(localizeText(siteNames(text)));
const CHANNEL_RU = { telegram: "Telegram", email: "почта", mail: "почта" };

/** GET /health `monitor` + `sites` → the normalised monitor used by the shell helpers. */
function monitorOf(d) {
  const m = d.monitor || {};
  return normalize({ ...m, cooldown: m.cooldown || d.cooldown || null, http: m.http && m.http.length ? m.http : d.sites || [] });
}

function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export default function HealthScreen({ params = {} }) {
  const logs = (params.rest || "").startsWith("logs");
  useTopbar({ title: "Состояние" }, []);
  return html`<div class="health-screen">
    <${PageHeader} title="Состояние" subtitle="Всё ли работает — и что сделать, если нет" />
    <${Tabs}
      value=${logs ? "logs" : "overview"}
      onChange=${(v) => navigate(v === "logs" ? "/health/logs" : "/health", { scroll: false })}
      items=${[
        { value: "overview", label: "Обзор", icon: "activity" },
        { value: "logs", label: "Журнал", icon: "scroll-text" },
      ]}
      label="Раздел"
    />
    ${logs ? html`<${LogsView} />` : html`<${Overview} />`}
  </div>`;
}

// ------------------------------------------------------------------ overview
function useHealth() {
  // fast answer first (no AI probe), then the full one with the AI check
  const [tick, setTick] = useState(0);
  const fast = useAsync(() => api.get("/health", { params: { ai: 0 } }), [tick]);
  const full = useAsync(() => api.get("/health", { timeout: 20000 }), [tick]);
  useEffect(() => {
    const again = () => setTick((n) => n + 1);
    const offs = ["run_started", "run_finished", "monitor_paused", "monitor_resumed", "health_alert", "settings_changed"].map((t) => onEvent(t, again));
    const poll = setInterval(() => !document.hidden && again(), 60000);
    return () => {
      offs.forEach((off) => off());
      clearInterval(poll);
    };
  }, []);
  const data = full.data || fast.data;
  useEffect(() => {
    if (data) setBadge("health", data.level === "error" ? "dot-red" : data.level === "warn" ? "dot-amber" : 0);
  }, [data && data.level]);
  return { data, aiLoading: full.loading && !full.data, error: !data && (fast.error || full.error), reload: () => setTick((n) => n + 1) };
}

function Overview() {
  const h = useHealth();
  const now = useNow(1000);
  if (h.error) return html`<${ErrorState} error=${h.error} onRetry=${h.reload} />`;
  if (!h.data) return html`<${OverviewSkeleton} />`;
  const d = h.data;
  const n = d.notifications || {};
  const channels = (n.channels || []).filter((c) => c.enabled && c.configured !== false);
  const anyChannel = n.any_channel ?? channels.length > 0;
  return html`
    <${TopBanner} d=${d} reload=${h.reload} now=${now} />
    <div class="htiles">
      <${ChecksTile} d=${d} now=${now} reload=${h.reload} />
      <${AiTile} d=${d} loading=${h.aiLoading} reload=${h.reload} />
      ${d.cloud && d.cloud.enabled && html`<${CloudTile} d=${d} now=${now} />`}
      <${SiteTile} d=${d} now=${now} />
      <${EbayTile} d=${d} />
      <${NotifyTile} d=${d} />
      <${QueueTile} d=${d} />
      <${ScoutTile} d=${d} reload=${h.reload} />
    </div>
    <${Runs} runs=${d.runs || []} />
    <p class="heartbeat">
      <${Icon} name="bell-ring" size=${16} />
      ${!anyChannel
        ? html`Утренний отчёт «жив» приходит в Telegram или на почту — <a href="/settings/notifications">подключи уведомления</a>, чтобы знать, что программа работает.`
        : n.heartbeat_hour != null && n.heartbeat_active !== false
          ? `Каждый день в ${d.notifications.heartbeat_hour}:00 пришлю отчёт «жив». Нет отчёта — значит, компьютер или программа выключены.`
          : html`Утренний отчёт «жив» выключен — <a href="/settings/notifications">включи его</a>, чтобы знать, что программа работает.`}
    </p>
  `;
}

/** The server's `action` ({label_ru, href}) as a button; `href` may also name a local action. */
function ServerAction({ action, reload }) {
  const label = action.label_ru || action.label || "Открыть";
  let href = action.href || "";
  // a link back to this very page means "do it here": infer the action from its label
  if (!href || href === "/health" || href === window.location.pathname) {
    if (/продолж/i.test(label)) href = "action:resume";
    else if (/проверить сейчас|запустить проверку/i.test(label)) href = "action:run";
    else if (/нейросет|модел/i.test(label) && /провер/i.test(label)) href = "action:test-ai";
    else if (/тест|отправить/i.test(label)) href = "action:test-notify";
  }
  if (href === "action:test-notify") return html`<${Button} size="sm" variant="secondary" icon="send" onClick=${() => testNotify()}>${label}<//>`;
  const icon = /resume|продолж/i.test(href + label) ? "play" : /settings\/region|нагрузк/i.test(href + label) ? "sliders-horizontal" : /log|журнал/i.test(href + label) ? "scroll-text" : "arrow-right";
  if (/^(action:|#)?resume$/i.test(href) || href === "/monitor/resume")
    return html`<${Button} size="sm" variant="secondary" icon="play" onClick=${() => monitorAction("resume").then(reload)}>${label}<//>`;
  if (/^(action:|#)?(test[-_]?ai|ai[-_]?test)$/i.test(href)) return html`<${Button} size="sm" variant="secondary" icon="scan-eye" onClick=${() => testAi(reload)}>${label}<//>`;
  if (/^(action:|#)?run$/i.test(href) || href === "/monitor/run")
    return html`<${Button} size="sm" variant="secondary" icon="refresh-cw" onClick=${() => monitorAction("run").then(reload)}>${label}<//>`;
  const to = href.replace(/^\/settings\/search\b/, "/settings/region");
  return html`<${Button} size="sm" variant="secondary" icon=${icon} href=${to || "/settings"}>${label}<//>`;
}

function TopBanner({ d, reload, now }) {
  const m = monitorOf(d);
  const cd = cooldownOf(m, now);
  let level = d.level;
  let banner = human(d.banner_ru);
  const firstDetails = ((d.problems || [])[0] || {}).details;
  if (firstDetails && !banner.details) banner = { ...banner, details: firstDetails };
  // older servers say «Всё работает» while checks are stopped or paused: be truthful anyway
  const onlyPause = (d.problems || []).every((p) => /пауз|огранич|блокир|cooldown/i.test(p.text_ru || ""));
  if (cd && (level === "ok" || (level === "error" && onlyPause))) {
    // a block pause is waited out by itself: calm amber, one sentence, when it ends
    level = "warn";
    banner = { message: `${cd.site} попросил паузу — продолжу${cd.label ? ` ${/^\d/.test(cd.label) ? "в " : ""}${cd.label}` : " сам"}. Ничего делать не нужно.`, details: banner.details };
  } else if (level === "ok" && m.state === "stopped") {
    level = "warn";
    banner = { message: "Автопроверка выключена — новые объявления смотрю только по кнопке «Проверить сейчас»", details: "" };
  } else if (level === "ok" && m.paused) {
    level = "warn";
    banner = { message: "Проверки на паузе — новые объявления не смотрю", details: "" };
  }
  const tone = LEVEL_TONE[level] || "neutral";
  const first = (d.problems || [])[0];
  let action = null;
  if (d.action && (d.action.href || d.action.label_ru)) action = html`<${ServerAction} action=${d.action} reload=${reload} />`;
  else if (m.paused) action = html`<${Button} size="sm" variant="secondary" icon="play" onClick=${() => monitorAction("resume").then(reload)}>Продолжить<//>`;
  else if (first || cd) {
    const t = (first && first.text_ru) || "";
    if (/нейросет|модел|lm studio/i.test(t)) action = html`<${Button} size="sm" variant="secondary" icon="scan-eye" onClick=${() => testAi(reload)}>Проверить нейросеть<//>`;
    else if (cd || /огранич|лимит|пауз|блок/i.test(t)) action = html`<${Button} size="sm" variant="secondary" icon="sliders-horizontal" href="/settings/region">Снизить нагрузку<//>`;
    else if (/не доставлено/i.test(t)) action = html`<${Button} size="sm" variant="secondary" icon="send" onClick=${() => testNotify()}>Отправить тест<//>`;
    else if (/ошиб/i.test(t)) action = html`<${Button} size="sm" variant="secondary" icon="scroll-text" href="/health/logs">Открыть журнал<//>`;
  }
  const more = (d.problems || [])
    .slice(1)
    .map((p) => human(p.text_ru).message)
    .filter((t) => t && t !== banner.message);
  return html`<section class=${cx("hbanner", `tone-${tone}`)} role=${tone === "danger" ? "alert" : "status"}>
    <span class="hbanner__icon"><${Icon} name=${level === "ok" ? "circle-check" : level === "warn" ? "triangle-alert" : "circle-alert"} size=${22} /></span>
    <div class="hbanner__body">
      <div class="hbanner__title">${banner.message}</div>
      ${more.length > 0 && html`<ul class="hbanner__more">${more.map((t) => html`<li>${t}</li>`)}</ul>`}
      <${Details} text=${banner.details} />
    </div>
    ${action}
  </section>`;
}

function Tile({ icon, title, tone = "neutral", state, children, actions, tip, wide = false }) {
  return html`<section class=${cx("htile", wide && "htile--wide")}>
    <header class="htile__head">
      <span class=${cx("htile__icon", `tone-${tone}`)}><${Icon} name=${icon} size=${20} /></span>
      <div class="htile__titles">
        <h3>${title}${tip && html` <${Tooltip} text=${tip}><${Icon} name="circle-help" size=${14} class="htile__tip" /><//>`}</h3>
        <div class=${cx("htile__state", `tone-${tone}`)}><span class="sdot"></span><span class="htile__state-text">${state}</span></div>
      </div>
    </header>
    <div class="htile__body">${children}</div>
    ${actions && html`<footer class="htile__actions">${actions}</footer>`}
  </section>`;
}

function ChecksTile({ d, now, reload }) {
  const m = d.monitor || {};
  const cd = cooldownOf(monitorOf(d), now);
  const [busy, setBusy] = useState(null);
  const act = async (kind) => {
    setBusy(kind);
    await monitorAction(kind);
    setBusy(null);
    refreshMonitor();
    reload();
  };
  const left = m.next_run_at ? (new Date(m.next_run_at) - now) / 1000 : null;
  let tone = "profit";
  let state = localizeText(m.state_ru) || "—";
  if (!m.available) tone = "neutral";
  else if (m.running) tone = "info";
  else if (m.paused) tone = "haggle";
  else if (m.state === "stopped") tone = "neutral";
  else if (cd) {
    // never promise a check inside a block pause
    tone = "haggle";
    state = cd.label ? html`Пауза до <b class="num">${cd.label}</b> · ${cd.site} попросил отдохнуть` : `Пауза · ${cd.site} попросил отдохнуть`;
  } else if (left != null && left > 0) state = html`Работает · следующая через <b class="num">${clock(left)}</b>`;
  const p = m.progress || null;
  const last = m.last_summary;
  return html`<${Tile}
    icon="radar"
    title="Проверки"
    tone=${tone}
    state=${state}
    actions=${m.available &&
    html`${!m.running && html`<${Button} size="sm" variant="secondary" icon="refresh-cw" loading=${busy === "run"} onClick=${() => act("run")}>Проверить сейчас<//>`}
      ${m.paused
        ? html`<${Button} size="sm" variant="ghost" icon="play" loading=${busy === "resume"} onClick=${() => act("resume")}>Продолжить<//>`
        : html`<${Button} size="sm" variant="ghost" icon="pause" loading=${busy === "pause"} onClick=${() => act("pause")}>Пауза<//>`}`}
  >
    ${m.running && p && p.total
      ? html`<div class="hmetric"><span>Сейчас</span><b>поиск ${p.index || 0} из ${p.total}</b></div>
          <div class="gauge"><span style=${{ width: `${Math.min(100, ((p.index || 0) / p.total) * 100)}%`, background: "var(--blue-solid)" }}></span></div>`
      : null}
    <div class="hmetric"><span>Как часто</span><b>${m.state === "stopped" ? "только по кнопке" : `каждые ${Math.round(m.interval_minutes || 0)} мин`}</b></div>
    <div class="hmetric"><span>Активных поисков</span><b class="num">${m.searches_enabled ?? "—"}</b></div>
    ${last &&
    html`<div class="hmetric"><span>Последняя</span><b>${ago(last.finished_at || last.started_at, now)} · ${number(last.new_listings || 0)} новых, ${number(last.deals_found || 0)} выгодных</b></div>`}
  <//>`;
}

async function testAi(reload) {
  const id = toast({ kind: "loading", title: "Жду ответа от нейросети… (до пары минут на слабом ПК)" });
  try {
    const r = await api.post("/ai/test", { sample: true }, { timeout: 200000 });
    if (r.ok) toast({ id, kind: "success", title: r.message_ru || `Нейросеть работает · ответ за ${Math.round(r.seconds || 0)} с`, message: r.warning_ru || "" });
    else {
      const e = human(r.error_ru || r.message_ru || "Нейросеть не ответила");
      toast({ id, kind: "error", title: e.message, message: r.warning_ru || "", details: e.details });
    }
  } catch (e) {
    toast({ id, kind: "error", title: e.message, details: e.details });
  }
  reload && reload();
}

async function testNotify(channel) {
  const id = toast({ kind: "loading", title: "Отправляю тестовое уведомление…" });
  try {
    const r = await api.post("/notify/test", {}, { params: channel ? { channel } : null, timeout: 60000 });
    const res = Object.entries(r.results || {});
    const bad = res.filter(([, v]) => !v.ok);
    const name = (k) => CHANNEL_RU[k] || k;
    if (!bad.length) toast({ id, kind: "success", title: "Тест отправлен — проверь телефон", message: res.length ? `Куда: ${res.map(([k]) => name(k)).join(", ")}` : "" });
    else {
      const parts = bad.map(([k, v]) => ({ k, h: human(v.message_ru || v.error_ru) }));
      toast({ id, kind: "error", title: parts.map(({ k, h }) => `${name(k)[0].toUpperCase()}${name(k).slice(1)}: ${h.message}`).join(" · "), details: parts.map(({ h }) => h.details).filter(Boolean).join("\n") });
    }
  } catch (e) {
    toast({ id, kind: "error", title: e.message, details: e.details });
  }
}

function AiTile({ d, loading, reload }) {
  const ai = d.ai;
  const [busy, setBusy] = useState(false);
  let tone = "neutral";
  let state = "Проверяю…";
  if (ai) {
    if (!ai.enabled) state = "Выключена";
    else if (ai.ok === true) (tone = "profit"), (state = "Работает");
    else if (ai.ok === false) (tone = "danger"), (state = "Не отвечает");
    else state = ai.state_ru || "Неизвестно";
  } else if (!loading) state = "Неизвестно";
  return html`<${Tile}
    icon="scan-eye"
    title="Нейросеть"
    tone=${tone}
    state=${state}
    actions=${html`<${Button} size="sm" variant="secondary" icon="play" loading=${busy} disabled=${ai && !ai.enabled} onClick=${async () => {
        setBusy(true);
        await testAi(reload);
        setBusy(false);
      }}>Проверить<//>
      <${Button} size="sm" variant="ghost" href="/settings/ai">${ai && !ai.enabled ? "Включить" : "Сменить модель"}<//>`}
  >
    ${loading && !ai
      ? html`<${Skeleton} w="80%" h=${12} /><${Skeleton} w="60%" h=${12} />`
      : ai &&
        html`${ai.enabled
          ? html`<div class="hmetric"><span>Модель</span><code class="mono">${ai.resolved_model || ai.model || "—"}</code></div>
              <div class="hmetric"><span>Сервер</span><b>${ai.cloud_name ? `${ai.cloud_name} (облако)` : ai.provider === "ollama" ? "Ollama" : ai.provider === "anthropic" ? "Claude" : "LM Studio"}</b></div>
              ${ai.latency_ms != null && ai.ok !== false && html`<div class="hmetric"><span>Ответ сервера</span><b class="num">${ai.latency_ms < 1000 ? `${ai.latency_ms} мс` : `${(ai.latency_ms / 1000).toFixed(1).replace(".", ",")} с`}</b></div>`}
              ${ai.error_ru && html`<div class="htile__err">${human(ai.error_ru).message}<${Details} text=${human(ai.error_ru).details || ai.details} /></div>`}`
          : html`<p class="htile__note">Без нейросети фото никто не проверяет — уведомления приходят с пометкой ⚠.</p>`}
        ${d.monitor && d.monitor.last_summary && html`<div class="hmetric"><span>Вызовов за проверку</span><b class="num">${d.monitor.last_summary.ai_calls || 0}</b></div>`}`}
  <//>`;
}

const CLOUD_STATE = {
  ok: { tone: "profit", label: "Работает" },
  flaky: { tone: "haggle", label: "Бывают отказы" },
  low: { tone: "haggle", label: "Лимит почти потрачен" },
  limited: { tone: "danger", label: "Лимит исчерпан" },
};

/** «Облако» (docs/design/CLOUD_AI.md): the free cloud AI's usage today, 429s, the local stand-in. */
function CloudTile({ d, now }) {
  const c = d.cloud;
  const st = CLOUD_STATE[c.state] || CLOUD_STATE.ok;
  const state = c.limited && c.fallback_active ? "Лимит исчерпан · работает компьютер" : c.limited && c.limited_reason === "cooldown" ? "Пауза" : st.label;
  const limit = c.daily_limit;
  const used = c.used_today || 0;
  const other = (c.endpoints || []).slice(1);
  return html`<${Tile}
    icon="cloud"
    title=${`Облако · ${c.provider_name || ""}`}
    tone=${c.limited && c.fallback_active ? "haggle" : st.tone}
    state=${state}
    tip="Бесплатная нейросеть в облаке: сколько запросов потрачено сегодня, сколько осталось, и работает ли компьютер про запас."
    actions=${html`<${Button} size="sm" variant="ghost" href="/settings/ai#cloud">Настроить<//>`}
  >
    ${limit
      ? html`<${Meter} label="Запросов сегодня" value=${Math.min(used, limit)} max=${limit} valueText=${`${number(Math.min(used, limit))} из ${number(limit)}`} marker=${0.8} />`
      : html`<div class="hmetric"><span>Запросов сегодня</span><b class="num">${number(used)}</b></div>`}
    ${c.limited_ru && html`<p class="htile__lead t-amber">${human(c.limited_ru).message}</p>`}
    ${c.next_reset && html`<div class="hmetric"><span>Лимит обнулится</span><b>${whenTime(c.next_reset, now)}</b></div>`}
    ${c.rpm && html`<div class="hmetric"><span>В минуту</span><b class="num">до ${c.rpm} запросов</b></div>`}
    <div class="hmetric"><span>«Подожди» от сервиса</span><b class=${cx("num", c.count_429_today > 0 && "t-amber")}>${number(c.count_429_today || 0)}</b></div>
    <div class="hmetric"><span>Компьютер про запас</span><b class=${cx(c.fallback_active && "t-amber")}>${!c.fallback_configured ? "не настроен" : c.fallback_active ? "работает сейчас" : "готов"}</b></div>
    ${c.estimate_ru && !c.limited && html`<p class="htile__note"><${Icon} name="gauge" size=${14} /><span>${human(c.estimate_ru).message}</span></p>`}
    ${other.map((e) => html`<div class="hmetric"><span>${e.provider_name}</span><b class="num">${e.daily_limit ? `${number(e.used_today || 0)} из ${number(e.daily_limit)}` : number(e.used_today || 0)}</b></div>`)}
  <//>`;
}

function SiteTile({ d, now }) {
  const site = (d.sites || []).find((s) => /kleinanzeigen/i.test(s.host)) || null;
  const cooldown = site && site.cooldown_until && new Date(site.cooldown_until) > now ? site.cooldown_until : null;
  const tone = !site ? "neutral" : cooldown ? "haggle" : site.blocked ? "danger" : site.exhausted ? "haggle" : "profit";
  const state = !site
    ? "Запросов ещё не было"
    : cooldown
      ? html`Пауза до ${untilTime(cooldown, now)} · ещё <b class="num">${clock((new Date(cooldown) - now) / 1000)}</b>`
      : site.exhausted
        ? "Лимит в час исчерпан — продолжу позже"
        : "Работает";
  return html`<${Tile}
    icon="store"
    title="Kleinanzeigen"
    tone=${tone}
    state=${state}
    tip="Я ограничиваю число запросов в час, чтобы Kleinanzeigen не заблокировал. После блокировки сам делаю паузу."
    actions=${html`<${Button} size="sm" variant="ghost" icon="sliders-horizontal" href="/settings/region">Снизить нагрузку<//>`}
  >
    ${site
      ? html`<${Meter} label="Запросов за час" value=${site.requests_last_hour || 0} max=${site.limit_per_hour || 150} marker=${0.4} />
          ${site.strikes > 0 && html`<div class="hmetric"><span>Блокировок подряд</span><b class="num">${site.strikes}</b></div>`}
          ${site.last_block_reason &&
          html`<div class="hmetric"><span>Последняя блокировка</span><b>${blockReason(site.last_block_reason)}${site.last_block_at ? ` · ${ago(site.last_block_at, now)}` : ""}</b></div>`}`
      : html`<p class="htile__note">Статистика появится после первой проверки.</p>`}
  <//>`;
}

function EbayTile({ d }) {
  const e = d.ebay || {};
  const site = (d.sites || []).find((s) => /ebay/i.test(s.host));
  return html`<${Tile}
    icon="gavel"
    title="eBay"
    tone=${e.configured ? "profit" : "neutral"}
    state=${e.configured ? "Подключён" : "Не подключён"}
    actions=${e.configured
      ? html`<${Button} size="sm" variant="ghost" href="/settings/ebay">Настройки eBay<//>`
      : html`<${Button} size="sm" variant="secondary" icon="plus" href="/settings/ebay">Подключить<//>`}
  >
    ${e.configured
      ? html`<div class="hmetric"><span>Площадка</span><b>${e.marketplace_id || "EBAY_DE"}</b></div>
          ${site && html`<div class="hmetric"><span>Запросов за час</span><b class="num">${site.requests_last_hour}${site.limit_per_hour ? ` / ${site.limit_per_hour}` : ""}</b></div>`}`
      : html`<p class="htile__note">Аукционы и «Sofort-Kaufen» с eBay.de — через официальный API, бесплатно.</p>`}
  <//>`;
}

function NotifyTile({ d }) {
  const n = d.notifications || { channels: [] };
  const [busy, setBusy] = useState(false);
  const active = (n.channels || []).filter((c) => c.enabled);
  const failed = active.reduce((s, c) => s + (c.failed_24h || 0), 0);
  const tone = !active.length ? "neutral" : failed ? "haggle" : "profit";
  const state = !active.length ? "Не настроены" : failed ? `Не доставлено: ${failed}` : "Работают";
  return html`<${Tile}
    icon="send"
    title="Уведомления"
    tone=${tone}
    state=${state}
    actions=${html`<${Button} size="sm" variant="secondary" icon="send" loading=${busy} disabled=${!active.length} onClick=${async () => {
        setBusy(true);
        await testNotify();
        setBusy(false);
      }}>Отправить тест<//>
      <${Button} size="sm" variant="ghost" href="/settings/notifications">Настроить<//>`}
  >
    ${(n.channels || []).map(
      (c) => html`<div class="hmetric">
        <span>${c.label}</span>
        <b class=${cx(!c.enabled && "muted", c.failed_24h > 0 && "t-amber")}>
          ${!c.enabled ? "выключено" : !c.configured ? "не настроено" : c.failed_24h ? `ошибки: ${c.failed_24h}` : c.last_delivery_at ? `✓ последнее ${ago(c.last_delivery_at)}` : "✓ готово"}
        </b>
      </div>`,
    )}
    ${n.queued > 0 && html`<div class="hmetric"><span>В очереди</span><b class="num">${n.queued}</b></div>`}
    ${active.find((c) => c.last_error) &&
    html`<div class="htile__err">${human(active.find((c) => c.last_error).last_error).message}<${Details} text=${human(active.find((c) => c.last_error).last_error).details || active.find((c) => c.last_error).last_error_details} /></div>`}
  <//>`;
}

function QueueTile({ d }) {
  const b = d.backlog || {};
  const st = d.storage || {};
  return html`<${Tile}
    icon="hourglass"
    title="Очередь оценки"
    tone=${b.expired_24h ? "haggle" : "profit"}
    state=${b.pending ? `ждут оценки ${number(b.pending)}` : "Очередь пуста"}
    tip="За одну проверку я открываю ограниченное число объявлений и вызовов нейросети. Остальные ждут следующей проверки; слишком старые пропускаю."
  >
    <div class="hmetric"><span>Ждут оценки</span><b class="num">${number(b.pending || 0)}</b></div>
    <div class="hmetric"><span>Не успел за 24 ч</span><b class=${cx("num", b.expired_24h > 0 && "t-amber")}>${number(b.expired_24h || 0)}</b></div>
    ${st.db_bytes != null && html`<div class="hmetric"><span>База данных</span><b>${bytes(st.db_bytes)}${st.free_bytes != null ? ` · свободно ${bytes(st.free_bytes)}` : ""}</b></div>`}
    ${d.uptime_seconds != null && html`<div class="hmetric"><span>Программа работает</span><b>${span(d.uptime_seconds)}</b></div>`}
  <//>`;
}

async function testScout(reload) {
  const id = toast({ kind: "loading", title: "Разведчик читает 4 объявления-примера… (до пары минут на слабом сервере)" });
  try {
    const r = await api.post("/ai/scout/test", {}, { timeout: 240000 });
    const e = human(r.message_ru || r.error_ru || "Разведчик не ответил");
    if (r.ok) toast({ id, kind: "success", title: e.message });
    else toast({ id, kind: r.answered ? "warning" : "error", title: e.message, details: e.details, action: { label: "Настроить", href: "/settings/ai#scout" } });
  } catch (err) {
    toast({ id, kind: "error", title: err.message, details: err.details });
  }
  reload && reload();
}

/** «Разведчик» (AI scout, docs/design/AI_SCOUT.md §12): reads every new ad on the always-on server. */
function ScoutTile({ d, reload }) {
  const s = d.scout;
  const [busy, setBusy] = useState(false);
  if (!s) return null;
  const st = scoutState(s);
  const text = human(s.text_ru);
  const q = s.vision_queue || {};
  const problem = s.state === "down" || s.state === "too_small" || s.state === "behind" || s.state === "quota";
  return html`<${Tile}
    icon="telescope"
    title="Разведчик"
    tone=${st.tone}
    state=${st.label}
    wide
    tip="Маленькая нейросеть на сервере читает каждое новое объявление и находит то, что скрипт пропускает: опечатки, комплекты, старый ПК с дорогой видеокартой. Цены — только по истории объявлений."
    actions=${html`${s.enabled &&
      html`<${Button} size="sm" variant="secondary" icon="play" loading=${busy} onClick=${async () => {
        setBusy(true);
        await testScout(reload);
        setBusy(false);
      }}>Проверить<//>`}
      <${Button} size="sm" variant=${s.enabled ? "ghost" : "secondary"} href=${s.state === "too_small" ? "/settings/ai#models" : "/settings/ai#scout"}>
        ${!s.enabled ? "Включить" : s.state === "too_small" ? "Подобрать модель" : "Настроить"}
      <//>`}
  >
    ${text.message && html`<p class=${cx("htile__lead", problem && `t-${st.tone === "danger" ? "red" : "amber"}`)}>${text.message}</p>`}
    ${text.details && html`<${Details} text=${text.details} />`}
    ${s.enabled &&
    s.state !== "too_small" &&
    html`<div class="htile__grid">
      <div class="hmetric"><span>Прочитал за час</span><b class="num">${number(s.read_last_hour || 0)}${s.seen_last_hour ? ` из ${number(s.seen_last_hour)}` : ""}</b></div>
      <div class="hmetric"><span>Не успел за час</span><b class=${cx("num", s.overflow_last_hour > 0 && "t-amber")}>${number(s.overflow_last_hour || 0)}</b></div>
      ${s.failed_last_hour > 0 && html`<div class="hmetric"><span>Не разобрал</span><b class="num t-amber">${number(s.failed_last_hour)}</b></div>`}
      ${s.speed_ru &&
      html`<div class="hmetric"><span>Скорость${s.speed_expected ? html` <span class="tag scout-est" title="Ещё не измерено — оценка по модели и железу">оценка</span>` : ""}</span><b>${scoutSpeed(s)}</b></div>`}
      ${s.mode_ru && html`<div class="hmetric"><span>Режим</span><b>${human(s.mode_ru).message}</b></div>`}
      ${s.model && html`<div class="hmetric"><span>Модель</span><code class="mono">${s.model}</code></div>`}
    </div>`}
    ${q.waiting > 0 &&
    html`<p class="htile__note"><${Icon} name="hourglass" size=${14} /><span>Ждут проверки фото: <b class="num">${number(q.waiting)}</b>${q.max_wait_minutes ? ` — жду ПК с нейросетью до ${Math.round(q.max_wait_minutes)} мин, потом пришлю с пометкой «фото не проверены»` : ""}</span></p>`}
  <//>`;
}

// ------------------------------------------------------------------ runs
function RunsChart({ runs }) {
  const list = runs.slice(0, 48).reverse();
  const [hover, setHover] = useState(null);
  if (list.length < 2) return null;
  const max = Math.max(1, ...list.map((r) => r.deals_found || 0));
  const w = 100 / list.length;
  const r = hover != null ? list[hover] : null;
  return html`<div class="rchart">
    <div class="rchart__head">
      <span class="overline">Выгодных за проверку</span>
      <span class="rchart__read">${r ? `${dateTime(r.started_at)} · ${r.deals_found || 0} выгодных · ${r.new_listings || 0} новых` : `последние ${list.length} ${plural(list.length, "проверка", "проверки", "проверок")}`}</span>
    </div>
    <div class="rchart__bars" onPointerLeave=${() => setHover(null)} role="img" aria-label="Выгодных находок за каждую проверку">
      ${list.map(
        (run, i) => html`<span
          class=${cx("rchart__slot", hover === i && "is-hover")}
          style=${{ width: `${w}%` }}
          onPointerEnter=${() => setHover(i)}
          title=${`${dateTime(run.started_at)}: ${run.deals_found || 0} выгодных`}
        >
          <span class=${cx("rchart__bar", run.error_count > 0 && "is-err", !run.deals_found && "is-zero")} style=${{ height: `${Math.max(4, ((run.deals_found || 0) / max) * 100)}%` }}></span>
        </span>`,
      )}
    </div>
  </div>`;
}

function Runs({ runs }) {
  const isPhone = useMediaQuery(BREAKPOINTS.phone);
  const [open, setOpen] = useState(null);
  if (!runs.length)
    return html`<section class="runs">
      <h2 class="runs__title">Последние проверки</h2>
      <div class="runs__empty"><${Icon} name="hourglass" size=${20} />Проверок ещё не было — первая начнётся через минуту после запуска.</div>
    </section>`;
  return html`<section class="runs">
    <h2 class="runs__title">Последние проверки</h2>
    <${RunsChart} runs=${runs} />
    ${isPhone
      ? html`<div class="runlist">
          ${runs.slice(0, 20).map(
            (r) => html`<div class="runcard">
              <div class="runcard__top"><b>${r.started_at_label || dateTime(r.started_at)}</b><span class="muted">${r.duration_seconds != null ? span(r.duration_seconds) : "идёт…"}</span></div>
              <div class="runcard__nums num">
                <span>${r.new_listings} новых</span><span>${r.evaluated} оценено</span><span class=${r.deals_found ? "t-green" : ""}>${r.deals_found} выгодных</span><span>${r.notified} увед.</span>
              </div>
              ${r.error_count > 0 &&
              html`<button type="button" class="runerr" onClick=${() => setOpen(open === r.id ? null : r.id)}>
                  <${Icon} name="circle-alert" size=${14} />${r.error_count} ${plural(r.error_count, "ошибка", "ошибки", "ошибок")}
                </button>
                ${open === r.id && html`<${RunErrors} errors=${r.errors} details=${r.error_details} />`}`}
            </div>`,
          )}
        </div>`
      : html`<table class="rtable">
          <thead>
            <tr><th>Когда</th><th>Длилась</th><th class="r">Новых</th><th class="r">Оценено</th><th class="r">Выгодных</th><th class="r">Уведомлений</th><th class="r">Ошибки</th></tr>
          </thead>
          <tbody>
            ${runs.slice(0, 30).map(
              (r) => html`<tr>
                  <td>${r.started_at_label || dateTime(r.started_at)}</td>
                  <td class="muted">${r.duration_seconds != null ? span(r.duration_seconds) : "идёт…"}</td>
                  <td class="r num">${number(r.new_listings)}</td>
                  <td class="r num">${number(r.evaluated)}</td>
                  <td class=${cx("r num", r.deals_found > 0 && "t-green strong")}>${number(r.deals_found)}</td>
                  <td class="r num">${number(r.notified)}</td>
                  <td class="r">
                    ${r.error_count > 0
                      ? html`<button type="button" class="runerr" onClick=${() => setOpen(open === r.id ? null : r.id)} aria-expanded=${open === r.id}>${r.error_count}</button>`
                      : html`<span class="muted">—</span>`}
                  </td>
                </tr>
                ${open === r.id &&
                html`<tr class="rtable__errs"><td colspan="7"><${RunErrors} errors=${r.errors} details=${r.error_details} /></td></tr>`}`,
            )}
          </tbody>
        </table>`}
  </section>`;
}

/** Run errors in plain words; the raw text of each stays under «Подробнее». */
function RunErrors({ errors = [], details = [] }) {
  return html`<ul class="runerrs">
    ${errors.map((e, i) => {
      const h = human(e);
      return html`<li>${h.message}<${Details} text=${h.details || (details && details[i]) || ""} /></li>`;
    })}
  </ul>`;
}

/** "HTTP 403" → "сайт ответил отказом (403)". */
function blockReason(text) {
  const t = String(text || "");
  const code = (t.match(/\b(403|429|503)\b/) || [])[1];
  if (code === "429") return "слишком много запросов";
  if (code) return `сайт ответил отказом (${code})`;
  return human(t).message;
}

function OverviewSkeleton() {
  return html`<${Skeleton} h=${64} radius="var(--r-lg)" />
    <div class="htiles">${[0, 1, 2, 3, 4, 5].map(() => html`<section class="htile"><${Skeleton} w="50%" h=${16} /><${Skeleton} w="80%" h=${12} /><${Skeleton} w="70%" h=${12} /></section>`)}</div>`;
}

