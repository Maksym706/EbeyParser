// Лента — the deals feed (brief §4.2): hero + filters + cards/rows, infinite scroll,
// live new deals (SSE), side drawer with the deal on desktop, keyboard j/k/s/h/c/o/Enter/?.
import { html, cx, useState, useEffect, useRef, useCallback, useMemo } from "../lib/html.js";
import { useAsync, useLocalState, useMediaQuery, useNow, useDebouncedCallback, BREAKPOINTS } from "../lib/hooks.js";
import { onEvent } from "../lib/events.js";
import { appStore, useStore } from "../lib/store.js";
import { setQuery, navigate } from "../lib/router.js";
import { plural } from "../lib/format.js";
import { Icon, Button, Drawer, EmptyState, ErrorState, Kbd, Modal, toast, Banner } from "../ui/index.js";
import { useTopbar } from "../shell/topbar.js";
import { api } from "../lib/api.js";
import "../features/icons-extra.js";
import { setBadge } from "../features/badges.js";
import { DealCard, DealRow, DealCardSkeleton, DealRowSkeleton } from "../features/deal-card.js";
import { DealView } from "../features/deal-view.js";
import { dealsApi, applyUpdate, toggleStar, hideDeal } from "../features/deal-actions.js";
import { markSeen, decide } from "../features/deal-model.js";
import { copyText, quickMessage, openAd } from "../features/messages.js";
import { FilterBar, fromQuery, toQuery, hasQuery, toParams, activeCount, DEFAULTS, LAST_KEY } from "./feed/filters.js";
import { Hero, SetupChecklist, DemoRibbon, HeroSkeleton } from "./feed/hero.js";

const PAGE = 30;
const smooth = () => (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
const KEEP_MS = 10 * 60 * 1000;
// the last feed (list + scroll) survives a trip to a deal page and back
let snapshot = null;
let lastScrollY = 0; // tracked continuously: navigate() scrolls to the top before the feed unmounts

// ------------------------------------------------------------------ data
function useFeed(params, key) {
  const cached = snapshot && snapshot.key === key && Date.now() - snapshot.at < KEEP_MS ? snapshot : null;
  const [state, setState] = useState(() =>
    cached ? { ...cached.state, loading: false, error: null, more: false } : { items: [], total: null, loading: true, error: null, more: false, done: false },
  );
  const restoredFrom = useRef(cached);
  const stateRef = useRef(state);
  stateRef.current = state;
  const [fresh, setFresh] = useState(() => new Set()); // ids that just arrived (glow)
  const [pending, setPending] = useState([]); // new deals waiting behind the «↑ N новых» pill
  const seq = useRef(0);
  const itemsRef = useRef([]);
  itemsRef.current = state.items;

  const load = useCallback(async () => {
    const id = ++seq.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    setPending([]);
    try {
      const res = await dealsApi.list({ ...params, limit: PAGE, offset: 0, facets: 1 });
      if (id !== seq.current) return;
      setState({ items: res.items, total: res.total, loading: false, error: null, more: false, done: res.items.length < PAGE, facets: res.facets || null });
    } catch (error) {
      if (id !== seq.current) return;
      setState((s) => ({ ...s, loading: false, error }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const loadMore = useCallback(async () => {
    const s0 = itemsRef.current;
    const id = seq.current;
    setState((s) => (s.more || s.done ? s : { ...s, more: true }));
    try {
      const res = await dealsApi.list({ ...params, limit: PAGE, offset: s0.length });
      if (id !== seq.current) return;
      setState((s) => {
        const have = new Set(s.items.map((d) => d.id));
        const add = res.items.filter((d) => !have.has(d.id));
        return { ...s, items: [...s.items, ...add], total: res.total ?? s.total, more: false, done: res.items.length < PAGE };
      });
    } catch (e) {
      if (id !== seq.current) return;
      setState((s) => ({ ...s, more: false }));
      toast.error(`Не получилось загрузить ещё: ${e.message}`, { action: { label: "Повторить", onClick: () => loadMore() } });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  /** Live refresh: new ids go on top (glow) when the user is at the top, else behind the pill. */
  const refresh = useCallback(
    async (atTop) => {
      const id = seq.current;
      try {
        const res = await dealsApi.list({ ...params, limit: PAGE, offset: 0 });
        if (id !== seq.current) return;
        const have = new Set(itemsRef.current.map((d) => d.id));
        const incoming = res.items.filter((d) => !have.has(d.id));
        if (!incoming.length) {
          // still update the known cards (statuses, prices)
          const byId = new Map(res.items.map((d) => [d.id, d]));
          setState((s) => ({ ...s, total: res.total ?? s.total, items: s.items.map((d) => (byId.has(d.id) ? { ...d, ...byId.get(d.id) } : d)) }));
          return;
        }
        if (atTop) {
          setState((s) => ({ ...s, total: res.total ?? s.total, items: [...incoming, ...s.items] }));
          glow(incoming.map((d) => d.id));
        } else {
          setPending((p) => {
            const ids = new Set(p.map((d) => d.id));
            return [...incoming.filter((d) => !ids.has(d.id)), ...p];
          });
        }
      } catch {
        /* the next event / poll retries */
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  );

  const glow = (ids) => {
    setFresh(new Set(ids));
    setTimeout(() => setFresh(new Set()), 1400);
  };

  const showPending = () => {
    setState((s) => {
      const have = new Set(s.items.map((d) => d.id));
      return { ...s, items: [...pending.filter((d) => !have.has(d.id)), ...s.items] };
    });
    glow(pending.map((d) => d.id));
    setPending([]);
  };

  const patch = useCallback((data) => setState((s) => ({ ...s, items: s.items.map((d) => applyUpdate(d, data)) })), []);

  useEffect(() => {
    if (restoredFrom.current && restoredFrom.current.key === key) {
      // came back from a deal: show the same list at the same place, then check for news quietly
      const y = restoredFrom.current.scrollY;
      restoredFrom.current = null;
      requestAnimationFrame(() => window.scrollTo(0, y));
      refresh(y < 160);
      return;
    }
    restoredFrom.current = null;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  useEffect(() => {
    const onScroll = () => {
      lastScrollY = window.scrollY;
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      snapshot = { key, state: stateRef.current, scrollY: lastScrollY, at: Date.now() };
    };
  }, [key]);

  return { ...state, fresh, pending, load, loadMore, refresh, showPending, patch };
}

/** Today's numbers for the hero: GET /summary/today. */
function useToday(tick) {
  return useAsync(() => api.get("/summary/today"), [tick]);
}

// ------------------------------------------------------------------ screen
export default function FeedScreen({ query = {} }) {
  const isPhone = useMediaQuery(BREAKPOINTS.phone);
  const wide = useMediaQuery("(min-width: 1280px)");
  const now = useNow(60000);
  const app = useStore(appStore, (s) => s.app) || {};
  const [view, setView] = useLocalState("ebp.feed.view", "grid");
  const [sel, setSel] = useState(-1);
  const [help, setHelp] = useState(false);
  const [todayTick, setTodayTick] = useState(0);
  const listRef = useRef(null);

  // badge + title: the feed is being looked at
  useEffect(() => appStore.set({ newDeals: 0 }), []);

  // restore the last used filters when the address has none
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    if (hasQuery(query)) return;
    try {
      const last = JSON.parse(localStorage.getItem(LAST_KEY) || "null");
      if (last && activeCount({ ...DEFAULTS, ...last }) + (last.sort && last.sort !== "best" ? 1 : 0) > 0)
        setQuery(toQuery({ ...DEFAULTS, ...last }), { replace: true });
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const f = useMemo(() => fromQuery(query), [JSON.stringify(query)]);
  const q = query.q || "";
  const set = (patch) => {
    const next = { ...f, ...patch };
    try {
      localStorage.setItem(LAST_KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
    setQuery(toQuery(next));
  };
  const reset = () => {
    try {
      localStorage.removeItem(LAST_KEY);
    } catch {
      /* ignore */
    }
    setQuery({ ...toQuery(DEFAULTS), q: null });
  };

  // desktop search field in the top bar
  const [qText, setQText] = useState(q);
  useEffect(() => setQText(q), [q]);
  const pushQ = useDebouncedCallback((v) => setQuery({ q: v || null }), 300);
  const search = {
    value: qText,
    onChange: (v) => {
      setQText(v);
      pushQ(v);
    },
    placeholder: "Найти в ленте: 3090, iPhone…",
  };
  useTopbar({ search }, [qText]);

  const params = useMemo(() => toParams(f, q), [f, q]);
  const key = JSON.stringify(params);
  const feed = useFeed(params, key);
  const today = useToday(todayTick);
  useEffect(() => {
    if (today.data && today.data.followups != null) setBadge("deals", today.data.followups);
  }, [today.data]);

  // items actually shown (hidden ones stay in memory for «Отменить»)
  const showHidden = f.st === "ignored";
  const items = feed.items.filter((d) => (showHidden ? d.status === "ignored" : d.status !== "ignored"));

  // ---------------------------------------------------------------- live updates
  const unseenLive = useRef(0);
  useEffect(() => {
    const offs = [
      onEvent("deal_found", () => {
        if (!document.hidden) setTimeout(() => appStore.set({ newDeals: 0 }), 0); // the user is looking at the feed
        feed.refresh(window.scrollY < 160);
        setTodayTick((n) => n + 1);
        if (document.hidden) {
          unseenLive.current += 1;
          document.title = `(${unseenLive.current}) Лента · EbeyParser`;
        }
      }),
      onEvent("deal_updated", (data) => {
        if (data && (data.local || data.card || data.patch)) feed.patch(data);
      }),
      onEvent("run_finished", () => {
        feed.refresh(window.scrollY < 160);
        setTodayTick((n) => n + 1);
      }),
      onEvent("connected", () => feed.refresh(window.scrollY < 160)),
      onEvent("data_changed", () => {
        feed.load();
        setTodayTick((n) => n + 1);
      }),
    ];
    const onFocus = () => {
      unseenLive.current = 0;
      document.title = "Лента · EbeyParser";
      appStore.set({ newDeals: 0 });
    };
    const onReload = () => {
      feed.load();
      setTodayTick((n) => n + 1);
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("ebp:feed-reload", onReload);
    return () => {
      offs.forEach((off) => off());
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("ebp:feed-reload", onReload);
    };
  }, [feed.refresh, feed.load]);

  // fallback polling when the live connection is down
  const live = useStore(appStore, (s) => s.live);
  useEffect(() => {
    if (live === "open") return undefined;
    const t = setInterval(() => !document.hidden && feed.refresh(window.scrollY < 160), 30000);
    return () => clearInterval(t);
  }, [live, feed.refresh]);

  // ---------------------------------------------------------------- infinite scroll
  const sentinel = useRef(null);
  useEffect(() => {
    if (!sentinel.current || !("IntersectionObserver" in window)) return undefined;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) feed.loadMore();
    }, { rootMargin: "600px 0px" });
    io.observe(sentinel.current);
    return () => io.disconnect();
  }, [feed.loadMore, feed.done, feed.loading, items.length]);

  // ---------------------------------------------------------------- drawer
  const openId = query.deal || null;
  const lastOpen = useRef(null);
  if (openId) lastOpen.current = openId;
  const openIndex = openId ? items.findIndex((d) => String(d.id) === String(openId)) : -1;
  const openCard = openIndex >= 0 ? items[openIndex] : null;
  const openDeal = (deal, e) => {
    markSeen(deal.id);
    if (isPhone) return; // the link navigates to /deal/:id
    if (e) e.preventDefault();
    setSel(items.findIndex((d) => d.id === deal.id));
    setQuery({ deal: deal.id }, { replace: Boolean(openId) });
  };
  const closeDeal = () => setQuery({ deal: null }, { replace: false });
  const step = (dir) => {
    const base = openIndex >= 0 ? openIndex : sel;
    const next = Math.min(items.length - 1, Math.max(0, base + dir));
    if (next === base || !items[next]) return;
    setSel(next);
    markSeen(items[next].id);
    if (openId) setQuery({ deal: items[next].id }, { replace: true });
    scrollToCard(items[next].id);
    if (dir > 0 && next >= items.length - 3 && !feed.done) feed.loadMore();
  };

  // hero «Лучшее сегодня» opens the drawer too
  const onClickCapture = (e) => {
    const a = e.target.closest && e.target.closest("[data-deal-open]");
    if (!a || isPhone || e.metaKey || e.ctrlKey) return;
    const id = a.getAttribute("data-deal-open");
    e.preventDefault();
    markSeen(id);
    setQuery({ deal: id }, { replace: false });
  };

  // ---------------------------------------------------------------- keyboard
  useEffect(() => {
    const onKey = (e) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = document.activeElement;
      if (t && (/^(input|textarea|select)$/i.test(t.tagName) || t.isContentEditable)) return;
      if (document.querySelector(".overlay .modal, .overlay .drawer:not(.deal-drawer), .lightbox, .pop__panel")) return;
      const cur = items[openIndex >= 0 ? openIndex : sel];
      switch (e.key) {
        case "j":
          step(1);
          break;
        case "k":
          step(-1);
          break;
        case "Enter":
          if (!cur || openId || (t && t.closest && t.closest("button, a"))) return;
          if (isPhone) navigate(`/deal/${encodeURIComponent(cur.id)}`);
          else openDeal(cur);
          break;
        case "s":
          if (cur) toggleStar(cur);
          break;
        case "h":
          if (cur) {
            hideDeal(cur);
            step(1);
          }
          break;
        case "c":
          if (cur) copyText(quickMessage(cur)).then((ok) => ok && toast.success("Сообщение скопировано — вставь его в чат продавцу"));
          break;
        case "o":
          if (cur && cur.url) openAd(cur.url);
          break;
        case "?":
          setHelp(true);
          break;
        default:
          return;
      }
      e.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  // ---------------------------------------------------------------- render
  const Item = isPhone || view === "rows" ? DealRow : DealCard;
  const Skel = isPhone || view === "rows" ? DealRowSkeleton : DealCardSkeleton;
  const firstLoad = feed.loading && !feed.items.length;
  const filtered = activeCount(f) > 0 || Boolean(q);

  let body;
  if (feed.error && !feed.items.length) {
    body = html`<div class="feed-state">
      <${ErrorState} error=${feed.error} title="Не удалось загрузить ленту" onRetry=${() => feed.load()} />
    </div>`;
  } else if (firstLoad) {
    body = html`<div class=${cx("feed", `feed--${isPhone ? "rows" : view}`)}>${Array.from({ length: 6 }, (_, i) => html`<${Skel} key=${i} />`)}</div>`;
  } else if (!items.length) {
    body = html`<div class="feed-state"><${FeedEmpty} filtered=${filtered} app=${app} summary=${today.data} reset=${reset} /></div>`;
  } else {
    body = html`<div class=${cx("feed", `feed--${isPhone ? "rows" : view}`, feed.loading && "is-refreshing")} ref=${listRef} role="feed" aria-busy=${feed.loading}>
      ${items.map(
        (d, i) => html`<${Item}
          key=${d.id}
          deal=${d}
          now=${now}
          onOpen=${openDeal}
          selected=${i === (openIndex >= 0 ? openIndex : sel)}
          glow=${feed.fresh.has(d.id)}
        />`,
      )}
    </div>
    <div class="feed-more" ref=${sentinel}>
      ${feed.more
        ? html`<${Skel} />`
        : !feed.done
          ? html`<${Button} variant="secondary" icon="chevron-down" onClick=${() => feed.loadMore()}>Показать ещё<//>`
          : items.length > 6 && html`<span class="muted small">Это всё${feed.total != null ? ` · ${feed.total} ${plural(feed.total, "находка", "находки", "находок")}` : ""}</span>`}
    </div>`;
  }

  return html`<div class=${cx("feed-screen", wide && openId && "feed-screen--panel")} onClickCapture=${onClickCapture}>
    <${DemoRibbon} />
    ${today.loading && !today.data ? html`<${HeroSkeleton} />` : html`<${Hero} summary=${today.data} loading=${today.loading} />`}
    <${SetupChecklist} summary=${today.data} />
    <div class=${cx("feed-toolbar", !filtered && !feed.loading && !feed.items.length && "is-hidden")}>
      <${FilterBar} f=${f} set=${set} view=${view} setView=${setView} total=${feed.total} reset=${reset} facets=${feed.facets} search=${search} />
      ${(filtered || feed.total != null) &&
      html`<div class="feed-count">
        ${feed.total != null && html`<span class="num">${feed.total} ${plural(feed.total, "находка", "находки", "находок")}</span>`}
        ${q && html`<span class="tag">«${q}» <button type="button" aria-label="Убрать поиск" onClick=${() => setQuery({ q: null })}><${Icon} name="x" size=${12} /></button></span>`}
        ${f.search &&
        html`<span class="tag"><${Icon} name="radar" size=${12} />${f.search} <button type="button" aria-label="Показать все поиски" onClick=${() => set({ search: "" })}><${Icon} name="x" size=${12} /></button></span>`}
        ${f.st === "ignored" &&
        html`<span class="tag"><${Icon} name="eye-off" size=${12} />Скрытые <button type="button" aria-label="Показать обычную ленту" onClick=${() => set({ st: "" })}><${Icon} name="x" size=${12} /></button></span>`}
        ${filtered && html`<button type="button" class="linkish" onClick=${reset}><${Icon} name="filter-x" size=${14} />Сбросить фильтры</button>`}
        ${!isPhone && html`<button type="button" class="linkish kbd-hint" onClick=${() => setHelp(true)}><${Kbd}>?</${Kbd}> клавиши</button>`}
      </div>`}
      ${feed.pending.length > 0 &&
      html`<button
        type="button"
        class="newpill"
        onClick=${() => {
          window.scrollTo({ top: 0, behavior: smooth() });
          feed.showPending();
        }}
      >
        <${Icon} name="arrow-up" size=${14} />${feed.pending.length} ${plural(feed.pending.length, "новая", "новых", "новых")}
      </button>`}
    </div>
    ${feed.error && feed.items.length > 0 && html`<${Banner} tone="neutral" icon="wifi-off">Нет связи с программой — показываю то, что было раньше. Переподключусь сам.<//>`}
    ${body}
    ${!isPhone &&
    html`<${Drawer}
      open=${Boolean(openId)}
      onClose=${closeDeal}
      modal=${!wide}
      width=${wide ? 600 : 560}
      class="deal-drawer"
      restoreFocus=${() => lastOpen.current && document.querySelector(`[data-deal="${CSS.escape(String(lastOpen.current))}"] .dcard__link`)}
      title=${openCard ? openCard.title : "Сделка"}
      header=${html`<${DrawerHead} card=${openCard} index=${openIndex} total=${items.length} onPrev=${openIndex > 0 ? () => step(-1) : null} onNext=${openIndex >= 0 && openIndex < items.length - 1 ? () => step(1) : null} id=${openId} />`}
    >
      ${openId && html`<${DealView} key=${openId} id=${openId} initial=${openCard} mode="drawer" />`}
    <//>`}
    <${ShortcutSheet} open=${help} onClose=${() => setHelp(false)} />
  </div>`;

  function scrollToCard(id) {
    const el = document.querySelector(`[data-deal="${CSS.escape(String(id))}"]`);
    if (el) el.scrollIntoView({ block: "nearest", behavior: smooth() });
  }
}

function DrawerHead({ card, index, total, onPrev, onNext, id }) {
  const dec = card ? decide(card) : null;
  return html`<div class="dhead">
    <div class="dhead__nav">
      <button type="button" class="icon-btn icon-btn--ghost icon-btn--sm" aria-label="Предыдущая (k)" title="Предыдущая (k)" disabled=${!onPrev} onClick=${onPrev}>
        <${Icon} name="chevron-up" size=${18} />
      </button>
      <button type="button" class="icon-btn icon-btn--ghost icon-btn--sm" aria-label="Следующая (j)" title="Следующая (j)" disabled=${!onNext} onClick=${onNext}>
        <${Icon} name="chevron-down" size=${18} />
      </button>
      ${index >= 0 && html`<span class="dhead__pos num">${index + 1} / ${total}</span>`}
    </div>
    ${dec && html`<span class=${cx("dhead__verb", `tone-${dec.tone}`)}><${Icon} name=${dec.icon} size=${14} />${dec.verb}</span>`}
    <span class="grow"></span>
    <a class="icon-btn icon-btn--ghost icon-btn--sm" href=${`/deal/${encodeURIComponent(id || "")}`} aria-label="Открыть на всю страницу" title="Открыть на всю страницу">
      <${Icon} name="maximize-2" size=${16} />
    </a>
    ${card && card.url && html`<${Button} size="sm" variant="secondary" iconRight="arrow-up-right" href=${card.url}>Открыть объявление<//>`}
  </div>`;
}

function FeedEmpty({ filtered, app, summary, reset }) {
  const counts = app.counts || {};
  const first = (summary && summary.learning) || app.first_run || {};
  const demo = Boolean(app.demo && (app.demo.loaded || app.demo === true));
  // filters first: an empty result of a chip is never "set up your searches" (P0-2)
  if (filtered) {
    return html`<${EmptyState}
      icon="sliders-horizontal"
      title="По этим фильтрам ничего не нашлось"
      message="Сбрось фильтры, чтобы снова увидеть все выгодные находки."
      action=${html`<${Button} variant="primary" icon="filter-x" onClick=${reset}>Сбросить фильтры<//>`}
    />`;
  }
  if (counts.searches === 0 && !demo) {
    return html`<${EmptyState}
      icon="radar"
      tone="brand"
      title="Здесь появятся выгодные находки"
      message="Сначала расскажи, что искать — это 3 минуты."
      action=${html`<${Button} variant="primary" icon="sparkles" href="/welcome">Настроить<//>`}
      secondary=${html`<${Button} variant="secondary" icon="flask-conical" onClick=${loadDemo}>Посмотреть демо<//>`}
    />`;
  }
  if (counts.searches === 0) {
    return html`<${EmptyState}
      icon="radar"
      tone="brand"
      title="Здесь пока пусто"
      message="Демо-находки скрыты. Настрой свои поиски — и здесь появятся настоящие объявления."
      action=${html`<${Button} variant="primary" icon="sparkles" href="/welcome">Настроить по-настоящему<//>`}
    />`;
  }
  if (first.learning) {
    // the hero above already shows the learning progress — keep this one short
    return html`<${EmptyState}
      compact
      icon="radar"
      tone="info"
      title="Здесь появятся находки"
      message="Как только изучу рынок, лучшие объявления будут сверху — с ценой, прибылью и готовым сообщением продавцу."
    />`;
  }
  return html`<${EmptyState}
    icon="coffee"
    title="Сегодня выгодного пока не было"
    message="Обычно 2–5 находок в день. Можно расширить радиус или добавить категории."
    action=${html`<${Button} variant="secondary" icon="radar" href="/searches">Изменить поиски<//>`}
  />`;
}

async function loadDemo() {
  const id = toast({ kind: "loading", title: "Загружаю демо-данные…" });
  try {
    await api.post("/demo/load", {});
    toast({ id, kind: "success", title: "Демо загружено — это примеры, не настоящие объявления" });
    const { loadApp } = await import("../lib/app.js");
    await loadApp().catch(() => {});
    window.dispatchEvent(new CustomEvent("ebp:feed-reload"));
  } catch (e) {
    toast({ id, kind: "error", title: e.message });
  }
}

const KEYS = [
  ["j", "следующая находка"],
  ["k", "предыдущая"],
  ["Enter", "открыть"],
  ["Esc", "закрыть"],
  ["s", "в избранное"],
  ["h", "скрыть"],
  ["c", "скопировать сообщение продавцу"],
  ["o", "открыть объявление на сайте"],
  ["/", "поиск"],
  ["?", "эта подсказка"],
];

function ShortcutSheet({ open, onClose }) {
  return html`<${Modal} open=${open} onClose=${onClose} title="Клавиши" icon="keyboard" size="sm">
    <dl class="keys">
      ${KEYS.map(([k, v]) => html`<div><dt><${Kbd}>${k}</${Kbd}></dt><dd>${v}</dd></div>`)}
    </dl>
  <//>`;
}

