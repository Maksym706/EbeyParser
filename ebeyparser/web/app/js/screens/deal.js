// Сделка — one deal on its own page (brief §4.3). Telegram / e-mail links point here;
// the feed shows the same view in a side drawer on desktop.
import { html, useEffect } from "../lib/html.js";
import { useTopbar } from "../shell/topbar.js";
import { DealView } from "../features/deal-view.js";

export default function DealScreen({ params, query = {} }) {
  useTopbar({ title: "Сделка" }, []);
  useEffect(() => window.scrollTo(0, 0), [params.id]);
  const back = query.from === "deals" ? "/deals" : "/";
  return html`<div class="deal-page"><${DealView} key=${params.id} id=${params.id} mode="page" backHref=${back} /></div>`;
}
