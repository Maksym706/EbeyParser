// Photo gallery with a swipeable main image, thumbnails and a full-screen lightbox (brief §4.3.1).
import { html, cx, useState, useEffect, useRef } from "../lib/html.js";
import { Icon, Portal } from "../ui/index.js";
import "./icons-extra.js";

export function Gallery({ images = [], title = "", stock = false }) {
  const list = images.filter(Boolean);
  const [index, setIndex] = useState(0);
  const [zoom, setZoom] = useState(null);
  const strip = useRef(null);

  useEffect(() => {
    setIndex(0);
    if (strip.current) strip.current.scrollLeft = 0;
  }, [list[0]]);

  const go = (i) => {
    const n = (i + list.length) % list.length;
    const el = strip.current;
    const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (el) el.scrollTo({ left: n * el.clientWidth, behavior: still ? "auto" : "smooth" });
    setIndex(n);
  };
  const onScroll = (e) => {
    const el = e.currentTarget;
    const i = Math.round(el.scrollLeft / Math.max(1, el.clientWidth));
    if (i !== index) setIndex(i);
  };

  if (!list.length) {
    return html`<div class="gallery gallery--empty">
      <div class="gallery__main"><span class="dimg__ph"><${Icon} name="image" size=${36} stroke=${1.5} /><span>Фото нет</span></span></div>
    </div>`;
  }

  return html`<div class="gallery">
    <div class="gallery__main">
      <div class="gallery__strip" ref=${strip} onScroll=${onScroll}>
        ${list.map(
          (src, i) => html`<button type="button" class="gallery__slide" onClick=${() => setZoom(i)} aria-label=${`Открыть фото ${i + 1} на весь экран`}>
            <img src=${src} alt=${i === 0 ? title : ""} loading=${i < 2 ? "eager" : "lazy"} referrerpolicy="no-referrer" decoding="async" />
          </button>`,
        )}
      </div>
      ${stock && html`<span class="gallery__warn"><${Icon} name="triangle-alert" size=${14} />Похоже на фото из интернета</span>`}
      ${list.length > 1 &&
      html`<span class="gallery__count num">${index + 1} / ${list.length}</span>
        <button type="button" class="gallery__nav gallery__nav--prev" aria-label="Предыдущее фото" onClick=${() => go(index - 1)}>
          <${Icon} name="chevron-left" size=${20} />
        </button>
        <button type="button" class="gallery__nav gallery__nav--next" aria-label="Следующее фото" onClick=${() => go(index + 1)}>
          <${Icon} name="chevron-right" size=${20} />
        </button>`}
      <button type="button" class="gallery__zoom" aria-label="На весь экран" onClick=${() => setZoom(index)}><${Icon} name="maximize-2" size=${16} /></button>
    </div>
    ${list.length > 1 &&
    html`<div class="gallery__thumbs" role="tablist" aria-label="Фото">
      ${list.map(
        (src, i) => html`<button type="button" role="tab" aria-selected=${i === index} class=${cx("gallery__thumb", i === index && "is-on")} onClick=${() => go(i)}>
          <img src=${src} alt="" loading="lazy" referrerpolicy="no-referrer" />
        </button>`,
      )}
    </div>`}
    ${zoom != null && html`<${Lightbox} images=${list} start=${zoom} onClose=${(i) => (setZoom(null), i != null && go(i))} />`}
  </div>`;
}

function Lightbox({ images, start = 0, onClose }) {
  const [i, setI] = useState(start);
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef(null);
  const box = useRef(null);
  const n = images.length;
  const go = (k) => {
    setI((k + n) % n);
    setScale(1);
    setPan({ x: 0, y: 0 });
  };
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        e.preventDefault();
        onClose(i);
      } else if (e.key === "ArrowRight") {
        e.stopPropagation();
        go(i + 1);
      } else if (e.key === "ArrowLeft") {
        e.stopPropagation();
        go(i - 1);
      }
    };
    window.addEventListener("keydown", onKey, true); // window capture runs before the drawer's handler
    const wasLocked = document.documentElement.classList.contains("scroll-locked");
    document.documentElement.classList.add("scroll-locked");
    const t = setTimeout(() => box.current && box.current.focus(), 20);
    return () => {
      clearTimeout(t);
      window.removeEventListener("keydown", onKey, true);
      if (!wasLocked) document.documentElement.classList.remove("scroll-locked");
    };
  }, [i]);
  const onWheel = (e) => {
    e.preventDefault();
    const next = Math.min(4, Math.max(1, scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    setScale(next);
    if (next === 1) setPan({ x: 0, y: 0 });
  };
  const onDown = (e) => {
    if (scale > 1) {
      drag.current = { x: e.clientX - pan.x, y: e.clientY - pan.y, moved: false };
      e.currentTarget.setPointerCapture && e.currentTarget.setPointerCapture(e.pointerId);
    } else drag.current = { sx: e.clientX, moved: false };
  };
  const onMove = (e) => {
    const d = drag.current;
    if (!d) return;
    if (scale > 1) {
      d.moved = true;
      setPan({ x: e.clientX - d.x, y: e.clientY - d.y });
    } else if (Math.abs(e.clientX - d.sx) > 10) d.moved = true;
  };
  const onUp = (e) => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (scale === 1 && d.moved && d.sx != null) {
      const dx = e.clientX - d.sx;
      if (Math.abs(dx) > 50) go(i + (dx < 0 ? 1 : -1));
      return;
    }
    if (!d.moved) {
      if (scale > 1) {
        setScale(1);
        setPan({ x: 0, y: 0 });
      } else setScale(2);
    }
  };
  return html`<${Portal}>
    <div class="lightbox" role="dialog" aria-modal="true" aria-label="Фото" tabindex="-1" ref=${box} onClick=${(e) => e.target === e.currentTarget && onClose(i)}>
      <div class="lightbox__bar">
        <span class="num">${i + 1} / ${n}</span>
        <span class="lightbox__hint">${scale > 1 ? "Перетащи, чтобы двигать · клик — уменьшить" : "Колесо или клик — увеличить"}</span>
        <button type="button" class="lightbox__btn" aria-label="Закрыть" onClick=${() => onClose(i)}><${Icon} name="x" size=${22} /></button>
      </div>
      <div class="lightbox__stage" onWheel=${onWheel} onPointerDown=${onDown} onPointerMove=${onMove} onPointerUp=${onUp} onPointerCancel=${() => (drag.current = null)}>
        <img
          src=${images[i]}
          alt=""
          referrerpolicy="no-referrer"
          draggable="false"
          style=${{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`, cursor: scale > 1 ? "grab" : "zoom-in" }}
        />
      </div>
      ${n > 1 &&
      html`<button type="button" class="lightbox__nav lightbox__nav--prev" aria-label="Предыдущее" onClick=${() => go(i - 1)}><${Icon} name="chevron-left" size=${28} /></button>
        <button type="button" class="lightbox__nav lightbox__nav--next" aria-label="Следующее" onClick=${() => go(i + 1)}><${Icon} name="chevron-right" size=${28} /></button>`}
    </div>
  <//>`;
}
