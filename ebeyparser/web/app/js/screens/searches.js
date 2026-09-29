// Поиски — list / edit / toggle searches (Frontend B).
// Route: "/searches"   Data: GET /api/v1/searches, /searches/{id}, /searches/{id}/toggle, /searches/preview, /searches/apply
import { html } from "../lib/html.js";
import { ScreenPlaceholder } from "./placeholder.js";

export default function SearchesScreen() {
  return html`<${ScreenPlaceholder}
    title="Поиски"
    subtitle="Что и где программа ищет для тебя"
    icon="radar"
    note="Здесь будет список поисков с переключателями и счётчиками найденного."
  />`;
}
