// App-level info from GET /api/v1/app (onboarded?, version, demo mode, …) kept in appStore.app.
import { api, ApiError } from "./api.js";
import { appStore } from "./store.js";

export async function loadApp() {
  try {
    const app = await api.get("/app");
    appStore.set({ app: { ...app, loaded: true } });
    return app;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      // API not deployed yet: behave as an already configured app
      const app = { onboarded: true, missing: true, loaded: true };
      appStore.set({ app });
      return app;
    }
    const prev = appStore.get().app;
    if (!prev || !prev.loaded) appStore.set({ app: { loaded: false, error: e } });
    throw e;
  }
}

export function isOnboarded(app) {
  if (!app) return true;
  if (typeof app.onboarded === "boolean") return app.onboarded;
  if (app.onboarding && typeof app.onboarding.done === "boolean") return app.onboarding.done;
  return true;
}
