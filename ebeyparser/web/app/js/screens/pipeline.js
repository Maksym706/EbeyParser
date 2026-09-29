// Мои сделки — pipeline Избранное → Написал → Купил → Продал (Frontend B).
// Route: "/pipeline"   Data: GET /api/v1/deals?status=…, PATCH /deals/{id} {status, bought_price, sold_price}
import { html } from "../lib/html.js";
import { ScreenPlaceholder } from "./placeholder.js";

export default function PipelineScreen() {
  return html`<${ScreenPlaceholder}
    title="Мои сделки"
    subtitle="Избранное → Написал → Купил → Продал"
    icon="square-kanban"
    note="Здесь будет доска твоих сделок и заработанная прибыль."
  />`;
}
