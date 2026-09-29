// Entry point: boot, routing, live events. Screens live in ./screens, components in ./ui.
import "./lib/theme.js";
import { html, render, useEffect, useState, useErrorBoundary } from "./lib/html.js";
import { loadApp, isOnboarded } from "./lib/app.js";
import { appStore, useStore } from "./lib/store.js";
import { useRoute, matchPath, navigate } from "./lib/router.js";
import { connectEvents, onEvent } from "./lib/events.js";
import { money } from "./lib/format.js";
import { useInterval } from "./lib/hooks.js";
import { routes, redirects } from "./routes.js";
import { Shell, Logo, OfflineBanner } from "./shell/shell.js";
import { refreshMonitor } from "./shell/monitor.js";
import { Toaster, toast, DialogHost, Skeleton, EmptyState, Button } from "./ui/index.js";

function redirectPrefix(path) {
  for (const [from, to] of Object.entries(redirects)) if (from !== "/feed" && path.startsWith(from + "/")) return to + path.slice(from.length);
  return null;
}

// ------------------------------------------------------------------ route view
const moduleCache = new Map();

function findRoute(path) {
  for (const r of routes) {
    const params = matchPath(r.path, path);
    if (params) return { route: r, params };
  }
  return null;
}

function NotFound() {
  return html`<${EmptyState}
    icon="compass"
    title="Такой страницы нет"
    message="Возможно, ссылка устарела. Вернись в ленту — там всё самое интересное."
    action=${html`<${Button} variant="primary" href="/" icon="sparkles">Открыть ленту<//>`}
  />`;
}

function ScreenError({ error, reset }) {
  return html`<${EmptyState}
    icon="bug"
    tone="danger"
    title="Экран не открылся"
    message=${"Что-то пошло не так при показе этой страницы. " + ((error && error.message) || "")}
    action=${html`<${Button} icon="refresh-cw" onClick=${reset}>Попробовать снова<//>`}
  />`;
}

function Screen({ component: C, params, query }) {
  const [error, reset] = useErrorBoundary((e) => console.error(e));
  if (error) return html`<${ScreenError} error=${error} reset=${reset} />`;
  return html`<${C} params=${params} query=${query} />`;
}

function useScreen(route) {
  const [, force] = useState(0);
  const cached = route && moduleCache.get(route.path);
  useEffect(() => {
    if (!route || moduleCache.has(route.path)) return;
    let alive = true;
    route
      .load()
      .then((m) => {
        moduleCache.set(route.path, { component: m.default });
      })
      .catch((e) => {
        console.error(e);
        moduleCache.set(route.path, { error: e });
      })
      .finally(() => alive && force((n) => n + 1));
    return () => {
      alive = false;
    };
  }, [route]);
  return cached;
}

function ScreenSkeleton() {
  return html`<div class="screen-skeleton">
    <${Skeleton} w="40%" h=${30} />
    <${Skeleton} w="60%" h=${16} />
    <div class="grid mt-6"><${Skeleton} variant="card" /><${Skeleton} variant="card" /><${Skeleton} variant="card" /></div>
  </div>`;
}

// ------------------------------------------------------------------ app
function App() {
  const route = useRoute();
  const app = useStore(appStore, (s) => s.app);
  const [bootError, setBootError] = useState(null);

  useEffect(() => {
    loadApp().catch((e) => setBootError(e));
    refreshMonitor();
    connectEvents();
  }, []);

  // live events → toasts, badge, monitor refresh
  useEffect(() => {
    const offs = [
      onEvent("deal_found", (d) => {
        const deal = d || {};
        const listing = deal.listing || deal;
        const id = listing.ad_id || deal.id || deal.ad_id;
        const profit = deal.profit ?? (deal.evaluation && deal.evaluation.estimate && deal.evaluation.estimate.profit);
        appStore.set({ newDeals: appStore.get().newDeals + 1 });
        toast({
          kind: "deal",
          title: "Новая выгодная сделка",
          message: [listing.title, profit != null ? `прибыль ~${money(profit)}` : null].filter(Boolean).join(" · "),
          action: id ? { label: "Открыть", href: `/deal/${encodeURIComponent(id)}` } : { label: "Лента", href: "/" },
          duration: 9000,
        });
      }),
      onEvent("run_started", () => refreshMonitor()),
      onEvent("run_finished", () => refreshMonitor()),
      onEvent("monitor", () => refreshMonitor()),
      onEvent("monitor_state", () => refreshMonitor()),
      onEvent("connected", () => refreshMonitor()),
    ];
    return () => offs.forEach((off) => off());
  }, []);

  // fallback polling (SSE down) and recovery after the API was unreachable
  const connection = useStore(appStore, (s) => s.connection);
  const live = useStore(appStore, (s) => s.live);
  useInterval(() => refreshMonitor(), live === "open" ? 60000 : 20000);
  useInterval(
    () => {
      loadApp().then(() => setBootError(null), () => {});
    },
    connection === "offline" || bootError ? 5000 : null,
  );

  // redirects + onboarding gate
  const target = redirects[route.path] || redirectPrefix(route.path);
  useEffect(() => {
    if (target) navigate(target + (window.location.search || ""), { replace: true });
  }, [target]);
  const onboarded = isOnboarded(app);
  const onOnboarding = route.path === "/welcome" || route.path.startsWith("/welcome/");
  useEffect(() => {
    if (app && app.loaded && !onboarded && !onOnboarding && !target) navigate("/welcome", { replace: true });
  }, [app, onboarded, onOnboarding, target]);

  const found = findRoute(route.path);
  const mod = useScreen(found && found.route);
  const title = found ? found.route.title : "Страница не найдена";
  useEffect(() => {
    document.title = `${title} · EbeyParser`;
  }, [title]);

  if (!app || (!app.loaded && !bootError)) return html`<${Splash} />`;
  if (bootError && !app.loaded) return html`<${BootError} error=${bootError} onRetry=${() => loadApp().then(() => setBootError(null), setBootError)} />`;

  let body;
  if (!found) body = html`<${NotFound} />`;
  else if (!mod) body = html`<${ScreenSkeleton} />`;
  else if (mod.error) body = html`<${ScreenError} error=${mod.error} reset=${() => window.location.reload()} />`;
  else body = html`<div class="screen" key=${route.path}><${Screen} component=${mod.component} params=${found.params} query=${route.query} /></div>`;

  const chrome = found && found.route.fullscreen
    ? html`<div class="fullscreen"><${OfflineBanner} />${body}</div>`
    : html`<${Shell} nav=${found && found.route.nav} title=${title}>${body}<//>`;

  return html`${chrome}<${Toaster} /><${DialogHost} />`;
}

function Splash() {
  return html`<div class="splash" aria-busy="true">
    <div class="splash__logo"><${Logo} size=${44} withText=${false} /></div>
    <div class="splash__bar"><span></span></div>
  </div>`;
}

function BootError({ error, onRetry }) {
  const offline = error && error.status === 0;
  return html`<div class="splash">
    <${EmptyState}
      icon=${offline ? "wifi-off" : "circle-alert"}
      tone="danger"
      title=${offline ? "Программа не отвечает" : "Не получилось открыть приложение"}
      message=${offline
        ? "Похоже, EbeyParser на компьютере закрыт или ещё запускается. Запусти его — страница обновится сама."
        : (error && error.message) || "Попробуй обновить страницу."}
      action=${html`<${Button} variant="primary" icon="refresh-cw" onClick=${onRetry}>Попробовать снова<//>`}
    />
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
document.documentElement.classList.add("app-ready");
