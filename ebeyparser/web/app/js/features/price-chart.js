// Keepa-style market price chart (brief §4.3.6): asking prices of the same product over time,
// p25–p75 band, median line, «выгодно до» reference line, this ad as a highlighted dot.
// Inline SVG sized to its container; hover/tap shows the nearest price, click opens it.
import { html, cx, useState, useEffect, useRef, useMemo } from "../lib/html.js";
import { money, plural } from "../lib/format.js";
import { Icon } from "../ui/index.js";

const DAY = 86400000;

function toTime(v) {
  if (!v) return null;
  const t = new Date(v).getTime();
  return Number.isNaN(t) ? null : t;
}

function quantile(sorted, q) {
  if (!sorted.length) return null;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/** GET /deals/{id}/market → { points, median, p25, p75, maxBuy, medianLine, caption, … }. */
export function normalizeHistory(res) {
  if (!res || typeof res !== "object") return null;
  const raw = res.points || res.price_points || res.items || (Array.isArray(res) ? res : []);
  const all = raw
    .map((p) => ({
      price: Number(p.price),
      t: toTime(p.seen_at || p.at || p.date || p.first_seen),
      source: p.source_label || p.source || "",
      sold: Boolean(p.sold),
      url: p.url || "",
      title: p.title || "",
      self: Boolean(p.this_ad || p.self),
    }))
    .filter((p) => Number.isFinite(p.price) && p.price > 0);
  const stats = res.stats || {};
  return {
    points: all.filter((p) => !p.self),
    median: stats.median ?? res.median ?? null,
    p25: stats.p25 ?? res.p25 ?? null,
    p75: stats.p75 ?? res.p75 ?? null,
    count: stats.count ?? null,
    soldCount: stats.sold_count ?? null,
    avgListedDays: stats.avg_days_listed ?? null,
    maxBuy: res.max_buy_price ?? null,
    medianLine: (res.median_line || []).map((m) => ({ t: toTime(m.date), v: Number(m.median) })).filter((m) => m.t && Number.isFinite(m.v)),
    days: res.days ?? null,
    caption: res.caption_ru || "",
    available: res.available !== false,
    product: res.product || "",
  };
}

const RANGES = [
  { key: 30, label: "30 дн" },
  { key: 60, label: "60 дн" },
  { key: 0, label: "Всё" },
];

function niceTicks(min, max, count = 4) {
  const span = max - min || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const start = Math.ceil(min / step) * step;
  const out = [];
  for (let v = start; v <= max + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

const dayLabel = (t) => new Date(t).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
const srcLabel = (s) => ({ kleinanzeigen: "Kleinanzeigen", ebay: "eBay", ebay_sold: "eBay · продано", history: "история" }[s] || s || "");

/**
 * <PriceChart history={normalized} price={290} tone="profit" adTime={iso} />
 * Renders nothing but a caption when there is too little data (caller shows comparables).
 */
export function PriceChart({ history, price, tone = "profit", adTime, height = 160 }) {
  const wrap = useRef(null);
  const [width, setWidth] = useState(520);
  const [range, setRange] = useState(60);
  const [hover, setHover] = useState(null);

  useEffect(() => {
    if (!wrap.current) return undefined;
    const measure = () => wrap.current && setWidth(Math.max(260, Math.round(wrap.current.getBoundingClientRect().width)));
    measure();
    if (!("ResizeObserver" in window)) return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  const now = Date.now();
  const H = history || {};
  const all = H.points || [];
  const dated = all.filter((p) => p.t);
  const visible = useMemo(() => {
    const from = range ? now - range * DAY : -Infinity;
    return dated.filter((p) => p.t >= from).sort((a, b) => a.t - b.t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history, range]);

  const prices = visible.map((p) => p.price).sort((a, b) => a - b);
  const median = range === 0 && H.median != null ? H.median : quantile(prices, 0.5) ?? H.median;
  const p25 = range === 0 && H.p25 != null ? H.p25 : quantile(prices, 0.25) ?? H.p25;
  const p75 = range === 0 && H.p75 != null ? H.p75 : quantile(prices, 0.75) ?? H.p75;
  const maxBuy = H.maxBuy;

  const pad = { l: 46, r: maxBuy != null ? 92 : 16, t: 14, b: 24 };
  const h = height;
  const w = width;
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;

  const adT = toTime(adTime) || now;
  const tMin = visible.length ? Math.min(visible[0].t, adT) : now - (range || 60) * DAY;
  const tMax = Math.max(now, adT);
  const vals = [...prices, price, maxBuy, median].filter((v) => v != null && Number.isFinite(v));
  let yMin = vals.length ? Math.min(...vals) : 0;
  let yMax = vals.length ? Math.max(...vals) : 100;
  const padY = (yMax - yMin || yMax || 50) * 0.12;
  yMin = Math.max(0, yMin - padY);
  yMax = yMax + padY;
  const ticks = niceTicks(yMin, yMax, 3);
  const x = (t) => pad.l + ((t - tMin) / Math.max(1, tMax - tMin)) * iw;
  const y = (v) => pad.t + (1 - (v - yMin) / Math.max(1e-6, yMax - yMin)) * ih;

  if (dated.length < 3) return null;

  const pick = (px, py) => {
    let best = null;
    let bestD = Infinity;
    for (const p of visible) {
      const d = Math.hypot(x(p.t) - px, (y(p.price) - py) * 0.6);
      if (d < bestD) {
        bestD = d;
        best = p;
      }
    }
    return bestD < 60 ? best : null;
  };

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHover(pick(e.clientX - r.left, e.clientY - r.top));
  };

  const onKey = (e) => {
    if (!visible.length) return;
    const i = hover ? visible.indexOf(hover) : -1;
    if (e.key === "ArrowRight") setHover(visible[Math.min(visible.length - 1, i + 1)]);
    else if (e.key === "ArrowLeft") setHover(visible[Math.max(0, i < 0 ? visible.length - 1 : i - 1)]);
    else if (e.key === "Enter" && hover && hover.url) window.open(hover.url, "_blank", "noopener,noreferrer");
    else return;
    e.preventDefault();
  };

  const xTicks = [tMin, tMin + (tMax - tMin) / 2, tMax];
  const line = (H.medianLine || []).filter((m) => m.t >= tMin);
  const medianPath =
    line.length >= 2 ? line.map((m, i) => `${i ? "L" : "M"}${x(Math.min(m.t + DAY / 2, tMax)).toFixed(1)},${y(m.v).toFixed(1)}`).join(" ") : null;
  const tip = hover
    ? {
        left: Math.min(w - 150, Math.max(0, x(hover.t) - 70)),
        top: Math.max(0, y(hover.price) - 58),
      }
    : null;

  return html`<div class="pchart">
    <div class="pchart__head">
      <div class="pchart__seg" role="tablist" aria-label="Период">
        ${RANGES.map(
          (r) => html`<button type="button" role="tab" aria-selected=${range === r.key} class=${cx(range === r.key && "is-on")} onClick=${() => (setRange(r.key), setHover(null))}>
            ${r.label}
          </button>`,
        )}
      </div>
      <span class="pchart__legend">
        <span class="lg lg--dot"></span>объявления
        ${visible.some((p) => p.sold) && html`<span class="lg lg--sold"></span>продано`}
        <span class=${cx("lg lg--self", `tone-${tone}`)}></span>это объявление
      </span>
    </div>
    <div class="pchart__plot" ref=${wrap} style=${{ height: `${h}px` }}>
      <svg
        width=${w}
        height=${h}
        viewBox=${`0 0 ${w} ${h}`}
        role="img"
        tabindex="0"
        aria-label=${`График цен: ${visible.length} ${plural(visible.length, "объявление", "объявления", "объявлений")}, медиана ${money(median)}`}
        onPointerMove=${onMove}
        onPointerLeave=${() => setHover(null)}
        onKeyDown=${onKey}
        onClick=${() => hover && hover.url && window.open(hover.url, "_blank", "noopener,noreferrer")}
        class=${cx(hover && hover.url && "is-link")}
      >
        ${ticks.map(
          (v) => html`<g>
            <line class="pc-grid" x1=${pad.l} x2=${w - pad.r} y1=${y(v)} y2=${y(v)} />
            <text class="pc-axis" x=${pad.l - 8} y=${y(v) + 4} text-anchor="end">${Math.round(v)}</text>
          </g>`,
        )}
        ${xTicks.map(
          (t, i) => html`<text class="pc-axis" x=${x(t)} y=${h - 6} text-anchor=${i === 0 ? "start" : i === 2 ? "end" : "middle"}>
            ${i === 2 ? "сегодня" : dayLabel(t)}
          </text>`,
        )}
        ${p25 != null && p75 != null && html`<rect class="pc-band" x=${pad.l} width=${iw} y=${y(p75)} height=${Math.max(1, y(p25) - y(p75))} rx="2" />`}
        ${medianPath
          ? html`<path class="pc-median" d=${medianPath} fill="none" />`
          : median != null && html`<line class="pc-median" x1=${pad.l} x2=${w - pad.r} y1=${y(median)} y2=${y(median)} />`}
        ${median != null && html`<text class="pc-label" x=${pad.l + 4} y=${y(median) - 6}>медиана ${money(median)}</text>`}
        ${maxBuy != null &&
        maxBuy >= yMin &&
        maxBuy <= yMax &&
        html`<g>
          <line class="pc-max" x1=${pad.l} x2=${w - pad.r} y1=${y(maxBuy)} y2=${y(maxBuy)} />
          <text class="pc-label pc-label--max" x=${w - pad.r + 6} y=${y(maxBuy) + 4}>выгодно до</text>
          <text class="pc-label pc-label--max strong" x=${w - pad.r + 6} y=${y(maxBuy) + 17}>${money(maxBuy)}</text>
        </g>`}
        ${hover && html`<line class="pc-cross" x1=${x(hover.t)} x2=${x(hover.t)} y1=${pad.t} y2=${h - pad.b} />`}
        ${visible.map(
          (p) => html`<circle
            class=${cx("pc-dot", p.sold && "is-sold", hover === p && "is-hover")}
            cx=${x(p.t)}
            cy=${y(p.price)}
            r=${hover === p ? 5 : 4}
          />`,
        )}
        ${price != null &&
        html`<g class=${cx("pc-self", `tone-${tone}`)}>
          <circle cx=${x(adT)} cy=${y(price)} r="6" />
          <text class="pc-label pc-label--self" x=${x(adT) - 10} y=${y(price) + (y(price) < pad.t + 18 ? 18 : -10)} text-anchor="end">${money(price)}</text>
        </g>`}
      </svg>
      ${tip &&
      html`<div class="pchart__tip" style=${{ left: `${tip.left}px`, top: `${tip.top}px` }} role="status">
        <b class="num">${money(hover.price)}</b>
        <span>${dayLabel(hover.t)} · ${hover.sold ? "продано" : srcLabel(hover.source)}</span>
        ${hover.url && html`<small>нажми, чтобы открыть <${Icon} name="arrow-up-right" size=${11} /></small>`}
      </div>`}
    </div>
  </div>`;
}

/** Caption under the chart: «Рынок ~420 € по 23 объявлениям за 60 дней · держатся в среднем 5 дней». */
export function marketCaption({ market, count, days, avgListedDays, soldCount }) {
  const parts = [];
  if (market != null) {
    let s = `Рынок ~${money(market)}`;
    if (count) s += ` по ${count} ${plural(count, "объявлению", "объявлениям", "объявлениям")}`;
    if (days) s += ` за ${days} ${plural(days, "день", "дня", "дней")}`;
    parts.push(s);
  }
  if (avgListedDays) parts.push(`держатся в среднем ${Math.round(avgListedDays)} ${plural(Math.round(avgListedDays), "день", "дня", "дней")}`);
  if (soldCount) parts.push(`продано на eBay: ${soldCount}`);
  return parts.join(" · ");
}
