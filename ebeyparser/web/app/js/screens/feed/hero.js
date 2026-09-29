// Feed hero (brief §4.2.1): today's numbers + best deal + live monitor strip,
// the learning-progress variant, the «Осталось настроить» checklist and the demo ribbon.
import { html, cx, useState, useEffect } from "../../lib/html.js";
import { onEvent } from "../../lib/events.js";
import { useNow } from "../../lib/hooks.js";
import { appStore, useStore } from "../../lib/store.js";
import { api } from "../../lib/api.js";
import { money, number, plural, ago, clockTime } from "../../lib/format.js";
import { isOnboarded, loadApp } from "../../lib/app.js";
import { navigate } from "../../lib/router.js";
import { Icon, Button, IconButton, Ring, toast, Skeleton } from "../../ui/index.js";
import { monitorAction, cooldownOf, nextCheckAt } from "../../shell/monitor.js";
import "../../features/icons-extra.js";
import { DealImage } from "../../features/deal-card.js";
import { decide, normalizeCard } from "../../features/deal-model.js";

export const SETTINGS = { ai: "/settings/ai", telegram: "/settings/notifications", ebay: "/settings/ebay", categories: "/searches", location: "/settings/region", money: "/settings/money" };

function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

const hm = (iso) => clockTime(iso);

/** Where the finds will show up: Telegram, e-mail or just here. */
function channelText(app) {
  const f = (app && app.features) || {};
  if (f.telegram) return "Пришлю в Telegram";
  if (f.email) return "Пришлю на почту";
  return "Покажу здесь, в ленте";
}

/** «● Работает · следующая проверка через 12:03» + Проверить сейчас / Пауза. */
export function StatusStrip({ compact = false }) {
  const m = useStore(appStore, (s) => s.monitor);
  const now = useNow(1000);
  const [busy, setBusy] = useState(null);
  // live run progress «поиск 3 из 8» straight from the event stream
  useEffect(
    () =>
      onEvent("run_progress", (d) => {
        const cur = appStore.get().monitor;
        if (cur && d) appStore.set({ monitor: { ...cur, running: true, progress: d } });
      }),
    [],
  );
  const act = async (kind) => {
    setBusy(kind);
    await monitorAction(kind);
    setBusy(null);
  };
  if (!m) return html`<div class="strip"><${Skeleton} w="60%" h=${14} /></div>`;
  const raw = m.raw || {};
  const cd = cooldownOf(m, now);
  let tone = "profit";
  let text;
  let pulse = true;
  if (m.available === false) {
    tone = "neutral";
    pulse = false;
    text = m.message || "Фоновые проверки выключены";
  } else if (m.running) {
    tone = "info";
    const p = (m.progress && typeof m.progress === "object" ? m.progress : raw.progress) || {};
    text = p.total
      ? `Идёт проверка… ${p.index ?? p.done ?? 0} из ${p.total} ${plural(p.total, "поиска", "поисков", "поисков")}${p.search_name ? ` · ${p.search_name}` : ""}`
      : raw.state_ru || "Идёт проверка…";
  } else if (raw.state === "stopped" || raw.state === "off") {
    tone = "neutral";
    pulse = false;
    text = `${raw.state_ru || "Автопроверка выключена"}${m.lastRunAt ? ` · последняя проверка ${ago(m.lastRunAt, now)}` : ""}`;
  } else if (m.paused) {
    tone = "neutral";
    pulse = false;
    text = "Проверки на паузе";
  } else if (cd) {
    tone = "haggle";
    pulse = false;
    text = `${cd.label ? `Пауза до ${cd.label}` : "Пауза"}: ${cd.site} попросил отдохнуть${cd.strikes ? ` (блокировка №${cd.strikes})` : ""}. Сам продолжу — ничего делать не нужно.`;
  } else if (m.nextRunAt) {
    const left = (new Date(m.nextRunAt) - now) / 1000;
    text = left > 0 ? html`Работает · следующая проверка через <b class="num">${clock(left)}</b>` : "Работает · проверка вот-вот начнётся";
  } else {
    text = m.lastRunAt ? `Работает · последняя проверка ${ago(m.lastRunAt, now)}` : "Работает · жду первой проверки";
  }
  return html`<div class=${cx("strip", compact && "strip--compact")}>
    <span class=${cx("sdot", `tone-${tone}`, pulse && "is-pulse")}></span>
    <span class="strip__text">${text}</span>
    ${m.available !== false &&
    html`<span class="strip__actions">
      ${m.paused
        ? html`<${Button} size="sm" variant="secondary" icon="play" loading=${busy === "resume"} onClick=${() => act("resume")} aria-label="Продолжить проверки">Продолжить<//>`
        : !m.running &&
          html`<${Button} size="sm" variant="ghost" icon="refresh-cw" loading=${busy === "run"} onClick=${() => act("run")} aria-label="Проверить сейчас" title="Проверить сейчас">Проверить сейчас<//>
            <${IconButton} size="sm" icon="pause" label="Пауза" loading=${busy === "pause"} onClick=${() => act("pause")} />`}
    </span>`}
  </div>`;
}

/** summary = GET /summary/today. */
export function Hero({ summary, loading }) {
  const app = useStore(appStore, (st) => st.app) || {};
  const m = useStore(appStore, (st) => st.monitor);
  const s = summary || {};
  const first = s.learning || app.first_run || {};
  if (first.learning && !s.count) return html`<${LearningHero} first=${first} monitor=${m} />`;
  const best = s.best ? normalizeCard(s.best) : null;
  const personal = Boolean(s.personal_only);
  const count = s.count || 0;
  const potential = personal ? s.potential_savings : s.potential_profit;
  return html`<section class="hero" aria-label="Сводка за сегодня">
    <div class="hero__main">
      <div class="overline">Сегодня</div>
      ${loading && !summary
        ? html`<${Skeleton} w="70%" h=${34} /><${Skeleton} w="50%" h=${14} />`
        : html`<h1 class="hero__title">
              ${personal
                ? html`<b class="num">${number(s.personal_count ?? count)}</b> ${plural(s.personal_count ?? count, "находка", "находки", "находок")} для себя`
                : html`Найдено <b class="num">${number(count)}</b> ${plural(count, "выгодное", "выгодных", "выгодных")}`}
            </h1>
            <p class="hero__sub">
              ${potential > 0 && html`${personal ? "Экономия" : "Потенциал"} <b class="t-green num">${money(potential, { sign: !personal })}</b> · `}
              ${s.ads_seen != null
                ? html`просмотрено <span class="num">${number(s.ads_seen)}</span> ${plural(s.ads_seen, "объявление", "объявления", "объявлений")}`
                : count
                  ? "Лучшие — сверху"
                  : "Пока тихо — я продолжаю смотреть"}
            </p>
            ${first.learning &&
            (app.demo && app.demo.loaded
              ? (first.searches || []).length > 0 && html`<p class="hero__note"><${Icon} name="hourglass" size=${14} />Ниже — демо-находки. Твои поиски пока изучают рынок.</p>`
              : first.message_ru && html`<p class="hero__note"><${Icon} name="hourglass" size=${14} />${first.message_ru}</p>`)}`}
    </div>
    ${best &&
    html`<a class=${cx("hero__best", `tone-${decide(best).tone}`)} href=${`/deal/${encodeURIComponent(best.id)}`} data-deal-open=${best.id}>
      <${DealImage} src=${best.image} class="hero__thumb" />
      <span class="hero__best-text">
        <span class="overline">Лучшее сегодня</span>
        <span class="hero__best-title">${best.title}</span>
        <span class="hero__best-money num">
          ${money(best.price)} → <b>${best.profit != null ? money(best.profit, { sign: best.purpose !== "personal" }) : decide(best).verb}</b>
        </span>
      </span>
      <${Icon} name="chevron-right" size=${18} />
    </a>`}
    <${StatusStrip} />
  </section>`;
}

function LearningHero({ first, monitor }) {
  const app = useStore(appStore, (st) => st.app) || {};
  const now = useNow(15000);
  const total = first.enabled_searches || (first.searches || []).length || 0;
  const pending = (first.searches || []).filter((s) => s.state === "pending").length;
  const done = Math.max(0, total - pending);
  // never promise a check that falls inside a block pause (brief §2.3)
  const cd = cooldownOf(monitor, now);
  const at = nextCheckAt(monitor, now);
  const stopped = monitor && (monitor.state === "stopped" || monitor.available === false);
  // the server knows when the first alerts can come (after a pause, the next run); else our estimate
  const lbl = first.first_alerts_label || "";
  const around = lbl ? (/^\d/.test(lbl) ? `около ${lbl}` : lbl) : "";
  const when = monitor && monitor.paused
    ? "проверки на паузе — нажми «Продолжить»"
    : stopped
      ? "автопроверка выключена — нажми «Проверить сейчас»"
      : cd
        ? `первые уведомления после паузы${around ? `, ${around}` : cd.label ? `, около ${cd.label}` : ""}`
        : `первые уведомления после следующей проверки${lbl ? ` (${around})` : at ? ` (~${hm(at)})` : ""}`;
  return html`<section class="hero hero--learning" aria-label="Изучаю рынок">
    <div class="hero__main">
      <div class="overline"><${Icon} name="hourglass" size=${12} />Первый запуск</div>
      <h1 class="hero__title">Изучаю рынок в твоём районе</h1>
      <p class="hero__sub">
        ${done} из ${total} ${plural(total, "поиска", "поисков", "поисков")} · собрано <b class="num">${number(first.price_points || 0)}</b> ${plural(first.price_points || 0, "цена", "цены", "цен")} · ${when}
      </p>
      <div class="gauge gauge--lg" role="progressbar" aria-valuemin="0" aria-valuemax=${total} aria-valuenow=${done}>
        <span style=${{ width: `${total ? Math.max(4, (done / total) * 100) : 4}%` }}></span>
      </div>
      <p class="hero__note">Первая проверка каждого поиска только собирает цены — так я пойму, что на самом деле дёшево. ${channelText(app)}.</p>
    </div>
    <${StatusStrip} />
  </section>`;
}

const SETUP_ITEMS = {
  telegram: { label: "Telegram", time: "2 мин", icon: "send" },
  ai: { label: "Нейросеть", time: "5 мин", icon: "scan-eye" },
  ebay: { label: "eBay", time: "5 мин", icon: "gavel" },
  categories: { label: "Поиски", time: "3 мин", icon: "radar" },
};

/** «Осталось настроить» — stays until Telegram and the AI are done. */
export function SetupChecklist({ summary }) {
  const app = useStore(appStore, (st) => st.app) || {};
  const list = (summary && summary.setup_checklist && summary.setup_checklist.items) ||
    ((app.onboarding && app.onboarding.steps) || []).filter((x) => SETUP_ITEMS[x.key]);
  if (!list.length) return null;
  const byKey = Object.fromEntries(list.map((x) => [x.key, x]));
  const essentialLeft = ["telegram", "ai"].some((k) => byKey[k] && !byKey[k].done);
  if (!essentialLeft) return null;
  const todo = list.filter((x) => !x.done && SETUP_ITEMS[x.key]);
  const done = list.filter((x) => x.done).length;
  return html`<section class="setup-card">
    <${Ring} value=${done / list.length} size=${44} stroke=${4} tone="profit">
      <span class="num">${done}/${list.length}</span>
    <//>
    <div class="setup-card__body">
      <b>Осталось настроить</b>
      <div class="setup-card__items">
        ${todo.map(
          (x) => html`<a class="setup-item" href=${SETTINGS[x.key] || "/settings"}>
            <span class="setup-item__box"></span><${Icon} name=${SETUP_ITEMS[x.key].icon} size=${14} />${SETUP_ITEMS[x.key].label}<span class="muted">(${SETUP_ITEMS[x.key].time})</span>
          </a>`,
        )}
      </div>
    </div>
  </section>`;
}

/** Ribbon shown while demo data is loaded. */
export function DemoRibbon() {
  const app = useStore(appStore, (s) => s.app) || {};
  const [busy, setBusy] = useState(false);
  const demo = app.demo;
  if (!demo || !(demo.loaded || demo === true)) return null;
  const clear = async () => {
    setBusy(true);
    try {
      await api.post("/demo/clear", {});
      const cur = appStore.get().app || {};
      appStore.set({ app: { ...cur, demo: { loaded: false, count: 0 } } });
      if (!isOnboarded(cur)) {
        // nothing is set up yet: the feed would be empty — go to the setup, and say why
        toast.success("Демо-данные убраны — давай настроим поиск", { message: "Это займёт 3–5 минут" });
        navigate("/welcome");
      } else {
        toast.success("Демо-данные убраны");
        window.dispatchEvent(new CustomEvent("ebp:feed-reload"));
      }
      loadApp().catch(() => {});
    } catch (e) {
      toast.error(e);
    }
    setBusy(false);
  };
  return html`<div class="demo-ribbon">
    <${Icon} name="flask-conical" size=${16} />
    <span class="demo-ribbon__text"><b>Демо-данные</b> — примеры, чтобы посмотреть, как всё выглядит.</span>
    <span class="grow"></span>
    <${Button} size="sm" variant="ghost" loading=${busy} onClick=${clear}>Убрать демо<//>
    <${Button} size="sm" variant="secondary" onClick=${() => navigate("/welcome")}>Настроить по-настоящему<//>
  </div>`;
}

export function HeroSkeleton() {
  return html`<section class="hero" aria-hidden="true">
    <div class="hero__main"><${Skeleton} w="20%" h=${10} /><${Skeleton} w="60%" h=${34} /><${Skeleton} w="45%" h=${14} /></div>
    <div class="strip"><${Skeleton} w="50%" h=${14} /></div>
  </section>`;
}

