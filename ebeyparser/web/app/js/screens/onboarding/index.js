// First-run onboarding (brief §4.1): full screen, 9 steps, resumable, «Дальше» works on every screen.
// Route: /welcome/:step?   Steps: welcome · where · what · money · wishlist · ai · telegram · ebay · done
import { html, cx, useEffect, useRef, useState } from "../../lib/html.js";
import { navigate } from "../../lib/router.js";
import { useStore } from "../../lib/store.js";
import { api } from "../../lib/api.js";
import { useMediaQuery } from "../../lib/hooks.js";
import { Icon, IconButton, Button, Glyph, Skeleton, confirm } from "../../ui/index.js";
import { Logo } from "../../shell/shell.js";
import { draftStore, loadDraft, updateDraft } from "./draft.js";
import { WelcomeStep, WhereStep, WhatStep, MoneyStep, WishlistStep } from "./steps-basic.js";
import { AiStep, TelegramStep, EbayStep } from "./steps-connect.js";
import { DoneStep } from "./done.js";

export const STEPS = [
  { key: "welcome", component: WelcomeStep },
  { key: "where", title: "Где искать", subtitle: "Город и сколько готов ехать за покупкой", icon: "map-pin", component: WhereStep },
  { key: "what", title: "Что искать", subtitle: "Категории, где чаще всего бывают выгодные вещи", icon: "layout-grid", component: WhatStep },
  { key: "money", title: "Деньги", subtitle: "Сколько тратить и насколько выгодным должно быть предложение", icon: "wallet", component: MoneyStep },
  { key: "wishlist", title: "Для себя", heading: "Ищешь что-то для себя?", subtitle: "Например, железо для AI-сервера. Буду следить и скажу, когда цена ниже рынка.", icon: "heart", optional: true, component: WishlistStep },
  { key: "ai", title: "Нейросеть", subtitle: "Смотрит фото и описание — бесплатно и прямо на твоём компьютере", icon: "scan-eye", tone: "blue", optional: true, component: AiStep },
  { key: "telegram", title: "Telegram", subtitle: "Самый быстрый способ узнать о находке — сообщение с фото прямо в телефон", icon: "send", tone: "blue", optional: true, component: TelegramStep },
  { key: "ebay", title: "eBay", heading: "Добавить eBay?", subtitle: "Аукционы и «Sofort-Kaufen» с eBay.de — через официальный API, бесплатно. Можно пропустить.", icon: "gavel", tone: "violet", optional: true, component: EbayStep },
  { key: "done", title: "Готово", heading: "Всё готово", subtitle: "Проверь, что получилось, и запускай", icon: "rocket", component: DoneStep },
];
const COUNTED = STEPS.length - 1; // «Шаг 3 из 8»: the welcome screen is not counted

/** Whether a step is finished (for the stepper check marks). */
function isDone(key, d) {
  if (!d) return false;
  switch (key) {
    case "where":
      return Boolean(d.location && d.location_confirmed);
    case "what":
      return Boolean(d.category_ids && d.category_ids.length) || (d.wishlist || []).some((w) => w.item);
    case "money":
      return Boolean(d.money_seen);
    case "wishlist":
      return (d.wishlist || []).some((w) => w.item) || d.wishlist_skipped;
    case "ai":
      return d.ai.done || d.ai.skipped;
    case "telegram":
      return d.telegram.done || d.telegram.linked || d.telegram.skipped || (d.email && d.email.done);
    case "ebay":
      return d.ebay.done || d.ebay.skipped;
    default:
      return false;
  }
}

/** Can the user press «Дальше» (and with what hint when not)? */
function gate(key, d) {
  if (key === "where" && !(d.location && d.location.trim())) return "Укажи город или почтовый индекс";
  if (key === "where" && !d.location_confirmed) return "Выбери город из подсказок";
  return null;
}

function Stepper({ index, draft, onGo }) {
  return html`<nav class="onb-stepper" aria-label="Шаги настройки">
    <ol>
      ${STEPS.slice(1).map((s, i) => {
        const n = i + 1;
        const done = isDone(s.key, draft) && n !== index;
        const current = n === index;
        const reachable = n <= index || done || isDone(STEPS[n - 1].key, draft);
        const skipped = s.optional && draft && draft[s.key] && draft[s.key].skipped;
        return html`<li key=${s.key} class=${cx("onb-stepper__item", current && "is-current", done && "is-done")}>
          <button type="button" disabled=${!reachable || current} onClick=${() => onGo(s.key)} aria-current=${current ? "step" : undefined}>
            <span class="onb-stepper__dot">${done ? html`<${Icon} name="check" size=${12} stroke=${3} />` : n}</span>
            <span class="onb-stepper__text">
              <span class="onb-stepper__label">${s.title}</span>
              ${s.optional && html`<span class="onb-stepper__hint">${skipped ? "пропущено" : "необязательно"}</span>`}
            </span>
          </button>
        </li>`;
      })}
    </ol>
  </nav>`;
}

export default function OnboardingScreen({ params }) {
  const { draft, loaded } = useStore(draftStore);
  const phone = useMediaQuery("(max-width: 599px)");
  const key = params.step || "welcome";
  const index = Math.max(0, STEPS.findIndex((s) => s.key === key));
  const step = STEPS[index];
  const [busy, setBusy] = useState(false);
  const [dir, setDir] = useState(1);

  useEffect(() => {
    if (!loaded) loadDraft();
  }, [loaded]);
  const scroller = useRef(null);
  useEffect(() => {
    if (STEPS[index].key !== key) navigate("/welcome", { replace: true });
    if (key !== "welcome" && draft) updateDraft({ last_step: key });
    if (scroller.current) scroller.current.scrollTop = 0; // every step starts at the top
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const go = (k) => {
    const to = STEPS.findIndex((s) => s.key === k);
    setDir(to >= index ? 1 : -1);
    navigate(k === "welcome" ? "/welcome" : `/welcome/${k}`);
  };
  const next = () => go(STEPS[Math.min(STEPS.length - 1, index + 1)].key);
  const back = () => go(STEPS[Math.max(0, index - 1)].key);

  const skip = async () => {
    if (step.key === "ai" && !draft.ai.done) {
      const ok = await confirm({
        title: "Продолжить без нейросети?",
        message: "Без нейросети фото никто не проверит — уведомления придут с пометкой ⚠ «фото не проверены». Включить можно потом в настройках.",
        confirmLabel: "Продолжить без нейросети",
        cancelLabel: "Настроить",
        icon: "scan-eye",
      });
      if (!ok) return;
    }
    const part = step.key === "wishlist" ? { wishlist_skipped: true } : { [step.key]: { ...(draft[step.key] || {}), skipped: true } };
    updateDraft(part);
    if (["wishlist", "ai", "telegram", "ebay"].includes(step.key)) {
      api.post("/onboarding/skip", { step: step.key, skipped: true }).catch(() => {});
    }
    next();
  };

  const onNext = async () => {
    if (step.key === "money") updateDraft({ money_seen: true });
    // optional steps: «Дальше» without finishing = skip (with the AI warning)
    if (step.optional && !isDone(step.key, draft) && !(step.key === "wishlist" && (draft.wishlist || []).some((w) => w.item))) return skip();
    next();
  };

  if (!loaded || !draft) {
    return html`<div class="onb onb--loading"><div class="onb-card"><div class="onb-main"><div class="onb-content">
      <${Skeleton} w=${48} h=${48} radius="14px" /><${Skeleton} w="50%" h=${26} /><${Skeleton} w="70%" /><${Skeleton} h=${120} radius="16px" />
    </div></div></div></div>`;
  }

  const Comp = step.component;
  if (step.key === "welcome") {
    return html`<div class="onb onb--welcome"><${Comp} draft=${draft} onNext=${next} /></div>`;
  }

  const blocked = gate(step.key, draft);
  const isLast = step.key === "done";
  const nextLabel = "Дальше";
  return html`<div class="onb">
    <div class="onb-card">
      <aside class="onb-rail">
        <div class="onb-rail__logo"><${Logo} size=${28} /></div>
        <${Stepper} index=${index} draft=${draft} onGo=${go} />
        <div class="onb-rail__foot">
          <${Icon} name="lock" size=${14} />
          Всё хранится только на этом компьютере
        </div>
      </aside>
      <section class="onb-main">
        <header class="onb-progress">
          ${phone && html`<${IconButton} icon="chevron-left" label="Назад" size="sm" onClick=${back} />`}
          <div class="onb-progress__text">Шаг ${index} из ${COUNTED}</div>
          ${step.optional && phone && !isDone(step.key, draft) && html`<button type="button" class="onb-progress__skip" onClick=${skip}>Пропустить</button>`}
          <div class="onb-progress__bar"><span style=${{ width: `${(index / COUNTED) * 100}%` }}></span></div>
        </header>
        <div class="onb-scroll" ref=${scroller}>
          <div class=${cx("onb-content", dir < 0 && "is-back")} key=${step.key}>
            <div class="onb-head">
              <${Glyph} icon=${step.icon} tone=${step.tone || "green"} />
              <h1 class="onb-head__title">${step.heading || step.title}</h1>
              <p class="onb-head__subtitle">${step.subtitle}</p>
            </div>
            <${Comp} draft=${draft} onNext=${next} go=${go} busy=${busy} setBusy=${setBusy} />
          </div>
        </div>
        ${!isLast &&
        html`<footer class="onb-footer">
          ${!phone && html`<${Button} variant="ghost" icon="arrow-left" onClick=${back}>Назад<//>`}
          <span class="onb-footer__hint">${blocked || ""}</span>
          ${step.optional && !phone && !isDone(step.key, draft) && html`<${Button} variant="ghost" onClick=${skip}>${step.key === "telegram" ? "Настрою позже" : "Пропустить"}<//>`}
          <${Button} variant="primary" size=${phone ? "lg" : "md"} block=${phone} iconRight="arrow-right" disabled=${Boolean(blocked)} onClick=${onNext}>${nextLabel}<//>
        </footer>`}
      </section>
    </div>
  </div>`;
}
