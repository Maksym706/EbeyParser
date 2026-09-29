// Monitor status (sidebar card + mobile pill): running / paused / idle, next check, pause/resume, "check now".
import { html, cx, useState } from "../lib/html.js";
import { api } from "../lib/api.js";
import { appStore, useStore } from "../lib/store.js";
import { until, ago } from "../lib/format.js";
import { useNow } from "../lib/hooks.js";
import { Icon, Button, IconButton, StatusDot, Progress, toast, Modal } from "../ui/index.js";

let inflight = null;

/** Reload GET /monitor into appStore.monitor (coalesces parallel calls). */
export function refreshMonitor() {
  if (inflight) return inflight;
  inflight = api
    .get("/monitor")
    .then((m) => appStore.set({ monitor: normalize(m) }))
    .catch((e) => {
      if (e.status === 404) appStore.set({ monitor: { available: false } });
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** GET /monitor → the fields the shell needs (see ebeyparser/web/api/routes_monitor.py). */
export function normalize(m) {
  if (!m || typeof m !== "object") return { available: false };
  const last = m.last_summary || m.last_run || null;
  const p = m.progress;
  return {
    available: m.available !== false,
    running: Boolean(m.running),
    paused: Boolean(m.paused),
    loop: m.loop !== false,
    state: m.state || null, // off | running | paused | idle | stopped
    stateText: m.state_ru || null,
    nextRunAt: m.next_run_at || null,
    lastRunAt: (last && (last.finished_at || last.started_at)) || null,
    lastRun: last,
    progress: p && p.total ? Math.min(1, (p.index || 0) / p.total) : null,
    progressText: p && p.total ? `${p.index || 0} из ${p.total} поисков${p.search_name ? ` · ${p.search_name}` : ""}` : null,
    intervalMinutes: m.interval_minutes ?? null,
    learning: m.learning || null,
    raw: m,
  };
}

export async function monitorAction(kind) {
  const labels = {
    run: ["Запускаю проверку…", "Проверка запущена"],
    pause: ["Ставлю на паузу…", "Проверки на паузе"],
    resume: ["Продолжаю…", "Проверки снова работают"],
  };
  const [loading, done] = labels[kind];
  try {
    await toast.promise(api.post(`/monitor/${kind}`), { loading, success: done, error: (e) => e.message });
  } catch {
    /* toast shown */
  }
  if (kind === "run") appStore.set({ monitor: { ...(appStore.get().monitor || {}), running: true } });
  refreshMonitor();
}

export function monitorSummary(m, now = new Date()) {
  if (!m) return { tone: "neutral", label: "Загрузка…", detail: "" };
  if (m.available === false) return { tone: "neutral", label: "Проверки выключены", detail: "Программа запущена без фоновых проверок" };
  if (m.running) return { tone: "blue", label: "Идёт проверка", detail: m.progressText ? `${m.progressText}` : "Это займёт пару минут", pulse: true };
  if (m.paused) return { tone: "neutral", label: "Проверки на паузе", detail: "Новые объявления не смотрю" };
  if (m.state === "stopped") return { tone: "neutral", label: "Автопроверка выключена", detail: m.lastRunAt ? `Последняя проверка ${ago(m.lastRunAt, now)}` : "Можно проверить вручную" };
  const next = m.nextRunAt ? `Следующая проверка ${until(m.nextRunAt, now)}` : m.lastRunAt ? `Последняя проверка ${ago(m.lastRunAt, now)}` : "Ждёт первой проверки";
  return { tone: "green", label: "Работает", detail: next, pulse: true };
}

/** Sidebar card; `compact` (icon rail) shows a status button that opens the card in a dialog. */
export function MonitorCard({ compact = false }) {
  if (compact) return html`<${MonitorRailButton} />`;
  return html`<${MonitorCardFull} />`;
}

function MonitorRailButton() {
  const m = useStore(appStore, (s) => s.monitor);
  const now = useNow(15000);
  const [open, setOpen] = useState(false);
  const s = monitorSummary(m, now);
  return html`
    <button type="button" class="monitor-rail" title=${`${s.label}. ${s.detail}`} aria-label=${`Мониторинг: ${s.label}`} onClick=${() => setOpen(true)}>
      <${Icon} name="radar" size=${20} />
      <${StatusDot} tone=${s.tone} pulse=${s.pulse} />
    </button>
    <${Modal} open=${open} onClose=${() => setOpen(false)} title="Проверки" size="sm">
      <${MonitorCardFull} />
    <//>
  `;
}

function MonitorCardFull() {
  const m = useStore(appStore, (s) => s.monitor);
  const now = useNow(15000);
  const [busy, setBusy] = useState(null);
  const s = monitorSummary(m, now);
  const act = async (kind) => {
    setBusy(kind);
    await monitorAction(kind);
    setBusy(null);
  };
  const available = m && m.available !== false;
  return html`<div class=${cx("monitor-card", `monitor-card--${s.tone}`)}>
    <div class="monitor-card__top">
      <${StatusDot} tone=${s.tone} pulse=${s.pulse} />
      <div class="monitor-card__text">
        <div class="monitor-card__label">${s.label}</div>
        <div class="monitor-card__detail">${s.detail}</div>
      </div>
      ${available &&
      !m.running &&
      (m.paused || (m.loop && m.state !== "stopped")) &&
      html`<${IconButton}
        size="sm"
        icon=${m.paused ? "play" : "pause"}
        label=${m.paused ? "Продолжить мониторинг" : "Поставить на паузу"}
        loading=${busy === "pause" || busy === "resume"}
        onClick=${() => act(m.paused ? "resume" : "pause")}
      />`}
    </div>
    ${m && m.running && html`<${Progress} size="sm" tone="blue" value=${m.progress} label="Идёт проверка" />`}
    ${available &&
    !m.running &&
    html`<${Button} size="sm" variant="secondary" block icon="refresh-cw" loading=${busy === "run"} onClick=${() => act("run")}>Проверить сейчас<//>`}
  </div>`;
}

/** Phone app-bar status (dot only when `compact`); tap opens the card in a sheet. */
export function MonitorPill({ compact = false }) {
  const m = useStore(appStore, (s) => s.monitor);
  const now = useNow(15000);
  const [open, setOpen] = useState(false);
  const s = monitorSummary(m, now);
  return html`
    <button type="button" class=${cx("monitor-pill", compact && "monitor-pill--compact", `monitor-pill--${s.tone}`)} onClick=${() => setOpen(true)} aria-label=${`Мониторинг: ${s.label}`} title=${s.label}>
      <${StatusDot} tone=${s.tone} pulse=${s.pulse} />
      ${!compact && html`<span>${s.label === "Проверяю объявления" ? "Проверяю…" : s.label}</span>`}
    </button>
    <${Modal} open=${open} onClose=${() => setOpen(false)} title="Проверки" size="sm">
      <${MonitorCardFull} />
    <//>
  `;
}
