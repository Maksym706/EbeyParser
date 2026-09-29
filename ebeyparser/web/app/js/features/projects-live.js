// «Сборки» live layer (spec §7): the `project_updated` server event keeps the list cards fresh,
// shows the alert toast («Сборка «LLM-сервер»: MI50 за 175 € — ниже цели» → «Открыть») and keeps
// the «Сборки» nav badge = tracking builds with an offer below target (`headline_ru`).
// Imported once by the shell; screens subscribe to the same event for their own refetch.
import { onEvent } from "../lib/events.js";
import { appStore } from "../lib/store.js";
import { routeStore } from "../lib/router.js";
import { toast } from "../ui/index.js";
import { projectsApi, projectsStore, forgetView, ru } from "./projects-common.js";
import { setBadge } from "./badges.js";

/** Badge = tracking builds whose best offer is below its target right now. */
export function badgeFrom(cards) {
  return (cards || []).filter((c) => c && c.status === "tracking" && c.headline_ru).length;
}

function applyCards(cards) {
  projectsStore.set({ cards });
  setBadge("projects", badgeFrom(cards));
}

let loading = null;
export function refreshProjects() {
  if (loading) return loading;
  loading = projectsApi
    .list()
    .then((res) => {
      applyCards((res && res.items) || []);
      return res;
    })
    .catch(() => null)
    .finally(() => {
      loading = null;
    });
  return loading;
}

function onUpdate(data) {
  if (!data || data.id == null) return;
  const id = Number(data.id);
  const cards = projectsStore.get().cards;
  if (data.reason === "deleted") {
    forgetView(id);
    if (cards) applyCards(cards.filter((c) => c.id !== id));
    return;
  }
  if (data.card && cards) {
    const has = cards.some((c) => c.id === id);
    applyCards(has ? cards.map((c) => (c.id === id ? data.card : c)) : [data.card, ...cards]);
  } else if (!cards) {
    refreshProjects();
  }
  if (data.reason === "alert" && data.alert) {
    const here = routeStore.get().path === `/projects/${id}`;
    toast({
      kind: "success",
      icon: "boxes",
      title: ru(data.alert.text_ru) || "Сборка: предложение ниже цели",
      message: data.alert.delivered === false ? "Уведомление только здесь — Telegram не настроен" : "",
      action: here ? null : { label: "Открыть", href: `/projects/${id}` },
      duration: 8000,
    });
  }
}

let started = false;
/** Called once by the shell. */
export function startProjectsLive() {
  if (started) return;
  started = true;
  onEvent("project_updated", onUpdate);
  onEvent("connected", () => refreshProjects());
  // the badge from the first moment (SSE may connect later or not at all)
  const app = appStore.get().app;
  if (!app || app.onboarded !== false) setTimeout(refreshProjects, 400);
}
