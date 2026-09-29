// Мои сделки — pipeline Избранное → Написал → Купил → Продал (brief §4.4).
// Desktop: 4 kanban columns with drag & drop (buttons work too); phone: segmented list.
// Data: GET /api/v1/pipeline (columns + summary); moves go through PATCH /deals/{id}.
import { html, cx, useState, useEffect, useRef } from "../lib/html.js";
import { useAsync, useMediaQuery, useNow, BREAKPOINTS } from "../lib/hooks.js";
import { api, authToken } from "../lib/api.js";
import { onEvent } from "../lib/events.js";
import { navigate } from "../lib/router.js";
import { money, plural, ago } from "../lib/format.js";
import { Icon, PageHeader, ErrorState, Skeleton, Tooltip } from "../ui/index.js";
import { useTopbar } from "../shell/topbar.js";
import "../features/icons-extra.js";
import { setBadge } from "../features/badges.js";
import { normalizeCard, STATUS, PIPELINE, decide } from "../features/deal-model.js";
import { applyUpdate, moveTo, markBought, markSold, setStatus, writeToSeller } from "../features/deal-actions.js";
import { DealImage, DecisionPill } from "../features/deal-card.js";
import { openAd } from "../features/messages.js";

const HINTS = {
  starred: "Нажимай ☆ на находках — они соберутся здесь",
  contacted: "Когда напишешь продавцу — отметь «Написал», я напомню проверить ответ",
  bought: "Отмечай покупки — посчитаю, сколько вложено",
  sold: "Здесь будет твоя реальная прибыль",
};
const TITLES = { starred: "Избранное", contacted: "Написал", bought: "Купил", sold: "Продал" };
const DAY = 86400000;

function daysSince(iso, now) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : Math.floor((now - t) / DAY);
}

// ------------------------------------------------------------------ data
function usePipeline() {
  const q = useAsync(() => api.get("/pipeline"), []);
  const [local, setLocal] = useState({}); // id -> patch (optimistic moves)
  const timer = useRef(null);
  useEffect(() => {
    const later = () => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => q.reload(true), 700);
    };
    const offs = [
      onEvent("deal_updated", (data) => {
        if (data && data.local) setLocal((m) => ({ ...m, [data.ad_id]: { ...(m[data.ad_id] || {}), ...data.patch } }));
        later();
      }),
      onEvent("data_changed", later),
      onEvent("connected", later),
    ];
    return () => {
      offs.forEach((off) => off());
      clearTimeout(timer.current);
    };
  }, []);
  useEffect(() => {
    if (q.data) setLocal({});
  }, [q.data]);

  // regroup the cards by (possibly optimistic) status
  let columns = null;
  if (q.data && Array.isArray(q.data.columns)) {
    const all = q.data.columns.flatMap((c) => (c.items || []).map((it) => normalizeCard(it)));
    const cards = all.map((c) => (local[c.id] ? applyUpdate(c, { ad_id: c.id, patch: local[c.id] }) : c));
    columns = PIPELINE.map((key) => {
      const src = q.data.columns.find((c) => c.key === key) || {};
      const items = cards.filter((c) => c.status === key);
      const moved = items.length !== (src.items || []).length;
      return { key, title: src.title_ru || TITLES[key], count: moved ? items.length : src.count ?? items.length, amount: moved ? null : src.amount, items };
    });
  }
  return { ...q, columns, summary: q.data && q.data.summary, hidden: q.data ? q.data.hidden : 0 };
}

// ------------------------------------------------------------------ screen
export default function PipelineScreen({ params = {} }) {
  const isPhone = useMediaQuery(BREAKPOINTS.phone);
  const now = useNow(60000);
  const p = usePipeline();
  const col = PIPELINE.includes(params.column) ? params.column : "starred";
  useTopbar({ title: "Мои сделки" }, []);
  useEffect(() => {
    if (p.summary && p.summary.followups != null) setBadge("deals", p.summary.followups);
  }, [p.summary]);

  const csv = api.url("/export/deals.csv", authToken() ? { token: authToken() } : null);

  return html`<div class="pipe-screen">
    <${PageHeader}
      title="Мои сделки"
      subtitle="От находки до продажи — и сколько ты реально заработал"
      actions=${html`<a class="btn btn--secondary btn--md" href=${csv} download data-native><${Icon} name="download" size=${18} /><span class="btn__label">Скачать CSV</span></a>`}
    />
    ${p.error && !p.data
      ? html`<${ErrorState} error=${p.error} onRetry=${() => p.reload()} />`
      : html`
          <${Tiles} summary=${p.summary} loading=${!p.data} />
          ${!p.columns
            ? html`<${BoardSkeleton} phone=${isPhone} />`
            : isPhone
              ? html`<${PhoneList} columns=${p.columns} col=${col} now=${now} />`
              : html`<${Board} columns=${p.columns} now=${now} />`}
          ${p.hidden > 0 &&
          html`<a class="pipe-archive" href="/?st=ignored"><${Icon} name="eye-off" size=${16} />Скрытые и «не вышло»: ${p.hidden}<${Icon} name="chevron-right" size=${16} /></a>`}
        `}
  </div>`;
}

// ------------------------------------------------------------------ tiles
function Spark({ values = [] }) {
  if (values.length < 2) return null;
  const w = 96;
  const h = 32;
  const max = Math.max(1, ...values.map((v) => Math.abs(v)));
  const min = Math.min(0, ...values);
  const x = (i) => (i / (values.length - 1)) * (w - 6) + 3;
  const y = (v) => h - 3 - ((v - min) / (max - min || 1)) * (h - 6);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = values.length - 1;
  return html`<svg class="spark" width=${w} height=${h} viewBox=${`0 0 ${w} ${h}`} aria-hidden="true">
    <path d=${d} fill="none" />
    <circle cx=${x(last)} cy=${y(values[last])} r="3" />
  </svg>`;
}

function Tiles({ summary, loading }) {
  if (loading && !summary)
    return html`<div class="tiles">${[0, 1, 2, 3].map(() => html`<div class="tile"><${Skeleton} w="50%" h=${10} /><${Skeleton} w="70%" h=${28} /><${Skeleton} w="60%" h=${12} /></div>`)}</div>`;
  const s = summary || {};
  const months = (s.earned_by_month || []).map((m) => m.profit || 0);
  const acc = s.accuracy;
  return html`<div class="tiles">
    <div class="tile">
      <div class="overline">Заработано за месяц</div>
      <div class="tile__row">
        <div class=${cx("tile__value", s.earned_month > 0 && "t-green", s.earned_month < 0 && "t-red")}>${money(s.earned_month || 0, { sign: true })}</div>
        <${Spark} values=${months} />
      </div>
      <div class="tile__sub">всего ${money(s.realized_total || 0, { sign: true })} · ${s.sold_count || 0} ${plural(s.sold_count || 0, "продажа", "продажи", "продаж")}</div>
    </div>
    <div class="tile">
      <div class="overline">Вложено сейчас</div>
      <div class="tile__value">${money(s.invested || 0)}</div>
      <div class="tile__sub">
        ${s.in_stock ? `в ${s.in_stock} ${plural(s.in_stock, "вещи", "вещах", "вещах")}` : "пока ничего не куплено"}
        ${s.stale_stock ? html` · <span class="t-amber">${s.stale_stock} залежалось</span>` : ""}
      </div>
    </div>
    <div class="tile">
      <div class="overline">Ожидаемая прибыль в наличии</div>
      <div class=${cx("tile__value", s.expected_in_stock > 0 && "t-green")}>${money(s.expected_in_stock || 0, { sign: true })}</div>
      <div class="tile__sub">если продашь по рынку</div>
    </div>
    <div class="tile">
      <div class="overline tile__label">
        Точность прогноза
        <${Tooltip} text="Сравниваю прибыль, которую я обещал, с тем, сколько ты реально заработал. Нужно минимум 3 продажи." placement="bottom-end">
          <span class="tile__help" tabindex="0" aria-label="Что это"><${Icon} name="circle-help" size=${14} /></span>
        <//>
      </div>
      ${acc
        ? html`<div class=${cx("tile__value", acc.avg_delta_percent >= -5 ? "t-green" : acc.avg_delta_percent < -25 ? "t-red" : "t-amber")}>
              ${acc.avg_delta_percent > 0 ? "+" : acc.avg_delta_percent < 0 ? "−" : ""}${Math.abs(acc.avg_delta_percent)} %
            </div>
            <div class="tile__sub">${acc.message_ru} · ${acc.n} ${plural(acc.n, "сделка", "сделки", "сделок")}</div>`
        : html`<div class="tile__value tile__value--muted">—</div>
            <div class="tile__sub">появится после 3 продаж</div>`}
    </div>
  </div>`;
}

// ------------------------------------------------------------------ board (desktop)
function Board({ columns, now }) {
  const [over, setOver] = useState(null);
  const drag = useRef(null);
  const drop = (key) => {
    const card = drag.current;
    setOver(null);
    drag.current = null;
    if (!card || card.status === key) return;
    moveTo(card, key);
  };
  return html`<div class="board">
    ${columns.map(
      (c) => html`<section
        class=${cx("board__col", over === c.key && "is-over")}
        aria-label=${c.title}
        onDragOver=${(e) => {
          if (!drag.current) return;
          e.preventDefault();
          if (over !== c.key) setOver(c.key);
        }}
        onDragLeave=${(e) => {
          if (!e.currentTarget.contains(e.relatedTarget)) setOver(null);
        }}
        onDrop=${(e) => {
          e.preventDefault();
          drop(c.key);
        }}
      >
        <${ColumnHead} col=${c} />
        <div class="board__list">
          ${c.items.length
            ? c.items.map(
                (d) => html`<${PipeCard}
                  key=${d.id}
                  deal=${d}
                  col=${c.key}
                  now=${now}
                  draggable
                  onDragStart=${(e) => {
                    drag.current = d;
                    e.dataTransfer.effectAllowed = "move";
                    try {
                      e.dataTransfer.setData("text/plain", d.id);
                    } catch {
                      /* ignore */
                    }
                  }}
                  onDragEnd=${() => {
                    drag.current = null;
                    setOver(null);
                  }}
                />`,
              )
            : html`<div class="board__empty"><${Icon} name=${STATUS[c.key].icon} size=${20} />${HINTS[c.key]}</div>`}
        </div>
      </section>`,
    )}
  </div>`;
}

function ColumnHead({ col }) {
  const sum = col.amount;
  return html`<header class=${cx("board__head", `st-${col.key}`)}>
    <span class="board__icon"><${Icon} name=${STATUS[col.key].icon} size=${16} /></span>
    <span class="board__title">${col.title}</span>
    <span class="board__count num">${col.count}</span>
    <span class="grow"></span>
    ${sum != null && sum !== 0 && html`<span class=${cx("board__sum num", col.key === "sold" && (sum >= 0 ? "t-green" : "t-red"))}>${money(sum, { sign: col.key === "sold" })}</span>`}
  </header>`;
}

// ------------------------------------------------------------------ phone
function PhoneList({ columns, col, now }) {
  const current = columns.find((c) => c.key === col) || columns[0];
  return html`<div class="pipe-phone">
    <div class="pipe-seg" role="tablist" aria-label="Этапы">
      ${columns.map(
        (c) => html`<button
          type="button"
          role="tab"
          aria-selected=${c.key === current.key}
          class=${cx(c.key === current.key && "is-on")}
          onClick=${() => navigate(`/deals/${c.key}`, { replace: true, scroll: false })}
        >
          <span>${c.title}</span><b class="num">${c.count}</b>
        </button>`,
      )}
    </div>
    <div class="pipe-phone__list">
      ${current.items.length
        ? current.items.map((d) => html`<${PipeCard} key=${d.id} deal=${d} col=${current.key} now=${now} />`)
        : html`<div class="board__empty board__empty--big"><${Icon} name=${STATUS[current.key].icon} size=${28} />${HINTS[current.key]}</div>`}
    </div>
  </div>`;
}

// ------------------------------------------------------------------ card
function PipeCard({ deal, col, now, ...drag }) {
  const open = (e) => {
    if (e.target.closest("button, a")) return;
    navigate(`/deal/${encodeURIComponent(deal.id)}?from=deals`);
  };
  const stop = (fn) => (e) => {
    e.stopPropagation();
    fn();
  };
  const ageDays = daysSince(deal.first_seen, now);
  const contactedHours = deal.contacted_at || deal.status_changed_at ? (now - new Date(deal.contacted_at || deal.status_changed_at)) / 3600000 : null;
  const stockDays = daysSince(deal.bought_at, now);
  const soldDays = deal.sold_at && deal.bought_at ? Math.max(0, Math.round((new Date(deal.sold_at) - new Date(deal.bought_at)) / DAY)) : null;
  const paid = deal.bought_price ?? deal.buy_price ?? deal.price;
  const real = deal.realized_profit;

  let body = null;
  let actions = null;
  if (col === "starred") {
    body = html`<${DecisionPill} deal=${deal} size="sm" class="dpill--inline" />
      ${ageDays != null && ageDays >= 3 && html`<span class="pcard__warn"><${Icon} name="clock" size=${13} />может быть уже продано — объявлению ${ageDays} дн.</span>`}`;
    // an auction is not negotiated in a chat: the move is a bid on eBay (P1-4)
    actions = html`${decide(deal).kind === "bid"
        ? html`<button type="button" class="pcard__btn pcard__btn--main" onClick=${stop(() => openAd(deal.url))}><${Icon} name="external-link" size=${15} />На eBay</button>`
        : html`<button type="button" class="pcard__btn pcard__btn--main" onClick=${stop(() => writeToSeller(deal))}><${Icon} name="message-square" size=${15} />Написать</button>`}
      <button type="button" class="pcard__btn" onClick=${stop(() => markBought(deal))}>Купил</button>`;
  } else if (col === "contacted") {
    const nudge = contactedHours != null && contactedHours >= 24;
    body = html`<span class="pcard__line">написал ${deal.contacted_at || deal.status_changed_at ? ago(deal.contacted_at || deal.status_changed_at, now) : ""}${deal.offer_price != null ? html` · предложил ~<b class="num">${money(deal.offer_price)}</b>` : ""}</span>
      ${nudge && html`<span class="pcard__nudge"><${Icon} name="bell-ring" size=${13} />Продавец ответил?</span>`}`;
    actions = html`<button type="button" class="pcard__btn pcard__btn--main" onClick=${stop(() => markBought(deal))}><${Icon} name="package-check" size=${15} />Купил</button>
      <button
        type="button"
        class="pcard__btn"
        onClick=${stop(() => setStatus(deal, "ignored", { extra: { hidden_reason: "lost" }, message: "Не вышло — убрал в архив" }))}
      >
        Не вышло
      </button>`;
  } else if (col === "bought") {
    const stale = stockDays != null && stockDays > 21;
    body = html`<span class="pcard__line">купил за <b class="num">${money(paid)}</b>${deal.market_price != null ? html` · продать ~<b class="num">${money(deal.market_price)}</b>` : ""}</span>
      ${stockDays != null &&
      html`<span class=${cx("pcard__line", stale && "t-amber")}>
        ${stale ? html`<span class="pcard__chip"><${Icon} name="hourglass" size=${12} />залежалось</span>` : ""} ${stockDays > 0 ? `в наличии ${stockDays} ${plural(stockDays, "день", "дня", "дней")}` : "куплено сегодня"}
      </span>`}`;
    actions = html`<button type="button" class="pcard__btn pcard__btn--main" onClick=${stop(() => markSold(deal))}><${Icon} name="banknote" size=${15} />Продал</button>`;
  } else if (col === "sold") {
    body = html`<span class="pcard__line num">${money(paid)} → ${money(deal.sold_price)}</span>
      ${real != null && html`<span class=${cx("pcard__profit num", real >= 0 ? "t-green" : "t-red")}>${money(real, { sign: true })}</span>`}
      <span class="pcard__line muted">
        ${deal.profit != null ? `прогноз ${money(deal.profit, { sign: true })}` : ""}${real != null ? ` · факт ${money(real, { sign: true })}` : ""}${soldDays != null ? (soldDays > 0 ? ` · продано за ${soldDays} ${plural(soldDays, "день", "дня", "дней")}` : " · продано в тот же день") : ""}
      </span>`;
  }
  const d = decide(deal);
  return html`<article class=${cx("pcard", `tone-${d.tone}`)} onClick=${open} ...${drag} tabindex="0" onKeyDown=${(e) => e.key === "Enter" && open(e)}>
    <div class="pcard__top">
      <${DealImage} src=${deal.image} class="pcard__img" />
      <div class="pcard__main">
        <a class="pcard__title" href=${`/deal/${encodeURIComponent(deal.id)}?from=deals`}>${deal.title}</a>
        <span class="pcard__price num">${deal.is_free ? "Бесплатно" : money(deal.price)}${deal.negotiable ? html` <span class="vb">VB</span>` : ""}</span>
      </div>
    </div>
    <div class="pcard__body">${body}</div>
    ${actions && html`<div class="pcard__actions">${actions}</div>`}
  </article>`;
}

function BoardSkeleton({ phone }) {
  if (phone)
    return html`<div class="pipe-phone"><${Skeleton} h=${40} radius="var(--r-md)" />${[0, 1, 2].map(() => html`<${Skeleton} h=${96} radius="var(--r-lg)" />`)}</div>`;
  return html`<div class="board">
    ${PIPELINE.map(
      () => html`<section class="board__col">
        <${Skeleton} w="60%" h=${16} />
        <${Skeleton} h=${104} radius="var(--r-md)" />
        <${Skeleton} h=${104} radius="var(--r-md)" />
      </section>`,
    )}
  </div>`;
}

