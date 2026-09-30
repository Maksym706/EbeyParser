// Route table (history API; the server answers every non-API path with index.html, and
// old "#/deal/123"-style links are converted on boot). Each screen is a module whose
// default export is a component receiving { params, query }. Screens load lazily.
//
// Frontend B: fill in the screen modules under js/screens/ — no need to edit this file
// unless you add a brand-new top-level route. Sub-pages go through params.rest:
//   /searches/new/category → searches.js with params.rest === "new/category"
//   /health/logs           → health.js   with params.rest === "logs"

export const routes = [
  { path: "/", nav: "feed", title: "Лента", load: () => import("./screens/feed.js") },
  // focus: on phones the deal is a full-screen page — its own top bar and action bar, no app bar / tab bar
  { path: "/deal/:id", nav: "feed", title: "Сделка", focus: true, load: () => import("./screens/deal.js") },
  { path: "/deals/:column?", nav: "deals", title: "Мои сделки", load: () => import("./screens/pipeline.js") },
  // «Сборки» (docs/design/PROJECTS.md): list, /projects/new, /projects/:id, /projects/:id/slot/:slot
  { path: "/projects/*", nav: "projects", title: "Сборки", load: () => import("./screens/projects/index.js") },
  { path: "/searches/*", nav: "searches", title: "Поиски", load: () => import("./screens/searches.js") },
  { path: "/health/*", nav: "health", title: "Состояние", load: () => import("./screens/health.js") },
  { path: "/settings/:section?", nav: "settings", title: "Настройки", load: () => import("./screens/settings/index.js") },
  { path: "/welcome/:step?", fullscreen: true, title: "Первая настройка", load: () => import("./screens/onboarding/index.js") },
  { path: "/dev/ui", title: "Дизайн-система", load: () => import("./screens/gallery.js") }, // component gallery (QA)
];

/** Old / alternative addresses → current ones (links from Telegram, the classic UI, the README). */
export const redirects = {
  "/feed": "/",
  "/pipeline": "/deals",
  "/setup": "/welcome",
  "/onboarding": "/welcome",
  "/status": "/health",
};

/**
 * Main navigation (sidebar on desktop, icon rail on tablets, bottom tab bar on phones).
 * `tab: false` = not in the phone tab bar (max 5); `parent` = the tab that is active instead.
 */
export const NAV = [
  { id: "feed", href: "/", label: "Лента", icon: "zap" },
  { id: "deals", href: "/deals", label: "Мои сделки", short: "Сделки", icon: "wallet" },
  { id: "projects", href: "/projects", label: "Сборки", icon: "boxes", tab: false, parent: "deals" },
  { id: "searches", href: "/searches", label: "Поиски", icon: "radar" },
  { id: "health", href: "/health", label: "Состояние", icon: "activity" },
  { id: "settings", href: "/settings", label: "Настройки", icon: "settings" },
];
