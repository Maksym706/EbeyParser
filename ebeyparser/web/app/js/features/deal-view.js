// Сделка — the deal detail used by the full page (/deal/:id) and the feed's side drawer.
// Order (brief §4.3): gallery → title → decision → red flags → profit breakdown → price chart →
// comparables → AI findings → description → seller & safety → checklist → composer →
// status actions & notes → why this score.
import { html, cx, useState, useEffect, useRef } from "../lib/html.js";
import { useAsync, useDebouncedCallback, useIsMobile, useLocalState } from "../lib/hooks.js";
import { onEvent } from "../lib/events.js";
import { money, percent, ago, dateTime, plural, whenTime } from "../lib/format.js";
import { api } from "../lib/api.js";
import { Icon, Button, IconButton, Badge, Skeleton, EmptyState, ErrorState, Tooltip, toast, SaveState, Checkbox } from "../ui/index.js";
import "./icons-extra.js";
import {
  normalizeCard,
  decide,
  explainFlags,
  markSeen,
  productType,
  CHECKLISTS,
  SAFETY_CHECKLIST,
  TYPE_LABELS,
  STATUS,
  PIPELINE,
  HIDE_REASONS,
  distanceText,
  sourceLabel,
  humanOffer,
  secondsLeft,
  actionOf,
  isAuction,
  ruCondition,
  ruAttrKey,
  ruAttrValue,
  ruTag,
  ruDateText,
  ruNumbers,
  dedupe,
} from "./deal-model.js";
import { dealsApi, applyUpdate, setStatus, toggleStar, hideDeal, unhideDeal, moveTo, markBought, markSold, writeToSeller } from "./deal-actions.js";
import { Gallery } from "./gallery.js";
import { PriceChart, normalizeHistory, marketCaption } from "./price-chart.js";
import { Composer } from "./composer.js";
import { Popover, Menu } from "./popover.js";
import { Countdown } from "./deal-card.js";
import { copyText, quickMessage } from "./messages.js";

const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ------------------------------------------------------------------ data
/** Load the detail; seed it with the list card for an instant first paint. */
function useDeal(id, initial) {
  const q = useAsync(() => dealsApi.get(id), [id]);
  const [local, setLocal] = useState(null);
  useEffect(() => setLocal(null), [id]);
  useEffect(() => {
    if (q.data) setLocal(null);
  }, [q.data]);
  useEffect(
    () =>
      onEvent("deal_updated", (data) => {
        if (!data || String(data.ad_id ?? (data.card && data.card.id)) !== String(id)) return;
        if (data.local) setLocal((prev) => applyUpdate(prev || { id: String(id) }, data));
        else q.reload(true);
      }),
    [id],
  );
  // useAsync keeps the previous result while loading: ignore it when it belongs to another deal
  const card = q.data ? normalizeCard(q.data) : null;
  const fresh = card && String(card.id) === String(id) ? { ...card, ...q.data, id: String(card.id) } : null;
  const base = fresh || (initial && String(initial.id) === String(id) ? { ...initial } : null);
  const deal = base && local ? { ...base, ...stripId(local) } : base;
  return { deal, loading: !fresh && !q.error, error: fresh ? null : q.error, reload: q.reload, full: Boolean(fresh) };
}

const stripId = ({ id, ...rest }) => rest;

// ------------------------------------------------------------------ main
/**
 * <DealView id initial mode="page|drawer" onClose onPrev onNext />
 */
export function DealView({ id, initial, mode = "page", onClose, onPrev, onNext, backHref = "/" }) {
  const { deal, loading, error, reload, full } = useDeal(id, initial);
  const isMobile = useIsMobile();
  const [offer, setOffer] = useState(null);
  const scroller = useRef(null);

  useEffect(() => {
    markSeen(id);
    setOffer(null);
    const el = scroller.current && scroller.current.closest(".drawer__body");
    if (el) el.scrollTop = 0;
  }, [id]);

  if (error && !deal) {
    const notFound = error.status === 404 && !error.missing;
    return html`<div class="dv dv--state" ref=${scroller}>
      ${notFound
        ? html`<${EmptyState}
            icon="search-x"
            title="Объявление не найдено"
            message="Возможно, оно удалено из базы или ссылка неполная."
            action=${html`<${Button} variant="primary" href="/" icon="zap">В ленту<//>`}
          />`
        : html`<${ErrorState} error=${error} onRetry=${() => reload()} />`}
    </div>`;
  }
  if (!deal) return html`<div ref=${scroller}><${DealSkeleton} /></div>`;

  const dec = decide(deal);
  const flags = explainFlags(deal);
  const images = (deal.listing && deal.listing.images && deal.listing.images.length ? deal.listing.images : [deal.image]).filter(Boolean);
  const stock = Boolean((deal.ai && deal.ai.stock_photos) || (deal.ai_second && deal.ai_second.stock_photos));

  return html`<div class=${cx("dv", `dv--${mode}`)} ref=${scroller}>
    ${mode === "page" && html`<${PageBar} deal=${deal} backHref=${backHref} onPrev=${onPrev} onNext=${onNext} reload=${reload} />`}

    <div class="dv__grid">
      <div class="dv__col dv__col--media">
        ${(deal.unchecked || deal.ai_checked === false) &&
        html`<div class="callout tone-haggle"><${Icon} name="triangle-alert" size=${18} /><div><b>Фото не проверены нейросетью</b> — посмотри сам внимательно.</div></div>`}
        <${Gallery} images=${images} title=${deal.title} stock=${stock} />
        <${TitleBlock} deal=${deal} menu=${mode === "drawer" ? html`<${Menu} items=${overflowItems(deal, reload)} />` : null} />
      </div>

      <div class="dv__col dv__col--main">
        <${DecisionBlock} deal=${deal} dec=${dec} offer=${offer} setOffer=${setOffer} />
        ${flags.length > 0 && html`<${RedFlags} flags=${flags} />`}
        ${loading && !full
          ? html`<div class="dv__loading"><${Skeleton} h=${150} radius="var(--r-lg)" /><${Skeleton} h=${120} radius="var(--r-lg)" /></div>`
          : html`
              <${Breakdown} deal=${deal} offer=${offer} />
              <${Market} deal=${deal} tone=${dec.tone} />
              <${AiFindings} deal=${deal} />
              <${Description} deal=${deal} />
              <${SellerBlock} deal=${deal} />
              <${MeetChecklist} deal=${deal} />
              ${dec.kind !== "bid" &&
              html`<section class="dv-sec" id="composer">
                <h3 class="dv-sec__title"><${Icon} name="message-square" size=${18} />Сообщение продавцу</h3>
                <${Composer} deal=${deal} offer=${offer} />
              </section>`}
              <${StatusBlock} deal=${deal} />
              <${Notes} deal=${deal} />
              <${WhyScore} deal=${deal} />
            `}
      </div>
    </div>
    ${isMobile && html`<${BottomBar} deal=${deal} dec=${dec} />`}
  </div>`;
}

// ------------------------------------------------------------------ bars
function overflowItems(deal, reload) {
  return [
    deal.url && { label: deal.source === "ebay" ? "Открыть на eBay" : "Открыть на Kleinanzeigen", icon: "external-link", href: deal.url },
    {
      label: "Скопировать ссылку на объявление",
      icon: "link",
      onClick: () => copyText(deal.url || "").then((ok) => (ok ? toast.success("Ссылка скопирована") : toast.error("Не получилось скопировать"))),
    },
    {
      label: "Ссылка на эту страницу",
      icon: "share-2",
      onClick: () => copyText(`${location.origin}/deal/${encodeURIComponent(deal.id)}`).then((ok) => ok && toast.success("Ссылка скопирована")),
    },
    {
      label: "Переоценить",
      icon: "refresh-cw",
      hint: "заново посчитать рынок и спросить нейросеть",
      onClick: () => reevaluate(deal, reload),
    },
    { divider: true },
    deal.status === "ignored"
      ? { label: "Вернуть в ленту", icon: "undo-2", onClick: () => unhideDeal(deal) }
      : { label: "Скрыть", icon: "eye-off", danger: true, onClick: () => hideDeal(deal) },
  ];
}

async function reevaluate(deal, reload) {
  const id = toast({ kind: "loading", title: "Переоцениваю… это может занять до минуты" });
  try {
    const res = await dealsApi.reevaluate(deal.id);
    const jobId = res && (res.job_id || (res.job && res.job.id));
    if (jobId) await api.waitJob(jobId, { timeout: 15 * 60 * 1000 });
    toast({ id, kind: "success", title: "Готово — оценка обновлена" });
    reload();
  } catch (e) {
    toast({ id, kind: "error", title: e.missing ? "Переоценка появится в следующем обновлении" : `Не получилось: ${e.message}` });
  }
}

function PageBar({ deal, backHref, onPrev, onNext, reload }) {
  const share = () => {
    const url = `${location.origin}/deal/${encodeURIComponent(deal.id)}`;
    if (navigator.share) navigator.share({ title: deal.title, url }).catch(() => {});
    else copyText(url).then((ok) => ok && toast.success("Ссылка скопирована"));
  };
  return html`<div class="dv-bar dv-bar--page">
    <a class="dv-bar__back" href=${backHref} onClick=${(e) => {
      if (history.state && history.state.__inApp) {
        e.preventDefault();
        history.back();
      }
    }}><${Icon} name="chevron-left" size=${20} /><span>Назад</span></a>
    <span class="grow"></span>
    ${onPrev && html`<${IconButton} icon="chevron-up" label="Предыдущая" size="sm" onClick=${onPrev} />`}
    ${onNext && html`<${IconButton} icon="chevron-down" label="Следующая" size="sm" onClick=${onNext} />`}
    <${IconButton} icon="share-2" label="Поделиться" size="sm" onClick=${share} />
    ${deal.url && html`<${Button} size="sm" variant="secondary" iconRight="arrow-up-right" href=${deal.url} class="dv-bar__open">Открыть<//>`}
    <${Menu} items=${overflowItems(deal, reload)} />
  </div>`;
}

function BottomBar({ deal, dec }) {
  const starred = deal.status === "starred";
  const primary =
    dec.kind === "bid"
      ? html`<${Button} variant="primary" icon="external-link" href=${deal.url}>Открыть на eBay<//>`
      : ["contacted", "bought", "sold"].includes(deal.status)
        ? deal.status === "contacted"
          ? html`<${Button} variant="primary" icon="package-check" onClick=${() => markBought(deal)}>Купил<//>`
          : deal.status === "bought"
            ? html`<${Button} variant="primary" icon="banknote" onClick=${() => markSold(deal)}>Продал<//>`
            : html`<${Button} variant="secondary" icon="external-link" href=${deal.url}>Объявление<//>`
        : html`<${Button} variant="primary" icon="message-square" onClick=${() => writeToSeller(deal)}>Написать<//>`;
  return html`<div class="dv-bottom">
    <button type="button" class=${cx("dv-bottom__icon", starred && "is-on")} aria-pressed=${starred} aria-label=${starred ? "Убрать из избранного" : "В избранное"} onClick=${() => toggleStar(deal)}>
      <${Icon} name="star" size=${22} />
    </button>
    <${Menu}
      label="Статус"
      icon="ellipsis"
      align="start"
      items=${[
        { label: "Написал продавцу", icon: "message-square", onClick: () => moveTo(deal, "contacted") },
        { label: "Купил за…", icon: "package-check", onClick: () => markBought(deal) },
        { label: "Продал за…", icon: "banknote", onClick: () => markSold(deal) },
        { divider: true },
        dec.kind !== "bid" && { label: "Скопировать сообщение продавцу", icon: "copy", onClick: () => copyText(quickMessage(deal)).then((ok) => ok && toast.success("Сообщение скопировано")) },
        deal.status === "ignored"
          ? { label: "Вернуть в ленту", icon: "undo-2", onClick: () => unhideDeal(deal) }
          : { label: "Скрыть", icon: "eye-off", danger: true, onClick: () => hideDeal(deal) },
      ]}
    />
    <div class="dv-bottom__primary">${primary}</div>
  </div>`;
}

// ------------------------------------------------------------------ title
function TitleBlock({ deal, menu }) {
  const place = distanceText(deal);
  const posted = ruDateText(deal.posted_at_ru || deal.posted_at_text);
  const condition = deal.condition_ru || ruCondition(deal.condition);
  const shipping =
    deal.shipping_cost != null && deal.shipping_cost > 0
      ? `+ ${money(deal.shipping_cost, { cents: deal.shipping_cost % 1 !== 0 })} доставка`
      : deal.shipping_possible
        ? "доставка возможна"
        : deal.shipping_possible === false
          ? "только самовывоз"
          : "";
  return html`<div class="dv-title">
    <div class="dv-title__row">
      <h2 class="dv-title__h">${deal.title}</h2>
      ${menu}
    </div>
    <div class="dv-title__price">
      <span class="price-xl num">${deal.is_free ? "Бесплатно" : money(deal.price)}</span>
      ${deal.negotiable && html`<${Tooltip} text="VB — продавец готов торговаться"><span class="vb">VB</span><//>`}
      ${shipping && html`<span class="dv-title__ship">${shipping}</span>`}
    </div>
    <div class="dv-title__meta">
      ${place && html`<span><${Icon} name="map-pin" size=${14} />${place}</span>`}
      ${deal.first_seen && html`<span title=${dateTime(deal.first_seen)}><${Icon} name="clock" size=${14} />${ago(deal.first_seen)}</span>`}
      ${posted && html`<span class="muted">опубликовано ${posted}</span>`}
    </div>
    <div class="dv-title__chips">
      <span class="tag">${sourceLabel(deal)}</span>
      ${deal.search_name &&
      html`<a class="tag tag--link" href=${`/?search=${encodeURIComponent(deal.search_name)}`}><${Icon} name="radar" size=${12} />${deal.search_name}</a>`}
      ${deal.purpose === "personal" && html`<span class="tag tone-profit"><${Icon} name="piggy-bank" size=${12} />Для себя</span>`}
      ${condition && html`<span class="tag" title=${deal.condition && deal.condition !== condition ? deal.condition : undefined}><${Icon} name="package" size=${12} />${condition}</span>`}
    </div>
  </div>`;
}

// ------------------------------------------------------------------ decision
function DecisionBlock({ deal, dec, offer, setOffer }) {
  const haggle = dec.kind === "haggle" || (dec.kind === "personal" && actionOf(deal) === "haggle");
  const bid = dec.kind === "bid";
  const buyPrice = deal.buy_price ?? deal.price;
  const baseProfit = deal.profit;
  // profit is linear in the buy price: profit(x) = profit + (buyPrice - x)
  const profitAt = (x) => (baseProfit != null && buyPrice != null && x != null ? baseProfit + (buyPrice - x) : null);
  const suggested =
    deal.offer_price != null
      ? humanOffer(deal.offer_price)
      : deal.max_buy_price != null && deal.price != null
        ? humanOffer(Math.min(deal.max_buy_price, deal.price))
        : deal.price;
  const current = offer ?? suggested;
  const min = deal.price != null ? Math.floor((deal.price * 0.6) / 5) * 5 : 0;
  const max = Math.max(deal.price ?? 0, Math.ceil(((deal.max_buy_price ?? deal.price ?? 100) * 1.1) / 5) * 5);
  const over = deal.max_buy_price != null && current != null && current > deal.max_buy_price;
  const pAt = profitAt(current);
  const personal = deal.purpose === "personal";
  const endsAt = deal.auction && deal.auction.ends_at;
  const left = secondsLeft(endsAt);

  return html`<section class=${cx("dblock", `tone-${dec.tone}`)}>
    <div class="dblock__head">
      <span class="dblock__icon"><${Icon} name=${dec.icon} size=${22} stroke=${2.25} /></span>
      <div class="grow">
        <div class="dblock__title">${dec.title}</div>
        ${dec.sub && html`<div class="dblock__sub num">${dec.sub}</div>`}
        ${bid &&
        endsAt &&
        html`<div class="dblock__sub">
          конец через <b><${Countdown} endsAt=${endsAt} /></b>
          ${left > 0 && html` (${whenTime(endsAt)})`}
        </div>`}
      </div>
      ${deal.score != null &&
      html`<${Tooltip} text=${`Оценка ${Math.round(deal.score)} из 100`}><span class="dblock__score num">${Math.round(deal.score)}</span><//>`}
    </div>

    ${haggle &&
    deal.price != null &&
    html`<div class=${cx("offer", over && "is-over")}>
      <div class="offer__row">
        <span>Моё предложение</span>
        <b class="num">${money(current)}</b>
        <span class=${cx("offer__profit num", pAt != null && (pAt >= 0 ? "t-green" : "t-red"))}>
          ${over ? "уже невыгодно" : pAt != null ? `${personal ? "экономия" : "прибыль"} ${money(pAt, { sign: !personal })}` : ""}
        </span>
      </div>
      <input
        type="range"
        class="offer__range"
        min=${min}
        max=${max}
        step="5"
        value=${current}
        aria-label="Сумма предложения"
        aria-valuetext=${money(current)}
        style=${{ "--pct": `${((current - min) / Math.max(1, max - min)) * 100}%`, "--limit": deal.max_buy_price != null ? `${((deal.max_buy_price - min) / Math.max(1, max - min)) * 100}%` : "100%" }}
        onInput=${(e) => setOffer(Number(e.currentTarget.value))}
      />
      <div class="offer__scale num">
        <span>${money(min)}</span>
        ${deal.max_buy_price != null && html`<span class="offer__limit">выгодно до ${money(deal.max_buy_price)}</span>`}
        <span>${money(max)}</span>
      </div>
    </div>`}

    ${bid &&
    html`<p class="dblock__tip"><${Icon} name="info" size=${16} />Не повышай ставку раньше времени. Поставь свой максимум за 5–10 секунд до конца — eBay сам доторгуется за тебя.</p>`}

    <div class="dblock__actions">
      ${bid
        ? html`<${Button} variant="primary" icon="external-link" href=${deal.url}>Открыть на eBay<//>`
        : html`<${Button}
            variant="primary"
            icon="message-square"
            onClick=${() => {
              const el = document.getElementById("composer");
              if (el) el.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
            }}
            >${haggle ? `Написать с предложением ${money(humanOffer(current))}` : "Написать продавцу"}<//
          >`}
      ${!bid && deal.url && html`<${Button} variant="secondary" iconRight="arrow-up-right" href=${deal.url}>Открыть объявление<//>`}
    </div>
  </section>`;
}

function RedFlags({ flags }) {
  const scam = flags.some((f) => f.scam);
  return html`<section class=${cx("flags", scam ? "tone-danger" : "tone-haggle")} role=${scam ? "alert" : "note"}>
    <h3 class="flags__title"><${Icon} name=${scam ? "shield-alert" : "triangle-alert"} size=${18} />${scam ? "Осторожно" : "Обрати внимание"}</h3>
    <ul class="flags__list">
      ${flags.map(
        (f) => html`<li class=${cx("flags__item", !f.scam && "is-soft")}>
          <${Icon} name=${f.scam ? "shield-alert" : "triangle-alert"} size=${16} />
          <div><b>${f.text}</b>${f.why && html`<span>${f.why}</span>`}</div>
        </li>`,
      )}
    </ul>
  </section>`;
}

// ------------------------------------------------------------------ breakdown
const COST_ROWS = ["margin", "fees", "shipping", "buy"];

function Breakdown({ deal, offer }) {
  const b = deal.breakdown;
  const personal = deal.purpose === "personal";
  const [atOffer, setAtOffer] = useState(false);
  useEffect(() => {
    if (offer != null) setAtOffer(true);
  }, [offer]);
  if (!b || !b.available) {
    if (!b) return null;
    return html`<section class="dv-sec">
      <h3 class="dv-sec__title"><${Icon} name="calculator" size=${18} />${personal ? "Сколько сэкономлю" : "Расчёт прибыли"}</h3>
      <p class="muted">${b.reason_ru || "Посчитать не получилось"}</p>
    </section>`;
  }
  const offerPrice = offer ?? deal.offer_price;
  const canOffer = offerPrice != null && deal.price != null && offerPrice < deal.price;
  const buyRow = b.rows.find((r) => r.key === "buy");
  const buyBase = buyRow ? -buyRow.value : deal.buy_price ?? deal.price;
  const useOffer = canOffer && atOffer;
  const rows = b.rows.map((r) =>
    r.key === "buy" && useOffer ? { ...r, value: -offerPrice, note: `моё предложение (в объявлении ${money(deal.price)})` } : r,
  );
  const total = useOffer ? b.total + (buyBase - offerPrice) : b.total;
  const roi = useOffer ? (offerPrice ? total / offerPrice : null) : b.roi;
  const market = (rows.find((r) => r.key === "market") || {}).value;
  // stacked bar: how the market price splits into costs, buy price and profit
  const parts = market
    ? [
        { key: "buy", v: Math.max(0, -(rows.find((r) => r.key === "buy") || { value: 0 }).value), cls: "seg-buy", label: "цена покупки" },
        { key: "costs", v: Math.max(0, -rows.filter((r) => ["margin", "fees", "shipping", "other"].includes(r.key) && r.value < 0).reduce((s, r) => s + r.value, 0)), cls: "seg-cost", label: "запас и расходы" },
        { key: "profit", v: Math.max(0, total), cls: "seg-profit", label: personal ? "экономия" : "прибыль" },
      ]
    : [];
  const sum = parts.reduce((s, p) => s + p.v, 0) || 1;

  return html`<section class="dv-sec">
    <div class="dv-sec__head">
      <h3 class="dv-sec__title"><${Icon} name="calculator" size=${18} />${personal ? "Сколько сэкономлю" : "Расчёт прибыли"}</h3>
      ${canOffer &&
      html`<label class="mini-toggle">
        <input type="checkbox" checked=${useOffer} onChange=${(e) => setAtOffer(e.currentTarget.checked)} />
        <span>при моём предложении ${money(humanOffer(offerPrice))}</span>
      </label>`}
    </div>
    <table class="wf">
      <tbody>
        ${rows.map(
          (r, i) => html`<tr class=${cx((r.value < 0 || COST_ROWS.includes(r.key)) && "is-minus")}>
            <th scope="row">${i > 0 ? (COST_ROWS.includes(r.key) || r.value < 0 ? "− " : "+ ") : ""}${r.label}${r.note && html`<small>${ruNumbers(r.note)}</small>`}</th>
            <td class="num">${COST_ROWS.includes(r.key) && !r.value ? "0 €" : money(r.value, { sign: r.key === "other" })}</td>
          </tr>`,
        )}
      </tbody>
      <tfoot>
        <tr class=${cx("wf__total", total >= 0 ? "is-pos" : "is-neg")}>
          <th scope="row">= ${useOffer ? (personal ? "Экономия" : "Чистая прибыль") : b.total_label || "Итого"}</th>
          <td class="num">${money(total, { sign: !personal })}</td>
        </tr>
        ${roi != null &&
        !personal &&
        html`<tr class="wf__roi">
          <th scope="row"><${Tooltip} text="Сколько заработаешь на каждый вложенный евро"><span class="dotted">ROI</span><//></th>
          <td class="num">${percent(roi)}</td>
        </tr>`}
      </tfoot>
    </table>
    ${parts.length > 0 &&
    html`<div class="stackbar" role="img" aria-label=${parts.map((p) => `${p.label} ${money(p.v)}`).join(", ")}>
        ${parts.filter((p) => p.v > 0).map((p) => html`<span class=${p.cls} style=${{ flexGrow: p.v / sum }} title=${`${p.label}: ${money(p.v)}`}></span>`)}
      </div>
      <div class="stackbar__legend">
        ${parts.map((p) => html`<span><i class=${p.cls}></i>${p.label}</span>`)}
      </div>`}
    <div class="dv-sec__foot">
      <span><${Icon} name="database" size=${14} />Источник рынка: ${b.market_source_label || deal.market_source_label || "—"}${b.query ? html` · «${b.query}»` : ""}</span>
      <${Popover}
        label="Как считается"
        trigger=${(p) => html`<button type="button" class="linkish" ...${p}><${Icon} name="circle-help" size=${14} />Как считается?</button>`}
      >
        <div class="explain">
          <b>Как я считаю прибыль</b>
          <p>Беру рыночную цену — за сколько такое реально продаётся — и вычитаю запас на торг и риск, комиссии и твою доставку покупателю. Потом вычитаю цену покупки.</p>
          <p>Рыночную цену беру из своей истории цен похожих объявлений, из похожих объявлений на Kleinanzeigen и из реальных продаж на eBay.</p>
          ${b.notes && html`<p class="muted">${b.notes}</p>`}
        </div>
      <//>
    </div>
  </section>`;
}

// ------------------------------------------------------------------ market
function Market({ deal, tone }) {
  const est = (deal.evaluation && deal.evaluation.estimate) || {};
  const comps = est.comparables || [];
  const hist = useAsync(() => dealsApi.market(deal.id, 0).then(normalizeHistory), [deal.id]);
  const [all, setAll] = useState(false);
  const history = hist.data;
  const enough = history && history.points.filter((p) => p.t).length >= 3;
  const market = deal.market_price ?? est.market_price;
  const caption =
    (enough && history.caption) ||
    marketCaption({
      market,
      count: est.sample_size || comps.length,
      days: est.history_days,
      soldCount: comps.filter((c) => c.sold).length || null,
    });
  const shown = all ? comps : comps.slice(0, 5);
  if (!market && !comps.length && !enough) return null;
  const withMax = history ? { ...history, maxBuy: history.maxBuy ?? deal.max_buy_price } : null;

  return html`<section class="dv-sec">
    <h3 class="dv-sec__title"><${Icon} name="chart-line" size=${18} />Рынок</h3>
    ${hist.loading && !history
      ? html`<${Skeleton} h=${160} radius="var(--r-md)" />`
      : enough
        ? html`<${PriceChart} history=${withMax} price=${deal.price} tone=${tone} adTime=${deal.first_seen} />`
        : html`<p class="muted small">${hist.error && !hist.error.missing ? "График недоступен." : "Истории цен ещё мало — оценка по похожим объявлениям."}</p>`}
    ${caption && html`<p class="dv-sec__caption">${caption}${est.warning ? html` · <span class="t-amber">${est.warning}</span>` : ""}</p>`}
    ${comps.length > 0 &&
    html`<div class=${cx("comps", `tone-${tone}`)}>
      <div class="comps__head">Похожие предложения</div>
      <div class="comps__row comps__row--self">
        <span class="comps__title">Это объявление</span>
        <span class="comps__price num">${money(deal.price)}</span>
      </div>
      ${shown.map(
        (c) => html`<a class="comps__row" href=${c.url || undefined} target="_blank" rel="noopener noreferrer">
          <span class="comps__title">${c.title}</span>
          <span class="comps__meta">
            ${c.sold && !/продано/i.test(c.source_label || "") && html`<span class="tag tone-info">продано</span>`}
            <span class=${cx("tag", c.sold && "tone-info")}>${c.source_label || c.source}</span>
            ${(c.date_ru || c.date_text) && ruDateText(c.date_ru || c.date_text) && html`<span class="muted">${ruDateText(c.date_ru || c.date_text)}</span>`}
          </span>
          <span class="comps__price num">${money(c.price)}</span>
        </a>`,
      )}
      ${comps.length > 5 &&
      html`<button type="button" class="linkish comps__more" onClick=${() => setAll(!all)}>
        ${all ? "Свернуть" : `Показать все ${comps.length}`}<${Icon} name=${all ? "chevron-up" : "chevron-down"} size=${14} />
      </button>`}
    </div>`}
  </section>`;
}

// ------------------------------------------------------------------ AI
function YesNo({ value, yes = "Да", no = "Нет", unknown = "Неясно", invert = false }) {
  if (value === true) return html`<span class=${invert ? "t-amber" : "t-green"}><${Icon} name=${invert ? "triangle-alert" : "check"} size=${14} />${yes}</span>`;
  if (value === false) return html`<span class=${invert ? "t-green" : "t-red"}><${Icon} name=${invert ? "check" : "x"} size=${14} />${no}</span>`;
  return html`<span class="muted">${invert ? unknown : `? ${unknown}`}</span>`;
}

function AiPanel({ ai, title = "Что увидела нейросеть", second = false }) {
  if (!ai) return null;
  if (ai.failed) {
    return html`<div class="ai ai--failed"><${Icon} name="triangle-alert" size=${16} />Нейросеть не ответила — фото не проверены. ${ai.reasoning || ""}</div>`;
  }
  const conf = ai.confidence_percent ?? Math.round((ai.confidence || 0) * 100);
  const vTone = { buy: "profit", maybe: "haggle", skip: "danger" }[ai.verdict] || "neutral";
  const vLabel = { buy: "Покупай", maybe: "Подумай", skip: "Не выгодно" }[ai.verdict] || ai.verdict_label || ai.verdict;
  return html`<div class=${cx("ai", second && "ai--second")}>
    <div class="ai__head">
      <${Icon} name="scan-eye" size=${18} />
      <span class="ai__title">${title}</span>
      ${ai.model && html`<code class="ai__model">${ai.model}</code>`}
      ${ai.verdict && html`<${Badge} tone=${vTone} size="sm">${vLabel}<//>`}
    </div>
    <dl class="ai__grid">
      ${ai.product && html`<div><dt>Товар</dt><dd>${ai.product}</dd></div>`}
      <div><dt>Состояние</dt><dd>${ai.condition_label || ai.condition || "—"}</dd></div>
      <div><dt>Фото = описание</dt><dd><${YesNo} value=${ai.photo_matches_description} /></dd></div>
      <div><dt>Блокировка</dt><dd><${YesNo} value=${ai.locked} yes="⚠ возможна" no="не видно" unknown="не видно" invert /></dd></div>
      <div><dt>Фото из интернета</dt><dd><${YesNo} value=${ai.stock_photos} yes="похоже" no="нет" unknown="неясно" invert /></dd></div>
      ${ai.item_type && ai.item_type !== "unclear" && html`<div><dt>Что продают</dt><dd>${ai.item_type_label || ai.item_type}</dd></div>`}
      ${ai.defects && ai.defects.length > 0 &&
      html`<div class="ai__wide"><dt>Дефекты</dt><dd class="ai__chips">${ai.defects.map((d) => html`<span class="tag tone-haggle">${d}</span>`)}</dd></div>`}
      ${ai.estimated_market_price != null && html`<div><dt>Оценка цены ИИ</dt><dd class="num">${money(ai.estimated_market_price)}</dd></div>`}
      <div class="ai__wide">
        <dt>Уверенность</dt>
        <dd class="ai__conf"><span class="gauge"><span style=${{ width: `${conf}%` }}></span></span><b class="num">${conf} %</b></dd>
      </div>
    </dl>
    ${ai.reasoning && html`<blockquote class="ai__quote">${ai.reasoning}</blockquote>`}
  </div>`;
}

function AiFindings({ deal }) {
  const [second, setSecond] = useState(false);
  if (!deal.ai && !deal.ai_second) return null;
  return html`<section class="dv-sec">
    <${AiPanel} ai=${deal.ai} />
    ${deal.ai_second &&
    html`<button type="button" class="dv-disc" aria-expanded=${second} onClick=${() => setSecond(!second)}>
        <${Icon} name=${second ? "chevron-up" : "chevron-down"} size=${16} />Второе мнение${deal.ai_second.model ? ` (${deal.ai_second.model})` : ""}
      </button>
      ${second && html`<${AiPanel} ai=${deal.ai_second} title="Второе мнение" second />`}`}
  </section>`;
}

function Description({ deal }) {
  const text = deal.listing && deal.listing.description;
  const listing = deal.listing || {};
  // the server's Russian labels ([{key, key_ru, value, value_ru}]) when present, else our own mapping
  const attrs = Array.isArray(listing.attributes_ru)
    ? listing.attributes_ru.map((a) => [a.key_ru || ruAttrKey(a.key), a.value_ru || ruAttrValue(a.key, a.value), a.key])
    : Object.entries(listing.attributes || {}).map(([k, v]) => [ruAttrKey(k), ruAttrValue(k, v), k]);
  const [open, setOpen] = useState(false);
  if (!text && !attrs.length) return null;
  const long = text && text.length > 420;
  return html`<section class="dv-sec">
    <h3 class="dv-sec__title"><${Icon} name="file-text" size=${18} />Описание продавца</h3>
    ${attrs.length > 0 &&
    html`<dl class="attrs">${attrs.map(([k, v, de]) => html`<div><dt title=${de && de !== k ? de : undefined}>${k}</dt><dd>${v}</dd></div>`)}</dl>`}
    ${text &&
    html`<p class=${cx("desc", long && !open && "is-clamped")} lang="de">${text}</p>
      ${long && html`<button type="button" class="linkish" onClick=${() => setOpen(!open)}>${open ? "Свернуть" : "Показать полностью"}</button>`}`}
  </section>`;
}

// ------------------------------------------------------------------ seller & safety
function SellerBlock({ deal }) {
  const s = deal.seller || {};
  const tags = (deal.listing && deal.listing.tags) || [];
  const opts = (deal.listing && deal.listing.buying_options) || [];
  const [safetyOpen, setSafetyOpen] = useLocalState("ebp.safety.open", true);
  const commercial = s.commercial || deal.seller_type === "commercial";
  const chips = [
    s.type_label || (deal.seller_type === "private" ? "Частное лицо" : null)
      ? html`<span class=${cx("tag", commercial && "tone-haggle")} title=${commercial ? "У коммерческих нет торга, зато есть гарантия" : ""}>
          <${Icon} name=${commercial ? "store" : "user-round"} size=${12} />${s.type_label || "Частное лицо"}
        </span>`
      : null,
    s.name ? html`<span class="tag">${s.name}</span>` : null,
    s.feedback_percent != null
      ? html`<span class="tag tone-info">${String(s.feedback_percent).replace(".", ",")} %${s.feedback_score != null ? ` · ${s.feedback_score} ${plural(s.feedback_score, "отзыв", "отзыва", "отзывов")}` : ""}</span>`
      : null,
    ...dedupe([...tags.map(ruTag), ...opts.map((o) => ruTag((o && o.label) || (o && o.key) || o))]).map(
      (t) => html`<span class="tag"><${Icon} name=${/доставк|самовывоз/i.test(t) ? "truck" : /аукцион/i.test(t) ? "gavel" : "tag"} size=${12} />${t}</span>`,
    ),
  ].filter(Boolean);
  return html`<section class="dv-sec">
    ${chips.length > 0 &&
    html`<h3 class="dv-sec__title"><${Icon} name="user-round" size=${18} />Продавец</h3>
      <div class="chips-row">${chips}</div>
      ${commercial && html`<p class="muted small">Коммерческий продавец — торг маловероятен, зато есть гарантия.</p>`}`}
    <div class=${cx("safety", !safetyOpen && "is-collapsed")}>
      <button type="button" class="safety__head" aria-expanded=${safetyOpen} onClick=${() => setSafetyOpen(!safetyOpen)}>
        <${Icon} name="shield-check" size=${18} /><b>Безопасная сделка</b><${Icon} name=${safetyOpen ? "chevron-up" : "chevron-down"} size=${16} />
      </button>
      ${safetyOpen &&
      html`<p>Плати наличными при встрече или через «Sicher bezahlen». Не переходи в WhatsApp, не плати «PayPal Freunde», не открывай ссылки на оплату из сообщений.</p>`}
    </div>
  </section>`;
}

function MeetChecklist({ deal }) {
  const type = productType(deal);
  const items = [...(CHECKLISTS[type] || []), ...SAFETY_CHECKLIST];
  const [checked, setChecked] = useLocalState(`ebp.check.${deal.id}`, []);
  const copy = () => {
    const text = `Проверь при встрече — ${deal.title}\n` + items.map((t) => `${checked.includes(t) ? "☑" : "☐"} ${t}`).join("\n");
    copyText(text).then((ok) => (ok ? toast.success("Чек-лист скопирован") : toast.error("Не получилось скопировать")));
  };
  return html`<section class="dv-sec">
    <div class="dv-sec__head">
      <h3 class="dv-sec__title"><${Icon} name="clipboard-check" size=${18} />Проверь при встрече${type !== "generic" && html`<span class="tag">${TYPE_LABELS[type]}</span>`}</h3>
      <button type="button" class="linkish" onClick=${copy}><${Icon} name="copy" size=${14} />Скопировать чек-лист</button>
    </div>
    <div class="checks">
      ${items.map(
        (t) => html`<${Checkbox}
          checked=${checked.includes(t)}
          label=${t}
          onChange=${(v) => setChecked(v ? [...checked, t] : checked.filter((x) => x !== t))}
        />`,
      )}
    </div>
  </section>`;
}

// ------------------------------------------------------------------ status
function StatusBlock({ deal }) {
  const status = deal.status || "new";
  const idx = PIPELINE.indexOf(status);
  const p = deal.pipeline || {};
  const bought = deal.bought_price ?? p.bought_price;
  const sold = deal.sold_price ?? p.sold_price;
  const real = deal.realized_profit ?? p.realized_profit ?? (sold != null && bought != null ? sold - bought : null);
  const expected = p.expected_profit ?? deal.profit;
  return html`<section class="dv-sec" id="status">
    <div class="dv-sec__head">
      <h3 class="dv-sec__title"><${Icon} name="wallet" size=${18} />Мои сделки</h3>
      ${status === "ignored" && html`<${Badge} tone="neutral" icon="eye-off">Скрыто<//>`}
    </div>
    <ol class="pstep" aria-label="Этап сделки">
      ${PIPELINE.map((key, i) => {
        const done = idx >= i;
        const cur = idx === i;
        return html`<li class=${cx("pstep__item", done && "is-done", cur && "is-current")}>
          <button
            type="button"
            onClick=${() => (cur ? (key === "starred" ? setStatus(deal, "new", { message: "Убрано из избранного" }) : null) : moveTo(deal, key))}
            aria-current=${cur ? "step" : undefined}
            title=${cur ? (key === "starred" ? "Убрать из избранного" : "") : `Отметить: ${STATUS[key].label}`}
          >
            <span class="pstep__dot"><${Icon} name=${done ? "check" : STATUS[key].icon} size=${14} stroke=${2.5} /></span>
            <span class="pstep__label">${STATUS[key].label}</span>
          </button>
        </li>`;
      })}
    </ol>
    ${(bought != null || sold != null) &&
    html`<div class="pmoney">
      ${bought != null && html`<div><span>Купил за</span><b class="num">${money(bought)}</b>${(deal.bought_at || p.bought_at) && html`<small>${ago(deal.bought_at || p.bought_at)}</small>`}</div>`}
      ${sold != null && html`<div><span>Продал за</span><b class="num">${money(sold)}</b>${(deal.sold_at || p.sold_at) && html`<small>${ago(deal.sold_at || p.sold_at)}</small>`}</div>`}
      ${real != null &&
      html`<div class=${real >= 0 ? "is-pos" : "is-neg"}>
        <span>Реальная прибыль</span><b class="num">${money(real, { sign: true })}</b>
        ${expected != null && html`<small>прогноз ${money(expected, { sign: true })}</small>`}
      </div>`}
    </div>`}
    <div class="status-actions">
      <${Button} size="sm" variant=${status === "starred" ? "soft" : "secondary"} icon="star" onClick=${() => toggleStar(deal)}>
        ${status === "starred" ? "В избранном" : "В избранное"}
      <//>
      <${Button} size="sm" variant="secondary" icon="message-square" onClick=${() => moveTo(deal, "contacted")} disabled=${idx >= 1}>Написал<//>
      <${Button} size="sm" variant="secondary" icon="package-check" onClick=${() => markBought(deal)} disabled=${idx >= 2}>Купил за…<//>
      <${Button} size="sm" variant="secondary" icon="banknote" onClick=${() => markSold(deal)} disabled=${idx >= 3}>Продал за…<//>
      ${status === "ignored"
        ? html`<${Button} size="sm" variant="ghost" icon="undo-2" onClick=${() => unhideDeal(deal)}>Вернуть<//>`
        : html`<${HideButton} deal=${deal} />`}
    </div>
  </section>`;
}

export function HideButton({ deal, size = "sm" }) {
  return html`<${Popover}
    label="Почему скрыть"
    width=${280}
    trigger=${(p) => html`<button type="button" class=${`btn btn--ghost btn--${size}`} ...${p}><${Icon} name="eye-off" size=${16} /><span class="btn__label">Скрыть</span></button>`}
  >
    ${(close) => html`<div class="hide-pop">
      <b>Почему скрываешь?</b>
      <p class="muted small">Так я лучше пойму, что тебе не подходит.</p>
      <div class="chips-row">
        ${HIDE_REASONS.map(
          (r) => html`<button type="button" class="chip chip--sm" onClick=${() => (close(), hideDeal(deal, r.key))}>${r.label}</button>`,
        )}
      </div>
      <button type="button" class="linkish" onClick=${() => (close(), hideDeal(deal))}>Просто скрыть</button>
    </div>`}
  <//>`;
}

function Notes({ deal }) {
  const [text, setText] = useState(deal.note || "");
  const [state, setState] = useState(null);
  const last = useRef(deal.note || "");
  useEffect(() => {
    setText(deal.note || "");
    last.current = deal.note || "";
  }, [deal.id]);
  const save = async (value) => {
    if (value === last.current) return;
    setState("saving");
    try {
      await dealsApi.patch(deal.id, { note: value }, deal);
      last.current = value;
      setState("saved");
      setTimeout(() => setState((s) => (s === "saved" ? null : s)), 2000);
    } catch (e) {
      setState("error");
      toast.error(`Заметка не сохранилась: ${e.message}`);
    }
  };
  const debounced = useDebouncedCallback(save, 800);
  return html`<section class="dv-sec">
    <div class="dv-sec__head">
      <h3 class="dv-sec__title"><${Icon} name="notebook-pen" size=${18} />Заметка</h3>
      <${SaveState} state=${state} />
    </div>
    <textarea
      class="note"
      rows="3"
      placeholder="Например: написал в 14:10, продавец отвечает вечером"
      value=${text}
      onInput=${(e) => {
        setText(e.currentTarget.value);
        debounced(e.currentTarget.value);
      }}
      onBlur=${() => save(text)}
    ></textarea>
  </section>`;
}

function WhyScore({ deal }) {
  const reasons = (deal.evaluation && deal.evaluation.reasons) || deal.reasons || [];
  const [open, setOpen] = useState(false);
  if (!reasons.length) return null;
  const ev = deal.evaluation || {};
  return html`<section class="dv-sec dv-sec--quiet">
    <button type="button" class="dv-disc" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <${Icon} name=${open ? "chevron-up" : "chevron-down"} size=${16} />Почему такая оценка${deal.score != null ? ` · ${Math.round(deal.score)} из 100` : ""}
    </button>
    ${open &&
    html`<ul class="why">
        ${reasons.map((r) => html`<li>${r}</li>`)}
      </ul>
      ${ev.stage_label && html`<p class="muted small">Глубина проверки: ${ev.stage_label}${ev.evaluated_at ? ` · ${ago(ev.evaluated_at)}` : ""}</p>`}`}
  </section>`;
}

// ------------------------------------------------------------------ skeleton
export function DealSkeleton() {
  return html`<div class="dv dv--skeleton" aria-busy="true" aria-label="Загрузка">
    <div class="dv__grid">
      <div class="dv__col">
        <span class="skeleton" style="display:block;aspect-ratio:4/3;border-radius:var(--r-md)"></span>
        <span class="skeleton" style="display:block;width:80%;height:22px;margin-top:16px"></span>
        <span class="skeleton" style="display:block;width:40%;height:28px;margin-top:10px"></span>
      </div>
      <div class="dv__col">
        <span class="skeleton" style="display:block;height:140px;border-radius:var(--r-xl)"></span>
        <span class="skeleton" style="display:block;height:180px;border-radius:var(--r-lg);margin-top:16px"></span>
      </div>
    </div>
  </div>`;
}
