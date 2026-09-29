// App chrome (brief §3.1): sidebar ≥1024 px (collapsible to an icon rail), icon rail 600–1023 px,
// top app bar + bottom tab bar < 600 px. Plus the offline banner and the "check an ad" dialog.
import { html, cx, useState, useEffect } from "../lib/html.js";
import { appStore, useStore } from "../lib/store.js";
import { themeStore, toggleTheme } from "../lib/theme.js";
import { navigate } from "../lib/router.js";
import { useMediaQuery, useLocalState } from "../lib/hooks.js";
import { NAV } from "../routes.js";
import { Icon, IconButton, Button, Tooltip } from "../ui/index.js";
import { MonitorCard, MonitorPill } from "./monitor.js";
import { CheckLinkModal, looksLikeAdUrl } from "./checklink.js";
import { topbarStore } from "./topbar.js";

export function Logo({ size = 32, withText = true }) {
  return html`<span class="logo">
    <svg class="logo__mark" width=${size} height=${size} viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <linearGradient id="logo-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#4ade80" />
          <stop offset="1" stop-color="#16a34a" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#logo-g)" />
      <path d="M9.5 21.5 14 17l3 3 6-6.5" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M18.5 13.2h4.8V18" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="10.2" cy="10.2" r="1.9" fill="#fff" fill-opacity=".9" />
    </svg>
    ${withText && html`<span class="logo__text">Ebey<b>Parser</b></span>`}
  </span>`;
}

/** Badge per nav item: number (count) or "dot-red" / "dot-amber". */
function useNavBadges() {
  const newDeals = useStore(appStore, (s) => s.newDeals);
  const badges = useStore(appStore, (s) => s.badges);
  return { feed: newDeals || 0, ...(badges || {}) };
}

function NavBadge({ value, variant }) {
  if (!value) return null;
  if (typeof value === "string" && value.startsWith("dot")) return html`<span class=${cx("nav-dot", value === "dot-red" ? "nav-dot--red" : "nav-dot--amber")}></span>`;
  return html`<span class=${cx("nav-badge", variant)}>${value > 99 ? "99+" : value}</span>`;
}

function Sidebar({ nav, collapsed, onToggle }) {
  const badges = useNavBadges();
  const app = useStore(appStore, (s) => s.app);
  const { theme } = useStore(themeStore);
  return html`<aside class=${cx("sidebar", collapsed && "is-collapsed")} aria-label="Главное меню">
    <div class="sidebar__head">
      <a class="sidebar__brand" href="/" aria-label="EbeyParser — лента"><${Logo} size=${30} withText=${!collapsed} /></a>
    </div>
    <nav class="sidebar__nav">
      ${NAV.map(
        (item) => html`<a key=${item.id} href=${item.href} class=${cx("side-link", nav === item.id && "is-active")} aria-current=${nav === item.id ? "page" : undefined} title=${item.label}>
          <span class="side-link__icon"><${Icon} name=${item.icon} size=${20} /><${NavBadge} value=${badges[item.id]} /></span>
          <span class="side-link__label">${item.label}</span>
        </a>`,
      )}
    </nav>
    <div class="sidebar__bottom">
      ${app && app.demo && app.demo.loaded && html`<a class="sidebar__demo" href="/settings/data" title="Показаны демо-данные"><${Icon} name="flask-conical" size=${14} /><span>Демо-данные</span></a>`}
      <${MonitorCard} compact=${collapsed} />
      <div class="sidebar__tools">
        <${IconButton} size="sm" icon=${theme === "dark" ? "sun" : "moon"} label=${theme === "dark" ? "Светлая тема" : "Тёмная тема"} onClick=${toggleTheme} />
        <span class="sidebar__version">${app && app.version ? `v${app.version}` : ""}</span>
        <${IconButton} size="sm" class="sidebar__collapse" icon=${collapsed ? "chevrons-right" : "chevrons-left"} label=${collapsed ? "Развернуть меню" : "Свернуть меню"} onClick=${onToggle} />
      </div>
    </div>
  </aside>`;
}

function TabBar({ nav }) {
  const badges = useNavBadges();
  return html`<nav class="tabbar" aria-label="Разделы">
    ${NAV.map(
      (item) => html`<a key=${item.id} href=${item.href} class=${cx("tab", nav === item.id && "is-active")} aria-current=${nav === item.id ? "page" : undefined}>
        <span class="tab__icon"><${Icon} name=${item.icon} size=${24} stroke=${nav === item.id ? 2.2 : 1.9} /><${NavBadge} value=${badges[item.id]} /></span>
        <span class="tab__label">${item.short || item.label}</span>
      </a>`,
    )}
  </nav>`;
}

function TopSearch({ search }) {
  return html`<label class="topsearch">
    <${Icon} name="search" size=${16} />
    <input
      type="search"
      value=${search.value ?? ""}
      placeholder=${search.placeholder || "Найти…"}
      aria-label=${search.label || search.placeholder || "Поиск"}
      onInput=${(e) => search.onChange && search.onChange(e.currentTarget.value)}
      data-topsearch
    />
    <kbd class="kbd">/</kbd>
  </label>`;
}

function TopBar({ title, onCheck, phone }) {
  const slot = useStore(topbarStore);
  const newDeals = useStore(appStore, (s) => s.newDeals);
  const shownTitle = slot.title || title;
  if (phone) {
    return html`<header class="appbar">
      <h1 class="appbar__title">${shownTitle}</h1>
      <${MonitorPill} compact />
      <${IconButton} icon="scan-search" label="Проверить объявление" onClick=${() => onCheck("")} />
    </header>`;
  }
  return html`<header class="topbar">
    <h1 class="topbar__title">${shownTitle}</h1>
    ${slot.search && html`<${TopSearch} search=${slot.search} />`}
    <div class="topbar__spacer"></div>
    ${slot.actions && html`<div class="topbar__actions">${slot.actions}</div>`}
    <${Button} variant="secondary" icon="scan-search" class="topbar__check" onClick=${() => onCheck("")} title="Вставь ссылку на объявление — посчитаю выгоду (Ctrl+K)">
      <span class="hide-md">Проверить объявление</span>
    <//>
    <${Tooltip} text=${newDeals ? `Новых находок: ${newDeals}` : "Новых находок нет"} placement="bottom-end">
      <${IconButton}
        icon="bell"
        label="Новые находки"
        badge=${newDeals}
        onClick=${() => {
          appStore.set({ newDeals: 0 });
          navigate("/");
        }}
      />
    <//>
  </header>`;
}

export function OfflineBanner() {
  const connection = useStore(appStore, (s) => s.connection);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    // don't flash the banner for a single hiccup
    if (connection === "ok") return setVisible(false);
    const t = setTimeout(() => setVisible(true), 1200);
    return () => clearTimeout(t);
  }, [connection]);
  if (!visible || connection === "ok") return null;
  const auth = connection === "auth";
  return html`<div class=${cx("offline", auth && "offline--auth")} role="alert">
    <${Icon} name=${auth ? "lock" : "wifi-off"} size=${18} />
    <span class="offline__text">
      ${auth
        ? "Нужен ключ доступа: открой ссылку с ?token=… из окна программы на компьютере."
        : "Нет связи с программой — показываю то, что было загружено. Переподключаюсь сам…"}
    </span>
    ${!auth && html`<${Button} size="sm" variant="ghost" icon="refresh-cw" onClick=${() => window.location.reload()}>Обновить<//>`}
  </div>`;
}

/** Page frame around a routed screen. */
export function Shell({ nav, title, children }) {
  const phone = useMediaQuery("(max-width: 599px)");
  const rail = useMediaQuery("(min-width: 600px) and (max-width: 1023px)");
  const [collapsedPref, setCollapsed] = useLocalState("ebp-sidebar-collapsed", false);
  const collapsed = rail || collapsedPref;
  const [check, setCheck] = useState({ open: false, url: "" });
  const openCheck = (url) => setCheck({ open: true, url: url || "" });
  useEffect(() => {
    const typing = () => /input|textarea|select/i.test(document.activeElement && document.activeElement.tagName);
    const onKey = (e) => {
      if (e.key === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        openCheck("");
      } else if (e.key === "/" && !typing()) {
        const input = document.querySelector("[data-topsearch]");
        if (input) {
          e.preventDefault();
          input.focus();
        }
      }
    };
    const onPaste = (e) => {
      // pasting an ad URL anywhere outside a text field opens the check dialog
      if (typing()) return;
      const text = (e.clipboardData || window.clipboardData).getData("text");
      if (looksLikeAdUrl(text)) openCheck(text.trim());
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("paste", onPaste);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("paste", onPaste);
    };
  }, []);
  return html`<div class=${cx("shell", collapsed && "shell--rail", phone && "shell--phone")}>
    ${!phone && html`<${Sidebar} nav=${nav} collapsed=${collapsed} onToggle=${() => setCollapsed(!collapsedPref)} />`}
    <div class="shell__main">
      <${OfflineBanner} />
      <${TopBar} title=${title} onCheck=${openCheck} phone=${phone} />
      <main class="content" id="main">${children}</main>
    </div>
    ${phone && html`<${TabBar} nav=${nav} />`}
    <${CheckLinkModal} open=${check.open} initialUrl=${check.url} onClose=${() => setCheck({ open: false, url: "" })} />
  </div>`;
}
