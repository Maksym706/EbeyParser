// Сборки — the list (spec §3): one card per build with its progress, best total against the
// budget and the next step. Live: `project_updated` replaces a card in place (no refetch).
import { html, cx, useEffect, useState } from "../../lib/html.js";
import { useIsMobile } from "../../lib/hooks.js";
import { navigate } from "../../lib/router.js";
import { useStore } from "../../lib/store.js";
import { plural } from "../../lib/format.js";
import { Icon, Button, PageHeader, EmptyState, ErrorState, Skeleton, Ring, Chip, toast } from "../../ui/index.js";
import { useTopbar } from "../../shell/topbar.js";
import { projectsStore, projectsApi, cacheView, ru, StatusChip, DealsSwitch, STATUS_ORDER, TEMPLATE_ICON } from "../../features/projects-common.js";
import { refreshProjects, badgeFrom } from "../../features/projects-live.js";

const STARTERS = [
  { key: "llm_48", label: "LLM-сервер", icon: "server" },
  { key: "nas", label: "NAS", icon: "hard-drive" },
  { key: "gaming_1080p", label: "Игровой ПК", icon: "gamepad-2" },
];

function sorted(cards) {
  // tracking first, then paused, drafts, done; the API already returns newest first inside a group
  return [...cards].sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9));
}

export default function ProjectsList() {
  const phone = useIsMobile();
  const cards = useStore(projectsStore, (s) => s.cards);
  const [error, setError] = useState(null);
  const load = () => {
    setError(null);
    return projectsApi.list().then(
      (res) => {
        projectsStore.set({ cards: (res && res.items) || [] });
        refreshProjects(); // badge
      },
      (e) => setError(e),
    );
  };
  useEffect(() => {
    load();
  }, []);
  useTopbar({ title: phone ? "Мои сделки" : "Сборки" }, [phone]);

  const items = cards ? sorted(cards) : null;
  let body;
  if (error && !cards) body = html`<${ErrorState} error=${error} title="Не удалось загрузить сборки" onRetry=${load} />`;
  else if (!items) body = html`<div class="pj-cards">${[0, 1, 2].map((i) => html`<${CardSkeleton} key=${i} />`)}</div>`;
  else if (!items.length) body = html`<${Empty} />`;
  else body = html`<div class="pj-cards">${items.map((c) => html`<${ProjectCard} key=${c.id} card=${c} />`)}</div>`;

  return html`<div class="pj-screen pj-list">
    ${phone && html`<${DealsSwitch} value="projects" count=${badgeFrom(cards)} />`}
    <${PageHeader}
      title="Сборки"
      subtitle="Собираю компьютеры из б/у деталей по лучшей цене"
      actions=${items && items.length ? html`<${Button} variant="primary" icon="plus" href="/projects/new">Новая сборка<//>` : null}
    />
    ${body}
  </div>`;
}

function Empty() {
  return html`<div class="pj-empty">
    <${EmptyState}
      icon="boxes"
      tone="brand"
      title="Собери компьютер из б/у деталей по лучшей цене"
      message="Скажи, что нужно — я составлю план, проверю совместимость и поймаю каждую деталь дешевле рынка"
    >
      <div class="pj-empty__cta"><${Button} variant="primary" icon="plus" href="/projects/new">Новая сборка<//></div>
      <div class="pj-empty__starters" role="group" aria-label="Шаблоны">
        <span class="pj-empty__or">или начни с шаблона</span>
        ${STARTERS.map((s) => html`<${Chip} key=${s.key} icon=${s.icon} onClick=${() => navigate(`/projects/new?template=${s.key}`)}>${s.label}<//>`)}
      </div>
    <//>
  </div>`;
}

function CardSkeleton() {
  return html`<div class="pj-card pj-card--sk" aria-hidden="true">
    <div class="pj-card__top"><${Skeleton} w=${40} h=${40} radius="var(--r-md)" /><div class="grow"><${Skeleton} w="60%" h=${16} /><${Skeleton} w="40%" h=${12} /></div></div>
    <${Skeleton} w="70%" h=${22} />
    <${Skeleton} w="50%" h=${12} />
  </div>`;
}

/** Primary button per next_step (spec §3). */
function NextStep({ card }) {
  const [busy, setBusy] = useState(false);
  const step = (card.next_step && card.next_step.key) || "wait";
  const href = `/projects/${card.id}`;
  if (step === "track") return html`<${Button} size="sm" variant="primary" icon="radar" href=${`${href}?track=1`}>Начать отслеживание<//>`;
  if (step === "resume")
    return html`<${Button}
      size="sm"
      variant="secondary"
      icon="play"
      loading=${busy}
      onClick=${async (e) => {
        e.stopPropagation();
        setBusy(true);
        try {
          cacheView(await projectsApi.patch(card.id, { status: "tracking" }));
          toast.success("Снова слежу за сборкой");
        } catch (err) {
          toast.error(err);
        } finally {
          setBusy(false);
        }
      }}
      >Продолжить<//
    >`;
  if (step === "buy") return html`<${Button} size="sm" variant="tinted" tone="green" icon="arrow-right" href=${card.next_step.slot ? `${href}#slot-${card.next_step.slot}` : href}>Посмотреть<//>`;
  return html`<${Button} size="sm" variant="secondary" href=${href}>Открыть<//>`;
}

function ProjectCard({ card }) {
  const over = card.over_budget > 0;
  const fits = card.fits_budget === true;
  const open = (e) => {
    if (e.target.closest("a, button")) return;
    navigate(`/projects/${card.id}`);
  };
  return html`<article class=${cx("pj-card", `is-${card.status}`)} onClick=${open}>
    <div class="pj-card__top">
      <span class="pj-card__icon" aria-hidden="true"><${Icon} name=${card.icon || TEMPLATE_ICON[card.template] || "boxes"} size=${20} /></span>
      <div class="pj-card__head">
        <a class="pj-card__name" href=${`/projects/${card.id}`}>${card.name}</a>
        <span class="pj-card__sub">${ru(card.template_label)}</span>
      </div>
      <${StatusChip} status=${card.status} label=${card.status_label} />
    </div>
    <div class="pj-card__money">
      <${Ring} value=${card.progress || 0} size=${44} stroke=${4} tone=${card.status === "done" ? "brand" : "neutral"}>
        <span class="num">${card.slots_done}</span>
      <//>
      <div class="pj-card__totals">
        <span class=${cx("pj-card__total num", fits && "t-green", over && "t-amber")}>${ru(card.total_label)}</span>
        <span class="pj-card__progress">${ru(card.progress_label)}${over ? html` · <span class="tag tone-haggle">над бюджетом</span>` : ""}</span>
      </div>
    </div>
    ${card.headline_ru && html`<div class="pj-card__headline tone-profit"><span class="sdot"></span><span>${ru(card.headline_ru)}</span></div>`}
    <div class="pj-card__foot">
      <${NextStep} card=${card} />
      <span class="pj-card__meta">
        ${card.alerts > 0 && html`<span><${Icon} name="bell" size=${13} />${card.alerts} ${plural(card.alerts, "уведомление", "уведомления", "уведомлений")}</span>`}
        ${card.updated_at_label && html`<span>обновлено ${card.updated_at_label}</span>`}
      </span>
    </div>
  </article>`;
}
