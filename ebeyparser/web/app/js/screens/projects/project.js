// One build — /projects/:id (spec §5 plan view while `draft`, §6 tracking view otherwise) and the
// phone-only part page /projects/:id/slot/:slot. Renders instantly from the view cache, refetches,
// and follows `project_updated` (1 s debounce).
import { html, cx, useState, useEffect, useRef } from "../../lib/html.js";
import { useIsMobile, useNow } from "../../lib/hooks.js";
import { navigate, setQuery } from "../../lib/router.js";
import { onEvent } from "../../lib/events.js";
import { appStore, useStore } from "../../lib/store.js";
import { count } from "../../lib/format.js";
import { Icon, Button, EmptyState, ErrorState, Skeleton, Banner, Drawer, Modal, Field, NumberInput, toast } from "../../ui/index.js";
import { useTopbar } from "../../shell/topbar.js";
import { Menu } from "../../features/popover.js";
import { DealView } from "../../features/deal-view.js";
import { projectsStore, projectsApi, cacheView, forgetView, changeChoice, ru, money, lower, handoff, StatusChip } from "../../features/projects-common.js";
import { PlanBody, ChecksPanel, InactiveSlots, SlotCard, AddItemDialog, flashSlot } from "../../features/projects-plan.js";
import { TotalsBar, TrackSlot, BoughtDialog, TrackSheet, AlertsList, GpuRankingSheet, AltSheet, DeleteDialog, deleteProjectSearches } from "../../features/projects-track.js";

// ------------------------------------------------------------------ data
function useProject(id) {
  const view = useStore(projectsStore, (s) => s.views[id]);
  const [error, setError] = useState(null);
  const seq = useRef(0);
  const load = () => {
    const n = ++seq.current;
    return projectsApi.get(id).then(
      (v) => {
        if (n !== seq.current) return;
        cacheView(v);
        setError(null);
      },
      (e) => n === seq.current && setError(e),
    );
  };
  return { p: view, error, load, setView: (v) => cacheView(v) };
}

/** Local handlers shared by the page and the part page. */
function makeActions(p, setView, { setBusy, setReadOnly }) {
  const wrap = async (key, fn) => {
    setBusy(key);
    try {
      return await fn();
    } catch (e) {
      if (e.status === 403 && e.code === "read_only") setReadOnly(e);
      else toast.error(e);
      return false;
    } finally {
      setBusy(null);
    }
  };
  const patch = (body) => projectsApi.patch(p.id, body).then((v) => (setView(v), v));
  return {
    onPick: (slotKey, optionKey) =>
      wrap(`${slotKey}:${optionKey}`, async () => {
        const v = await changeChoice(p, slotKey, optionKey, { onView: setView });
        if (v) flashSlot(slotKey);
        return Boolean(v);
      }),
    onFix: (fix) =>
      wrap(`${fix.slot}:${fix.option}`, async () => {
        const v = await changeChoice(p, fix.slot, fix.option, { onView: setView, title: `Исправил: ${lower(ru(fix.label_ru))}` });
        if (v) flashSlot(fix.slot);
        return Boolean(v);
      }),
    onSlot: (key, body, { undoOf } = {}) =>
      wrap(`slot:${key}`, async () => {
        await patch({ slots: { [key]: body } });
        const slot = (p.slots || []).find((s) => s.key === key) || { label: "Часть" };
        if (body.status) {
          const text = { have: "уже есть", skipped: "не нужно", open: "снова ищу" }[body.status];
          toast.success(`${slot.label}: ${text}`, {
            message: body.status === "open" ? "" : "Больше не ищу её и не считаю в сумме",
            action: undoOf ? { label: "Отменить", onClick: () => patch({ slots: { [key]: { status: undoOf } } }).catch((e) => toast.error(e)) } : null,
          });
        } else if (body.target_price) toast.success(`${slot.label}: своя цель ${money(body.target_price)}`, { message: "Проверяю объявления до 30 % дороже цели — с продавцом можно поторговаться" });
        else if (body.auto_target) toast.success(`${slot.label}: цель снова считаю сам`);
        else if (body.note !== undefined) toast.success(body.note ? "Заметка сохранена" : "Заметка убрана");
        return true;
      }),
    onRemove: (key) =>
      wrap(`slot:${key}`, async () => {
        const slot = (p.slots || []).find((s) => s.key === key);
        await patch({ remove_slots: [key] });
        const opt = slot && (slot.options || []).find((o) => o.chosen);
        toast.success(`Убрал: ${slot ? slot.label : "вещь"}`, {
          action: opt
            ? { label: "Вернуть", onClick: () => patch({ add_items: [{ label: slot.label, query: opt.query || slot.label, target_price: opt.target_by === "user" ? opt.target_unit : null, qty: slot.qty || 1 }] }).catch((e) => toast.error(e)) }
            : null,
        });
        return true;
      }),
    onAddItem: (item) =>
      wrap("add", async () => {
        await patch({ add_items: [item] });
        toast.success(`Добавил: ${item.label}`);
        return true;
      }),
    onBudget: (budget) =>
      wrap("budget", async () => {
        await patch(budget == null ? { clear_budget: true } : { budget });
        toast.success(budget == null ? "Бюджет убран" : `Бюджет: ${money(budget)}`, { message: "Цели частей пересчитаны" });
        return true;
      }),
    onRename: (name) =>
      wrap("name", async () => {
        await patch({ name });
        return true;
      }),
    onStatus: (status) =>
      wrap("status", async () => {
        await patch({ status });
        if (status === "paused") toast({ kind: "neutral", icon: "pause", title: "Сборка на паузе — поиски не работают", action: { label: "Отменить", onClick: () => patch({ status: "tracking" }).catch((e) => toast.error(e)) } });
        else toast.success("Снова слежу за сборкой");
        return true;
      }),
  };
}

// ------------------------------------------------------------------ page
export default function ProjectPage({ id, slotKey, query = {} }) {
  const phone = useIsMobile();
  const now = useNow(60000);
  const { p, error, load, setView } = useProject(id);
  const app = useStore(appStore, (s) => s.app);
  const monitor = useStore(appStore, (s) => s.monitor);
  const [busy, setBusy] = useState(null);
  const [readOnly, setReadOnly] = useState(null);
  const [sheet, setSheet] = useState(null); // track | delete | ranking | budget | add
  const [bought, setBought] = useState(null); // {slot, offer}
  const [alt, setAlt] = useState(null); // {slot, alt}
  const [deal, setDeal] = useState(null); // {ad_id, title}
  const [editName, setEditName] = useState(false);
  const deleting = useRef(false);

  useEffect(() => {
    load();
  }, [id]);
  // live: refetch on the build's own events; a deletion elsewhere closes the page. The server
  // sends no project_updated for a build being deleted; `deleting` still covers our own DELETE,
  // whose project_deleted may arrive before its answer.
  useEffect(() => {
    let t = null;
    const later = () => {
      clearTimeout(t);
      t = setTimeout(() => !deleting.current && load(), 1000);
    };
    const offs = [
      onEvent("project_updated", (d) => {
        if (!d || String(d.id) !== String(id) || deleting.current) return;
        if (["offer", "run", "alert", "bought", "updated", "tracking"].includes(d.reason)) later();
      }),
      onEvent("project_deleted", (d) => {
        if (!d || String(d.id) !== String(id) || deleting.current) return;
        clearTimeout(t);
        forgetView(id);
        toast.info("Сборку удалили");
        navigate("/projects", { replace: true });
      }),
      onEvent("connected", later),
    ];
    return () => {
      offs.forEach((off) => off());
      clearTimeout(t);
    };
  }, [id]);
  // hand-off from the creation screen / the list («Начать отслеживание»)
  useEffect(() => {
    if (!p) return;
    if (handoff.flash) {
      flashSlot(handoff.flash);
      handoff.flash = null;
    }
    if (handoff.track || query.track === "1") {
      handoff.track = false;
      if (query.track) setQuery({ track: null });
      if (p.status === "draft" || p.status === "paused") setSheet("track");
    }
  }, [Boolean(p)]);
  // «Посмотреть» from the list: /projects/1#slot-gpu
  useEffect(() => {
    if (p && window.location.hash.startsWith("#slot-")) flashSlot(window.location.hash.slice(6));
  }, [Boolean(p)]);

  useTopbar({ title: p ? p.name : "Сборка" }, [p && p.name]);

  if (!p && error) {
    if (error.status === 404 && !error.missing)
      return html`<div class="pj-screen"><${EmptyState}
        icon="boxes"
        title="Сборка не найдена — возможно, её удалили"
        action=${html`<${Button} variant="primary" icon="arrow-left" href="/projects">К сборкам<//>`}
      /></div>`;
    return html`<div class="pj-screen"><${ErrorState} error=${error} title="Не получилось открыть сборку" onRetry=${load} /></div>`;
  }
  if (!p) return html`<${PageSkeleton} />`;

  const actions = makeActions(p, setView, { setBusy, setReadOnly });
  const draft = p.status === "draft";
  const tracking = p.status === "tracking";
  const paused = p.status === "paused";
  const done = p.status === "done";
  const gpuSlot = (p.slots || []).find((s) => s.kind === "gpu" && s.active);
  const writable = !app || app.config_writable !== false;
  const interval = monitor && monitor.intervalMinutes ? Math.round(monitor.intervalMinutes) : 30;
  const since = p.tracking_since ? new Date(p.tracking_since).getTime() : 0;
  const justStarted = tracking && since && now - since < interval * 2 * 60000;
  const openDeal = (o) => {
    if (!o || !o.ad_id) return;
    if (phone) navigate(`/deal/${encodeURIComponent(o.ad_id)}`);
    else setDeal({ ad_id: o.ad_id, title: o.title });
  };
  const handlers = {
    ...actions,
    onBought: (slot, offer) => setBought({ slot, offer }),
    onUndo: (slot) =>
      (async () => {
        setBusy(`undo:${slot.key}`);
        try {
          const res = await projectsApi.unbought(p.id, slot.key);
          if (res && res.project) setView(res.project);
          toast.info(ru(res && res.message_ru) || "Покупка отменена");
        } catch (e) {
          toast.error(e);
        } finally {
          setBusy(null);
        }
      })(),
    onDeal: openDeal,
    onAlt: (slot, a) => setAlt({ slot, alt: a }),
  };
  const menu = [
    { label: "Переименовать", icon: "pencil", onClick: () => setEditName(true) },
    { label: "Изменить бюджет", icon: "wallet", onClick: () => setSheet("budget") },
    !draft && { label: "Показать поиски", icon: "radar", href: "/searches" },
    !draft && gpuSlot && { label: "Лучшие видеокарты по ценности", icon: "trophy", onClick: () => setSheet("ranking") },
    { divider: true },
    { label: "Удалить сборку", icon: "trash-2", danger: true, onClick: () => setSheet("delete") },
  ];

  // ---------------------------------------------------------------- the part page (phone)
  if (slotKey) {
    const slot = (p.slots || []).find((s) => s.key === slotKey);
    return html`<div class="pj-screen pj-project pj-slot-page">
      <a class="page-header__back" href=${`/projects/${p.id}`}><${Icon} name="chevron-left" size=${18} />${p.name}</a>
      ${!slot
        ? html`<${EmptyState} icon="search-x" title="Такой части в сборке нет" action=${html`<${Button} href=${`/projects/${p.id}`}>К сборке<//>`} />`
        : draft
          ? html`<${SlotCard} slot=${slot} plan=${p} mode="draft" onPick=${actions.onPick} onSlot=${actions.onSlot} onRemove=${actions.onRemove} busy=${busy} />`
          : html`<${TrackSlot} p=${p} slot=${slot} handlers=${handlers} busy=${busy} justStarted=${justStarted} interval=${interval} full />`}
      ${renderOverlays()}
    </div>`;
  }

  const primary = draft
    ? html`<${Button} variant="primary" icon="radar" disabled=${!writable} onClick=${() => setSheet("track")}>Начать отслеживание<//>`
    : tracking
      ? html`<${Button} variant="secondary" icon="pause" loading=${busy === "status"} disabled=${!writable} onClick=${() => actions.onStatus("paused")}>Пауза<//>`
      : paused
        ? html`<${Button} variant="primary" icon="play" loading=${busy === "status"} disabled=${!writable} onClick=${() => actions.onStatus("tracking")}>Продолжить<//>`
        : null;

  return html`<div class=${cx("pj-screen pj-project", `is-${p.status}`, draft && phone && "has-dock")}>
    <a class="page-header__back" href="/projects"><${Icon} name="chevron-left" size=${18} />Сборки</a>
    <header class="pj-head">
      <span class="pj-head__icon" aria-hidden="true"><${Icon} name=${p.icon || "boxes"} size=${22} /></span>
      <div class="pj-head__titles">
        ${editName
          ? html`<${NameForm} value=${p.name} onCancel=${() => setEditName(false)} onSave=${async (name) => (await actions.onRename(name)) && setEditName(false)} />`
          : html`<h1 class="pj-head__title">${p.name}<button type="button" class="pj-inline-edit pj-head__edit" aria-label="Переименовать" onClick=${() => setEditName(true)}><${Icon} name="pencil" size=${16} /></button></h1>`}
        <p class="pj-head__sub">
          ${ru(p.template_label)} ·
          <button type="button" class="pj-inline-edit" onClick=${() => setSheet("budget")}>${p.budget ? `бюджет ${money(p.budget)}` : "без бюджета"}<${Icon} name="pencil" size=${13} /></button>
        </p>
      </div>
      <div class="pj-head__side">
        <${StatusChip} status=${p.status} label=${p.status_label} extra=${tracking && p.searches ? count(p.searches, "поиск", "поиска", "поисков") : null} />
        <div class="pj-head__actions">
          ${!(draft && phone) && primary}
          <${Menu} label="Ещё действия со сборкой" items=${menu} />
        </div>
      </div>
    </header>

    ${readOnly && html`<${Banner} tone="haggle" title="Не могу сохранить настройки" details=${readOnly.details} onClose=${() => setReadOnly(null)}>${readOnly.message}<//>`}
    ${!writable && !done && !readOnly && html`<${Banner} tone="haggle" title="Не могу сохранить настройки">Нет доступа к файлу настроек — сборку можно смотреть, но поиски для неё не запустятся.<//>`}
    ${paused &&
    html`<${Banner} tone="neutral" icon="pause" title="Сборка на паузе — поиски не работают" action=${html`<${Button} size="sm" variant="secondary" icon="play" loading=${busy === "status"} onClick=${() => actions.onStatus("tracking")}>Продолжить<//>`}>
      Предложения ниже — те, что я видел до паузы.
    <//>`}
    ${done && html`<${DoneCard} p=${p} reload=${load} />`}

    ${draft
      ? html`<${PlanBody}
          plan=${p}
          mode="draft"
          busy=${busy}
          handlers=${{ ...actions, onAddItem: () => setSheet("add") }}
        />`
      : html`<div class="pj-plan">
          <${TotalsBar} p=${p} />
          ${p.checks_summary && p.checks_summary.status !== "ok" && html`<${ChecksPanel} checks=${p.checks || []} summary=${p.checks_summary} onFix=${actions.onFix} busy=${busy} />`}
          <div class="pj-slots">
            ${sortedSlots(p).map((s) => html`<${TrackSlot} key=${s.key} p=${p} slot=${s} handlers=${handlers} busy=${busy} justStarted=${justStarted} interval=${interval} />`)}
          </div>
          ${(p.template === "custom" || p.kind === "custom") && html`<div class="pj-add"><${Button} variant="secondary" icon="plus" onClick=${() => setSheet("add")}>Добавить вещь<//></div>`}
          <${InactiveSlots} slots=${p.slots} />
          <${AlertsList} p=${p} onDeal=${openDeal} />
        </div>`}

    ${draft &&
    phone &&
    html`<div class="savebar pj-savebar pj-draftbar" role="region" aria-label="Начать отслеживание">
      <div class="savebar__inner">
        <span class="savebar__text">Черновик: поиски ещё не созданы</span>
        <div class="savebar__actions"><${Button} variant="primary" icon="radar" disabled=${!writable} onClick=${() => setSheet("track")}>Начать отслеживание<//></div>
      </div>
    </div>`}
    ${renderOverlays()}
  </div>`;

  function renderOverlays() {
    return html`
      <${TrackSheet}
        open=${sheet === "track"}
        p=${p}
        onClose=${() => setSheet(null)}
        onStarted=${(view) => {
          if (view) setView(view);
          setSheet(null);
          window.scrollTo({ top: 0 });
        }}
      />
      <${BudgetDialog} open=${sheet === "budget"} value=${p.budget} onClose=${() => setSheet(null)} onSave=${actions.onBudget} />
      <${AddItemDialog} open=${sheet === "add"} onClose=${() => setSheet(null)} onAdd=${actions.onAddItem} />
      <${DeleteDialog}
        open=${sheet === "delete"}
        p=${p}
        onClose=${() => setSheet(null)}
        onStart=${(on) => (deleting.current = on)}
        onDeleted=${(res) => {
          deleting.current = true;
          setSheet(null);
          forgetView(p.id);
          toast.success(ru(res && res.message_ru) || "Сборка удалена");
          if (res && res.warning_ru) toast.warning(ru(res.warning_ru));
          navigate("/projects", { replace: true });
        }}
      />
      <${GpuRankingSheet} open=${sheet === "ranking"} p=${p} onClose=${() => setSheet(null)} onDeal=${openDeal} onBought=${(slot, offer) => (setSheet(null), setBought({ slot, offer }))} />
      <${BoughtDialog} open=${Boolean(bought)} p=${p} slot=${bought && bought.slot} offer=${bought && bought.offer} onClose=${() => setBought(null)} onView=${setView} />
      <${AltSheet} open=${Boolean(alt)} p=${p} slot=${alt && alt.slot} alt=${alt && alt.alt} onClose=${() => setAlt(null)} onDeal=${openDeal} onPick=${actions.onPick} />
      ${!phone &&
      html`<${Drawer} open=${Boolean(deal)} onClose=${() => setDeal(null)} width=${560} class="deal-drawer" title=${deal ? deal.title : "Сделка"}>
        ${deal && html`<${DealView} key=${deal.ad_id} id=${deal.ad_id} mode="drawer" />`}
      <//>`}
    `;
  }
}

/** GPU first (the key part), then the plan order; inactive parts go to the quiet line. */
function sortedSlots(p) {
  const active = (p.slots || []).filter((s) => s.active);
  return [...active.filter((s) => s.kind === "gpu"), ...active.filter((s) => s.kind !== "gpu")];
}

function NameForm({ value, onSave, onCancel }) {
  const [v, setV] = useState(value);
  const [busy, setBusy] = useState(false);
  return html`<form
    class="pj-name-form"
    onSubmit=${async (e) => {
      e.preventDefault();
      if (!v.trim()) return;
      setBusy(true);
      await onSave(v.trim());
      setBusy(false);
    }}
  >
    <input class="pj-name-edit__input" value=${v} maxLength=${80} onInput=${(e) => setV(e.currentTarget.value)} onKeyDown=${(e) => e.key === "Escape" && onCancel()} aria-label="Название сборки" autoFocus />
    <${Button} type="submit" size="sm" variant="primary" loading=${busy} disabled=${!v.trim()}>Сохранить<//>
    <${Button} size="sm" variant="ghost" onClick=${onCancel}>Отмена<//>
  </form>`;
}

function BudgetDialog({ open, value, onClose, onSave }) {
  const [v, setV] = useState(value);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  useEffect(() => {
    if (open) {
      setV(value);
      setErr(null);
    }
  }, [open]);
  const save = async (next) => {
    if (next != null && (next < 1 || next > 100000)) return setErr("Можно от 1 до 100 000 €");
    setBusy(true);
    const ok = await onSave(next);
    setBusy(false);
    if (ok) onClose();
  };
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    title="Бюджет сборки"
    icon="wallet"
    size="sm"
    footer=${html`${value ? html`<${Button} variant="ghost" onClick=${() => save(null)} disabled=${busy}>Без бюджета<//>` : null}<span class="grow"></span><${Button} variant="ghost" onClick=${onClose}>Отмена<//><${Button} variant="primary" loading=${busy} onClick=${() => save(v)}>Сохранить<//>`}
  >
    <form
      onSubmit=${(e) => {
        e.preventDefault();
        save(v);
      }}
    >
      <${Field} label="Сколько готов потратить на всё" error=${err} help="Цели частей пересчитаю так, чтобы вся сборка уложилась в бюджет">
        ${(id) => html`<${NumberInput} id=${id} value=${v} onChange=${(x) => (setV(x), setErr(null))} min=${1} max=${100000} suffix="€" invalid=${Boolean(err)} data-autofocus />`}
      <//>
    </form>
  <//>`;
}

function DoneCard({ p, reload }) {
  const active = (p.searches_list || []).filter((s) => s.exists).length;
  return html`<section class="pj-done">
    <span class="pj-done__icon"><${Icon} name="package-check" size=${26} /></span>
    <div class="grow">
      <h2 class="pj-done__title">Всё собрано 🎉</h2>
      <p class="pj-done__text">Потрачено <b class="num">${money(p.spent || 0)}</b>${p.budget ? html` из <b class="num">${money(p.budget)}</b>` : ""}${p.budget && p.spent <= p.budget ? html` — <span class="t-green">уложился в бюджет</span>` : ""}</p>
    </div>
    ${active > 0 && html`<${Button} variant="secondary" icon="trash-2" onClick=${async () => (await deleteProjectSearches(p)) && reload()}>Удалить поиски сборки<//>`}
  </section>`;
}

function PageSkeleton() {
  return html`<div class="pj-screen pj-project" aria-busy="true">
    <${Skeleton} w=${80} h=${14} />
    <div class="pj-head"><${Skeleton} w=${44} h=${44} radius="var(--r-md)" /><div class="grow"><${Skeleton} w="45%" h=${26} /><${Skeleton} w="30%" h=${14} /></div></div>
    <${Skeleton} h=${150} radius="var(--r-lg)" />
    ${[0, 1, 2, 3].map((i) => html`<${Skeleton} key=${i} h=${120} radius="var(--r-lg)" />`)}
  </div>`;
}
