// Temporary screen used until Frontend B fills a route module. Safe to delete afterwards.
import { html } from "../lib/html.js";
import { PageHeader, Card, EmptyState, Skeleton } from "../ui/index.js";

export function ScreenPlaceholder({ title, subtitle, icon, note, action }) {
  return html`<div class="placeholder-screen">
    <${PageHeader} title=${title} subtitle=${subtitle} />
    <${Card}>
      <${EmptyState}
        compact
        icon=${icon}
        tone="brand"
        title="Этот экран скоро появится"
        message=${note}
        action=${action}
      />
      <div class="grid mt-4" style=${{ "--min": "200px" }}>
        <${Skeleton} variant="card" />
        <${Skeleton} variant="card" />
        <${Skeleton} variant="card" />
      </div>
    <//>
  </div>`;
}

