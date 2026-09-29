// Здоровье — monitor runs, AI / site / notification status, logs (Frontend B).
// Route: "/health"   Data: GET /api/v1/health, /runs, /logs, /stats/timeseries
import { html } from "../lib/html.js";
import { ScreenPlaceholder } from "./placeholder.js";

export default function HealthScreen() {
  return html`<${ScreenPlaceholder}
    title="Здоровье"
    subtitle="Всё ли работает: проверки, нейросеть, уведомления"
    icon="heart-pulse"
    note="Здесь будет состояние программы, история проверок и понятные подсказки, если что-то сломалось."
  />`;
}
