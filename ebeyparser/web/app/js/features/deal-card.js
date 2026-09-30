// Deal card (grid) and deal row (phone / compact list) — brief §4.2.3, §4.2.4.
// Decision pill leads with the verb and the money; red/amber flag chip; star / write / hide.
import { html, cx, useState, useEffect, useRef } from "../lib/html.js";
import { useInterval } from "../lib/hooks.js";
import { money } from "../lib/format.js";
import { Icon, Tooltip } from "../ui/index.js";
import "./icons-extra.js";
import { decide, explainFlags, isSeen, isAuction, shortAge, ageMinutes, distanceText, secondsLeft, countdown } from "./deal-model.js";
import { toggleStar, hideDeal, unhideDeal, writeToSeller } from "./deal-actions.js";
import { openAd } from "./messages.js";
import { FoundByBadge, SuperMark, isSuper } from "./scout.js";

/** Re-render every `ms` while the element is on screen (auction countdowns). */
export function useVisibleTick(ref, ms, enabled = true) {
  const [visible, setVisible] = useState(false);
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!enabled || !ref.current || !("IntersectionObserver" in window)) {
      setVisible(enabled);
      return undefined;
    }
    const io = new IntersectionObserver((entries) => setVisible(entries.some((e) => e.isIntersecting)));
    io.observe(ref.current);
    return () => io.disconnect();
  }, [enabled]);
  useInterval(() => setTick((n) => n + 1), enabled && visible ? ms : null);
}

/** The coloured "what to do" pill. */
export function DecisionPill({ deal, size = "md", class: cls = "" }) {
  const ref = useRef(null);
  const auction = isAuction(deal) && deal.auction && deal.auction.ends_at;
  useVisibleTick(ref, 1000, Boolean(auction));
  const d = decide(deal);
  return html`<div ref=${ref} class=${cx("dpill", `tone-${d.tone}`, `dpill--${size}`, d.urgent && "is-urgent", cls)}>
    <${Icon} name=${d.icon} size=${size === "sm" ? 14 : 16} stroke=${2.25} />
    <span class="dpill__text num">${d.pill}</span>
  </div>`;
}

/** Glass chip over the photo: «Покупай · 87». */
export function ActionBadge({ deal }) {
  const d = decide(deal);
  return html`<${Tooltip} text=${deal.score != null ? `Оценка ${Math.round(deal.score)} из 100` : null}>
    <span class=${cx("abadge", `tone-${d.tone}`)}><span class="abadge__dot"></span>${d.badge}</span>
  <//>`;
}

function AiSignal({ deal }) {
  if (deal.ai_checked === false || deal.unchecked)
    return html`<span class="sig sig--warn"><${Icon} name="triangle-alert" size=${14} />Фото не проверены</span>`;
  if (deal.ai_checked)
    return html`<${Tooltip} text="Фото проверены нейросетью"><span class="sig sig--ai"><${Icon} name="scan-eye" size=${14} />ИИ</span><//>`;
  return null;
}

function FlagChip({ deal }) {
  const flags = explainFlags(deal);
  if (!flags.length) return null;
  const f = flags[0];
  return html`<div class=${cx("flagchip", f.scam ? "tone-danger" : "tone-haggle")} title=${f.why || f.text}>
    <${Icon} name=${f.scam ? "shield-alert" : "triangle-alert"} size=${14} />
    <span class="flagchip__text">${f.text}</span>
    ${flags.length > 1 && html`<span class="flagchip__more">+${flags.length - 1}</span>`}
  </div>`;
}

function Freshness({ deal, now }) {
  const status = deal.status;
  if (status === "contacted") return html`<span class="mchip mchip--status"><${Icon} name="message-square" size=${12} />Написал</span>`;
  if (status === "bought") return html`<span class="mchip mchip--status"><${Icon} name="package-check" size=${12} />Куплено</span>`;
  if (status === "sold") return html`<span class="mchip mchip--status"><${Icon} name="banknote" size=${12} />Продано</span>`;
  const mins = ageMinutes(deal.first_seen, now);
  if (mins == null) return null;
  return html`<span class="mchip"><span class=${cx("mchip__dot", mins < 15 && "is-fresh")}></span>${shortAge(deal.first_seen, now)}</span>`;
}

export function DealImage({ src, alt = "", class: cls = "", eager = false }) {
  const [state, setState] = useState(src ? "loading" : "error");
  const img = useRef(null);
  useEffect(() => {
    // cached / data: images can finish before the load listener is attached
    const el = img.current;
    if (!src) setState("error");
    else if (el && el.complete) setState(el.naturalWidth > 0 ? "ok" : "error");
    else setState("loading");
  }, [src]);
  return html`<span class=${cx("dimg", `is-${state}`, cls)}>
    ${src &&
    state !== "error" &&
    html`<img
      ref=${img}
      src=${src}
      alt=${alt}
      loading=${eager ? "eager" : "lazy"}
      decoding="async"
      referrerpolicy="no-referrer"
      onLoad=${() => setState("ok")}
      onError=${() => setState("error")}
    />`}
    ${state === "error" && html`<span class="dimg__ph"><${Icon} name="image" size=${28} stroke=${1.5} /></span>`}
  </span>`;
}

/** Shared quick-actions row. */
function QuickActions({ deal, compact = false }) {
  const starred = deal.status === "starred";
  const bid = decide(deal).kind === "bid";
  const stop = (fn) => (e) => {
    e.preventDefault();
    e.stopPropagation();
    fn();
  };
  return html`<div class=${cx("qa", compact && "qa--compact")}>
    ${bid
      ? html`<button type="button" class="qa__btn qa__btn--main" onClick=${stop(() => openAd(deal.url))} title="Открыть аукцион на eBay" aria-label="Открыть на eBay">
          <${Icon} name="external-link" size=${16} /><span>На eBay</span>
        </button>`
      : html`<button type="button" class="qa__btn qa__btn--main" onClick=${stop(() => writeToSeller(deal))} title="Скопировать сообщение продавцу и открыть объявление" aria-label="Написать продавцу">
          <${Icon} name="message-square" size=${16} /><span>Написать</span>
        </button>`}
    <button
      type="button"
      class=${cx("qa__btn", starred && "is-on")}
      aria-pressed=${starred}
      aria-label=${starred ? "Убрать из избранного" : "В избранное"}
      title=${starred ? "Убрать из избранного (s)" : "В избранное (s)"}
      onClick=${stop(() => toggleStar(deal))}
    >
      <${Icon} name="star" size=${16} />
    </button>
    ${deal.status === "ignored"
      ? html`<button type="button" class="qa__btn" aria-label="Вернуть в ленту" title="Вернуть в ленту" onClick=${stop(() => unhideDeal(deal))}>
          <${Icon} name="undo-2" size=${16} />${!compact && html`<span>Вернуть</span>`}
        </button>`
      : html`<button type="button" class="qa__btn" aria-label="Скрыть" title="Скрыть (h)" onClick=${stop(() => hideDeal(deal))}>
          <${Icon} name="eye-off" size=${16} />${!compact && html`<span>Скрыть</span>`}
        </button>`}
  </div>`;
}

/**
 * Grid card. `onOpen(deal, event)` — the feed opens the drawer; without it the link navigates.
 */
export function DealCard({ deal, now, onOpen, selected = false, glow = false }) {
  const d = decide(deal, { now });
  const unseen = !deal.seen && !isSeen(deal.id);
  const href = `/deal/${encodeURIComponent(deal.id)}`;
  const place = distanceText(deal);
  return html`<article
    class=${cx("dcard", `tone-${d.tone}`, selected && "is-selected", glow && "is-new", deal.status === "ignored" && "is-hidden", d.kind === "skip" && "is-skip", isSuper(deal) && "is-super")}
    data-deal=${deal.id}
  >
    <div class="dcard__media">
      <${DealImage} src=${deal.image} />
      <div class="dcard__tl"><${ActionBadge} deal=${deal} /></div>
      <button
        type="button"
        class=${cx("dcard__star", deal.status === "starred" && "is-on")}
        aria-pressed=${deal.status === "starred"}
        aria-label=${deal.status === "starred" ? "Убрать из избранного" : "В избранное"}
        onClick=${(e) => {
          e.preventDefault();
          e.stopPropagation();
          toggleStar(deal);
        }}
      >
        <${Icon} name="star" size=${16} stroke=${2.25} />
      </button>
      <div class="dcard__bl">
        <span class="mchip">${deal.source === "ebay" ? "eBay" : "KA"}${deal.images_count > 1 ? html`<span class="mchip__sep">·</span>1/${deal.images_count}` : null}</span>
      </div>
      <div class="dcard__br"><${Freshness} deal=${deal} now=${now} /></div>
    </div>
    <div class="dcard__body">
      <${SuperMark} deal=${deal} class="dcard__super" />
      <h3 class="dcard__title">
        ${unseen && html`<span class="unseen-dot" aria-label="Новое"></span>`}
        <a class="dcard__link" href=${href} onClick=${(e) => onOpen && onOpen(deal, e)}>${deal.title}</a>
      </h3>
      <div class="dcard__price">
        <span class="price-lg num">${deal.is_free ? "Бесплатно" : money(deal.price)}</span>
        ${deal.negotiable && html`<span class="vb">VB</span>`}
        ${deal.market_price != null && html`<span class="dcard__market num">рынок ~${money(deal.market_price)}</span>`}
      </div>
      <${DecisionPill} deal=${deal} />
      <div class="dcard__signals">
        ${place && html`<span class="sig"><${Icon} name="map-pin" size=${14} />${place}</span>`}
        <${AiSignal} deal=${deal} />
        <${FoundByBadge} deal=${deal} />
      </div>
      <${FlagChip} deal=${deal} />
    </div>
    <${QuickActions} deal=${deal} />
  </article>`;
}

/** Horizontal row: phone default and the desktop "compact" view. */
export function DealRow({ deal, now, onOpen, selected = false, glow = false }) {
  const d = decide(deal, { now });
  const unseen = !deal.seen && !isSeen(deal.id);
  const href = `/deal/${encodeURIComponent(deal.id)}`;
  const place = distanceText(deal);
  const age = shortAge(deal.first_seen, now);
  return html`<article
    class=${cx("drow", `tone-${d.tone}`, selected && "is-selected", glow && "is-new", deal.status === "ignored" && "is-hidden", d.kind === "skip" && "is-skip", isSuper(deal) && "is-super")}
    data-deal=${deal.id}
  >
    <div class="drow__media">
      <${DealImage} src=${deal.image} />
      <span class="drow__dot" title=${d.verb}></span>
    </div>
    <div class="drow__body">
      <${SuperMark} deal=${deal} class="drow__super" />
      <h3 class="drow__title">
        ${unseen && html`<span class="unseen-dot" aria-label="Новое"></span>`}
        <a class="dcard__link" href=${href} onClick=${(e) => onOpen && onOpen(deal, e)}>${deal.title}</a>
      </h3>
      <div class="drow__price">
        <span class=${cx("drow__verb", `tone-${d.tone}`)} title=${deal.score != null ? `Оценка ${Math.round(deal.score)} из 100` : undefined}>${d.badge}</span>
        <span class="price-md num">${deal.is_free ? "Бесплатно" : money(deal.price)}</span>
        ${deal.negotiable && html`<span class="vb">VB</span>`}
        ${deal.market_price != null && html`<span class="dcard__market num">~${money(deal.market_price)}</span>`}
      </div>
      <${DecisionPill} deal=${deal} size="sm" class="dpill--inline" />
      <div class="drow__signals">
        ${place && html`<span class="sig"><${Icon} name="map-pin" size=${13} />${place}</span>`}
        ${age && html`<span class="sig"><${Icon} name="clock" size=${13} />${age}</span>`}
        <${AiSignal} deal=${deal} />
        <${FoundByBadge} deal=${deal} />
      </div>
      <${FlagChip} deal=${deal} />
    </div>
    <${QuickActions} deal=${deal} compact />
  </article>`;
}

export function DealCardSkeleton() {
  return html`<div class="dcard dcard--skeleton" aria-hidden="true">
    <div class="dcard__media"><span class="skeleton sk-fill"></span></div>
    <div class="dcard__body">
      <span class="skeleton" style="width:85%;height:14px"></span>
      <span class="skeleton" style="width:55%;height:14px"></span>
      <span class="skeleton" style="width:40%;height:20px;margin-top:4px"></span>
      <span class="skeleton" style="width:100%;height:36px;border-radius:var(--r-md)"></span>
      <span class="skeleton" style="width:60%;height:12px"></span>
    </div>
  </div>`;
}

export function DealRowSkeleton() {
  return html`<div class="drow drow--skeleton" aria-hidden="true">
    <div class="drow__media"><span class="skeleton sk-fill"></span></div>
    <div class="drow__body">
      <span class="skeleton" style="width:90%;height:14px"></span>
      <span class="skeleton" style="width:40%;height:16px"></span>
      <span class="skeleton" style="width:65%;height:26px;border-radius:var(--r-md)"></span>
      <span class="skeleton" style="width:50%;height:12px"></span>
    </div>
  </div>`;
}

/** Countdown text that ticks every second while visible (bid blocks). */
export function Countdown({ endsAt, clock = true, class: cls = "" }) {
  const ref = useRef(null);
  useVisibleTick(ref, 1000, Boolean(endsAt));
  const left = secondsLeft(endsAt);
  return html`<span ref=${ref} class=${cx("num", left != null && left < 3600 && left > 0 && "t-red", cls)}>${countdown(left, { clock })}</span>`;
}
