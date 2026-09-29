// Monitor status (sidebar card + mobile pill): running / paused / idle, next check, pause/resume, "check now".
import { html, cx, useState } from "../lib/html.js";
import { api } from "../lib/api.js";
import { appStore, useStore } from "../lib/store.js";
import { until, ago } from "../lib/format.js";
import { useNow } from "../lib/hooks.js";
import { Button, IconButton, StatusDot, Progress, toast, Modal } from "../ui/index.js";

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

/** Accept a few plausible shapes of the /monitor payload. */
export function normalize(m) {
  if (!m || typeof m !== "object") return { available: false };
  const last = m.last_run || m.lastRun || null;
  return {
    available: m.available !== false && m.enabled !== false,
    running: Boolean(m.running ?? m.checking ?? m.busy),
    paused: Boolean(m.paused),
    loop: m.loop ?? m.active ?? true,
    nextRunAt: m.next_run_at || m.nextRunAt || null,
    lastRunAt: m.last_run_at || (last && (last.finished_at || last.started_at)) || null,
    lastRun: last,
    progress: m.progress ?? null,
    stage: m.stage_ru || m.stage || null,
    intervalMinutes: m.interval_minutes ?? null,
    message: m.message_ru || m.reason_ru || null,
    raw: m,
  };
}

export async function monitorAction(kind) {
  const labels = {
    run: ["Запускаю проверку…", "Проверка началась — новые сделки появятся в ленте"],
    pause: ["Ставлю на паузу…", "Мониторинг на паузе"],
    resume: ["Возобновляю…", "Мониторинг снова работает"],
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
  if (m.available === false) return { tone: "neutral", label: "Мониторинг выключен", detail: m.message || "Программа запущена без фоновых проверок" };
  if (m.running) return { tone: "info", label: "Проверяю объявления", detail: m.stage || "Это займёт пару минут", pulse: true };
  if (m.paused) return { tone: "haggle", label: "На паузе", detail: "Новые объявления не проверяются" };
  const next = m.nextRunAt ? `Следующая проверка ${until(m.nextRunAt, now)}` : m.lastRunAt ? `Последняя проверка ${ago(m.lastRunAt, now)}` : "Ждёт первой проверки";
  return { tone: "profit", label: "Работает", detail: next, pulse: true };
}

/** Sidebar card. */
export function MonitorCard({ compact = false }) {
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
  return html`<div class=${cx("monitor-card", compact && "monitor-card--compact", `monitor-card--${s.tone}`)}>
    <div class="monitor-card__top">
      <${StatusDot} tone=${s.tone} pulse=${s.pulse} />
      <div class="monitor-card__text">
        <div class="monitor-card__label">${s.label}</div>
        <div class="monitor-card__detail">${s.detail}</div>
      </div>
      ${available &&
      !m.running &&
      html`<${IconButton}
        size="sm"
        icon=${m.paused ? "play" : "pause"}
        label=${m.paused ? "Продолжить мониторинг" : "Поставить на паузу"}
        loading=${busy === "pause" || busy === "resume"}
        onClick=${() => act(m.paused ? "resume" : "pause")}
      />`}
    </div>
    ${m && m.running && html`<${Progress} size="sm" tone="info" value=${typeof m.progress === "number" ? m.progress : null} label="Идёт проверка" />`}
    ${available &&
    !m.running &&
    html`<${Button} size="sm" variant="secondary" block icon="refresh-cw" loading=${busy === "run"} onClick=${() => act("run")}>Проверить сейчас<//>`}
  </div>`;
}

/** Mobile top-bar pill; tap opens the card in a sheet. */
export function MonitorPill() {
  const m = useStore(appStore, (s) => s.monitor);
  const now = useNow(15000);
  const [open, setOpen] = useState(false);
  const s = monitorSummary(m, now);
  return html`
    <button type="button" class=${cx("monitor-pill", `monitor-pill--${s.tone}`)} onClick=${() => setOpen(true)} aria-label=${`Мониторинг: ${s.label}`}>
      <${StatusDot} tone=${s.tone} pulse=${s.pulse} />
      <span>${s.label === "Проверяю объявления" ? "Проверяю…" : s.label}</span>
    </button>
    <${Modal} open=${open} onClose=${() => setOpen(false)} title="Мониторинг" size="sm">
      <${MonitorCard} />
    <//>
  `;
}
