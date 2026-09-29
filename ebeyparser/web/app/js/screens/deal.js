// Сделка — one deal in detail (Frontend B).
// Route: "/deal/:id"  (Telegram/e-mail links point here)   Data: GET /api/v1/deals/{id}, PATCH /deals/{id},
// POST /deals/{id}/reevaluate (job), GET /deals/{id}/price-history
import { html } from "../lib/html.js";
import { ScreenPlaceholder } from "./placeholder.js";

export default function DealScreen({ params }) {
  return html`<${ScreenPlaceholder}
    title="Сделка"
    subtitle=${`Объявление ${params.id}`}
    icon="tag"
    note="Здесь будет подробный разбор: фото, расчёт прибыли, мнение нейросети и готовое сообщение продавцу."
  />`;
}
