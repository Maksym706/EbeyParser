# EbeyParser web app (SPA)

This is the browser UI served by FastAPI at `/`. It is **Preact + htm as native ES modules**. There is no build
step, no Node and no CDN: everything is vendored in this folder and works offline. The product rules are in
`docs/design/DESIGN_BRIEF.md`; the tokens and components below implement its §6.

## File layout

```
index.html              import map, theme pre-boot, CSS links, PWA manifest link
manifest.webmanifest    "Add to home screen" (icons in icons/)
vendor/                 preact 10.29.8, preact/hooks, htm 3.1.1, uqr 0.1.3 (QR) + LICENSE-*.txt
fonts/                  Inter (latin, latin-ext, cyrillic subsets; opsz+wght) + LICENSE-inter.txt
css/
  tokens.css            ALL design tokens, light + dark (brief §6.1–6.6). Never hard-code colours.
  base.css              reset, type scale (.t-display/.t-h1/.t-h2/.t-h3/.t-overline/.price-xl…), utilities
  components.css        styles of the ui/ components
  shell.css             sidebar / rail / app bar / tab bar / offline banner / check-ad dialog
  onboarding.css        onboarding + shared setup widgets (Frontend A)
  settings.css          settings screen (Frontend A)
  screens.css           feed, deal, pipeline, searches, health (Frontend B)
js/
  main.js               boot: loads /app, onboarding gate, route rendering, live-event toasts
  routes.js             route table + redirects + main NAV
  lib/                  html.js, api.js, store.js, router.js, events.js, hooks.js, format.js,
                        theme.js, tones.js, links.js, app.js
  ui/                   component library (import everything from ui/index.js)
  shell/                app chrome: shell.js, topbar.js (useTopbar), monitor.js, checklink.js
  setup/                integration widgets shared by onboarding & settings (where, what, money,
                        ai, telegram, mail-ebay)
  screens/              one module per route (default export = screen component)
    onboarding/, settings/        Frontend A
    feed.js, deal.js, pipeline.js, searches.js, health.js (+ subfolders)   Frontend B
  features/             Frontend B's shared pieces (deal card, composer, charts …)
```

Ownership: Frontend A owns `lib/`, `ui/`, `shell/`, `setup/`, core CSS, `index.html`, onboarding and settings.
Frontend B owns its screens, `features/` and `css/screens.css`. If you need a generic component, build it in
`features/` first; it can be promoted to `ui/` later.

## Writing a screen

```js
// js/screens/searches.js  → route "/searches/*" (see routes.js)
import { html, useState } from "../lib/html.js";
import { api } from "../lib/api.js";
import { useAsync } from "../lib/hooks.js";
import { useTopbar } from "../shell/topbar.js";
import { PageHeader, Button, Card, Skeleton, EmptyState, ErrorState, toast } from "../ui/index.js";

export default function SearchesScreen({ params, query }) {
  const { data, error, loading, reload } = useAsync(() => api.get("/searches"), []);
  useTopbar({ actions: html`<${Button} variant="primary" icon="plus" href="/searches/new">Новый поиск<//>` }, []);
  if (error) return html`<${ErrorState} error=${error} onRetry=${reload} />`;
  if (loading && !data) return html`<${Skeleton} variant="card" />`;
  return html`<${PageHeader} title="Поиски" subtitle="Что и где я ищу" /> …`;
}
```

- **Props:** `params` holds the path params (`:id`, and `rest` for `*` routes such as `/health/*`).
  `query` is the parsed query string.
- **Links:** use plain `<a href="/deal/123">`. Same-origin clicks are routed without a reload (not `/api`,
  `/app`, `/classic`, `target=_blank` or `data-native`).
  In code, use `navigate("/x")`, `navigate(url, { replace: true })` and `setQuery({ f: "haggle" })` from
  `lib/router.js`.
- **Routes** (history API; the server returns `index.html` for every non-API path, so reloads and Telegram links
  work):

  | Route | Screen |
  |---|---|
  | `/` | feed |
  | `/deal/:id` | deal |
  | `/deals/:column?` | pipeline |
  | `/searches/*` | searches |
  | `/health/*` | health |
  | `/settings/:section?` | settings |
  | `/welcome/:step?` | onboarding |

  Old links such as `/pipeline`, `/setup`, `/status` and `/#/deal/1` redirect to the new routes. To add a
  top-level route, add one line to `routes.js`.
- **Title / top bar:** the route title shows in the top bar and the document title. `useTopbar({ search, actions,
  title }, deps)` from `shell/topbar.js` adds a desktop search field (`{ value, onChange, placeholder }`,
  focused by `/`) and page actions. It clears itself when the screen unmounts.
- **Layout:** the shell provides the gutter and the max width (1440). Breakpoints (hooks `useIsMobile()` /
  `useMediaQuery(BREAKPOINTS.tablet)`):
  - phone < 600: bottom tabs, bottom sheets
  - tablet 600–1023: icon rail
  - desktop ≥ 1024: sidebar
  - wide ≥ 1280
- **Errors & states:** every screen has four designed states (brief §5). Use:
  - `Skeleton` while loading
  - `EmptyState` when there is nothing to show
  - `ErrorState` (it shows a calm "скоро будет" placeholder when the endpoint is missing, `error.missing`)
  - `Banner` for partial / stale data

## Talking to the backend: `lib/api.js`

```js
api.get("/deals", { params: { verdict: "buy", limit: 30 } }) // path is relative to /api/v1
api.post("/deals/123/reevaluate", {})         // → 202 {job}; then:
const result = await api.waitJob(job.id, { onProgress })
api.patch("/deals/123", { status: "starred" })
api.put("/secrets", {...}); api.del("/demo")
api.url("/backup", { include_secrets: 1 })     // for <a href download>
```

- Errors are thrown as `ApiError` with:
  - `.message`: Russian text you can show as is. It passes through `humanize()` (a safety net): a text that
    still carries a CLI command, a config file name, an exception class or a local URL becomes a calm Russian
    sentence with a next step
  - `.details`: the technical text (the server's `error.details` or what `humanize()` replaced). Show it only
    collapsed: `<Details text=${e.details} />`, `Banner details=…`, `TestResult details=…`,
    `toast.error(e)` (adds «Подробнее» by itself)
  - `.status`, `.code`
  - `.fields`: `{ "pricing.min_profit": "не меньше 0" }`; use `err.field("min_profit")` for one field
  - `.missing`: the endpoint doesn't exist
  - `.network`: the program isn't reachable
- Network failures also flip `appStore.connection` to `"offline"`, and the shell shows the offline banner by
  itself. Don't add your own.
- Texts from non-throwing results (`{ ok: false, error_ru }`, run errors, health problems) go through
  `humanize(localizeText(text))` → `{ message, details }` before they are shown.
- Auth: same-origin cookie. A `?token=` in the page URL is kept in sessionStorage and sent as
  `X-EbeyParser-Token`.
- `useAsync(fn, deps)` returns `{ data, error, loading, reload, setData }`. Also available: `useDebounced`,
  `useDebouncedCallback`, `useInterval` (pauses in hidden tabs), `useNow`, `useLocalState`, `useMediaQuery`.

## Global state: `lib/store.js`

- `createStore(initial)` returns a store; `useStore(store, selector)` re-renders only when the selected slice
  changes.
- `appStore` fields:
  - `app`: GET /app
  - `monitor`: normalised GET /monitor
  - `connection`
  - `live`: SSE state
  - `newDeals`
  - `badges`
- Nav badges:
  - The feed badge is `newDeals`. Reset it when the feed is visible with `appStore.set({ newDeals: 0 })`.
  - Others: `appStore.set({ badges: { deals: 3, health: "dot-red", settings: "dot-amber" } })`.
- `refreshMonitor()` and `monitorAction("run" | "pause" | "resume")` live in `shell/monitor.js`.
  `loadApp()` in `lib/app.js` reloads `/app`.

## Live updates: `lib/events.js`

`onEvent("deal_found", (data) => …)` returns an unsubscribe function, so it is usable straight from
`useEffect`. The SSE connection to `/api/v1/events` is opened once by `main.js` and reconnects with backoff.

- Server events: `ready`, `run_started`, `run_finished`, `deal_found {ad_id, verdict, action, score, card}`,
  `deal_updated {ad_id, card}`, `health_alert`, `settings_changed`, `searches_changed`, `monitor_paused`,
  `monitor_resumed`, `job_finished`.
- Local pseudo-event: `connected`.
- `onEvent("*", ({ type, data }) => …)` receives every event.
- `main.js` already shows the "Новая выгодная находка" toast and bumps `newDeals`.

## Component library: `ui/index.js`

| Group | Components |
|---|---|
| Actions | `Button` (variant `primary · secondary · ghost · danger · danger-ghost · tinted` + `tone`, `link`; size `sm 32 · md 40 · lg 48`; `icon`, `iconRight`, `loading`, `block`, `href`), `IconButton` (`label` required → aria + tooltip; `badge`), `CopyButton` (check-mark feedback) |
| Display | `Card`, `CardHeader`, `Badge` (tone, `soft · solid · outline`, sizes), `Chip` (filter chip; selected = ink + check), `Glyph` (tinted icon tile), `Avatar`, `Money` (tabular, `sign`, `tone="auto"`), `StatusDot` (pulse), `Kbd`, `Divider`, `ExternalLink`, `Icon`, `QrCode` |
| Forms | `Field` (label, help, error, optional; render-prop `(id) => …`), `Input` (`icon`, `prefix`, `suffix`, `trailing`), `NumberInput` (comma decimals, returns numbers/null; `min`/`max` are **never clamped** — an out-of-range value shows «Можно от 0 до 50 %» under the field; `showFieldErrors(root)` before saving), `SecretInput` (eye toggle, "сохранён" placeholder), `Textarea`, `Select`, `Toggle` (switch; with `label`/`description` → full row), `Checkbox`, `Slider` (discrete `steps` with ticks, `bubble="drag·always·none"`, `marks`, `tone`), `Segmented`, `NumberStepper`, `Autocomplete`, `ChipInput` (word lists), `ChoiceCards` (radio cards), `SettingRow` (label left, control right), `TestResult` (loading/ok/warn/fail line for «Проверить»), `SaveState` |
| Feedback | `toast(...)` / `toast.success/error/warning/info/promise`, `Banner` (tones, `details`), `Details` (collapsed «Подробнее»), `Progress` (determinate / indeterminate), `Ring`, `Meter` (load bar: green < 60 %, amber, red > 85 %, `marker`), `Checklist`, `Steps`, `Skeleton` (`variant="card"·"row"`), `EmptyState`, `ErrorState` |
| Overlays | `Modal` (bottom sheet on phones), `Drawer` (right drawer ≥ 600 px, bottom sheet with drag-to-close on phones; `modal={false}` = side panel without scrim, `restoreFocus(prev)` = where focus goes on close), `await confirm({ title, message, confirmLabel, tone: "danger" })`, `Tooltip`, `HelpTip` («?» popover, click/touch), `Portal` |
| Layout | `PageHeader`, `Section`, `KeyValue`, `Stat`, `Tabs` (`line · pills`) |

Details:

- **Tones:** components accept the brief's hues (`green · amber · violet · red · blue · neutral`) or the meanings
  (`profit · haggle · bid · danger · info`). `lib/tones.js` has `hue()`, `verdictTone()` and `DECISION_ICONS`.
- **Toasts:** use `toast({ kind: "deal" | "success" | …, title, message, action: { label, href | onClick },
  action2, details, duration })`. The stack moves itself above docked controls (drawer / sheet / dialog
  footers, the phone deal action bar, the settings save bar, the tab bar, the onboarding footer) and beside
  an open side drawer, so a toast never covers a button.
  - Durations: 4 s plain, 6 s with an action, 8 s for errors.
  - At most 3 are shown at once, newest on top.
  - For undo: `toast.success("Скрыто", { action: { label: "Отменить", onClick: undo } })`.
- **Icons:** `<Icon name="gavel" size={16} />` renders a Lucide glyph (210 in `ui/icons.js`; brief names such
  as `trash-2` and `circle-help` are aliased).
  - To add icons at runtime: `import { ICONS } from "../ui/icons.js"; ICONS["name"] = '<path …/>'` (see
    `features/icons-extra.js`).
  - Sizes: 14 meta, 16 buttons/chips, 20 nav, 24 tab bar.
- **Formatting (`lib/format.js`):**
  - `money(v, { sign, cents })` → «1 250 €» / «−35 €»
  - `percent(0.38)` → «38 %»
  - `plural`, `count(3, "поиск", "поиска", "поисков")`
  - `ago`, `until`, `span`, `dateTime`, `bytes`
  - every time is **Europe/Berlin**, whatever the browser's zone: `clockTime` «21:34», `whenTime` «завтра в 08:10»,
    `untilTime` «завтра 03:10» (for «пауза до …»), `dateShort` «15.09», `berlinDay` (for `<input type=date>`),
    `localizeText` (ISO timestamps inside server texts → «21:34»). Never print raw ISO; prefer a server
    `*_label` field when there is one
  - `everyLabel(30)` → «каждые 30 мин»
  - `radiusLabel`
- **Theme:** `lib/theme.js` exports `setTheme("system" | "light" | "dark")`, `toggleTheme()` and `themeStore`.
  `index.html` applies the theme before first paint.

## CSS conventions

- **Classes:** BEM-ish: `.block`, `.block__part`, `.block--variant`, state classes `.is-active`,
  `.is-selected`, `.is-open`. Screen styles go into `css/screens.css`, prefixed per screen (`.feed-…`,
  `.deal-…`).
- **Colour:** tokens only: `--bg`, `--surface-1..3`, `--border(-strong)`, `--text/-2/-3/-disabled`,
  `--ink/--on-ink`, and `--{green,amber,violet,red,blue}-{bg,border,solid,text,on-solid}`.
- **Other tokens:**
  - Spacing: `--s-1 … --s-11` (2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64 px).
  - Radii: `--r-xs … --r-xl`, `--r-full`.
  - Motion: `--d-fast/base/enter/exit`, `--ease-standard/enter/exit`.
  - Shadows: `--shadow-sm/md/lg/toast`.
  - Z-index: `--z-*`.
- **Typography:** prices and counters use `.num` / `.money` (tabular numbers). Prefer the type classes
  (`.t-h2`, `.t-caption`, `.price-lg`) or the `--fs-*` / `--lh-*` tokens.
- **Touch and motion:** 44 px touch targets on coarse pointers (the tokens and `@media (pointer: coarse)` rules
  handle controls; small inline controls get an invisible `::after` hit extender). All animations respect
  `prefers-reduced-motion` globally; JS scrolling uses `behavior: "smooth"` only when motion is allowed.
- **Ids:** use `useId` from `lib/html.js` (unique across render roots — Portals are separate roots, where
  Preact's own `useId` repeats and `<label for>` would point at the page underneath).

## Testing & QA

- **Component gallery:** open `/dev/ui` (not in the navigation). It shows every component in the current theme and
  is the quickest check after a CSS change.
- `tests/test_spa.py` checks:
  - SPA serving at `/` and deep links
  - content types
  - that every relative import resolves
  - that bare imports are in the import map
  - vendored files and licences
  - that no URL outside `ALLOWED_LINK_HOSTS` (user-facing links only; keep them in `lib/links.js`) appears in
    JS/CSS/HTML
- Syntax check without a browser: `node --check file.js`, if Node is around. Users never need Node.
- Demo data for screenshots: open the app, then onboarding → «Сначала посмотреть на примере» (`POST /api/v1/demo`),
  or run `python -m ebeyparser demo`.
