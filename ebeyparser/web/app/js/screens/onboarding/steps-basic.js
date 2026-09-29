// Onboarding steps 0–4: Welcome, Где, Что, Деньги, Для себя (brief §4.1.1–4.1.5).
import { html, cx, useEffect, useState } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate } from "../../lib/router.js";
import { loadApp } from "../../lib/app.js";
import { money, count } from "../../lib/format.js";
import { useStore } from "../../lib/store.js";
import { Icon, Button, Field, Input, NumberInput, Toggle, IconButton, Chip, Banner, ChoiceCards, toast } from "../../ui/index.js";
import { Logo } from "../../shell/shell.js";
import { LocationPicker, RadiusSlider, RadiusDisc } from "../../setup/where.js";
import { useCategories, CategoryGrid, LoadMeter } from "../../setup/what.js";
import { StrategyCards, ExampleBox, BudgetField, ManualPricing, PURPOSES, DEFAULT_PRESETS, detectStrategy } from "../../setup/money.js";
import { draftStore, updateDraft, pick } from "./draft.js";

// ------------------------------------------------------------------ 0. Welcome
export function WelcomeStep({ draft, onNext }) {
  const [demo, setDemo] = useState(false);
  const resume = draft && draft.last_step && draft.last_step !== "welcome";
  const loadDemo = async () => {
    setDemo(true);
    try {
      await api.post("/demo", {}, { timeout: 30000 });
      await loadApp();
      toast.success("Демо-данные загружены", { message: "Настроить по-настоящему можно в любой момент" });
      navigate("/");
    } catch (e) {
      toast.error(e.missing ? "Демо появится в следующем обновлении" : e.message);
    } finally {
      setDemo(false);
    }
  };
  const benefits = [
    { icon: "radar", tone: "green", text: "Смотрю все новые объявления в твоём районе 24/7" },
    { icon: "scan-eye", tone: "blue", text: "Проверяю фото и описание локальной нейросетью — бесплатно и приватно" },
    { icon: "send", tone: "amber", text: "Присылаю в Telegram только то, что правда выгодно, и сколько предложить" },
  ];
  return html`<div class="welcome">
    <div class="welcome__logo"><${Logo} size=${48} withText=${false} /></div>
    <h1 class="welcome__title">Привет! Найдём, что можно выгодно купить</h1>
    <p class="welcome__subtitle">Настройка займёт 3–5 минут. Всё можно поменять потом.</p>
    <ul class="welcome__benefits">
      ${benefits.map(
        (b, i) => html`<li style=${{ animationDelay: `${120 + i * 80}ms` }}>
          <span class=${cx("glyph", `glyph--${b.tone}`)}><${Icon} name=${b.icon} size=${20} /></span>
          <span>${b.text}</span>
        </li>`,
      )}
    </ul>
    <div class="welcome__actions">
      <${Button} variant="primary" size="lg" iconRight="arrow-right" onClick=${() => (resume ? navigate(`/welcome/${draft.last_step}`) : onNext())}>
        ${resume ? "Продолжить настройку" : "Начать"}
      <//>
      <${Button} variant="ghost" icon="flask-conical" loading=${demo} onClick=${loadDemo}>Сначала посмотреть на примере<//>
    </div>
    <p class="welcome__privacy"><${Icon} name="lock" size=${14} />Данные остаются только на твоём компьютере</p>
  </div>`;
}

// ------------------------------------------------------------------ 1. Где
export function WhereStep({ draft }) {
  const [radius, setRadius] = useState(draft.radius_km);
  useEffect(() => setRadius(draft.radius_km), [draft.radius_km]);
  return html`<div class="where">
    <div class="where__form">
      <${Field} label="Город или почтовый индекс" help="Можно район Берлина или индекс, например 10115">
        ${() => html`<${LocationPicker}
          value=${draft.location}
          label=${draft.location_label}
          autoFocus=${!draft.location}
          onChange=${(v) => updateDraft({ location: v.location, location_label: v.label, location_confirmed: v.confirmed })}
        />`}
      <//>
      <div class="field">
        <div class="field__top"><span class="field__label">Радиус поиска</span></div>
        <${RadiusSlider} value=${radius} onChange=${setRadius} onCommit=${(v) => updateDraft({ radius_km: v })} />
        <p class="field__help">Чем больше радиус — тем больше находок, но дальше ехать за покупкой.</p>
      </div>
    </div>
    <${RadiusDisc} km=${radius} city=${draft.location_label || draft.location} />
  </div>`;
}

// ------------------------------------------------------------------ 2. Что
export function WhatStep({ draft }) {
  const cats = useCategories(draft.location, draft.radius_km);
  const selected = draft.category_ids || [];
  useEffect(() => {
    // first visit: preselect the recommended categories
    if (draft.category_ids === null && cats.items.length) {
      updateDraft({ category_ids: cats.items.filter((c) => c.recommended).map((c) => c.id) });
    }
  }, [cats.items.length]);
  const toggle = (id, cat) =>
    updateDraft((d) => {
      const cur = d.category_ids || [];
      const on = cur.includes(id);
      return {
        category_ids: on ? cur.filter((x) => x !== id) : [...cur, id],
        category_names: cat && !cat.recommended ? { ...(d.category_names || {}), [id]: cat.name_de } : d.category_names,
      };
    });
  const recommended = cats.items.filter((c) => c.recommended).map((c) => c.id);
  const onlyRecommended = selected.length === recommended.length && recommended.every((id) => selected.includes(id));
  const wishes = (draft.wishlist || []).filter((w) => w.item).length;
  return html`<div class="what">
    <div class="what__toolbar">
      <span class="what__count">Выбрано: <b class="num">${selected.length}</b></span>
      <div class="row" style=${{ "--gap": "6px" }}>
        <${Chip} selected=${onlyRecommended} onClick=${() => updateDraft({ category_ids: recommended })}>Только рекомендуемые<//>
        <${Chip} onClick=${() => updateDraft({ category_ids: [] })} disabled=${!selected.length}>Снять все<//>
        <${Button} variant="ghost" size="sm" icon="refresh-cw" loading=${cats.refreshing} onClick=${cats.refresh} title="1–2 запроса к kleinanzeigen.de: сколько объявлений в каждой категории рядом с тобой">
          Сверить с сайтом
        <//>
      </div>
    </div>
    ${cats.error && cats.source !== "live" && cats.refreshing === false && cats.live === false && cats.error && html`<${Banner} tone="amber">Не получилось сверить с сайтом: ${cats.error}. Показываю встроенный список — он тоже подходит.<//>`}
    <${CategoryGrid} items=${cats.items} selected=${selected} onToggle=${toggle} loading=${cats.loading} counting=${cats.refreshing} />
    ${!selected.length &&
    html`<p class="what__hint"><${Icon} name="info" size=${16} />${wishes ? `Будут только поиски «для себя» (${count(wishes, "вещь", "вещи", "вещей")}).` : "Выбери хотя бы одну категорию — или добавь, что ищешь для себя, на следующем шаге."}</p>`}
    <div class="what__meter">
      <${LoadMeter} categories=${selected.length} keywords=${wishes} />
    </div>
  </div>`;
}

// ------------------------------------------------------------------ 3. Деньги
export function MoneyStep({ draft }) {
  const { options } = useStore(draftStore);
  const presets = (options && options.presets) || DEFAULT_PRESETS;
  const budgetRange = (options && options.budget_range) || { min: 50, max: 1500, step: 10 };
  const [manual, setManual] = useState(draft.strategy === "custom");
  const [budget, setBudget] = useState(draft.max_price || 400);
  useEffect(() => setBudget(draft.max_price || 400), [draft.max_price]);
  const setStrategy = (key) => updateDraft({ strategy: key, pricing: pick(presets[key]) });
  return html`<div class="money-step">
    <section class="onb-section">
      <h2 class="onb-section__title">Для чего ищешь</h2>
      <${ChoiceCards} value=${draft.purpose} onChange=${(v) => updateDraft({ purpose: v })} options=${PURPOSES} columns=${3} />
    </section>

    <section class="onb-section">
      <h2 class="onb-section__title">Сколько готов потратить на одну вещь</h2>
      <${BudgetField}
        value=${budget}
        min=${budgetRange.min}
        max=${budgetRange.max}
        step=${budgetRange.step}
        onChange=${(v) => {
          setBudget(v);
          updateDraft({ max_price: v });
        }}
      />
    </section>

    ${draft.purpose !== "personal" &&
    html`<section class="onb-section">
      <div class="onb-section__head">
        <h2 class="onb-section__title">Стратегия</h2>
        ${draft.strategy === "custom" && html`<span class="badge badge--blue badge--soft badge--sm">Свой</span>`}
      </div>
      <${StrategyCards} value=${draft.strategy} onChange=${setStrategy} presets=${presets} />
      <button type="button" class="disclosure" aria-expanded=${manual} onClick=${() => setManual(!manual)}>
        <${Icon} name=${manual ? "chevron-up" : "chevron-down"} size=${16} />Настроить вручную
      </button>
      ${manual &&
      html`<${ManualPricing}
        pricing=${draft.pricing}
        onChange=${(p) => updateDraft({ pricing: p, strategy: detectStrategy(p, presets) })}
      />`}
      <${ExampleBox} pricing=${draft.pricing} />
      <p class="fees-line">
        <${Icon} name="receipt" size=${14} />Комиссии при продаже: 0 % (частные продавцы не платят) ·
        <a href="/settings/money">Изменить</a>
      </p>
    </section>`}
  </div>`;
}

// ------------------------------------------------------------------ 4. Для себя
export function WishlistStep({ draft }) {
  const { options } = useStore(draftStore);
  const suggestions = (options && options.wishlist_suggestions) || [];
  const rows = draft.wishlist && draft.wishlist.length ? draft.wishlist : [];
  const setRows = (next) => updateDraft({ wishlist: next, wishlist_skipped: false });
  const add = (item = "", max_price = null) => setRows([...rows, { item, max_price, haggle: true }]);
  const update = (i, patch) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i) => setRows(rows.filter((_, j) => j !== i));
  useEffect(() => {
    if (!rows.length) add();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const used = new Set(rows.map((r) => (r.item || "").toLowerCase()));
  return html`<div class="wishlist">
    <div class="wish-rows">
      ${rows.map(
        (r, i) => html`<div class="wish-row" key=${i}>
          <div class="wish-row__main">
            <div class="wish-row__item">
              <${Input} value=${r.item} onChange=${(v) => update(i, { item: v })} placeholder="Что ищешь, например RTX 3090" aria-label="Что ищешь" icon="search" />
            </div>
            <div class="wish-row__price">
              <${NumberInput} value=${r.max_price} onChange=${(v) => update(i, { max_price: v })} prefix="до" suffix="€" placeholder="550" aria-label="Максимальная цена" />
            </div>
            <${IconButton} icon="x" label="Убрать" onClick=${() => remove(i)} />
          </div>
          <${Toggle} size="sm" checked=${r.haggle !== false} onChange=${(v) => update(i, { haggle: v })} label="Показывать и чуть дороже (+20 %) — для торга" />
        </div>`,
      )}
    </div>
    <${Button} variant="secondary" icon="plus" onClick=${() => add()}>Добавить<//>
    ${suggestions.length > 0 &&
    html`<div class="wish-suggest">
      <div class="t-overline">Популярное</div>
      <div class="row" style=${{ "--gap": "6px" }}>
        ${suggestions
          .filter((s) => !used.has(s.item.toLowerCase()))
          .map(
            (s) => html`<${Chip}
              key=${s.item}
              icon="plus"
              onClick=${() => {
                const empty = rows.findIndex((r) => !r.item);
                if (empty >= 0) update(empty, { item: s.item, max_price: s.max_price });
                else add(s.item, s.max_price);
              }}
              >${s.item} · ${money(s.max_price)}<//
            >`,
          )}
      </div>
    </div>`}
  </div>`;
}
