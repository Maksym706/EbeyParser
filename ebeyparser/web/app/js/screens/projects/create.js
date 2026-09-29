// Новая сборка — /projects/new (spec §4): step 1 «Что собираем?» (template cards, the goal in own
// words, budget, AI), step 2 the plan preview (POST /projects/plan is a dry run — nothing is saved
// until «Сохранить…»). A variant pick or a «Исправить» in the preview saves a draft first: only a
// saved build can be re-checked by the server (the plan preview takes no `choices`).
import { html, cx, useState, useEffect, useRef } from "../../lib/html.js";
import { useAsync, useIsMobile } from "../../lib/hooks.js";
import { navigate } from "../../lib/router.js";
import { money } from "../../lib/format.js";
import { Icon, Button, Field, Textarea, NumberInput, Slider, Toggle, Input, Skeleton, Banner, EmptyState, toast } from "../../ui/index.js";
import { useTopbar } from "../../shell/topbar.js";
import { projectsApi, cacheView, diffChosen, sideChangesText, shortOption, ru, handoff, lower, TEMPLATE_ICON } from "../../features/projects-common.js";
import { PlanBody } from "../../features/projects-plan.js";

const GOAL_HELP = "Например: „сервер для нейросетей, 70B в Q4, бюджет 1500 €“ или „ThinkPad T480 до 200 €, док-станция, монитор 27 дюймов — бюджет 400 €“";
const SLOW_AFTER = 20000;

export default function CreateProject({ query = {} }) {
  const phone = useIsMobile();
  const tq = useAsync(() => projectsApi.templates(), []);
  const templates = (tq.data && tq.data.items) || [];
  const ai = (tq.data && tq.data.ai) || { available: false };
  const [template, setTemplate] = useState(query.template || null);
  const [goal, setGoal] = useState("");
  const [budget, setBudget] = useState(null);
  const [useAi, setUseAi] = useState(true);
  const [rows, setRows] = useState([{ label: "", target_price: null, qty: 1 }]);
  const [errors, setErrors] = useState({});
  const [state, setState] = useState({ step: "form", loading: false, ai: false, slow: false, error: null });
  const [preview, setPreview] = useState(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(null);
  const ctrl = useRef(null);
  const slowTimer = useRef(null);
  const custom = template === "custom";

  useTopbar({ title: "Новая сборка" }, []);
  // a template from the URL fills the budget once the list is here
  useEffect(() => {
    if (!template || budget != null) return;
    const t = templates.find((x) => x.key === template);
    if (t && t.default_budget) setBudget(t.default_budget);
  }, [tq.data]);
  useEffect(() => () => ctrl.current && ctrl.current.abort(), []);

  const pickTemplate = (key) => {
    const next = template === key ? null : key;
    setTemplate(next);
    setErrors({});
    const t = templates.find((x) => x.key === next);
    if (t && t.default_budget && budget == null) setBudget(t.default_budget);
  };

  const run = async (withAi) => {
    const items = custom ? rows.filter((r) => r.label.trim()).map((r) => ({ label: r.label.trim(), query: r.label.trim(), target_price: r.target_price || null, qty: r.qty || 1 })) : [];
    if (!goal.trim() && !template) return setErrors({ goal: "Опиши цель или выбери шаблон" });
    if (custom && !items.length && !goal.trim()) return setErrors({ rows: "Добавь хотя бы одну вещь" });
    if (budget != null && (budget < 1 || budget > 100000)) return setErrors({ budget: "Можно от 1 до 100 000 €" });
    setErrors({});
    if (ctrl.current) ctrl.current.abort();
    const c = new AbortController();
    ctrl.current = c;
    const body = { goal: goal.trim(), use_ai: Boolean(withAi) };
    if (budget != null) body.budget = budget;
    if (template) body.template = template;
    if (items.length) body.items = items;
    clearTimeout(slowTimer.current);
    setState({ step: "form", loading: true, ai: Boolean(withAi), slow: false, error: null });
    if (withAi) slowTimer.current = setTimeout(() => setState((s) => (s.loading ? { ...s, slow: true } : s)), SLOW_AFTER);
    try {
      const plan = await projectsApi.plan(body, { signal: c.signal, timeout: withAi ? 150000 : 30000 });
      if (c.signal.aborted) return;
      setPreview(plan);
      setName(plan.name || "");
      setState({ step: "preview", loading: false, ai: false, slow: false, error: null });
      window.scrollTo({ top: 0 });
    } catch (e) {
      if (c.signal.aborted) return;
      if (e.status === 422 && e.fields && Object.keys(e.fields).length) {
        setErrors({ goal: e.field("goal"), budget: e.field("budget"), template: e.field("template"), rows: e.field("items") });
        setState({ step: "form", loading: false, ai: false, slow: false, error: null });
      } else setState({ step: "form", loading: false, ai: false, slow: false, error: e });
    } finally {
      clearTimeout(slowTimer.current);
    }
  };
  const submit = () => run(ai.available && useAi);
  const withoutAi = () => run(false);

  // ---------------------------------------------------------------- save (spec §4.2)
  const save = async ({ choices = {}, track = false, flash = null, what } = {}) => {
    const body = { plan: preview.plan, choices };
    if (name.trim() && name.trim() !== preview.name) body.name = name.trim();
    const view = await projectsApi.create(body);
    cacheView(view);
    handoff.flash = flash;
    handoff.track = track;
    if (what) {
      const others = diffChosen(preview, view, Object.keys(choices));
      toast.success(what(view), { message: [sideChangesText(others), "Сохранил как черновик — поиски ещё не созданы"].filter(Boolean).join(". ") });
    } else if (!track) toast.success("Сборка сохранена", { message: "Черновик — поиски появятся, когда нажмёшь «Начать отслеживание»" });
    navigate(`/projects/${view.id}`, { replace: true, scroll: Boolean(track) || !flash });
    return view;
  };
  const guard = async (key, fn) => {
    if (busy) return false;
    setBusy(key);
    try {
      await fn();
      return true;
    } catch (e) {
      toast.error(e);
      return false;
    } finally {
      setBusy(null);
    }
  };
  const pickInPreview = (slotKey, optionKey) =>
    guard(`${slotKey}:${optionKey}`, () =>
      save({
        choices: { [slotKey]: optionKey },
        flash: slotKey,
        what: (view) => {
          const s = (view.slots || []).find((x) => x.key === slotKey);
          return `${s ? s.label : "Часть"}: ${ru(shortOption(s ? s.chosen_label : optionKey))}`;
        },
      }),
    );
  const fixInPreview = (fix) =>
    guard(`${fix.slot}:${fix.option}`, () =>
      save({
        choices: { [fix.slot]: fix.option },
        flash: fix.slot,
        what: () => `Исправил: ${lower(ru(fix.label_ru))}`,
      }),
    );

  // ---------------------------------------------------------------- render
  if (tq.error && !tq.data)
    return html`<div class="pj-screen"><${EmptyState} icon="circle-alert" tone="danger" title="Не получилось открыть создание сборки" message=${tq.error.message} action=${html`<${Button} icon="refresh-cw" onClick=${() => tq.reload()}>Ещё раз<//>`} /></div>`;

  if (state.step === "preview" && preview)
    return html`<div class="pj-screen pj-project pj-preview">
      <a class="page-header__back" href="/projects"><${Icon} name="chevron-left" size=${18} />Сборки</a>
      <header class="pj-head">
        <span class="pj-head__icon" aria-hidden="true"><${Icon} name=${preview.icon || "boxes"} size=${22} /></span>
        <div class="pj-head__titles">
          <label class="pj-name-edit">
            <span class="sr-only">Название сборки</span>
            <input class="pj-name-edit__input" value=${name} maxLength=${80} onInput=${(e) => setName(e.currentTarget.value)} aria-label="Название сборки" />
            <${Icon} name="pencil" size=${16} />
          </label>
          <p class="pj-head__sub">${ru(preview.template_label)}${preview.budget ? ` · бюджет ${money(preview.budget)}` : ""}</p>
        </div>
        <span class="pj-status tone-neutral"><${Icon} name="eye" size=${12} />Предпросмотр</span>
      </header>
      <${PlanBody} plan=${preview} mode="preview" handlers=${{ onPick: pickInPreview, onFix: fixInPreview }} busy=${busy} />
      <div class="savebar pj-savebar" role="region" aria-label="Сохранить сборку">
        <div class="savebar__inner">
          <${Button} variant="ghost" icon="arrow-left" aria-label="Назад к цели" class="pj-savebar__back" onClick=${() => setState({ ...state, step: "form" })}>${phone ? null : "Назад"}<//>
          <span class="savebar__text">${phone ? "Ещё не сохранено" : "План ещё не сохранён"}</span>
          <div class="savebar__actions">
            <${Button} variant="ghost" loading=${busy === "draft"} disabled=${Boolean(busy)} onClick=${() => guard("draft", () => save())}>${phone ? "Черновик" : "Сохранить черновик"}<//>
            <${Button} variant="primary" icon="radar" loading=${busy === "track"} disabled=${Boolean(busy)} onClick=${() => guard("track", () => save({ track: true }))}>
              ${phone ? "Сохранить и следить" : "Сохранить и начать отслеживание"}
            <//>
          </div>
        </div>
      </div>
    </div>`;

  const example = (templates.find((t) => t.key === (template || "llm_48")) || {}).example_goal || "";
  return html`<div class="pj-screen pj-create">
    <a class="page-header__back" href="/projects"><${Icon} name="chevron-left" size=${18} />Сборки</a>
    <header class="pj-create__head">
      <h1 class="page-header__title">Новая сборка</h1>
      <p class="page-header__subtitle">Скажи, что нужно — я составлю план, проверю совместимость и поймаю каждую деталь дешевле рынка</p>
    </header>
    <section class="pj-create__card" aria-label="Что собираем">
      <h2 class="pj-create__title">Что собираем?</h2>
      <div class="pj-tpls" role="radiogroup" aria-label="Шаблоны">
        ${!tq.data
          ? [0, 1, 2, 3, 4].map((i) => html`<div key=${i} class="pj-tpl pj-tpl--sk"><${Skeleton} w=${36} h=${36} radius="var(--r-md)" /><${Skeleton} w="80%" h=${14} /><${Skeleton} w="60%" h=${12} /></div>`)
          : templates.map(
              (t) => html`<button type="button" key=${t.key} role="radio" aria-checked=${template === t.key} class=${cx("pj-tpl", template === t.key && "is-selected")} onClick=${() => pickTemplate(t.key)}>
                <span class="pj-tpl__icon"><${Icon} name=${t.icon || TEMPLATE_ICON[t.key] || "boxes"} size=${20} /></span>
                <span class="pj-tpl__title">${t.title}</span>
                <span class="pj-tpl__sub">${t.subtitle}</span>
                <span class="pj-tpl__check" aria-hidden="true"><${Icon} name="circle-check" size=${20} /></span>
              </button>`,
            )}
      </div>
      ${errors.template && html`<p class="field__error" role="alert"><${Icon} name="circle-alert" size=${14} />${errors.template}</p>`}

      ${custom
        ? html`<${ItemsEditor} rows=${rows} setRows=${setRows} error=${errors.rows} />`
        : html`<${Field} label=${template ? "Уточни своими словами" : "Или опиши своими словами"} optional=${Boolean(template)} error=${errors.goal} help=${GOAL_HELP}>
            ${(id) => html`<${Textarea} id=${id} value=${goal} onChange=${(v) => (setGoal(v), errors.goal && setErrors({ ...errors, goal: null }))} rows=${3} maxLength=${2000} placeholder=${example} invalid=${Boolean(errors.goal)} />`}
          <//>`}

      <div class="pj-budget">
        <${Field} label="Бюджет" optional error=${errors.budget} help="Можно не указывать, если бюджет уже в цели">
          ${(id) => html`<div class="pj-budget__row">
            <div class="pj-budget__slider">
              <${Slider} value=${budget ?? 1000} min=${100} max=${5000} step=${50} onChange=${(v) => setBudget(v)} format=${(v) => money(v)} label="Бюджет" tone="green" marks=${false} />
            </div>
            <div class="pj-budget__input"><${NumberInput} id=${id} value=${budget} onChange=${setBudget} min=${1} max=${100000} suffix="€" invalid=${Boolean(errors.budget)} placeholder="1 500" /></div>
          </div>`}
        <//>
      </div>

      <div class="pj-ai-row">
        ${ai.available
          ? html`<span class="pj-ai-chip tone-info"><${Icon} name="bot" size=${14} />Нейросеть поможет понять цель своими словами</span>
              <${Toggle} checked=${useAi} onChange=${setUseAi} label="С нейросетью" size="sm" />`
          : html`<span class="pj-ai-chip"><${Icon} name="bot" size=${14} />Нейросеть выключена — составлю план по правилам</span>
              <a class="linkish" href="/settings/ai">Включить</a>`}
      </div>

      ${state.error &&
      html`<${Banner} tone="danger" title="Не получилось составить план" details=${state.error.details} action=${html`<${Button} size="sm" icon="refresh-cw" onClick=${submit}>Ещё раз<//>`}>
        ${state.error.message}
      <//>`}

      <div class="pj-create__actions">
        <${Button} variant="primary" size=${phone ? "lg" : "md"} block=${phone} iconRight="arrow-right" loading=${state.loading} onClick=${submit}>Составить план<//>
      </div>
    </section>
    ${state.loading && state.ai && html`<${PlanWaiting} slow=${state.slow} onSkip=${withoutAi} />`}
  </div>`;
}

/** The AI can take up to 2 minutes: skeleton + an honest label + «без нейросети» after 20 s. */
function PlanWaiting({ slow, onSkip }) {
  return html`<section class="pj-waiting" aria-live="polite" aria-busy="true">
    <p class="pj-waiting__label"><${Icon} name="bot" size=${18} /><span>Нейросеть читает цель и выбирает детали…</span></p>
    ${slow &&
    html`<div class="pj-waiting__slow">
      <span>Можно подождать или продолжить без нейросети</span>
      <${Button} size="sm" variant="ghost" icon="chevrons-right" onClick=${onSkip}>Продолжить без нейросети<//>
    </div>`}
    <div class="pj-waiting__sk">
      <${Skeleton} h=${120} radius="var(--r-lg)" />
      <${Skeleton} h=${72} radius="var(--r-lg)" />
      <${Skeleton} h=${140} radius="var(--r-lg)" />
      <${Skeleton} h=${140} radius="var(--r-lg)" />
    </div>
  </section>`;
}

/** «Свой список»: [что ищешь] [до … €] [× шт] [×] + «Добавить». */
function ItemsEditor({ rows, setRows, error }) {
  const set = (i, patch) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return html`<div class="pj-items">
    <span class="field__label">Что нужно купить</span>
    ${rows.map(
      (r, i) => html`<div class="pj-items__row" key=${i}>
        <div class="pj-items__label"><${Input} value=${r.label} onChange=${(v) => set(i, { label: v })} placeholder="что ищешь — как пишут в объявлениях" aria-label=${`Вещь ${i + 1}`} maxLength=${80} invalid=${Boolean(error) && i === 0 && !r.label.trim()} /></div>
        <div class="pj-items__price"><${NumberInput} value=${r.target_price} onChange=${(v) => set(i, { target_price: v })} min=${1} max=${100000} prefix="до" suffix="€" aria-label="До скольки евро" /></div>
        <div class="pj-items__qty"><${NumberInput} value=${r.qty} onChange=${(v) => set(i, { qty: v })} min=${1} max=${20} prefix="×" suffix="шт" aria-label="Сколько штук" /></div>
        <button type="button" class="pj-items__rm" aria-label="Убрать" disabled=${rows.length === 1} onClick=${() => setRows(rows.filter((_, j) => j !== i))}><${Icon} name="x" size=${16} /></button>
      </div>`,
    )}
    ${error ? html`<p class="field__error" role="alert"><${Icon} name="circle-alert" size=${14} />${error}</p>` : html`<p class="field__help">Пиши так, как продавцы называют вещь: „ThinkPad T480“, „Monitor 27 Zoll“</p>`}
    <div><${Button} size="sm" variant="ghost" icon="plus" disabled=${rows.length >= 10} onClick=${() => setRows([...rows, { label: "", target_price: null, qty: 1 }])}>Добавить<//></div>
  </div>`;
}
