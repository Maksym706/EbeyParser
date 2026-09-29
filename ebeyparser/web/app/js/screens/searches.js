// Поиски — searches manager (brief §4.5): load meter, grouped cards with toggle / stats /
// state badge / menu, «Новый поиск» chooser and the edit side panel.
// Sub-routes (params.rest): "new" → chooser, "new/<kind>" → new search, "<id>" → edit.
import { html, cx, useState, useEffect } from "../lib/html.js";
import { useAsync, useNow } from "../lib/hooks.js";
import { api, humanize } from "../lib/api.js";
import { onEvent } from "../lib/events.js";
import { navigate } from "../lib/router.js";
import { ago, number, everyLabel, localizeText } from "../lib/format.js";
import { Icon, Button, PageHeader, EmptyState, ErrorState, Skeleton, Toggle, Tooltip, Banner, Meter, Modal, Details, toast } from "../ui/index.js";
import { useTopbar } from "../shell/topbar.js";
import "../features/icons-extra.js";
import { Menu, Popover } from "../features/popover.js";
import { KindChooser, SearchEditor, deleteSearch } from "./searches/editor.js";

const KIND_ICON = { category: "layout-grid", keyword: "search", url: "link", wishlist: "heart", ebay: "gavel" };
const GROUPS = [
  { key: "category", title: "Категории в районе", kinds: ["category", "url"] },
  { key: "keyword", title: "По словам", kinds: ["keyword"] },
  { key: "wishlist", title: "Для себя", kinds: ["wishlist"] },
  { key: "ebay", title: "eBay", kinds: ["ebay"] },
];

function groupOf(s) {
  if (s.kind === "ebay") return "ebay";
  if (s.purpose === "personal" || s.kind === "wishlist") return "wishlist";
  if (s.kind === "keyword") return "keyword";
  return "category";
}

export default function SearchesScreen({ params = {} }) {
  const rest = (params.rest || "").split("/").filter(Boolean);
  const list = useAsync(() => api.get("/searches"), []);
  const cats = useAsync(() => api.get("/categories").then((r) => r.items || []).catch(() => []), []);
  const now = useNow(30000);
  const [local, setLocal] = useState({}); // id -> {enabled} optimistic

  useEffect(() => {
    const offs = [onEvent("searches_changed", () => list.reload(true)), onEvent("run_finished", () => list.reload(true)), onEvent("settings_changed", () => list.reload(true))];
    return () => offs.forEach((off) => off());
  }, []);
  useEffect(() => setLocal({}), [list.data]);

  const data = list.data || {};
  const items = (data.items || []).map((s) => (local[s.id] ? { ...s, ...local[s.id] } : s));
  const editable = data.editable !== false;
  const catIcon = Object.fromEntries((cats.data || []).map((c) => [c.id, c.icon]));
  const base = items.find((s) => s.config && s.config.location) || {};

  // sub-route state
  const chooser = rest[0] === "new" && !rest[1];
  const newKind = rest[0] === "new" && rest[1] ? rest[1] : null;
  const editId = rest[0] && rest[0] !== "new" ? decodeURIComponent(rest[0]) : null;
  const editing = editId ? items.find((s) => s.id === editId) : null;
  const close = () => navigate("/searches", { scroll: false });

  useTopbar({ title: "Поиски" }, []);

  const toggle = async (s, enabled) => {
    setLocal((m) => ({ ...m, [s.id]: { enabled } }));
    try {
      await api.post(`/searches/${encodeURIComponent(s.id)}/toggle`, { enabled });
      toast({ kind: enabled ? "success" : "neutral", title: enabled ? `«${s.name}» включён` : `«${s.name}» выключен — не тратит запросы`, duration: 3500 });
      list.reload(true);
    } catch (e) {
      setLocal((m) => ({ ...m, [s.id]: { enabled: !enabled } }));
      toast.error(`Не получилось: ${e.message}`);
    }
  };
  const duplicate = async (s) => {
    try {
      const copy = await api.post(`/searches/${encodeURIComponent(s.id)}/duplicate`, {});
      toast.success("Копия создана", { action: copy && copy.id ? { label: "Изменить", href: `/searches/${encodeURIComponent(copy.id)}` } : null });
      list.reload(true);
    } catch (e) {
      toast.error(`Не получилось: ${e.message}`);
    }
  };
  const remove = async (s) => {
    if (await deleteSearch(s)) {
      if (editId) close();
      list.reload(true);
    }
  };

  let body;
  if (list.error && !list.data) body = html`<${ErrorState} error=${list.error} onRetry=${() => list.reload()} />`;
  else if (!list.data) body = html`<div class="scards">${[0, 1, 2, 3, 4, 5].map(() => html`<div class="scard scard--sk"><${Skeleton} w="60%" h=${16} /><${Skeleton} w="80%" h=${24} /><${Skeleton} w="70%" h=${12} /></div>`)}</div>`;
  else if (!items.length)
    body = html`<${EmptyState}
      icon="radar"
      tone="brand"
      title="Поисков нет"
      message="Расскажи, что и где искать — я буду проверять новые объявления каждые полчаса."
      action=${html`<${Button} variant="primary" icon="sparkles" href="/welcome">Настроить за 3 минуты<//>`}
      secondary=${html`<${Button} variant="secondary" icon="plus" onClick=${() => navigate("/searches/new")}>Добавить вручную<//>`}
    />`;
  else
    body = GROUPS.map((g) => {
      const group = items.filter((s) => groupOf(s) === g.key);
      if (!group.length) return null;
      return html`<section class="sgrp" key=${g.key}>
        <h2 class="sgrp__title">${g.title}<span class="num">${group.length}</span></h2>
        <div class="scards">
          ${group.map(
            (s) => html`<${SearchCard}
              key=${s.id}
              s=${s}
              now=${now}
              icon=${s.kind === "category" && s.config && catIcon[s.config.category_id] ? catIcon[s.config.category_id] : KIND_ICON[s.kind] || "search"}
              editable=${editable}
              onToggle=${(v) => toggle(s, v)}
              onDuplicate=${() => duplicate(s)}
              onDelete=${() => remove(s)}
            />`,
          )}
        </div>
      </section>`;
    });

  return html`<div class="searches-screen">
    <${PageHeader}
      title="Поиски"
      subtitle="Что и где я ищу. Выключенные поиски не тратят запросы."
      actions=${html`<${Button} variant="primary" icon="plus" onClick=${() => navigate("/searches/new", { scroll: false })} disabled=${!editable}>Новый поиск<//>`}
    />
    ${!editable && list.data && html`<${Banner} tone="haggle" title="Не могу сохранить настройки">Нет доступа к файлу настроек — поиски можно только посмотреть.<//>`}
    ${data.estimate && data.estimate.searches !== 0 && items.some((s) => s.enabled && s.source !== "ebay") && html`<${LoadMeter} est=${data.estimate} />`}
    ${body}
    <${KindChooser} open=${chooser} onClose=${close} ebayConfigured=${data.ebay_configured} onPick=${(k) => navigate(`/searches/new/${k}`, { replace: true, scroll: false })} />
    ${newKind === "ebay" &&
    data.ebay_configured === false &&
    html`<${EbayMissing} onClose=${close} />`}
    <${SearchEditor}
      open=${(Boolean(newKind) && !(newKind === "ebay" && data.ebay_configured === false)) || Boolean(editing)}
      search=${editing}
      kind=${newKind}
      base=${base.config || {}}
      categories=${cats.data || []}
      readonly=${!editable}
      onClose=${close}
      onDelete=${remove}
      onSaved=${() => {
        close();
        list.reload(true);
      }}
    />
  </div>`;
}

/** «Нагрузка на Kleinanzeigen»: a meter and one plain sentence; the maths stays in «Как это работает?». */
function LoadMeter({ est }) {
  const tone = { ok: "green", warn: "amber", danger: "red" }[est.level] || "green";
  const every = everyLabel(est.interval_minutes);
  const sentence = est.short_ru
    ? `${est.short_ru}${/^Безопасно/.test(est.short_ru) ? ` · проверяю ${every}` : ""}`
    : tone === "red"
      ? "Слишком много поисков — есть риск блокировки. Выключи ненужные."
      : tone === "amber"
        ? `Нагрузка высокая — проверяю ${every}, чтобы не заблокировали.`
        : `Проверяю ${every} — безопасно, блокировки маловероятны.`;
  return html`<div class="loadcard">
    <${Meter}
      label="Нагрузка на Kleinanzeigen"
      value=${est.pages_per_hour || 0}
      max=${est.cap_per_hour || 150}
      marker=${0.4}
      tone=${tone}
      valueText=${est.pages_label_ru || `~${number(est.pages_per_hour || 0)} из ${number(est.cap_per_hour || 150)} страниц в час`}
      hint=${sentence}
    />
    <${Popover}
      label="Как это работает"
      trigger=${(p) => html`<button type="button" class="linkish" ...${p}><${Icon} name="circle-help" size=${14} />Как это работает?</button>`}
    >
      <div class="explain">
        <b>Почему не чаще</b>
        <p>Kleinanzeigen блокирует тех, кто открывает слишком много страниц. Я держу нагрузку ниже безопасной отметки (чёрточка на шкале) и сам делаю паузы.</p>
        <p>Больше категорий — реже проверка каждой. Выключи ненужные поиски, чтобы проверять остальные чаще.</p>
        ${est.suggested_interval && est.suggested_interval > est.interval_minutes && html`<p class="muted">Рекомендую проверять не чаще, чем ${everyLabel(est.suggested_interval)}.</p>`}
        ${est.text_ru && html`<${Details} text=${est.text_ru} label="Подробный расчёт" />`}
      </div>
    <//>
  </div>`;
}

/** /searches/new/ebay without eBay keys: say what to do instead of opening an editor that can't work. */
function EbayMissing({ onClose }) {
  return html`<${Modal} open=${true} onClose=${onClose} title="Сначала подключи eBay" icon="gavel" size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Не сейчас<//><${Button} variant="primary" icon="key-round" href="/settings/ebay">Подключить eBay<//>`}>
    <p class="muted-line">Поиск по eBay работает через официальный бесплатный доступ. Добавь ключи в настройках — это 5 минут, и сюда можно будет вернуться.</p>
  <//>`;
}

// postal code → "Neukölln, Berlin" for the cards (cached for the session)
const placeNames = new Map();
function usePlaceName(value) {
  const plz = /^\d{5}$/.test(String(value || "").trim()) ? String(value).trim() : null;
  const [, force] = useState(0);
  useEffect(() => {
    if (!plz || placeNames.has(plz)) return;
    placeNames.set(plz, null);
    api
      .get("/locations", { params: { q: plz, limit: 3 } })
      .then((r) => {
        const hit = ((r && r.items) || []).find((p) => p.plz === plz && p.kind !== "plz") || null;
        placeNames.set(plz, hit ? hit.label || hit.name : null);
        force((n) => n + 1);
      })
      .catch(() => {});
  }, [plz]);
  return plz ? placeNames.get(plz) || null : null;
}

function stateOf(s) {
  const st = s.stats || {};
  if (st.errors && st.errors.length) return { tone: "danger", icon: "circle-alert", label: "Ошибка", tip: humanize(localizeText(st.errors[0])).message };
  if (!s.enabled) return { tone: "neutral", icon: "power-off", label: "Выключен", tip: "Не проверяется и не тратит запросы" };
  if (st.learning === "pending") return { tone: "info", icon: "hourglass", label: "Обучение", tip: "Первая проверка только изучает цены" };
  if (st.learning === "learned") return { tone: "info", icon: "hourglass", label: "Цены изучены", tip: st.learning_ru };
  if (!st.last_run_at) return { tone: "neutral", icon: "circle-dashed", label: "Не проверен", tip: "Ещё не было проверки" };
  return { tone: "profit", icon: "circle-check", label: "Активен", tip: "" };
}

function SearchCard({ s, now, icon, editable, onToggle, onDuplicate, onDelete }) {
  const st = s.stats || {};
  const state = stateOf(s);
  const cfg = s.config || {};
  const place = cfg.location_label || usePlaceName(cfg.location) || cfg.location;
  const chips = [];
  if (s.source === "ebay") chips.push(cfg.local_pickup_only ? `Самовывоз ${place || ""} · ${cfg.radius_km || 0} км` : "вся Германия");
  else if (place) chips.push(`${place}${cfg.radius_km ? ` · ${cfg.radius_km} км` : " · только город"}`);
  if (s.purpose === "personal") {
    // «Для себя до 600 € · до 500 €» read as nonsense: one clear chip per number
    chips.push(cfg.target_price ? `Хочу за ${number(cfg.target_price)} €` : "Для себя");
    if (cfg.max_price != null && cfg.max_price !== cfg.target_price) chips.push(`показывать до ${number(cfg.max_price)} €`);
  } else if (cfg.min_price != null || cfg.max_price != null)
    chips.push(cfg.min_price != null && cfg.max_price != null ? `${number(cfg.min_price)}–${number(cfg.max_price)} €` : cfg.max_price != null ? `до ${number(cfg.max_price)} €` : `от ${number(cfg.min_price)} €`);
  if (cfg.query && s.kind !== "wishlist") chips.push(`«${cfg.query}»`);
  const href = `/searches/${encodeURIComponent(s.id)}`;
  return html`<article class=${cx("scard", !s.enabled && "is-off")}>
    <div class="scard__top">
      <span class="scard__icon"><${Icon} name=${icon} size=${20} /></span>
      <div class="scard__head">
        <a class="scard__name" href=${href}>${s.name}</a>
        <${Tooltip} text=${state.tip}>
          <span class=${cx("sbadge", `tone-${state.tone}`)}><${Icon} name=${state.icon} size=${12} />${state.label}</span>
        <//>
      </div>
      <${Toggle} checked=${s.enabled} onChange=${onToggle} disabled=${!editable} />
    </div>
    <div class="scard__chips">
      ${chips.map((c) => html`<span class="tag">${c}</span>`)}
      <span class=${cx("tag", s.source === "ebay" && "tone-bid")}>${s.source_label || (s.source === "ebay" ? "eBay" : "Kleinanzeigen")}</span>
    </div>
    ${st.errors && st.errors.length > 0 && html`<p class="scard__err"><${Icon} name="circle-alert" size=${14} />${humanize(localizeText(st.errors[0])).message}</p>`}
    <div class="scard__stats">
      <div><span>Новых за 24 ч</span><b class="num">${number(st.ads_24h || 0)}</b></div>
      <div><span>Выгодных</span><b class=${cx("num", st.deals > 0 && "t-green")}>${number(st.deals || 0)}</b></div>
      <div><span>Проверка</span><b>${st.last_run_at ? ago(st.last_run_at, now) : "ещё не было"}</b></div>
    </div>
    <div class="scard__foot">
      <a class="linkish" href=${`/?search=${encodeURIComponent(s.name)}`}><${Icon} name="zap" size=${14} />Находки${st.deals + (st.maybe || 0) > 0 ? ` (${st.deals + (st.maybe || 0)})` : ""}</a>
      <span class="grow"></span>
      <a class="btn btn--ghost btn--sm" href=${href}><${Icon} name="pencil" size=${16} /><span class="btn__label">Изменить</span></a>
      <${Menu}
        label="Ещё"
        items=${[
          { label: "Изменить", icon: "pencil", onClick: () => navigate(href, { scroll: false }), disabled: !editable },
          { label: "Дублировать", icon: "copy-plus", onClick: onDuplicate, disabled: !editable },
          { label: "Показать находки", icon: "zap", onClick: () => navigate(`/?search=${encodeURIComponent(s.name)}`) },
          { divider: true },
          { label: "Удалить", icon: "trash-2", danger: true, onClick: onDelete, disabled: !editable },
        ]}
      />
    </div>
  </article>`;
}

