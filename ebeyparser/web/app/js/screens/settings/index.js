// Настройки (brief §4.7): desktop = sub-nav + content (max 720 px); phone = list of sections → section page.
// Route: /settings/:section?   Data: GET/PATCH /settings, PUT /secrets, /access, /data, integration tests.
import { html, cx } from "../../lib/html.js";
import { useIsMobile } from "../../lib/hooks.js";
import { useStore, appStore } from "../../lib/store.js";
import { Icon, ErrorState, Skeleton, PageHeader } from "../../ui/index.js";
import { useSettingsForm, useUnsavedGuard, SaveBar } from "./form.js";
import { RegionSection, MoneySection, AiSection } from "./sections-a.js";
import { NotificationsSection, EbaySection, AccessSection, DataSection, AboutSection } from "./sections-b.js";

export const SETTINGS_SECTIONS = [
  { key: "region", label: "Поиск и регион", icon: "map-pin", description: "Город, радиус, как часто проверять", component: RegionSection },
  { key: "money", label: "Деньги", icon: "wallet", description: "Стратегия, прибыль, комиссии", component: MoneySection },
  { key: "ai", label: "Нейросеть", icon: "scan-eye", description: "LM Studio, модель, второе мнение", component: AiSection },
  { key: "notifications", label: "Уведомления", icon: "bell", description: "Telegram, почта, что присылать", component: NotificationsSection },
  { key: "ebay", label: "eBay", icon: "gavel", description: "Ключи API и площадка", component: EbaySection },
  { key: "access", label: "Доступ с телефона", icon: "smartphone", description: "Wi-Fi, Tailscale, QR-код", component: AccessSection, standalone: true },
  { key: "data", label: "Данные и бэкап", icon: "database", description: "Размер базы, резервная копия", component: DataSection, standalone: true },
  { key: "about", label: "О программе", icon: "info", description: "Тема, версия, лицензии", component: AboutSection, standalone: true },
];

/** Old or guessed section slugs → real ones (links from notifications, older builds). */
const ALIASES = { search: "region", location: "region", where: "region", interval: "region", pricing: "money", notify: "notifications", telegram: "notifications", email: "notifications", backup: "data", access: "access", phone: "access" };

/** Amber dots in the sub-nav: what still needs setting up. */
function useAttention() {
  const app = useStore(appStore, (s) => s.app);
  const f = (app && app.features) || {};
  return { ai: app && !f.ai, notifications: app && !f.telegram && !f.email };
}

function SubNav({ current, attention }) {
  return html`<nav class="settings-nav" aria-label="Разделы настроек">
    ${SETTINGS_SECTIONS.map(
      (s) => html`<a key=${s.key} href=${`/settings/${s.key}`} class=${cx("settings-nav__link", current === s.key && "is-active")} aria-current=${current === s.key ? "page" : undefined}>
        <${Icon} name=${s.icon} size=${18} />
        <span>${s.label}</span>
        ${attention[s.key] && html`<span class="nav-dot nav-dot--amber settings-nav__dot" title="Не настроено"></span>`}
      </a>`,
    )}
  </nav>`;
}

function SectionList({ attention }) {
  return html`<div class="list settings-list">
    ${SETTINGS_SECTIONS.map(
      (s) => html`<a key=${s.key} class="list__row" href=${`/settings/${s.key}`}>
        <span class="glyph glyph--neutral glyph--sm"><${Icon} name=${s.icon} size=${16} /></span>
        <span class="list__main">
          <span class="list__title">${s.label}</span>
          <span class="list__desc">${s.description}</span>
        </span>
        ${attention[s.key] && html`<span class="nav-dot nav-dot--amber" style=${{ position: "static" }}></span>`}
        <${Icon} name="chevron-right" size=${18} class="list__chevron" />
      </a>`,
    )}
  </div>`;
}

export default function SettingsScreen({ params }) {
  const phone = useIsMobile();
  const form = useSettingsForm();
  useUnsavedGuard(form);
  const attention = useAttention();
  const key = (params.section && (ALIASES[params.section] || params.section)) || (phone ? null : "region");
  const section = SETTINGS_SECTIONS.find((s) => s.key === key);

  if (phone && !section) {
    return html`<div class="settings">
      <${PageHeader} title="Настройки" subtitle="Всё меняется мышкой — без файлов и команд" />
      <${SectionList} attention=${attention} />
    </div>`;
  }

  const Section = section ? section.component : null;
  let body;
  if (!section) body = html`<${ErrorState} title="Такого раздела нет" error=${{ message: "Выбери раздел слева." }} />`;
  else if (section.standalone) body = html`<${Section} form=${form} />`;
  else if (form.loadError) body = html`<${ErrorState} error=${form.loadError} onRetry=${form.reload} />`;
  else if (!form.draft) body = html`<div class="sgroup"><${Skeleton} w="40%" h=${18} /><${Skeleton} count=${4} h=${40} radius="12px" /></div>`;
  else body = html`<${Section} form=${form} />`;

  const readOnly = form.settings && form.settings.editable === false;
  return html`<div class="settings">
    ${!phone && html`<${SubNav} current=${key} attention=${attention} />`}
    <div class="settings-main">
      <header class="settings-head">
        ${phone && html`<a class="page-header__back" href="/settings"><${Icon} name="chevron-left" size=${18} />Настройки</a>`}
        <h1 class="settings-head__title">${section ? section.label : "Настройки"}</h1>
        ${section && html`<p class="settings-head__desc">${section.description}</p>`}
      </header>
      ${readOnly && html`<div class="banner banner--amber mb-5" role="note"><span class="banner__icon"><${Icon} name="lock" size=${18} /></span><div class="banner__body"><div class="banner__text">Не могу сохранить настройки: нет доступа к файлу настроек.</div></div></div>`}
      <div class="settings-body" key=${key}>${body}</div>
      ${!(section && section.standalone) && html`<${SaveBar} form=${form} />`}
    </div>
  </div>`;
}
