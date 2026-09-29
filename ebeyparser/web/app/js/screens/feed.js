// Лента — the deals feed (Frontend B).
// Route: "/"   Props: { params, query }   Data: GET /api/v1/deals, /stats/overview; live: onEvent("deal_found")
// Reset the sidebar badge when the feed is visible: appStore.set({ newDeals: 0 }).
import { html, useEffect } from "../lib/html.js";
import { appStore } from "../lib/store.js";
import { ScreenPlaceholder } from "./placeholder.js";

export default function FeedScreen() {
  useEffect(() => appStore.set({ newDeals: 0 }), []);
  return html`<${ScreenPlaceholder}
    title="Лента"
    subtitle="Самые выгодные объявления — сверху"
    icon="sparkles"
    note="Здесь будут карточки сделок с фото, ценой и ожидаемой прибылью."
  />`;
}
